"""Food photo handler: size choice, hand-off to the vision model, error answers, preview and follow-up."""

import base64
import io
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import zxingcpp
from PIL import Image
from sqlalchemy import select
from test_barcode import code_image

from gymbot.db.models import FoodEntry, Product
from gymbot.handlers import log_text, photo
from gymbot.handlers import products as product_cards
from gymbot.llm.openrouter import LLMError, OpenRouterClient
from gymbot.llm.schemas import ParsedLabel, ParseResult, PhotoParse
from gymbot.services import products as pr
from gymbot.services.products import ProductInfo

T0 = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
USER = 42
JPEG = b"\xff\xd8\xff\xe0fake-jpeg-bytes\x00\x01"
SAMSA = {"description": "самса", "grams": 250, "kcal": 650, "protein_g": 25, "fat_g": 40, "carbs_g": 45}


def size(side: int, file_id: str | None = None, height: int | None = None):
    return SimpleNamespace(width=side, height=height if height is not None else side, file_id=file_id or f"f{side}")


def sizes(*sides: int):
    return [size(s) for s in sides]


def make_message(
    caption: str | None = None,
    photo_sizes=None,
    image: bytes | None = JPEG,
    *,
    chat_type: str = "private",
    media_group_id: str | None = None,
):
    return SimpleNamespace(
        photo=photo_sizes if photo_sizes is not None else sizes(90, 320, 800, 1280, 2560),
        caption=caption,
        media_group_id=media_group_id,
        date=T0,
        from_user=SimpleNamespace(id=USER, full_name="Amir"),
        chat=SimpleNamespace(id=USER, type=chat_type),
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
    stores = (log_text.PENDING, log_text.CONTEXT, log_text.FACTS, log_text.LOOKUPS, photo._ALBUMS)
    for store in stores:
        store.clear()
    yield
    for store in stores:
        store.clear()


@pytest.fixture(autouse=True)
def no_diary_answer(monkeypatch):
    async def parser_answer(message, text, result, *args):
        return result

    monkeypatch.setattr(log_text, "_diary_answer", parser_answer)


# ---- largest ----


def test_largest_takes_the_biggest_size_a_barcode_needs_every_pixel():
    assert photo.largest(sizes(90, 320, 800, 1280, 2560)).file_id == "f2560"


def test_largest_without_the_huge_one():
    assert photo.largest(sizes(90, 320, 800)).file_id == "f800"


def test_largest_order_does_not_matter():
    assert photo.largest(sizes(2560, 1280, 90, 800)).file_id == "f2560"


def test_largest_beyond_2560_is_still_taken():
    assert photo.largest(sizes(2560, 1600, 4000)).file_id == "f4000"


def test_largest_goes_by_area_not_by_the_longer_side():
    wide = size(1280, "wide", height=720)  # 921600 px
    tall = size(900, "tall", height=1600)  # 1440000 px: longer side is larger too
    squat = size(1500, "squat", height=1000)  # 1500000 px: bigger area, but 1500 < 1600
    assert photo.largest([size(320), wide, tall]).file_id == "tall"
    assert photo.largest([size(320), wide, tall, squat]).file_id == "squat"


def test_largest_single_size():
    assert photo.largest(sizes(90)).file_id == "f90"
    assert photo.largest(sizes(3000)).file_id == "f3000"


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
    """A ParseResult is wrapped like the real client does (PhotoParse); a PhotoParse is passed as it is.

    `http` is only used for the Open Food Facts lookup, i.e. when a barcode is found.
    """
    if isinstance(result, ParseResult):
        result = PhotoParse(result=result)
    return SimpleNamespace(parse_photo=AsyncMock(return_value=result, side_effect=exc), http=object())


async def run(msg, llm, settings, db):
    await photo.log_photo(msg, settings, db, llm)


async def test_photo_goes_to_the_vision_model_and_then_to_the_preview(settings, db, reply, known):
    result = food_result()
    llm = fake_llm(result)
    msg = make_message("  17   штук,\n250 г ")

    await run(msg, llm, settings, db)

    msg.bot.send_chat_action.assert_awaited_once_with(USER, "typing")
    msg.bot.download.assert_awaited_once()
    assert msg.bot.download.await_args.args[0].file_id == "f2560"  # the largest: a barcode needs the pixels
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


# ---- review fixes: download errors, private chats only, albums, no revision of a typed preview ----


@pytest.mark.parametrize("broken", ["send_chat_action", "download"])
async def test_telegram_errors_before_the_model_answer_failed(settings, db, reply, known, caplog, broken):
    llm = fake_llm(food_result())
    msg = make_message("плов")
    setattr(msg.bot, broken, AsyncMock(side_effect=RuntimeError("secret detail")))

    await run(msg, llm, settings, db)

    msg.answer.assert_awaited_once_with(photo.FAILED)
    llm.parse_photo.assert_not_awaited()
    reply.assert_not_awaited()
    assert "secret detail" not in caplog.text  # only the exception type is logged
    assert "RuntimeError" in caplog.text


@pytest.mark.parametrize(("chat_type", "handled"), [("private", True), ("group", False), ("supergroup", False)])
async def test_photos_are_handled_only_in_private_chats(chat_type, handled):
    (handler,) = photo.router.message.handlers
    ok, _ = await handler.check(make_message("плов", chat_type=chat_type))
    assert ok is handled


async def test_album_reads_only_the_first_photo_and_says_so_once(settings, db, reply, known):
    llm = fake_llm(food_result())
    first = make_message("плов", media_group_id="g1")
    second = make_message(None, media_group_id="g1")
    third = make_message(None, media_group_id="g1")

    for msg in (first, second, third):
        await run(msg, llm, settings, db)

    first.answer.assert_awaited_once_with(photo.ALBUM)
    llm.parse_photo.assert_awaited_once()
    reply.assert_awaited_once()
    for msg in (second, third):
        msg.answer.assert_not_awaited()
        msg.bot.download.assert_not_awaited()

    # Another album, and a single photo, are read again.
    other = make_message(None, media_group_id="g2")
    single = make_message("самса")
    await run(other, llm, settings, db)
    await run(single, llm, settings, db)
    assert llm.parse_photo.await_count == 3
    other.answer.assert_awaited_once_with(photo.ALBUM)
    single.answer.assert_not_awaited()


def test_album_memory_is_bounded(monkeypatch):
    monkeypatch.setattr(photo, "MAX_ALBUMS", 3)
    for i in range(5):
        assert photo.first_of_album(f"g{i}")
    assert len(photo._ALBUMS) == 3
    assert not photo.first_of_album("g4")
    assert photo.first_of_album("g0")  # the oldest was forgotten


async def test_photo_does_not_revise_an_open_typed_preview(settings, db):
    plov = {"description": "плов", "grams": 300, "kcal": 540, "protein_g": 18, "fat_g": 21, "carbs_g": 69}
    bread = {"description": "лепёшка", "grams": 250, "kcal": 650, "protein_g": 22, "fat_g": 4, "carbs_g": 130}
    v = Vision(settings, {"foods": [{**plov, "grams": 350, "kcal": 630, "protein_g": 21, "fat_g": 25, "carbs_g": 80}]})
    v.text_answers = [{"kind": "food", "foods": [bread, plov]}]
    typed = make_message(None)
    typed.text = "лепёшка, плов 300 г"
    await log_text.process_text(typed, typed.text, settings, db, v.client)
    typed_token = preview_token(typed)

    shot = make_message(None)
    shot.date = T0 + timedelta(minutes=1)
    await photo.log_photo(shot, settings, db, v.client)

    # Both previews stay: the typed one with the bread is not replaced by the photo's plov.
    assert len(log_text.PENDING) == 2
    assert [f.description for f in log_text.PENDING[typed_token].result.foods] == ["лепёшка", "плов"]
    photo_token = preview_token(shot)
    assert photo_token != typed_token
    assert log_text.PENDING[photo_token].raw_text == "[photo]"
    assert [f.description for f in log_text.PENDING[photo_token].result.foods] == ["плов"]
    # The dialog continues with the photo: a follow-up revises it, not the typed preview.
    assert log_text.CONTEXT[USER].token == photo_token


# ---- packaged products: barcode -> Open Food Facts, label, caption hints ----

CODE = "5000159407236"
MARS = ProductInfo("Mars", "Mars", 450.0, 4.0, 16.8, 70.0, net_weight_g=51.0, barcode=CODE, source="off")
NO_FOODS = ParseResult(kind="food")


def label_parse(name="Протеиновый батончик", brand="Bombbar", kcal=360, protein=33, fat=12, carbs=30, **kw) -> PhotoParse:
    label = {
        "name": name,
        "brand": brand,
        "per100": {"kcal": kcal, "protein_g": protein, "fat_g": fat, "carbs_g": carbs},
        "net_weight_g": 60,
        **kw,
    }
    return PhotoParse(result=NO_FOODS, label=ParsedLabel.model_validate(label))


@pytest.fixture(autouse=True)
def clean_cards():
    stores = (product_cards.CARDS, product_cards.PREVIEWS, product_cards.CHOICES, product_cards.LATEST)
    for store in stores:
        store.clear()
    yield
    for store in stores:
        store.clear()


@pytest.fixture
def code(monkeypatch):
    """decode_async finds a barcode; off_product is a mock the test configures."""
    decode = AsyncMock(return_value=CODE)
    off = AsyncMock(return_value=MARS)
    monkeypatch.setattr(photo.barcode, "decode_async", decode)
    monkeypatch.setattr(photo.pr, "off_product", off)
    return SimpleNamespace(decode=decode, off=off)


def card_of(msg):
    (card,) = product_cards.CARDS.values()
    return card


async def test_barcode_found_in_off_makes_a_product_card(settings, db, reply, known, code):
    llm = fake_llm(NO_FOODS)
    msg = make_message(None)

    await run(msg, llm, settings, db)

    code.decode.assert_awaited_once_with(JPEG)
    code.off.assert_awaited_once_with(CODE, llm.http)
    reply.assert_not_awaited()
    msg.answer.assert_awaited_once()
    text = msg.answer.await_args.args[0]
    assert text.startswith("Mars (Open Food Facts)") and "Сколько съел?" in text
    card = card_of(msg)
    assert card.product == MARS and card.raw_text == "[photo]" and card.sent_at == T0 and card.note is None


async def test_vision_and_off_both_run_for_one_photo(settings, db, reply, known, code):
    llm = fake_llm(label_parse(kcal=450, protein=4, fat=17, carbs=70))
    await run(make_message("x"), llm, settings, db)
    llm.parse_photo.assert_awaited_once()
    code.off.assert_awaited_once()


async def test_off_error_falls_back_to_the_label(settings, db, reply, known, code, caplog):
    code.off.side_effect = httpx.ConnectError("secret detail")
    llm = fake_llm(label_parse())
    msg = make_message(None)

    await run(msg, llm, settings, db)

    card = card_of(msg)
    p = card.product
    assert (p.name, p.brand, p.source, p.barcode, p.kcal) == ("Протеиновый батончик", "Bombbar", "label", CODE, 360)
    assert "Сколько съел?" in msg.answer.await_args.args[0]
    assert "secret detail" not in caplog.text and "ConnectError" in caplog.text


async def test_off_error_and_no_label_says_not_found_with_the_code(settings, db, reply, known, code):
    code.off.side_effect = httpx.ReadTimeout("slow")
    msg = make_message(None)
    await run(msg, fake_llm(NO_FOODS), settings, db)
    msg.answer.assert_awaited_once_with(pr.NOT_FOUND.format(code=CODE))
    assert product_cards.CARDS == {}


async def test_vision_failure_with_an_off_hit_still_makes_a_card(settings, db, reply, known, code):
    msg = make_message(None)
    await run(msg, fake_llm(exc=LLMError("all models failed")), settings, db)
    assert card_of(msg).product == MARS
    assert "Сколько съел?" in msg.answer.await_args.args[0]
    reply.assert_not_awaited()


async def test_vision_failure_with_a_code_off_does_not_know(settings, db, reply, known, code):
    code.off.return_value = None
    msg = make_message(None)
    await run(msg, fake_llm(exc=LLMError("all models failed")), settings, db)
    msg.answer.assert_awaited_once_with(pr.NOT_FOUND.format(code=CODE))


async def test_code_unknown_to_off_without_label_or_foods_says_not_found_with_the_code(
    settings, db, reply, known, code
):
    code.off.return_value = None
    msg = make_message("x")
    await run(msg, fake_llm(NO_FOODS), settings, db)
    msg.answer.assert_awaited_once_with(pr.NOT_FOUND.format(code=CODE))
    assert CODE in msg.answer.await_args.args[0]
    reply.assert_not_awaited()


async def test_code_unknown_to_off_but_a_plate_goes_to_the_usual_preview(settings, db, reply, known, code):
    code.off.return_value = None
    result = food_result()
    msg = make_message("x")
    await run(msg, fake_llm(result), settings, db)
    reply.assert_awaited_once()
    assert reply.await_args.args[2] is result
    msg.answer.assert_not_awaited()


async def test_off_hit_wins_over_a_plate_estimate(settings, db, reply, known, code):
    msg = make_message("x")
    await run(msg, fake_llm(food_result()), settings, db)
    reply.assert_not_awaited()
    assert card_of(msg).product == MARS


async def test_unreadable_label_asks_for_another_photo(settings, db, reply, known):
    llm = fake_llm(label_parse(kcal=900, protein=3, fat=2, carbs=5))
    msg = make_message(None)
    await run(msg, llm, settings, db)
    msg.answer.assert_awaited_once_with(pr.LABEL_UNCLEAR)
    reply.assert_not_awaited()
    assert product_cards.CARDS == {}


async def test_off_without_numbers_and_an_unreadable_label_asks_for_another_photo(settings, db, reply, known, code):
    code.off.return_value = replace(MARS, kcal=None, protein_g=None, fat_g=None, carbs_g=None)
    msg = make_message(None)
    await run(msg, fake_llm(label_parse(kcal=900, protein=3, fat=2, carbs=5)), settings, db)
    msg.answer.assert_awaited_once_with(pr.LABEL_UNCLEAR)


async def test_off_without_numbers_and_no_label_says_so(settings, db, reply, known, code):
    code.off.return_value = replace(MARS, kcal=None, protein_g=None, fat_g=None, carbs_g=None)
    msg = make_message(None)
    await run(msg, fake_llm(NO_FOODS), settings, db)
    msg.answer.assert_awaited_once_with(pr.NO_NUMBERS.format(name="Mars"))


async def test_label_without_a_barcode_makes_a_card(settings, db, reply, known):
    msg = make_message(None)
    await run(msg, fake_llm(label_parse()), settings, db)
    p = card_of(msg).product
    assert (p.source, p.barcode, p.net_weight_g) == ("label", None, 60)
    reply.assert_not_awaited()


async def test_off_is_not_asked_without_a_barcode(settings, db, reply, known, monkeypatch):
    off = AsyncMock()
    monkeypatch.setattr(photo.pr, "off_product", off)
    await run(make_message("x"), fake_llm(food_result()), settings, db)
    off.assert_not_awaited()
    reply.assert_awaited_once()


async def test_off_label_difference_is_shown_as_a_note(settings, db, reply, known, code):
    msg = make_message(None)
    await run(msg, fake_llm(label_parse(kcal=540, protein=4, fat=26, carbs=70)), settings, db)
    assert "540" in card_of(msg).note and "450" in card_of(msg).note
    assert "540" in msg.answer.await_args.args[0]


async def test_caption_with_grams_goes_straight_to_the_preview(settings, db, reply, known, code):
    llm = fake_llm(NO_FOODS)
    msg = make_message("50 г")
    await run(msg, llm, settings, db)
    text = msg.answer.await_args.args[0]
    assert text.startswith("Записать еду? Всего 225 ккал") and "Mars, 50 г" in text
    (preview,) = product_cards.PREVIEWS.values()
    assert preview.raw_text == "[photo] 50 г"
    assert llm.parse_photo.await_args.args[2] == "50 г"  # the model still gets the caption


@pytest.mark.parametrize(("caption", "grams"), [("вся пачка", 51), ("половина", 25.5), ("2", 102)])
async def test_caption_with_package_amounts(settings, db, reply, known, code, caption, grams):
    await run(make_message(caption), fake_llm(NO_FOODS), settings, db)
    (preview,) = product_cards.PREVIEWS.values()
    assert preview.food.grams == grams


async def test_caption_serving_without_a_serving_size_asks(settings, db, reply, known, code):
    msg = make_message("1 порция")
    await run(msg, fake_llm(NO_FOODS), settings, db)
    assert "Сколько съел?" in msg.answer.await_args.args[0]
    assert product_cards.PREVIEWS == {}


def test_caption_hints():
    assert photo.caption_hints("") == (None, None)
    amount, alias = photo.caption_hints("50 г")
    assert amount.grams == 50 and alias is None
    amount, alias = photo.caption_hints("мой протеин")
    assert amount is None and alias == "протеин"
    amount, alias = photo.caption_hints("мой протеин 2 скупа")
    assert amount.servings == 2 and alias == "протеин"
    assert photo.caption_hints("мой протеин 2")[1] == "протеин"  # a bare number is an amount, not a name
    assert photo.caption_hints("очень " * 10)[1] is None  # too long for a name


async def test_caption_name_for_a_label_without_a_name(settings, db, reply, known):
    msg = make_message("мой протеин")
    await run(msg, fake_llm(label_parse(name=None, brand=None)), settings, db)
    card = card_of(msg)
    assert card.product.name == "протеин" and card.alias is None


async def test_caption_name_becomes_an_alias_when_the_label_has_a_name(settings, db, reply, known):
    msg = make_message("мой протеин")
    await run(msg, fake_llm(label_parse()), settings, db)
    card = card_of(msg)
    assert card.product.name == "Протеиновый батончик" and card.alias == "протеин"


async def test_caption_name_with_amount_on_a_nameless_label(settings, db, reply, known):
    msg = make_message("мой протеин 30 г")
    await run(msg, fake_llm(label_parse(name=None, brand=None)), settings, db)
    assert "протеин, 30 г" in msg.answer.await_args.args[0]


async def test_plate_photo_never_reaches_the_product_flow(settings, db, reply, known):
    msg = make_message("17 штук")
    await run(msg, fake_llm(food_result()), settings, db)
    assert product_cards.CARDS == {}
    reply.assert_awaited_once()


async def test_downscaled_copy_goes_to_the_model_and_the_original_to_the_decoder(settings, db, reply, known, monkeypatch):
    decode = AsyncMock(return_value=None)
    monkeypatch.setattr(photo.barcode, "decode_async", decode)
    seen = []

    def downscale(data, max_side):
        seen.append((data, max_side))
        return b"small"

    monkeypatch.setattr(photo.barcode, "downscale", downscale)
    llm = fake_llm(food_result())
    await run(make_message("x"), llm, settings, db)
    decode.assert_awaited_once_with(JPEG)
    assert seen == [(JPEG, photo.MAX_SIDE)] and photo.MAX_SIDE == 1280
    assert llm.parse_photo.await_args.args[0] == base64.b64encode(b"small").decode()


# ---- end to end: a real barcode image, the real client and Open Food Facts over MockTransport ----

OFF_MARS = {
    "status": 1,
    "product": {
        "product_name": "Mars",
        "brands": "Mars",
        "nutriments": {
            "energy-kcal_100g": 450, "proteins_100g": 4.04, "fat_100g": 16.8, "carbohydrates_100g": 70,
        },
        "product_quantity": "51",
    },
}


class Shop(Vision):
    """Vision plus Open Food Facts: requests to the OFF host get `off_answer`."""

    def __init__(self, settings, photo_answer, off_answer=None, off_status=200):
        super().__init__(settings, photo_answer)
        self.off_requests: list[httpx.Request] = []
        self.off_answer, self.off_status = off_answer, off_status

    def _handle(self, req: httpx.Request) -> httpx.Response:
        if req.url.host == "world.openfoodfacts.org":
            self.off_requests.append(req)
            return httpx.Response(self.off_status, json=self.off_answer or {"status": 0})
        return super()._handle(req)


def barcode_photo(digits: str = CODE) -> bytes:
    page = Image.new("L", (2560, 1920), 255)
    page.paste(code_image(digits, zxingcpp.BarcodeFormat.EAN13), (800, 700))
    out = io.BytesIO()
    page.convert("RGB").save(out, "JPEG", quality=92)
    return out.getvalue()


async def test_barcode_photo_end_to_end_card_amount_save(settings, db, monkeypatch):
    monkeypatch.setattr(product_cards.live, "publish", lambda *a: None)
    shop = Shop(settings, {"foods": []}, OFF_MARS)
    msg = make_message(None, image=barcode_photo())

    await photo.log_photo(msg, settings, db, shop.client)

    (off_req,) = shop.off_requests
    assert off_req.url.path == f"/api/v2/product/{CODE}.json"
    (body,) = shop.photo_bodies
    url = body["messages"][1]["content"][1]["image_url"]["url"]
    sent_image = Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1])))
    assert max(sent_image.size) == 1280  # the model got the downscaled copy
    text = msg.answer.await_args.args[0]
    assert text.startswith("Mars (Open Food Facts)") and "Упаковка 51 г" in text
    kb = msg.answer.await_args.kwargs["reply_markup"]
    all_button = next(b for row in kb.inline_keyboard for b in row if b.text.startswith("Вся упаковка"))

    cb = SimpleNamespace(
        data=all_button.callback_data,
        from_user=SimpleNamespace(id=USER, full_name="Amir"),
        message=SimpleNamespace(edit_text=AsyncMock()),
        answer=AsyncMock(),
    )
    await product_cards.choose_amount(cb)
    preview_kb = cb.message.edit_text.await_args.kwargs["reply_markup"]
    save = SimpleNamespace(
        data=preview_kb.inline_keyboard[0][0].callback_data,
        from_user=cb.from_user,
        message=SimpleNamespace(edit_text=AsyncMock()),
        answer=AsyncMock(),
    )
    await product_cards.save(save, db)

    async with db() as session:
        (entry,) = (await session.scalars(select(FoodEntry))).all()
        (product,) = (await session.scalars(select(Product))).all()
    assert entry.description == "Mars, 51 г" and float(entry.kcal) == 229.5 and entry.estimated is False
    assert entry.raw_text == "[photo]"
    assert (product.barcode, product.name, float(product.net_weight_g)) == (CODE, "Mars", 51)
    assert log_text.PENDING == {}  # the typed-food confirm flow was never involved


async def test_barcode_photo_unknown_to_off_falls_back_to_the_label_end_to_end(settings, db):
    label = {"label": {"name": "Йогурт", "brand": None, "per100": {"kcal": 100, "protein_g": 5, "fat_g": 3.5, "carbs_g": 12}}}
    shop = Shop(settings, label, off_status=404)
    msg = make_message(None, image=barcode_photo())

    await photo.log_photo(msg, settings, db, shop.client)

    assert len(shop.off_requests) == 1
    p = card_of(msg).product
    assert (p.name, p.source, p.barcode) == ("Йогурт", "label", CODE)


async def test_off_down_end_to_end_still_answers(settings, db):
    shop = Shop(settings, {"foods": []}, off_status=500)
    msg = make_message(None, image=barcode_photo())
    await photo.log_photo(msg, settings, db, shop.client)
    msg.answer.assert_awaited_once_with(pr.NOT_FOUND.format(code=CODE))
