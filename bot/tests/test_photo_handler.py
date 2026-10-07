"""Food photo handler: size choice, hand-off to the vision model, error answers, preview and follow-up."""

import base64
import io
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from gymbot.handlers import log_text, photo
from gymbot.llm.openrouter import LLMError, OpenRouterClient
from gymbot.llm.schemas import ParseResult

T0 = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
USER = 42
JPEG = b"\xff\xd8\xff\xe0fake-jpeg-bytes\x00\x01"
SAMSA = {"description": "самса", "grams": 250, "kcal": 650, "protein_g": 25, "fat_g": 40, "carbs_g": 45}


def size(side: int, file_id: str | None = None, height: int | None = None):
    return SimpleNamespace(width=side, height=height if height is not None else side, file_id=file_id or f"f{side}")


def sizes(*sides: int):
    return [size(s) for s in sides]


def make_message(caption: str | None = None, photo_sizes=None, image: bytes | None = JPEG):
    return SimpleNamespace(
        photo=photo_sizes if photo_sizes is not None else sizes(90, 320, 800, 1280, 2560),
        caption=caption,
        date=T0,
        from_user=SimpleNamespace(id=USER, full_name="Amir"),
        chat=SimpleNamespace(id=USER),
        answer=AsyncMock(),
        bot=SimpleNamespace(
            send_chat_action=AsyncMock(),
            download=AsyncMock(return_value=None if image is None else io.BytesIO(image)),
        ),
    )


def food_result(*, foods=True, unknown=()) -> ParseResult:
    return ParseResult.model_validate(
        {"kind": "food", "foods": [SAMSA] if foods else [], "unknown_terms": list(unknown)}
    )


@pytest.fixture(autouse=True)
def clean_state():
    for store in (log_text.PENDING, log_text.CONTEXT, log_text.FACTS, log_text.LOOKUPS):
        store.clear()
    yield
    for store in (log_text.PENDING, log_text.CONTEXT, log_text.FACTS, log_text.LOOKUPS):
        store.clear()


@pytest.fixture(autouse=True)
def no_diary_answer(monkeypatch):
    async def parser_answer(message, text, result, *args):
        return result

    monkeypatch.setattr(log_text, "_diary_answer", parser_answer)


# ---- pick_size ----


def test_pick_size_takes_the_largest_within_1280():
    chosen = photo.pick_size(sizes(90, 320, 800, 1280, 2560))
    assert chosen.file_id == "f1280"


def test_pick_size_without_the_huge_one():
    assert photo.pick_size(sizes(90, 320, 800)).file_id == "f800"


def test_pick_size_order_does_not_matter():
    assert photo.pick_size(sizes(2560, 1280, 90, 800)).file_id == "f1280"


def test_pick_size_all_too_big_takes_the_smallest():
    assert photo.pick_size(sizes(2560, 1600, 4000)).file_id == "f1600"


def test_pick_size_uses_the_longer_side():
    wide = size(1280, "wide", height=720)
    tall_big = size(900, "tall", height=1600)  # 1600 > 1280: does not fit
    assert photo.pick_size([size(320), wide, tall_big]).file_id == "wide"


def test_pick_size_single_size():
    assert photo.pick_size(sizes(90)).file_id == "f90"
    assert photo.pick_size(sizes(3000)).file_id == "f3000"


def test_history_text():
    assert photo.history_text("17 штук, 250 г") == "Фото еды. Подпись: 17 штук, 250 г"
    assert photo.history_text("") == "Фото еды"


# ---- log_photo with fakes ----


@pytest.fixture
def reply(monkeypatch):
    fake = AsyncMock()
    monkeypatch.setattr(photo, "reply_with_result", fake)
    return fake


@pytest.fixture
def known(monkeypatch):
    fake = AsyncMock(return_value=["самса ~150 г"])
    monkeypatch.setattr(photo.facts, "prompt_facts", fake)
    return fake


def fake_llm(result=None, exc=None):
    return SimpleNamespace(parse_photo=AsyncMock(return_value=result, side_effect=exc))


async def run(msg, llm, settings, db):
    await photo.log_photo(msg, settings, db, llm)


async def test_photo_goes_to_the_vision_model_and_then_to_the_preview(settings, db, reply, known):
    result = food_result()
    llm = fake_llm(result)
    msg = make_message("  17   штук,\n250 г ")

    await run(msg, llm, settings, db)

    msg.bot.send_chat_action.assert_awaited_once_with(USER, "typing")
    msg.bot.download.assert_awaited_once()
    assert msg.bot.download.await_args.args[0].file_id == "f1280"
    known.assert_awaited_once()
    assert known.await_args.args[1] == USER
    llm.parse_photo.assert_awaited_once_with(
        base64.b64encode(JPEG).decode(), "image/jpeg", "17 штук, 250 г", ["самса ~150 г"]
    )
    reply.assert_awaited_once()
    args, kwargs = reply.await_args
    assert args == (msg, "Фото еды. Подпись: 17 штук, 250 г", result, settings, db, llm)
    assert kwargs == {
        "raw_text": "[photo] 17 штук, 250 г",
        "prefix": photo.PREFIX,
        "known": ["самса ~150 г"],
    }
    msg.answer.assert_not_awaited()


async def test_photo_without_caption(settings, db, reply, known):
    llm = fake_llm(food_result())
    msg = make_message(None)

    await run(msg, llm, settings, db)

    assert llm.parse_photo.await_args.args[2] == ""
    args, kwargs = reply.await_args
    assert args[1] == "Фото еды"
    assert kwargs["raw_text"] == "[photo]"  # no trailing space
    assert kwargs["prefix"] == photo.PREFIX


async def test_whitespace_caption_counts_as_none(settings, db, reply, known):
    llm = fake_llm(food_result())
    await run(make_message(" \n "), llm, settings, db)
    assert llm.parse_photo.await_args.args[2] == ""
    assert reply.await_args.kwargs["raw_text"] == "[photo]"


async def test_only_small_sizes_download_the_largest_that_fits(settings, db, reply, known):
    msg = make_message("x", photo_sizes=sizes(90, 320, 800))
    await run(msg, fake_llm(food_result()), settings, db)
    assert msg.bot.download.await_args.args[0].file_id == "f800"


async def test_no_food_answers_without_a_preview(settings, db, reply, known):
    msg = make_message("кружка")
    await run(msg, fake_llm(food_result(foods=False)), settings, db)
    msg.answer.assert_awaited_once_with(photo.NO_FOOD)
    reply.assert_not_awaited()


async def test_unknown_terms_without_foods_still_get_a_reply(settings, db, reply, known):
    msg = make_message("что-то")
    result = food_result(foods=False, unknown=["гульчатай"])
    await run(msg, fake_llm(result), settings, db)
    reply.assert_awaited_once()
    assert reply.await_args.args[2] is result
    msg.answer.assert_not_awaited()


async def test_llm_error_answers_failed(settings, db, reply, known):
    msg = make_message("x")
    await run(msg, fake_llm(exc=LLMError("all models failed")), settings, db)
    msg.answer.assert_awaited_once_with(photo.FAILED)
    reply.assert_not_awaited()


async def test_failed_download_answers_failed_and_skips_the_model(settings, db, reply, known):
    llm = fake_llm(food_result())
    msg = make_message("x", image=None)
    await run(msg, llm, settings, db)
    msg.answer.assert_awaited_once_with(photo.FAILED)
    llm.parse_photo.assert_not_awaited()
    reply.assert_not_awaited()


async def test_image_is_not_stored_or_sent_to_the_preview(settings, db, reply, known):
    """Only the text description travels on; the bytes (or their base64) are in no reply argument."""
    llm = fake_llm(food_result())
    await run(make_message("x"), llm, settings, db)
    b64 = base64.b64encode(JPEG).decode()
    args, kwargs = reply.await_args
    assert b64 not in repr(args[1]) and b64 not in repr(kwargs)


# ---- end to end: real client over MockTransport, real log_text.reply_with_result ----


class Vision:
    """OpenRouterClient over MockTransport: photo requests get `photo_answer`, text ones queued answers."""

    def __init__(self, settings, photo_answer):
        self.photo_answer = photo_answer
        self.text_answers: list[dict] = []
        self.photo_bodies: list[dict] = []
        self.text_bodies: list[dict] = []
        http = httpx.AsyncClient(transport=httpx.MockTransport(self._handle), timeout=60)
        s = settings.model_copy(update={"openrouter_api_key": "k", "groq_api_key": "", "stt_api_key": ""})
        self.client = OpenRouterClient(s, http)

    def _handle(self, req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        if isinstance(body["messages"][1]["content"], list):
            self.photo_bodies.append(body)
            answer = self.photo_answer
        else:
            self.text_bodies.append(body)
            answer = self.text_answers.pop(0)
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(answer, ensure_ascii=False)}}]})


def preview_token(msg) -> str:
    kb = msg.answer.await_args.kwargs["reply_markup"]
    return kb.inline_keyboard[0][0].callback_data.split(":", 1)[1]


async def test_photo_makes_a_preview_and_a_text_follow_up_replaces_it(settings, db):
    v = Vision(settings, {"foods": [SAMSA], "note": None})
    msg = make_message("17 штук, 250 г")

    await photo.log_photo(msg, settings, db, v.client)

    # The vision model got the picture and the caption.
    assert len(v.photo_bodies) == 1 and v.text_bodies == []
    parts = v.photo_bodies[0]["messages"][1]["content"]
    assert parts[0] == {"type": "text", "text": "Подпись: 17 штук, 250 г"}
    assert parts[1]["image_url"]["url"] == "data:image/jpeg;base64," + base64.b64encode(JPEG).decode()

    # The preview: prefix first, buttons, and a Pending that stores "[photo] <caption>".
    msg.answer.assert_awaited_once()
    text = msg.answer.await_args.args[0]
    assert text.startswith(photo.PREFIX + "Записать еду?")
    assert msg.answer.await_args.kwargs["reply_markup"] is not None
    assert len(log_text.PENDING) == 1
    first_token = preview_token(msg)
    pending = log_text.PENDING[first_token]
    assert pending.user_id == USER
    assert pending.raw_text == "[photo] 17 штук, 250 г"
    assert [f.description for f in pending.result.foods] == ["самса"]
    assert pending.result.foods[0].grams == 250

    # A follow-up text revises the photo preview through the text parser.
    revised = {
        "kind": "food",
        "revises": True,
        "foods": [{**SAMSA, "description": "самса, 17 шт", "grams": 250, "kcal": 650}],
    }
    v.text_answers = [revised]
    follow = make_message("их было 17, порция 250 г")
    follow.date = T0 + timedelta(minutes=1)

    await log_text.process_text(follow, "их было 17, порция 250 г", settings, db, v.client)

    sent = v.text_bodies[0]["messages"]
    assert {"role": "user", "content": "Фото еды. Подпись: 17 штук, 250 г"} in sent
    history_at = sent.index({"role": "user", "content": "Фото еды. Подпись: 17 штук, 250 г"})
    assert sent[history_at + 1]["role"] == "assistant"
    assert "самса" in sent[history_at + 1]["content"]  # the photo's result, as the parser's own answer
    assert "их было 17, порция 250 г" in sent[-1]["content"]
    assert history_at < len(sent) - 1

    assert first_token not in log_text.PENDING  # replaced, not duplicated
    assert len(log_text.PENDING) == 1
    (new_token,) = log_text.PENDING
    assert new_token != first_token
    assert preview_token(follow) == new_token
    assert log_text.PENDING[new_token].raw_text == "[photo] 17 штук, 250 г\nих было 17, порция 250 г"
    assert log_text.PENDING[new_token].result.foods[0].description == "самса, 17 шт"


async def test_photo_without_caption_stores_a_bare_marker(settings, db):
    v = Vision(settings, {"foods": [SAMSA]})
    msg = make_message(None)

    await photo.log_photo(msg, settings, db, v.client)

    (pending,) = log_text.PENDING.values()
    assert pending.raw_text == "[photo]"
    assert v.photo_bodies[0]["messages"][1]["content"][0]["text"] == "Оцени еду на фото."
    # ... and the dialog history calls it a bare photo
    v.text_answers = [{"kind": "food", "revises": True, "foods": [SAMSA]}]
    follow = make_message("это была самса")
    follow.date = T0 + timedelta(minutes=1)
    await log_text.process_text(follow, "это была самса", settings, db, v.client)
    assert {"role": "user", "content": "Фото еды"} in v.text_bodies[0]["messages"]


async def test_model_without_food_in_the_photo_end_to_end(settings, db):
    v = Vision(settings, {"foods": []})
    msg = make_message("стол")

    await photo.log_photo(msg, settings, db, v.client)

    msg.answer.assert_awaited_once_with(photo.NO_FOOD)
    assert log_text.PENDING == {} and log_text.CONTEXT == {}


async def test_all_vision_routes_down_end_to_end(settings, db):
    def handler(req):
        return httpx.Response(500, json={"error": "boom"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=60)
    s = settings.model_copy(update={"openrouter_api_key": "k", "groq_api_key": "", "stt_api_key": ""})
    msg = make_message("x")

    await photo.log_photo(msg, s, db, OpenRouterClient(s, http))

    msg.answer.assert_awaited_once_with(photo.FAILED)
    assert log_text.PENDING == {}
