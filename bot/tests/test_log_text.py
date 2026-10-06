"""Free-text handler: dialog context between messages, stale previews, no echo."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select

from gymbot.db.models import FoodEntry
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
        message=SimpleNamespace(edit_text=AsyncMock()),
        answer=AsyncMock(),
    )


def token_of(msg) -> str:
    kb = msg.answer.await_args.kwargs["reply_markup"]
    return kb.inline_keyboard[0][0].callback_data.split(":", 1)[1]


@pytest.fixture(autouse=True)
def clean_state():
    log_text.PENDING.clear()
    log_text.CONTEXT.clear()
    yield
    log_text.PENDING.clear()
    log_text.CONTEXT.clear()


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
