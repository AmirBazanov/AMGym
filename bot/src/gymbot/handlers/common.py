from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup, WebAppInfo

from gymbot.config import get_settings

router = Router(name="common")


@router.message(CommandStart())
async def start(message: Message) -> None:
    url = get_settings().miniapp_url
    kb = (
        ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="Открыть дневник", web_app=WebAppInfo(url=url))]],
            resize_keyboard=True,
        )
        if url
        else None
    )
    await message.answer(
        "Пиши тренировку обычным текстом, например:\n"
        "«жим лёжа 3 по 10 на 60, разводки 3х12 по 14»\n"
        "Или еду: «200 г гречки и 2 яйца».",
        reply_markup=kb,
    )
