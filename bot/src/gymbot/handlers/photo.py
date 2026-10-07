"""Food photos: download -> a vision model -> the same food preview as typed text (log_text.reply_with_result).

The caption is a hint for the model ("17 штук, 250 г"). The preview joins the dialog context, so a follow-up
like "их было 17, порция 250 г" revises it through the text parser. Stored raw_text is "[photo] <caption>";
the image itself goes only to the vision provider and is never saved or logged.
"""

from __future__ import annotations

import base64
import logging

from aiogram import F, Router
from aiogram.types import Message, PhotoSize

from gymbot.config import Settings
from gymbot.db.session import Sessionmaker
from gymbot.handlers.log_text import reply_with_result
from gymbot.llm.openrouter import LLMError, OpenRouterClient
from gymbot.services import facts

log = logging.getLogger(__name__)
router = Router(name="photo")

MAX_SIDE = 1280  # px; enough to see the food, and Groq counts a picture against the per-minute token quota
NO_FOOD = "Не вижу на фото еды. Опиши словами, что съел."
FAILED = "Не получилось распознать фото, опиши словами."
PREFIX = "Оценка по фото, граммы можно поправить словами.\n\n"


def pick_size(sizes: list[PhotoSize]) -> PhotoSize:
    """The largest size with both sides within MAX_SIDE, else the smallest one.

    Telegram lists sizes from small to large; the largest is usually 1280 px, newer clients also send 2560 px.
    """
    fitting = [s for s in sizes if max(s.width, s.height) <= MAX_SIDE]
    if fitting:
        return max(fitting, key=lambda s: s.width * s.height)
    return min(sizes, key=lambda s: s.width * s.height)


def history_text(caption: str) -> str:
    """How the photo reads in the dialog history the text parser sees with the next message."""
    return f"Фото еды. Подпись: {caption}" if caption else "Фото еды"


@router.message(F.photo)
async def log_photo(
    message: Message, settings: Settings, sessionmaker: Sessionmaker, llm: OpenRouterClient
) -> None:
    sizes = message.photo
    assert sizes  # guaranteed by the filter
    bot = message.bot
    assert bot is not None
    caption = " ".join((message.caption or "").split())
    await bot.send_chat_action(message.chat.id, "typing")
    image = await bot.download(pick_size(sizes))  # BytesIO, a JPEG of a few hundred KB
    if image is None:
        await message.answer(FAILED)
        return
    user_id = message.from_user.id  # type: ignore[union-attr]
    async with sessionmaker() as session:
        known = await facts.prompt_facts(session, user_id)
    image_b64 = base64.b64encode(image.getvalue()).decode("ascii")
    try:
        result = await llm.parse_photo(image_b64, "image/jpeg", caption, known)
    except LLMError as e:
        log.warning("photo: every vision route failed: %s", type(e).__name__)
        await message.answer(FAILED)
        return
    if not result.foods and not result.unknown_terms:
        await message.answer(NO_FOOD)
        return
    await reply_with_result(
        message,
        history_text(caption),
        result,
        settings,
        sessionmaker,
        llm,
        raw_text=f"[photo] {caption}".rstrip(),
        prefix=PREFIX,
        known=known,
    )
