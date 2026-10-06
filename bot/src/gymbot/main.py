"""Entry point: `python -m gymbot.main`.

One process runs everything: database migrations, program import, the Telegram bot (long polling)
and the HTTP server with the Mini App API and the built Mini App.
"""

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import uvicorn
from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.types import BotCommand, MenuButtonWebApp, TelegramObject, User, WebAppInfo

from gymbot.api.app import create_app
from gymbot.config import Settings, get_settings
from gymbot.db.migrate import upgrade_head
from gymbot.db.session import make_engine
from gymbot.handlers import common, log_text
from gymbot.services.programs import sync_programs

log = logging.getLogger("gymbot")


class AllowedUsers(BaseMiddleware):
    """Personal app: ignore everyone not in ALLOWED_USER_IDS (empty list = allow all)."""

    def __init__(self, allowed: list[int]):
        self.allowed = set(allowed)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: User | None = data.get("event_from_user")
        if self.allowed and (user is None or user.id not in self.allowed):
            return None
        return await handler(event, data)


async def setup_bot_ui(bot: Bot, settings: Settings) -> None:
    await bot.set_my_commands(
        [
            BotCommand(command="today", description="План на сегодня"),
            BotCommand(command="undo", description="Удалить последнюю запись"),
            BotCommand(command="help", description="Как записывать"),
        ]
    )
    if settings.miniapp_url:
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(text="Дневник", web_app=WebAppInfo(url=settings.miniapp_url))
        )
        log.info("menu button -> %s", settings.miniapp_url)
    else:
        log.warning("MINIAPP_URL is empty: the Mini App button is not set")


async def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    engine, sessionmaker = make_engine(settings.database_url)
    await upgrade_head(engine)
    async with sessionmaker() as session:
        await sync_programs(session, settings.programs_dir)

    if not settings.miniapp_dist.is_dir():
        log.warning("%s not found: run `npm run build` in miniapp/ to serve the Mini App", settings.miniapp_dist)
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(settings, sessionmaker),
            host=settings.api_host,
            port=settings.api_port,
            log_level="info",
            proxy_headers=True,
        )
    )

    log.info("API and Mini App on http://localhost:%s", settings.api_port)
    if not settings.run_bot:
        try:
            await server.serve()
        finally:
            await engine.dispose()
        return

    bot = Bot(settings.bot_token)
    dp = Dispatcher(settings=settings, sessionmaker=sessionmaker)
    dp.update.outer_middleware(AllowedUsers(settings.allowed_user_ids))
    dp.include_routers(common.router, log_text.router)  # log_text last: it catches all text
    await setup_bot_ui(bot, settings)
    polling = asyncio.create_task(dp.start_polling(bot, handle_signals=False))
    try:
        await server.serve()  # returns on Ctrl+C (uvicorn handles the signals)
    finally:
        with contextlib.suppress(RuntimeError):  # polling may not have started yet
            await dp.stop_polling()
        await polling
        await bot.session.close()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(run())
