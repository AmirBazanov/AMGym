"""The dashboard «Дела Амира» beyond its workouts: GET/PUT /api/hub/state, the site on /hub/ and
POST /api/hub/deploy (gymbot.services.hub)."""

import asyncio
import json
import re
import subprocess

import httpx
import pytest
from alembic import command
from conftest import make_settings
from pydantic import ValidationError
from sqlalchemy import inspect, text

from gymbot.api.app import create_app
from gymbot.db import migrate
from gymbot.db.session import make_engine
from gymbot.services import hub, live
from gymbot.services.users import get_or_create_user

READ = "hub-read"
WRITE = "hub-write"
DEPLOY = "hub-deploy"
ORIGIN = "https://amirbazanov.github.io"
URL = f"/api/hub/state?token={READ}"
W = {"X-Hub-Write-Token": WRITE}
UPDATED_AT = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")


def client(tmp_path, db, **kw) -> httpx.AsyncClient:
    opts = {"hub_token": READ, "hub_write_token": WRITE, "hub_dir": tmp_path / "my-hub"} | kw
    app = create_app(make_settings(tmp_path, **opts), db)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def owner(db) -> int:
    async with db() as s:
        user = await get_or_create_user(s, 42, "Amir")
        await s.commit()
        return user.id


# ---- state ----


async def test_get_without_state(tmp_path, db):
    async with client(tmp_path, db) as c:
        assert (await c.get(URL)).json() == {"version": 0, "updated_at": None, "state": {}}  # no owner yet
        await owner(db)
        r = await c.get(URL)
    assert r.status_code == 200
    assert r.json() == {"version": 0, "updated_at": None, "state": {}}
    assert r.headers["cache-control"] == "no-store"


async def test_put_then_get_and_conflict(tmp_path, db, monkeypatch):
    uid = await owner(db)
    published: list[tuple] = []
    monkeypatch.setattr(live, "publish", lambda *a: published.append(a))
    state = {"done": {"t1": True}, "journal": [{"date": "2026-10-29", "text": "ок"}]}
    async with client(tmp_path, db) as c:
        r = await c.put(URL, json={"base_version": 0, "state": state}, headers=W)
        assert r.status_code == 200
        first = r.json()
        assert first["version"] == 1 and first["state"] == state
        assert UPDATED_AT.match(first["updated_at"])
        assert (await c.get(URL)).json() == first

        r = await c.put(URL, json={"base_version": 1, "state": {"done": {}}}, headers=W)
        assert r.status_code == 200 and r.json()["version"] == 2
        second = r.json()

        # A device still on version 1 (or a second first save) gets the stored state and changes nothing.
        for base in (1, 0, 7):
            r = await c.put(URL, json={"base_version": base, "state": {"stale": True}}, headers=W)
            assert r.status_code == 409
            assert r.json() == second
        assert (await c.get(URL)).json() == second
    assert published == [(uid, "hub"), (uid, "hub")]


async def test_concurrent_saves_one_wins(tmp_path, db):
    await owner(db)
    async with client(tmp_path, db) as c:
        rs = await asyncio.gather(*(
            c.put(URL, json={"base_version": 0, "state": {"n": n}}, headers=W) for n in range(4)
        ))
        assert sorted(r.status_code for r in rs) == [200, 409, 409, 409]
        assert (await c.get(URL)).json()["version"] == 1


async def test_put_errors(tmp_path, db):
    await owner(db)
    body = {"base_version": 0, "state": {}}
    async with client(tmp_path, db) as c:
        assert (await c.put(URL, json=body)).status_code == 403  # no write token
        assert (await c.put(URL, json=body, headers={"X-Hub-Write-Token": "nope"})).status_code == 403
        assert (await c.put("/api/hub/state?token=nope", json=body, headers=W)).status_code == 403
        assert (await c.get("/api/hub/state?token=nope")).status_code == 403
        big = {"base_version": 0, "state": {"x": "я" * hub.STATE_MAX_BYTES}}
        assert (await c.put(URL, json=big, headers=W)).status_code == 413
        for bad in ([1], {"base_version": 0, "state": [1]}, {"base_version": 0, "state": "x"},
                    {"base_version": -1, "state": {}}, {"base_version": True, "state": {}}, {"state": {}}):
            r = await c.put(URL, json=bad, headers=W)
            assert r.status_code == 422, bad
        r = await c.put(URL, content=b"{not json", headers=W | {"Content-Type": "application/json"})
        assert r.status_code == 422
        assert (await c.get(URL)).json()["version"] == 0
    async with client(tmp_path, db, hub_write_token="") as c:
        assert (await c.put(URL, json=body, headers=W)).status_code == 404
        assert (await c.get(URL)).status_code == 200  # reading still works
    async with client(tmp_path, db, hub_token="") as c:
        assert (await c.get(URL)).status_code == 404


async def test_state_cors(tmp_path, db):
    await owner(db)
    async with client(tmp_path, db) as c:
        r = await c.options(URL, headers={"Origin": ORIGIN, "Access-Control-Request-Method": "PUT",
                                          "Access-Control-Request-Headers": "content-type,x-hub-write-token"})
        assert r.status_code == 204
        assert r.headers["access-control-allow-origin"] == ORIGIN
        assert set(r.headers["access-control-allow-methods"].replace(" ", "").split(",")) == {"GET", "PUT", "OPTIONS"}
        assert "x-hub-write-token" in r.headers["access-control-allow-headers"].lower()
        r = await c.put(URL, json={"base_version": 0, "state": {}}, headers=W | {"Origin": "http://localhost:8765"})
        assert r.headers["access-control-allow-origin"] == "http://localhost:8765"
        r = await c.get(URL, headers={"Origin": "https://evil.example"})
        assert "access-control-allow-origin" not in r.headers
        r = await c.options(URL, headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "PUT"})
        assert "access-control-allow-origin" not in r.headers


# ---- the site on /hub/ ----


def site(tmp_path):
    root = tmp_path / "my-hub"
    for name, body in {
        "index.html": "<!doctype html><title>hub</title>", "app.js": "console.log(1)", "style.css": "a{}",
        ".git/config": "[core]", ".claude/settings.json": "{}", "docs/notes.md": "# private",
        ".env": "X=1", "sub/.secret": "s",
    }.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(body)
    return root


async def test_site_files_and_cache(tmp_path, db):
    site(tmp_path)
    async with client(tmp_path, db) as c:
        r = await c.get("/hub")
        assert r.status_code in (307, 308) and r.headers["location"] == "/hub/"
        r = await c.get("/hub/")
        assert r.status_code == 200 and "<title>hub</title>" in r.text
        assert r.headers["cache-control"] == "no-cache"
        assert (await c.get("/hub/index.html")).headers["cache-control"] == "no-cache"
        r = await c.get("/hub/app.js?v=3")
        assert r.status_code == 200 and r.text == "console.log(1)"
        assert "cache-control" not in r.headers and r.headers["etag"]
        for path in ("/hub/.git/config", "/hub/.claude/settings.json", "/hub/docs/notes.md", "/hub/.env",
                     "/hub/%2Egit/config", "/hub/sub/.secret", "/hub/../.env", "/hub/docs/"):
            assert (await c.get(path)).status_code == 404, path


async def test_site_missing_or_off(tmp_path, db):
    async with client(tmp_path, db) as c:  # HUB_DIR not cloned yet
        assert (await c.get("/hub/")).status_code == 404
        assert (await c.get("/hub/app.js")).status_code == 404
    async with client(tmp_path, db, hub_dir="") as c:
        r = await c.get("/hub/data/gym.db")
        assert r.status_code == 404


def test_hub_dir_must_be_absolute_and_outside_the_app(tmp_path):
    assert make_settings(tmp_path, hub_dir="").hub_dir is None
    with pytest.raises(ValidationError):
        make_settings(tmp_path, hub_dir="data")
    with pytest.raises(ValidationError):
        make_settings(tmp_path, hub_dir="/")


# ---- deploy ----


def deploy_client(tmp_path, db, **kw) -> httpx.AsyncClient:
    return client(tmp_path, db, hub_deploy_token=DEPLOY, **kw)


async def test_deploy(tmp_path, db, monkeypatch):
    calls = []

    async def fake_pull(hub_dir, repo, ssh_key=None):
        calls.append((hub_dir, repo))
        return {"commit": "abc123", "changed": True}

    monkeypatch.setattr(hub, "pull", fake_pull)
    async with deploy_client(tmp_path, db) as c:
        assert (await c.post("/api/hub/deploy")).status_code == 403
        assert (await c.post("/api/hub/deploy", headers={"X-Hub-Deploy-Token": "nope"})).status_code == 403
        assert calls == []
        r = await c.post("/api/hub/deploy", headers={"X-Hub-Deploy-Token": DEPLOY})
        assert r.status_code == 200 and r.json() == {"commit": "abc123", "changed": True}
        assert calls == [(tmp_path / "my-hub", "https://github.com/AmirBazanov/my-hub.git")]
        r = await c.post("/api/hub/deploy", headers={"X-Hub-Deploy-Token": DEPLOY})  # within 10 s
        assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1
        assert len(calls) == 1
    async with client(tmp_path, db) as c:  # HUB_DEPLOY_TOKEN empty
        assert (await c.post("/api/hub/deploy", headers={"X-Hub-Deploy-Token": ""})).status_code == 404


async def test_deploy_git_error(tmp_path, db, monkeypatch):
    async def failing(hub_dir, repo, ssh_key=None):
        raise hub.GitError("fatal: Not possible to fast-forward, aborting.")

    monkeypatch.setattr(hub, "pull", failing)
    async with deploy_client(tmp_path, db) as c:
        r = await c.post("/api/hub/deploy", headers={"X-Hub-Deploy-Token": DEPLOY})
    assert r.status_code == 500
    assert "fast-forward" in r.json()["detail"]


def sh(*args, cwd=None):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


def commit(repo, name, text):
    (repo / name).write_text(text)
    sh("git", "add", name, cwd=repo)
    sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", name, cwd=repo)


async def test_pull_clones_then_fast_forwards(tmp_path):
    """Real git against a local repository: the first call clones, the next ones pull."""
    origin = tmp_path / "origin"
    origin.mkdir()
    sh("git", "init", "-q", "-b", "main", cwd=origin)
    commit(origin, "index.html", "v1")
    target = tmp_path / "my-hub"

    first = await hub.pull(target, str(origin))
    assert first["changed"] is True and (target / "index.html").read_text() == "v1"
    assert (await hub.pull(target, str(origin))) == {"commit": first["commit"], "changed": False}
    commit(origin, "index.html", "v2")
    second = await hub.pull(target, str(origin))
    assert second["changed"] is True and second["commit"] != first["commit"]
    assert (target / "index.html").read_text() == "v2"
    with pytest.raises(hub.GitError):
        await hub.pull(tmp_path / "other", str(tmp_path / "missing"))


async def test_git_timeout(monkeypatch):
    with pytest.raises(hub.GitError, match="timed out"):
        await hub.git("-c", "alias.wait=!sleep 5", "wait", timeout=0.2)


def test_state_out_roundtrip():
    assert hub.state_out(None) == {"version": 0, "updated_at": None, "state": {}}
    assert json.loads(json.dumps(hub.state_out(None))) == hub.state_out(None)


async def test_migration_0016_round_trip(tmp_path):
    engine, _ = make_engine(f"sqlite+aiosqlite:///{tmp_path}/m.db")

    async def migrate_to(target: str):
        def fn(conn):
            cfg = migrate._config()
            cfg.attributes["connection"] = conn
            (command.upgrade if target == "head" else command.downgrade)(cfg, target)

        async with engine.begin() as conn:
            await conn.run_sync(fn)

    async def tables() -> set[str]:
        async with engine.connect() as conn:
            return set(await conn.run_sync(lambda c: inspect(c).get_table_names()))

    try:
        await migrate_to("head")
        assert "hub_state" in await tables()
        async with engine.begin() as conn:
            await conn.execute(text("PRAGMA foreign_keys=ON"))
            await conn.execute(text("INSERT INTO users (telegram_id, rest_seconds, created_at) VALUES (1, 90, '2026-10-01')"))
            row = "INSERT INTO hub_state (user_id, version, state, updated_at) VALUES (1, 1, '{}', '2026-10-01')"
            await conn.execute(text(row))
        with pytest.raises(Exception, match="UNIQUE"):  # one blob per owner
            async with engine.begin() as conn:
                await conn.execute(text(row))
        await migrate_to("0015")
        down = await tables()
        assert "hub_state" not in down and "day_adjustments" in down
        await migrate_to("head")
        assert "hub_state" in await tables()
    finally:
        await engine.dispose()
