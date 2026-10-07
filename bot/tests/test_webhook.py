"""Telegram webhook mode: the endpoint, background processing, delivery setup and the new settings."""

import asyncio
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from aiogram import Bot, Dispatcher
from aiogram.methods import SendMessage
from aiogram.types import Update
from conftest import TOKEN, make_settings
from pydantic import ValidationError

from gymbot.api.app import create_app
from gymbot.api.webhook import WEBHOOK_PATH, WebhookHandler
from gymbot.main import configure_delivery, webhook_secret

SECRET = "s3cret_-token"
HEADER = "X-Telegram-Bot-Api-Secret-Token"
UPDATE = {
    "update_id": 1,
    "message": {
        "message_id": 1,
        "date": 0,
        "chat": {"id": 42, "type": "private"},
        "from": {"id": 42, "is_bot": False, "first_name": "A"},
        "text": "hi",
    },
}


class FakeDispatcher:
    """Records fed updates; `gate` (if set) holds feed_update until released; `error` is raised after recording."""

    def __init__(self, error: Exception | None = None, result=None, gate: asyncio.Event | None = None) -> None:
        self.updates: list[Update] = []
        self.fed = asyncio.Event()
        self.error = error
        self.result = result
        self.gate = gate

    async def feed_update(self, bot, update):
        self.updates.append(update)
        self.fed.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        return self.result


@pytest.fixture
async def bot():
    b = Bot(TOKEN)  # no network calls: nothing here talks to Telegram
    yield b
    await b.session.close()


def make_client(settings, db, handler: WebhookHandler) -> httpx.AsyncClient:
    app = create_app(settings, db, routers=[handler.router()])
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


# 1. secret token check


async def test_missing_secret_header_is_403(settings, db, bot):
    dp = FakeDispatcher()
    handler = WebhookHandler(bot, dp, SECRET)
    async with make_client(settings, db, handler) as c:
        r = await c.post(WEBHOOK_PATH, json=UPDATE)
    assert r.status_code == 403
    assert dp.updates == []
    assert not handler.tasks


@pytest.mark.parametrize("value", ["wrong", SECRET + "x", SECRET[:-1], SECRET.upper()])
async def test_wrong_secret_header_is_403(settings, db, bot, value):
    dp = FakeDispatcher()
    handler = WebhookHandler(bot, dp, SECRET)
    async with make_client(settings, db, handler) as c:
        r = await c.post(WEBHOOK_PATH, json=UPDATE, headers={HEADER: value})
    assert r.status_code == 403
    assert dp.updates == []
    assert not handler.tasks


async def test_empty_secret_header_is_403_even_for_body_that_would_be_valid(settings, db, bot):
    dp = FakeDispatcher()
    handler = WebhookHandler(bot, dp, SECRET)
    async with make_client(settings, db, handler) as c:
        r = await c.post(WEBHOOK_PATH, json=UPDATE, headers={HEADER: ""})
    assert r.status_code == 403
    assert dp.updates == []


# 2. valid update


async def test_valid_update_is_acknowledged_and_fed_to_dispatcher(settings, db, bot):
    dp = FakeDispatcher()
    handler = WebhookHandler(bot, dp, SECRET)
    async with make_client(settings, db, handler) as c:
        r = await c.post(WEBHOOK_PATH, json=UPDATE, headers={HEADER: SECRET})
        assert r.status_code == 200
        await asyncio.wait_for(dp.fed.wait(), 2)
    assert len(dp.updates) == 1
    update = dp.updates[0]
    assert isinstance(update, Update)
    assert update.update_id == 1
    assert update.message is not None
    assert update.message.text == "hi"
    assert update.message.chat.id == 42
    assert update.message.bot is bot  # the update is bound to our bot so message.answer() works


async def test_response_does_not_wait_for_the_handler(settings, db, bot):
    gate = asyncio.Event()  # the handler stays busy until we release it
    dp = FakeDispatcher(gate=gate)
    handler = WebhookHandler(bot, dp, SECRET)
    async with make_client(settings, db, handler) as c:
        r = await asyncio.wait_for(c.post(WEBHOOK_PATH, json=UPDATE, headers={HEADER: SECRET}), 2)
        assert r.status_code == 200
        await asyncio.wait_for(dp.fed.wait(), 2)
        assert len(handler.tasks) == 1
        gate.set()
        await handler.drain(2)
    assert not handler.tasks


# 3. invalid bodies


@pytest.mark.parametrize(
    "content",
    [b"not json", b"", b"[1, 2]", b'"text"', b"{}", b'{"update_id": "abc"}', b'{"message": {"text": "hi"}}'],
)
async def test_invalid_body_is_400(settings, db, bot, content):
    dp = FakeDispatcher()
    handler = WebhookHandler(bot, dp, SECRET)
    async with make_client(settings, db, handler) as c:
        r = await c.post(
            WEBHOOK_PATH, content=content, headers={HEADER: SECRET, "Content-Type": "application/json"}
        )
    assert r.status_code == 400
    assert dp.updates == []
    assert not handler.tasks


async def test_secret_is_checked_before_the_body(settings, db, bot):
    handler = WebhookHandler(bot, FakeDispatcher(), SECRET)
    async with make_client(settings, db, handler) as c:
        r = await c.post(WEBHOOK_PATH, content=b"not json", headers={HEADER: "wrong"})
    assert r.status_code == 403


# 4. the Mini App mount must not swallow the webhook route


async def test_webhook_not_shadowed_by_miniapp_mount(tmp_path, db, bot):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html>miniapp</html>")
    settings = make_settings(tmp_path, miniapp_dist=dist)
    dp = FakeDispatcher()
    handler = WebhookHandler(bot, dp, SECRET)
    async with make_client(settings, db, handler) as c:
        r = await c.post(WEBHOOK_PATH, json=UPDATE, headers={HEADER: SECRET})
        assert r.status_code == 200
        await asyncio.wait_for(dp.fed.wait(), 2)
        index = await c.get("/")
        assert index.status_code == 200
        assert "miniapp" in index.text
        bad = await c.post(WEBHOOK_PATH, json=UPDATE, headers={HEADER: "wrong"})
        assert bad.status_code == 403  # still our route, not a StaticFiles 405/404
    assert dp.updates[0].update_id == 1


# 5. process() and drain()


async def test_process_swallows_handler_errors(bot, caplog):
    dp = FakeDispatcher(error=RuntimeError("boom"))
    handler = WebhookHandler(bot, dp, SECRET)
    update = Update.model_validate(UPDATE, context={"bot": bot})
    with caplog.at_level("ERROR", logger="gymbot.api.webhook"):
        await handler.process(update)  # must not raise
    assert dp.updates == [update]
    assert any("failed to process update 1" in r.getMessage() for r in caplog.records)


async def test_process_calls_returned_telegram_method():
    method = SendMessage(chat_id=42, text="x")
    dp = FakeDispatcher(result=method)
    fake = AsyncMock()  # calling the bot with a method = sending it to Telegram
    handler = WebhookHandler(fake, dp, SECRET)
    await handler.process(Update.model_validate(UPDATE))
    fake.assert_awaited_once_with(method)


async def test_error_in_background_task_leaves_handler_working(settings, db, bot):
    dp = FakeDispatcher(error=RuntimeError("boom"))
    handler = WebhookHandler(bot, dp, SECRET)
    async with make_client(settings, db, handler) as c:
        r = await c.post(WEBHOOK_PATH, json=UPDATE, headers={HEADER: SECRET})
        assert r.status_code == 200
        await asyncio.wait_for(dp.fed.wait(), 2)
        await handler.drain(2)
        assert not handler.tasks
        dp.fed.clear()
        r = await c.post(WEBHOOK_PATH, json={**UPDATE, "update_id": 2}, headers={HEADER: SECRET})
        assert r.status_code == 200
        await asyncio.wait_for(dp.fed.wait(), 2)
        await handler.drain(2)
    assert [u.update_id for u in dp.updates] == [1, 2]


async def test_drain_without_tasks_returns_immediately(bot):
    await asyncio.wait_for(WebhookHandler(bot, FakeDispatcher(), SECRET).drain(0), 1)


async def test_drain_waits_for_in_flight_task(settings, db, bot):
    gate = asyncio.Event()
    dp = FakeDispatcher(gate=gate)
    handler = WebhookHandler(bot, dp, SECRET)
    async with make_client(settings, db, handler) as c:
        await c.post(WEBHOOK_PATH, json=UPDATE, headers={HEADER: SECRET})
        await asyncio.wait_for(dp.fed.wait(), 2)
        (task,) = handler.tasks
        drain = asyncio.create_task(handler.drain(5))
        done, _ = await asyncio.wait({drain}, timeout=0.1)
        assert not done  # still waiting for the in-flight update
        assert not task.cancelled()
        gate.set()
        await asyncio.wait_for(drain, 2)
    assert task.done() and not task.cancelled()
    assert not handler.tasks


async def test_drain_cancels_tasks_after_timeout(settings, db, bot):
    dp = FakeDispatcher(gate=asyncio.Event())  # never released
    handler = WebhookHandler(bot, dp, SECRET)
    async with make_client(settings, db, handler) as c:
        await c.post(WEBHOOK_PATH, json=UPDATE, headers={HEADER: SECRET})
        await asyncio.wait_for(dp.fed.wait(), 2)
        (task,) = handler.tasks
        await asyncio.wait_for(handler.drain(0.05), 2)
    assert task.cancelled()
    assert not handler.tasks


# 6-7. configure_delivery


def fake_bot(webhook_url: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        set_webhook=AsyncMock(),
        delete_webhook=AsyncMock(),
        get_webhook_info=AsyncMock(return_value=SimpleNamespace(url=webhook_url)),
    )


async def test_configure_delivery_webhook_mode_sets_webhook(tmp_path):
    settings = make_settings(tmp_path, bot_mode="webhook", public_url="https://gym.example.com/")
    dp = Dispatcher()
    b = fake_bot()
    await configure_delivery(b, dp, settings, SECRET)
    b.set_webhook.assert_awaited_once_with(
        url="https://gym.example.com/telegram/webhook",
        secret_token=SECRET,
        allowed_updates=dp.resolve_used_update_types(),
        drop_pending_updates=False,
    )
    b.delete_webhook.assert_not_awaited()


async def test_configure_delivery_webhook_mode_uses_the_given_secret(tmp_path):
    settings = make_settings(tmp_path, bot_mode="webhook", public_url="https://gym.example.com")
    b = fake_bot()
    await configure_delivery(b, Dispatcher(), settings, "other-secret")
    assert b.set_webhook.await_args.kwargs["secret_token"] == "other-secret"
    assert b.set_webhook.await_args.kwargs["url"] == "https://gym.example.com/telegram/webhook"


async def test_configure_delivery_polling_without_webhook_deletes_nothing(tmp_path):
    settings = make_settings(tmp_path, bot_mode="polling")
    b = fake_bot(webhook_url="")
    await configure_delivery(b, Dispatcher(), settings, "")
    b.get_webhook_info.assert_awaited_once()
    b.set_webhook.assert_not_awaited()
    b.delete_webhook.assert_not_awaited()


async def test_configure_delivery_polling_removes_existing_webhook(tmp_path):
    settings = make_settings(tmp_path, bot_mode="polling")
    b = fake_bot(webhook_url="https://x/telegram/webhook")
    await configure_delivery(b, Dispatcher(), settings, "")
    b.set_webhook.assert_not_awaited()
    b.delete_webhook.assert_awaited_once_with(drop_pending_updates=False)


async def test_configure_delivery_default_mode_is_polling(tmp_path):
    settings = make_settings(tmp_path)
    assert settings.bot_mode == "polling"
    b = fake_bot()
    await configure_delivery(b, Dispatcher(), settings, "")
    b.set_webhook.assert_not_awaited()


# 8. webhook_secret


def test_webhook_secret_returns_configured_value(tmp_path):
    assert webhook_secret(make_settings(tmp_path, webhook_secret="abc_DEF-123")) == "abc_DEF-123"


def test_webhook_secret_generates_valid_random_value(tmp_path):
    settings = make_settings(tmp_path)
    first, second = webhook_secret(settings), webhook_secret(settings)
    for value in (first, second):
        assert re.fullmatch(r"[A-Za-z0-9_-]{1,256}", value)
    assert first != second


# 9. settings


def test_miniapp_url_falls_back_to_public_url(tmp_path):
    settings = make_settings(tmp_path, public_url="https://gym.example.com/")
    assert settings.public_url == "https://gym.example.com"
    assert settings.miniapp_url == "https://gym.example.com"


def test_public_url_is_stripped_of_spaces_and_slashes(tmp_path):
    assert make_settings(tmp_path, public_url="  https://gym.example.com//  ").public_url == "https://gym.example.com"


def test_explicit_miniapp_url_is_kept(tmp_path):
    settings = make_settings(tmp_path, public_url="https://gym.example.com", miniapp_url="https://app.example.com/x")
    assert settings.miniapp_url == "https://app.example.com/x"
    assert settings.public_url == "https://gym.example.com"


def test_empty_urls_stay_empty(tmp_path):
    settings = make_settings(tmp_path)
    assert settings.public_url == ""
    assert settings.miniapp_url == ""


@pytest.mark.parametrize("value", ["a b", "a/b", "a+b", "ключ", "a" * 257])
def test_webhook_secret_with_invalid_value_is_rejected(tmp_path, value):
    with pytest.raises(ValidationError):
        make_settings(tmp_path, webhook_secret=value)


def test_webhook_secret_allows_max_length(tmp_path):
    assert make_settings(tmp_path, webhook_secret="a" * 256).webhook_secret == "a" * 256


def test_bot_mode_rejects_unknown_value(tmp_path):
    with pytest.raises(ValidationError):
        make_settings(tmp_path, bot_mode="longpoll")
