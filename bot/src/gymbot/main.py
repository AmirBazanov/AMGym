"""Entry point: `python -m gymbot.main` (long polling, good for local dev and a small VPS)."""

import asyncio
import logging

from aiogram import Bot, Dispatcher

from gymbot.config import get_settings
from gymbot.handlers import common, log_text


async def run() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    bot = Bot(settings.bot_token)
    dp = Dispatcher()
    dp.include_routers(common.router, log_text.router)  # log_text last: it catches all text
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(run())
