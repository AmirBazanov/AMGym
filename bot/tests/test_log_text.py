"""Free-text handler: dialog context between messages, stale previews, no echo."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select

from gymbot.db.models import ActiveWorkout, FoodEntry, User, UserFact, WellbeingEntry
from gymbot.handlers import log_text
from gymbot.llm.openrouter import OpenRouterClient
from gymbot.llm.prompts import EXAMPLES, MINIAPP_SETUP_ANSWER
from gymbot.llm.schemas import ParseResult
from gymbot.services import food_lookup
from gymbot.services.workouts import WorkoutIn

T0 = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
USER = 42


def food(n: int, name: str = "самса", revises: bool = False) -> dict:
    return {
        "kind": "food",
        "revises": revises,
        "foods": [
            {"description": f"{name}, {n} шт", "grams": 150 * n, "kcal": 400 * n, "protein_g": 15 * n,
             "fat_g": 22 * n, "carbs_g": 38 * n}
        ],
    }


class FakeLLM:
    """MockTransport-backed OpenRouterClient that replays queued answers and records request bodies."""

    def __init__(self, settings):
        self.answers: list[dict] = []
        self.bodies: list[dict] = []
        self.gates: dict[int, asyncio.Event] = {}  # request number -> event the answer waits for
        http = httpx.AsyncClient(transport=httpx.MockTransport(self._handle))
        self.client = OpenRouterClient(settings.model_copy(update={"openrouter_api_key": "k"}), http)

    async def _handle(self, req: httpx.Request) -> httpx.Response:
        self.bodies.append(json.loads(req.content))
        answer = self.answers.pop(0)
        content = answer if isinstance(answer, str) else json.dumps(answer, ensure_ascii=False)
        gate = self.gates.get(len(self.bodies) - 1)
        if gate is not None:
            async with asyncio.timeout(2):
                await gate.wait()
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    async def requested(self, n: int) -> None:
        async with asyncio.timeout(2):
            while len(self.bodies) < n:
                await asyncio.sleep(0.001)

    def had_history(self) -> bool:
        """Whether the last request had dialog turns between the few-shot examples and the text."""
        return self.last_messages()[-2] != EXAMPLES[-1][1]

    def last_messages(self) -> list[str]:
        return [m["content"] for m in self.bodies[-1]["messages"]]


def message(text: str, at: datetime = T0, user_id: int = USER):
    return SimpleNamespace(
        text=text,
        date=at,
        from_user=SimpleNamespace(id=user_id, full_name="Amir"),
        chat=SimpleNamespace(id=user_id),
        bot=SimpleNamespace(send_chat_action=AsyncMock()),
        answer=AsyncMock(),
    )


def callback(data: str, user_id: int = USER):
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=user_id, full_name="Amir"),
        message=SimpleNamespace(edit_text=AsyncMock(), edit_reply_markup=AsyncMock()),
        answer=AsyncMock(),
    )


def token_of(msg) -> str:
    kb = msg.answer.await_args.kwargs["reply_markup"]
    return kb.inline_keyboard[0][0].callback_data.split(":", 1)[1]


@pytest.fixture(autouse=True)
def clean_state():
    for store in (log_text.PENDING, log_text.CONTEXT, log_text.FACTS):
        store.clear()
    yield
    for store in (log_text.PENDING, log_text.CONTEXT, log_text.FACTS):
        store.clear()


@pytest.fixture(autouse=True)
def no_diary_answer(request, monkeypatch):
    """Tests here are about the parser's dialog; the diary answer to questions has its own tests (`diary`)."""
    if "diary" in request.fixturenames:
        return

    async def parser_answer(message, text, result, *args):
        return result

    monkeypatch.setattr(log_text, "_diary_answer", parser_answer)


@pytest.fixture
def diary():
    log_text.QA.clear()
    yield
    log_text.QA.clear()


@pytest.fixture
def llm(settings):
    return FakeLLM(settings)


async def send(text, llm, settings, db, at=T0):
    msg = message(text, at)
    await log_text.log_free_text(msg, settings, db, llm.client)
    return msg


async def test_second_message_gets_previous_exchange_as_history(llm, settings, db):
    llm.answers = [food(1), food(3)]
    first = await send("три куриные самсы", llm, settings, db)
    assert "три куриные самсы" == llm.last_messages()[-1]
    first_token = token_of(first)

    second = await send("три штуки", llm, settings, db, T0 + timedelta(minutes=2))
    sent = llm.last_messages()
    assert sent[-3] == "три куриные самсы"
    assert json.loads(sent[-2])["foods"][0]["grams"] == 150  # the previous parsed JSON
    assert sent[-1] == "три штуки"

    # The corrected preview replaces the old one: its button no longer saves a duplicate.
    assert first_token not in log_text.PENDING
    pending = log_text.PENDING[token_of(second)]
    assert pending.result.foods[0].grams == 450
    assert pending.raw_text == "три куриные самсы\nтри штуки"


async def test_context_expires(llm, settings, db):
    llm.answers = [food(1), food(1, "гречка")]
    await send("три куриные самсы", llm, settings, db)
    await send("гречка 200 г", llm, settings, db, T0 + log_text.CONTEXT_TTL + timedelta(seconds=1))
    assert not llm.had_history()
    assert len(log_text.PENDING) == 2  # unrelated record: the first preview stays valid


async def test_save_clears_context(llm, settings, db):
    llm.answers = [food(3), food(1)]
    msg = await send("три куриные самсы", llm, settings, db)
    cb = callback(f"save:{token_of(msg)}")
    await log_text.save(cb, settings, db)
    async with db() as s:
        rows = (await s.scalars(select(FoodEntry))).all()
    assert [(r.raw_text, float(r.grams)) for r in rows] == [("три куриные самсы", 450.0)]

    await send("ещё две", llm, settings, db, T0 + timedelta(minutes=1))
    assert not llm.had_history()


async def test_drop_clears_context(llm, settings, db):
    llm.answers = [food(3), food(1)]
    msg = await send("три куриные самсы", llm, settings, db)
    await log_text.drop(callback(f"drop:{token_of(msg)}"))
    await send("гречка", llm, settings, db, T0 + timedelta(minutes=1))
    assert not llm.had_history()


async def test_saving_old_preview_keeps_newer_context(llm, settings, db):
    llm.answers = [food(1), food(1, "гречка"), food(2, "гречка")]
    later = T0 + log_text.CONTEXT_TTL + timedelta(minutes=1)
    first = await send("самса", llm, settings, db)
    await send("гречка", llm, settings, db, later)  # context expired: a new record, first preview stays
    await log_text.save(callback(f"save:{token_of(first)}"), settings, db)
    await send("нет, две порции", llm, settings, db, later + timedelta(minutes=1))
    assert llm.last_messages()[-3] == "гречка"


async def test_unknown_keeps_record_and_save_of_its_preview_ends_it(llm, settings, db):
    llm.answers = [food(1), {"kind": "unknown", "clarification": "Сколько штук?"}, food(2)]
    first = await send("самса", llm, settings, db)
    await send("ну эту", llm, settings, db, T0 + timedelta(minutes=1))
    assert token_of(first) in log_text.PENDING  # not savable: the first preview is still valid
    assert log_text.CONTEXT[USER].token == token_of(first)
    await log_text.save(callback(f"save:{token_of(first)}"), settings, db)
    await send("две", llm, settings, db, T0 + timedelta(minutes=2))
    assert "самса" not in llm.last_messages()[-3]


UNKNOWN = {"kind": "unknown", "clarification": "Сколько штук?"}


async def test_unrelated_record_after_question_keeps_first_preview(llm, settings, db):
    llm.answers = [food(3), UNKNOWN, workout("жим лёжа")]
    first = await send("три самсы", llm, settings, db)
    await send("ммм", llm, settings, db, T0 + timedelta(minutes=1))
    third = await send("жим 3х10 на 60", llm, settings, db, T0 + timedelta(minutes=2))
    sent = llm.last_messages()
    assert sent[-5] == "три самсы" and json.loads(sent[-4])["kind"] == "food"  # the record is not lost
    assert sent[-3] == "ммм" and json.loads(sent[-2])["kind"] == "unknown"
    assert token_of(first) in log_text.PENDING
    assert log_text.PENDING[token_of(third)].raw_text == "жим 3х10 на 60"


async def test_answer_to_question_revises_record(llm, settings, db):
    llm.answers = [food(1), UNKNOWN, food(4, revises=True)]
    first = await send("самса", llm, settings, db)
    await send("ммм", llm, settings, db, T0 + timedelta(minutes=1))
    third = await send("четыре", llm, settings, db, T0 + timedelta(minutes=2))
    assert llm.last_messages()[-5] == "самса"
    assert token_of(first) not in log_text.PENDING
    assert log_text.PENDING[token_of(third)].raw_text == "самса\nммм\nчетыре"


async def test_question_first_continues_chain(llm, settings, db):
    llm.answers = [UNKNOWN, food(2, "гречка")]
    await send("гречка", llm, settings, db)
    second = await send("две порции", llm, settings, db, T0 + timedelta(minutes=1))
    assert llm.last_messages()[-3] == "гречка"
    assert log_text.PENDING[token_of(second)].raw_text == "гречка\nдве порции"


async def test_raw_text_keeps_whole_chain_history_is_capped(llm, settings, db):
    llm.answers = [food(1), *(food(n, revises=True) for n in range(2, 6))]
    texts = ["самса", "две", "нет, три", "четыре", "пять"]
    for i, t in enumerate(texts):
        msg = await send(t, llm, settings, db, T0 + timedelta(minutes=i))
    assert log_text.PENDING[token_of(msg)].raw_text == "\n".join(texts)
    assert llm.last_messages()[-3] == "\n".join(texts[1:4])  # last MAX_CHAIN messages of the chain


async def test_save_of_old_preview_during_parse_makes_correction_new(llm, settings, db):
    llm.answers = [food(1), food(3, revises=True)]
    first = await send("самса", llm, settings, db)
    llm.gates[1] = asyncio.Event()
    msg = message("три штуки", T0 + timedelta(minutes=1))
    task = asyncio.create_task(log_text.log_free_text(msg, settings, db, llm.client))
    await llm.requested(2)
    await log_text.save(callback(f"save:{token_of(first)}"), settings, db)  # pressed while the LLM thinks
    llm.gates[1].set()
    await task
    assert log_text.PENDING[token_of(msg)].raw_text == "три штуки"
    assert log_text.CONTEXT[USER].texts == ["три штуки"]


async def test_stale_context_does_not_drop_newer_preview(llm, settings, db):
    llm.answers = [food(1), food(2, revises=True), food(3, revises=True)]
    await send("самса", llm, settings, db)
    llm.gates[1] = asyncio.Event()
    slow = message("три", T0 + timedelta(minutes=1))
    task = asyncio.create_task(log_text.log_free_text(slow, settings, db, llm.client))
    await llm.requested(2)
    fast = await send("две", llm, settings, db, T0 + timedelta(minutes=1))  # parsed while "три" waits
    llm.gates[1].set()
    await task
    assert token_of(fast) in log_text.PENDING
    assert log_text.PENDING[token_of(slow)].raw_text == "три"


async def test_drop_ignores_other_users_token(llm, settings, db):
    llm.answers = [food(1)]
    msg = await send("самса", llm, settings, db)
    await log_text.drop(callback(f"drop:{token_of(msg)}", user_id=USER + 1))
    assert token_of(msg) in log_text.PENDING


async def test_saved_food_message_has_no_question(llm, settings, db):
    llm.answers = [food(1)]
    msg = await send("самса", llm, settings, db)
    cb = callback(f"save:{token_of(msg)}")
    await log_text.save(cb, settings, db)
    assert "Записать" not in cb.message.edit_text.await_args.args[0]


async def test_question_does_not_replace_context(llm, settings, db):
    llm.answers = [food(1), {"kind": "question", "clarification": "Около 300 ккал."}, food(3)]
    await send("самса", llm, settings, db)
    await send("а сколько в ней калорий?", llm, settings, db, T0 + timedelta(minutes=1))
    await send("три штуки", llm, settings, db, T0 + timedelta(minutes=2))
    assert llm.last_messages()[-3] == "самса"


async def test_question_answer_never_echoes_user_text(llm, settings, db):
    llm.answers = [{"kind": "question", "clarification": "три штуки"}]
    msg = await send("три штуки", llm, settings, db)
    reply = msg.answer.await_args.args[0]
    assert reply != "три штуки" and "Не понял" in reply


def half_flatbread() -> dict:
    return {**food(1), "foods": [{**food(1)["foods"][0], "description": "Лепёшка, 0.5 шт"}]}


def wellbeing(*pains: str, sleep: float | None = 6, energy: int | None = 2, revises: bool = False) -> dict:
    return {
        "kind": "wellbeing",
        "revises": revises,
        "wellbeing": {"sleep_hours": sleep, "energy": energy, "pains": [{"place": p, "severity": 3} for p in pains]},
    }


def workout(*names: str, revises: bool = False) -> dict:
    return {
        "kind": "workout",
        "revises": revises,
        "exercises": [{"exercise": n, "sets": [{"reps": 10, "weight_kg": 60}]} for n in names],
    }


@pytest.mark.parametrize(
    ("prev", "new", "expected"),
    [
        (food(1), food(3, revises=True), True),  # flag from the model
        (food(1, "Самса куриная"), food(3, "самса куриная"), True),  # same name, ", N шт" and case ignored
        (food(1), food(1, "гречка"), False),
        (workout("жим лёжа"), workout("присед"), False),
        (workout("жим лёжа", "присед"), workout("Присед"), False),  # same exercise = next set, not a fix
        (workout("жим лёжа"), workout("жим лёжа", revises=True), True),
        (food(1, "жим лёжа"), workout("жим лёжа"), False),  # different kind
        (food(1), workout("присед", revises=True), True),  # the flag wins
        (food(1, "лепёшка"), {**food(1), "foods": [*food(1, "плов")["foods"], *food(2, "лепёшка")["foods"]]}, True),
        (half_flatbread(), food(1, "лепёшка"), True),  # fractional pieces are cut too
        (wellbeing("плечо"), wellbeing("колено"), True),  # one wellbeing record per dialog
        (wellbeing("плечо"), food(1), False),
        (food(1), wellbeing("плечо"), False),
        (food(1), wellbeing("плечо", revises=True), False),  # a wellbeing record cannot fix food
        (wellbeing("плечо"), food(1, revises=True), False),
    ],
)
def test_is_revision(prev, new, expected):
    assert log_text.is_revision(ParseResult.model_validate(prev), ParseResult.model_validate(new)) is expected


async def test_unrelated_second_record_keeps_first_preview(llm, settings, db):
    llm.answers = [workout("жим лёжа"), workout("присед")]
    first = await send("жим лёжа 3 по 10 на 60", llm, settings, db)
    second = await send("присед 4 по 8 на 80", llm, settings, db, T0 + timedelta(minutes=1))
    assert llm.last_messages()[-3] == "жим лёжа 3 по 10 на 60"  # history is still sent
    assert log_text.CONTEXT[USER].raw_text == "присед 4 по 8 на 80"

    for msg in (first, second):
        cb = callback(f"save:{token_of(msg)}")
        await log_text.save(cb, settings, db)
        assert cb.answer.await_args.kwargs.get("show_alert") is None  # no "устарела"
    from gymbot.db.models import WorkoutSet

    async with db() as s:
        raw = sorted(r.raw_text for r in (await s.scalars(select(WorkoutSet))).all())
    assert raw == ["жим лёжа 3 по 10 на 60", "присед 4 по 8 на 80"]


async def test_revision_by_flag_replaces_preview(llm, settings, db):
    llm.answers = [food(1), food(4, "яйцо", revises=True)]
    first = await send("самса", llm, settings, db)
    second = await send("нет, четыре", llm, settings, db, T0 + timedelta(minutes=1))
    assert token_of(first) not in log_text.PENDING
    assert log_text.PENDING[token_of(second)].raw_text == "самса\nнет, четыре"


async def test_revision_by_name_without_flag(llm, settings, db):
    llm.answers = [food(1), food(3)]  # the model forgot revises
    first = await send("куриная самса", llm, settings, db)
    second = await send("три штуки", llm, settings, db, T0 + timedelta(minutes=1))
    assert token_of(first) not in log_text.PENDING
    assert log_text.PENDING[token_of(second)].raw_text == "куриная самса\nтри штуки"


async def test_food_saved_with_message_time_not_tap_time(llm, settings, db):
    # Sent at 20:58 UTC (23:58 Moscow), confirmed after midnight: still that day's food.
    llm.answers = [food(1)]
    sent_at = T0.replace(hour=20, minute=58)
    msg = await send("самса", llm, settings, db, sent_at)
    await log_text.save(callback(f"save:{token_of(msg)}"), settings, db)
    async with db() as s:
        row = (await s.scalars(select(FoodEntry))).one()
    assert row.eaten_at.replace(tzinfo=None) == sent_at.replace(tzinfo=None)


async def say(transcript, llm, settings, db, at=T0):
    """A voice message as handlers/voice.py passes it on."""
    msg = message("", at)
    msg.text = None
    await log_text.process_text(
        msg, transcript, settings, db, llm.client,
        raw_text=f"[voice] {transcript}", prefix=f"Распознал: «{transcript}»\n\n",
    )
    return msg


async def test_voice_marker_only_in_raw_text_not_for_the_model(llm, settings, db):
    llm.answers = [workout("жим лёжа")]
    msg = await say("жим лёжа три по десять на шестьдесят", llm, settings, db)
    assert llm.last_messages()[-1] == "жим лёжа три по десять на шестьдесят"
    assert log_text.PENDING[token_of(msg)].raw_text == "[voice] жим лёжа три по десять на шестьдесят"
    assert log_text.CONTEXT[USER].texts == ["жим лёжа три по десять на шестьдесят"]
    reply = msg.answer.await_args.args[0]
    assert reply.startswith("Распознал: «жим лёжа три по десять на шестьдесят»\n\nЗаписать?")

    await log_text.save(callback(f"save:{token_of(msg)}"), settings, db)
    from gymbot.db.models import WorkoutSet

    async with db() as s:
        raw = {r.raw_text for r in (await s.scalars(select(WorkoutSet))).all()}
    assert raw == {"[voice] жим лёжа три по десять на шестьдесят"}


async def test_voice_in_chain_marks_only_its_own_line(llm, settings, db):
    llm.answers = [food(1), food(3, revises=True), food(4, revises=True)]
    await send("самса", llm, settings, db)
    second = await say("три штуки", llm, settings, db, T0 + timedelta(minutes=1))
    assert log_text.PENDING[token_of(second)].raw_text == "самса\n[voice] три штуки"
    third = await send("нет, четыре", llm, settings, db, T0 + timedelta(minutes=2))
    assert log_text.PENDING[token_of(third)].raw_text == "самса\n[voice] три штуки\nнет, четыре"
    sent = llm.last_messages()
    assert sent[-3] == "самса\nтри штуки"  # the history the model sees has no marker
    assert not any("[voice]" in m for m in sent)


async def test_voice_question_keeps_marker_through_clarification(llm, settings, db):
    llm.answers = [food(1), UNKNOWN, food(4, revises=True)]
    await send("самса", llm, settings, db)
    asked = await say("ммм", llm, settings, db, T0 + timedelta(minutes=1))
    assert asked.answer.await_args.args[0] == "Распознал: «ммм»\n\nСколько штук?"
    third = await send("четыре", llm, settings, db, T0 + timedelta(minutes=2))
    assert log_text.PENDING[token_of(third)].raw_text == "самса\n[voice] ммм\nчетыре"
    assert not any("[voice]" in m for m in llm.last_messages())


async def test_note_reaches_the_reply(llm, settings, db):
    llm.answers = [food(3), {**food(3, revises=True), "note": "Белок 45 → 34 г: самса так себе, больше теста."}]
    await send("три куриные самсы", llm, settings, db)
    msg = await send("самса была так себе, белка поменьше", llm, settings, db, T0 + timedelta(minutes=1))
    assert "Белок 45 → 34 г" in msg.answer.await_args.args[0]


async def test_answer_to_question_inside_record_revises_it(llm, settings, db):
    first_answer = {**food(1, "плов"), "clarification": "Косушка — это что?"}
    llm.answers = [first_answer, food(1, "плов")]  # the model forgot revises, the name matches
    first = await send("плов, косушку", llm, settings, db)
    assert "Уточни: Косушка — это что?" in first.answer.await_args.args[0]
    second = await send("каса, пиала плова", llm, settings, db, T0 + timedelta(minutes=1))
    assert json.loads(llm.last_messages()[-2])["clarification"] == "Косушка — это что?"  # the model saw it
    assert token_of(first) not in log_text.PENDING
    assert log_text.PENDING[token_of(second)].raw_text == "плов, косушку\nкаса, пиала плова"


async def test_saved_message_drops_the_question(llm, settings, db):
    llm.answers = [{**food(1, "плов"), "clarification": "Косушка — это что?"}]
    msg = await send("плов, косушку", llm, settings, db)
    cb = callback(f"save:{token_of(msg)}")
    await log_text.save(cb, settings, db)
    assert "Уточни" not in cb.message.edit_text.await_args.args[0]


async def test_wellbeing_saved_with_message_time_and_raw_text(llm, settings, db):
    llm.answers = [{**wellbeing("левое плечо"), "wellbeing": {
        "sleep_hours": 6.5, "sleep_quality": 2, "energy": 2, "mood": None,
        "pains": [{"place": "левое плечо", "severity": 3}], "note": "после ночной смены"}}]
    sent_at = T0.replace(hour=20, minute=58)
    msg = await send("спал 6.5 часов, болит левое плечо, сил мало", llm, settings, db, sent_at)
    assert msg.answer.await_args.args[0].startswith("Записать самочувствие?")
    cb = callback(f"save:{token_of(msg)}")
    await log_text.save(cb, settings, db)
    shown = cb.message.edit_text.await_args.args[0]
    assert not shown.startswith("Записать") and "Сон 6.5 ч (качество 2/5)" in shown
    assert shown.endswith("Самочувствие сохранено ✅")
    async with db() as s:
        row = (await s.scalars(select(WellbeingEntry))).one()
        assert await s.scalar(select(FoodEntry.id)) is None
    assert row.noted_at.replace(tzinfo=None) == sent_at.replace(tzinfo=None)
    assert float(row.sleep_hours) == 6.5 and row.sleep_quality == 2 and row.energy == 2 and row.mood is None
    assert json.loads(row.pains) == [{"place": "левое плечо", "severity": 3}]
    assert row.note == "после ночной смены"
    assert row.raw_text == "спал 6.5 часов, болит левое плечо, сил мало"
    assert USER not in log_text.CONTEXT


async def test_wellbeing_without_pains_stores_null(llm, settings, db):
    llm.answers = [wellbeing(sleep=8, energy=5)]
    msg = await send("спал отлично 8 часов", llm, settings, db)
    await log_text.save(callback(f"save:{token_of(msg)}"), settings, db)
    async with db() as s:
        row = (await s.scalars(select(WellbeingEntry))).one()
    assert row.pains is None and row.note is None and float(row.sleep_hours) == 8


async def test_wellbeing_correction_replaces_preview_even_without_flag(llm, settings, db):
    llm.answers = [wellbeing("плечо"), wellbeing("колено")]  # the model forgot revises
    first = await send("спал 6 часов, болит плечо", llm, settings, db)
    second = await send("нет, плечо не болит, болит колено", llm, settings, db, T0 + timedelta(minutes=1))
    assert llm.last_messages()[-3] == "спал 6 часов, болит плечо"  # the model saw the record
    assert token_of(first) not in log_text.PENDING
    pending = log_text.PENDING[token_of(second)]
    assert pending.raw_text == "спал 6 часов, болит плечо\nнет, плечо не болит, болит колено"
    await log_text.save(callback(f"save:{token_of(second)}"), settings, db)
    async with db() as s:
        rows = (await s.scalars(select(WellbeingEntry))).all()
    assert len(rows) == 1 and json.loads(rows[0].pains)[0]["place"] == "колено"


async def test_wellbeing_with_stray_flag_keeps_food_preview(llm, settings, db):
    llm.answers = [food(1), wellbeing("плечо", revises=True)]
    first = await send("самса", llm, settings, db)
    await send("спал 6 часов, болит плечо", llm, settings, db, T0 + timedelta(minutes=1))
    assert token_of(first) in log_text.PENDING and len(log_text.PENDING) == 2
    assert log_text.CONTEXT[USER].raw_text == "спал 6 часов, болит плечо"


async def test_food_after_wellbeing_keeps_both_previews(llm, settings, db):
    llm.answers = [wellbeing("плечо"), food(1)]
    first = await send("спал 6 часов, болит плечо", llm, settings, db)
    await send("самса", llm, settings, db, T0 + timedelta(minutes=1))
    assert token_of(first) in log_text.PENDING and len(log_text.PENDING) == 2


async def test_empty_wellbeing_is_not_savable(llm, settings, db):
    llm.answers = [{"kind": "wellbeing", "wellbeing": {"pains": []}, "clarification": "Как спалось?"}]
    msg = await send("ну такое", llm, settings, db)
    assert msg.answer.await_args.args[0] == "Как спалось?"
    assert "reply_markup" not in msg.answer.await_args.kwargs and not log_text.PENDING


# ---- facts: "запомни: ..." and the model's `remember` ----


def buttons(msg) -> list[list[str]]:
    kb = msg.answer.await_args.kwargs.get("reply_markup")
    return [[b.callback_data.split(":", 1)[0] for b in row] for row in kb.inline_keyboard] if kb else []


async def facts_in_db(db) -> list[UserFact]:
    async with db() as s:
        return list((await s.scalars(select(UserFact).order_by(UserFact.id))).all())


async def add_fact(db, text: str, active: bool = True) -> None:
    async with db() as s:
        user = await s.scalar(select(User).where(User.telegram_id == USER))
        if user is None:
            user = User(telegram_id=USER, rest_seconds=90)
            s.add(user)
            await s.flush()
        s.add(UserFact(user_id=user.id, text=text, category="other", active=active))
        await s.commit()


async def test_remember_command_saves_after_button_without_llm(llm, settings, db):
    msg = await send("Запомни: не ем творог.", llm, settings, db)
    assert llm.bodies == []  # no model call
    assert msg.answer.await_args.args[0] == "Запомнить? «не ем творог»"
    assert buttons(msg) == [["remember", "drop"]]
    assert await facts_in_db(db) == []  # nothing before the button
    cb = callback(f"remember:{token_of(msg)}")
    await log_text.remember(cb, db)
    [fact] = await facts_in_db(db)
    assert (fact.text, fact.category, fact.active, fact.source_text) == (
        "не ем творог", "food", True, "Запомни: не ем творог."
    )
    assert cb.message.edit_text.await_args.args[0].startswith("Запомнил: «не ем творог»")
    again = callback(f"remember:{token_of(msg)}")
    await log_text.remember(again, db)  # double tap
    assert again.answer.await_args.kwargs.get("show_alert") is True
    assert len(await facts_in_db(db)) == 1


@pytest.mark.parametrize(
    ("text", "fact"),
    [
        ("запомни, что я не ем творог", "я не ем творог"),
        ("запомни что тренируюсь по утрам", "тренируюсь по утрам"),
        ("Запомните — самса у нас 150 г", "самса у нас 150 г"),
        ("запомни, пожалуйста, что я не ем творог", "я не ем творог"),
    ],
)
async def test_remember_command_forms(llm, settings, db, text, fact):
    msg = await send(text, llm, settings, db)
    assert msg.answer.await_args.args[0] == f"Запомнить? «{fact}»" and llm.bodies == []


async def test_remember_command_empty_or_too_long(llm, settings, db):
    empty = await send("запомни", llm, settings, db)
    assert "запомни:" in empty.answer.await_args.args[0] and not log_text.FACTS
    long = await send("запомни: " + "x" * 201, llm, settings, db)
    assert "200" in long.answer.await_args.args[0] and not log_text.FACTS
    assert llm.bodies == []


async def test_word_starting_with_remember_goes_to_the_model(llm, settings, db):
    llm.answers = [{"kind": "question", "clarification": "Понял."}]
    await send("запомнилось плохо", llm, settings, db)
    assert len(llm.bodies) == 1


async def test_remember_command_by_voice_keeps_marker_in_source(llm, settings, db):
    msg = await say("Запомни, что я не ем творог", llm, settings, db)
    assert msg.answer.await_args.args[0] == "Распознал: «Запомни, что я не ем творог»\n\nЗапомнить? «я не ем творог»"
    await log_text.remember(callback(f"remember:{token_of(msg)}"), db)
    [fact] = await facts_in_db(db)
    assert fact.source_text == "[voice] Запомни, что я не ем творог"


async def test_remember_duplicate_is_not_saved_twice(llm, settings, db):
    await add_fact(db, "Не ем творог!")
    msg = await send("запомни: не ем ТВОРОГ", llm, settings, db)
    cb = callback(f"remember:{token_of(msg)}")
    await log_text.remember(cb, db)
    assert len(await facts_in_db(db)) == 1
    assert "уже помню" in cb.message.edit_text.await_args.args[0].lower()


async def test_remember_limit_keeps_the_offer(llm, settings, db):
    for i in range(50):
        await add_fact(db, f"факт {i}")
    msg = await send("запомни: не ем творог", llm, settings, db)
    cb = callback(f"remember:{token_of(msg)}")
    await log_text.remember(cb, db)
    assert cb.answer.await_args.kwargs.get("show_alert") is True and "/facts" in cb.answer.await_args.args[0]
    assert token_of(msg) in log_text.FACTS and len(await facts_in_db(db)) == 50


def with_fact(answer: dict, fact: str) -> dict:
    return {**answer, "remember": fact}


async def test_record_with_fact_offers_separate_button(llm, settings, db):
    llm.answers = [with_fact(food(2), "самса ~150 г")]
    msg = await send("съел 2 самсы, они у нас большие, грамм по 150", llm, settings, db)
    reply = msg.answer.await_args.args[0]
    assert reply.startswith("Записать еду?") and reply.endswith("\n\nЗапомнить: «самса ~150 г»")
    assert buttons(msg) == [["save", "drop"], ["remember"]]
    token = token_of(msg)

    cb = callback(f"remember:{token}")
    await log_text.remember(cb, db)
    [fact] = await facts_in_db(db)
    assert fact.text == "самса ~150 г" and fact.category == "food"
    assert fact.source_text == "съел 2 самсы, они у нас большие, грамм по 150"
    kb = cb.message.edit_reply_markup.await_args.kwargs["reply_markup"]
    assert [[b.callback_data for b in row] for row in kb.inline_keyboard] == [[f"save:{token}", f"drop:{token}"]]
    assert token in log_text.PENDING  # the record can still be saved

    save = callback(f"save:{token}")
    await log_text.save(save, settings, db)
    assert save.message.edit_text.await_args.kwargs.get("reply_markup") is None
    async with db() as s:
        assert len((await s.scalars(select(FoodEntry))).all()) == 1


async def test_save_first_keeps_the_remember_button(llm, settings, db):
    llm.answers = [with_fact(food(2), "самса ~150 г")]
    msg = await send("съел 2 самсы, они у нас большие, грамм по 150", llm, settings, db)
    token = token_of(msg)
    save = callback(f"save:{token}")
    await log_text.save(save, settings, db)
    shown = save.message.edit_text.await_args
    assert "Запомнить: «самса ~150 г»" in shown.args[0]
    kb = shown.kwargs["reply_markup"]
    assert [[b.callback_data for b in row] for row in kb.inline_keyboard] == [[f"remember:{token}"]]
    cb = callback(f"remember:{token}")
    await log_text.remember(cb, db)
    assert [f.text for f in await facts_in_db(db)] == ["самса ~150 г"]
    assert cb.message.edit_reply_markup.await_args.kwargs["reply_markup"] is None


async def test_drop_forgets_the_offered_fact(llm, settings, db):
    llm.answers = [with_fact(food(2), "самса ~150 г")]
    msg = await send("съел 2 самсы, они у нас большие", llm, settings, db)
    await log_text.drop(callback(f"drop:{token_of(msg)}"))
    assert not log_text.FACTS and not log_text.PENDING


async def test_drop_of_fact_only_offer_checks_owner(llm, settings, db):
    msg = await send("запомни: не ем творог", llm, settings, db)
    await log_text.drop(callback(f"drop:{token_of(msg)}", user_id=777))
    assert token_of(msg) in log_text.FACTS
    await log_text.drop(callback(f"drop:{token_of(msg)}"))
    assert not log_text.FACTS


async def test_known_fact_is_not_offered_again(llm, settings, db):
    await add_fact(db, "Самса ~150 г.")
    llm.answers = [with_fact(food(2), "самса ~150 г")]
    msg = await send("две самсы", llm, settings, db)
    assert "Запомнить" not in msg.answer.await_args.args[0]
    assert buttons(msg) == [["save", "drop"]] and not log_text.FACTS
    assert "самса ~150 г" in llm.bodies[-1]["messages"][0]["content"]  # active facts reach the model


async def test_inactive_facts_are_not_sent_to_the_model(llm, settings, db):
    await add_fact(db, "старый факт", active=False)
    llm.answers = [food(1)]
    await send("самса", llm, settings, db)
    assert "старый факт" not in llm.bodies[-1]["messages"][0]["content"]


async def test_fact_in_a_question_is_offered_and_context_kept(llm, settings, db):
    llm.answers = [food(1), with_fact({"kind": "question", "clarification": "Учту."}, "аллергия на орехи")]
    await send("самса", llm, settings, db)
    before = log_text.CONTEXT[USER]
    msg = await send("у меня аллергия на орехи", llm, settings, db, T0 + timedelta(minutes=1))
    assert msg.answer.await_args.args[0] == "Учту.\n\nЗапомнить: «аллергия на орехи»"
    assert buttons(msg) == [["remember", "drop"]]
    assert log_text.CONTEXT[USER] is before
    await log_text.remember(callback(f"remember:{token_of(msg)}"), db)
    [fact] = await facts_in_db(db)
    assert fact.category == "health"


async def test_revision_carries_the_offered_fact(llm, settings, db):
    llm.answers = [with_fact(food(2), "самса ~150 г"), food(3, revises=True)]
    first = await send("съел 2 самсы, они у нас большие", llm, settings, db)
    second = await send("нет, три", llm, settings, db, T0 + timedelta(minutes=1))
    assert token_of(first) not in log_text.FACTS
    assert buttons(second) == [["save", "drop"], ["remember"]]
    assert "Запомнить: «самса ~150 г»" in second.answer.await_args.args[0]
    assert log_text.FACTS[token_of(second)].text == "самса ~150 г"


# ---- unknown words: web lookup variants (gymbot.services.food_lookup) ----

KURT = food_lookup.Option(
    name="курт (сушёный сыр)", portion_g=25, kcal=65, protein_g=6, fat_g=4, carbs_g=1,
    note="на 100 г 260 ккал, по Open Food Facts",
)  # fmt: skip
KURT_SMALL = food_lookup.Option(
    name="курт (творожный)", portion_g=10, kcal=26, protein_g=2.5, fat_g=1.5, carbs_g=0.3
)
KUTAB = food_lookup.Option(
    name="кутаб (лепёшка с зеленью)", portion_g=150, kcal=330, protein_g=10, fat_g=12, carbs_g=44, note="оценка"
)
BURSAK = food_lookup.Option(
    name="бурсак (жареное тесто)", portion_g=30, kcal=110, protein_g=2, fat_g=6, carbs_g=11,
    note="оценка по описанию",
)  # fmt: skip

TEA = {"description": "чай", "grams": 200, "kcal": 2, "protein_g": 0, "fat_g": 0, "carbs_g": 0.5}
UNCLEAR_KURT = {"kind": "food", "foods": [], "clarification": "«курт» — это что?", "unknown_terms": ["курт"]}
BURSAK_TEXT = "съел пару бурсаков и чай"
BURSAK_WITH_TEA = {
    "kind": "food",
    "foods": [TEA],
    "clarification": "«бурсак» — это что?",
    "unknown_terms": ["бурсак"],
}


class FakeFind:
    """Replaces food_lookup.find_options: options per term (or `default`), a call log, optional failure."""

    def __init__(self):
        self.default: list | Exception = []
        self.by_term: dict[str, list | Exception] = {}
        self.calls: list[tuple] = []

    async def __call__(self, term, phrase, llm, http, *, tavily_key=""):
        self.calls.append((term, phrase, llm, http, tavily_key))
        result = self.by_term.get(term, self.default)
        if isinstance(result, Exception):
            raise result
        return result

    def terms(self) -> list[str]:
        return [c[0] for c in self.calls]


@pytest.fixture
def find(monkeypatch):
    fake = FakeFind()
    monkeypatch.setattr(food_lookup, "find_options", fake)
    return fake


@pytest.fixture(autouse=True)
def clean_lookups():
    log_text.LOOKUPS.clear()
    yield
    log_text.LOOKUPS.clear()


def reply_markup(msg):
    return msg.answer.await_args.kwargs["reply_markup"]


def edited_markup(cb):
    return cb.message.edit_text.await_args.kwargs["reply_markup"]


def labels(markup) -> list[list[str]]:
    return [[b.text for b in row] for row in markup.inline_keyboard]


def data(markup) -> list[list[str]]:
    return [[b.callback_data for b in row] for row in markup.inline_keyboard]


def lookup_token(msg) -> str:
    for row in reply_markup(msg).inline_keyboard:
        for b in row:
            if b.callback_data.startswith("lookup:"):
                return b.callback_data.split(":")[1]
    raise AssertionError("no lookup button")


async def pick(token: str, choice: int | str, user_id: int = USER):
    cb = callback(f"lookup:{token}:{choice}", user_id)
    await log_text.pick(cb)
    return cb


def stale_alert(cb) -> bool:
    return cb.answer.await_args == ((log_text.LOOKUP_STALE,), {"show_alert": True})


async def test_unclear_message_with_variants_offers_buttons_instead_of_the_question(llm, settings, db, find):
    llm.answers = [UNCLEAR_KURT]
    find.default = [KURT, KURT_SMALL]
    msg = await send("съел 5 маленьких куртов", llm, settings, db)
    token = lookup_token(msg)
    text = msg.answer.await_args.args[0]
    assert "Не знаю «курт»" in text and "Варианты на 5 шт" in text
    assert "курт (сушёный сыр), 25 г: 65 ккал" in text and "(на 100 г 260 ккал, по Open Food Facts)" in text
    assert "Уточни" not in text and "это что" not in text  # the model's question is replaced by the variants
    kb = reply_markup(msg)
    assert [row[0] for row in labels(kb)[:2]] == ["1 · курт 25 г · 65 ккал", "2 · курт 10 г · 26 ккал"]
    assert data(kb) == [
        [f"lookup:{token}:0"], [f"lookup:{token}:1"], [f"lookup:{token}:other", f"drop:{token}"],
    ]  # fmt: skip
    assert labels(kb)[-1] == ["Другое", "✖ Отмена"]
    assert not log_text.PENDING  # nothing to save yet
    assert token in log_text.LOOKUPS
    assert log_text.CONTEXT[USER].token is None


async def test_lookup_gets_the_original_text_and_key(llm, settings, db, find):
    llm.answers = [UNCLEAR_KURT]
    find.default = [KURT]
    keyed = settings.model_copy(update={"tavily_api_key": "tk"})
    await send("съел 5 маленьких куртов", llm, keyed, db)
    ((term, phrase, used_llm, http, key),) = find.calls
    assert (term, phrase, key) == ("курт", "съел 5 маленьких куртов", "tk")
    assert used_llm is llm.client and http is llm.client.http


async def test_tap_on_a_variant_makes_a_record_that_still_needs_save(llm, settings, db, find):
    text = "съел 5 маленьких куртов"
    llm.answers = [UNCLEAR_KURT]
    find.default = [KURT, KURT_SMALL]
    msg = await send(text, llm, settings, db)
    token = lookup_token(msg)

    cb = await pick(token, 0)
    pending = log_text.PENDING[token]
    assert pending.user_id == USER and pending.raw_text == text
    assert pending.result.kind == "food" and pending.result.unknown_terms == []
    (food_line,) = pending.result.foods
    assert food_line.description == "курт (сушёный сыр), 5 шт" and food_line.grams == 125
    assert food_line.kcal == pytest.approx(5 * KURT.kcal, abs=1)
    cb.message.edit_text.assert_awaited_once()
    shown = cb.message.edit_text.await_args.args[0]
    assert shown.startswith("Записать еду?") and "курт (сушёный сыр), 5 шт 125 г" in shown
    assert "Не знаю" not in shown and "lookup" not in shown
    kb = edited_markup(cb)
    assert labels(kb)[0] == ["✅ Сохранить", "✖ Отмена"]
    assert data(kb) == [[f"save:{token}", f"drop:{token}"]]  # no variant buttons remain
    assert token not in log_text.LOOKUPS
    ex = log_text.CONTEXT[USER]
    assert ex.token == token and ex.result is pending.result and ex.raw_text == text
    cb.answer.assert_awaited_once_with()

    await log_text.save(callback(f"save:{token}"), settings, db)
    async with db() as s:
        (row,) = (await s.scalars(select(FoodEntry))).all()
    assert row.description == "курт (сушёный сыр), 5 шт" and float(row.grams) == 125
    assert row.raw_text == text
    assert token not in log_text.PENDING and USER not in log_text.CONTEXT


async def test_next_message_after_a_tap_sees_the_picked_record(llm, settings, db, find):
    llm.answers = [UNCLEAR_KURT, food(1, "курт (сушёный сыр)", revises=True)]
    find.default = [KURT]
    msg = await send("съел 5 курт", llm, settings, db)
    await pick(lookup_token(msg), 0)
    await send("нет, шесть", llm, settings, db, T0 + timedelta(minutes=1))
    sent = llm.last_messages()
    assert sent[-3] == "съел 5 курт"
    assert json.loads(sent[-2])["foods"][0]["description"] == "курт (сушёный сыр), 5 шт"


async def test_record_with_an_unknown_word_keeps_save_cancel_first(llm, settings, db, find):
    llm.answers = [BURSAK_WITH_TEA]
    find.default = [BURSAK]
    msg = await send(BURSAK_TEXT, llm, settings, db)
    token = token_of(msg)  # the first button is still "Сохранить"
    assert token in log_text.PENDING and token in log_text.LOOKUPS
    kb = reply_markup(msg)
    assert labels(kb)[0] == ["✅ Сохранить", "✖ Отмена"]
    assert data(kb)[1:] == [[f"lookup:{token}:0"], [f"lookup:{token}:other"]]
    assert labels(kb)[2] == ["Другое"]
    text = msg.answer.await_args.args[0]
    assert text.startswith("Записать еду?") and "• чай 200 г" in text
    assert "Не знаю «бурсак». Варианты на 2 шт:" in text
    assert "«бурсак» — это что?" not in text  # replaced by the variants
    assert log_text.PENDING[token].result.foods[0].description == "чай"  # nothing was added yet

    cb = await pick(token, 0)
    result = log_text.PENDING[token].result
    assert [f.description for f in result.foods] == ["чай", "бурсак (жареное тесто), 2 шт"]
    assert result.foods[1].grams == 60
    assert result.note == "бурсак (жареное тесто): оценка по описанию"
    assert result.clarification is None and result.unknown_terms == []
    assert log_text.PENDING[token].raw_text == BURSAK_TEXT
    shown = cb.message.edit_text.await_args.args[0]
    assert "бурсак (жареное тесто), 2 шт" in shown and "оценка по описанию" in shown and "Не знаю" not in shown
    assert labels(edited_markup(cb)) == [["✅ Сохранить", "✖ Отмена"]]
    assert token not in log_text.LOOKUPS


async def test_variant_replaces_the_word_the_model_also_put_in_foods(llm, settings, db, find):
    made_up = {"description": "курт", "grams": 100, "kcal": 500, "protein_g": 30, "fat_g": 30, "carbs_g": 20}
    llm.answers = [{**UNCLEAR_KURT, "kind": "food", "foods": [TEA, made_up]}]
    find.default = [KURT]
    msg = await send("чай и 3 курта", llm, settings, db)
    token = token_of(msg)
    await pick(token, 0)
    foods = log_text.PENDING[token].result.foods
    assert [f.description for f in foods] == ["чай", "курт (сушёный сыр), 3 шт"]
    assert foods[1].grams == 75 and foods[1].kcal == pytest.approx(3 * KURT.kcal, abs=1)


async def test_other_only_shows_a_hint(llm, settings, db, find):
    llm.answers = [BURSAK_WITH_TEA]
    find.default = [BURSAK]
    msg = await send(BURSAK_TEXT, llm, settings, db)
    token = token_of(msg)
    before = log_text.PENDING[token].result
    cb = await pick(token, "other")
    cb.answer.assert_awaited_once_with(log_text.OTHER_HINT, show_alert=True)
    cb.message.edit_text.assert_not_awaited()
    assert token in log_text.LOOKUPS and log_text.PENDING[token].result is before


async def test_other_on_an_unclear_message_keeps_the_variants(llm, settings, db, find):
    llm.answers = [UNCLEAR_KURT]
    find.default = [KURT]
    msg = await send("курт", llm, settings, db)
    token = lookup_token(msg)
    cb = await pick(token, "other")
    cb.answer.assert_awaited_once_with(log_text.OTHER_HINT, show_alert=True)
    assert token in log_text.LOOKUPS
    cb = await pick(token, 0)  # still works after "Другое"
    assert token in log_text.PENDING and cb.message.edit_text.await_count == 1


async def test_someone_elses_tap_changes_nothing(llm, settings, db, find):
    llm.answers = [BURSAK_WITH_TEA]
    find.default = [BURSAK]
    msg = await send(BURSAK_TEXT, llm, settings, db)
    token = token_of(msg)
    before = log_text.PENDING[token].result
    cb = await pick(token, 0, user_id=7)
    assert stale_alert(cb)
    cb.message.edit_text.assert_not_awaited()
    assert log_text.PENDING[token].result is before and token in log_text.LOOKUPS
    cb = await pick(token, 0)  # the owner can still pick
    assert len(log_text.PENDING[token].result.foods) == 2


async def test_double_tap_adds_the_variant_once(llm, settings, db, find):
    llm.answers = [BURSAK_WITH_TEA]
    find.default = [BURSAK, food_lookup.Option(name="бурсак (казахский)", portion_g=40, kcal=150, protein_g=3,
                                                fat_g=8, carbs_g=16)]  # fmt: skip
    msg = await send(BURSAK_TEXT, llm, settings, db)
    token = token_of(msg)
    first = await pick(token, 0)
    second = await pick(token, 1)
    assert stale_alert(second)
    first.message.edit_text.assert_awaited_once()
    second.message.edit_text.assert_not_awaited()
    foods = log_text.PENDING[token].result.foods
    assert [f.description for f in foods] == ["чай", "бурсак (жареное тесто), 2 шт"]


async def test_tap_after_cancel_is_stale(llm, settings, db, find):
    llm.answers = [BURSAK_WITH_TEA]
    find.default = [BURSAK]
    msg = await send(BURSAK_TEXT, llm, settings, db)
    token = token_of(msg)
    await log_text.drop(callback(f"drop:{token}"))
    assert token not in log_text.LOOKUPS and token not in log_text.PENDING
    cb = await pick(token, 0)
    assert stale_alert(cb)
    assert token not in log_text.PENDING  # a stale tap must not resurrect the record
    cb.message.edit_text.assert_not_awaited()


async def test_tap_after_save_is_stale(llm, settings, db, find):
    llm.answers = [BURSAK_WITH_TEA]
    find.default = [BURSAK]
    msg = await send(BURSAK_TEXT, llm, settings, db)
    token = token_of(msg)
    await log_text.save(callback(f"save:{token}"), settings, db)
    assert token not in log_text.LOOKUPS
    cb = await pick(token, 0)
    assert stale_alert(cb)
    async with db() as s:
        rows = (await s.scalars(select(FoodEntry))).all()
    assert [r.description for r in rows] == ["чай"]  # the variant was not added after saving


async def test_cancel_of_an_unclear_message_ends_it(llm, settings, db, find):
    llm.answers = [UNCLEAR_KURT]
    find.default = [KURT]
    msg = await send("курт", llm, settings, db)
    token = lookup_token(msg)
    cb = callback(f"drop:{token}")
    await log_text.drop(cb)
    cb.message.edit_text.assert_awaited_once_with("Отменено.")
    assert token not in log_text.LOOKUPS and USER not in log_text.CONTEXT
    assert stale_alert(await pick(token, 0))
    assert not log_text.PENDING


async def test_tap_after_a_newer_message_to_an_unclear_one_is_stale(llm, settings, db, find):
    llm.answers = [UNCLEAR_KURT, {"kind": "unknown", "clarification": "Сколько штук?"}]
    find.default = [KURT]
    msg = await send("курт", llm, settings, db)
    await send("ну эти", llm, settings, db, T0 + timedelta(minutes=1))
    cb = await pick(lookup_token(msg), 0)
    assert stale_alert(cb)
    assert not log_text.PENDING


async def test_revision_makes_the_old_variants_stale(llm, settings, db, find):
    llm.answers = [BURSAK_WITH_TEA, {"kind": "food", "revises": True, "foods": [TEA, {**TEA, "description": "кофе"}]}]
    find.default = [BURSAK]
    first = await send(BURSAK_TEXT, llm, settings, db)
    old = token_of(first)
    second = await send("и кофе, без бурсаков", llm, settings, db, T0 + timedelta(minutes=1))
    assert old not in log_text.LOOKUPS and old not in log_text.PENDING
    assert not log_text.LOOKUPS  # the new preview has no unknown words, so no variants
    assert buttons(second) == [["save", "drop"]]
    assert stale_alert(await pick(old, 0))
    assert [f.description for f in log_text.PENDING[token_of(second)].result.foods] == ["чай", "кофе"]


async def test_revision_that_still_has_the_unknown_word_gets_fresh_variants(llm, settings, db, find):
    again = {**BURSAK_WITH_TEA, "revises": True}
    llm.answers = [BURSAK_WITH_TEA, again]
    find.default = [BURSAK]
    first = await send(BURSAK_TEXT, llm, settings, db)
    second = await send("нет, три бурсака", llm, settings, db, T0 + timedelta(minutes=1))
    old, new = token_of(first), token_of(second)
    assert old != new and old not in log_text.LOOKUPS and new in log_text.LOOKUPS
    assert stale_alert(await pick(old, 0))
    await pick(new, 0)
    assert log_text.PENDING[new].result.foods[-1].description == "бурсак (жареное тесто), 3 шт"


# ---- no variants: exactly the old behaviour ----


async def test_no_variants_for_a_record_is_the_old_preview(llm, settings, db, find):
    llm.answers = [BURSAK_WITH_TEA]
    find.default = []
    msg = await send(BURSAK_TEXT, llm, settings, db)
    expected = log_text.render_preview(ParseResult.model_validate(BURSAK_WITH_TEA), source_text=BURSAK_TEXT)
    assert msg.answer.await_args.args[0] == expected
    assert "Уточни: «бурсак» — это что?" in expected
    assert buttons(msg) == [["save", "drop"]]
    assert not log_text.LOOKUPS and token_of(msg) in log_text.PENDING


async def test_no_variants_for_an_unclear_message_is_the_models_question(llm, settings, db, find):
    llm.answers = [UNCLEAR_KURT]
    find.default = []
    msg = await send("съел 5 курт", llm, settings, db)
    msg.answer.assert_awaited_once_with("«курт» — это что?")  # plain text, no keyboard
    assert not log_text.LOOKUPS and not log_text.PENDING
    assert find.terms() == ["курт"]


async def test_kind_unknown_with_unknown_terms_gets_variants_too(llm, settings, db, find):
    llm.answers = [{"kind": "unknown", "clarification": "«курт» — это что?", "unknown_terms": ["курт"]}]
    find.default = [KURT]
    msg = await send("курт", llm, settings, db)
    assert "Не знаю «курт»" in msg.answer.await_args.args[0]
    assert lookup_token(msg) in log_text.LOOKUPS


async def test_lookup_is_not_called_without_unknown_terms(llm, settings, db, find):
    llm.answers = [food(1), {"kind": "unknown", "clarification": "Сколько штук?"}]
    find.default = [KURT]
    await send("самса", llm, settings, db)
    await send("ну эту", llm, settings, db, T0 + timedelta(minutes=1))
    assert find.calls == [] and not log_text.LOOKUPS


async def test_lookup_is_not_called_for_questions_and_workouts(llm, settings, db, find):
    llm.answers = [
        {"kind": "question", "clarification": "В курте около 260 ккал.", "unknown_terms": ["курт"]},
        {**workout("жим лёжа"), "unknown_terms": ["курт"]},
    ]
    find.default = [KURT]
    await send("сколько ккал в курте?", llm, settings, db)
    await send("жим 3х10 на 60", llm, settings, db, T0 + timedelta(minutes=1))
    assert find.calls == [] and not log_text.LOOKUPS


async def test_question_about_a_pending_record_gets_no_variants(llm, settings, db, find):
    llm.answers = [food(1), {"kind": "unknown", "clarification": "«курт» — это что?", "unknown_terms": ["курт"]}]
    find.default = [KURT]
    first = await send("самса", llm, settings, db)
    second = await send("и курт", llm, settings, db, T0 + timedelta(minutes=1))
    assert find.calls == [] and not log_text.LOOKUPS  # variants would edit another preview
    assert second.answer.await_args.args[0] == "«курт» — это что?"
    assert token_of(first) in log_text.PENDING and log_text.CONTEXT[USER].token == token_of(first)


async def test_at_most_two_words_are_looked_up(llm, settings, db, find):
    answer = {**BURSAK_WITH_TEA, "unknown_terms": ["курт", "кутаб", "бурсак"], "clarification": "что это?"}
    llm.answers = [answer]
    find.by_term = {"курт": [KURT], "кутаб": [KUTAB]}
    msg = await send("чай, курт, кутаб, бурсак", llm, settings, db)
    assert find.terms() == ["курт", "кутаб"]
    text = msg.answer.await_args.args[0]
    assert "Не знаю «курт»" in text and "Не знаю «кутаб»" in text and "Не знаю «бурсак»" not in text
    assert "Уточни: «бурсак» — это что?" in text  # the rest stays a question
    assert "что это?" not in text  # the model's own wording is replaced
    token = token_of(msg)
    assert data(reply_markup(msg))[1:] == [
        [f"lookup:{token}:0"], [f"lookup:{token}:1"], [f"lookup:{token}:other"],
    ]  # fmt: skip


async def test_word_without_options_stays_a_question_when_another_has_them(llm, settings, db, find):
    answer = {**BURSAK_WITH_TEA, "unknown_terms": ["курт", "кутаб"], "clarification": "что это?"}
    llm.answers = [answer]
    find.by_term = {"курт": [], "кутаб": [KUTAB]}
    msg = await send("чай, курт, кутаб", llm, settings, db)
    text = msg.answer.await_args.args[0]
    assert "Не знаю «кутаб»" in text and "Не знаю «курт»" not in text
    assert "Уточни: «курт» — это что?" in text
    token = token_of(msg)
    await pick(token, 0)
    result = log_text.PENDING[token].result
    assert result.unknown_terms == ["курт"] and result.clarification == "«курт» — это что?"
    assert token not in log_text.LOOKUPS  # nothing left to pick


async def test_two_words_are_resolved_one_tap_at_a_time(llm, settings, db, find):
    answer = {**BURSAK_WITH_TEA, "unknown_terms": ["курт", "кутаб"]}
    llm.answers = [answer]
    find.by_term = {"курт": [KURT, KURT_SMALL], "кутаб": [KUTAB]}
    msg = await send("чай, курт, кутаб", llm, settings, db)
    token = token_of(msg)

    cb = await pick(token, 0)
    assert token in log_text.LOOKUPS  # кутаб is still open
    shown = cb.message.edit_text.await_args.args[0]
    assert "Не знаю «кутаб»" in shown and "Не знаю «курт»" not in shown
    assert data(edited_markup(cb)) == [
        [f"save:{token}", f"drop:{token}"], [f"lookup:{token}:2"], [f"lookup:{token}:other"],
    ]  # the numbers of the buttons are kept
    assert [f.description for f in log_text.PENDING[token].result.foods] == ["чай", "курт (сушёный сыр)"]

    again = await pick(token, 1)  # another variant for the word that is already resolved
    again.answer.assert_awaited_once_with("Для «курт» вариант уже выбран.")
    again.message.edit_text.assert_not_awaited()
    assert len(log_text.PENDING[token].result.foods) == 2

    await pick(token, 2)
    assert [f.description for f in log_text.PENDING[token].result.foods] == [
        "чай", "курт (сушёный сыр)", "кутаб (лепёшка с зеленью)",
    ]  # fmt: skip
    assert token not in log_text.LOOKUPS


async def test_bad_choice_is_ignored(llm, settings, db, find):
    llm.answers = [BURSAK_WITH_TEA]
    find.default = [BURSAK]
    msg = await send(BURSAK_TEXT, llm, settings, db)
    token = token_of(msg)
    for choice in ("9", "x", ""):
        cb = await pick(token, choice)
        cb.answer.assert_awaited_once_with()
        cb.message.edit_text.assert_not_awaited()
    assert token in log_text.LOOKUPS and len(log_text.PENDING[token].result.foods) == 1


async def test_lookup_failure_never_costs_the_preview(llm, settings, db, find):
    llm.answers = [BURSAK_WITH_TEA]
    find.default = RuntimeError("boom")
    msg = await send(BURSAK_TEXT, llm, settings, db)  # no exception
    expected = log_text.render_preview(ParseResult.model_validate(BURSAK_WITH_TEA), source_text=BURSAK_TEXT)
    assert msg.answer.await_args.args[0] == expected
    assert buttons(msg) == [["save", "drop"]] and not log_text.LOOKUPS


async def test_lookup_failure_for_an_unclear_message_keeps_the_question(llm, settings, db, find):
    llm.answers = [UNCLEAR_KURT]
    find.default = RuntimeError("boom")
    msg = await send("курт", llm, settings, db)
    msg.answer.assert_awaited_once_with("«курт» — это что?")
    assert not log_text.LOOKUPS


async def test_one_failing_word_drops_all_variants_but_not_the_preview(llm, settings, db, find):
    answer = {**BURSAK_WITH_TEA, "unknown_terms": ["курт", "кутаб"]}
    llm.answers = [answer]
    find.by_term = {"курт": [KURT], "кутаб": RuntimeError("boom")}
    msg = await send("чай, курт, кутаб", llm, settings, db)
    assert buttons(msg) == [["save", "drop"]] and not log_text.LOOKUPS
    assert "Уточни:" in msg.answer.await_args.args[0]


async def test_variants_keep_the_voice_prefix(llm, settings, db, find):
    prefix = "Распознал: «съел курт»\n\n"
    llm.answers = [UNCLEAR_KURT]
    find.default = [KURT]
    msg = message("съел курт")
    await log_text.process_text(msg, "съел курт", settings, db, llm.client, raw_text="[voice] съел курт", prefix=prefix)
    assert msg.answer.await_args.args[0].startswith(prefix + "Не знаю «курт»")
    token = lookup_token(msg)
    cb = await pick(token, 0)
    assert cb.message.edit_text.await_args.args[0].startswith(prefix + "Записать еду?")
    assert log_text.PENDING[token].raw_text == "[voice] съел курт"


async def test_variants_with_an_offered_fact_keep_the_remember_row(llm, settings, db, find):
    llm.answers = [{**BURSAK_WITH_TEA, "remember": "бурсак ~30 г"}]
    find.default = [BURSAK]
    msg = await send(BURSAK_TEXT, llm, settings, db)
    token = token_of(msg)
    assert data(reply_markup(msg))[-1] == [f"remember:{token}"]
    assert "Запомнить: «бурсак ~30 г»" in msg.answer.await_args.args[0]
    cb = await pick(token, 0)
    assert "Запомнить: «бурсак ~30 г»" in cb.message.edit_text.await_args.args[0]
    assert data(edited_markup(cb))[-1] == [f"remember:{token}"]


async def test_remember_tap_keeps_the_open_variants(llm, settings, db, find):
    llm.answers = [{**BURSAK_WITH_TEA, "remember": "бурсак ~30 г"}]
    find.default = [BURSAK]
    msg = await send(BURSAK_TEXT, llm, settings, db)
    token = token_of(msg)
    cb = callback(f"remember:{token}")
    await log_text.remember(cb, db)
    rows = data(cb.message.edit_reply_markup.await_args.kwargs["reply_markup"])
    assert rows[0] == [f"save:{token}", f"drop:{token}"]
    assert [f"lookup:{token}:0"] in rows and [f"lookup:{token}:other"] in rows
    assert [f"remember:{token}"] not in rows


# ---- questions answered from the diary (gymbot.services.answer) ----


async def test_question_is_answered_from_the_diary(llm, settings, db, diary):
    llm.answers = [{"kind": "question", "clarification": "Не знаю."}, "Сегодня жим лёжа 3×8–12, начни с 40 кг."]
    msg = await send("что у меня сегодня и с каким весом?", llm, settings, db)
    assert msg.answer.await_args.args[0] == "Сегодня жим лёжа 3×8–12, начни с 40 кг."
    system, *rest = llm.bodies[-1]["messages"]
    assert "Сводка:" in system["content"] and "План на сегодня" in system["content"]
    assert "response_format" not in llm.bodies[-1]
    assert [m["content"] for m in rest] == ["что у меня сегодня и с каким весом?"]


async def test_next_question_sees_previous_answers(llm, settings, db, diary):
    llm.answers = [
        {"kind": "question", "clarification": "-"}, "Нужен вес и повторы.",
        {"kind": "question", "clarification": "-"}, "Ок.",
    ]
    await send("какая тебе нужна информация?", llm, settings, db)
    await send("а по жиму?", llm, settings, db, T0 + timedelta(minutes=1))
    turns = [(m["role"], m["content"]) for m in llm.bodies[-1]["messages"][1:]]
    assert turns == [
        ("user", "какая тебе нужна информация?"), ("assistant", "Нужен вес и повторы."), ("user", "а по жиму?"),
    ]
    parser = [m["content"] for m in llm.bodies[-2]["messages"]]  # the parser saw the question too
    assert parser[-3] == "какая тебе нужна информация?" and "Нужен вес и повторы." in parser[-2]


async def test_old_answers_are_forgotten(llm, settings, db, diary):
    llm.answers = [{"kind": "question", "clarification": "-"}, "Раз.", {"kind": "question", "clarification": "-"}, "Два."]
    await send("первый вопрос?", llm, settings, db)
    await send("второй вопрос?", llm, settings, db, T0 + log_text.CONTEXT_TTL + timedelta(minutes=1))
    assert len(llm.bodies[-1]["messages"]) == 2  # system + the question


async def test_diary_answer_failure_keeps_the_parsers_answer(llm, settings, db, diary):
    llm.answers = [{"kind": "question", "clarification": "Около 250 ккал на 100 г."}, "", "", ""]  # no route answers
    msg = await send("сколько калорий в шашлыке?", llm, settings, db)
    assert msg.answer.await_args.args[0] == "Около 250 ккал на 100 г."
    assert log_text.QA == {}


async def test_records_do_not_go_to_the_diary_answer(llm, settings, db, diary):
    llm.answers = [food(1)]
    await send("самса", llm, settings, db)
    assert len(llm.bodies) == 1


async def test_question_about_a_pending_preview_keeps_the_parsers_answer(llm, settings, db, diary):
    llm.answers = [food(1), {"kind": "question", "clarification": "Около 300 ккал."}]
    await send("самса", llm, settings, db)
    msg = await send("а сколько в ней калорий?", llm, settings, db, T0 + timedelta(minutes=1))
    assert len(llm.bodies) == 2 and msg.answer.await_args.args[0] == "Около 300 ккал."


async def test_small_talk_gets_no_diary_answer(llm, settings, db, diary):
    llm.answers = [{"kind": "question", "clarification": "Привет! Пиши, что съел или сделал."}]
    msg = await send("привет", llm, settings, db)
    assert len(llm.bodies) == 1 and msg.answer.await_args.args[0].startswith("Привет!")


async def test_diary_answer_starts_the_program_and_shows_todays_plan(llm, settings, db, diary):
    from gymbot.db.models import UserProgram

    llm.answers = [{"kind": "question", "clarification": "-"}, "План: см. выше."]
    await send("что у меня сегодня по тренировке?", llm, settings, db)
    async with db() as s:
        assert await s.scalar(select(UserProgram)) is not None  # like /plan on first use
    system = llm.bodies[-1]["messages"][0]["content"]
    assert "Программа «" in system and "План на сегодня" in system


# ---- a Mini App setup request is not a record ----

SETUP_TEXT = "Запиши тогда в мини-ап мою программу, чтобы я мог прийти и запустить тренировку"


async def test_miniapp_setup_request_is_not_a_workout(llm, settings, db):
    from gymbot.llm.prompts import MINIAPP_SETUP_ANSWER

    # The parser (wrongly) turned the request into sets with weights it remembered.
    llm.answers = [{**workout("жим лёжа"), "exercises": [
        {"exercise": "жим лёжа", "sets": [{"reps": 8, "weight_kg": 90}] * 3}]}]
    msg = await send(SETUP_TEXT, llm, settings, db)
    assert buttons(msg) == [] and not log_text.PENDING
    assert USER not in log_text.CONTEXT or log_text.CONTEXT[USER].token is None
    assert MINIAPP_SETUP_ANSWER in msg.answer.await_args.args[0]
    assert "Записать" not in msg.answer.await_args.args[0]


async def test_miniapp_setup_request_goes_to_the_diary_answer(llm, settings, db, diary):
    from gymbot.llm.prompts import MINIAPP_SETUP_ANSWER

    llm.answers = [workout("жим лёжа"), "Сегодня жим лёжа 3×8, начни с 60 кг."]
    msg = await send(SETUP_TEXT, llm, settings, db)
    assert len(llm.bodies) == 2  # the parser, then the diary answer
    assert msg.answer.await_args.args[0] == "Сегодня жим лёжа 3×8, начни с 60 кг."
    assert msg.answer.await_args.args[0] != MINIAPP_SETUP_ANSWER
    assert buttons(msg) == [] and not log_text.PENDING


async def test_setup_guard_does_not_fire_for_a_workout_with_numbers(llm, settings, db):
    llm.answers = [workout("жим лёжа")]
    msg = await send("запиши жим лёжа 3 по 10 на 60", llm, settings, db)
    assert msg.answer.await_args.args[0].startswith("Записать")
    assert buttons(msg) == [["save", "drop"]]
    assert log_text.PENDING[token_of(msg)].result.kind == "workout"


async def test_setup_guard_does_not_fire_for_a_workout_that_says_the_program_weight(llm, settings, db):
    llm.answers = [workout("жим лёжа")]
    msg = await send("запиши жим лёжа три по десять с весом как в программе", llm, settings, db)
    assert msg.answer.await_args.args[0].startswith("Записать")
    assert buttons(msg) == [["save", "drop"]]
    assert log_text.PENDING[token_of(msg)].result.kind == "workout"


def test_setup_request_pattern():
    wk = ParseResult.model_validate(workout("жим лёжа"))
    for text in (
        SETUP_TEXT,
        "запиши в мини-ап мою программу и выставь рабочие веса на сегодня",
        "выставь веса на сегодня",
        "поставь рабочие веса",
        "Запиши тогда в мини-ап мою программу, чтобы я мог сейчас просто в зал прийти и запустить тренировку",
        "выстави рабочие веса на сегодня",
    ):
        assert log_text.is_setup_request(text, wk), text
    assert not log_text.is_setup_request("запиши жим лёжа 3 по 10 на 60", wk)  # digits
    assert not log_text.is_setup_request("жим лёжа три по десять", wk)  # no request
    assert not log_text.is_setup_request(SETUP_TEXT, ParseResult.model_validate(food(1)))  # not a workout


@pytest.mark.parametrize(
    "text",
    [
        "поставил рекорд в жиме, всё по программе",  # a done verb, not an imperative
        "запиши тренировку по программе, всё сделал",  # "сделал"
        "запиши жим лёжа три по десять с весом как в программе",  # number words
        "запиши приседания три подхода по восемь с весом шестьдесят, остальное по программе",
        "запиши в мини-ап пятьдесят на восемь",
        "запиши в приложение двести на раз",
        "запиши в приложение тренировку, всё сделал",
        "запиши в приложение жим лёжа",  # names the parsed exercise
    ],
)
def test_setup_guard_stays_quiet_for_a_workout_the_user_states(text):
    wk = ParseResult.model_validate(workout("жим лёжа"))
    assert wk.kind == "workout"
    assert not log_text.is_setup_request(text, wk), text


# ---- a reply that is not a record never claims a write (ACTION_CLAIM, NO_ACTION) ----


@pytest.mark.parametrize(
    "reply",
    [
        "Записал твой подход ✅",
        "записал",
        "Записано: жим 80×8",
        "Записала!",
        "Добавил подход в дневник",
        "Добавлен подход",
        "Добавила в дневник",
        "Сохранил 3 подхода",
        "Сохранено ✅",
        "Сохранила",
        "Обновил рабочий вес",
        "ОК, ЗАПИСАЛ",  # case-insensitive
        "Ты не записал, а я записал",  # the second one is the bot's own claim
    ],
)
def test_claims_action_detects_a_claimed_write(reply):
    assert log_text.claims_action(reply) is True, reply


@pytest.mark.parametrize(
    "reply",
    [
        None,
        "",
        "Около 300 ккал.",
        "Подходы запишешь после зала.",  # a future action of the user
        "Ты записал 3 подхода, они в мини-аппе.",  # what the user did
        "ты добавил подход в мини-апп",
        "Вы записали жим в мини-аппе, а я вижу его в сводке.",
        "Ты сохранил тренировку? Тогда она в истории.",
        MINIAPP_SETUP_ANSWER,
    ],
)
def test_claims_action_ignores_honest_replies(reply):
    assert log_text.claims_action(reply) is False, reply


def test_the_no_action_replacement_is_not_a_claim_itself():
    assert log_text.claims_action(log_text.NO_ACTION) is False


def test_claims_action_never_flags_the_miniapp_setup_answer_even_with_a_claim_word():
    assert log_text.claims_action(MINIAPP_SETUP_ANSWER + " Записал.") is True  # only the exact text is exempt
    assert log_text.claims_action(MINIAPP_SETUP_ANSWER) is False


@pytest.mark.parametrize("kind", ["question", "unknown"])
def test_honest_replaces_a_claim_in_a_non_record(kind):
    result = ParseResult.model_validate({"kind": kind, "clarification": "Записал твой подход ✅"})
    fixed = log_text.honest(result)
    assert fixed.kind == kind and fixed.clarification == log_text.NO_ACTION
    assert result.clarification == "Записал твой подход ✅"  # the original is not mutated


def test_honest_keeps_honest_replies_and_records():
    question = ParseResult.model_validate({"kind": "question", "clarification": "Около 300 ккал."})
    assert log_text.honest(question) is question
    setup = ParseResult(kind="question", clarification=MINIAPP_SETUP_ANSWER)
    assert log_text.honest(setup) is setup
    record = ParseResult.model_validate({**workout("жим лёжа"), "clarification": "Записал? Уточни вес."})
    assert log_text.honest(record) is record  # a real record keeps its note, the preview says "Записать?"
    assert log_text.honest(ParseResult.model_validate({"kind": "unknown"})).clarification is None


async def test_parser_reply_claiming_a_write_becomes_no_action(llm, settings, db):
    llm.answers = [{"kind": "question", "clarification": "Записал твой подход ✅"}]
    msg = await send("жим 80×8", llm, settings, db)
    assert msg.answer.await_args.args[0] == log_text.NO_ACTION
    assert "reply_markup" not in msg.answer.await_args.kwargs and not log_text.PENDING


async def test_unknown_reply_claiming_a_write_becomes_no_action(llm, settings, db):
    llm.answers = [{"kind": "unknown", "clarification": "Сохранил, всё в дневнике"}]
    msg = await send("ммм ну это", llm, settings, db)
    assert msg.answer.await_args.args[0] == log_text.NO_ACTION


async def test_diary_answer_claiming_a_write_becomes_no_action(llm, settings, db, diary):
    # The parser's claim sends a short text without "?" to the diary answer; that one claims a write too.
    llm.answers = [{"kind": "question", "clarification": "Записал твой подход ✅"}, "Добавил подход в дневник."]
    msg = await send("жим 80×8", llm, settings, db)
    assert len(llm.bodies) == 2  # the parser, then the diary answer
    assert msg.answer.await_args.args[0] == log_text.NO_ACTION
    assert log_text.QA[USER][0][1] == log_text.NO_ACTION  # the next question sees the corrected reply
    assert not log_text.PENDING


async def test_honest_diary_answer_replaces_the_parsers_claim_and_stays(llm, settings, db, diary):
    answer = "В мини-аппе отмечено 2 подхода жима лёжа."
    llm.answers = [{"kind": "question", "clarification": "Записал твой подход ✅"}, answer]
    msg = await send("жим 80×8", llm, settings, db)
    assert len(llm.bodies) == 2
    assert msg.answer.await_args.args[0] == answer


async def test_claim_inside_a_long_question_diary_answer_is_replaced(llm, settings, db, diary):
    llm.answers = [{"kind": "question", "clarification": "-"}, "Сохранил твой вес, теперь он 80 кг."]
    msg = await send("какой у меня сейчас рабочий вес в жиме?", llm, settings, db)
    assert msg.answer.await_args.args[0] == log_text.NO_ACTION


async def test_miniapp_setup_answer_is_not_replaced_by_no_action(llm, settings, db):
    llm.answers = [{"kind": "question", "clarification": MINIAPP_SETUP_ANSWER}]
    msg = await send("выставь рабочие веса", llm, settings, db)
    assert msg.answer.await_args.args[0] == MINIAPP_SETUP_ANSWER


# ---- wants_diary: questions about what the bot sees in the Mini App ----


@pytest.mark.parametrize(
    "text",
    ["тебе видно", "видишь?", "Видишь", "ты видишь", "я вижу", "в мини-аппе", "мини-апп", "миниапп", "Mini App", "mini app"],
)
def test_wants_diary_for_short_visibility_questions(text):
    assert len(text.split()) <= log_text.SMALL_TALK_WORDS  # short: only the visibility words make it a question
    assert log_text.wants_diary(text) is True


@pytest.mark.parametrize("text", ["привет", "спасибо", "ок", "понял", "жим 80×8"])
def test_wants_diary_is_false_for_small_talk(text):
    assert log_text.wants_diary(text) is False


def test_wants_diary_for_questions_and_long_texts_as_before():
    assert log_text.wants_diary("что у меня сегодня?") is True
    assert log_text.wants_diary("расскажи про мой прогресс по жиму") is True  # more than SMALL_TALK_WORDS


@pytest.mark.parametrize("text", ["тебе видно", "видишь"])
async def test_visibility_question_goes_to_the_diary_answer(llm, settings, db, diary, text):
    answer = "Вижу в мини-аппе: жим лёжа 82.5×8, 82.5×8 (2 из 4 подходов)."
    llm.answers = [{"kind": "question", "clarification": "Пока нет."}, answer]
    msg = await send(text, llm, settings, db)
    assert len(llm.bodies) == 2  # the parser, then the diary answer
    assert msg.answer.await_args.args[0] == answer


# ---- workout preview warns about sets already ticked in the Mini App ----

NOTE = (
    "В мини-аппе уже отмечено: жим лёжа 82.5×8 ×2 — если это те же подходы, не сохраняй, "
    "они попадут в историю по «Завершить»."
)
BENCH_PREVIEW = "Записать?\n• жим лёжа: 60 кг × 10"


async def put_snapshot(db, *, updated_ago: timedelta = timedelta(), done: bool = True, started_ago=timedelta(minutes=30)):
    """The user's workout in progress, relative to the real clock (the overlap check uses datetime.now)."""
    now = datetime.now(UTC)
    sets = [{"weight": 82.5, "reps": 8 if done else None, "done": done} for _ in range(2)]
    body = WorkoutIn.model_validate({
        "id": "w1", "programId": "", "week": 1, "weekday": 3, "startedAt": now - started_ago,
        "exercises": [
            {"name": "жим лёжа", "target": "4х6-8", "sets": sets},
            {"name": "присед", "sets": [{"weight": 100, "reps": 5, "done": True}]},
        ],
    })
    async with db() as s:
        user = await s.scalar(select(User).where(User.telegram_id == USER))
        if user is None:
            user = User(telegram_id=USER, rest_seconds=90)
            s.add(user)
            await s.flush()
        s.add(ActiveWorkout(
            user_id=user.id, client_id="w1", payload=body.model_dump_json(), updated_at=now - updated_ago
        ))
        await s.commit()


async def test_workout_preview_warns_about_sets_already_in_the_miniapp(llm, settings, db):
    await put_snapshot(db)
    llm.answers = [workout("жим лёжа")]
    msg = await send("жим лёжа 10 на 60", llm, settings, db)
    assert msg.answer.await_args.args[0] == f"{BENCH_PREVIEW}\n\n{NOTE}"
    assert buttons(msg) == [["save", "drop"]]  # the user still decides
    assert "присед" not in msg.answer.await_args.args[0]  # only the exercises of the preview


async def test_overlap_note_goes_before_the_fact_offer(llm, settings, db):
    await put_snapshot(db)
    llm.answers = [with_fact(workout("жим лёжа"), "жим лёжа делаю с паузой")]
    msg = await send("жим лёжа 10 на 60, обычно с паузой", llm, settings, db)
    assert msg.answer.await_args.args[0] == f"{BENCH_PREVIEW}\n\n{NOTE}\n\nЗапомнить: «жим лёжа делаю с паузой»"
    assert buttons(msg) == [["save", "drop"], ["remember"]]


async def test_workout_preview_without_a_snapshot_has_no_note(llm, settings, db):
    llm.answers = [workout("жим лёжа")]
    msg = await send("жим лёжа 10 на 60", llm, settings, db)
    assert msg.answer.await_args.args[0] == BENCH_PREVIEW


async def test_workout_preview_for_another_exercise_has_no_note(llm, settings, db):
    await put_snapshot(db)
    llm.answers = [workout("становая тяга")]
    msg = await send("становая 10 на 60", llm, settings, db)
    assert msg.answer.await_args.args[0] == "Записать?\n• становая тяга: 60 кг × 10"


async def test_workout_preview_ignores_a_snapshot_with_nothing_ticked(llm, settings, db):
    await put_snapshot(db, done=False)
    llm.answers = [workout("жим лёжа")]
    msg = await send("жим лёжа 10 на 60", llm, settings, db)
    assert msg.answer.await_args.args[0] == BENCH_PREVIEW


async def test_workout_preview_ignores_a_stale_snapshot(llm, settings, db):
    await put_snapshot(db, updated_ago=timedelta(hours=7))
    llm.answers = [workout("жим лёжа")]
    msg = await send("жим лёжа 10 на 60", llm, settings, db)
    assert msg.answer.await_args.args[0] == BENCH_PREVIEW


async def test_food_preview_has_no_workout_note(llm, settings, db):
    await put_snapshot(db)
    llm.answers = [food(1, "жим лёжа")]
    msg = await send("жим лёжа", llm, settings, db)
    assert "В мини-аппе" not in msg.answer.await_args.args[0]


async def test_overlap_check_failure_keeps_the_preview(llm, settings, db, monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(log_text.aw, "overlap_for", boom)
    llm.answers = [workout("жим лёжа")]
    msg = await send("жим лёжа 10 на 60", llm, settings, db)
    assert msg.answer.await_args.args[0] == BENCH_PREVIEW and buttons(msg) == [["save", "drop"]]


@pytest.mark.parametrize(
    "reply",
    [
        "Да, вижу: жим 82.5×8 ×2. Ничего не записано, пока не нажмёшь «Завершить».",
        "Последний подход добавлен в 18:40, в мини-аппе 2 из 4",
        "Я ничего не сохранил, это только превью.",
        "Ты уже добавил вес, молодец",
        "Ты вчера записал 3 подхода",
        "к жиму добавилось 5 кг",
    ],
)
def test_honest_diary_replies_are_not_claims(reply):
    assert not log_text.claims_action(reply)


@pytest.mark.parametrize(
    "reply", ["Подход добавлен.", "Записал: жим 80×8.", "Я сохранил твой подход", "Готово, добавил подход в дневник"]
)
def test_bot_claims_are_caught(reply):
    assert log_text.claims_action(reply)


async def test_objection_after_a_diary_answer_goes_to_the_diary(llm, settings, db, diary):
    # The first question is factual: answered from the database (an empty one here), no model call.
    llm.answers = [
        {"kind": "question", "clarification": "-"},
        {"kind": "unknown", "clarification": "Уточни название упражнения."}, "Да, 6 упражнений и 24 подхода, 6530 кг.",
    ]
    first = await send("сколько сегодня по тоннажу?", llm, settings, db)
    assert first.answer.await_args.args[0] == "Сегодня в истории тренировки нет.\nТренировок в истории пока нет."
    assert len(llm.bodies) == 1  # only the parser
    msg = await send("там должно быть 6 упражнений всего 24 подхода", llm, settings, db, T0 + timedelta(minutes=1))
    assert msg.answer.await_args.args[0] == "Да, 6 упражнений и 24 подхода, 6530 кг."
    assert len(llm.bodies) == 3  # the parser, the parser, the model's answer to the objection
    turns = [(m["role"], m["content"]) for m in llm.bodies[-1]["messages"][1:]]
    assert turns[:2] == [
        ("user", "сколько сегодня по тоннажу?"),
        ("assistant", "Сегодня в истории тренировки нет.\nТренировок в истории пока нет."),
    ]


async def test_unclear_message_without_a_recent_answer_stays_unclear(llm, settings, db, diary):
    llm.answers = [{"kind": "unknown", "clarification": "Уточни название упражнения."}]
    msg = await send("шесть по двадцать четыре", llm, settings, db)
    assert len(llm.bodies) == 1 and "Уточни" in msg.answer.await_args.args[0]
