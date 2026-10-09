"""Program edits from the chat through log_text.process_text: preview, buttons, apply, stale and foreign taps."""

import json
from datetime import UTC, date, datetime
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import func, select
from test_log_text import FakeLLM as _FakeLLM
from test_log_text import callback, message

from gymbot.db.models import DeloadState, Program
from gymbot.handlers import chat_edit as hce
from gymbot.handlers import log_text
from gymbot.services import chat_edit as ce
from gymbot.services import program_editor as pe
from gymbot.services.users import active_program, get_or_create_user

PARSER_DEFAULT = {"kind": "question", "clarification": "Ок"}
EDIT_HEAD = "Ты меняешь программу тренировок"
T0 = datetime(2026, 10, 9, 9, 0, tzinfo=UTC)  # Friday: program week 1
TODAY = date(2026, 10, 9)
USER = 42
FRENCH = "французский жим лёжа"


class FakeLLM(_FakeLLM):
    """Edit requests (system prompt starts with EDIT_HEAD) take the queued answer; a parser request takes it
    only when it is a parser answer (no "actions" key), else PARSER_DEFAULT, a non-record reply."""

    async def _handle(self, req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        is_edit = body["messages"][0]["content"].startswith(EDIT_HEAD)
        head = self.answers[0] if self.answers else None
        if not is_edit and (head is None or (isinstance(head, dict) and "actions" in head)):
            self.answers.insert(0, PARSER_DEFAULT)
        return await super()._handle(req)

    def edit_bodies(self) -> list[dict]:
        return [b for b in self.bodies if b["messages"][0]["content"].startswith(EDIT_HEAD)]

    def edit_calls(self) -> int:
        return len(self.edit_bodies())

    def parser_calls(self) -> int:
        return len(self.bodies) - self.edit_calls()


@pytest.fixture(autouse=True)
def clean_state():
    for store in (hce.EDITS, log_text.PENDING, log_text.CONTEXT, log_text.FACTS):
        store.clear()
    yield
    for store in (hce.EDITS, log_text.PENDING, log_text.CONTEXT, log_text.FACTS):
        store.clear()


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr(hce, "utcnow", lambda: T0)


@pytest.fixture(autouse=True)
def no_diary_answer(monkeypatch):
    async def parser_answer(message, text, result, *args):
        return result, None

    monkeypatch.setattr(log_text, "_diary_answer", parser_answer)


@pytest.fixture(autouse=True)
def published(monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(hce.live, "publish", lambda *args: calls.append(args))
    return calls


@pytest.fixture
def llm(settings):
    return FakeLLM(settings)


def remove_french(day=None):
    return {"actions": [{
        "type": "remove", "day": day or {"weekday": None, "focus": "руки", "when": None},
        "exercise": "французский жим", "scope": None,
    }], "summary": "Убрать французский жим"}


def add_pullups():
    return {"actions": [{
        "type": "add", "day": {"weekday": None, "focus": "руки", "when": None}, "name": "подтягивания",
        "sets": 3, "repsMin": 8, "repsMax": None, "dropReps": None, "after": None, "scope": None,
    }], "summary": "Подтягивания"}


async def run(text, settings, db, llm, at=T0):
    msg = message(text, at=at)
    await log_text.process_text(msg, text, settings, db, llm.client)
    return msg


def buttons(msg) -> list[str]:
    kb = msg.answer.await_args.kwargs["reply_markup"]
    return [b.callback_data for row in kb.inline_keyboard for b in row]


def token_of(msg) -> str:
    return buttons(msg)[0].split(":")[1]


def shown(msg) -> str:
    return msg.answer.await_args.args[0]


async def active_snapshot(db) -> ce.Snapshot:
    async with db() as s:
        user = await get_or_create_user(s, USER)
        snap = await ce.load_snapshot(s, user, TODAY)
        await s.commit()
    return snap


async def owned_programs(db) -> int:
    async with db() as s:
        return await s.scalar(select(func.count()).select_from(Program).where(Program.owner_user_id.is_not(None)))


def names(snap, week=1, weekday=5) -> list[str]:
    return [i.name for i in snap.day(week, weekday).items]


async def test_preview_apply_and_double_tap(settings, db, llm, published, monkeypatch):
    llm.answers.append(remove_french({"weekday": None, "focus": "руки", "when": None}))
    handled = AsyncMock(return_value=False)
    monkeypatch.setattr(log_text.saved_edits, "handle", handled)
    msg = await run("убери французский жим из дня рук", settings, db, llm)
    assert llm.edit_calls() == 1 and llm.parser_calls() == 0
    handled.assert_not_awaited()
    assert msg.answer.await_count == 1
    assert "<b>Что изменю:</b>" in shown(msg) and msg.answer.await_args.kwargs["parse_mode"] == "HTML"
    assert FRENCH in shown(msg) and "Создам твою копию" in shown(msg)
    token = token_of(msg)
    assert buttons(msg) == [f"eapply:{token}", f"edrop:{token}"]
    assert await owned_programs(db) == 0  # the preview wrote nothing

    cb = callback(f"eapply:{token}")
    await hce.apply_edit(cb, settings, db)
    assert FRENCH not in names(await active_snapshot(db))
    assert len(published) == 1 and published[0][1:] == ("program", "plan", "state")
    assert "Готово" in cb.message.edit_text.await_args.args[0] and FRENCH in cb.message.edit_text.await_args.args[0]
    assert await owned_programs(db) == 1

    again = callback(f"eapply:{token}")
    await hce.apply_edit(again, settings, db)
    again.answer.assert_awaited_with(hce.STALE, show_alert=True)
    again.message.edit_text.assert_not_awaited()
    assert len(published) == 1 and await owned_programs(db) == 1


async def test_weight_command_applies_a_day_weight_and_publishes_state(settings, db, llm, published):
    llm.answers.append({"actions": [{
        "type": "weight", "said": "сгибания с супинацией",
        "exercise": "сгибания с гантелями на бицепс с супинацией", "weight_kg": 30, "date": None,
    }], "summary": ""})
    msg = await run("поставь на сгибания с супинацией 30 кг", settings, db, llm)
    assert "Вес на пн 12.10" in shown(msg) and "Создам" not in shown(msg)
    cb = callback(f"eapply:{token_of(msg)}")
    await hce.apply_edit(cb, settings, db)
    assert published[0][1:] == ("state",) and await owned_programs(db) == 0
    assert "Готово" in cb.message.edit_text.await_args.args[0]


async def test_empty_actions_go_to_the_parser(settings, db, llm):
    llm.answers.append({"actions": [], "summary": ""})
    msg = await run("на разгибания 4 подхода по 10–12", settings, db, llm)
    assert llm.edit_calls() == 1 and llm.parser_calls() == 1
    assert hce.EDITS == {} and msg.answer.await_count == 1
    assert "Что изменю" not in shown(msg)


async def test_weekday_delete_rejected_by_the_edit_model_reaches_saved_edits(settings, db, llm, monkeypatch):
    """«в понедельник» routes to the edit model, but a saved-record delete it rejects still gets saved_edits."""
    handle = AsyncMock(return_value=True)
    monkeypatch.setattr(log_text.saved_edits, "handle", handle)
    llm.answers.append({"actions": [], "summary": ""})
    await run("удали последний подход в понедельник", settings, db, llm)
    assert llm.edit_calls() == 1 and handle.await_count == 1


@pytest.mark.parametrize("text", [
    "сделал жим 80×8", "что сегодня?", "удали последнюю запись", "запиши самочувствие",
    "на этой неделе делоад, спал плохо", "разгрузочная неделя, болит колено",
])
async def test_non_commands_never_reach_the_edit_model(text, settings, db, llm):
    await run(text, settings, db, llm)
    assert llm.edit_calls() == 0 and hce.EDITS == {}


async def test_llm_error_falls_through_to_the_parser(settings, db, llm):
    llm.answers.extend(["oops", "oops"])  # the client retries once on output without JSON
    msg = await run("на разгибания 4 подхода по 10–12", settings, db, llm)
    assert llm.edit_calls() >= 1 and llm.parser_calls() == 1
    assert hce.EDITS == {} and "Что изменю" not in shown(msg)


async def test_day_clarify_button_builds_the_preview_without_the_model(settings, db, llm):
    llm.answers.append(add_pullups())
    msg = await run("добавь подтягивания в день рук", settings, db, llm)
    assert shown(msg).startswith("Какой день")
    token = token_of(msg)
    assert buttons(msg)[:2] == [f"eclar:{token}:0", f"eclar:{token}:1"] and buttons(msg)[2] == f"edrop:{token}"
    labels = [b.text for row in msg.answer.await_args.kwargs["reply_markup"].inline_keyboard for b in row]
    assert labels[0].startswith("пн") and labels[1].startswith("пт")

    cb = callback(f"eclar:{token}:1")
    await hce.clarify(cb, settings, db, llm.client)
    assert llm.edit_calls() == 1
    body = cb.message.edit_text.await_args.args[0]
    assert "Что изменю" in body and "пт «Руки и плечи»: добавить подтягивания" in body
    kb = cb.message.edit_text.await_args.kwargs["reply_markup"]
    assert [b.callback_data for row in kb.inline_keyboard for b in row] == [f"eapply:{token}", f"edrop:{token}"]

    await hce.apply_edit(callback(f"eapply:{token}"), settings, db)
    snap = await active_snapshot(db)
    assert names(snap)[-1] == "подтягивания" and "подтягивания" not in names(snap, weekday=1)


async def test_clarify_double_tap_and_failure(settings, db, llm, monkeypatch):
    llm.answers.append(add_pullups())
    msg = await run("добавь подтягивания в день рук", settings, db, llm)
    token = token_of(msg)
    first = callback(f"eclar:{token}:1")

    async def tap_again_meanwhile(pending, today, sessionmaker):
        second = callback(f"eclar:{token}:0")
        await hce.clarify(second, settings, db, llm.client)  # while the first one works
        assert second.answer.await_args.kwargs.get("show_alert") is True
        raise RuntimeError("db down")

    monkeypatch.setattr(hce, "_build", tap_again_meanwhile)
    await hce.clarify(first, settings, db, llm.client)
    assert first.answer.await_count == 1  # the button never keeps spinning
    assert first.message.edit_text.await_args.args[0] == "Не получилось, повтори команду."
    assert hce.EDITS == {}


async def test_models_own_clarify_asks_again_with_the_answer(settings, db, llm):
    llm.answers.append({"actions": [{
        "type": "clarify", "question": "Какие сгибания заменить?", "options": ["с супинацией", "с пронацией"],
    }], "summary": ""})
    msg = await run("замени сгибания в понедельник на молотки", settings, db, llm)
    assert shown(msg) == "Какие сгибания заменить?"
    token = token_of(msg)
    assert buttons(msg)[:2] == [f"eclar:{token}:0", f"eclar:{token}:1"]

    llm.answers.append({"actions": [{
        "type": "replace", "day": {"weekday": 1, "focus": None, "when": None},
        "exercise": "сгибания с гантелями на бицепс с пронацией", "new_name": "молотки", "scope": "this_week",
    }], "summary": ""})
    cb = callback(f"eclar:{token}:1")
    await hce.clarify(cb, settings, db, llm.client)
    assert llm.edit_calls() == 2
    assert "Уточнение: с пронацией" in llm.edit_bodies()[1]["messages"][-1]["content"]
    body = cb.message.edit_text.await_args.args[0]
    assert "Что изменю" in body and "→ молотки" in body
    cb.answer.assert_awaited()


async def test_program_changed_between_preview_and_apply(settings, db, llm, published):
    llm.answers.append(remove_french())
    msg = await run("убери французский жим из дня рук", settings, db, llm)
    token = token_of(msg)
    # "The Mini App" edits the same program in between (it forks the template into the active copy).
    async with db() as s:
        user = await get_or_create_user(s, USER)
        snap = await ce.load_snapshot(s, user, TODAY)
        up = await active_program(s, user, TODAY)
        other = next(i for i in snap.day(1, 5).items if i.name == "жим сидя в смите")
        op = {"op": "remove", "week": 1, "weekday": 5, "weeks": [1], "itemId": other.id}
        await pe.edit_program(s, user, up, snap.slug, snap.version, [op], today=TODAY)
        await s.commit()
    cb = callback(f"eapply:{token}")
    await hce.apply_edit(cb, settings, db)
    assert cb.message.edit_text.await_args.args[0] == hce.CHANGED
    snap = await active_snapshot(db)
    assert FRENCH in names(snap) and "жим сидя в смите" not in names(snap)  # only the other edit is there
    assert await owned_programs(db) == 1 and published == []


async def test_foreign_user_cannot_apply_or_cancel(settings, db, llm, published):
    llm.answers.append(remove_french())
    msg = await run("убери французский жим из дня рук", settings, db, llm)
    token = token_of(msg)
    for cb in (callback(f"eapply:{token}", user_id=777), callback(f"edrop:{token}", user_id=777)):
        if cb.data.startswith("eapply"):
            await hce.apply_edit(cb, settings, db)
        else:
            await hce.drop_edit(cb)
        cb.answer.assert_awaited_with(hce.STALE, show_alert=True)
        cb.message.edit_text.assert_not_awaited()
    foreign = callback(f"eclar:{token}:0", user_id=777)
    await hce.clarify(foreign, settings, db, llm.client)
    foreign.answer.assert_awaited_with(hce.STALE, show_alert=True)
    assert token in hce.EDITS and published == [] and await owned_programs(db) == 0

    cb = callback(f"eapply:{token}")
    await hce.apply_edit(cb, settings, db)
    assert "Готово" in cb.message.edit_text.await_args.args[0]
    assert FRENCH not in names(await active_snapshot(db))


async def test_cancel_then_apply_is_stale(settings, db, llm, published):
    llm.answers.append(remove_french())
    msg = await run("убери французский жим из дня рук", settings, db, llm)
    token = token_of(msg)
    drop = callback(f"edrop:{token}")
    await hce.drop_edit(drop)
    assert drop.message.edit_text.await_args.args[0] == "Отменено."
    late = callback(f"eapply:{token}")
    await hce.apply_edit(late, settings, db)
    late.answer.assert_awaited_with(hce.STALE, show_alert=True)
    assert published == [] and await owned_programs(db) == 0


async def test_old_preview_expires(settings, db, llm, monkeypatch):
    llm.answers.append(remove_french())
    msg = await run("убери французский жим из дня рук", settings, db, llm)
    token = token_of(msg)
    monkeypatch.setattr(hce, "utcnow", lambda: T0 + hce.TTL + hce.timedelta(minutes=1))
    cb = callback(f"eapply:{token}")
    await hce.apply_edit(cb, settings, db)
    cb.answer.assert_awaited_with(hce.STALE, show_alert=True)
    assert await owned_programs(db) == 0


async def test_invalid_action_note_is_shown_with_the_valid_preview(settings, db, llm):
    data = remove_french()
    data["actions"].append({"type": "weight", "said": "жим", "exercise": "жим лёжа", "weight_kg": 0, "date": None})
    llm.answers.append(data)
    msg = await run("убери французский жим из дня рук", settings, db, llm)
    assert "Что изменю" in shown(msg) and "Не применю — вес" in shown(msg)


async def test_exercise_not_in_day_is_a_note_without_buttons(settings, db, llm):
    """Nothing to apply and not a saved-record edit either: the notes answer the command."""
    data = {"actions": [{"type": "prescribe", "day": {"weekday": 5, "focus": None, "when": None},
                         "exercise": "становая тяга", "sets": 4, "repsMin": 8, "repsMax": None, "dropReps": None,
                         "intensity": None, "scope": None}], "summary": ""}
    llm.answers.append(data)
    msg = await run("на становую 4 подхода по 8", settings, db, llm)
    assert "нет «становая тяга»" in shown(msg) and msg.answer.await_args.kwargs.get("reply_markup") is None
    assert hce.EDITS == {} and llm.parser_calls() == 0


async def test_notes_only_plan_gives_way_to_saved_edits(settings, db, llm, monkeypatch):
    """«убери становую тягу из пятничной тренировки»: no such exercise on Friday, but it reads as a saved-record
    delete, so saved_edits gets the text instead of a note that would hide it."""
    handle = AsyncMock(return_value=True)
    monkeypatch.setattr(log_text.saved_edits, "handle", handle)
    data = remove_french({"weekday": 5, "focus": None, "when": None})
    data["actions"][0]["exercise"] = "становая тяга"
    llm.answers.append(data)
    msg = await run("убери становую тягу из пятничной тренировки", settings, db, llm)
    assert handle.await_count == 1 and hce.EDITS == {} and msg.answer.await_count == 0


def deload_next_week():
    return {"actions": [{"type": "deload", "week": "next_week", "start": None}], "summary": ""}


async def deload_row(db):
    async with db() as s:
        return await s.scalar(select(DeloadState))


async def test_deload_preview_apply_publishes_the_plan(settings, db, llm, published):
    llm.answers.append(deload_next_week())
    msg = await run("следующая неделя — делоад", settings, db, llm)
    assert llm.edit_calls() == 1 and llm.parser_calls() == 0
    assert "Что изменю" in shown(msg) and "Разгрузочная неделя с пн 12.10 по вс 18.10" in shown(msg)
    assert "Создам" not in shown(msg)
    assert await deload_row(db) is None  # the preview wrote nothing

    cb = callback(f"eapply:{token_of(msg)}")
    await hce.apply_edit(cb, settings, db)
    st = await deload_row(db)
    assert (st.started_on, st.until) == (date(2026, 10, 12), date(2026, 10, 18))
    assert len(published) == 1 and published[0][1:] == ("plan",)
    assert "Готово" in cb.message.edit_text.await_args.args[0]
    assert await owned_programs(db) == 0

    again = callback(f"eapply:{token_of(msg)}")
    await hce.apply_edit(again, settings, db)
    again.answer.assert_awaited_with(hce.STALE, show_alert=True)
    assert len(published) == 1


async def test_swap_preview_has_both_dates_and_apply_publishes_everything(settings, db, llm, published):
    llm.answers.append({"actions": [{
        "type": "swap_days", "a": {"weekday": None, "focus": None, "when": "today"},
        "b": {"weekday": None, "focus": "база", "when": None}, "scope": None,
    }], "summary": ""})
    msg = await run("сделай сегодня базу вместо рук", settings, db, llm)
    assert "пт 09.10" in shown(msg) and "ср 07.10" in shown(msg) and "Создам твою копию" in shown(msg)
    assert await owned_programs(db) == 0

    cb = callback(f"eapply:{token_of(msg)}")
    await hce.apply_edit(cb, settings, db)
    snap = await active_snapshot(db)
    assert snap.day(1, 5).focus == "База" and snap.day(1, 3).focus == "Руки и плечи"
    assert snap.day(2, 5).focus == "Руки и плечи"
    assert len(published) == 1 and published[0][1:] == ("program", "plan", "state")
    assert await owned_programs(db) == 1


async def test_swap_plus_deload_in_one_confirmation(settings, db, llm, published):
    llm.answers.append({"actions": [
        {"type": "move_day", "src": {"when": "today"}, "dst": {"when": "tomorrow"}},
        {"type": "deload", "week": "next_week", "start": None},
    ], "summary": ""})
    msg = await run("перенеси тренировку на завтра и со следующей недели разгрузка", settings, db, llm)
    assert "пт 09.10 → сб 10.10" in shown(msg) and "Разгрузочная неделя с пн 12.10" in shown(msg)
    await hce.apply_edit(callback(f"eapply:{token_of(msg)}"), settings, db)
    assert (await deload_row(db)).started_on == date(2026, 10, 12)
    assert (await active_snapshot(db)).day(1, 6).focus == "Руки и плечи"
    assert published[0][1:] == ("program", "plan", "state")
