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
from gymbot.handlers import advice, common, log_text, voice
from gymbot.llm.openrouter import OpenRouterClient
from gymbot.services.access import is_allowed
from gymbot.services.programs import sync_programs
from gymbot.services.reminders import reminder_loop
from gymbot.services.users import get_or_create_user

log = logging.getLogger("gymbot")


class AllowedUsers(BaseMiddleware):
    """Personal app: ignore everyone else (see gymbot.services.access)."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: User | None = data.get("event_from_user")
        if user is None:
            return None
        async with data["sessionmaker"]() as session:
            if not await is_allowed(session, data["settings"], user.id):
                log.info("ignored update from user %s (not the owner)", user.id)
                return None
            # Register on first contact so the owner is fixed even before any data is saved.
            await get_or_create_user(session, user.id, user.full_name)
            await session.commit()
        return await handler(event, data)


async def setup_bot_ui(bot: Bot, settings: Settings) -> None:
    await bot.set_my_commands(
        [
            BotCommand(command="today", description="План на сегодня"),
            BotCommand(command="undo", description="Удалить последнюю запись"),
            BotCommand(command="advice", description="Советы по питанию, тренировкам и восстановлению"),
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


async def shutdown(
    dp: Dispatcher,
    polling: asyncio.Task[None],
    reminders: asyncio.Task[None],
    bot: Bot,
    engine: Any,
    llm: OpenRouterClient,
) -> None:
    reminders.cancel()
    with contextlib.suppress(Exception, asyncio.CancelledError):
        await reminders
    with contextlib.suppress(RuntimeError):  # polling may already be stopped
        await dp.stop_polling()
    with contextlib.suppress(Exception, asyncio.CancelledError):
        await polling
    await bot.session.close()
    await llm.aclose()
    await engine.dispose()


async def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    if settings.dev_user_id and settings.miniapp_url:
        raise SystemExit("DEV_USER_ID is for local browser testing only; remove it when MINIAPP_URL is set")
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
    # One LLM client per process: it remembers which models reject response_format.
    llm = OpenRouterClient(settings)
    dp = Dispatcher(settings=settings, sessionmaker=sessionmaker, llm=llm)
    dp.update.outer_middleware(AllowedUsers())
    # voice before log_text: the filters do not overlap, but the order is kept explicit.
    dp.include_routers(common.router, advice.router, voice.router, log_text.router)  # log_text last: it catches all text
    await setup_bot_ui(bot, settings)
    polling = asyncio.create_task(dp.start_polling(bot, handle_signals=False))
    # If polling dies (e.g. the token was revoked), stop the HTTP server too instead of running half-alive.
    polling.add_done_callback(lambda _: setattr(server, "should_exit", True))
    reminders = asyncio.create_task(reminder_loop(bot, sessionmaker, settings, llm))
    try:
        await server.serve()  # returns on Ctrl+C (uvicorn handles the signals)
    finally:
        await asyncio.shield(shutdown(dp, polling, reminders, bot, engine, llm))


if __name__ == "__main__":
    asyncio.run(run())
