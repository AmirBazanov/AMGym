"""Body weight from the chat: "вес 84.6" -> "Записать вес? 84,6 кг" -> "✅ Сохранить" -> saved.

process_text (handlers/log_text.py) calls `offer` for a message that is only a weigh-in
(gymbot.services.body_weight.parse_chat) instead of the parser, unless a preview or the model's question is
open: then "вес 85" may answer it. Nothing is written before the tap; previews live in memory (PENDING, by
token) for TTL. The router is attached to log_text.router (see there), so main.py needs no change.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from gymbot.config import Settings
from gymbot.db.session import Sessionmaker
from gymbot.services import body_weight as bw
from gymbot.services import live
from gymbot.services.users import get_or_create_user

router = Router(name="body_weight")

STALE = "Эта запись уже сохранена или устарела."
MAX_PENDING = 200
TTL = timedelta(minutes=15)


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class WeightPending:
    user_id: int  # Telegram id
    weight_kg: Decimal
    raw_text: str
    sent_at: datetime  # the message's time (UTC): its local day is the measurement's day


PENDING: dict[str, WeightPending] = {}


def keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="✅ Сохранить", callback_data=f"bwsave:{token}"),
            InlineKeyboardButton(text="✖ Отмена", callback_data=f"bwdrop:{token}"),
        ]]
    )


async def offer(message: Message, weight_kg: Decimal, raw_text: str, prefix: str = "") -> None:
    """The "Записать вес?" preview with its buttons; nothing is written yet."""
    if len(PENDING) >= MAX_PENDING:
        PENDING.pop(next(iter(PENDING)))
    token = secrets.token_hex(6)
    PENDING[token] = WeightPending(message.from_user.id, weight_kg, raw_text, message.date)  # type: ignore[union-attr]
    await message.answer(f"{prefix}Записать вес? {bw.kg_text(weight_kg)} кг", reply_markup=keyboard(token))


def _take(cb: CallbackQuery) -> WeightPending | None:
    """Pop the caller's own fresh preview (pop: a double tap must not save twice); someone else's stays."""
    token = (cb.data or "").split(":", 1)[1]
    pending = PENDING.pop(token, None)
    if pending is not None and pending.user_id != cb.from_user.id:
        PENDING[token] = pending
        return None
    if pending is not None and utcnow() - pending.sent_at > TTL:
        return None
    return pending


def saved_text(saved: bw.Saved) -> str:
    text = f"Вес {bw.kg_text(saved.row.weight_kg)} кг записан ✅"
    if saved.replaced is not None and saved.replaced != saved.row.weight_kg:
        text += f"\nЗаменил {bw.kg_text(saved.replaced)} кг за {saved.row.day:%d.%m}."
    if saved.before is not None:
        diff = saved.row.weight_kg - saved.before.weight_kg
        sign = "+" if diff > 0 else ""
        text += f"\n{sign}{bw.kg_text(diff)} кг к {saved.before.day:%d.%m} ({bw.kg_text(saved.before.weight_kg)} кг)."
    return text


@router.callback_query(F.data.startswith("bwsave:"))
async def save_weight(cb: CallbackQuery, settings: Settings, sessionmaker: Sessionmaker) -> None:
    pending = _take(cb)
    if pending is None:
        await cb.answer(STALE, show_alert=True)
        return
    token = (cb.data or "").split(":", 1)[1]
    tz = ZoneInfo(settings.timezone)
    try:
        async with sessionmaker() as session:
            user = await get_or_create_user(session, cb.from_user.id, cb.from_user.full_name)
            day = pending.sent_at.astimezone(tz).date()
            saved = await bw.upsert(
                session, user, day, pending.weight_kg, pending.sent_at, "chat", raw_text=pending.raw_text
            )
            text = saved_text(saved)
            await session.commit()
    except Exception:
        PENDING[token] = pending  # let the user press again
        raise
    live.publish(user.id, "weight", "state")
    if cb.message:
        await cb.message.edit_text(text)  # type: ignore[union-attr]
    await cb.answer()


@router.callback_query(F.data.startswith("bwdrop:"))
async def drop_weight(cb: CallbackQuery) -> None:
    if _take(cb) is None:
        await cb.answer(STALE, show_alert=True)
        return
    if cb.message:
        await cb.message.edit_text("Отменено.")  # type: ignore[union-attr]
    await cb.answer()
