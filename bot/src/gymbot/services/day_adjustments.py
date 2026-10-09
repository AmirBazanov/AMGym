"""Manual one-day changes of the plan said in the chat: "сегодня облегчённо, −20 %", "сегодня полегче", "на
завтра на подход меньше", "в пятницу без ног" (gymbot.services.chat_edit, action `adjust_day`).

One row per user and local day (DayAdjustment). It is an input of the adaptive day plan
(gymbot.services.plan.collect_inputs), so the plan's hash changes and the plan is rebuilt with it, and a
rebuild never drops it: the rules apply it like a deload (the lighter value wins, rest still wins) and the
model may not make it heavier. Weights for another day (gymbot.services.next_weights.day_weights) apply it
too. Pure data here; applying it to an exercise is plan.adjust_item (it owns the muscle groups).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from gymbot.db.models import DayAdjustment

# plan.muscle_group keys -> Russian, as said after "без": "без ног", "без бицепса".
GROUP_LABELS = {
    "legs": "ног",
    "biceps": "бицепса",
    "triceps": "трицепса",
    "shoulders": "плеч",
    "chest": "груди",
    "back": "спины",
    "abs": "пресса",
}
FACTOR_RANGE = (0.3, 0.99)  # only lighter, never below what a light day of pain could give
SETS_DELTA_RANGE = (-5, -1)
NOTE_MAX = 200


@dataclass
class DayAdjust:
    """Plain data (it goes into plan.PlanInputs, hashed with dataclasses.asdict): no ORM row, no Decimal."""

    weight_factor: float | None = None
    sets_delta: int | None = None
    skip_exercises: list[str] = field(default_factory=list)  # program exercise names, sorted
    skip_groups: list[str] = field(default_factory=list)  # GROUP_LABELS keys, sorted
    note: str | None = None

    def empty(self) -> bool:
        return self.weight_factor is None and self.sets_delta is None and not self.skip_exercises and not self.skip_groups

    def merged(self, new: DayAdjust) -> DayAdjust:
        """`new` said for the same day: its values replace these, the skips add up."""
        return DayAdjust(
            new.weight_factor if new.weight_factor is not None else self.weight_factor,
            new.sets_delta if new.sets_delta is not None else self.sets_delta,
            sorted(set(self.skip_exercises) | set(new.skip_exercises)),
            sorted(set(self.skip_groups) | set(new.skip_groups)),
            new.note or self.note,
        )


def _pct(factor: float) -> int:
    return round((1 - factor) * 100)


def sets_words(delta: int) -> str:
    n = abs(delta)
    return "на подход меньше" if n == 1 else f"на {n} подхода меньше" if n < 5 else f"на {n} подходов меньше"


def describe(adj: DayAdjust) -> str:
    """«веса −20 %, на подход меньше, без ног и бицепса, пропуск: приседания со штангой»."""
    parts = []
    if adj.weight_factor is not None:
        parts.append(f"веса −{_pct(adj.weight_factor)} %")
    if adj.sets_delta is not None:
        parts.append(sets_words(adj.sets_delta))
    if adj.skip_groups:
        labels = [GROUP_LABELS.get(g, g) for g in adj.skip_groups]
        parts.append("без " + (" и ".join(labels) if len(labels) <= 2 else ", ".join(labels)))
    if adj.skip_exercises:
        parts.append("пропуск: " + ", ".join(adj.skip_exercises))
    return ", ".join(parts)


def summary(adj: DayAdjust) -> str:
    """The day plan's note: «Твоя поправка на день: веса −20 %, на подход меньше.»"""
    return f"Твоя поправка на день: {describe(adj)}."


SUMMARY_MARK = "Твоя поправка"
SKIP_REASON = "по твоей просьбе"


def _view(row: DayAdjustment) -> DayAdjust:
    skip: dict[str, Any] = row.skip_json if isinstance(row.skip_json, dict) else {}
    return DayAdjust(
        float(row.weight_factor) if row.weight_factor is not None else None,
        row.sets_delta,
        sorted(str(x) for x in skip.get("exercises") or []),
        sorted(str(x) for x in skip.get("groups") or []),
        row.note,
    )


async def _row(session: AsyncSession, user_id: int, day: date) -> DayAdjustment | None:
    return await session.scalar(
        select(DayAdjustment).where(DayAdjustment.user_id == user_id, DayAdjustment.day == day)
    )


async def get(session: AsyncSession, user_id: int, day: date) -> DayAdjust | None:
    row = await _row(session, user_id, day)
    return _view(row) if row is not None else None


async def between(session: AsyncSession, user_id: int, first: date, last: date) -> dict[date, DayAdjust]:
    rows = await session.scalars(
        select(DayAdjustment).where(
            DayAdjustment.user_id == user_id, DayAdjustment.day >= first, DayAdjustment.day <= last
        )
    )
    return {r.day: _view(r) for r in rows}


async def upsert(
    session: AsyncSession,
    user_id: int,
    day: date,
    adj: DayAdjust,
    raw_text: str | None,
    source: str = "chat",
    now: datetime | None = None,
) -> None:
    """Store the whole adjustment for (user, day), replacing what was there (the caller merges first);
    select-then-write keeps it dialect-neutral."""
    if adj.empty():
        raise ValueError("empty day adjustment")
    row = await _row(session, user_id, day)
    if row is None:
        row = DayAdjustment(user_id=user_id, day=day)
        session.add(row)
    row.weight_factor = Decimal(str(round(adj.weight_factor, 2))) if adj.weight_factor is not None else None
    row.sets_delta = adj.sets_delta
    skip = {"exercises": list(adj.skip_exercises), "groups": list(adj.skip_groups)}
    row.skip_json = skip if adj.skip_exercises or adj.skip_groups else None
    row.note = adj.note[:NOTE_MAX] if adj.note else None
    row.source = source
    row.raw_text = raw_text
    row.created_at = now or datetime.now(UTC)
    await session.flush()


async def clear(session: AsyncSession, user_id: int, day: date) -> bool:
    """Remove the adjustment of (user, day); whether there was one."""
    result = await session.execute(
        delete(DayAdjustment).where(DayAdjustment.user_id == user_id, DayAdjustment.day == day)
    )
    return bool(result.rowcount)  # type: ignore[attr-defined]
