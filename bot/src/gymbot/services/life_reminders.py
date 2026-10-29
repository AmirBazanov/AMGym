"""«Дела» reminders: a timed plan shortly before it starts, a deadline on its eve.

Run by gymbot.services.reminders.reminder_loop on every check (no loop of its own), for the owner only
(gymbot.services.access.owner_user). Times follow gymbot.services.reminders: local wall clock in TIMEZONE,
compared on the UTC timeline.

- A plan (life_events) with a start time is due when `start - remind_before <= now < start` (remind_before
  NULL: users.life_remind_before) and `reminded_at` is NULL; plans without a time and the weekly schedule
  (life_slots) are not reminded. Buttons «✅ Сделано» / «+30 мин» (gymbot.handlers.life_reminders).
- Deadlines due tomorrow get one message at EVE_MINUTE (20:00) local, marked in `reminded_on`.

Like reminders, every message is claimed before it is sent by a conditional UPDATE (committed), so two checks
in one minute or two processes send it once. A transient send error releases the claim (the next check
inside the window retries); a permanent one (bot blocked, chat not found) keeps it.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from sqlalchemy import or_, update
from sqlalchemy.ext.asyncio import AsyncSession

from gymbot.config import Settings
from gymbot.db.models import LifeDeadline, LifeEvent, User
from gymbot.db.session import Sessionmaker
from gymbot.services import life
from gymbot.services.access import owner_user
from gymbot.services.reminders import due_at, due_day, hhmm_to_minute

log = logging.getLogger(__name__)

# send(chat_id, text, reply_markup=..., parse_mode=...): Bot.send_message.
Send = Callable[..., Awaitable[Any]]

EVE_MINUTE = 20 * 60  # deadlines of tomorrow are reminded at 20:00 local
SNOOZE = timedelta(minutes=30)
OPEN_LIFE = "Открыть дела"
DONE = "✅ Сделано"
SNOOZE_TEXT = "+30 мин"


def life_url(settings: Settings) -> str | None:
    """The Mini App opened on «Дела» (?section=life), None without a Mini App URL."""
    url = settings.miniapp_url
    if not url:
        return None
    return url + ("&" if "?" in url else "?") + "section=life"


def open_kb(settings: Settings) -> InlineKeyboardMarkup | None:
    url = life_url(settings)
    if url is None:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=OPEN_LIFE, web_app=WebAppInfo(url=url))]])


def event_kb(event_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text=DONE, callback_data=f"lrdone:{event_id}"),
            InlineKeyboardButton(text=SNOOZE_TEXT, callback_data=f"lrsnooze:{event_id}"),
        ]]
    )


# ---- plans ----


def event_start(day: date, start: str, tz: ZoneInfo) -> datetime:
    """The UTC moment of a plan's local `start` ("ЧЧ:ММ") on `day`."""
    return due_at(day, hhmm_to_minute(start), tz).astimezone(UTC)


def due_events(view: life.LifeView, now_local: datetime, default_before: int) -> list[LifeEvent]:
    """Timed, open, not yet reminded plans whose reminder window `start - before <= now < start` is open now.
    `now_local` is aware (its tzinfo is TIMEZONE, the zone of the plans' dates and times)."""
    tz = now_local.tzinfo
    assert isinstance(tz, ZoneInfo)
    now = now_local.astimezone(UTC)
    out = []
    for e in view.events:
        if e.start is None or e.done_at is not None or e.reminded_at is not None:
            continue
        start = event_start(e.day, e.start, tz)
        before = timedelta(minutes=default_before if e.remind_before is None else e.remind_before)
        if start - before <= now < start:
            out.append(e)
    return out


def minutes_left(start_utc: datetime, now_utc: datetime) -> int:
    return max(1, math.ceil((start_utc - now_utc).total_seconds() / 60))


def _in(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes} мин"
    hours, rest = divmod(minutes, 60)
    return f"{hours} ч" + (f" {rest} мин" if rest else "")


def event_text(title: str, start: str, minutes: int) -> str:
    """«Через 30 мин (11:00): Позаниматься HTTP-сервером на C» (plain text)."""
    return f"Через {_in(minutes)} ({start}): {title}"


def deadlines_text(titles: list[str]) -> str:
    """The eve message: «Завтра дедлайн: …» or a list (plain text)."""
    if len(titles) == 1:
        return f"Завтра дедлайн: {titles[0]}"
    return "Завтра дедлайны:\n" + "\n".join(f"• {t}" for t in titles)


def _deadline_title(d: LifeDeadline) -> str:
    return d.title + (f" ({d.course})" if d.course else "")


# ---- the check ----


@dataclass
class _Claim:
    """A message to send once its claim succeeded; `release` undoes the claim after a transient send error."""

    what: str  # for the log
    text: str
    reply_markup: InlineKeyboardMarkup | None
    claim: Any  # an UPDATE statement
    release: Any


async def _run_claim(session: AsyncSession, stmt: Any) -> int:
    result = await session.execute(stmt.execution_options(synchronize_session=False))
    await session.commit()
    return int(result.rowcount)  # type: ignore[attr-defined]


def _event_claims(view: life.LifeView, user: User, now: datetime, tz: ZoneInfo) -> list[_Claim]:
    out = []
    for e in due_events(view, now.astimezone(tz), user.life_remind_before):
        assert e.start is not None
        claim = (
            update(LifeEvent)
            .where(
                LifeEvent.id == e.id, LifeEvent.reminded_at.is_(None), LifeEvent.done_at.is_(None),
                LifeEvent.deleted_at.is_(None), LifeEvent.day == e.day, LifeEvent.start == e.start,
            )
            .values(reminded_at=now)
        )
        # Only the row we marked (a snooze in between resets it to NULL, and then there is nothing to undo).
        release = (
            update(LifeEvent)
            .where(LifeEvent.id == e.id, LifeEvent.reminded_at.is_not(None), LifeEvent.day == e.day,
                   LifeEvent.start == e.start)
            .values(reminded_at=None)
        )
        text = event_text(e.title, e.start, minutes_left(event_start(e.day, e.start, tz), now))
        out.append(_Claim(f"plan {e.id}", text, event_kb(e.id), claim, release))
    return out


async def _deadline_claim(
    session: AsyncSession, view: life.LifeView, user: User, now: datetime, tz: ZoneInfo, settings: Settings
) -> _Claim | None:
    """One message for all deadlines of tomorrow not yet reminded; each row is claimed on its own."""
    eve = due_day(EVE_MINUTE, now, tz)
    if eve is None:
        return None
    tomorrow = eve + timedelta(days=1)
    rows = [d for d in view.deadlines
            if d.day == tomorrow and d.done_at is None and (d.reminded_on is None or d.reminded_on < eve)]
    if not rows:
        return None
    claimed: list[LifeDeadline] = []
    for d in rows:
        stmt = (
            update(LifeDeadline)
            .where(
                LifeDeadline.id == d.id, LifeDeadline.day == tomorrow, LifeDeadline.done_at.is_(None),
                LifeDeadline.deleted_at.is_(None),
                or_(LifeDeadline.reminded_on.is_(None), LifeDeadline.reminded_on < eve),
            )
            .values(reminded_on=eve)
        )
        if await _run_claim(session, stmt) == 1:
            claimed.append(d)
    if not claimed:
        return None
    ids = [d.id for d in claimed]
    release = (
        update(LifeDeadline).where(LifeDeadline.id.in_(ids), LifeDeadline.reminded_on == eve).values(reminded_on=None)
    )
    text = deadlines_text([_deadline_title(d) for d in claimed])
    return _Claim(f"deadlines {ids}", text, open_kb(settings), None, release)


async def _deliver(session: AsyncSession, send: Send, chat_id: int, item: _Claim) -> bool:
    try:
        await send(chat_id, item.text, reply_markup=item.reply_markup)
    except (TelegramForbiddenError, TelegramBadRequest) as e:
        log.warning("life %s: not delivered, not retried: %s", item.what, e)
        return False
    except Exception:
        log.exception("life %s: send failed, will retry", item.what)
        try:
            await _run_claim(session, item.release)
        except Exception:
            await session.rollback()
            log.exception("life %s: failed to release the claim", item.what)
        return False
    return True


async def tick(send: Send | None, sessionmaker: Sessionmaker, settings: Settings, now_utc: datetime) -> int:
    """One check of the owner's «Дела»: plans due for a reminder, tomorrow's deadlines at 20:00. Returns how many messages were sent. Without `send` (no bot: RUN_BOT=false) nothing is done."""
    if send is None:
        return 0
    tz = ZoneInfo(settings.timezone)
    now = now_utc.astimezone(UTC)
    today = now.astimezone(tz).date()
    sent = 0
    async with sessionmaker() as session:
        user = await owner_user(session, settings)
        if user is None:
            return 0
        chat_id = user.telegram_id
        view = await life.snapshot(session, user.id, today)
        items = _event_claims(view, user, now, tz)
        deadline = await _deadline_claim(session, view, user, now, tz, settings)
        for item in items:
            if await _run_claim(session, item.claim) != 1:
                continue  # sent by another check, or moved / done / deleted meanwhile
            sent += await _deliver(session, send, chat_id, item)
        if deadline is not None:  # already claimed
            sent += await _deliver(session, send, chat_id, deadline)
    return sent
