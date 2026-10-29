"""The dashboard «Дела Амира» from the bot: /hub opens it as a Mini App, and tasks and house chores said in the chat
(text or voice: «добавь задачу купить билеты», «вынес мусор») get a preview with "✅ Применить"
(gymbot.services.hub_tasks).

process_text (handlers/log_text.py) asks `stage` first among the commands; a message the model turned into at
least one valid action is answered here, anything else (not ours, no action, any error) goes on as usual. Only
the owner: the dashboard's state is theirs (gymbot.services.access.owner_user).

Nothing is written until "✅ Применить": it loads the state, applies the actions to it and saves it with the
version it read (gymbot.services.hub.save_state); a save from a device in between (409) is retried on the
newer state, which is the merge. New task ids are minted at stage time, previews live in memory (PENDING) for
TTL and are taken on the first tap, so a second tap or a restart answers STALE.
"""

from __future__ import annotations

import logging
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo

from gymbot.config import Settings
from gymbot.db.session import Sessionmaker
from gymbot.llm.openrouter import OpenRouterClient
from gymbot.llm.prompts_hub import build_hub_messages
from gymbot.services import hub, live
from gymbot.services import hub_tasks as ht
from gymbot.services.access import owner_user

log = logging.getLogger(__name__)
router = Router(name="hub")

STALE = "Устарело, повтори команду"
NOT_SET = "Дашборд не настроен: нет адреса мини-аппа (PUBLIC_URL)."
FAILED = "Не получилось сохранить, попробуй ещё раз."
OPEN = "Дела: задачи, дом, расписание."
TTL = timedelta(minutes=15)
MAX_PENDING = 100
SAVE_ATTEMPTS = 3


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class HubPending:
    user_id: int  # Telegram id
    raw: str  # how the message is kept (voice: "[voice] …"), logged with the applied actions
    actions: list[ht.Action]
    lines: list[str]
    notes: list[str]
    sent_at: datetime


PENDING: dict[str, HubPending] = {}


def hub_url(settings: Settings) -> str | None:
    base = settings.miniapp_url or settings.public_url
    return f"{base.rstrip('/')}/hub/" if base else None


def open_kb(settings: Settings) -> InlineKeyboardMarkup | None:
    """An inline web_app button: the dashboard gets Telegram initData (a reply-keyboard button would not)."""
    url = hub_url(settings)
    if url is None:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Открыть дела", web_app=WebAppInfo(url=url))]])


def apply_kb(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="✅ Применить", callback_data=f"happly:{token}"),
            InlineKeyboardButton(text="Отмена", callback_data=f"hdrop:{token}"),
        ]]
    )


@router.message(Command("hub"))
async def cmd_hub(message: Message, settings: Settings) -> None:
    kb = open_kb(settings)
    await message.answer(OPEN if kb else NOT_SET, reply_markup=kb)


def _today(at: datetime, settings: Settings) -> date:
    return at.astimezone(ZoneInfo(settings.timezone)).date()


def _new_id() -> str:
    return f"u{time.time_ns() // 1_000_000}{secrets.randbelow(1000):03d}"  # app.js mints 'u' + Date.now()


async def stage(
    message: Message, text: str, raw: str, settings: Settings, sessionmaker: Sessionmaker, llm: OpenRouterClient
) -> HubPending | None:
    """The model's tasks and chores in `text`, for the owner only; None when not ours or on any error."""
    tg = message.from_user
    if tg is None or settings.hub_dir is None or not ht.is_hub_command(text):
        return None
    try:
        async with sessionmaker() as session:
            owner = await owner_user(session, settings)
            if owner is None or owner.telegram_id != tg.id:
                return None
            state = (await hub.get_state(session, owner.id))["state"]
        site = ht.read_site(settings.hub_dir)
        today = _today(message.date, settings)
        context = ht.prompt_context(state, site, today)
        data = await llm.complete_json(build_hub_messages(text, context), prefer="actions", purpose="json")
        actions, notes = ht.parse_actions(data, state, site, today, _new_id)
    except Exception as e:  # noqa: BLE001 - a failing dashboard path must never cost the user a record
        log.warning("hub command failed (%s), the usual path", type(e).__name__)
        return None
    if not actions:
        return None
    return HubPending(tg.id, raw, actions, ht.describe(actions, state, site, today), notes, message.date)


def render(pending: HubPending) -> str:
    tail = "\n\n" + "\n".join(pending.notes) if pending.notes else ""
    return "В дела:\n" + "\n".join(f"• {line}" for line in pending.lines) + tail


async def send(message: Message, pending: HubPending, *, prefix: str = "") -> None:
    now = pending.sent_at
    for token in [t for t, p in PENDING.items() if now - p.sent_at > TTL]:
        PENDING.pop(token, None)
    while len(PENDING) >= MAX_PENDING:
        PENDING.pop(next(iter(PENDING)))
    token = secrets.token_urlsafe(8)
    PENDING[token] = pending
    await message.answer(prefix + render(pending), reply_markup=apply_kb(token))


async def save(settings: Settings, sessionmaker: Sessionmaker, actions: list[ht.Action]) -> int | None:
    """Apply `actions` to the stored state and save it; the owner's user id, None without an owner."""
    site = ht.read_site(settings.hub_dir)
    for attempt in range(SAVE_ATTEMPTS):
        async with sessionmaker() as session:
            owner = await owner_user(session, settings)
            if owner is None:
                return None
            current = await hub.get_state(session, owner.id)
            new = ht.apply(current["state"], actions, site)
            try:
                await hub.save_state(session, owner.id, current["version"], new, utcnow().replace(microsecond=0))
            except hub.Conflict:
                if attempt == SAVE_ATTEMPTS - 1:
                    raise
                continue  # a device saved in between: apply again on its state
            return owner.id
    return None


@router.callback_query(F.data.startswith("happly:"))
async def on_apply(cb: CallbackQuery, settings: Settings, sessionmaker: Sessionmaker) -> None:
    token = (cb.data or "").split(":", 1)[1]
    pending = PENDING.get(token)
    if pending is None or cb.from_user.id != pending.user_id:
        await cb.answer(STALE)
        return
    PENDING.pop(token, None)  # the first tap takes it: a second one is STALE, nothing is added twice
    if utcnow() - pending.sent_at > TTL:
        await cb.answer(STALE)
        return
    try:
        owner_id = await save(settings, sessionmaker, pending.actions)
    except hub.Conflict:
        await cb.answer(FAILED)
        return
    if owner_id is None:
        await cb.answer(STALE)
        return
    live.publish(owner_id, "hub")
    log.info("hub actions from the chat %r: %s", pending.raw, [a.kind for a in pending.actions])
    await cb.answer("Готово")
    if cb.message:
        text = "Готово:\n" + "\n".join(f"• {line}" for line in pending.lines)
        await cb.message.edit_text(text, reply_markup=open_kb(settings))  # type: ignore[union-attr]


@router.callback_query(F.data.startswith("hdrop:"))
async def on_drop(cb: CallbackQuery) -> None:
    token = (cb.data or "").split(":", 1)[1]
    pending = PENDING.get(token)
    if pending is not None and cb.from_user.id == pending.user_id:
        PENDING.pop(token, None)
    await cb.answer()
    if cb.message:
        await cb.message.edit_text("Отменено.")  # type: ignore[union-attr]
