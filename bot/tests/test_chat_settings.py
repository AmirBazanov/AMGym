"""Mini App settings from the chat: routing, the settings model's actions, preview, apply."""

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import httpx
import pytest
from alembic import command
from sqlalchemy import inspect, select, text
from test_log_text import FakeLLM as _FakeLLM
from test_log_text import callback, food, message, workout

from gymbot.db import migrate
from gymbot.db.models import Exercise, Program, Reminder, User, UserProgram, WeightOverride
from gymbot.db.session import make_engine
from gymbot.handlers import chat_settings as hcs
from gymbot.handlers import log_text
from gymbot.llm.openrouter import LLMError
from gymbot.services import answer as answer_service
from gymbot.services import baselines, overrides
from gymbot.services import chat_settings as cs

PARSER_DEFAULT = {"kind": "question", "clarification": "Ок"}


class FakeLLM(_FakeLLM):
    """The parser now runs after every settings call: its request gets the next queued answer only when that
    one is a parser answer (no "actions" key); otherwise PARSER_DEFAULT, a non-record reply."""

    async def _handle(self, req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        is_settings = "Ты переводишь команду" in body["messages"][0]["content"]
        head = self.answers[0] if self.answers else None
        if not is_settings and (head is None or (isinstance(head, dict) and "actions" in head)):
            self.answers.insert(0, PARSER_DEFAULT)
        return await super()._handle(req)

    def settings_calls(self) -> int:
        return sum("Ты переводишь команду" in b["messages"][0]["content"] for b in self.bodies)

    def parser_calls(self) -> int:
        return len(self.bodies) - self.settings_calls()


T0 = datetime(2026, 10, 7, 9, 0, tzinfo=UTC)  # Wednesday, 12:00 in Europe/Moscow-like zones
USER = 42


@pytest.fixture(autouse=True)
def clean_state():
    for store in (hcs.SETTINGS, log_text.PENDING, log_text.CONTEXT, log_text.FACTS):
        store.clear()
    yield
    for store in (hcs.SETTINGS, log_text.PENDING, log_text.CONTEXT, log_text.FACTS):
        store.clear()


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    """Apply happens "right after" the last message: the handler's clock follows message.date."""
    now = {"t": T0}
    real = log_text.stage_settings

    async def tracked(message, *args, **kwargs):
        now["t"] = message.date
        return await real(message, *args, **kwargs)

    monkeypatch.setattr(log_text, "stage_settings", tracked)
    monkeypatch.setattr(hcs, "utcnow", lambda: now["t"])
    return now


@pytest.fixture(autouse=True)
def no_diary_answer(monkeypatch):
    async def parser_answer(message, text, result, *args):
        return result

    monkeypatch.setattr(log_text, "_diary_answer", parser_answer)


def settings_token(msg) -> str:
    kb = msg.answer.await_args.kwargs["reply_markup"]
    return kb.inline_keyboard[0][0].callback_data.split(":", 1)[1]


async def run(text, settings, db, llm, at=T0):
    msg = message(text, at=at)
    await log_text.process_text(msg, text, settings, db, llm.client)
    return msg


async def apply(token, settings, db):
    cb = callback(f"sapply:{token}")
    await hcs.apply_settings(cb, settings, db)
    return cb


async def test_targets_partial_preview_and_apply(settings, db):
    llm = FakeLLM(settings)
    async with db() as s:
        s.add(User(telegram_id=USER, rest_seconds=90, kcal_target=2500, fat_target_g=80))
        await s.commit()
    llm.answers.append({"actions": [{"type": "targets", "kcal": 2800, "protein": 170, "fat": None, "carbs": None}]})
    msg = await run("норма 2800 ккал, белок 170", settings, db, llm)
    shown = msg.answer.await_args.args[0]
    assert shown.startswith("Применить?") and "ккал 2500 → 2800" in shown and "белок — → 170 г" in shown
    # The parser still saw the text; its non-record reply gave way to the settings preview.
    assert llm.settings_calls() == 1 and llm.parser_calls() == 1 and msg.answer.await_count == 1
    cb = await apply(settings_token(msg), settings, db)
    assert cb.message.edit_text.await_args.args[0].startswith("Готово ✅")
    async with db() as s:
        u = await s.scalar(select(User))
        assert (u.kcal_target, u.protein_target_g, u.fat_target_g, u.carbs_target_g) == (2800, 170, 80, None)
    cb2 = await apply(settings_token(msg), settings, db)
    cb2.answer.assert_awaited_with(hcs.STALE, show_alert=True)


async def test_weight_override_upsert_and_unmatched(settings, db):
    llm = FakeLLM(settings)
    llm.answers.append({"actions": [
        {"type": "weight", "said": "жим", "exercise": "жим лёжа", "weight_kg": 85},
        {"type": "weight", "said": "тяга блока", "exercise": None, "weight_kg": 75},
        {"type": "weight", "said": "подъём на носки", "exercise": "подъём на носки", "weight_kg": 50},
    ]})
    msg = await run("поставь сегодня жим 85, тягу блока 75 и носки 50", settings, db, llm)
    shown = msg.answer.await_args.args[0]
    assert "жим лёжа 85 кг" in shown and "тяга вертикального блока 75 кг" in shown
    assert "«подъём на носки»" in shown
    await apply(settings_token(msg), settings, db)
    llm.answers.append({"actions": [{"type": "weight", "said": "жим", "exercise": "жим лёжа", "weight_kg": 87.5}]})
    msg = await run("поставь сегодня жим 87.5", settings, db, llm)
    assert "(было 85)" in msg.answer.await_args.args[0]
    await apply(settings_token(msg), settings, db)
    async with db() as s:
        rows = (await s.scalars(select(WeightOverride).order_by(WeightOverride.id))).all()
    assert [(float(r.weight_kg), r.day) for r in rows] == [(87.5, date(2026, 10, 7)), (75.0, date(2026, 10, 7))]


async def test_reminders_add_weekdays_and_delete_by_text(settings, db):
    llm = FakeLLM(settings)
    llm.answers.append({"actions": [
        {"type": "reminder_add", "time": "9:00", "kind": "text", "text": "Выпей креатин", "weekdays": []},
        {"type": "reminder_add", "time": "21:00", "kind": "nutrition", "text": None, "weekdays": [1, 2, 3, 4, 5]},
    ]})
    msg = await run("напоминай про креатин каждый день в 9 утра и добить белок в 21 по будням", settings, db, llm)
    shown = msg.answer.await_args.args[0]
    assert "09:00 каждый день: «Выпей креатин»" in shown and "21:00 по будням: остаток КБЖУ" in shown
    await apply(settings_token(msg), settings, db)
    async with db() as s:
        rows = (await s.scalars(select(Reminder).order_by(Reminder.id))).all()
    assert [(r.minute_of_day, r.weekday) for r in rows] == [(540, None)] + [(1260, d) for d in range(5)]
    llm.answers.append({"actions": [{"type": "reminder_delete", "ids": [], "kind": None, "about": "креатин"}]})
    msg = await run("убери напоминание про креатин", settings, db, llm)
    assert "Удалить напоминание: 09:00 каждый день: «Выпей креатин»" in msg.answer.await_args.args[0]
    await apply(settings_token(msg), settings, db)
    llm.answers.append({"actions": [{"type": "reminder_disable", "ids": [], "kind": "nutrition", "about": "КБЖУ"}]})
    msg = await run("выключи напоминания про КБЖУ", settings, db, llm)
    assert "Выключить напоминание: 21:00 по будням: остаток КБЖУ" in msg.answer.await_args.args[0]
    await apply(settings_token(msg), settings, db)
    async with db() as s:
        rows = (await s.scalars(select(Reminder))).all()
    assert len(rows) == 5 and not any(r.enabled for r in rows)


async def test_program_restart_and_rest(settings, db):
    llm = FakeLLM(settings)
    async with db() as s:
        u = User(telegram_id=USER, rest_seconds=90)
        s.add(u)
        await s.flush()
        s.add(UserProgram(user_id=u.id, program_id=1, started_on=date(2026, 9, 28)))
        await s.commit()
    llm.answers.append({"actions": [
        {"type": "program", "program": None, "start_date": "2026-10-07"},
        {"type": "rest", "seconds": 120},
    ]})
    msg = await run("начни программу заново с сегодня и таймер отдыха 2 минуты", settings, db, llm)
    shown = msg.answer.await_args.args[0]
    assert "старт с пн 05.10" in shown and "Таймер отдыха: 1:30 → 2:00" in shown
    cb = await apply(settings_token(msg), settings, db)
    assert cb.message.edit_text.await_args.args[0].startswith("Готово ✅")
    async with db() as s:
        up = await s.scalar(select(UserProgram).order_by(UserProgram.id.desc()))
        u = await s.scalar(select(User))
    assert up.started_on == date(2026, 10, 5) and u.rest_seconds == 120


async def test_no_actions_falls_through_to_parser(settings, db):
    llm = FakeLLM(settings)
    llm.answers.append({"actions": []})
    llm.answers.append({"kind": "question", "clarification": "Сделаю"})
    msg = await run("напоминай мне почаще", settings, db, llm)
    assert llm.settings_calls() == 1 and llm.parser_calls() == 1
    assert msg.answer.await_args.args[0] == "Сделаю"


async def test_remind_with_a_question_word_is_not_routed(settings, db):
    llm = FakeLLM(settings)
    llm.answers.append({"kind": "question", "clarification": "Ок"})
    msg = await run("напомни, что я ел вчера", settings, db, llm)
    assert llm.settings_calls() == 0 and msg.answer.await_args.args[0] == "Ок"


async def test_invalid_values_rejected(settings, db):
    llm = FakeLLM(settings)
    llm.answers.append({"actions": [{"type": "targets", "kcal": 10000}, {"type": "rest", "seconds": 5}]})
    llm.answers.append({"kind": "question", "clarification": "Ок"})
    msg = await run("норма 10000 ккал", settings, db, llm)
    # Understood as settings, every value out of range, the parser found no record: the notes instead.
    assert llm.parser_calls() == 1 and msg.answer.await_count == 1
    shown = msg.answer.await_args.args[0]
    assert "норма" in shown and "таймер отдыха" in shown and "reply_markup" not in msg.answer.await_args.kwargs


# ---- helpers for the extended cases ----


def no_button(msg) -> bool:
    return "reply_markup" not in msg.answer.await_args.kwargs


async def seed_user(db, telegram_id: int = USER, **fields) -> int:
    async with db() as s:
        u = User(telegram_id=telegram_id, rest_seconds=90, **fields)
        s.add(u)
        await s.commit()
        return u.id


async def seed_reminders(db, user_id: int, rows: list[dict]) -> list[int]:
    async with db() as s:
        made = [
            Reminder(user_id=user_id, **{"kind": "text", "weekday": None, "enabled": True, **row}) for row in rows
        ]
        s.add_all(made)
        await s.commit()
        return [r.id for r in made]


async def all_reminders(db) -> list[Reminder]:
    async with db() as s:
        return list((await s.scalars(select(Reminder).order_by(Reminder.id))).all())


async def all_overrides(db) -> list[WeightOverride]:
    async with db() as s:
        return list((await s.scalars(select(WeightOverride).order_by(WeightOverride.id))).all())


@pytest.fixture
async def catalog(db) -> list[str]:
    async with db() as s:
        return await baselines.catalog(s, None)


# ---- 1. routing ----

POSITIVE = [
    "норма 2800 ккал, белок 170",
    "поставь белок 180",
    "жиры 80 углеводы 350 в день",
    "норма: жиры восемьдесят углеводы триста",
    "белок 170 в день",
    "цель 2500 ккал",
    "поставь таймер 2 минуты",
    "напомни в 9 выпить креатин",
    "напоминай про креатин каждый день в 9 утра",
    "напомни добить белок в 21:00 по будням",
    "убери напоминание про креатин",
    "выключи напоминания про КБЖУ",
    "поставь сегодня жим 85",
    "выстави на сегодня присед 100, тягу блока 75",
    "поставь сегодня жим восемьдесят пять",
    "перенеси старт программы на понедельник",
    "начни программу заново с сегодня",
    "переключи на программу руки",
    "таймер отдыха 2 минуты",
    "работаю сегодня с 85 в жиме",
]
NEGATIVE = [
    "запиши жим 85 на 8",
    "жим 85 на 8",
    "сегодня жим 85 на 8",
    "съел две самсы",
    "сегодня присед сто на пять",
    "сделал жим три по десять на шестьдесят",
    "жим лёжа 3 по 10 на 60",
    "съел творог 200 г, белок 30 г",
    "выпил протеин, 30 г белка",
    "съел курицу 200 г, белок 40, жиры 10",
    "поставил рекорд в жиме 100",
    "поставь сегодня жим 85 на 8",
    "поставь сегодня жим 85 3х8",
    "поставь сегодня жим 85 два подхода",
    "какая у меня норма?",
    "выстави рабочие веса на сегодня",
    "белок 170",
    "Запиши тогда в мини-ап мою программу, чтобы я мог прийти и запустить тренировку",
]


@pytest.mark.parametrize("text_", POSITIVE)
def test_routing_settings_commands(text_):
    assert cs.is_settings_request(text_)


@pytest.mark.parametrize("text_", NEGATIVE)
def test_routing_sets_food_and_questions_stay_with_parser(text_):
    assert not cs.is_settings_request(text_)


@pytest.mark.parametrize(
    ("text_", "answer", "first_line"),
    [
        ("жим 85 на 8", workout("жим лёжа"), "Записать?"),
        ("сегодня жим 85 на 8", workout("жим лёжа"), "Записать?"),
        ("съел две самсы", food(2), "Записать еду?"),
    ],
)
async def test_negative_text_makes_exactly_one_llm_request(settings, db, text_, answer, first_line):
    llm = FakeLLM(settings)
    llm.answers.append(answer)
    msg = await run(text_, settings, db, llm)
    assert len(llm.bodies) == 1  # the parser only; a settings call would have emptied the queue
    shown = msg.answer.await_args.args[0]
    assert shown.startswith(first_line)
    assert hcs.SETTINGS == {}


# ---- 2. parse_actions ----

TARGETS_NOTE = "Не применю — норма: ккал от 800 до 6000, белок от 40, жиры от 20, углеводы от 50 до 500 г."


@pytest.mark.parametrize(
    ("item", "note_part"),
    [
        ({"type": "targets", "kcal": 799}, "норма"),
        ({"type": "targets", "kcal": 6001}, "норма"),
        ({"type": "targets", "protein": 501}, "норма"),
        ({"type": "targets", "kcal": 2800, "protein": 501}, "норма"),  # one bad field rejects the action
        ({"type": "targets"}, "норма"),
        ({"type": "reminder_add", "time": "25:00", "kind": "nutrition"}, "напоминание"),
        ({"type": "reminder_add", "time": "09:00", "kind": "nutrition", "weekdays": [0]}, "напоминание"),
        ({"type": "reminder_add", "time": "09:00", "kind": "nutrition", "weekdays": [8]}, "напоминание"),
        ({"type": "reminder_add", "time": "09:00", "kind": "text"}, "напоминание"),
        ({"type": "reminder_add", "time": "09:00", "kind": "text", "text": "  "}, "напоминание"),
        ({"type": "weight", "said": "жим", "weight_kg": 0.5}, "вес"),
        ({"type": "weight", "said": "жим", "weight_kg": 501}, "вес"),
        ({"type": "rest", "seconds": 14}, "таймер"),
        ({"type": "rest", "seconds": 601}, "таймер"),
        ({"type": "program"}, "программа"),
    ],
)
def test_parse_actions_drops_invalid_value_with_a_note(item, note_part):
    valid, notes = cs.parse_actions({"actions": [item]})
    assert valid == []
    assert len(notes) == 1 and notes[0].startswith("Не применю — ") and note_part in notes[0]


@pytest.mark.parametrize(
    "data",
    [
        {"actions": [{"type": "teleport", "where": "moon"}]},
        {"actions": [{"kind": "text"}]},
        {"actions": ["targets", 5, None, ["targets"]]},
        {"actions": "targets"},
        {"actions": None},
        {},
        None,
        [],
        "nope",
        {"kind": "question"},
    ],
)
def test_parse_actions_ignores_junk_silently(data):
    assert cs.parse_actions(data) == ([], [])


@pytest.mark.parametrize("value", [800, 6000])
def test_parse_actions_kcal_bounds_are_inclusive(value):
    valid, notes = cs.parse_actions({"actions": [{"type": "targets", "kcal": value}]})
    assert [a.kcal for a in valid] == [value] and notes == []


def test_parse_actions_mix_keeps_the_valid_ones():
    valid, notes = cs.parse_actions({"actions": [
        {"type": "targets", "kcal": 2800},
        {"type": "targets", "kcal": 6001},
        {"type": "rest", "seconds": 14},
        {"type": "weight", "said": "жим", "exercise": "жим лёжа", "weight_kg": 85},
        {"type": "reminder_add", "time": "25:00", "kind": "checkin"},
        {"type": "unknown"},
        "junk",
        {"type": "rest", "seconds": 120},
    ]})
    assert [type(a).__name__ for a in valid] == ["TargetsAction", "WeightAction", "RestAction"]
    assert valid[2].seconds == 120
    assert len(notes) == 3  # targets, rest and reminder: one note per kind of rejected action
    assert notes[0] == TARGETS_NOTE


def test_parse_actions_pads_time_and_normalizes_weekdays():
    valid, notes = cs.parse_actions({"actions": [
        {"type": "reminder_add", "time": "9:00", "kind": "nutrition"},
        {"type": "reminder_add", "time": "09:30", "kind": "advice", "weekdays": [1, 2, 3, 4, 5, 6, 7]},
        {"type": "reminder_add", "time": "10:00", "kind": "checkin", "weekdays": [5, 3, 3, 1]},
    ]})
    assert notes == []
    assert [a.time for a in valid] == ["09:00", "09:30", "10:00"]
    assert valid[0].weekdays == []
    assert valid[1].weekdays == []  # all seven days is daily
    assert valid[2].weekdays == [1, 3, 5]


def test_parse_actions_dedups_notes_of_the_same_kind():
    valid, notes = cs.parse_actions({"actions": [{"type": "rest", "seconds": 1}, {"type": "rest", "seconds": 9999}]})
    assert valid == [] and len(notes) == 1


# ---- 3. partial targets ----


async def test_targets_change_only_mentioned_fields_and_keep_none(settings, db):
    llm = FakeLLM(settings)
    await seed_user(db, kcal_target=2500, fat_target_g=80)  # protein and carbs are None
    llm.answers.append({"actions": [{"type": "targets", "carbs": 350}]})
    msg = await run("поставь углеводы 350", settings, db, llm)
    shown = msg.answer.await_args.args[0]
    assert "углеводы — → 350 г" in shown and "ккал" not in shown and "жиры" not in shown
    await apply(settings_token(msg), settings, db)
    async with db() as s:
        u = await s.scalar(select(User))
    assert (u.kcal_target, u.protein_target_g, u.fat_target_g, u.carbs_target_g) == (2500, None, 80, 350)


async def test_same_targets_are_a_note_without_button_and_without_parser(settings, db):
    llm = FakeLLM(settings)
    await seed_user(db, kcal_target=2800, protein_target_g=170)
    llm.answers.append({"actions": [{"type": "targets", "kcal": 2800, "protein": 170}]})
    msg = message("норма 2800 ккал, белок 170")
    staged = await hcs.stage_settings(msg, msg.text, settings, db, llm.client)
    assert staged is not None and not staged.ready() and staged.notes == ["Норма КБЖУ уже такая."]
    await hcs.send_staged(msg, staged)
    msg.answer.assert_awaited_once()
    assert msg.answer.await_args.args[0] == "Норма КБЖУ уже такая."
    assert no_button(msg) and hcs.SETTINGS == {}
    assert len(llm.bodies) == 1  # stage_settings itself makes only the settings call
    async with db() as s:
        u = await s.scalar(select(User))
    assert (u.kcal_target, u.protein_target_g) == (2800, 170)


async def test_same_targets_through_process_text_never_reach_the_parser(settings, db):
    llm = FakeLLM(settings)
    await seed_user(db, kcal_target=2800)
    llm.answers.append({"actions": [{"type": "targets", "kcal": 2800}]})
    msg = await run("норма 2800 ккал", settings, db, llm)
    assert msg.answer.await_args.args[0] == "Норма КБЖУ уже такая." and no_button(msg)
    assert llm.parser_calls() == 1 and msg.answer.await_count == 1 and log_text.PENDING == {}


async def test_mixed_same_and_new_targets_show_only_the_change(settings, db):
    llm = FakeLLM(settings)
    await seed_user(db, kcal_target=2800, protein_target_g=170)
    llm.answers.append({"actions": [{"type": "targets", "kcal": 2800, "protein": 180}]})
    msg = await run("норма 2800 ккал, белок 180", settings, db, llm)
    shown = msg.answer.await_args.args[0]
    assert "белок 170 → 180 г" in shown and "ккал" not in shown
    assert not no_button(msg)


# ---- 4. reminders ----


async def test_reminder_add_daily_marks_last_sent(settings, db):
    llm = FakeLLM(settings)
    llm.answers.append({"actions": [
        {"type": "reminder_add", "time": "09:00", "kind": "text", "text": "Выпей креатин", "weekdays": []},
        {"type": "reminder_add", "time": "21:00", "kind": "checkin", "weekdays": []},
    ]})
    msg = await run("напоминай про креатин каждый день в 9 утра и чекин в 21", settings, db, llm)
    await apply(settings_token(msg), settings, db)
    rows = await all_reminders(db)
    assert [(r.minute_of_day, r.weekday, r.kind, r.enabled) for r in rows] == [
        (540, None, "text", True), (1260, None, "checkin", True),
    ]
    assert all(r.last_sent_on is not None for r in rows)
    # 09:00 had passed at 12:00 local: marked done today; 21:00 is still ahead: marked yesterday
    assert rows[0].last_sent_on == date(2026, 10, 7) and rows[1].last_sent_on == date(2026, 10, 6)


async def test_reminder_weekend_days_become_rows_5_and_6(settings, db):
    llm = FakeLLM(settings)
    llm.answers.append({"actions": [
        {"type": "reminder_add", "time": "10:00", "kind": "advice", "weekdays": [6, 7]},
    ]})
    msg = await run("напоминай совет по выходным в 10", settings, db, llm)
    assert "10:00 по выходным: совет" in msg.answer.await_args.args[0]
    await apply(settings_token(msg), settings, db)
    rows = await all_reminders(db)
    assert sorted(r.weekday for r in rows) == [5, 6]
    assert all(r.last_sent_on is not None and r.minute_of_day == 600 for r in rows)


async def test_reminder_already_on_some_weekdays_only_adds_the_missing(settings, db):
    llm = FakeLLM(settings)
    uid = await seed_user(db)
    await seed_reminders(db, uid, [{"minute_of_day": 1260, "kind": "nutrition", "weekday": 0}])
    llm.answers.append({"actions": [
        {"type": "reminder_add", "time": "21:00", "kind": "nutrition", "weekdays": [1, 2, 3, 4, 5]},
    ]})
    msg = await run("напомни добить белок в 21:00 по будням", settings, db, llm)
    await apply(settings_token(msg), settings, db)
    assert sorted(r.weekday for r in await all_reminders(db)) == [0, 1, 2, 3, 4]


async def test_reminder_delete_by_explicit_ids_ignores_other_users(settings, db):
    llm = FakeLLM(settings)
    mine = await seed_user(db)
    theirs = await seed_user(db, telegram_id=7)
    (own_id,) = await seed_reminders(db, mine, [{"minute_of_day": 540, "text": "Свой"}])
    (foreign_id,) = await seed_reminders(db, theirs, [{"minute_of_day": 540, "text": "Чужой"}])
    llm.answers.append({"actions": [{"type": "reminder_delete", "ids": [own_id, foreign_id]}]})
    msg = await run("убери напоминания", settings, db, llm)
    shown = msg.answer.await_args.args[0]
    assert "Удалить напоминание: 09:00 каждый день: «Свой»" in shown and "Чужой" not in shown
    await apply(settings_token(msg), settings, db)
    assert [r.id for r in await all_reminders(db)] == [foreign_id]


async def test_reminder_delete_of_only_foreign_id_finds_nothing(settings, db):
    llm = FakeLLM(settings)
    await seed_user(db)
    theirs = await seed_user(db, telegram_id=7)
    (foreign_id,) = await seed_reminders(db, theirs, [{"minute_of_day": 540, "text": "Чужой"}])
    llm.answers.append({"actions": [{"type": "reminder_delete", "ids": [foreign_id]}]})
    msg = await run("убери напоминание", settings, db, llm)
    assert msg.answer.await_args.args[0] == "Не нашёл напоминание."
    assert no_button(msg) and hcs.SETTINGS == {}
    assert [r.id for r in await all_reminders(db)] == [foreign_id]


async def test_reminder_delete_by_kind_takes_all_its_rows(settings, db):
    llm = FakeLLM(settings)
    uid = await seed_user(db)
    await seed_reminders(db, uid, [{"minute_of_day": 1260, "kind": "nutrition", "weekday": d} for d in range(5)])
    await seed_reminders(db, uid, [{"minute_of_day": 540, "text": "Выпей креатин"}])
    llm.answers.append({"actions": [{"type": "reminder_delete", "ids": [], "kind": "nutrition", "about": None}]})
    msg = await run("убери напоминание про КБЖУ", settings, db, llm)
    assert "Удалить напоминание: 21:00 по будням: остаток КБЖУ" in msg.answer.await_args.args[0]
    await apply(settings_token(msg), settings, db)
    assert [(r.kind, r.text) for r in await all_reminders(db)] == [("text", "Выпей креатин")]


async def test_reminder_not_found_has_no_button(settings, db):
    llm = FakeLLM(settings)
    uid = await seed_user(db)
    await seed_reminders(db, uid, [{"minute_of_day": 540, "text": "Выпей креатин"}])
    llm.answers.append({"actions": [{"type": "reminder_delete", "ids": [], "kind": None, "about": "йога"}]})
    msg = await run("убери напоминание про йогу", settings, db, llm)
    assert msg.answer.await_args.args[0] == "Не нашёл напоминание «йога»."
    assert no_button(msg) and hcs.SETTINGS == {} and msg.answer.await_count == 1
    assert len(await all_reminders(db)) == 1


async def test_reminder_limit_per_user_is_respected(settings, db):
    llm = FakeLLM(settings)
    uid = await seed_user(db)
    await seed_reminders(db, uid, [{"minute_of_day": 60 + i, "text": f"n{i}"} for i in range(20)])
    llm.answers.append({"actions": [{"type": "reminder_add", "time": "21:30", "kind": "nutrition", "weekdays": []}]})
    msg = await run("напомни добить белок в 21:30", settings, db, llm)
    shown = msg.answer.await_args.args[0]
    assert "напоминаний будет больше 20" in shown and no_button(msg)
    assert len(await all_reminders(db)) == 20


async def test_reminder_limit_counts_every_weekday_row(settings, db):
    llm = FakeLLM(settings)
    uid = await seed_user(db)
    await seed_reminders(db, uid, [{"minute_of_day": 60 + i, "text": f"n{i}"} for i in range(16)])
    llm.answers.append({"actions": [
        {"type": "reminder_add", "time": "21:30", "kind": "nutrition", "weekdays": [1, 2, 3, 4, 5]},
    ]})
    msg = await run("напомни добить белок в 21:30 по будням", settings, db, llm)
    assert "напоминаний будет больше 20" in msg.answer.await_args.args[0] and no_button(msg)  # 16 + 5 > 20
    assert len(await all_reminders(db)) == 16


async def test_reminder_deleting_makes_room_for_a_new_one(settings, db):
    llm = FakeLLM(settings)
    uid = await seed_user(db)
    ids = await seed_reminders(db, uid, [{"minute_of_day": 60 + i, "text": f"n{i}"} for i in range(20)])
    llm.answers.append({"actions": [
        {"type": "reminder_delete", "ids": [ids[0]]},
        {"type": "reminder_add", "time": "21:30", "kind": "nutrition", "weekdays": []},
    ]})
    msg = await run("убери первое и напомни добить белок в 21:30", settings, db, llm)
    await apply(settings_token(msg), settings, db)
    rows = await all_reminders(db)
    assert len(rows) == 20 and ids[0] not in {r.id for r in rows}


async def test_reminder_duplicate_is_a_note_not_a_row(settings, db):
    llm = FakeLLM(settings)
    uid = await seed_user(db)
    await seed_reminders(db, uid, [{"minute_of_day": 540, "text": "Выпей креатин"}])
    llm.answers.append({"actions": [
        {"type": "reminder_add", "time": "09:00", "kind": "text", "text": "Выпей креатин", "weekdays": []},
    ]})
    msg = await run("напоминай про креатин каждый день в 9 утра", settings, db, llm)
    assert "Такое напоминание уже есть: 09:00 каждый день: «Выпей креатин»" in msg.answer.await_args.args[0]
    assert no_button(msg) and hcs.SETTINGS == {}
    assert len(await all_reminders(db)) == 1


async def test_reminder_deleted_between_preview_and_apply_still_applies(settings, db):
    llm = FakeLLM(settings)
    uid = await seed_user(db)
    await seed_reminders(db, uid, [{"minute_of_day": 540, "text": "Выпей креатин"}])
    llm.answers.append({"actions": [
        {"type": "reminder_delete", "ids": [], "kind": None, "about": "креатин"},
        {"type": "reminder_add", "time": "22:00", "kind": "checkin", "weekdays": []},
    ]})
    msg = await run("убери напоминание про креатин и добавь чекин в 22", settings, db, llm)
    token = settings_token(msg)
    async with db() as s:  # the Mini App removed it meanwhile
        await s.delete(await s.scalar(select(Reminder)))
        await s.commit()
    cb = await apply(token, settings, db)
    assert cb.message.edit_text.await_args.args[0].startswith("Готово ✅")
    cb.answer.assert_awaited_with()
    assert [(r.minute_of_day, r.kind) for r in await all_reminders(db)] == [(1320, "checkin")]


async def test_reminder_disable_and_enable_roundtrip(settings, db):
    llm = FakeLLM(settings)
    uid = await seed_user(db)
    await seed_reminders(db, uid, [{"minute_of_day": 540, "text": "Выпей креатин", "enabled": False}])
    llm.answers.append({"actions": [{"type": "reminder_enable", "ids": [], "about": "креатин"}]})
    msg = await run("включи напоминание про креатин", settings, db, llm)
    assert "Включить напоминание: 09:00 каждый день: «Выпей креатин» (выключено)" in msg.answer.await_args.args[0]
    await apply(settings_token(msg), settings, db)
    (row,) = await all_reminders(db)
    assert row.enabled is True and row.last_sent_on == date(2026, 10, 7)  # 09:00 passed: no instant fire


# ---- 5. weight overrides ----


@pytest.mark.parametrize(
    ("said", "pick", "expected"),
    [
        ("жим", None, "жим лёжа"),
        ("жим штанги", None, "жим лёжа"),
        ("тяга блока", None, "тяга вертикального блока"),
        ("присед", None, "присед со штангой"),
        ("тягу блока", "тяга вертикального блока", "тяга вертикального блока"),
        ("тягу блока", None, "тяга вертикального блока"),
        ("румынку", None, "румынская тяга"),
        ("французский", None, "французский жим лёжа"),
        ("жим лёжа", "жим лёжа", "жим лёжа"),
        ("подъём на носки", "подъём на носки", None),  # not in the program: never created
        ("бицепс", "жим лёжа", None),  # a pick sharing no word with what was said is rejected
        ("", None, None),
        (None, "жим лёжа", None),
    ],
)
def test_overrides_match_against_program_catalog(catalog, said, pick, expected):
    assert overrides.match(said, pick, catalog) == expected


def test_overrides_match_literal_catalog():
    names = ["жим лёжа", "жим лёжа 30°", "присед со штангой", "тяга вертикального блока",
             "тяга горизонтального блока", "румынская тяга", "французский жим лёжа"]
    assert overrides.match("жим", None, names) == "жим лёжа"
    assert overrides.match("тяга блока", None, names) == "тяга вертикального блока"
    assert overrides.match("тягу нижнего блока", None, names) == "тяга горизонтального блока"
    assert overrides.match("присед", None, names) == "присед со штангой"
    assert overrides.match("жим", None, ["жим лёжа 30°"]) is None  # SHORT_NAMES target missing: no guess
    assert overrides.match("бицепс", "жим лёжа", names) is None


async def test_overrides_upsert_keeps_one_row_per_user_exercise_day(db):
    uid = await seed_user(db)
    async with db() as s:
        (ex_id,) = (await s.scalars(select(Exercise.id).where(Exercise.name == "жим лёжа"))).all()
        await overrides.upsert(s, uid, ex_id, date(2026, 10, 7), 85)
        await overrides.upsert(s, uid, ex_id, date(2026, 10, 7), 87.5)
        await s.commit()
        assert [(float(o.weightKg), o.date) for o in await overrides.for_day(s, uid, date(2026, 10, 7))] == [
            (87.5, date(2026, 10, 7))
        ]
        await overrides.upsert(s, uid, ex_id, date(2026, 10, 8), 90)
        await s.commit()
    rows = await all_overrides(db)
    assert [(r.day, float(r.weight_kg)) for r in rows] == [(date(2026, 10, 7), 87.5), (date(2026, 10, 8), 90.0)]


async def test_override_day_is_the_local_date_of_the_message(settings, db):
    assert settings.timezone == "Europe/Moscow"
    late = datetime(2026, 10, 7, 22, 30, tzinfo=UTC)  # 01:30 on 8 October in Moscow
    llm = FakeLLM(settings)
    action = {"type": "weight", "said": "жим", "exercise": "жим лёжа", "weight_kg": 85}
    llm.answers.append({"actions": [action]})
    msg = await run("поставь сегодня жим 85", settings, db, llm, at=late)
    assert "(чт 08.10)" in msg.answer.await_args.args[0]
    await apply(settings_token(msg), settings, db)
    assert [(r.day, float(r.weight_kg)) for r in await all_overrides(db)] == [(date(2026, 10, 8), 85.0)]

    llm.answers.append({"actions": [{**action, "weight_kg": 90}]})
    msg = await run("поставь сегодня жим 90", settings, db, llm, at=late + timedelta(minutes=10))
    assert "(было 85)" in msg.answer.await_args.args[0]
    await apply(settings_token(msg), settings, db)
    assert [(r.day, float(r.weight_kg)) for r in await all_overrides(db)] == [(date(2026, 10, 8), 90.0)]

    llm.answers.append({"actions": [{**action, "weight_kg": 95}]})
    msg = await run("поставь сегодня жим 95", settings, db, llm, at=T0)  # 7 October locally: a new day
    assert "(было" not in msg.answer.await_args.args[0]
    await apply(settings_token(msg), settings, db)
    assert [(r.day, float(r.weight_kg)) for r in await all_overrides(db)] == [
        (date(2026, 10, 8), 90.0), (date(2026, 10, 7), 95.0),
    ]


async def test_same_weight_again_is_a_note_without_button(settings, db):
    llm = FakeLLM(settings)
    action = {"type": "weight", "said": "жим", "exercise": "жим лёжа", "weight_kg": 85}
    llm.answers.append({"actions": [action]})
    msg = await run("поставь сегодня жим 85", settings, db, llm)
    await apply(settings_token(msg), settings, db)
    llm.answers.append({"actions": [action]})
    msg = await run("поставь сегодня жим 85", settings, db, llm)
    assert msg.answer.await_args.args[0] == "жим лёжа: на сегодня уже 85 кг." and no_button(msg)


async def test_all_weights_unmatched_reply_with_note_and_write_nothing(settings, db):
    llm = FakeLLM(settings)
    llm.answers.append({"actions": [
        {"type": "weight", "said": "подъём на носки", "exercise": "подъём на носки", "weight_kg": 50},
        {"type": "weight", "said": "бицепс", "exercise": "жим лёжа", "weight_kg": 20},
    ]})
    msg = await run("поставь сегодня носки 50 и бицепс 20", settings, db, llm)
    assert msg.answer.await_args.args[0] == "Нет в программе, пропускаю: «подъём на носки», «бицепс»."
    assert no_button(msg) and hcs.SETTINGS == {} and msg.answer.await_count == 1
    assert await all_overrides(db) == []


async def test_last_weight_said_for_one_exercise_wins(settings, db):
    llm = FakeLLM(settings)
    llm.answers.append({"actions": [
        {"type": "weight", "said": "жим", "exercise": None, "weight_kg": 80},
        {"type": "weight", "said": "жим лёжа", "exercise": "жим лёжа", "weight_kg": 85},
    ]})
    msg = await run("поставь сегодня жим 80, нет, 85", settings, db, llm)
    await apply(settings_token(msg), settings, db)
    assert [float(r.weight_kg) for r in await all_overrides(db)] == [85.0]


# ---- 6. stale and foreign tokens ----


@pytest.mark.parametrize("prefix", ["sapply", "sdrop"])
async def test_unknown_token_is_stale(settings, db, prefix):
    cb = callback(f"{prefix}:deadbeef")
    await (hcs.apply_settings(cb, settings, db) if prefix == "sapply" else hcs.drop_settings(cb))
    cb.answer.assert_awaited_once_with(hcs.STALE, show_alert=True)
    cb.message.edit_text.assert_not_awaited()


async def test_someone_elses_token_is_stale_and_the_owner_can_still_apply(settings, db):
    llm = FakeLLM(settings)
    llm.answers.append({"actions": [{"type": "rest", "seconds": 120}]})
    msg = await run("таймер отдыха 2 минуты", settings, db, llm)
    token = settings_token(msg)
    for handler_cb in (
        (hcs.apply_settings, callback(f"sapply:{token}", user_id=7)),
        (hcs.drop_settings, callback(f"sdrop:{token}", user_id=7)),
    ):
        handler, cb = handler_cb
        await (handler(cb, settings, db) if handler is hcs.apply_settings else handler(cb))
        cb.answer.assert_awaited_once_with(hcs.STALE, show_alert=True)
        cb.message.edit_text.assert_not_awaited()
    assert token in hcs.SETTINGS
    async with db() as s:
        assert (await s.scalar(select(User.rest_seconds))) == 90
    cb = await apply(token, settings, db)
    assert cb.message.edit_text.await_args.args[0].startswith("Готово ✅")
    async with db() as s:
        assert (await s.scalar(select(User.rest_seconds))) == 120


async def test_drop_then_apply_is_stale(settings, db):
    llm = FakeLLM(settings)
    llm.answers.append({"actions": [{"type": "rest", "seconds": 120}]})
    msg = await run("таймер отдыха 2 минуты", settings, db, llm)
    token = settings_token(msg)
    cb = callback(f"sdrop:{token}")
    await hcs.drop_settings(cb)
    assert cb.message.edit_text.await_args.args[0] == "Отменено."
    cb2 = await apply(token, settings, db)
    cb2.answer.assert_awaited_with(hcs.STALE, show_alert=True)
    async with db() as s:
        assert (await s.scalar(select(User.rest_seconds))) == 90


# ---- 7. program ----


async def other_program(db, user_id: int) -> int:
    async with db() as s:
        p = Program(slug="other_4w", name="Другая программа")
        s.add(p)
        await s.flush()
        s.add(UserProgram(user_id=user_id, program_id=p.id, started_on=date(2026, 9, 28)))
        await s.commit()
        return p.id


@pytest.mark.parametrize(
    "said",
    ["arms_specialization_8w", "Специализация на руки, 3 дня, дропсеты (8 недель)", "руки"],
)
async def test_program_switch_by_slug_or_name(settings, db, said):
    llm = FakeLLM(settings)
    uid = await seed_user(db)
    await other_program(db, uid)
    llm.answers.append({"actions": [{"type": "program", "program": said, "start_date": None}]})
    msg = await run("переключи на программу руки", settings, db, llm)
    shown = msg.answer.await_args.args[0]
    assert "Программа «Специализация на руки, 3 дня, дропсеты (8 недель)»" in shown
    assert "вместо «Другая программа»" in shown and "старт с пн 28.09" in shown
    await apply(settings_token(msg), settings, db)
    async with db() as s:
        up = await s.scalar(select(UserProgram).order_by(UserProgram.id.desc()))
        slug = await s.scalar(select(Program.slug).where(Program.id == up.program_id))
    assert (slug, up.started_on) == ("arms_specialization_8w", date(2026, 9, 28))


async def test_program_unknown_name_is_a_note(settings, db):
    llm = FakeLLM(settings)
    llm.answers.append({"actions": [{"type": "program", "program": "пауэрлифтинг", "start_date": None}]})
    msg = await run("переключи на программу пауэрлифтинг", settings, db, llm)
    assert msg.answer.await_args.args[0] == "Не нашёл программу «пауэрлифтинг»."
    assert no_button(msg) and hcs.SETTINGS == {}
    async with db() as s:
        assert len((await s.scalars(select(UserProgram))).all()) == 1  # just the default one


@pytest.mark.parametrize("start", ["2027-03-01", "2025-01-01"])
async def test_program_start_too_far_changes_nothing(settings, db, start):
    llm = FakeLLM(settings)
    llm.answers.append({"actions": [{"type": "program", "program": None, "start_date": start}]})
    msg = await run("перенеси старт программы", settings, db, llm)
    assert "слишком далеко" in msg.answer.await_args.args[0] and no_button(msg)
    async with db() as s:
        (up,) = (await s.scalars(select(UserProgram))).all()
    assert up.started_on == date(2026, 10, 5)


async def test_program_start_92_days_ahead_is_still_allowed(settings, db):
    llm = FakeLLM(settings)
    far = date(2026, 10, 7) + timedelta(days=92)  # Wednesday: its Monday is earlier, inside the limit
    llm.answers.append({"actions": [{"type": "program", "program": None, "start_date": far.isoformat()}]})
    msg = await run("перенеси старт программы", settings, db, llm)
    assert "слишком далеко" not in msg.answer.await_args.args[0] and not no_button(msg)


@pytest.mark.parametrize("start", ["2026-10-12", "2026-10-14"])
async def test_program_future_monday_preview_says_it_starts_later(settings, db, start):
    llm = FakeLLM(settings)
    llm.answers.append({"actions": [{"type": "program", "program": None, "start_date": start}]})
    msg = await run("перенеси старт программы на понедельник", settings, db, llm)
    shown = msg.answer.await_args.args[0]
    assert "старт с пн 12.10 (начнётся пн 12.10; было с пн 05.10)" in shown
    await apply(settings_token(msg), settings, db)
    async with db() as s:
        up = await s.scalar(select(UserProgram).order_by(UserProgram.id.desc()))
    assert up.started_on == date(2026, 10, 12)


async def test_program_same_start_is_a_note(settings, db):
    llm = FakeLLM(settings)
    llm.answers.append({"actions": [{"type": "program", "program": None, "start_date": "2026-10-07"}]})
    msg = await run("начни программу заново с сегодня", settings, db, llm)  # this week's Monday is 05.10 already
    assert "и так идёт со старта пн 05.10" in msg.answer.await_args.args[0] and no_button(msg)


# ---- 8. the settings call fails ----


async def test_llm_error_in_settings_call_falls_through_to_parser(settings, db, monkeypatch):
    llm = FakeLLM(settings)

    async def broken(messages, prefer="kind", **_kw):
        raise LLMError("all models failed")

    monkeypatch.setattr(llm.client, "complete_json", broken)
    llm.answers.append({"kind": "question", "clarification": "Ок"})
    msg = await run("поставь белок 180", settings, db, llm)
    assert len(llm.bodies) == 1  # the parser's request
    assert msg.answer.await_args.args[0] == "Ок" and hcs.SETTINGS == {}


async def test_non_json_settings_answer_falls_through_to_parser(settings, db):
    llm = FakeLLM(settings)
    llm.answers.extend(["nope"] * (2 * len(llm.client.routes)))  # two attempts on every route
    llm.answers.append({"kind": "question", "clarification": "Ок"})
    msg = await run("поставь белок 180", settings, db, llm)
    assert len(llm.bodies) == 2 * len(llm.client.routes) + 1
    assert msg.answer.await_args.args[0] == "Ок" and hcs.SETTINGS == {}


async def test_settings_call_prefers_the_actions_key(settings, db):
    """An inner action with a "kind" key must not be taken for the whole answer."""
    llm = FakeLLM(settings)
    llm.answers.append({"actions": [{"type": "reminder_add", "time": "09:00", "kind": "nutrition", "weekdays": []}]})
    msg = await run("напомни добить белок в 9", settings, db, llm)
    assert "09:00 каждый день: остаток КБЖУ" in msg.answer.await_args.args[0]
    assert llm.settings_calls() == 1


# ---- 10. diary answer context ----


async def test_answer_context_has_todays_overrides_only(settings, db):
    tz = ZoneInfo(settings.timezone)
    now = datetime(2026, 10, 7, 22, 30, tzinfo=UTC)  # 8 October locally
    today = now.astimezone(tz).date()
    uid = await seed_user(db)
    async with db() as s:
        user = await s.get(User, uid)
        before = await answer_service.build_context(s, user, settings, None, tz, now)
        assert "Веса на сегодня" not in before
        ids = dict((await s.execute(select(Exercise.name, Exercise.id))).all())
        s.add(WeightOverride(user_id=uid, exercise_id=ids["жим лёжа"], day=today, weight_kg=Decimal(85)))
        s.add(WeightOverride(user_id=uid, exercise_id=ids["присед со штангой"], day=today, weight_kg=Decimal("87.5")))
        s.add(WeightOverride(user_id=uid, exercise_id=ids["румынская тяга"], day=today - timedelta(days=1),
                             weight_kg=Decimal(60)))
        await s.commit()
        ctx = await answer_service.build_context(s, user, settings, None, tz, now)
    assert ctx.endswith("Веса на сегодня, выставленные в чате: жим лёжа 85 кг, присед со штангой 87.5 кг.")
    assert "румынская тяга 60" not in ctx


# ---- 11. migration 0009 ----


def _schema(conn) -> dict:
    insp = inspect(conn)
    tables = set(insp.get_table_names())
    out: dict = {"tables": tables}
    if "weight_overrides" in tables:
        out["columns"] = {c["name"] for c in insp.get_columns("weight_overrides")}
        out["unique"] = [sorted(u["column_names"]) for u in insp.get_unique_constraints("weight_overrides")]
    return out


async def test_migration_0009_round_trip(tmp_path):
    engine, _ = make_engine(f"sqlite+aiosqlite:///{tmp_path}/m.db")

    def run_sync(fn):
        async def go():
            async with engine.begin() as conn:
                return await conn.run_sync(fn)

        return go()

    def migrate_to(target: str):
        def fn(conn):
            cfg = migrate._config()
            cfg.attributes["connection"] = conn
            (command.upgrade if target == "head" else command.downgrade)(cfg, target)

        return run_sync(fn)

    try:
        await migrate_to("head")
        up = await run_sync(_schema)
        assert "weight_overrides" in up["tables"]
        assert up["columns"] == {"id", "user_id", "exercise_id", "day", "weight_kg", "created_at"}
        assert up["unique"] == [["day", "exercise_id", "user_id"]]

        async with engine.begin() as conn:
            await conn.execute(text("INSERT INTO users (telegram_id, rest_seconds, created_at) VALUES (1, 90, '2026-10-01 12:00:00')"))
            await conn.execute(text("INSERT INTO exercises (name, aliases) VALUES ('жим лёжа', '[]')"))
            await conn.execute(
                text("INSERT INTO weight_overrides (user_id, exercise_id, day, weight_kg, created_at) "
                     "VALUES (1, 1, '2026-10-07', 85, '2026-10-07 09:00:00')")
            )  # fmt: skip
            with pytest.raises(Exception, match="UNIQUE"):  # one row per (user, exercise, day)
                await conn.execute(
                    text("INSERT INTO weight_overrides (user_id, exercise_id, day, weight_kg, created_at) "
                         "VALUES (1, 1, '2026-10-07', 90, '2026-10-07 09:05:00')")
                )  # fmt: skip

        await migrate_to("0008")
        down = await run_sync(_schema)
        assert "weight_overrides" not in down["tables"]
        assert "exercise_baselines" in down["tables"]  # the previous revision is intact
        async with engine.connect() as conn:
            assert (await conn.execute(text("SELECT telegram_id FROM users"))).scalar_one() == 1
            assert (await conn.execute(text("SELECT name FROM exercises"))).scalar_one() == "жим лёжа"

        await migrate_to("head")
        assert await run_sync(_schema) == up
        async with engine.connect() as conn:
            assert (await conn.execute(text("SELECT count(*) FROM weight_overrides"))).scalar_one() == 0
            assert (await conn.execute(text("SELECT telegram_id FROM users"))).scalar_one() == 1
    finally:
        await engine.dispose()


# ---- review fixes: mixed messages, open dialogs, bounds, errors, TTL, reminders ----

# Messages with a record in them, from both reviews: whatever the routing, the parser's record is shown.
RECORD_PHRASES = [
    ("съел плов, напомни про креатин в 9", "food"),
    ("норма калорий 2500 а сегодня съел плов", "food"),
    ("поставил таймер на 2 минуты, жим 80 на 8", "workout"),
    ("таймер 90 секунд жим 80 на 8", "workout"),
    ("по таймеру отдыхал 3 минуты между подходами жим 90 на 5", "workout"),
    ("поменял программу, сегодня присед 100 на 5", "workout"),
    ("запустил программу, первая тренировка: жим 80 на 8", "workout"),
    ("переключился на новую программу, жим 80 на 8", "workout"),
    ("сделал программу заново, жим 80 на 8", "workout"),
    ("норма, сделал жим 80 на 10", "workout"),
    ("бот напомнил, жим 80 на 8 сделал", "workout"),
    ("напомнило про креатин, выпил 5 г креатина", "food"),
    ("напоминалка сработала, съел творог 200 г", "food"),
    ("норма белка 160, съел курицу 200 г", "food"),
    ("съел в норму калорий, курица 200", "food"),
    ("белок 20 жиры 8 углеводы 25", "food"),
    ("поужинал пловом, напомни про креатин в 9", "food"),
    ("плов 300 г, напомни про креатин в 9", "food"),
    ("жим 80 8 раз, напомни про креатин в 9", "workout"),
    ("жим 85 пятерка, таймер отдыха 3 минуты", "workout"),
    ("пообедал, норма калорий 2500", "food"),
    ("Обед курица 200 г, в день это уже 120 белка", "food"),
    ("работаю сегодня с 85 в жиме пятерки", "workout"),
    ("поставь сегодня жим 85, вчера жал 80", "workout"),
    ("Сегодня белок 20 жиры 8 углеводы 25 на день", "food"),
    ("смени программу, сегодня жим 80 8 раз", "workout"),
]
FOOD_RECORD = {"kind": "food", "foods": [
    {"description": "плов", "grams": 300, "kcal": 500, "protein_g": 15, "fat_g": 20, "carbs_g": 60}]}
WORKOUT_RECORD = {"kind": "workout", "exercises": [
    {"exercise": "жим лёжа", "sets": [{"reps": 8, "weight_kg": 80, "drop_index": 0}]}]}
# A settings answer that would change something, so the settings preview appears next to the record.
SOME_SETTINGS = {"actions": [
    {"type": "reminder_add", "time": "09:00", "kind": "text", "text": "Выпей креатин", "weekdays": []},
    {"type": "targets", "kcal": 2500},
    {"type": "weight", "said": "жим", "exercise": "жим лёжа", "weight_kg": 85},
]}


@pytest.mark.parametrize(("text", "kind"), RECORD_PHRASES)
async def test_a_record_in_the_message_is_never_lost(settings, db, text, kind):
    llm = FakeLLM(settings)
    routed = cs.is_settings_request(text)
    if routed:
        llm.answers.append(SOME_SETTINGS)
    llm.answers.append(FOOD_RECORD if kind == "food" else WORKOUT_RECORD)
    msg = await run(text, settings, db, llm)
    assert llm.parser_calls() == 1 and llm.answers == []
    first = msg.answer.await_args_list[0].args[0]
    assert first.startswith("Записать еду?" if kind == "food" else "Записать?"), first
    assert len(log_text.PENDING) == 1  # the record waits for "Сохранить"
    if routed:  # the settings preview, if any, is a separate second message with its own buttons
        assert msg.answer.await_count == 2 and msg.answer.await_args.args[0].startswith("Применить?")
    else:
        assert msg.answer.await_count == 1 and hcs.SETTINGS == {}


async def test_setting_next_to_a_record_gives_both_previews(settings, db):
    llm = FakeLLM(settings)
    llm.answers.append({"actions": [
        {"type": "reminder_add", "time": "09:00", "kind": "text", "text": "Выпей креатин", "weekdays": []}]})
    llm.answers.append(FOOD_RECORD)
    msg = await run("съел плов, напомни про креатин в 9", settings, db, llm)
    texts = [c.args[0] for c in msg.answer.await_args_list]
    assert texts[0].startswith("Записать еду?") and texts[1].startswith("Применить?")
    assert "«Выпей креатин»" in texts[1]
    assert len(log_text.PENDING) == 1 and len(hcs.SETTINGS) == 1


async def test_settings_notes_are_dropped_next_to_a_record(settings, db):
    llm = FakeLLM(settings)
    await seed_user(db, kcal_target=2500)
    llm.answers.append({"actions": [{"type": "targets", "kcal": 2500}]})
    llm.answers.append(FOOD_RECORD)
    msg = await run("норма калорий 2500 а сегодня съел плов", settings, db, llm)
    assert msg.answer.await_count == 1 and msg.answer.await_args.args[0].startswith("Записать еду?")


async def test_override_echoed_as_a_workout_is_dropped(settings, db):
    llm = FakeLLM(settings)
    llm.answers.append({"actions": [
        {"type": "weight", "said": "жим", "exercise": "жим лёжа", "weight_kg": 85},
        {"type": "weight", "said": "присед", "exercise": "присед со штангой", "weight_kg": 100},
    ]})
    llm.answers.append({"kind": "workout", "exercises": [
        {"exercise": "жим лёжа", "sets": [{"reps": 1, "weight_kg": 85, "drop_index": 0}]},
        {"exercise": "присед", "sets": [{"reps": 1, "weight_kg": 100, "drop_index": 0}]},
    ]})
    msg = await run("поставь сегодня жим 85 и присед 100", settings, db, llm)
    assert msg.answer.await_count == 1 and msg.answer.await_args.args[0].startswith("Применить?")
    assert log_text.PENDING == {}


@pytest.mark.parametrize(("parsed", "why"), [
    ([("жим лёжа", 80)], "another weight"),
    ([("жим лёжа", 85), ("румынская тяга", 60)], "an exercise not in the command"),
])
async def test_workout_that_is_more_than_the_override_is_kept(settings, db, parsed, why):
    llm = FakeLLM(settings)
    llm.answers.append({"actions": [{"type": "weight", "said": "жим", "exercise": "жим лёжа", "weight_kg": 85}]})
    llm.answers.append({"kind": "workout", "exercises": [
        {"exercise": n, "sets": [{"reps": 5, "weight_kg": kg, "drop_index": 0}]} for n, kg in parsed]})
    msg = await run("поставь сегодня жим 85", settings, db, llm)
    assert msg.answer.await_count == 2, why
    assert msg.answer.await_args_list[0].args[0].startswith("Записать?")


def test_rep_signals_keep_the_workout():
    staged = hcs.Staged(cs.Plan(day=date(2026, 10, 7), said_weights={"жим лёжа": 85.0}), [], ["жим лёжа"])
    echo = log_text.ParseResult.model_validate(
        {"kind": "workout", "exercises": [{"exercise": "жим лёжа", "sets": [{"reps": 5, "weight_kg": 85}]}]})
    assert staged.fake_workout(echo, "поставь сегодня жим 85")
    assert staged.fake_workout(echo, "поставь пожалуйста сегодня жим 85")
    for phrase in ("поставь сегодня жим 85 на 5", "поставь сегодня жим 85 пятерка", "поставь сегодня жим 85 5 раз",
                 "поставь сегодня жим 85, получилось 6", "поставь сегодня жим 85, вчера жал 80"):
        assert not staged.fake_workout(echo, phrase), phrase


async def test_open_clarification_without_a_record_blocks_settings(settings, db):
    llm = FakeLLM(settings)
    unclear = log_text.ParseResult.model_validate({"kind": "unknown", "clarification": "Это что?"})
    log_text.CONTEXT[USER] = log_text.Exchange(["курт"], ["курт"], unclear, T0)  # token None: the model asks
    llm.answers.append(FOOD_RECORD)
    await run("норма 2800 ккал", settings, db, llm, at=T0 + timedelta(minutes=1))
    assert llm.settings_calls() == 0 and llm.parser_calls() == 1 and hcs.SETTINGS == {}


async def test_open_question_about_a_record_blocks_settings(settings, db):
    llm = FakeLLM(settings)
    rec = log_text.ParseResult.model_validate(FOOD_RECORD)
    q = log_text.Question(["а сколько там белка"], ["а сколько там белка"],
                          log_text.ParseResult.model_validate({"kind": "unknown", "clarification": "Уточни"}))
    log_text.CONTEXT[USER] = log_text.Exchange(["плов"], ["плов"], rec, T0, "stale", q)  # token not in PENDING
    llm.answers.append(FOOD_RECORD)
    await run("белок 170 в день", settings, db, llm, at=T0 + timedelta(minutes=1))
    assert llm.settings_calls() == 0


async def test_no_weight_for_today_from_a_logged_set(settings, db):
    llm = FakeLLM(settings)
    llm.answers.append({"actions": [
        {"type": "weight", "said": "жим", "exercise": "жим лёжа", "weight_kg": 80},
        {"type": "reminder_add", "time": "09:00", "kind": "text", "text": "креатин", "weekdays": []},
    ]})
    llm.answers.append(WORKOUT_RECORD)
    msg = await run("жим 80 8 раз, напомни про креатин в 9", settings, db, llm)
    texts = [c.args[0] for c in msg.answer.await_args_list]
    assert texts[0].startswith("Записать?") and texts[1].startswith("Применить?")
    assert "Вес на сегодня" not in texts[1] and "«креатин»" in texts[1]


async def test_one_off_reminder_is_a_note(settings, db):
    llm = FakeLLM(settings)
    llm.answers.append({"actions": [{"type": "one_off_reminder"}]})
    msg = await run("напомни мне завтра в 9 про тренировку", settings, db, llm)
    assert msg.answer.await_args.args[0] == cs.ONE_OFF and no_button(msg)
    assert await all_reminders(db) == []


async def test_settings_answer_when_the_parser_is_down(settings, db, monkeypatch):
    llm = FakeLLM(settings)

    async def down(*args, **kwargs):
        raise LLMError("all routes failed")

    llm.answers.append({"actions": [{"type": "rest", "seconds": 120}]})
    monkeypatch.setattr(llm.client, "parse_message", down)
    msg = await run("таймер отдыха 2 минуты", settings, db, llm)
    assert msg.answer.await_args.args[0].startswith("Применить?")


async def test_open_preview_is_answered_not_turned_into_a_norm(settings, db):
    llm = FakeLLM(settings)
    llm.answers.append(food(1, "батончик"))
    await run("съел батончик", settings, db, llm)
    llm.answers.append(food(1, "батончик", revises=True))
    await run("норма 2800 ккал", settings, db, llm, at=T0 + timedelta(minutes=1))
    # The food preview is open: the norm-looking text goes to the parser as its revision.
    assert len(llm.bodies) == 2 and all("Ты переводишь команду" not in b["messages"][0]["content"] for b in llm.bodies)
    assert hcs.SETTINGS == {}


async def test_after_the_preview_is_saved_settings_route_again(settings, db):
    llm = FakeLLM(settings)
    llm.answers.append(food(1, "батончик"))
    first = await run("съел батончик", settings, db, llm)
    await log_text.save(callback(f"save:{log_text.PENDING and next(iter(log_text.PENDING))}"), settings, db)
    assert log_text.CONTEXT == {}
    llm.answers.append({"actions": [{"type": "targets", "kcal": 2800}]})
    msg = await run("норма 2800 ккал", settings, db, llm, at=T0 + timedelta(minutes=1))
    assert first is not msg and msg.answer.await_args.args[0].startswith("Применить?")


@pytest.mark.parametrize("field,low", [("protein", 39), ("fat", 19), ("carbs", 49), ("kcal", 799)])
def test_daily_target_lower_bounds(field, low):
    valid, notes = cs.parse_actions({"actions": [{"type": "targets", field: low}]})
    assert valid == [] and notes == [TARGETS_NOTE]
    valid, _ = cs.parse_actions({"actions": [{"type": "targets", field: low + 1}]})
    assert len(valid) == 1


async def test_any_error_in_the_settings_path_falls_through(settings, db, monkeypatch):
    llm = FakeLLM(settings)

    async def broken(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(cs, "resolve", broken)
    llm.answers.append({"actions": [{"type": "targets", "kcal": 2800}]})
    llm.answers.append({"kind": "question", "clarification": "Ок"})
    msg = await run("норма 2800 ккал", settings, db, llm)
    assert len(llm.bodies) == 2 and msg.answer.await_args.args[0] == "Ок"


async def test_old_preview_is_stale(settings, db, clock):
    llm = FakeLLM(settings)
    llm.answers.append({"actions": [{"type": "rest", "seconds": 120}]})
    msg = await run("таймер отдыха 2 минуты", settings, db, llm)
    clock["t"] = T0 + hcs.TTL + timedelta(seconds=1)
    cb = await apply(settings_token(msg), settings, db)
    cb.answer.assert_awaited_with(hcs.STALE, show_alert=True)
    async with db() as s:
        assert (await s.scalar(select(User))).rest_seconds == 90


async def test_weight_for_a_day_that_passed_is_not_written(settings, db, clock):
    llm = FakeLLM(settings)
    late = datetime(2026, 10, 7, 20, 55, tzinfo=UTC)  # 23:55 in Moscow
    llm.answers.append({"actions": [{"type": "weight", "said": "жим", "exercise": "жим лёжа", "weight_kg": 85}]})
    msg = await run("поставь сегодня жим 85", settings, db, llm, at=late)
    clock["t"] = late + timedelta(minutes=10)  # 00:05 the next local day, still within TTL
    cb = await apply(settings_token(msg), settings, db)
    assert "тот день уже прошёл" in cb.message.edit_text.await_args.args[0]
    assert await all_overrides(db) == []


async def test_daily_reminder_covers_weekday_ones(settings, db):
    uid = await seed_user(db)
    await seed_reminders(db, uid, [{"minute_of_day": 540, "text": "Выпей креатин"}])
    llm = FakeLLM(settings)
    llm.answers.append({"actions": [
        {"type": "reminder_add", "time": "09:00", "kind": "text", "text": "Выпей креатин", "weekdays": [1, 3]}]})
    msg = await run("напоминай про креатин в 9 по пн и ср", settings, db, llm)
    assert "Такое напоминание уже есть" in msg.answer.await_args.args[0]
    assert "reply_markup" not in msg.answer.await_args.kwargs
    assert len(await all_reminders(db)) == 1


async def test_disabled_duplicate_is_switched_on_instead(settings, db):
    uid = await seed_user(db)
    [rid] = await seed_reminders(db, uid, [{"minute_of_day": 540, "text": "Выпей креатин", "enabled": False}])
    llm = FakeLLM(settings)
    llm.answers.append({"actions": [
        {"type": "reminder_add", "time": "09:00", "kind": "text", "text": "Выпей креатин", "weekdays": []}]})
    msg = await run("напоминай про креатин каждый день в 9 утра", settings, db, llm)
    shown = msg.answer.await_args.args[0]
    assert "Включить напоминание: 09:00 каждый день: «Выпей креатин» (выключено)" in shown
    assert "Новое напоминание" not in shown
    await apply(settings_token(msg), settings, db)
    rows = await all_reminders(db)
    assert [(r.id, r.enabled) for r in rows] == [(rid, True)]


async def test_duplicate_added_meanwhile_is_not_doubled_at_apply(settings, db):
    uid = await seed_user(db)
    llm = FakeLLM(settings)
    llm.answers.append({"actions": [
        {"type": "reminder_add", "time": "09:00", "kind": "text", "text": "Выпей креатин", "weekdays": []}]})
    msg = await run("напоминай про креатин каждый день в 9 утра", settings, db, llm)
    await seed_reminders(db, uid, [{"minute_of_day": 540, "text": "Выпей креатин"}])  # the Mini App, meanwhile
    cb = await apply(settings_token(msg), settings, db)
    assert "уже есть, не дублирую" in cb.message.edit_text.await_args.args[0]
    assert len(await all_reminders(db)) == 1


@pytest.mark.parametrize("about,text,hit", [
    ("креатин", "Выпей креатин", True),
    ("выпить креатин", "Выпей креатин", True),
    ("витамин д", "Выпей креатин", False),
    ("выпить", "Выпей креатин", False),  # only a verb: about nothing
    ("креатин и омегу", "Выпей креатин", False),  # every word has to be there
])
def test_about_matching_is_narrow(about, text, hit):
    r = Reminder(minute_of_day=540, kind="text", text=text, weekday=None, enabled=True)
    assert cs._about_matches(about, r) is hit
