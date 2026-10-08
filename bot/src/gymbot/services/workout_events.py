"""What follows a saved workout, after its commit: new personal records (gymbot.services.records) and, at
most once in a while, the deload offer (gymbot.services.deload). Used by the chat save, POST /api/workouts
and a saved edit of sets. Best-effort: never raises, a failure here must not look like a failed save.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Collection
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram.types import InlineKeyboardMarkup

from gymbot.db.session import Sessionmaker
from gymbot.services import deload, records

log = logging.getLogger(__name__)

# Sends a chat message to the owner: (text, keyboard or None).
Send = Callable[[str, InlineKeyboardMarkup | None], Awaitable[object]]


async def offer_deload(
    sessionmaker: Sessionmaker, user_id: int, send: Send, tz: ZoneInfo, now: datetime,
    programs_dir: Path | None = None,
) -> bool:
    """Send the deload offer when it is suggested and due; True when sent. Never raises."""
    try:
        async with sessionmaker() as session:
            text = await deload.maybe_offer(session, user_id, now, tz, programs_dir)
            if text is None:
                return False
            await session.commit()
        await send(text, deload.offer_keyboard())
        return True
    except Exception:
        log.exception("deload offer failed")
        return False


async def after_save(
    sessionmaker: Sessionmaker,
    user_id: int,
    set_ids: Collection[int],
    send: Send | None,
    tz: ZoneInfo,
    *,
    key: str | None = None,
    check_deload: bool = True,
    now: datetime | None = None,
    programs_dir: Path | None = None,
) -> list[records.Record]:
    """Records of `set_ids` (announced once per `key`), then the deload offer. Call after the commit."""
    found: list[records.Record] = []
    try:
        async with sessionmaker() as session:
            found = await records.announce(
                session, user_id, set_ids, (lambda text: send(text, None)) if send is not None else None, key=key
            )
    except Exception:
        log.exception("records after save failed")
    if check_deload and send is not None:
        await offer_deload(sessionmaker, user_id, send, tz, now or datetime.now(UTC), programs_dir)
    return found
