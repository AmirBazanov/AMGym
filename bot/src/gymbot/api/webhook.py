"""Telegram webhook endpoint: Telegram POSTs updates here (BOT_MODE=webhook) instead of us long-polling."""

import asyncio
import contextlib
import hmac
import logging
from typing import Annotated, Any

from aiogram import Bot, Dispatcher
from aiogram.methods import TelegramMethod
from aiogram.types import Update
from fastapi import APIRouter, Header, HTTPException, Request, Response
from pydantic import ValidationError

WEBHOOK_PATH = "/telegram/webhook"

log = logging.getLogger(__name__)


class WebhookHandler:
    """Answers Telegram with 200 right away and feeds the update to the dispatcher in the background.

    Telegram waits up to 60 s and resends on timeout; LLM parsing can take a while, so handlers must
    not block the response. Task references are kept so they are not garbage-collected mid-flight.
    """

    def __init__(self, bot: Bot, dp: Dispatcher, secret: str) -> None:
        self.bot = bot
        self.dp = dp
        self.secret = secret
        self.tasks: set[asyncio.Task[None]] = set()

    async def process(self, update: Update) -> None:
        try:
            result = await self.dp.feed_update(self.bot, update)
            # A handler may return a method meant as the webhook reply; we already answered, so call it.
            if isinstance(result, TelegramMethod):
                await self.bot(result)
        except Exception:
            log.exception("failed to process update %s", update.update_id)

    def router(self) -> APIRouter:
        router = APIRouter()

        @router.post(WEBHOOK_PATH, include_in_schema=False)
        async def telegram_webhook(
            request: Request,
            x_telegram_bot_api_secret_token: Annotated[str, Header()] = "",
        ) -> Response:
            if not hmac.compare_digest(x_telegram_bot_api_secret_token.encode(), self.secret.encode()):
                raise HTTPException(403, "bad secret token")
            try:
                payload: Any = await request.json()
                update = Update.model_validate(payload, context={"bot": self.bot})
            except (ValueError, ValidationError) as e:
                raise HTTPException(400, "not a Telegram update") from e
            task = asyncio.create_task(self.process(update))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
            return Response(status_code=200)

        return router

    async def drain(self, timeout: float) -> None:
        """On shutdown: let in-flight updates finish (they were already acknowledged), then cancel the rest."""
        if not self.tasks:
            return
        pending = list(self.tasks)
        _, still = await asyncio.wait(pending, timeout=timeout)
        for t in still:
            t.cancel()
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await asyncio.gather(*still, return_exceptions=True)
