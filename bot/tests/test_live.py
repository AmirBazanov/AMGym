"""Live updates for the Mini App: stream token, pub/sub hub, SSE generator, endpoints, publish points."""

import asyncio
import json
import logging
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
import uvicorn
from sqlalchemy import select
from test_chat_settings import FakeLLM as SettingsLLM
from test_chat_settings import apply, run, settings_token
from test_log_text import FakeLLM, callback, food, send, token_of

from gymbot import main
from gymbot.api.app import create_app
from gymbot.db.models import User
from gymbot.handlers import chat_settings as hcs
from gymbot.handlers import log_text
from gymbot.services import live

KEY = live.signing_key("123:abc")


@pytest.fixture(autouse=True)
def clean_hub():
    live.hub = live.Hub()
    for store in (hcs.SETTINGS, log_text.PENDING, log_text.CONTEXT, log_text.FACTS):
        store.clear()
    yield
    live.hub = live.Hub()


async def next_chunk(it, timeout: float = 2.0) -> str:
    return await asyncio.wait_for(anext(it), timeout)


# ---- token ----


def test_token_round_trip_and_expiry():
    token = live.make_token(KEY, 5, now=1000)
    assert live.verify_token(KEY, token, now=1000) == 5
    assert live.verify_token(KEY, token, now=1000 + live.TOKEN_TTL) == 5
    assert live.verify_token(KEY, token, now=1000 + live.TOKEN_TTL + 1) is None
    assert live.TOKEN_TTL == 60


def test_token_is_bound_to_user_key_and_format():
    token = live.make_token(KEY, 5, now=1000)
    uid, exp, sig = token.split(".")
    assert live.verify_token(KEY, f"6.{exp}.{sig}", now=1000) is None  # another user
    assert live.verify_token(KEY, f"{uid}.{int(exp) + 600}.{sig}", now=1000) is None  # extended expiry
    assert live.verify_token(live.signing_key("other:token"), token, now=1000) is None
    for bad in ("", "abc", "5.1060", "5.x.y", "-5.1060.sig", token + "x"):
        assert live.verify_token(KEY, bad, now=1000) is None


def test_live_key_differs_from_the_bot_token():
    assert KEY != b"123:abc" and len(KEY) == 32


def test_access_log_redacts_the_token():
    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:1", "GET", "/api/live?token=5.1060.secret&x=1", "1.1", 200), None,
    )
    assert live.RedactTokens().filter(record)
    assert "secret" not in record.getMessage() and "/api/live?token=***&x=1" in record.getMessage()
    live.install_log_redaction()
    live.install_log_redaction()
    filters = logging.getLogger("uvicorn.access").filters
    assert sum(isinstance(f, live.RedactTokens) for f in filters) == 1


# ---- hub ----


def test_publish_reaches_only_that_user_and_known_topics():
    hub = live.Hub()
    a, b = hub.subscribe(1), hub.subscribe(2)
    hub.publish(1, ["state", "bogus", "facts"])
    assert a.take() == ["facts", "state"] and b.take() == []
    assert not a.wake.is_set()


def test_at_most_three_streams_per_user_oldest_dropped():
    hub = live.Hub()
    subs = [hub.subscribe(1) for _ in range(4)]
    assert subs[0].closed and not any(s.closed for s in subs[1:])
    assert hub.count(1) == 3
    other = hub.subscribe(2)
    assert not other.closed and hub.count() == 4


def test_unsubscribe_cleans_up():
    hub = live.Hub()
    s1, s2 = hub.subscribe(1), hub.subscribe(1)
    hub.unsubscribe(s1)
    hub.unsubscribe(s1)  # twice is fine
    assert hub.count(1) == 1
    hub.unsubscribe(s2)
    assert hub.count(1) == 0 and hub._subs == {}


def test_close_all_ends_streams_and_refuses_new_ones():
    hub = live.Hub()
    s = hub.subscribe(1)
    hub.close_all()
    assert s.closed and s.wake.is_set()
    assert hub.subscribe(1).closed


def test_publish_never_raises(monkeypatch):
    def boom(*a):
        raise RuntimeError("x")

    monkeypatch.setattr(live.hub, "publish", boom)
    live.publish(1, "state")  # logged, not raised
    live.publish(None, "state")


# ---- the event stream ----


async def test_stream_hello_then_coalesced_change():
    hub = live.Hub()
    it = live.events(7, hub, coalesce=0.05, ping=5)
    assert await next_chunk(it) == "event: hello\ndata: {}\n\n"
    assert hub.count(7) == 1
    hub.publish(7, ["nutrition"])
    hub.publish(7, ["plan", "nutrition"])  # within the window: one event
    chunk = await next_chunk(it)
    assert chunk.startswith("event: change\ndata: ")
    assert json.loads(chunk.split("data: ", 1)[1]) == {"topics": ["nutrition", "plan"]}
    hub.publish(7, ["state"])
    assert json.loads((await next_chunk(it)).split("data: ", 1)[1]) == {"topics": ["state"]}
    await it.aclose()
    assert hub.count(7) == 0


async def test_stream_heartbeat():
    hub = live.Hub()
    it = live.events(7, hub, coalesce=0.01, ping=0.02)
    await next_chunk(it)
    assert await next_chunk(it) == ": ping\n\n"
    await it.aclose()


async def test_stream_other_user_gets_nothing():
    hub = live.Hub()
    mine, theirs = live.events(1, hub, coalesce=0.01, ping=5), live.events(2, hub, coalesce=0.01, ping=0.2)
    await next_chunk(mine)
    await next_chunk(theirs)
    hub.publish(1, ["facts"])
    assert "facts" in await next_chunk(mine)
    assert await next_chunk(theirs) == ": ping\n\n"  # nothing but the heartbeat
    await mine.aclose()
    await theirs.aclose()


async def test_stream_cancelled_unsubscribes():
    hub = live.Hub()
    chunks: list[str] = []

    async def consume():
        async for c in live.events(3, hub, coalesce=0.01, ping=5):
            chunks.append(c)

    task = asyncio.create_task(consume())
    while not chunks:
        await asyncio.sleep(0.001)
    assert hub.count(3) == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert hub.count(3) == 0


async def test_stream_ends_when_dropped_or_on_shutdown():
    hub = live.Hub(max_per_user=1)
    old = live.events(1, hub, coalesce=0.01, ping=5)
    await next_chunk(old)
    new = live.events(1, hub, coalesce=0.01, ping=5)
    await next_chunk(new)  # subscribing closes the old one
    with pytest.raises(StopAsyncIteration):
        await next_chunk(old)
    hub.close_all()
    with pytest.raises(StopAsyncIteration):
        await next_chunk(new)
    assert hub.count() == 0


async def test_server_shutdown_closes_streams_first(monkeypatch):
    closed = []
    monkeypatch.setattr(live, "close_all", lambda: closed.append(True))
    parent = AsyncMock()
    monkeypatch.setattr(uvicorn.Server, "shutdown", parent)
    await main.Server(uvicorn.Config(app=None)).shutdown()
    assert closed == [True] and parent.await_count == 1


# ---- endpoints ----


async def test_token_endpoint(client, auth, db):
    assert (await client.post("/api/live/token")).status_code == 401
    r = await client.post("/api/live/token", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["expiresIn"] == 60
    async with db() as s:
        user_id = await s.scalar(select(User.id).where(User.telegram_id == 42))
    assert live.verify_token(KEY, body["token"]) == user_id  # the database id, not the Telegram one


async def test_stream_rejects_bad_and_expired_tokens(client):
    assert (await client.get("/api/live")).status_code == 401
    assert (await client.get("/api/live", params={"token": "1.2.3"})).status_code == 401
    expired = live.make_token(KEY, 1, now=datetime.now(UTC).timestamp() - 120)
    assert (await client.get("/api/live", params={"token": expired})).status_code == 401


async def test_stream_over_asgi(settings, db):
    """The real endpoint through the app's middleware: hello, a change, cleanup on disconnect."""
    app = create_app(settings, db)
    token = live.make_token(KEY, 7)
    messages: list[dict] = []
    disconnect = asyncio.Event()

    async def receive():
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send_(msg):
        messages.append(msg)

    def body() -> str:
        return b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body").decode()

    async def until(pred):
        async with asyncio.timeout(3):
            while not pred():
                await asyncio.sleep(0.01)

    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "http_version": "1.1",
        "method": "GET", "scheme": "http", "path": "/api/live", "raw_path": b"/api/live",
        "query_string": f"token={token}".encode(), "headers": [(b"host", b"t")],
        "client": ("127.0.0.1", 1), "server": ("t", 80), "root_path": "",
    }
    task = asyncio.create_task(app(scope, receive, send_))
    await until(lambda: "event: hello" in body())
    start = next(m for m in messages if m["type"] == "http.response.start")
    headers = {k.decode(): v.decode() for k, v in start["headers"]}
    assert start["status"] == 200 and headers["content-type"].startswith("text/event-stream")
    assert headers["cache-control"] == "no-cache" and headers["x-accel-buffering"] == "no"
    assert live.hub.count(7) == 1
    live.publish(7, "facts", "state")
    await until(lambda: "event: change" in body())
    assert 'data: {"topics":["facts","state"]}' in body()
    disconnect.set()
    await asyncio.wait_for(task, 3)
    assert live.hub.count(7) == 0


async def test_api_writes_publish(client, auth, db):
    await client.get("/api/state", headers=auth)
    async with db() as s:
        user_id = await s.scalar(select(User.id).where(User.telegram_id == 42))
    sub = live.hub.subscribe(user_id)
    assert (await client.get("/api/state", headers=auth)).status_code == 200
    assert sub.take() == []  # reads publish nothing
    await client.put("/api/settings", json={"restSeconds": 120}, headers=auth)
    assert sub.take() == ["state"]
    r = await client.post("/api/reminders", json={"time": "09:00", "kind": "nutrition"}, headers=auth)
    assert sub.take() == ["reminders"]
    await client.delete(f"/api/reminders/{r.json()['id']}", headers=auth)
    assert sub.take() == ["reminders"]
    r = await client.post("/api/facts", json={"text": "не ем свинину", "category": "food"}, headers=auth)
    assert sub.take() == ["facts"]
    await client.delete(f"/api/facts/{r.json()['id']}", headers=auth)
    assert sub.take() == ["facts", "state"]


# ---- chat publish points ----


async def owner_id(db) -> int:
    async with db() as s:
        user = await s.scalar(select(User).where(User.telegram_id == 42))
        if user is None:
            user = User(telegram_id=42, rest_seconds=90)
            s.add(user)
            await s.commit()
        return user.id


async def test_chat_settings_apply_publishes(settings, db):
    llm = SettingsLLM(settings)
    sub = live.hub.subscribe(await owner_id(db))
    llm.answers.append({"actions": [
        {"type": "targets", "kcal": 2800, "protein": None, "fat": None, "carbs": None},
        {"type": "reminder_add", "time": "08:30", "kind": "text", "text": "креатин", "weekdays": []},
    ]})
    now = datetime.now(UTC)
    msg = await run("норма 2800 ккал и напоминай в 8:30 про креатин", settings, db, llm, at=now)
    assert "Применить?" in msg.answer.await_args.args[0]
    assert sub.take() == []  # a preview writes nothing
    await apply(settings_token(msg), settings, db)
    assert sub.take() == ["reminders", "state"]


def test_settings_plan_topics():
    from gymbot.services.chat_settings import Plan

    day = datetime.now(UTC).date()
    assert Plan(day=day).live_topics() == []
    assert Plan(day=day, rest=60).live_topics() == ["state"]
    assert Plan(day=day, weights={1: ("жим", 80.0)}).live_topics() == ["state"]
    assert Plan(day=day, program=("x", day)).live_topics() == ["state", "plan"]
    assert Plan(day=day, reminder_ops={1: "delete"}).live_topics() == ["reminders"]


async def test_food_saved_from_chat_publishes(settings, db):
    llm = FakeLLM(settings)
    sub = live.hub.subscribe(await owner_id(db))
    llm.answers = [food(2)]
    msg = await send("две самсы", llm, settings, db, at=datetime.now(UTC))
    assert sub.take() == []
    await log_text.save(callback(f"save:{token_of(msg)}"), settings, db)
    assert sub.take() == ["nutrition"]
