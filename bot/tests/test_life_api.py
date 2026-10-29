"""«Дела» API for the Mini App: /api/life/* (gymbot.api.life) — the view with the agenda, CRUD of every kind,
done/undone, notes with their body, settings, auth, 404 and 422."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from conftest import init_data, make_settings

from gymbot.db.models import LifeTask
from gymbot.services import live
from gymbot.services.users import get_or_create_user

BODIES = {
    "slots": {"weekday": 1, "start": "10:00", "title": "ИЗ Математика", "area": "work", "tag": "math",
              "place": "Восход · куратор", "vld": "15:00"},
    "events": {"start": "11:00", "title": "Позаниматься HTTP-сервером на C", "area": "study"},
    "deadlines": {"title": "Лабораторная №1", "course": "Software Engineering"},
    "tasks": {"title": "Обновить резюме", "tag": "работа"},
    "chores": {"title": "Уборка", "everyDays": 7},
    "projects": {"name": "AMGym", "kind": "work", "status": "active", "description": "бот",
                 "link": "https://github.com/x/y"},
    "notes": {"title": "Памятка по epoll", "body": "# epoll\n\nтекст", "tags": ["c"], "author": "bot"},
}
KEYS = {
    "slots": {"id", "weekday", "start", "end", "title", "area", "tag", "place", "vld", "note"},
    "events": {"id", "day", "start", "end", "title", "area", "projectId", "remindBefore", "done", "note"},
    "deadlines": {"id", "day", "title", "course", "projectId", "done"},
    "tasks": {"id", "title", "tag", "projectId", "done", "doneAt"},
    "chores": {"id", "title", "everyDays", "lastDone", "left"},
    "projects": {"id", "name", "kind", "status", "description", "link", "nextStep"},
    "notes": {"id", "title", "tags", "author", "projectId", "updatedAt"},
}


@pytest.fixture
def today(settings) -> date:
    return datetime.now(ZoneInfo(settings.timezone)).date()


@pytest.fixture
def published(monkeypatch):
    out: list[tuple] = []
    monkeypatch.setattr(live, "publish", lambda *a: out.append(a))
    return out


def body_of(path: str, today: date) -> dict:
    body = dict(BODIES[path])
    if path in ("events", "deadlines"):
        body["day"] = (today + timedelta(days=1)).isoformat()
    return body


async def test_auth(client, make_client, tmp_path):
    assert (await client.get("/api/life/view")).status_code == 401
    bad = {"X-Telegram-Init-Data": "user=%7B%7D&hash=00"}
    assert (await client.post("/api/life/tasks", json={"title": "x"}, headers=bad)).status_code == 401
    async with make_client(make_settings(tmp_path, allowed_user_ids=[1])) as c:
        assert (await c.get("/api/life/view", headers={"X-Telegram-Init-Data": init_data()})).status_code == 403


async def test_empty_view(client, auth, today):
    r = await client.get("/api/life/view", headers=auth)
    assert r.status_code == 200
    assert r.json() == {
        "today": today.isoformat(), "settings": {"remindBefore": 30, "summaryTime": "08:00", "summaryEnabled": True},
        "slots": [], "events": [], "deadlines": [], "tasks": [], "chores": [], "projects": [], "notes": [],
        "agenda": [],
    }


@pytest.mark.parametrize("path", list(BODIES))
async def test_crud(client, auth, today, published, path):
    body = body_of(path, today)
    r = await client.post(f"/api/life/{path}", json=body, headers=auth)
    assert r.status_code == 201, r.text
    obj = r.json()
    assert set(obj) == KEYS[path] | ({"body"} if path == "notes" else set())
    for key, value in body.items():
        assert obj[key] == value, key
    oid = obj["id"]
    listed = (await client.get("/api/life/view", headers=auth)).json()[path]
    assert [o["id"] for o in listed] == [oid]
    assert set(listed[0]) == KEYS[path]

    change = {"name": "AMGym 2", "nextStep": "выкатить"} if path == "projects" else {"title": "Новое"}
    r = await client.patch(f"/api/life/{path}/{oid}", json=change, headers=auth)
    assert r.status_code == 200, r.text
    assert all(r.json()[k] == v for k, v in change.items())

    assert (await client.delete(f"/api/life/{path}/{oid}", headers=auth)).status_code == 204
    assert (await client.get("/api/life/view", headers=auth)).json()[path] == []
    assert (await client.delete(f"/api/life/{path}/{oid}", headers=auth)).status_code == 404
    assert (await client.patch(f"/api/life/{path}/{oid}", json=change, headers=auth)).status_code == 404
    assert len(published) == 3 and {p[1] for p in published} == {"life"}


async def test_defaults_and_shape(client, auth, today):
    r = await client.post("/api/life/events", json={"day": today.isoformat(), "title": "Созвон"}, headers=auth)
    assert r.json() == {"id": r.json()["id"], "day": today.isoformat(), "start": None, "end": None,
                        "title": "Созвон", "area": "other", "projectId": None, "remindBefore": None, "done": False,
                        "note": None}
    r = await client.post("/api/life/chores", json={"title": "Мусор"}, headers=auth)
    assert r.json() == {"id": r.json()["id"], "title": "Мусор", "everyDays": 7, "lastDone": None, "left": 0}
    r = await client.post("/api/life/notes", json={"title": "Идея"}, headers=auth)
    note = r.json()
    assert (note["body"], note["tags"], note["author"]) == ("", [], "me")
    assert note["updatedAt"].endswith("Z")
    r = await client.get(f"/api/life/notes/{note['id']}", headers=auth)
    assert r.status_code == 200 and r.json() == note
    assert (await client.get("/api/life/notes/999", headers=auth)).status_code == 404


async def test_project_link(client, auth, today):
    pid = (await client.post("/api/life/projects", json={"name": "HTTP-сервер на C", "kind": "learning"},
                             headers=auth)).json()["id"]
    r = await client.post("/api/life/tasks", json={"title": "epoll", "projectId": pid}, headers=auth)
    assert r.status_code == 201 and r.json()["projectId"] == pid
    r = await client.post("/api/life/tasks", json={"title": "x", "projectId": 999}, headers=auth)
    assert r.status_code == 422 and r.json()["detail"] == "Нет проекта #999"
    tid = (await client.get("/api/life/view", headers=auth)).json()["tasks"][0]["id"]
    r = await client.patch(f"/api/life/tasks/{tid}", json={"projectId": None}, headers=auth)
    assert r.json()["projectId"] is None


async def test_validation_422(client, auth, today):
    cases = [
        ("tasks", {"title": "x", "color": "red"}, "Неизвестное поле «color»"),
        ("tasks", {"tag": "x"}, "Нужно поле «название»"),
        ("tasks", {"title": ""}, "не может быть пустым"),
        ("slots", {"weekday": 8, "start": "10:00", "title": "x"}, "день недели"),
        ("slots", {"weekday": 1, "start": "25:00", "title": "x"}, "ЧЧ:ММ"),
        ("events", {"day": (today + timedelta(days=500)).isoformat(), "title": "x"}, "дальше 400 дней"),
        ("events", {"day": today.isoformat(), "title": "x", "start": "12:00", "end": "11:00"}, "Конец раньше"),
        ("projects", {"name": "x", "kind": "hobby"}, "work, learning, own"),
        ("chores", {"title": "x", "everyDays": 0}, "от 1 до 365"),
    ]
    for path, body, msg in cases:
        r = await client.post(f"/api/life/{path}", json=body, headers=auth)
        assert r.status_code == 422, (path, body)
        assert msg in r.json()["detail"], r.json()
    assert (await client.post("/api/life/tasks", json=["x"], headers=auth)).status_code == 422
    assert (await client.get("/api/life/view", headers=auth)).json()["tasks"] == []
    sid = (await client.post("/api/life/slots", json=BODIES["slots"] | {"end": "11:00"}, headers=auth)).json()["id"]
    r = await client.patch(f"/api/life/slots/{sid}", json={"end": "09:00"}, headers=auth)  # against the stored start
    assert r.status_code == 422 and r.json()["detail"] == "Конец раньше начала"
    r = await client.patch(f"/api/life/slots/{sid}", json={"title": None}, headers=auth)
    assert r.status_code == 422


async def test_unknown_kind_and_other_users_rows(client, auth, db):
    assert (await client.post("/api/life/cars", json={}, headers=auth)).status_code == 404
    assert (await client.post("/api/life/settings", json={}, headers=auth)).status_code == 404
    async with db() as s:
        other = await get_or_create_user(s, 7, "other")
        task = LifeTask(user_id=other.id, title="чужая", position=1)
        s.add(task)
        await s.commit()
    for call in (
        client.patch(f"/api/life/tasks/{task.id}", json={"title": "моя"}, headers=auth),
        client.delete(f"/api/life/tasks/{task.id}", headers=auth),
        client.post(f"/api/life/tasks/{task.id}/done", headers=auth),
    ):
        assert (await call).status_code == 404
    assert (await client.get("/api/life/view", headers=auth)).json()["tasks"] == []


async def test_done_and_undone(client, auth, today, published):
    ids = {}
    for path in ("tasks", "events", "deadlines", "chores", "slots"):
        ids[path] = (await client.post(f"/api/life/{path}", json=body_of(path, today), headers=auth)).json()["id"]
    r = await client.post(f"/api/life/tasks/{ids['tasks']}/done", headers=auth)
    assert r.status_code == 200 and r.json()["done"] is True and r.json()["doneAt"].endswith("Z")
    first = r.json()["doneAt"]
    assert (await client.post(f"/api/life/tasks/{ids['tasks']}/done", headers=auth)).json()["doneAt"] == first
    r = await client.post(f"/api/life/tasks/{ids['tasks']}/undone", headers=auth)
    assert r.json()["done"] is False and r.json()["doneAt"] is None
    for path in ("events", "deadlines"):
        assert (await client.post(f"/api/life/{path}/{ids[path]}/done", headers=auth)).json()["done"] is True
        assert (await client.post(f"/api/life/{path}/{ids[path]}/undone", headers=auth)).json()["done"] is False

    r = await client.post(f"/api/life/chores/{ids['chores']}/done", headers=auth)
    assert r.json()["lastDone"] == today.isoformat() and r.json()["left"] == 7
    yesterday = (today - timedelta(days=1)).isoformat()
    r = await client.post(f"/api/life/chores/{ids['chores']}/done", json={"day": yesterday}, headers=auth)
    assert r.json()["lastDone"] == yesterday and r.json()["left"] == 6
    tomorrow = (today + timedelta(days=1)).isoformat()
    r = await client.post(f"/api/life/chores/{ids['chores']}/done", json={"day": tomorrow}, headers=auth)
    assert r.status_code == 422 and r.json()["detail"] == "Дата в будущем"
    r = await client.post(f"/api/life/chores/{ids['chores']}/undone", headers=auth)
    assert r.json()["lastDone"] is None and r.json()["left"] == 0

    assert (await client.post(f"/api/life/slots/{ids['slots']}/done", headers=auth)).status_code == 404
    assert (await client.post("/api/life/tasks/999/done", headers=auth)).status_code == 404
    assert all(p[1] == "life" for p in published)


async def test_view_agenda_for_a_date(client, auth, today):
    far = today + timedelta(days=30)
    await client.post("/api/life/slots", json={"weekday": far.isoweekday(), "start": "15:00", "title": "Урок",
                                               "area": "work", "place": "Восход"}, headers=auth)
    await client.post("/api/life/events", json={"day": far.isoformat(), "start": "11:00", "title": "Созвон",
                                                "area": "work"}, headers=auth)
    await client.post("/api/life/deadlines", json={"day": far.isoformat(), "title": "Курсовая"}, headers=auth)

    view = (await client.get("/api/life/view", headers=auth)).json()
    assert view["events"] == []  # beyond two weeks from today
    r = await client.get(f"/api/life/view?date={far.isoformat()}", headers=auth)
    view = r.json()
    assert view["today"] == today.isoformat()
    assert [e["title"] for e in view["events"]] == ["Созвон"]
    assert [(a["kind"], a["title"], a["start"]) for a in view["agenda"]] == [
        ("event", "Созвон", "11:00"), ("slot", "Урок", "15:00"), ("deadline", "Курсовая", None),
    ]
    assert view["agenda"][1] == {"kind": "slot", "id": view["slots"][0]["id"], "title": "Урок", "start": "15:00",
                                 "end": None, "area": "work", "place": "Восход", "done": False, "overdueDays": 0}
    too_far = (today + timedelta(days=401)).isoformat()
    assert (await client.get(f"/api/life/view?date={too_far}", headers=auth)).status_code == 422
    assert (await client.get("/api/life/view?date=завтра", headers=auth)).status_code == 422


async def test_settings(client, auth, published):
    r = await client.put("/api/life/settings", json={"summaryTime": "7:30", "remindBefore": 15}, headers=auth)
    assert r.status_code == 200
    assert r.json() == {"remindBefore": 15, "summaryTime": "07:30", "summaryEnabled": True}
    r = await client.put("/api/life/settings", json={"summaryEnabled": False}, headers=auth)
    assert r.json() == {"remindBefore": 15, "summaryTime": "07:30", "summaryEnabled": False}
    assert (await client.get("/api/life/view", headers=auth)).json()["settings"] == r.json()
    for bad in ({"summaryTime": "25:00"}, {"remindBefore": 2000}, {"summaryEnabled": "да"}, {"x": 1}):
        assert (await client.put("/api/life/settings", json=bad, headers=auth)).status_code == 422, bad
    assert len(published) == 2


async def test_routes_win_over_the_mini_app(tmp_path, make_client, auth):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("app")
    async with make_client(make_settings(tmp_path, miniapp_dist=dist)) as c:
        assert (await c.post("/api/life/tasks", json={"title": "x"}, headers=auth)).status_code == 201
        assert (await c.post("/api/life/cars", json={}, headers=auth)).status_code == 404
