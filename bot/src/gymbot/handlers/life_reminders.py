"""Buttons under a «Дела» plan reminder (gymbot.services.life_reminders): «✅ Сделано» marks the plan done,
«+30 мин» moves its start and end 30 minutes later, so it is reminded again before the new start.

Only the owner's plans. A plan gone, done, or already moved from this message answers STALE: «+30 мин» works
while the plan is still marked reminded, and the move clears that mark (life.update), so a second tap does not
move it twice.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, time

from aiogram import F, Router
from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from gymbot.config import Settings
from gymbot.db.models import LifeEvent, User
from gymbot.db.session import Sessionmaker
from gymbot.services import life
from gymbot.services.access import owner_user
from gymbot.services.life_reminders import SNOOZE
from gymbot.services.reminders import hhmm_to_minute

log = logging.getLogger(__name__)
router = Router(name="life_reminders")

STALE = "Устарело"


def utcnow() -> datetime:
    return datetime.now(UTC)


def _id(data: str | None) -> int | None:
    try:
        return int((data or "").split(":", 1)[1])
    except (IndexError, ValueError):
        return None


def snoozed(event: LifeEvent) -> dict[str, object]:
    """The fields of `event` moved by SNOOZE: start (and the day past midnight), end; an end that would wrap past
    midnight while the start does not is dropped (the end must stay after the start)."""
    assert event.start is not None
    minute = hhmm_to_minute(event.start)
    start = datetime.combine(event.day, time(minute // 60, minute % 60)) + SNOOZE
    fields: dict[str, object] = {"start": f"{start:%H:%M}"}
    if start.date() != event.day:
        fields["day"] = start.date()
    if event.end is not None:
        m = hhmm_to_minute(event.end)
        end = datetime.combine(event.day, time(m // 60, m % 60)) + SNOOZE
        fields["end"] = f"{end:%H:%M}" if end.date() == start.date() else None
    return fields


def _when(day: date, old: date, start: str) -> str:
    return start if day == old else f"{day:%d.%m} {start}"


async def _owner_event(
    cb: CallbackQuery, settings: Settings, session: AsyncSession, event_id: int | None
) -> tuple[User | None, LifeEvent | None]:
    owner = await owner_user(session, settings)
    if owner is None or event_id is None or cb.from_user.id != owner.telegram_id:
        return None, None
    return owner, await life.get(session, owner.id, "event", event_id)


@router.callback_query(F.data.startswith("lrdone:"))
async def on_done(cb: CallbackQuery, settings: Settings, sessionmaker: Sessionmaker) -> None:
    async with sessionmaker() as session:
        owner, event = await _owner_event(cb, settings, session, _id(cb.data))
        if owner is None or event is None or event.done_at is not None:
            await cb.answer(STALE)
            return
        await life.set_done(session, owner.id, "event", event.id, True, utcnow())
        title = event.title
        await life.commit(session, owner.id)
    await cb.answer("Готово")
    if cb.message:
        await cb.message.edit_text(f"✅ Сделано: {title}")  # type: ignore[union-attr]


@router.callback_query(F.data.startswith("lrsnooze:"))
async def on_snooze(cb: CallbackQuery, settings: Settings, sessionmaker: Sessionmaker) -> None:
    async with sessionmaker() as session:
        owner, event = await _owner_event(cb, settings, session, _id(cb.data))
        if (owner is None or event is None or event.done_at is not None or event.start is None
                or event.reminded_at is None):
            await cb.answer(STALE)
            return
        fields = snoozed(event)
        old_day, title = event.day, event.title
        try:
            await life.update(session, owner.id, "event", event.id, fields, utcnow())  # clears reminded_at
        except ValueError as e:
            await session.rollback()
            await cb.answer(str(e))
            return
        await life.commit(session, owner.id)
        when = _when(event.day, old_day, str(fields["start"]))
    await cb.answer(f"Перенёс на {when}")
    if cb.message:
        await cb.message.edit_text(f"Перенёс на {when}: {title}")  # type: ignore[union-attr]
