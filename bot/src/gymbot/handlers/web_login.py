"""/web: a link that signs the Mini App in from a computer browser (gymbot.services.web_login). The token rides in
the fragment (`#login=`), which the browser never sends to the server: it stays out of access logs."""

from __future__ import annotations

from datetime import UTC, datetime

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from gymbot.config import Settings
from gymbot.db.session import Sessionmaker
from gymbot.services import web_login
from gymbot.services.users import get_or_create_user

router = Router(name="web_login")

TEXT = (
    "Ссылка для входа с компьютера: откроет дневник и дела в браузере. Действует 30 дней, "
    "никому её не пересылай — по ней пускают как тебя. Если ссылка ушла не туда: /web выйти."
)
REVOKED = "Готово: все ссылки входа с компьютера, выданные раньше, больше не работают. Новая — /web."
REVOKE_WORDS = ("выйти", "выход", "отозвать", "logout")
NOT_SET = "Вход с компьютера не настроен: нет адреса (PUBLIC_URL)."


@router.message(Command("web"))
async def cmd_web(
    message: Message, settings: Settings, sessionmaker: Sessionmaker, command: CommandObject | None = None
) -> None:
    base = (settings.public_url or settings.miniapp_url).rstrip("/")
    tg = message.from_user
    if tg is not None and command is not None and (command.args or "").strip().lower() in REVOKE_WORDS:
        async with sessionmaker() as session:
            user = await get_or_create_user(session, tg.id, tg.full_name)
            user.web_login_since = datetime.now(UTC)
            await session.commit()
        await message.answer(REVOKED)
        return
    if not base or tg is None:
        await message.answer(NOT_SET)
        return
    token = web_login.make_token(web_login.signing_key(settings.bot_token), tg.id)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Открыть в браузере", url=f"{base}/#login={token}")]])
    await message.answer(TEXT, reply_markup=kb)
