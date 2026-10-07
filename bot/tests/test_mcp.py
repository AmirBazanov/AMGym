"""MCP server on /mcp: auth, Host check, tool list, and the tools against a migrated temp SQLite.

The harness is not a pytest fixture on purpose: the MCP session manager owns an anyio task group, which has
to be entered and exited in the same task (async-generator fixtures set up and tear down in different ones).
"""

import json
import types
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import httpx
import httpx2
from conftest import make_settings
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import select

from gymbot.api.app import create_app
from gymbot.db.models import (
    Exercise,
    FoodEntry,
    Reminder,
    User,
    UserFact,
    WeightOverride,
    Workout,
    WorkoutSet,
)
from gymbot.services.programs import get_or_create_exercise
from gymbot.services.users import get_or_create_user

TOKEN = "test-mcp-token"
BASE = "http://t"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
ACCEPT = {"Accept": "application/json, text/event-stream"}
INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "t", "version": "1"},
    },
}
TZ = "Europe/Moscow"
TOOLS = {
    "nutrition_summary", "training_summary", "wellbeing_summary", "program_status", "profile_and_facts",
    "query", "schema", "set_targets", "add_fact", "deactivate_fact", "set_reminder", "delete_reminder",
    "regenerate_plan", "log_note", "send_message", "service_status", "set_weight_override", "log_body_weight",
}


@asynccontextmanager
async def running(tmp_path, db, bot=None, **kw):
    settings = make_settings(tmp_path, mcp_token=TOKEN, public_url="https://t", **kw)
    app = create_app(settings, db, bot=bot)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as c,
    ):
        yield app, c


@asynccontextmanager
async def mcp_client(app):
    http = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=BASE, headers=AUTH)
    async with http, Client(streamable_http_client(f"{BASE}/mcp", http_client=http)) as client:
        yield client


class FakeBot:
    def __init__(self) -> None:
        self.calls: list[tuple[int, str, dict]] = []
        self.fail_html = False  # when True, Telegram "rejects" HTML markup once

    async def send_message(self, chat_id, text, **kw):
        self.calls.append((chat_id, text, kw))
        if self.fail_html and kw.get("parse_mode") == "HTML":
            self.fail_html = False
            from aiogram.exceptions import TelegramBadRequest

            raise TelegramBadRequest(method=None, message="can't parse entities")  # type: ignore[arg-type]
        return types.SimpleNamespace(message_id=7)


async def make_owner(db, **fields) -> int:
    async with db() as session:
        user = await get_or_create_user(session, 42, "Amir")
        for k, v in fields.items():
            setattr(user, k, v)
        await session.commit()
        return user.id


def local_noon_utc(day: date, tz: str = TZ) -> datetime:
    return datetime.combine(day, time(12), tzinfo=ZoneInfo(tz)).astimezone(UTC)


def local_today(tz: str = TZ) -> date:
    return datetime.now(ZoneInfo(tz)).date()


async def call(client, name: str, **args):
    return await client.call_tool(name, args)


def data(res):
    assert not res.is_error, res.content[0].text
    return json.loads(res.content[0].text)


# ---- transport: auth, Host, mounting ----


async def test_401_without_or_with_wrong_token(tmp_path, db):
    async with running(tmp_path, db) as (_, c):
        for headers in (ACCEPT, {**ACCEPT, "Authorization": "Bearer nope"}):
            r = await c.post("/mcp", json=INIT, headers=headers)
            assert r.status_code == 401
            assert r.headers["WWW-Authenticate"] == "Bearer"
        assert (await c.get("/api/health")).status_code == 200  # the rest of the app stays open
        assert (await c.post("/mcp", json=INIT, headers={**AUTH, **ACCEPT})).status_code == 200


async def test_421_for_unknown_host(tmp_path, db):
    settings = make_settings(tmp_path, mcp_token=TOKEN, public_url="https://t")
    app = create_app(settings, db)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://evil.example") as c,
    ):
        r = await c.post("/mcp", json=INIT, headers={**AUTH, **ACCEPT})
        assert r.status_code == 421


async def test_mcp_absent_without_token(tmp_path, db):
    app = create_app(make_settings(tmp_path, miniapp_dist=tmp_path / "none"), db)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as c:
        r = await c.post("/mcp", json=INIT, headers={**AUTH, **ACCEPT})
        assert r.status_code == 404


async def test_two_apps_in_sequence_each_have_their_session_manager(tmp_path, db):
    for _ in range(2):
        async with running(tmp_path, db) as (_, c):
            r = await c.post("/mcp", json=INIT, headers={**AUTH, **ACCEPT})
            assert r.status_code == 200


# ---- tool list ----


async def test_list_tools_and_annotations(tmp_path, db):
    async with running(tmp_path, db) as (app, _), mcp_client(app) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) == TOOLS
    assert len(tools) == 18
    assert tools["nutrition_summary"].annotations.read_only_hint is True
    assert tools["query"].annotations.read_only_hint is True
    assert tools["set_targets"].annotations.read_only_hint is False
    assert tools["set_weight_override"].annotations.read_only_hint is False


# ---- read tools ----


async def test_nutrition_summary(tmp_path, db):
    today = local_today()
    uid = await make_owner(db, kcal_target=2000, protein_target_g=150)
    async with db() as session:
        for day, what, kcal in ((today, "овсянка", 400), (today, "курица", 600), (today - timedelta(days=1), "паста", 700)):
            session.add(
                FoodEntry(
                    user_id=uid, eaten_at=local_noon_utc(day), description=what, kcal=Decimal(kcal),
                    protein_g=Decimal(30), fat_g=Decimal(10), carbs_g=Decimal(50),
                )
            )
        await session.commit()
    async with running(tmp_path, db) as (app, _), mcp_client(app) as client:
        out = data(await call(client, "nutrition_summary", days=7))
        short = data(await call(client, "nutrition_summary", days=3))
    assert len(out["days"]) == 7
    today_row = out["days"][-1]
    assert today_row["date"] == today.isoformat()
    assert today_row["entries"] == 2
    assert today_row["kcal"] == 1000
    assert today_row["left"]["kcal"] == 2000 - 1000
    assert out["days_logged"] == 2
    assert out["last_day"]["date"] == today.isoformat()
    assert len(out["last_day"]["entries"]) == 2
    assert len(short["days"]) == 3


async def test_training_summary(tmp_path, db):
    today = local_today()
    uid = await make_owner(db)
    yesterday, old = today - timedelta(days=1), today - timedelta(days=40)
    async with db() as session:
        ex = await get_or_create_exercise(session, "mcp test press")
        recent = Workout(user_id=uid, performed_on=yesterday, started_at=local_noon_utc(yesterday))
        recent.sets = [
            WorkoutSet(exercise_id=ex.id, set_index=1, reps=5, weight_kg=Decimal(100), drop_index=0),
            WorkoutSet(exercise_id=ex.id, set_index=2, reps=5, weight_kg=Decimal(100), drop_index=0),
            WorkoutSet(exercise_id=ex.id, set_index=2, reps=6, weight_kg=Decimal(80), drop_index=1),
        ]
        older = Workout(user_id=uid, performed_on=old, started_at=local_noon_utc(old))
        older.sets = [WorkoutSet(exercise_id=ex.id, set_index=1, reps=3, weight_kg=Decimal(110), drop_index=0)]
        session.add_all([recent, older])
        await session.commit()
    async with running(tmp_path, db) as (app, _), mcp_client(app) as client:
        out = data(await call(client, "training_summary", days=14))
    assert len(out["workouts"]) == 1
    w = out["workouts"][0]
    assert w["date"] == yesterday.isoformat()
    assert w["exercises"] == [{"name": "mcp test press", "sets": ["100×5", "100×5 → 80×6"]}]
    assert w["volume_kg"] == 100 * 5 * 2 + 80 * 6
    (rec,) = [r for r in out["e1rm_records"] if r["exercise"] == "mcp test press"]
    assert rec["period"]["e1rm"] == round(100 * (1 + 5 / 30), 1)
    assert rec["period"]["date"] == yesterday.isoformat()
    assert rec["all_time"]["e1rm"] == 121
    assert rec["all_time"]["set"] == "110×3"
    assert rec["all_time"]["date"] == old.isoformat()


async def test_query_select_and_limit(tmp_path, db):
    await make_owner(db)
    async with running(tmp_path, db) as (app, _), mcp_client(app) as client:
        out = data(await call(client, "query", sql="select telegram_id from users"))
        assert out == {"columns": ["telegram_id"], "rows": [[42]], "truncated": False}
        cut = data(await call(client, "query", sql="select id from exercises", limit=2))
    assert len(cut["rows"]) == 2
    assert cut["truncated"] is True


async def test_query_rejects_writes_and_keeps_db_intact(tmp_path, db):
    await make_owner(db)
    bad = [
        "delete from users",
        "select 1; drop table users",
        "PRAGMA table_info(users)",
        "select * from users where 1 attach",
        "update users set name='x'",
    ]
    async with running(tmp_path, db) as (app, _), mcp_client(app) as client:
        for sql in bad:
            res = await call(client, "query", sql=sql)
            assert res.is_error, sql
            assert "Запрос отклонён" in res.content[0].text, sql
        still = data(await call(client, "query", sql="select name from users"))
    assert still["rows"] == [["Amir"]]
    async with db() as session:
        assert (await session.scalar(select(User).where(User.telegram_id == 42))).name == "Amir"


async def test_schema_is_plain_ddl(tmp_path, db):
    async with running(tmp_path, db) as (app, _), mcp_client(app) as client:
        res = await call(client, "schema")
    assert not res.is_error
    assert "CREATE TABLE users" in res.content[0].text


async def test_read_tools_without_owner_are_errors(tmp_path, db):
    async with running(tmp_path, db) as (app, _), mcp_client(app) as client:
        for name in ("nutrition_summary", "training_summary", "wellbeing_summary", "profile_and_facts"):
            res = await call(client, name)
            assert res.is_error, name
    async with db() as session:
        assert await session.scalar(select(User.id)) is None  # read tools never create the owner


# ---- send_message ----


async def test_send_message_goes_to_the_owner(tmp_path, db):
    await make_owner(db)
    bot = FakeBot()
    async with running(tmp_path, db, bot=bot) as (app, _), mcp_client(app) as client:
        out = data(await call(client, "send_message", text="Привет, это тест"))
    assert out == {"sent": True, "message_id": 7, "html": False}
    (chat_id, text, kw) = bot.calls[0]
    assert chat_id == 42
    assert text == "Привет, это тест"
    assert kw.get("parse_mode") is None


async def test_send_message_without_bot(tmp_path, db):
    await make_owner(db)
    async with running(tmp_path, db) as (app, _), mcp_client(app) as client:
        res = await call(client, "send_message", text="hi")
    assert res.is_error
    assert "RUN_BOT" in res.content[0].text


async def test_send_message_too_long_is_rejected(tmp_path, db):
    await make_owner(db)
    bot = FakeBot()
    async with running(tmp_path, db, bot=bot) as (app, _), mcp_client(app) as client:
        res = await call(client, "send_message", text="x" * 4001)
    assert res.is_error
    assert bot.calls == []


# ---- write tools ----


async def test_set_targets_changes_only_given_fields(tmp_path, db):
    await make_owner(db, fat_target_g=70)
    async with running(tmp_path, db) as (app, _), mcp_client(app) as client:
        out = data(await call(client, "set_targets", kcal=2500, protein=160))
        empty = await call(client, "set_targets")
    assert out["targets"]["fat"] == 70
    assert empty.is_error
    async with db() as session:
        user = await session.scalar(select(User).where(User.telegram_id == 42))
    assert user.kcal_target == 2500
    assert user.protein_target_g == 160
    assert user.fat_target_g == 70


async def test_log_note_creates_prefixed_fact(tmp_path, db):
    await make_owner(db)
    async with running(tmp_path, db) as (app, _), mcp_client(app) as client:
        data(await call(client, "log_note", text="тренер сказал  добавить  присед"))
    async with db() as session:
        facts = (await session.scalars(select(UserFact))).all()
    assert len(facts) == 1
    assert facts[0].text == "[mcp] тренер сказал добавить присед"
    assert facts[0].category == "other"
    assert facts[0].active is True


async def test_set_reminder_text_requires_text(tmp_path, db):
    await make_owner(db)
    async with running(tmp_path, db) as (app, _), mcp_client(app) as client:
        res = await call(client, "set_reminder", time="09:30", kind="text")
    assert res.is_error
    async with db() as session:
        assert (await session.scalars(select(Reminder))).all() == []


async def test_set_and_delete_reminder(tmp_path, db):
    await make_owner(db)
    async with running(tmp_path, db) as (app, _), mcp_client(app) as client:
        made = data(await call(client, "set_reminder", time="09:30", kind="checkin"))
        assert made["time"] == "09:30"
        async with db() as session:
            (r,) = (await session.scalars(select(Reminder))).all()
            assert (r.id, r.minute_of_day, r.kind) == (made["id"], 9 * 60 + 30, "checkin")
        assert data(await call(client, "delete_reminder", id=made["id"])) == {"deleted": made["id"]}
        assert (await call(client, "delete_reminder", id=made["id"])).is_error
    async with db() as session:
        assert (await session.scalars(select(Reminder))).all() == []


async def test_send_message_html_and_fallback(tmp_path, db):
    await make_owner(db)
    bot = FakeBot()
    async with running(tmp_path, db, bot=bot) as (app, _), mcp_client(app) as client:
        out = data(await call(client, "send_message", text="<b>Питание</b>\nок", html=True))
        assert out["sent"] and out["html"] is True
        assert bot.calls[-1][2].get("parse_mode") == "HTML"

        bot.fail_html = True  # Telegram rejects the markup once
        out = data(await call(client, "send_message", text="<b>битая", html=True))
        assert out["sent"] and out["html"] is False and "разметка" in out["note"]
        assert bot.calls[-1][2].get("parse_mode") is None


async def test_set_weight_override_writes_today_and_repeat_updates(tmp_path, db):
    uid = await make_owner(db)
    today = local_today()
    async with running(tmp_path, db) as (app, _), mcp_client(app) as client:
        out = data(await call(client, "set_weight_override", exercise="жим лёжа", weight_kg=85))
        assert out == {"exercise": "жим лёжа", "weight_kg": 85, "date": today.isoformat()}
        again = data(await call(client, "set_weight_override", exercise="Жим лёжа", weight_kg=87.5))
        assert again["weight_kg"] == 87.5 and again["exercise"] == "жим лёжа"
        ambiguous = await call(client, "set_weight_override", exercise="жим", weight_kg=90)
        assert ambiguous.is_error  # exact names and known synonyms only: the chat-only short names are not here
        last = data(await call(client, "set_weight_override", exercise="жим лёжа", weight_kg=90))
        assert last["weight_kg"] == 90
    async with db() as session:
        rows = (await session.execute(select(WeightOverride, Exercise.name).join(Exercise))).all()
    assert [(o.user_id, name, o.day, float(o.weight_kg)) for o, name in rows] == [
        (uid, "жим лёжа", today, 90.0)
    ]


async def test_set_weight_override_null_clears_today(tmp_path, db):
    await make_owner(db)
    today = local_today()
    async with running(tmp_path, db) as (app, _), mcp_client(app) as client:
        await call(client, "set_weight_override", exercise="жим лёжа", weight_kg=85)
        out = data(await call(client, "set_weight_override", exercise="жим лёжа", weight_kg=None))
        assert out == {"exercise": "жим лёжа", "weight_kg": None, "date": today.isoformat(), "removed": True}
        again = data(await call(client, "set_weight_override", exercise="жим лёжа"))
        assert again["removed"] is False
    async with db() as session:
        assert (await session.scalars(select(WeightOverride))).all() == []


async def test_set_weight_override_unknown_exercise_or_weight_is_an_error(tmp_path, db):
    await make_owner(db)
    async with running(tmp_path, db) as (app, _), mcp_client(app) as client:
        unknown = await call(client, "set_weight_override", exercise="подъём на носки", weight_kg=50)
        assert unknown.is_error and "нет в программе" in unknown.content[0].text
        assert (await call(client, "set_weight_override", exercise="жим лёжа", weight_kg=0)).is_error
        assert (await call(client, "set_weight_override", exercise="жим лёжа", weight_kg=501)).is_error
    async with db() as session:
        assert (await session.scalars(select(WeightOverride))).all() == []


async def test_set_weight_override_without_owner_is_an_error(tmp_path, db):
    async with running(tmp_path, db) as (app, _), mcp_client(app) as client:
        res = await call(client, "set_weight_override", exercise="жим лёжа", weight_kg=85)
    assert res.is_error
