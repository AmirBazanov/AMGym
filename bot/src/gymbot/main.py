"""Entry point: `python -m gymbot.main`.

One process runs everything: database migrations, program import, the Telegram bot and the HTTP server
with the Mini App API and the built Mini App. The bot gets updates by long polling (BOT_MODE=polling,
local runs behind a quick tunnel) or by a webhook on the same HTTP server (BOT_MODE=webhook, a server
with a fixed HTTPS address in PUBLIC_URL behind a reverse proxy).
"""

import asyncio
import contextlib
import logging
import secrets
from collections.abc import Awaitable, Callable
from typing import Any

import uvicorn
from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.types import BotCommand, MenuButtonWebApp, TelegramObject, User, WebAppInfo
from fastapi import APIRouter

from gymbot.api.app import create_app
from gymbot.api.webhook import WEBHOOK_PATH, WebhookHandler
from gymbot.config import Settings, get_settings
from gymbot.db.migrate import upgrade_head
from gymbot.db.session import make_engine
from gymbot.handlers import advice, chat_settings, common, facts, log_text, plan, voice
from gymbot.llm.openrouter import OpenRouterClient
from gymbot.services import baselines, live
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


class Server(uvicorn.Server):
    """uvicorn waits for open responses before the app's lifespan shutdown runs, and a Mini App event
    stream never ends by itself: close the streams first, so a restart does not hang on them."""

    async def shutdown(self, sockets: list[Any] | None = None) -> None:
        live.close_all()
        await super().shutdown(sockets)


async def setup_bot_ui(bot: Bot, settings: Settings) -> None:
    await bot.set_my_commands(
        [
            BotCommand(command="today", description="План на сегодня"),
            BotCommand(command="plan", description="План с поправками под самочувствие"),
            BotCommand(command="undo", description="Удалить последнюю запись"),
            BotCommand(command="advice", description="Советы по питанию, тренировкам и восстановлению"),
            BotCommand(command="facts", description="Что я помню о тебе"),
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


def webhook_secret(settings: Settings) -> str:
    """WEBHOOK_SECRET, or a fresh random one (Telegram allows A-Za-z0-9_-, up to 256 characters)."""
    if settings.webhook_secret:
        return settings.webhook_secret
    log.info("WEBHOOK_SECRET is empty: generated a random one for this run")
    return secrets.token_urlsafe(32)


async def configure_delivery(bot: Bot, dp: Dispatcher, settings: Settings, secret: str) -> None:
    """Tell Telegram how to deliver updates. A webhook and getUpdates exclude each other."""
    if settings.bot_mode == "webhook":
        url = f"{settings.public_url}{WEBHOOK_PATH}"
        await bot.set_webhook(
            url=url,
            secret_token=secret,
            allowed_updates=dp.resolve_used_update_types(),  # the same set start_polling asks for
            drop_pending_updates=False,
        )
        log.info("webhook -> %s", url)
    else:
        # aiogram's start_polling does not remove a webhook, and getUpdates fails while one is set.
        # Note: with the production token this switches the server off its webhook.
        info = await bot.get_webhook_info()
        if info.url:
            log.warning("removing webhook %s to poll (use a separate bot token for local runs)", info.url)
            await bot.delete_webhook(drop_pending_updates=False)


async def shutdown(
    dp: Dispatcher,
    polling: asyncio.Task[None] | None,
    webhook: WebhookHandler | None,
    reminders: asyncio.Task[None],
    bot: Bot,
    engine: Any,
    llm: OpenRouterClient,
) -> None:
    reminders.cancel()
    with contextlib.suppress(Exception, asyncio.CancelledError):
        await reminders
    await baselines.cancel_all()  # unfinished facts are picked up by the next startup's backfill
    if polling is not None:
        with contextlib.suppress(RuntimeError):  # polling may already be stopped
            await dp.stop_polling()
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await polling
    if webhook is not None:
        # The webhook stays set: Telegram queues updates while the service restarts.
        await webhook.drain(timeout=10)
    await bot.session.close()
    await llm.aclose()
    await engine.dispose()


async def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    if settings.dev_user_id and settings.miniapp_url:
        raise SystemExit("DEV_USER_ID is for local browser testing only; remove it when MINIAPP_URL is set")
    if settings.bot_mode == "webhook" and not settings.public_url.startswith("https://"):
        raise SystemExit("BOT_MODE=webhook needs PUBLIC_URL=https://your.domain (Telegram calls webhooks over HTTPS)")
    engine, sessionmaker = make_engine(settings.database_url)
    await upgrade_head(engine)
    async with sessionmaker() as session:
        await sync_programs(session, settings.programs_dir)

    if not settings.miniapp_dist.is_dir():
        log.warning("%s not found: run `npm run build` in miniapp/ to serve the Mini App", settings.miniapp_dist)
    # One LLM client per process (bot and API): it remembers which models reject response_format.
    llm = OpenRouterClient(settings)
    log.info("LLM routes: %s", ", ".join(r.name for r in llm.routes) or "none (no API key)")
    # Working weights for facts saved before this feature or while the model was down (idempotent).
    baselines.start_backfill(sessionmaker, llm)

    bot: Bot | None = None
    dp: Dispatcher | None = None
    webhook: WebhookHandler | None = None
    routers: list[APIRouter] = []
    if settings.run_bot:
        bot = Bot(settings.bot_token)
        dp = Dispatcher(settings=settings, sessionmaker=sessionmaker, llm=llm)
        dp.update.outer_middleware(AllowedUsers())
        # voice before log_text: the filters do not overlap, but the order is kept explicit.
        dp.include_routers(
            common.router, advice.router, facts.router, plan.router, chat_settings.router, voice.router, log_text.router
        )  # log_text last: it catches all text
        if settings.bot_mode == "webhook":
            webhook = WebhookHandler(bot, dp, webhook_secret(settings))
            routers.append(webhook.router())

    server = Server(
        uvicorn.Config(
            create_app(settings, sessionmaker, llm, routers, bot=bot),
            host=settings.api_host,
            port=settings.api_port,
            log_level="info",
            proxy_headers=True,  # trusts X-Forwarded-For only from 127.0.0.1 (the reverse proxy / tunnel)
            timeout_graceful_shutdown=10,  # backstop: then uvicorn cancels whatever request still runs
        )
    )

    log.info("API and Mini App on http://localhost:%s", settings.api_port)
    if bot is None or dp is None:
        try:
            await server.serve()
        finally:
            await baselines.cancel_all()
            await llm.aclose()
            await engine.dispose()
        return

    await setup_bot_ui(bot, settings)
    await configure_delivery(bot, dp, settings, webhook.secret if webhook else "")
    polling: asyncio.Task[None] | None = None
    if webhook is None:
        polling = asyncio.create_task(dp.start_polling(bot, handle_signals=False))
        # If polling dies (e.g. the token was revoked), stop the HTTP server too instead of running half-alive.
        polling.add_done_callback(lambda _: setattr(server, "should_exit", True))
    reminders = asyncio.create_task(reminder_loop(bot, sessionmaker, settings, llm))
    try:
        await server.serve()  # returns on Ctrl+C / SIGTERM (uvicorn handles the signals)
    finally:
        await asyncio.shield(shutdown(dp, polling, webhook, reminders, bot, engine, llm))


if __name__ == "__main__":
    asyncio.run(run())
