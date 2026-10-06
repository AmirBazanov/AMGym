"""Voice messages: download -> speech-to-text -> the same flow as typed text (log_text.process_text).

The user sees what was recognized ("Распознал: «...»") above the preview, so a misheard number
is caught before saving. The stored raw_text is marked "[voice] <transcript>".
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import Message

from gymbot.config import Settings
from gymbot.db.session import Sessionmaker
from gymbot.handlers.log_text import process_text
from gymbot.llm.openrouter import OpenRouterClient
from gymbot.stt import STTError, STTRateLimited, transcribe

log = logging.getLogger(__name__)
router = Router(name="voice")

NO_KEY = "Голос не настроен: нужен ключ распознавания в .env."
TOO_LONG = "Слишком длинное, до {minutes} минут. Раздели на несколько голосовых или напиши текстом."
EMPTY = "Не расслышал, попробуй ещё раз."
RATE_LIMITED = "Лимит распознавания голоса пока исчерпан. Попробуй через минуту или напиши текстом."
STT_FAILED = "Не получилось распознать голос, попробуй ещё раз или напиши текстом."


@router.message(F.voice)
async def log_voice(
    message: Message, settings: Settings, sessionmaker: Sessionmaker, llm: OpenRouterClient
) -> None:
    voice = message.voice
    assert voice is not None  # guaranteed by the filter
    if not settings.stt_api_key:
        await message.answer(NO_KEY)
        return
    if voice.duration > settings.stt_max_seconds:
        await message.answer(TOO_LONG.format(minutes=max(1, settings.stt_max_seconds // 60)))
        return
    bot = message.bot
    assert bot is not None
    await bot.send_chat_action(message.chat.id, "typing")
    audio = await bot.download(voice)  # BytesIO; bots may download files up to 20 MB, plenty for voice
    if audio is None:
        await message.answer(STT_FAILED)
        return
    try:
        transcript = (await transcribe(audio.getvalue(), "voice.ogg", settings)).strip()
    except STTRateLimited as e:
        log.warning("%s", e)
        await message.answer(RATE_LIMITED)
        return
    except STTError as e:
        log.warning("%s", e)
        await message.answer(STT_FAILED)
        return
    if not transcript:
        await message.answer(EMPTY)
        return
    await process_text(
        message,
        transcript,
        settings,
        sessionmaker,
        llm,
        raw_text=f"[voice] {transcript}",
        prefix=f"Распознал: «{transcript}»\n\n",
    )
