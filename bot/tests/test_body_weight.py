"""Body weight: chat recognition and routing, the confirm buttons, one row per day, the profile rule,
the diary line, the Mini App API, live topics, MCP tool and migration 0011."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from alembic import command
from conftest import init_data
from sqlalchemy import inspect, select, text
from test_chat_settings import FakeLLM
from test_log_text import callback, message
from test_mcp import call, data, make_owner, mcp_client, running

from gymbot.db import migrate
from gymbot.db.models import BodyWeight, User
from gymbot.db.session import make_engine
from gymbot.handlers import body_weight as hbw
from gymbot.handlers import log_text
from gymbot.llm.schemas import ParseResult
from gymbot.services import answer as answer_service
from gymbot.services import body_weight as bw
from gymbot.services import live
from gymbot.services.users import get_or_create_user

USER = 42
MSK = ZoneInfo("Europe/Moscow")
T0 = datetime(2026, 10, 8, 9, 0, tzinfo=UTC)  # Thursday 12:00 in Moscow
DAY = date(2026, 10, 8)


@pytest.fixture(autouse=True)
def clean_state():
    for store in (hbw.PENDING, log_text.PENDING, log_text.CONTEXT, log_text.FACTS):
        store.clear()
    yield
    for store in (hbw.PENDING, log_text.PENDING, log_text.CONTEXT, log_text.FACTS):
        store.clear()


@pytest.fixture(autouse=True)
def no_diary_answer(monkeypatch):
    async def parser_answer(message, text, result, *args):
        return result

    monkeypatch.setattr(log_text, "_diary_answer", parser_answer)


@pytest.fixture
def published(monkeypatch) -> list[tuple[int, tuple[str, ...]]]:
    calls: list[tuple[int, tuple[str, ...]]] = []
    monkeypatch.setattr(live, "publish", lambda user_id, *topics: calls.append((user_id, topics)))
    return calls


# ---- 1. recognition ----


@pytest.mark.parametrize(
    ("text", "kg"),
    [
        ("вес 84.6", "84.6"),
        ("Вешу 84", "84"),
        ("утром 84,2 кг", "84.2"),
        ("взвесился 85", "85"),
        ("Взвесилась утром 62,5 кг.", "62.5"),
        ("вес: 84,6", "84.6"),
        ("мой вес 84 кг", "84"),
        ("вес сегодня 84.2!", "84.2"),
        ("вес 84.6 кг утром", "84.6"),
        ("с утра 84 кг", "84"),
        ("вес 30", "30"),
        ("вес 250", "250"),
    ],
)
def test_parse_chat_recognizes_a_weigh_in(text, kg):
    assert bw.parse_chat(text) == Decimal(kg)


@pytest.mark.parametrize(
    "text",
    [
        "жим 85",
        "съел 85 г",
        "вес на сегодня жим 85",
        "какой у меня вес",
        "вес 84.6?",
        "84 кг",
        "вес 25",
        "вес 300",
        "вес 29.9",
        "вес 250.5",
        "вес 200 г",
        "сегодня 100 кг",
        "утром 84",
        "присед 100 кг",
        "вес гантели 20",
        "вес 84.6, спал 7 часов",
        "как меняется вес",
        "",
    ],
)
def test_parse_chat_ignores_everything_else(text):
    assert bw.parse_chat(text) is None


@pytest.mark.parametrize(
    ("value", "shown"),
    [(Decimal("84.60"), "84,6"), (Decimal(84), "84"), (84.55, "84,55"), (-0.55, "−0,55"), (Decimal("-0.6"), "−0,6")],
)
def test_kg_text_uses_decimal_comma_and_real_minus(value, shown):
    assert bw.kg_text(value) == shown


# ---- 2. routing in process_text ----


async def run(text, settings, db, llm, at=T0, **kw):
    msg = message(text, at=at)
    await log_text.process_text(msg, text, settings, db, llm.client, **kw)
    return msg


def replies(msg) -> list[str]:
    return [c.args[0] for c in msg.answer.await_args_list]


def buttons(msg) -> list[str]:
    kb = msg.answer.await_args.kwargs["reply_markup"]
    return [b.callback_data for row in kb.inline_keyboard for b in row]


def pending_token(msg) -> str:
    return buttons(msg)[0].split(":", 1)[1]


async def test_weigh_in_gets_a_preview_and_skips_the_parser(settings, db):
    llm = FakeLLM(settings)
    msg = await run("вес 84.6", settings, db, llm)
    assert replies(msg) == ["Записать вес? 84,6 кг"]
    save, drop = buttons(msg)
    token = pending_token(msg)
    assert save == f"bwsave:{token}" and drop == f"bwdrop:{token}"
    assert llm.bodies == []  # neither the parser nor the settings model
    assert hbw.PENDING[token].weight_kg == Decimal("84.6")
    async with db() as s:
        assert (await s.scalars(select(BodyWeight))).all() == []  # nothing is written before the tap


async def test_weigh_in_preview_keeps_the_prefix(settings, db):
    llm = FakeLLM(settings)
    msg = await run("вес 84,6", settings, db, llm, prefix="Распознал: «вес 84,6»\n\n")
    assert replies(msg) == ["Распознал: «вес 84,6»\n\nЗаписать вес? 84,6 кг"]


@pytest.mark.parametrize("text", ["жим 85", "вес на сегодня жим 85", "вес 84.6?"])
async def test_other_texts_go_to_the_parser(settings, db, text):
    llm = FakeLLM(settings)
    msg = await run(text, settings, db, llm)
    assert llm.parser_calls() >= 1
    assert not any(r.startswith("Записать вес?") for r in replies(msg))
    assert hbw.PENDING == {}


async def test_open_dialog_sends_a_bare_number_to_the_parser(settings, db):
    llm = FakeLLM(settings)
    question = ParseResult(kind="question", clarification="Какой вес в подходе?")
    log_text.CONTEXT[USER] = log_text.Exchange(["жим лёжа"], ["жим лёжа"], question, T0)
    llm.answers.append({"kind": "question", "clarification": "Принял."})
    msg = await run("вес 85", settings, db, llm, at=T0 + timedelta(minutes=1))
    assert llm.parser_calls() == 1
    assert replies(msg) == ["Принял."]
    assert hbw.PENDING == {}


async def test_closed_dialog_does_not_block_the_preview(settings, db):
    llm = FakeLLM(settings)
    result = ParseResult(kind="question", clarification="Ок")
    # The record's preview is gone from PENDING (saved or cancelled): the dialog is over.
    log_text.CONTEXT[USER] = log_text.Exchange(["самса"], ["самса"], result, T0, token="gone")
    msg = await run("вес 85", settings, db, llm, at=T0 + timedelta(minutes=1))
    assert replies(msg) == ["Записать вес? 85 кг"] and llm.bodies == []


# ---- 3. the confirm buttons ----


async def preview(settings, db, text="вес 84.6", at=T0, **kw):
    msg = await run(text, settings, db, FakeLLM(settings), at=at, **kw)
    return pending_token(msg)


async def rows(db) -> list[BodyWeight]:
    async with db() as s:
        return list((await s.scalars(select(BodyWeight).order_by(BodyWeight.day))).all())


async def test_save_writes_one_chat_row_and_publishes(settings, db, published):
    token = await preview(settings, db)
    cb = callback(f"bwsave:{token}")
    await hbw.save_weight(cb, settings, db)
    (row,) = await rows(db)
    assert (row.day, row.weight_kg, row.source, row.raw_text) == (DAY, Decimal("84.60"), "chat", "вес 84.6")
    assert row.measured_at.replace(tzinfo=UTC) == T0
    assert cb.message.edit_text.await_args.args[0].startswith("Вес 84,6 кг записан")
    cb.answer.assert_awaited_once_with()
    async with db() as s:
        user = await s.scalar(select(User))
        assert user.weight_kg == Decimal("84.6")  # the newest day follows into the profile
    assert published == [(user.id, ("weight", "state"))]
    assert token not in hbw.PENDING


async def test_save_day_is_the_local_date_of_the_message(settings, db):
    late = datetime(2026, 10, 7, 22, 30, tzinfo=UTC)  # 01:30 on the 8th in Moscow
    token = await preview(settings, db, at=late)
    await hbw.save_weight(callback(f"bwsave:{token}"), settings, db)
    (row,) = await rows(db)
    assert row.day == date(2026, 10, 8)


async def test_voice_weigh_in_keeps_the_voice_marker(settings, db):
    token = await preview(settings, db, raw_text="[voice] вес 84.6")
    await hbw.save_weight(callback(f"bwsave:{token}"), settings, db)
    (row,) = await rows(db)
    assert row.raw_text == "[voice] вес 84.6"


async def test_second_tap_is_stale_and_writes_nothing(settings, db, published):
    token = await preview(settings, db)
    await hbw.save_weight(callback(f"bwsave:{token}"), settings, db)
    again = callback(f"bwsave:{token}")
    await hbw.save_weight(again, settings, db)
    again.answer.assert_awaited_once_with(hbw.STALE, show_alert=True)
    again.message.edit_text.assert_not_awaited()
    assert len(await rows(db)) == 1 and len(published) == 1


async def test_another_users_tap_is_refused_and_the_preview_stays(settings, db):
    token = await preview(settings, db)
    stranger = callback(f"bwsave:{token}", user_id=99)
    await hbw.save_weight(stranger, settings, db)
    stranger.answer.assert_awaited_once_with(hbw.STALE, show_alert=True)
    stranger.message.edit_text.assert_not_awaited()
    assert token in hbw.PENDING and await rows(db) == []
    cancel = callback(f"bwdrop:{token}", user_id=99)
    await hbw.drop_weight(cancel)
    cancel.answer.assert_awaited_once_with(hbw.STALE, show_alert=True)
    assert token in hbw.PENDING
    await hbw.save_weight(callback(f"bwsave:{token}"), settings, db)  # the owner still can
    assert len(await rows(db)) == 1


async def test_drop_cancels_without_writing(settings, db, published):
    token = await preview(settings, db)
    cb = callback(f"bwdrop:{token}")
    await hbw.drop_weight(cb)
    cb.message.edit_text.assert_awaited_once_with("Отменено.")
    assert await rows(db) == [] and published == [] and token not in hbw.PENDING
    late = callback(f"bwsave:{token}")
    await hbw.save_weight(late, settings, db)
    late.answer.assert_awaited_once_with(hbw.STALE, show_alert=True)
    assert await rows(db) == []


async def test_expired_preview_is_stale(settings, db, monkeypatch):
    token = await preview(settings, db)
    monkeypatch.setattr(hbw, "utcnow", lambda: T0 + hbw.TTL + timedelta(seconds=1))
    cb = callback(f"bwsave:{token}")
    await hbw.save_weight(cb, settings, db)
    cb.answer.assert_awaited_once_with(hbw.STALE, show_alert=True)
    assert await rows(db) == []


async def test_preview_just_inside_the_ttl_still_saves(settings, db, monkeypatch):
    token = await preview(settings, db)
    monkeypatch.setattr(hbw, "utcnow", lambda: T0 + hbw.TTL - timedelta(seconds=1))
    await hbw.save_weight(callback(f"bwsave:{token}"), settings, db)
    assert len(await rows(db)) == 1


async def test_chat_save_replaces_the_day_and_reports_it(settings, db):
    for i, said in enumerate(("вес 85", "вес 84.6")):
        token = await preview(settings, db, text=said, at=T0 + timedelta(minutes=i))
        cb = callback(f"bwsave:{token}")
        await hbw.save_weight(cb, settings, db)
    (row,) = await rows(db)
    assert row.weight_kg == Decimal("84.60")
    assert "Заменил 85 кг за 08.10." in cb.message.edit_text.await_args.args[0]


async def test_saved_text_compares_with_the_previous_day(settings, db):
    async with db() as s:
        user = await get_or_create_user(s, USER, "Amir")
        await bw.upsert(s, user, date(2026, 10, 1), 85.2, T0, "miniapp")
        saved = await bw.upsert(s, user, DAY, 84.6, T0, "chat")
    assert hbw.saved_text(saved) == "Вес 84,6 кг записан ✅\n−0,6 кг к 01.10 (85,2 кг)."


async def test_pending_is_capped(settings, db):
    llm = FakeLLM(settings)
    for i in range(hbw.MAX_PENDING + 5):
        await run(f"вес {60 + i % 100}", settings, db, llm)
    assert len(hbw.PENDING) == hbw.MAX_PENDING


# ---- 4. one row per day ----


async def make_user(db, **fields):
    async with db() as s:
        user = await get_or_create_user(s, USER, "Amir")
        for k, v in fields.items():
            setattr(user, k, v)
        await s.commit()
        return user.id


async def test_upsert_same_day_replaces(db):
    async with db() as s:
        user = await get_or_create_user(s, USER, "Amir")
        first = await bw.upsert(s, user, DAY, 85, T0, "chat", raw_text="вес 85")
        assert first.replaced is None
        second = await bw.upsert(s, user, DAY, 84.6, T0 + timedelta(hours=1), "miniapp", note="после зала")
        assert second.replaced == Decimal("85.00")
        await s.commit()
    (row,) = await rows(db)
    assert (row.weight_kg, row.source, row.raw_text, row.note) == (Decimal("84.60"), "miniapp", None, "после зала")


async def test_upsert_different_days_makes_two_rows(db):
    async with db() as s:
        user = await get_or_create_user(s, USER, "Amir")
        await bw.upsert(s, user, DAY, 84.6, T0, "chat")
        saved = await bw.upsert(s, user, DAY + timedelta(days=1), 84.2, T0, "chat")
        await s.commit()
    assert [r.weight_kg for r in await rows(db)] == [Decimal("84.60"), Decimal("84.20")]
    assert saved.replaced is None and saved.before.day == DAY and saved.before.weight_kg == Decimal("84.60")


async def test_upsert_rounds_to_two_decimals(db):
    async with db() as s:
        user = await get_or_create_user(s, USER, "Amir")
        saved = await bw.upsert(s, user, DAY, 84.567, T0, "chat")
        assert saved.row.weight_kg == Decimal("84.57")
        await s.commit()
    assert (await rows(db))[0].weight_kg == Decimal("84.57")


async def test_upsert_is_per_user(db):
    async with db() as s:
        a = await get_or_create_user(s, 1, "A")
        b = await get_or_create_user(s, 2, "B")
        await bw.upsert(s, a, DAY, 80, T0, "chat")
        saved = await bw.upsert(s, b, DAY, 90, T0, "chat")
        await s.commit()
    assert saved.replaced is None and len(await rows(db)) == 2


# ---- 5. the profile rule ----


async def test_newest_day_sets_the_profile_rounded_to_a_tenth(db):
    async with db() as s:
        user = await get_or_create_user(s, USER, "Amir")
        saved = await bw.upsert(s, user, DAY, 84.66, T0, "chat")
        assert saved.profile_updated and user.weight_kg == Decimal("84.7")
        await s.commit()
    async with db() as s:
        assert (await s.scalar(select(User.weight_kg))) == Decimal("84.7")


async def test_older_day_leaves_the_profile(db):
    async with db() as s:
        user = await get_or_create_user(s, USER, "Amir")
        await bw.upsert(s, user, DAY, 84.6, T0, "chat")
        saved = await bw.upsert(s, user, DAY - timedelta(days=3), 90, T0, "miniapp")
        assert not saved.profile_updated and user.weight_kg == Decimal("84.6")
        again = await bw.upsert(s, user, DAY, 84.0, T0, "chat")  # the newest day itself still counts
        assert again.profile_updated and user.weight_kg == Decimal("84.0")


async def test_remove_newest_moves_the_profile_to_the_new_newest(db):
    async with db() as s:
        user = await get_or_create_user(s, USER, "Amir")
        await bw.upsert(s, user, DAY - timedelta(days=5), 86.0, T0, "chat")
        await bw.upsert(s, user, DAY - timedelta(days=2), 85.0, T0, "chat")
        await bw.upsert(s, user, DAY, 84.0, T0, "chat")
        assert user.weight_kg == Decimal("84.0")
        assert await bw.remove(s, user, DAY) is True
        assert user.weight_kg == Decimal("85.0")
        await s.commit()
    assert [r.day for r in await rows(db)] == [DAY - timedelta(days=5), DAY - timedelta(days=2)]


async def test_remove_older_day_leaves_the_profile(db):
    async with db() as s:
        user = await get_or_create_user(s, USER, "Amir")
        await bw.upsert(s, user, DAY - timedelta(days=5), 86.0, T0, "chat")
        await bw.upsert(s, user, DAY, 84.0, T0, "chat")
        assert await bw.remove(s, user, DAY - timedelta(days=5)) is True
        assert user.weight_kg == Decimal("84.0")


async def test_remove_the_only_row_keeps_the_profile(db):
    async with db() as s:
        user = await get_or_create_user(s, USER, "Amir")
        await bw.upsert(s, user, DAY, 84.6, T0, "chat")
        assert await bw.remove(s, user, DAY) is True
        assert user.weight_kg == Decimal("84.6")
        await s.commit()
    assert await rows(db) == []


async def test_remove_missing_day_is_false(db):
    async with db() as s:
        user = await get_or_create_user(s, USER, "Amir")
        await bw.upsert(s, user, DAY, 84.6, T0, "chat")
        assert await bw.remove(s, user, DAY - timedelta(days=1)) is False
        assert user.weight_kg == Decimal("84.6")


# ---- 6. the diary line ----


async def put(s, user, day, kg, source="chat"):
    await bw.upsert(s, user, day, kg, datetime.combine(day, datetime.min.time(), UTC), source)


async def line(db, *entries, today=DAY):
    async with db() as s:
        user = await get_or_create_user(s, USER, "Amir")
        for day, kg in entries:
            await put(s, user, day, kg)
        await s.flush()
        return await bw.context_line(s, user.id, today)


async def test_context_line_empty_without_rows(db):
    assert await line(db) == ""


async def test_context_line_single_row(db):
    assert await line(db, (DAY, 84.6)) == "Вес тела: 84,6 кг (08.10)."


async def test_context_line_with_a_week_old_reference_has_the_trend(db):
    got = await line(db, (DAY - timedelta(days=7), 85.2), (DAY, 84.6))
    assert got == "Вес тела: 84,6 кг (08.10), 7 дней назад 85,2, тренд −0,6 кг/нед."


async def test_context_line_trend_is_per_week_and_signed(db):
    got = await line(db, (DAY - timedelta(days=14), 84.0), (DAY, 85.0))
    assert "14 дней назад 84" in got and "тренд +0,5 кг/нед" in got


async def test_context_line_reference_older_than_28_days_is_ignored(db):
    got = await line(db, (DAY - timedelta(days=29), 90.0), (DAY, 84.6))
    assert got == "Вес тела: 84,6 кг (08.10)."


async def test_context_line_reference_exactly_28_days_is_used(db):
    got = await line(db, (DAY - timedelta(days=28), 86.6), (DAY, 84.6))
    assert "28 дней назад 86,6" in got and "тренд −0,5 кг/нед" in got


async def test_context_line_reference_younger_than_a_week_is_ignored(db):
    got = await line(db, (DAY - timedelta(days=6), 90.0), (DAY, 84.6))
    assert "назад" not in got


async def test_context_line_picks_the_newest_eligible_reference(db):
    got = await line(db, (DAY - timedelta(days=20), 90.0), (DAY - timedelta(days=10), 85.0), (DAY, 84.0))
    assert "10 дней назад 85, тренд −1 кг/нед" not in got  # 10 days, not 7: -1.0 / 10 * 7 = -0.7
    assert ", 10 дней назад 85, тренд −0,7 кг/нед." in got


async def test_context_line_ignores_future_rows(db):
    got = await line(db, (DAY - timedelta(days=1), 84.6), (DAY + timedelta(days=1), 99.0))
    assert got == "Вес тела: 84,6 кг (07.10)."


@pytest.mark.parametrize(
    ("gap", "words"),
    [(7, "7 дней"), (11, "11 дней"), (12, "12 дней"), (21, "21 день"), (22, "22 дня"), (24, "24 дня"), (25, "25 дней")],
)
async def test_context_line_russian_plural(db, gap, words):
    got = await line(db, (DAY - timedelta(days=gap), 85.0), (DAY, 84.0))
    assert f", {words} назад 85, тренд" in got


async def test_context_line_is_per_user(db):
    async with db() as s:
        other = await get_or_create_user(s, 7, "Other")
        me = await get_or_create_user(s, USER, "Amir")
        await put(s, other, DAY, 99.0)
        await s.flush()
        assert await bw.context_line(s, me.id, DAY) == ""


async def test_gather_appends_the_line_to_the_summary_tail(settings, db):
    now = datetime(2026, 10, 8, 9, 0, tzinfo=UTC)
    async with db() as s:
        user = await get_or_create_user(s, USER, "Amir")
        await put(s, user, DAY, 84.6)
        await s.commit()
        ctx = await answer_service.gather(s, user, settings, None, MSK, now)
    assert ctx.text.rstrip().endswith("Вес тела: 84,6 кг (08.10).")


# ---- 7. API ----


def today_msk() -> date:
    return datetime.now(MSK).date()


async def post(client, auth, kg=84.6, day=None):
    body = {"weightKg": kg} if day is None else {"weightKg": kg, "date": day.isoformat()}
    return await client.post("/api/body-weight", json=body, headers=auth)


async def test_api_requires_init_data(client):
    assert (await client.get("/api/body-weight")).status_code == 401
    assert (await client.post("/api/body-weight", json={"weightKg": 84})).status_code == 401
    assert (await client.delete(f"/api/body-weight/{DAY}")).status_code == 401
    bad = {"X-Telegram-Init-Data": init_data(token="999:other")}
    assert (await client.get("/api/body-weight", headers=bad)).status_code == 401


async def test_api_get_empty(client, auth):
    r = await client.get("/api/body-weight", headers=auth)
    assert r.status_code == 200 and r.json() == []


async def test_api_post_defaults_to_today_and_returns_the_entry(client, auth):
    r = await post(client, auth, 84.6)
    assert r.status_code == 200
    assert r.json() == {"date": today_msk().isoformat(), "weightKg": 84.6, "source": "miniapp"}


async def test_api_post_same_day_twice_keeps_one_entry(client, auth):
    await post(client, auth, 85)
    assert (await post(client, auth, 84.2)).json()["weightKg"] == 84.2
    got = (await client.get("/api/body-weight", headers=auth)).json()
    assert got == [{"date": today_msk().isoformat(), "weightKg": 84.2, "source": "miniapp"}]


async def test_api_get_is_ascending_and_filters_by_days(client, auth):
    today = today_msk()
    for back, kg in ((40, 90.0), (0, 84.0), (10, 85.0)):  # not in date order on purpose
        assert (await post(client, auth, kg, today - timedelta(days=back))).status_code == 200
    everything = (await client.get("/api/body-weight", headers=auth)).json()
    assert [e["weightKg"] for e in everything] == [90.0, 85.0, 84.0]
    assert [e["date"] for e in everything] == sorted(e["date"] for e in everything)
    assert set(everything[0]) == {"date", "weightKg", "source"}
    for days, expected in ((1, [84.0]), (10, [84.0]), (11, [85.0, 84.0]), (40, [85.0, 84.0]), (41, [90.0, 85.0, 84.0])):
        got = (await client.get(f"/api/body-weight?days={days}", headers=auth)).json()
        assert [e["weightKg"] for e in got] == expected, days


@pytest.mark.parametrize("days", [0, -1, 3661])
async def test_api_get_rejects_days_out_of_range(client, auth, days):
    assert (await client.get(f"/api/body-weight?days={days}", headers=auth)).status_code == 422


async def test_api_get_accepts_the_days_limits(client, auth):
    for days in (1, 3660):
        assert (await client.get(f"/api/body-weight?days={days}", headers=auth)).status_code == 200


@pytest.mark.parametrize("kg", [29.9, 250.1, 0, -80])
async def test_api_post_rejects_weight_out_of_range(client, auth, kg):
    assert (await post(client, auth, kg)).status_code == 422
    assert (await client.get("/api/body-weight", headers=auth)).json() == []


@pytest.mark.parametrize("kg", [30, 250])
async def test_api_post_accepts_the_weight_limits(client, auth, kg):
    assert (await post(client, auth, kg)).status_code == 200


async def test_api_post_rejects_future_and_ancient_dates(client, auth):
    assert (await post(client, auth, 84, today_msk() + timedelta(days=1))).status_code == 422
    assert (await post(client, auth, 84, date(1999, 12, 31))).status_code == 422
    assert (await client.post("/api/body-weight", json={"weightKg": 84, "date": "nope"}, headers=auth)).status_code == 422
    assert (await client.get("/api/body-weight?days=3660", headers=auth)).json() == []


async def test_api_post_backfill_is_stored_for_that_day(client, auth):
    day = today_msk() - timedelta(days=3)
    assert (await post(client, auth, 86, day)).json()["date"] == day.isoformat()


async def test_api_delete_then_get_is_empty(client, auth):
    await post(client, auth, 84.6)
    r = await client.delete(f"/api/body-weight/{today_msk()}", headers=auth)
    assert r.status_code == 204 and r.content == b""
    assert (await client.get("/api/body-weight", headers=auth)).json() == []


async def test_api_delete_missing_is_404(client, auth):
    assert (await client.delete(f"/api/body-weight/{today_msk()}", headers=auth)).status_code == 404
    assert (await client.delete("/api/body-weight/not-a-date", headers=auth)).status_code == 422


async def test_api_refuses_a_stranger_and_keeps_the_owners_data(client, auth):
    await post(client, auth, 84.6)  # the first user becomes the owner
    other = {"X-Telegram-Init-Data": init_data(user_id=43, name="Other")}
    assert (await client.get("/api/body-weight", headers=other)).status_code == 403
    assert (await post(client, other, 70)).status_code == 403
    assert (await client.delete(f"/api/body-weight/{today_msk()}", headers=other)).status_code == 403
    assert len((await client.get("/api/body-weight", headers=auth)).json()) == 1


async def test_api_writes_publish_weight_and_state(client, auth, published):
    await post(client, auth, 84.6)
    assert [t for _, t in published] == [("weight", "state")]
    await client.delete(f"/api/body-weight/{today_msk()}", headers=auth)
    assert [t for _, t in published] == [("weight", "state")] * 2
    assert len({uid for uid, _ in published}) == 1


async def test_api_failed_writes_do_not_publish(client, auth, published):
    await post(client, auth, 20)
    await client.delete(f"/api/body-weight/{today_msk()}", headers=auth)
    assert published == []


async def test_api_profile_weight_follows_the_newest_post(client, auth):
    async def profile_weight():
        return (await client.get("/api/state", headers=auth)).json()["profile"]["weightKg"]

    assert await profile_weight() is None
    await post(client, auth, 84.6)
    assert await profile_weight() == 84.6
    await post(client, auth, 90, today_msk() - timedelta(days=5))  # an older day: no change
    assert await profile_weight() == 84.6
    await post(client, auth, 83.2)  # the same (newest) day again
    assert await profile_weight() == 83.2
    await client.delete(f"/api/body-weight/{today_msk()}", headers=auth)
    assert await profile_weight() == 90.0  # the new newest day


# ---- 8. live ----


def test_weight_is_a_live_topic():
    assert "weight" in live.TOPICS


def test_hub_delivers_the_weight_topic():
    hub = live.Hub()
    sub = hub.subscribe(1)
    hub.publish(1, ["weight", "state"])
    assert sub.take() == ["state", "weight"]


# ---- 9. MCP ----


async def test_mcp_log_body_weight(tmp_path, db):
    uid = await make_owner(db)
    today = datetime.now(MSK).date()
    async with running(tmp_path, db) as (app, _), mcp_client(app) as client:
        out = data(await call(client, "log_body_weight", weight_kg=84.6))
        assert out == {"date": today.isoformat(), "weight_kg": 84.6, "replaced": None, "profile_updated": True}
        again = data(await call(client, "log_body_weight", weight_kg=84.2))
        assert again["replaced"] == 84.6
        assert (await call(client, "log_body_weight", weight_kg=20)).is_error
        future = (today + timedelta(days=1)).isoformat()
        assert (await call(client, "log_body_weight", weight_kg=84, day=future)).is_error
    (row,) = await rows(db)
    assert (row.user_id, row.day, row.weight_kg, row.source) == (uid, today, Decimal("84.20"), "mcp")


# ---- 10. migration 0011 ----


def _schema(conn) -> dict:
    insp = inspect(conn)
    out: dict = {"tables": set(insp.get_table_names())}
    if "body_weights" in out["tables"]:
        out["columns"] = {c["name"] for c in insp.get_columns("body_weights")}
        out["unique"] = [sorted(u["column_names"]) for u in insp.get_unique_constraints("body_weights")]
    return out


async def test_migration_0011_round_trip(tmp_path):
    engine, _ = make_engine(f"sqlite+aiosqlite:///{tmp_path}/m.db")

    async def run_sync(fn):
        async with engine.begin() as conn:
            return await conn.run_sync(fn)

    async def migrate_to(target: str):
        def fn(conn):
            cfg = migrate._config()
            cfg.attributes["connection"] = conn
            (command.upgrade if target == "head" else command.downgrade)(cfg, target)

        await run_sync(fn)

    insert = (
        "INSERT INTO body_weights (user_id, day, measured_at, weight_kg, source) "
        "VALUES (1, :day, '2026-10-08 09:00:00', 84.6, 'chat')"
    )
    try:
        await migrate_to("head")
        up = await run_sync(_schema)
        assert "body_weights" in up["tables"]
        assert up["columns"] == {"id", "user_id", "day", "measured_at", "weight_kg", "source", "note", "raw_text"}
        assert up["unique"] == [["day", "user_id"]]

        async with engine.begin() as conn:
            await conn.execute(text("INSERT INTO users (telegram_id, rest_seconds, created_at) VALUES (1, 90, '2026-10-01 12:00:00')"))
            await conn.execute(text(insert), {"day": "2026-10-08"})
            with pytest.raises(Exception, match="UNIQUE"):  # one row per (user, day)
                await conn.execute(text(insert), {"day": "2026-10-08"})
        async with engine.begin() as conn:
            await conn.execute(text(insert), {"day": "2026-10-09"})

        await migrate_to("0010")
        down = await run_sync(_schema)
        assert "body_weights" not in down["tables"]
        assert "active_workouts" in down["tables"]  # the previous revision is intact
        async with engine.connect() as conn:
            assert (await conn.execute(text("SELECT telegram_id FROM users"))).scalar_one() == 1

        await migrate_to("head")
        assert await run_sync(_schema) == up
        async with engine.connect() as conn:
            assert (await conn.execute(text("SELECT count(*) FROM body_weights"))).scalar_one() == 0
    finally:
        await engine.dispose()
