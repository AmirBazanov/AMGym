"""Daily reminders: a plain asyncio loop over state in the DB (no APScheduler).

A reminder is due when `due <= now < due + GRACE` on the UTC timeline, where `due` is `minute_of_day`
on a local day in TIMEZONE (a time in the spring DST gap fires an hour later on the wall clock; a
repeated autumn time fires once, at its first occurrence). Duplicates are prevented by claiming the
reminder before sending: a conditional `UPDATE ... SET last_sent_on = :day WHERE last_sent_on IS NULL OR
last_sent_on < :day AND enabled AND minute_of_day = :minute`, committed, and only the caller whose UPDATE
changed the row sends. This is atomic on SQLite and Postgres, also with two processes.

Delivery is "at most once per claim", not strictly at most once per day:
- a crash between claim and send loses that day's reminder;
- a transient send error (network, timeout, flood control) reverts the claim, and the next check within
  GRACE retries; if the message did reach Telegram before the error (e.g. a read timeout), the user gets
  it twice;
- permanent errors (bot blocked, chat not found) keep the claim: no retries until the next day.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from gymbot.config import Settings
from gymbot.db.models import Reminder, User
from gymbot.db.session import Sessionmaker
from gymbot.services.access import is_allowed
from gymbot.services.nutrition import day_summary

log = logging.getLogger(__name__)

GRACE = timedelta(minutes=30)
CHECK_SECONDS = 30
MAX_PER_USER = 20
KINDS = ("text", "nutrition")

NO_TARGETS = "Норма КБЖУ не задана, задай её в дневнике → Питание → Настройки"
CLOSED = "КБЖУ на сегодня закрыт"
OPEN_NUTRITION = "Открыть питание"


def minute_to_hhmm(minute: int) -> str:
    return f"{minute // 60:02d}:{minute % 60:02d}"


def hhmm_to_minute(value: str) -> int:
    """'09:30' -> 570. The format is validated by the API schema."""
    h, m = value.split(":")
    return int(h) * 60 + int(m)


def due_at(day: date, minute: int, tz: ZoneInfo) -> datetime:
    """The moment `minute` local time on the local day `day` (aware, in `tz`)."""
    return datetime.combine(day, time(minute // 60, minute % 60), tzinfo=tz)


def due_day(minute: int, now_utc: datetime, tz: ZoneInfo) -> date | None:
    """Local day whose occurrence of `minute` is inside the send window now, else None.

    Yesterday is checked too: a 23:50 reminder stays due until 00:20 the next local day.
    """
    # Compare in UTC: aware datetimes sharing one tzinfo compare by wall clock, which breaks on DST days.
    now = now_utc.astimezone(UTC)
    today = now.astimezone(tz).date()
    for day in (today, today - timedelta(days=1)):
        due = due_at(day, minute, tz).astimezone(UTC)
        if due <= now < due + GRACE:
            return day
    return None


def initial_last_sent(minute: int, now_utc: datetime, tz: ZoneInfo) -> date:
    """`last_sent_on` for a reminder created (or re-timed / re-enabled) now.

    If today's time has already passed, mark today as done, otherwise the reminder would fire right
    away instead of from tomorrow. Otherwise mark yesterday, so a late-evening window that spills over
    midnight does not fire for yesterday either.
    """
    now = now_utc.astimezone(UTC)
    today = now.astimezone(tz).date()
    return today if due_at(today, minute, tz).astimezone(UTC) <= now else today - timedelta(days=1)


def nutrition_text(remaining_kcal: float | None, remaining_protein: float | None) -> str:
    """'Добей КБЖУ: осталось 760 ккал / 48 г белка'. Only kcal and protein are reminded about."""
    if remaining_kcal is None and remaining_protein is None:
        return NO_TARGETS
    parts = []
    if remaining_kcal is not None and round(remaining_kcal) > 0:
        parts.append(f"{round(remaining_kcal)} ккал")
    if remaining_protein is not None and round(remaining_protein) > 0:
        parts.append(f"{round(remaining_protein)} г белка")
    if not parts:
        return CLOSED
    return "Добей КБЖУ: осталось " + " / ".join(parts)


def nutrition_keyboard(miniapp_url: str) -> InlineKeyboardMarkup | None:
    # Inline (under the message), not a reply keyboard: that would replace the user's keyboard.
    if not miniapp_url:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=OPEN_NUTRITION, web_app=WebAppInfo(url=miniapp_url))]]
    )


@dataclass
class _Due:
    """Plain snapshot of a candidate row: ORM objects may expire on the commits inside tick()."""

    id: int
    user_id: int
    chat_id: int
    minute: int
    kind: str
    text: str | None
    previous: date | None
    day: date


@dataclass
class _Message:
    text: str
    reply_markup: InlineKeyboardMarkup | None = None


async def _build(session: AsyncSession, item: _Due, tz: ZoneInfo, miniapp_url: str) -> _Message:
    if item.kind == "nutrition":
        user = await session.get(User, item.user_id)
        assert user is not None  # FK with ON DELETE CASCADE
        s = await day_summary(session, user, item.day, tz)
        return _Message(nutrition_text(s.remaining.kcal, s.remaining.protein), nutrition_keyboard(miniapp_url))
    return _Message(item.text or "Напоминание")


async def _claim(session: AsyncSession, item: _Due) -> bool:
    """Atomically mark today as sent, unless already sent, disabled or re-timed since the snapshot."""
    result = await session.execute(
        update(Reminder)
        .where(
            Reminder.id == item.id,
            Reminder.enabled.is_(True),
            Reminder.minute_of_day == item.minute,
            or_(Reminder.last_sent_on.is_(None), Reminder.last_sent_on < item.day),
        )
        .values(last_sent_on=item.day)
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    return result.rowcount == 1  # type: ignore[attr-defined]


async def _release(session: AsyncSession, reminder_id: int, day: date, previous: date | None) -> None:
    """Undo our own claim only (the row may have been edited meanwhile)."""
    await session.execute(
        update(Reminder)
        .where(Reminder.id == reminder_id, Reminder.last_sent_on == day)
        .values(last_sent_on=previous)
        .execution_options(synchronize_session=False)
    )
    await session.commit()


async def tick(session: AsyncSession, bot: Bot, now_utc: datetime, tz: ZoneInfo, *, settings: Settings) -> int:
    """One check: send every reminder that is due now. Returns how many were sent.

    Only users who may use the app (ALLOWED_USER_IDS / owner, same rule as the handlers) get reminders.
    `settings.miniapp_url` is read here, at send time.
    """
    today = now_utc.astimezone(tz).date()
    rows = (
        await session.execute(
            select(Reminder, User)
            .join(User, Reminder.user_id == User.id)
            .where(
                Reminder.enabled.is_(True),
                or_(Reminder.last_sent_on.is_(None), Reminder.last_sent_on < today),
            )
            .order_by(Reminder.id)
        )
    ).all()  # materialize: no open cursor while committing below
    due: list[_Due] = []
    for reminder, user in rows:
        day = due_day(reminder.minute_of_day, now_utc, tz)
        if day is None or (reminder.last_sent_on is not None and reminder.last_sent_on >= day):
            continue
        due.append(
            _Due(
                reminder.id, user.id, user.telegram_id, reminder.minute_of_day, reminder.kind, reminder.text,
                reminder.last_sent_on, day,
            )
        )
    allowed: dict[int, bool] = {}
    for item in due:
        if item.chat_id not in allowed:
            allowed[item.chat_id] = await is_allowed(session, settings, item.chat_id)
    due = [item for item in due if allowed[item.chat_id]]
    miniapp_url = settings.miniapp_url
    sent = 0
    for item in due:
        try:
            # Build before claiming, so a failing summary does not leave a dangling claim.
            message = await _build(session, item, tz, miniapp_url)
            if not await _claim(session, item):
                continue  # sent by another check/process, or disabled/re-timed meanwhile
        except Exception:
            await session.rollback()
            log.exception("reminder %s: failed to prepare", item.id)
            continue
        try:
            await bot.send_message(item.chat_id, message.text, reply_markup=message.reply_markup)
        except (TelegramForbiddenError, TelegramBadRequest) as e:
            # Permanent for today (bot blocked, chat not found): keep the claim, retrying would only spam logs.
            log.warning("reminder %s: not delivered to %s, skipped for today: %s", item.id, item.chat_id, e)
            continue
        except Exception:
            log.exception("reminder %s: send failed, will retry within the grace window", item.id)
            try:
                await _release(session, item.id, item.day, item.previous)
            except Exception:
                await session.rollback()
                log.exception("reminder %s: failed to release the claim", item.id)
            continue
        sent += 1
    return sent


async def reminder_loop(bot: Bot, sessionmaker: Sessionmaker, settings: Settings) -> None:
    """Check every CHECK_SECONDS until cancelled. One failed check is logged and does not stop the loop."""
    tz = ZoneInfo(settings.timezone)
    log.info("reminders: checking every %s s", CHECK_SECONDS)
    while True:
        try:
            async with sessionmaker() as session:
                sent = await tick(session, bot, datetime.now(UTC), tz, settings=settings)
            if sent:
                log.info("reminders: sent %s", sent)
        except Exception:
            log.exception("reminders: check failed")
        await asyncio.sleep(CHECK_SECONDS)
