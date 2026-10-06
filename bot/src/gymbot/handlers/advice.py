"""/advice and "совет..." messages: AI advice on nutrition, training and recovery from the user's data."""

import re
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.utils.chat_action import ChatActionSender

from gymbot.config import Settings
from gymbot.db.session import Sessionmaker
from gymbot.llm.openrouter import LLMError, OpenRouterClient
from gymbot.services import advice
from gymbot.services.users import get_or_create_user

router = Router(name="advice")

# Whole words at the start of the message only: "советская колбаса 100 г" and "съел ..." go to the parser.
ADVICE_TEXT = re.compile(
    r"^\s*(совет(ы|уй|ов)?|что посоветуешь|посоветуй|рекомендаци[ия])\b", re.IGNORECASE
)
UNAVAILABLE = "Нейросеть сейчас недоступна, попробуй ещё раз чуть позже."


@router.message(Command("advice"))
@router.message(F.text.regexp(ADVICE_TEXT))
async def give_advice(message: Message, settings: Settings, sessionmaker: Sessionmaker, llm: OpenRouterClient) -> None:
    tz = ZoneInfo(settings.timezone)
    # Generation takes longer than one chat action lasts (~5 s): keep "typing" on until the answer.
    typing = ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id)  # type: ignore[arg-type]
    async with typing, sessionmaker() as session:
        user = await get_or_create_user(session, message.from_user.id, message.from_user.full_name)  # type: ignore[union-attr]
        await session.commit()  # the user row exists after the middleware; never hold a write lock below
        try:
            text = await advice.generate(session, user, settings, llm, tz, datetime.now(UTC))
        except LLMError:
            text = UNAVAILABLE
    await message.answer(text)
