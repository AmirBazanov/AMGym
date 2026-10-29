"""/web: a link that signs the Mini App in from a computer browser (gymbot.services.web_login)."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from gymbot.config import Settings
from gymbot.services import web_login

router = Router(name="web_login")

TEXT = (
    "Ссылка для входа с компьютера: откроет дневник и дела в браузере. Действует 30 дней, "
    "никому её не пересылай — по ней пускают как тебя."
)
NOT_SET = "Вход с компьютера не настроен: нет адреса (PUBLIC_URL)."


@router.message(Command("web"))
async def cmd_web(message: Message, settings: Settings) -> None:
    base = (settings.public_url or settings.miniapp_url).rstrip("/")
    tg = message.from_user
    if not base or tg is None:
        await message.answer(NOT_SET)
        return
    token = web_login.make_token(web_login.signing_key(settings.bot_token), tg.id)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Открыть в браузере", url=f"{base}/?login={token}")]])
    await message.answer(TEXT, reply_markup=kb)
