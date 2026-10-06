"""Free-text handler: dialog context between messages, stale previews, no echo."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select

from gymbot.db.models import FoodEntry, User, UserFact, WellbeingEntry
from gymbot.handlers import log_text
from gymbot.llm.openrouter import OpenRouterClient
from gymbot.llm.prompts import EXAMPLES
from gymbot.llm.schemas import ParseResult

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
        content = json.dumps(self.answers.pop(0), ensure_ascii=False)
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
