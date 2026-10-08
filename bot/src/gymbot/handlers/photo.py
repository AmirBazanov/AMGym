"""Food photos: download -> a vision model -> the same food preview as typed text (log_text.reply_with_result).

The caption is a hint for the model ("17 штук, 250 г"). The preview joins the dialog context, so a follow-up
like "их было 17, порция 250 г" revises it through the text parser. Stored raw_text is "[photo] <caption>";
the image itself goes only to the vision provider and is never saved or logged.

Only private chats: in a group every photo would cost a vision request. An album (media_group_id) arrives as
one message per photo; only the first one is read, with a note to send photos one at a time.
A photo never revises a typed preview it did not see (log_text.reply_with_result): it gets its own preview.

Packaged products: the largest size is downloaded (barcodes need resolution) and scanned locally
(services/barcode.py). With a code, the Open Food Facts lookup and the vision call run together; the vision
model reads a package label instead of estimating (PhotoParse.label). services.products.combine picks OFF,
else the label; then handlers/products.py asks «Сколько съел?» (a caption like "50 г" answers it at once).
One vision call per photo either way: the label is the fallback for products OFF lacks and the cross-check.
A plate with a barcoded product beside it keeps the plate estimate; the product gets its own card after it.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from dataclasses import replace

from aiogram import F, Router
from aiogram.types import Message, PhotoSize

from gymbot.config import Settings
from gymbot.db.session import Sessionmaker
from gymbot.handlers import products as product_cards
from gymbot.handlers.log_text import keep_typing, reply_with_result, with_typing
from gymbot.llm.openrouter import LLMError, OpenRouterClient
from gymbot.llm.schemas import ParseResult, PhotoParse
from gymbot.services import barcode, facts, plausibility
from gymbot.services import products as pr

log = logging.getLogger(__name__)
router = Router(name="photo")

MAX_SIDE = 1280  # px sent to the vision model: enough to see the food; Groq counts a picture against its quota
ALIAS_MAX = 40
NO_FOOD = "Не вижу на фото еды. Опиши словами, что съел."
FAILED = "Не получилось распознать фото, опиши словами."
ALBUM = "Пришли по одной фотографии: разбираю только первую."
MAX_ALBUMS = 200
_ALBUMS: dict[str, None] = {}  # media_group_ids already answered, oldest first (a bounded set)
PREFIX = "Оценка по фото, граммы можно поправить словами.\n\n"
ALSO_PACKAGE = "На фото ещё упаковка со штрихкодом. Если ел и её:\n\n"


def largest(sizes: list[PhotoSize]) -> PhotoSize:
    """The largest size (1280 px, or 2560 px from newer clients): a barcode needs every pixel."""
    return max(sizes, key=lambda s: s.width * s.height)


def caption_hints(caption: str) -> tuple[pr.Amount | None, str | None]:
    """An amount in the caption ("50 г", "половина") and a short name for the product ("мой протеин")."""
    amount, rest = pr.parse_amount(caption)
    name = " ".join(rest)
    alias = name if name and len(name) <= ALIAS_MAX and not any(c.isdigit() for c in name) else None
    return (None if amount.empty else amount), alias


def history_text(caption: str) -> str:
    """How the photo reads in the dialog history the text parser sees with the next message."""
    return f"Фото еды. Подпись: {caption}" if caption else "Фото еды"


def first_of_album(group_id: str | None) -> bool:
    """Whether a photo is to be read: not an album, or the first photo of its album to arrive."""
    if group_id is None:
        return True
    if group_id in _ALBUMS:
        return False
    if len(_ALBUMS) >= MAX_ALBUMS:
        _ALBUMS.pop(next(iter(_ALBUMS)))
    _ALBUMS[group_id] = None
    return True


@router.message(F.photo, F.chat.type == "private")
async def log_photo(
    message: Message, settings: Settings, sessionmaker: Sessionmaker, llm: OpenRouterClient
) -> None:
    sizes = message.photo
    assert sizes  # guaranteed by the filter
    bot = message.bot
    assert bot is not None
    album = message.media_group_id is not None
    if not first_of_album(message.media_group_id):
        return
    if album:
        await message.answer(ALBUM)
    caption = " ".join((message.caption or "").split())
    try:
        await bot.send_chat_action(message.chat.id, "typing")
        image = await bot.download(largest(sizes))  # BytesIO, a JPEG of a few hundred KB
    except Exception as e:  # noqa: BLE001 - network or Telegram errors: the user must still get an answer
        log.warning("photo: download failed: %s", type(e).__name__)
        image = None
    if image is None:
        await message.answer(FAILED)
        return
    user_id = message.from_user.id  # type: ignore[union-attr]
    async with sessionmaker() as session:
        known = await facts.prompt_facts(session, user_id)
    data = image.getvalue()
    code = await barcode.decode_async(data)
    small = await asyncio.to_thread(barcode.downscale, data, MAX_SIDE)
    image_b64 = base64.b64encode(small).decode("ascii")
    async with keep_typing(message):  # Claude looks at a photo for several seconds
        seen, off = await asyncio.gather(
            _vision(llm, image_b64, caption, known), _off(code, llm) if code else _nothing()
        )
    raw_text = f"[photo] {caption}".rstrip()
    decision = pr.combine(off, seen.label if seen else None, code)
    plate = seen is not None and seen.label is None and bool(seen.result.foods or seen.result.unknown_terms)
    if decision.product is not None and plate:
        # A plate with a packaged product beside it (a yogurt): the plate estimate stays, the product is offered
        # as a separate card; the caption's amount is the plate's, so the card asks.
        assert seen is not None
        plate_result = await _plausible(message, seen.result, caption, llm, known)
        await reply_with_result(
            message, history_text(caption), plate_result, settings, sessionmaker, llm,
            raw_text=raw_text, prefix=PREFIX, known=known,
        )  # fmt: skip
        await product_cards.offer(
            message, decision.product, raw_text=raw_text, note=decision.note, prefix=ALSO_PACKAGE, listen=False
        )  # "250 г" typed next corrects the plate, not this card
        return
    if decision.product is not None:
        amount, alias = caption_hints(caption)
        product = decision.product
        if alias and product.name == pr.LABEL_NAME:  # "мой протеин" under a label without a name: that is its name
            product, alias = replace(product, name=alias), None
        await product_cards.offer(message, product, raw_text=raw_text, amount=amount, note=decision.note, alias=alias)
        return
    if decision.problem:
        await message.answer(decision.problem)
        return
    if seen is None:
        await message.answer(pr.NOT_FOUND.format(code=code) if code else FAILED)
        return
    result = seen.result
    if not result.foods and not result.unknown_terms:
        await message.answer(pr.NOT_FOUND.format(code=code) if code else NO_FOOD)
        return
    result = await _plausible(message, result, caption, llm, known)
    await reply_with_result(
        message,
        history_text(caption),
        result,
        settings,
        sessionmaker,
        llm,
        raw_text=raw_text,
        prefix=PREFIX,
        known=known,
    )


async def _plausible(
    message: Message, result: ParseResult, caption: str, llm: OpenRouterClient, known: list[str]
) -> ParseResult:
    """A plate estimate after the plausibility check (services/plausibility). The repair round goes over the text
    parser with the photo as the previous turn: resending the image would cost ~2K tokens of the shared quota."""
    reparse = plausibility.parser_reparse(llm, history_text(caption), result, None, known)
    return await plausibility.review(result, with_typing(message, reparse))


async def _vision(llm: OpenRouterClient, image_b64: str, caption: str, known: list[str]) -> PhotoParse | None:
    """The vision answer, or None when every route failed (an OFF hit may still make a card)."""
    try:
        return await llm.parse_photo(image_b64, "image/jpeg", caption, known)
    except LLMError as e:
        log.warning("photo: every vision route failed: %s", type(e).__name__)
        return None


async def _off(code: str, llm: OpenRouterClient) -> pr.ProductInfo | None:
    """Open Food Facts by barcode; a network error counts as "not found" (the label may still help)."""
    try:
        return await pr.off_product(code, llm.http)
    except Exception as e:  # noqa: BLE001 - OFF is an extra source: it must never cost the user the reply
        log.warning("photo: Open Food Facts lookup failed: %s", type(e).__name__)
        return None


async def _nothing() -> None:
    return None
