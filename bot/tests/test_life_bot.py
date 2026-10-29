"""«Дела» from the chat (gymbot.handlers.life): the quick words, the «Дела» model and its preview, «Применить»
and «Устарело», notes the bot writes and their rewrite by words, kind="life" from the parser, «ДЕЛА: …» under
the diary answer and the «Дела» block of the answer (gymbot.services.answer, gymbot.services.life_brief).
Also the dashboard's Telegram initData on /api/hub/state (moved from the removed tests/test_hub_tasks.py)."""

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest
from conftest import init_data, make_settings
from sqlalchemy import select
from test_log_text import T0, USER, FakeLLM, callback, food, message, token_of

from gymbot.api.app import create_app
from gymbot.db.models import LifeDeadline, LifeEvent, LifeNote, LifeSlot
from gymbot.handlers import life as hl
from gymbot.handlers import log_text
from gymbot.llm.openrouter import LLMError
from gymbot.llm.prompts_life import LIFE_SYSTEM_PROMPT
from gymbot.services import answer as qa
from gymbot.services import live
from gymbot.services.life_gate import is_life_command
from gymbot.services.users import get_or_create_user

TODAY = date(2026, 10, 6)  # T0 in Europe/Moscow, a Tuesday
HTTP = "Позаниматься HTTP-сервером на C"


def add_event(day: str = "2026-10-06", start: str | None = "11:00", title: str = HTTP, area: str = "study") -> dict:
    fields = {"day": day, "title": title, "area": area} | ({"start": start} if start else {})
    return {"op": "add", "entity": "event", "fields": fields}


def life_answer(*actions: dict, question: dict | None = None) -> dict:
    return {"actions": list(actions), "question": question}


@pytest.fixture
def life_settings(tmp_path):
    return make_settings(tmp_path, public_url="https://gym.example")


@pytest.fixture
def settings(life_settings):  # the db fixture (conftest) builds its database from `settings`
    return life_settings


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr(hl, "utcnow", lambda: T0)


@pytest.fixture(autouse=True)
def clean():
    stores = (hl.PENDING, log_text.PENDING, log_text.CONTEXT, log_text.FACTS, log_text.QA)
    for store in stores:
        store.clear()
    yield
    for store in stores:
        store.clear()


@pytest.fixture
def published(monkeypatch):
    calls: list[tuple] = []
    real = live.publish

    def spy(*a):
        calls.append(a)
        real(*a)

    monkeypatch.setattr(live, "publish", spy)
    return calls


@pytest.fixture
def no_diary(monkeypatch):
    async def parser_answer(message, text, result, *args):
        return result, None, None

    monkeypatch.setattr(log_text, "_diary_answer", parser_answer)


async def owner(db, tg_id: int = USER) -> int:
    async with db() as s:
        user = await get_or_create_user(s, tg_id, "Amir")
        await s.commit()
        return user.id


async def put(db, *rows) -> None:
    async with db() as s:
        s.add_all(rows)
        await s.commit()


async def rows(db, model) -> list:
    async with db() as s:
        return list(await s.scalars(select(model).order_by(model.id)))


async def say(text, llm, settings, db, *, at=T0, **kw):
    msg = message(text, at)
    await log_text.process_text(msg, text, settings, db, llm.client, **kw)
    return msg


def shown(msg) -> str:
    return msg.answer.await_args.args[0]


def buttons(msg) -> list[tuple[str, str]]:
    kb = msg.answer.await_args.kwargs["reply_markup"]
    return [(b.text, b.callback_data) for row in kb.inline_keyboard for b in row]


def is_life_request(body: dict) -> bool:
    return body["messages"][0]["content"] == LIFE_SYSTEM_PROMPT


# ---- the quick words ----


# Diary records and program edits with words that once asked the «Дела» model first (review of 2026-10-29).
DIARY_WITH_LIFE_LOOKS = (
    "на завтрак пара яиц и тост", "пара бананов", "после пары сходил в зал, жим 80 на 8", "между парами съел шаурму",
    "урок бокса 60 минут", "запланируй тренировку ног на завтра", "хочу позаниматься в зале вечером",
    "подними жим на пару кг", "перенеси тренировку на пару дней",
)


@pytest.mark.parametrize("text", [
    "В учёбу добавь сегодня в 11 позаниматься HTTP-сервером на C", "добавь задачу купить билеты",
    "в дела: позвонить маме", "закрой задачу про резюме", "вынес мусор", "сделал уборку", "убрался дома",
    "постирал вчера", "закупился продуктами", "оплатил коммуналку", "туду: код", "дедлайн по лабе 10 ноября",
    "экзамен по матанализу в пятницу", "зачёт перенесли", "надо сдать лабу", "запиши заметку: идея",
    "составь памятку по epoll", "конспект про сокеты", "перенеси урок с понедельника на вторник",
    "пара по вышмату отменилась", "поменяй расписание", "запланируй созвон",
    "добавь в планы поход", "дела по дому", "проект AMGym на паузу", "пара в 10 отменилась",
    "пары в понедельник перенесли", "урок в 15", "урок английского в субботу", "хочу позаниматься английским",
])
def test_life_words(text):
    assert is_life_command(text)


@pytest.mark.parametrize("text", [
    "купил протеин", "позанимался в зале", "съел гречку 200 г", "убрал жим из программы", "оплатил абонемент",
    "сделал жим 3×8 по 60", "убери французский жим из дня рук", "продукты: курица 200 г", "мои продукты",
    "спал 6 часов", "пару подходов жима сделал", "съел пару яиц", "позанимался в зале, жим 3×8",
    *DIARY_WITH_LIFE_LOOKS,
    # pairs only with a time, a day or a subject: «после пары зайти в деканат» reaches «Дела» by the parser's
    # kind="life" (test_parser_kind_life_goes_to_the_life_model)
    "после пары зайти в деканат",
])
def test_not_life_words(text):
    assert not is_life_command(text)


@pytest.mark.parametrize("text", [
    *(t for t in DIARY_WITH_LIFE_LOOKS if t != "урок бокса 60 минут"),  # that one has no «Дела» look any more
    "купил продукты на ужин: курица 300 г", "сдал зачёт, съел пиццу 3 куска",
    "зачёт завтра, спал 5 часов", "после экзамена выпил кофе", "задача: жим 80 на 8", "уборка, вес 84.6",
    "перенеси пятничную тренировку на субботу, у меня экзамен", "убрался, на ужин курица с рисом",
])
def test_diary_records_never_ask_the_life_model_first(text):
    """Even when the quick words match, a message that may be a record goes to the parser first."""
    assert log_text._diary_like(text), text


@pytest.mark.parametrize("text", [
    "В учёбу добавь сегодня в 11 позаниматься HTTP-сервером на C", "добавь задачу купить билеты", "вынес мусор",
    "сделал уборку", "дедлайн по лабе 10 ноября", "перенеси урок ИЗ Математика с понедельника на вторник",
    "запланируй завтра в 15 созвон", "составь памятку по epoll", "добавь в планы поход",
    "добавь задачу купить молоко и хлеб", "в дела: купить курицу и рис", "закрой задачу про риски",
])
def test_life_commands_are_not_diary_records(text):
    assert is_life_command(text) and not log_text._diary_like(text), text


# ---- /life ----


async def test_life_command_opens_the_mini_app_on_life(life_settings, tmp_path):
    msg = message("/life")
    await hl.cmd_life(msg, life_settings)
    kb = msg.answer.await_args.kwargs["reply_markup"]
    assert kb.inline_keyboard[0][0].text == "Открыть дела"
    assert kb.inline_keyboard[0][0].web_app.url == "https://gym.example?section=life"
    with_query = make_settings(tmp_path, miniapp_url="https://app.example/?v=2")
    assert hl.life_url(with_query) == "https://app.example/?v=2&section=life"
    msg = message("/hub")
    await hl.cmd_life(msg, make_settings(tmp_path / "x"))
    assert shown(msg) == hl.NOT_SET


# ---- the model and the preview ----


async def test_study_plan_preview_apply_and_stale(life_settings, db, published):
    uid = await owner(db)
    llm = FakeLLM(life_settings)
    llm.answers.append(life_answer(add_event()))
    msg = await say("В учёбу добавь сегодня в 11 позаниматься HTTP-сервером на C", llm, life_settings, db,
                    raw_text="[voice] …", prefix="Распознал\n\n")
    assert len(llm.bodies) == 1 and is_life_request(llm.bodies[0])  # only the «Дела» model, not the parser
    assert "Сегодня: 2026-10-06, вторник" in llm.bodies[0]["messages"][1]["content"]
    assert shown(msg) == f"Распознал\n\nВ учёбу: «{HTTP}» — сегодня в 11:00"
    assert [t for t, _ in buttons(msg)] == ["✅ Применить", "Отмена"]
    assert await rows(db, LifeEvent) == []  # nothing before the button

    token = token_of(msg)
    cb = callback(f"lapply:{token}")
    await hl.on_apply(cb, life_settings, db)
    [event] = await rows(db, LifeEvent)
    assert (event.day, event.start, event.title, event.area, event.user_id) == (TODAY, "11:00", HTTP, "study", uid)
    assert (uid, "life") in published
    edited = cb.message.edit_text.await_args
    assert edited.args[0] == f"Готово:\n• Добавил план «{HTTP}»"
    assert edited.kwargs["reply_markup"].inline_keyboard[0][0].web_app.url == "https://gym.example?section=life"

    again = callback(f"lapply:{token}")  # a second tap adds nothing
    await hl.on_apply(again, life_settings, db)
    assert again.answer.await_args.args[0] == hl.STALE
    assert len(await rows(db, LifeEvent)) == 1


async def test_apply_failure_answers_and_never_writes_twice(life_settings, db, monkeypatch):
    """A database error in «Применить» (IntegrityError and the like): the tap is answered, nothing is written and
    the preview is not put back, so a second tap cannot write it twice."""
    await owner(db)
    llm = FakeLLM(life_settings)
    llm.answers.append(life_answer(add_event()))
    msg = await say("в учёбу добавь сегодня в 11 позаниматься", llm, life_settings, db)
    token = token_of(msg)

    async def broken(*args, **kwargs):
        raise RuntimeError("db is gone")

    monkeypatch.setattr(hl.life, "apply", broken)
    cb = callback(f"lapply:{token}")
    await hl.on_apply(cb, life_settings, db)
    assert cb.answer.await_args.args[0] == hl.SAVE_FAILED == "Не получилось сохранить, попробуй ещё раз"
    cb.message.edit_text.assert_not_awaited()
    assert token not in hl.PENDING and await rows(db, LifeEvent) == []
    again = callback(f"lapply:{token}")
    await hl.on_apply(again, life_settings, db)
    assert again.answer.await_args.args[0] == hl.STALE


async def test_restart_or_ttl_is_stale(life_settings, db, monkeypatch):
    await owner(db)
    llm = FakeLLM(life_settings)
    llm.answers.append(life_answer(add_event()))
    msg = await say("в учёбу добавь сегодня в 11 позаниматься", llm, life_settings, db)
    token = token_of(msg)
    monkeypatch.setattr(hl, "utcnow", lambda: T0 + hl.TTL + timedelta(seconds=1))
    cb = callback(f"lapply:{token}")
    await hl.on_apply(cb, life_settings, db)
    assert cb.answer.await_args.args[0] == hl.STALE
    hl.PENDING.clear()  # a restart
    cb = callback(f"lapply:{token}")
    await hl.on_apply(cb, life_settings, db)
    assert cb.answer.await_args.args[0] == hl.STALE
    assert await rows(db, LifeEvent) == []


async def test_slot_moved_to_another_weekday(life_settings, db):
    uid = await owner(db)
    await put(db, LifeSlot(user_id=uid, weekday=1, start="10:00", title="ИЗ Математика", area="work"))
    [slot] = await rows(db, LifeSlot)
    llm = FakeLLM(life_settings)
    llm.answers.append(life_answer({"op": "update", "entity": "slot", "id": slot.id, "fields": {"weekday": 2}}))
    msg = await say("перенеси урок ИЗ Математика с понедельника на вторник", llm, life_settings, db)
    assert f"#{slot.id} пн 10:00 ИЗ Математика" in llm.bodies[0]["messages"][1]["content"]
    assert shown(msg) == "Перенесу «ИЗ Математика» с пн на вт"
    await hl.on_apply(callback(f"lapply:{token_of(msg)}"), life_settings, db)
    [slot] = await rows(db, LifeSlot)
    assert slot.weekday == 2


async def test_far_date_is_dropped_with_a_note(life_settings, db):
    await owner(db)
    llm = FakeLLM(life_settings)
    llm.answers.append(life_answer(add_event(day="2099-01-01"), {"op": "add", "entity": "task",
                                                                 "fields": {"title": "Купить билеты"}}))
    msg = await say("в планы на 2099 год и задачу купить билеты", llm, life_settings, db)
    text = shown(msg)
    assert text.startswith("Добавлю задачу «Купить билеты»")
    assert "Дата 2099-01-01 дальше 400 дней от сегодня." in text


async def test_tomorrow_after_midnight_in_tashkent(tmp_path, db):
    """Review Focus 2: 19:30 UTC is 00:30 of the next day in Tashkent; the model gets that date."""
    settings = make_settings(tmp_path, timezone="Asia/Tashkent")
    await owner(db)
    llm = FakeLLM(settings)
    llm.answers.append(life_answer(add_event(day="2026-10-08", start="15:00", title="Созвон", area="work")))
    at = datetime(2026, 10, 6, 19, 30, tzinfo=UTC)
    msg = await say("запланируй завтра в 15 созвон", llm, settings, db, at=at)
    assert "Сегодня: 2026-10-07, среда" in llm.bodies[0]["messages"][1]["content"]
    assert shown(msg) == "В работу: «Созвон» — завтра в 15:00"


async def test_no_action_goes_on_to_the_parser(life_settings, db, no_diary):
    """Review Focus 1: a message with quick words is the parser's when the «Дела» model finds nothing. (Food
    with them, «купил продукты на ужин: курица 300 г», goes to the parser first: test_a_record_with_life_words…)"""
    await owner(db)
    llm = FakeLLM(life_settings)
    llm.answers += [life_answer(), {"kind": "question", "clarification": "Отдыхай."}]
    msg = await say("дела по дому подождут", llm, life_settings, db)
    assert len(llm.bodies) == 2 and is_life_request(llm.bodies[0]) and not is_life_request(llm.bodies[1])
    assert not hl.PENDING
    assert shown(msg) == "Отдыхай."


@pytest.mark.parametrize("text", ["купил продукты на ужин: курица 300 г", "между парами съел шаурму",
                                  "после пары сходил в зал, жим 80 на 8", "пара бананов", "запланируй тренировку ног на завтра"])
async def test_a_record_with_life_words_goes_to_the_parser_first(text, life_settings, db, no_diary):
    await owner(db)
    llm = FakeLLM(life_settings)
    llm.answers += [food(1, "курица")] * 3  # the parser (and its plausibility round): the «Дела» model never asked
    await say(text, llm, life_settings, db)
    assert llm.bodies and not any(is_life_request(b) for b in llm.bodies)


async def test_gym_words_never_ask_the_life_model(life_settings, db, no_diary):
    await owner(db)
    llm = FakeLLM(life_settings)
    llm.answers.append({"kind": "workout", "exercises": [
        {"exercise": "жим лёжа", "sets": [{"reps": 8, "weight_kg": 60}] * 3}]})
    await say("позанимался в зале, жим 3×8 по 60", llm, life_settings, db)
    assert len(llm.bodies) == 1 and not is_life_request(llm.bodies[0])
    assert len(log_text.PENDING) == 1


async def test_only_the_owner(life_settings, db):
    await owner(db, tg_id=1)  # someone else is the owner
    llm = FakeLLM(life_settings)
    msg = message("добавь задачу купить билеты")
    assert await hl.stage(msg, msg.text, msg.text, life_settings, db, llm.client) == (None, hl.NOT_OWNER)
    assert llm.bodies == []


async def test_model_failure_goes_on(life_settings, db):
    await owner(db)
    llm = FakeLLM(life_settings)
    llm.answers.append("not json at all")
    msg = message("добавь задачу купить билеты")
    assert await hl.stage(msg, msg.text, msg.text, life_settings, db, llm.client) == (None, hl.FAILED)


async def test_question_with_options_then_preview(life_settings, db):
    await owner(db)
    llm = FakeLLM(life_settings)
    llm.answers += [
        life_answer(question={"text": "На какой день?", "options": ["Сегодня", "Завтра"]}),
        life_answer(add_event(day="2026-10-07", start=None, title="Созвон", area="work")),
    ]
    msg = await say("запланируй созвон", llm, life_settings, db)
    assert shown(msg) == "На какой день?"
    assert [t for t, _ in buttons(msg)] == ["Сегодня", "Завтра", "Отмена"]
    data = buttons(msg)[1][1]
    cb = callback(data)
    await hl.on_option(cb, life_settings, db, llm.client)
    assert llm.bodies[1]["messages"][1]["content"].endswith("Сообщение: запланируй созвон\nУточнение: Завтра")
    edited = cb.message.edit_text.await_args
    assert edited.args[0] == "В работу: «Созвон» — завтра"
    apply = edited.kwargs["reply_markup"].inline_keyboard[0][0].callback_data
    await hl.on_apply(callback(apply), life_settings, db)
    assert [(e.day, e.title) for e in await rows(db, LifeEvent)] == [(date(2026, 10, 7), "Созвон")]
    stale = callback(data)  # the question's buttons are gone after the first tap
    await hl.on_option(stale, life_settings, db, llm.client)
    assert stale.answer.await_args.args[0] == hl.STALE


async def test_drop(life_settings, db):
    await owner(db)
    llm = FakeLLM(life_settings)
    llm.answers.append(life_answer({"op": "add", "entity": "task", "fields": {"title": "X"}}))
    msg = await say("добавь задачу x", llm, life_settings, db)
    token = token_of(msg)
    await hl.on_drop(callback(f"ldrop:{token}"))
    assert token not in hl.PENDING


# ---- notes ----

EPOLL = "# epoll\n\nМультиплексирование ввода-вывода в Linux.\n\n" + "- пункт\n" * 120
EPOLL_EXAMPLE = "# epoll\n\nС примером:\n\n```c\nint fd = epoll_create1(0);\n```\n"


async def test_memo_is_written_rewritten_by_words_and_saved(life_settings, db):
    uid = await owner(db)
    llm = FakeLLM(life_settings)
    llm.answers += [
        life_answer({"op": "write_note", "fields": {"title": "Памятка по epoll", "brief": "epoll: как работает"}}),
        EPOLL,
    ]
    msg = await say("составь памятку по epoll", llm, life_settings, db)
    assert len(llm.bodies) == 2
    writer = llm.bodies[1]["messages"]
    assert writer[1]["content"] == "Напиши заметку: epoll: как работает"
    text = shown(msg)
    assert text.startswith("Памятка: «Памятка по epoll»\n\n# epoll\n\nМультиплексирование")
    assert "…" in text and len(text) < 900 and hl.DRAFT_HINT in text
    assert [t for t, _ in buttons(msg)] == ["Сохранить", "Отмена"]
    first = token_of(msg)

    llm.answers.append(EPOLL_EXAMPLE)
    again = await say("добавь пример", llm, life_settings, db, at=T0 + timedelta(minutes=1))
    assert len(llm.bodies) == 3
    rewrite = llm.bodies[2]["messages"][1]["content"]
    assert "Текущий текст:\n# epoll\n\nМультиплексирование" in rewrite and rewrite.endswith("добавь пример")
    assert "epoll_create1" in shown(again)
    assert first not in hl.PENDING  # the older preview's «Сохранить» is gone

    await hl.on_apply(callback(f"lapply:{token_of(again)}"), life_settings, db)
    [note] = await rows(db, LifeNote)
    assert (note.title, note.body, note.author, note.user_id) == ("Памятка по epoll", EPOLL_EXAMPLE.strip(), "bot", uid)


async def test_a_record_while_a_draft_is_open_is_a_record(life_settings, db, no_diary):
    await owner(db)
    llm = FakeLLM(life_settings)
    llm.answers += [
        life_answer({"op": "write_note", "fields": {"title": "Памятка по epoll", "brief": "epoll"}}), EPOLL,
        food(1, "гречка"),
    ]
    await say("составь памятку по epoll", llm, life_settings, db)
    await say("съел гречку 200 г", llm, life_settings, db, at=T0 + timedelta(minutes=1))
    assert len(llm.bodies) == 3 and "гречку" in llm.bodies[2]["messages"][-1]["content"]
    assert len(log_text.PENDING) == 1 and len(hl.PENDING) == 1  # the draft is still there


async def test_note_said_word_for_word(life_settings, db):
    await owner(db)
    llm = FakeLLM(life_settings)
    said = "идея — статус прода рядом с задачами"
    llm.answers.append(life_answer({"op": "add", "entity": "note", "fields": {"title": "Идея", "body": said}}))
    msg = await say(f"запиши заметку: {said}", llm, life_settings, db)
    assert len(llm.bodies) == 1  # no writer call
    assert shown(msg) == "Сохраню заметку «Идея»"
    await hl.on_apply(callback(f"lapply:{token_of(msg)}"), life_settings, db)
    [note] = await rows(db, LifeNote)
    assert (note.body, note.author) == (said, "me")


async def test_saved_note_rewritten(life_settings, db):
    uid = await owner(db)
    await put(db, LifeNote(user_id=uid, title="Сокеты", body="# Сокеты\n\nДлинно и подробно.", tags=[]))
    [note] = await rows(db, LifeNote)
    llm = FakeLLM(life_settings)
    llm.answers += [
        life_answer({"op": "edit_note", "id": note.id, "fields": {"instruction": "сделай короче"}}),
        "# Сокеты\n\nКоротко.",
    ]
    msg = await say("заметку про сокеты сделай короче", llm, life_settings, db)
    rewrite = llm.bodies[1]["messages"][1]["content"]
    assert "Длинно и подробно." in rewrite and rewrite.endswith("сделай короче")
    assert shown(msg).startswith("Заметка «Сокеты» после правки:\n\n# Сокеты\n\nКоротко.")
    await hl.on_apply(callback(f"lapply:{token_of(msg)}"), life_settings, db)
    [note] = await rows(db, LifeNote)
    assert note.body == "# Сокеты\n\nКоротко." and note.author == "me"


def test_rewrite_instructions():
    # «убери про select» alone reads as a program edit too («убери жим»): the gym path wins, the note needs naming.
    for text in ("добавь пример", "короче", "а теперь убери из памятки про select", "сделай подробнее",
                 "добавь в памятку таблицу",
                 "замени в памятке пример на питон", "перепиши проще", "убери из памятки про poll"):
        assert hl.is_rewrite(text, TODAY), text
    for text in ("съел гречку 200 г", "добавь задачу купить билеты", "вынес мусор", "жим 3 подхода по 8", "привет",
                 "сделай сегодня день рук", "убери французский жим из дня рук", "замени жим на гантели",
                 "удали самсу", "исправь: самса была 2", "норма 2800 ккал", *RECORDS_NOT_REWRITES,
                 "короче, съел шаурму", "добавь задачу дописать конспект", "переведи тренировку на завтра"):
        assert not hl.is_rewrite(text, TODAY), text
    for text in ("сделай списком", "переведи на английский", "перефразируй", "другой заголовок", "упрости",
                 "покороче, пожалуйста", "в памятку добавь про edge-triggered", "добавь примеры кода"):
        assert hl.is_rewrite(text, TODAY), text


# Everyday words of a gym diary: with a draft open they still go to the parser, never to the note writer.
RECORDS_NOT_REWRITES = (
    "добавь банан", "добавь 2 яйца к завтраку", "замени курицу на индейку", "поменяй рис на гречку",
    "оставь только завтрак", "больше не болит колено", "меньше спал сегодня, 5 часов", "без сахара кофе выпил",
)


def open_draft(token: str = "draft", preview_id: int | None = 555) -> None:
    hl.PENDING[token] = hl.LifePending(
        USER, "составь памятку по epoll", "составь памятку по epoll", [], [], [], T0,
        note_draft=hl.NoteDraft("Памятка по epoll", "epoll", EPOLL), preview_id=preview_id,
    )


@pytest.fixture
def writer(monkeypatch) -> list[str]:
    calls: list[str] = []

    async def write(llm, brief, draft, instruction):
        calls.append(instruction)
        return EPOLL_EXAMPLE

    monkeypatch.setattr(hl, "_write", write)
    return calls


@pytest.mark.parametrize("text", RECORDS_NOT_REWRITES)
async def test_record_words_with_a_draft_open_go_their_way(text, life_settings, db, no_diary, writer):
    await owner(db)
    open_draft()
    llm = FakeLLM(life_settings)
    llm.answers += [food(1, "банан")] * 4  # whatever path takes it (parser, saved edits) gets an answer
    await say(text, llm, life_settings, db, at=T0 + timedelta(minutes=1))
    assert writer == [] and "draft" in hl.PENDING  # the draft is untouched
    assert llm.bodies and not any(is_life_request(b) for b in llm.bodies)


async def test_a_reply_to_the_draft_rewrites_it(life_settings, db, writer):
    await owner(db)
    open_draft()
    llm = FakeLLM(life_settings)
    msg = message("добавь банан в конце", T0 + timedelta(minutes=1))
    msg.reply_to_message = SimpleNamespace(message_id=555)
    msg.answer.return_value = SimpleNamespace(message_id=777)
    await log_text.process_text(msg, msg.text, life_settings, db, llm.client)
    assert writer == ["добавь банан в конце"] and llm.bodies == []
    assert "epoll_create1" in shown(msg) and "draft" not in hl.PENDING
    [(_, pending)] = hl.PENDING.items()
    assert pending.preview_id == 777  # the next reply goes to the new preview


async def test_a_reply_to_another_message_is_not_a_rewrite(life_settings, db, no_diary, writer):
    await owner(db)
    open_draft()
    llm = FakeLLM(life_settings)
    llm.answers += [food(1, "банан")] * 4  # the parser and its plausibility repair round
    msg = message("добавь банан", T0 + timedelta(minutes=1))
    msg.reply_to_message = SimpleNamespace(message_id=1)
    await log_text.process_text(msg, msg.text, life_settings, db, llm.client)
    assert writer == [] and "draft" in hl.PENDING and len(log_text.PENDING) == 1


async def test_a_program_edit_while_a_draft_is_open_is_a_program_edit(life_settings, db, monkeypatch):
    await owner(db)
    llm = FakeLLM(life_settings)
    llm.answers += [
        life_answer({"op": "write_note", "fields": {"title": "Памятка по epoll", "brief": "epoll"}}), EPOLL,
    ]
    await say("составь памятку по epoll", llm, life_settings, db)
    staged = []

    async def stage_edit(message, text, *args):
        staged.append(text)

    monkeypatch.setattr(log_text, "stage_edit", stage_edit)
    monkeypatch.setattr(log_text, "send_edit", stage_edit)
    await say("убери французский жим из дня рук", llm, life_settings, db, at=T0 + timedelta(minutes=1))
    assert staged[0] == "убери французский жим из дня рук"
    assert len(llm.bodies) == 2 and len(hl.PENDING) == 1  # no rewrite call; the draft is still there


# ---- kind="life" from the parser ----


async def test_parser_kind_life_goes_to_the_life_model(life_settings, db):
    await owner(db)
    llm = FakeLLM(life_settings)
    llm.answers += [{"kind": "life"}, life_answer(add_event(day="2026-10-10", start="15:00", title="Созвон с Ильёй",
                                                            area="work"))]
    msg = await say("в субботу в 15 созвон с Ильёй", llm, life_settings, db)
    assert not is_life_request(llm.bodies[0]) and is_life_request(llm.bodies[1])
    assert shown(msg) == "В работу: «Созвон с Ильёй» — сб 10.10 в 15:00"


async def test_parser_kind_life_nothing_to_do(life_settings, db):
    await owner(db)
    llm = FakeLLM(life_settings)
    llm.answers += [{"kind": "life"}, life_answer()]
    msg = await say("в субботу что-то с Ильёй", llm, life_settings, db)
    assert len(llm.bodies) == 2
    assert shown(msg) == hl.NOT_UNDERSTOOD and "Не понял, что сделать в делах" in hl.NOT_UNDERSTOOD


async def test_parser_kind_life_after_the_quick_words_asks_the_model_once(life_settings, db):
    await owner(db)
    llm = FakeLLM(life_settings)
    llm.answers += [life_answer(), {"kind": "life"}]
    msg = await say("зачёт перенесли, кажется", llm, life_settings, db)
    assert len(llm.bodies) == 2 and is_life_request(llm.bodies[0])
    assert shown(msg) == hl.NOT_UNDERSTOOD


async def test_parser_kind_life_shows_why_an_action_was_dropped(life_settings, db):
    await owner(db)
    llm = FakeLLM(life_settings)
    llm.answers += [{"kind": "life"}, life_answer(add_event(day="2099-01-01", title="Созвон"))]
    msg = await say("созвон первого января 2099", llm, life_settings, db)
    assert shown(msg) == "Дата 2099-01-01 дальше 400 дней от сегодня."


PIZZA = {"description": "пицца, 3 куска", "grams": 300, "kcal": 800, "protein_g": 33, "fat_g": 30, "carbs_g": 99}


async def test_parser_kind_life_with_food_is_a_food_record(life_settings, db, no_diary):
    """«сдал зачёт, съел пиццу 3 куска» called kind="life" with the food in it: the food is never lost."""
    await owner(db)
    llm = FakeLLM(life_settings)
    llm.answers += [{"kind": "life", "foods": [PIZZA]}] * 2  # the parser (and its plausibility round)
    msg = await say("сдал зачёт, съел пиццу 3 куска", llm, life_settings, db)
    assert not any(is_life_request(b) for b in llm.bodies) and not hl.PENDING
    [pending] = log_text.PENDING.values()
    assert pending.result.kind == "food" and pending.result.foods[0].description == "пицца, 3 куска"
    assert "пицца" in shown(msg)


async def test_parser_kind_life_with_wellbeing_is_a_wellbeing_record(life_settings, db, no_diary):
    await owner(db)
    llm = FakeLLM(life_settings)
    llm.answers.append({"kind": "life", "wellbeing": {"sleep_hours": 5}})
    await say("зачёт завтра, спал 5 часов", llm, life_settings, db)
    assert len(llm.bodies) == 1 and not hl.PENDING
    [pending] = log_text.PENDING.values()
    assert pending.result.kind == "wellbeing" and pending.result.wellbeing.sleep_hours == 5


def life_model_down(llm, monkeypatch) -> list[str]:
    """The «Дела» model fails (every model of the chain); the parser still answers."""
    calls: list[str] = []

    async def complete_json(messages, **kwargs):
        calls.append(messages[0]["content"])
        raise LLMError("all models failed")

    monkeypatch.setattr(llm.client, "complete_json", complete_json)
    return calls


async def test_parser_kind_life_with_an_unknown_word_is_still_life(life_settings, db):
    """An unfamiliar subject name in unknown_terms is no dish: without a meal in the text it stays «Дела»."""
    await owner(db)
    llm = FakeLLM(life_settings)
    llm.answers += [{"kind": "life", "unknown_terms": ["ОСиСП"]},
                    life_answer({"op": "add", "entity": "task", "fields": {"title": "Дедлайн по ОСиСП"}})]
    msg = await say("к пятнице ОСиСП сдаю", llm, life_settings, db)  # no quick words: the parser first
    assert len(llm.bodies) == 2 and is_life_request(llm.bodies[1]) and not log_text.PENDING
    assert "ОСиСП" in shown(msg)


async def test_parser_kind_life_with_an_unknown_dish_is_food(life_settings, db, no_diary, monkeypatch):
    async def no_variants(*args):
        return []

    monkeypatch.setattr(log_text, "_variants", no_variants)
    await owner(db)
    llm = FakeLLM(life_settings)
    llm.answers.append({"kind": "life", "unknown_terms": ["гульчатай"], "clarification": "гульчатай — это что?"})
    await say("сдал зачёт, съел гульчатай", llm, life_settings, db)
    assert len(llm.bodies) == 1 and not hl.PENDING


async def test_parser_kind_life_and_the_life_model_fails(life_settings, db, monkeypatch):
    """A failing «Дела» model is not «Не понял»: the user is told the model is down."""
    await owner(db)
    llm = FakeLLM(life_settings)
    failed = life_model_down(llm, monkeypatch)
    llm.answers.append({"kind": "life"})
    msg = await say("в субботу в 15 созвон с Ильёй", llm, life_settings, db)
    assert len(llm.bodies) == 1 and failed == [LIFE_SYSTEM_PROMPT]
    assert shown(msg) == hl.FAILED == "Нейросеть сейчас недоступна, попробуй ещё раз чуть позже."


async def test_quick_words_model_failed_then_parser_kind_life(life_settings, db, monkeypatch):
    """The «Дела» model failed after the quick words, the parser says kind="life": not asked again, and the
    answer says the model is down, not «Не понял»."""
    await owner(db)
    llm = FakeLLM(life_settings)
    failed = life_model_down(llm, monkeypatch)
    llm.answers.append({"kind": "life"})
    msg = await say("добавь задачу купить билеты", llm, life_settings, db)
    assert failed == [LIFE_SYSTEM_PROMPT] and len(llm.bodies) == 1 and not is_life_request(llm.bodies[0])
    assert shown(msg) == hl.FAILED


async def test_quick_words_model_found_nothing_then_parser_kind_life(life_settings, db):
    await owner(db)
    llm = FakeLLM(life_settings)
    llm.answers += [life_answer(), {"kind": "life"}]
    msg = await say("добавь задачу", llm, life_settings, db)
    assert shown(msg) == hl.NOT_UNDERSTOOD


# ---- the diary answer: «ДЕЛА: …» and the «Дела» block ----


def test_split_commands():
    assert qa.split_commands("Добавлю.\nДЕЛА: «добавь задачу купить билеты»") == (
        "Добавлю.", None, "добавь задачу купить билеты")
    assert qa.split_commands("Ок.\nПРАВКА: сделай сегодня день рук\n**ДЕЛА:** закрой задачу x") == (
        "Ок.", "сделай сегодня день рук", "закрой задачу x")
    assert qa.split_command("Руки — ок.\nПРАВКА: сделай сегодня день рук") == ("Руки — ок.", "сделай сегодня день рук")
    assert qa.split_command("Ок.\nДЕЛА: x") == ("Ок.", None)  # the line never reaches the chat
    plain = "Завтра:\nДела: пара в 10:00, потом зал"  # «Дела:» in an answer is not a command
    assert qa.split_commands(plain) == (plain, None, None)


async def test_answer_suggests_a_life_change_under_itself(life_settings, db):
    await owner(db)
    llm = FakeLLM(life_settings)
    llm.answers += [
        {"kind": "question", "clarification": "Не знаю."},
        "Добавлю в задачи.\nДЕЛА: добавь задачу купить билеты на поезд",
        life_answer({"op": "add", "entity": "task", "fields": {"title": "Купить билеты на поезд"}}),
    ]
    msg = await say("не забыть бы купить билеты на поезд, поможешь?", llm, life_settings, db)
    first, second = [c.args[0] for c in msg.answer.await_args_list]
    assert first == "Добавлю в задачи." and "ДЕЛА" not in first
    assert second == "Добавлю задачу «Купить билеты на поезд»"
    assert is_life_request(llm.bodies[2])
    assert llm.bodies[2]["messages"][1]["content"].endswith("Сообщение: добавь задачу купить билеты на поезд")
    assert log_text.QA[USER][-1][1].endswith("\nДЕЛА: добавь задачу купить билеты на поезд")


async def test_answer_context_has_the_life_block_for_the_owner_only(life_settings, db):
    uid = await owner(db)
    await put(
        db,
        LifeEvent(user_id=uid, day=TODAY, start="11:00", title=HTTP, area="study"),
        LifeSlot(user_id=uid, weekday=3, start="10:00", title="ИЗ Математика", area="work", place="Восход"),
    )
    tz = ZoneInfo(life_settings.timezone)
    async with db() as s:
        user = await get_or_create_user(s, USER, "Amir")
        text = await qa.build_context(s, user, life_settings, None, tz, T0)
        other = await get_or_create_user(s, 7, "Other")
        others = await qa.build_context(s, other, life_settings, None, tz, T0)
    assert "Дела (раздел «Дела»" in text
    assert f"Сегодня (вт 06.10): 11:00 {HTTP}" in text
    assert "Завтра (ср 07.10): 10:00 ИЗ Математика (Восход)" in text
    assert "Дела (раздел" not in others


async def test_answer_request_holds_the_life_block(life_settings, db):
    """A question that reaches the model: the «Дела» block is in its request."""
    uid = await owner(db)
    await put(db, LifeEvent(user_id=uid, day=TODAY + timedelta(days=1), start="09:00", title="Созвон", area="work"))
    llm = FakeLLM(life_settings)
    llm.answers.append("Завтра в 9:00 созвон.")
    reply = await qa.respond(db, USER, "Amir", "есть что-нибудь важное на неделе?", [], life_settings, llm.client, T0)
    assert reply.text == "Завтра в 9:00 созвон."
    assert "Завтра (ср 07.10): 09:00 Созвон" in llm.bodies[0]["messages"][0]["content"]


async def test_what_do_i_have_tomorrow_adds_the_agenda_to_the_training_day(life_settings, db):
    """«Что у меня завтра?» is answered from the database (the program day); the owner's «Дела» of that day
    follow in the same message. A question about the training itself gets the training alone."""
    from test_next_weights_day import NOW, OWNER_FRIDAY, NoModel, _owner

    async with db() as s:
        user = await _owner(s)
        s.add_all([
            LifeSlot(user_id=user.id, weekday=5, start="10:00", title="ИЗ Математика", area="work", place="Восход"),
            LifeDeadline(user_id=user.id, day=date(2026, 10, 9), title="Лаба 1"),
        ])
        await s.commit()
    reply = await qa.respond(db, 42, "Amir", "что у меня завтра?", [], life_settings, NoModel(), NOW)
    agenda = "\n\nДела на завтра (пт 09.10):\n• 10:00 ИЗ Математика (Восход)\n• дедлайн: Лаба 1"
    assert reply.text == OWNER_FRIDAY + agenda
    assert reply.html.endswith(agenda)
    training = await qa.respond(db, 42, "Amir", "какая завтра тренировка", [], life_settings, NoModel(), NOW)
    assert training.text == OWNER_FRIDAY


async def test_general_question_with_numbers_passes_the_check(life_settings, db):
    await owner(db)
    llm = FakeLLM(life_settings)
    said = ("epoll следит за множеством дескрипторов одним вызовом.\n"
            "• epoll_create1 создаёт экземпляр, epoll_ctl добавляет fd, epoll_wait ждёт до 64 событий за раз.\n"
            "• Было медленнее с select: он копирует весь набор, до 1024 fd.")
    llm.answers.append(said)
    reply = await qa.respond(db, USER, "Amir", "как работает epoll?", [], life_settings, llm.client, T0)
    assert reply.layer == qa.LLM and reply.text.startswith("epoll следит")


# ---- the dashboard inside Telegram: initData instead of the write token (moved from test_hub_tasks.py) ----

DATA_JS = """window.DATA = { todos: [ { id: "t1", text: "Обновить резюме", tag: "работа" } ], chores: [] };"""


@pytest.fixture
def hub_settings(tmp_path):
    site = tmp_path / "my-hub"
    site.mkdir()
    (site / "data.js").write_text(DATA_JS, encoding="utf-8")
    return make_settings(tmp_path, hub_dir=site, public_url="https://gym.example", hub_token="r", hub_write_token="w")


async def test_state_with_init_data(hub_settings, db):
    await owner(db)
    app = create_app(hub_settings, db)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        mine = {"X-Telegram-Init-Data": init_data(USER)}
        r = await c.put("/api/hub/state", json={"base_version": 0, "state": {"done": {}}}, headers=mine)
        assert r.status_code == 200 and r.json()["version"] == 1
        assert (await c.get("/api/hub/state", headers=mine)).json()["version"] == 1
        other = {"X-Telegram-Init-Data": init_data(7)}
        assert (await c.get("/api/hub/state", headers=other)).status_code == 403
        forged = {"X-Telegram-Init-Data": init_data(USER, token="999:zzz")}
        assert (await c.get("/api/hub/state", headers=forged)).status_code == 403
        # initData decides on its own: a bad one is not rescued by the tokens
        r = await c.get("/api/hub/state?token=r", headers=forged | {"X-Hub-Write-Token": "w"})
        assert r.status_code == 403
        r = await c.options("/api/hub/state", headers={"Origin": "https://aventrallc.github.io",
                                                      "Access-Control-Request-Method": "GET"})
        assert "X-Telegram-Init-Data" in r.headers["access-control-allow-headers"]

