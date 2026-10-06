"""Nutrition log summaries for the Mini App (and later for reminders).

Day boundaries are local midnights in TIMEZONE converted to UTC, so filtering uses the `eaten_at`
index and behaves the same on SQLite and Postgres. Never group by `func.date(eaten_at)`: that is the
UTC date and differs between dialects.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gymbot.db.models import FoodEntry, User

# ---- Mini App wire format (mirrors miniapp/src/api.ts). Numbers are floats, never Decimal strings. ----


class Targets(BaseModel):
    """Daily norm; None = not set."""

    kcal: int | None = None
    protein: int | None = None
    fat: int | None = None
    carbs: int | None = None


class Macros(BaseModel):
    kcal: float
    protein: float
    fat: float
    carbs: float


class Remaining(BaseModel):
    """targets - totals; negative = over the norm, None = no norm set."""

    kcal: float | None
    protein: float | None
    fat: float | None
    carbs: float | None


class FoodEntryOut(BaseModel):
    id: int
    eatenAt: datetime  # UTC
    time: str  # HH:MM in TIMEZONE
    description: str
    grams: float | None
    kcal: float
    protein: float
    fat: float
    carbs: float
    estimated: bool


class DaySummary(BaseModel):
    date: date
    targets: Targets
    totals: Macros
    remaining: Remaining
    entries: list[FoodEntryOut]


class WeekDay(BaseModel):
    date: date
    kcal: float
    protein: float
    fat: float
    carbs: float
    entries: int


class WeekSummary(BaseModel):
    targets: Targets
    days: list[WeekDay]


_KEYS = ("kcal", "protein", "fat", "carbs")


def _aware(dt: datetime) -> datetime:
    """SQLite drops the offset: store UTC, read naive values back as UTC."""
    return dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)


def local_day_bounds(day: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """[start, end) in UTC of the local calendar day `day` in `tz` (23/25 hours on DST switches)."""
    start = datetime.combine(day, time.min, tzinfo=tz)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz)
    return start.astimezone(UTC), end.astimezone(UTC)


def user_targets(user: User) -> Targets:
    return Targets(
        kcal=user.kcal_target, protein=user.protein_target_g, fat=user.fat_target_g, carbs=user.carbs_target_g
    )


def set_targets(user: User, changes: dict[str, int | None]) -> None:
    """Apply a partial update: only the given keys change, None resets a target."""
    columns = {"kcal": "kcal_target", "protein": "protein_target_g", "fat": "fat_target_g", "carbs": "carbs_target_g"}
    for key, value in changes.items():
        setattr(user, columns[key], value)


def _macros(e: FoodEntry) -> dict[str, Decimal]:
    return {"kcal": e.kcal, "protein": e.protein_g, "fat": e.fat_g, "carbs": e.carbs_g}


def _sum(entries: list[FoodEntry]) -> dict[str, Decimal]:
    # Sum Decimals, convert once: float sums would show 0.30000000000000004 in the UI.
    totals = dict.fromkeys(_KEYS, Decimal(0))
    for e in entries:
        for k, v in _macros(e).items():
            totals[k] += Decimal(v)
    return totals


async def _entries(session: AsyncSession, user: User, start: datetime, end: datetime) -> list[FoodEntry]:
    rows = await session.scalars(
        select(FoodEntry)
        .where(FoodEntry.user_id == user.id, FoodEntry.eaten_at >= start, FoodEntry.eaten_at < end)
        .order_by(FoodEntry.eaten_at, FoodEntry.id)
    )
    return list(rows)


def _entry_out(e: FoodEntry, tz: ZoneInfo) -> FoodEntryOut:
    eaten = _aware(e.eaten_at)
    return FoodEntryOut(
        id=e.id,
        eatenAt=eaten,
        time=eaten.astimezone(tz).strftime("%H:%M"),
        description=e.description,
        grams=float(e.grams) if e.grams is not None else None,
        kcal=float(e.kcal),
        protein=float(e.protein_g),
        fat=float(e.fat_g),
        carbs=float(e.carbs_g),
        estimated=e.estimated,
    )


async def day_summary(session: AsyncSession, user: User, day: date, tz: ZoneInfo) -> DaySummary:
    """What was eaten on the local day `day`, totals and what is left to the norm."""
    start, end = local_day_bounds(day, tz)
    entries = await _entries(session, user, start, end)
    targets = user_targets(user)
    totals = _sum(entries)
    remaining = {
        k: float(Decimal(t) - totals[k]) if (t := getattr(targets, k)) is not None else None for k in _KEYS
    }
    return DaySummary(
        date=day,
        targets=targets,
        totals=Macros(**{k: float(v) for k, v in totals.items()}),
        remaining=Remaining(**remaining),
        entries=[_entry_out(e, tz) for e in entries],
    )


async def week_summary(session: AsyncSession, user: User, end: date, tz: ZoneInfo) -> WeekSummary:
    """Seven local days ending with `end` (oldest first), days without food included with zeros."""
    days = [end - timedelta(days=i) for i in range(6, -1, -1)]
    start_utc, _ = local_day_bounds(days[0], tz)
    _, end_utc = local_day_bounds(end, tz)
    by_day: dict[date, list[FoodEntry]] = defaultdict(list)
    for e in await _entries(session, user, start_utc, end_utc):
        by_day[_aware(e.eaten_at).astimezone(tz).date()].append(e)
    out = []
    for d in days:
        totals = _sum(by_day[d])
        out.append(WeekDay(date=d, entries=len(by_day[d]), **{k: float(v) for k, v in totals.items()}))
    return WeekSummary(targets=user_targets(user), days=out)
