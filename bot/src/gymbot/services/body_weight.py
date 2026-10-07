"""Body weight: one measurement per local day, from the chat ("вес 84.6"), the Mini App or MCP.

- `parse_chat` recognizes a message that is only a weigh-in. It is an anchored allowlist on purpose: the
  whole message must be "вес / вешу / взвесился [когда] N [кг]" or "утром N кг", so "жим 85",
  "съел 85 г" or "вес на сегодня жим 85" (a settings command) never match. No question mark: "вес 84?"
  is a question. The chat handler (handlers/body_weight.py) shows "Записать вес?" and writes on a tap.
- `upsert` keeps one row per user and local day: a second measurement the same day replaces the first.
  The newest day also sets the profile weight (User.weight_kg); a backfilled older day does not.
- `remove` deletes a day; when it was the newest, the profile follows the new newest (none left: the
  profile keeps its value, it may be the user's own).
- `context_line` is the line for the diary answer (services/answer.py).
Weights are kg, `day` is the local date in TIMEZONE, `measured_at` is UTC.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from gymbot.db.models import BodyWeight, User
from gymbot.services import profile as prof
from gymbot.services.programs import normalize

MIN_KG = 30
MAX_KG = 250
DEFAULT_DAYS = 180  # GET /api/body-weight
MAX_DAYS = 3660
MIN_DATE = date(2000, 1, 1)
TREND_MIN_DAYS = 7  # the reference measurement for the trend is at least this much older...
TREND_MAX_DAYS = 28  # ...and at most this much

Source = Literal["chat", "miniapp", "mcp"]

# ---- chat recognition ----

_WHEN = r"(?:утром|с утра|сегодня|натощак|после сна)"
_KEY = r"(?:(?:мой|у меня)\s+)?(?:вес(?:\s+тела)?|вешу|взвесил(?:ся|ась)|взвешивание)"
_NUM = r"(?P<kg>\d{2,3}(?:[.,]\d{1,2})?)"
_UNIT = r"(?:кг|килограмм(?:а|ов)?|кило)"
_CHAT = (
    # "вес 84.6", "утром вешу 84 кг", "вес утром: 84,2", "взвесился 85 кг сегодня"
    re.compile(rf"^(?:{_WHEN}\s+)?{_KEY}(?:\s+{_WHEN})?\s*[:=,—–-]?\s*{_NUM}(?:\s*{_UNIT})?(?:\s+{_WHEN})?$"),
    # "утром 84,2 кг": without a keyword only a morning time word and the unit
    re.compile(rf"^(?:утром|с утра|натощак)\s+{_NUM}\s*{_UNIT}$"),
)


def parse_chat(text: str) -> Decimal | None:
    """The weight in kg when the whole message is a weigh-in (see the module doc), else None."""
    clean = normalize(text).rstrip(" .!")
    m = next((m for p in _CHAT if (m := p.match(clean))), None)
    if m is None:
        return None
    kg = Decimal(m["kg"].replace(",", "."))
    return kg if MIN_KG <= kg <= MAX_KG else None


def kg_text(x: Decimal | float) -> str:
    """84.60 -> "84,6", 84 -> "84", -0.55 -> "−0,55" (Russian decimal comma, a real minus)."""
    d = Decimal(str(x)).quantize(Decimal("0.01"), ROUND_HALF_UP)
    text = format(d.normalize(), "f").replace(".", ",")
    return text.replace("-", "−")


def _kg(x: float | Decimal) -> Decimal:
    # SQLite does not round Numeric(5,2) itself.
    return Decimal(str(x)).quantize(Decimal("0.01"), ROUND_HALF_UP)


# ---- Mini App wire format (mirrors miniapp/src/api.ts) ----


class BodyWeightOut(BaseModel):
    date: date  # local day in TIMEZONE
    weightKg: float
    source: Source


class BodyWeightIn(BaseModel):
    weightKg: float = Field(ge=MIN_KG, le=MAX_KG)
    date: dt.date | None = None  # default: today in TIMEZONE; not in the future (`dt.`: the field shadows it)


def entry_out(row: BodyWeight) -> BodyWeightOut:
    return BodyWeightOut(date=row.day, weightKg=float(row.weight_kg), source=row.source)  # type: ignore[arg-type]


def measured_at_for(day: date, tz: ZoneInfo, now_utc: datetime) -> datetime:
    """Now for today; local noon for another day (a backfilled measurement has no time of its own)."""
    if day == now_utc.astimezone(tz).date():
        return now_utc
    return datetime.combine(day, time(12), tz).astimezone(UTC)


# ---- reads ----


async def series(session: AsyncSession, user_id: int, today: date, days: int) -> list[BodyWeight]:
    """Measurements of the last `days` local days including `today`, oldest first."""
    rows = await session.scalars(
        select(BodyWeight)
        .where(BodyWeight.user_id == user_id, BodyWeight.day > today - timedelta(days=days), BodyWeight.day <= today)
        .order_by(BodyWeight.day)
    )
    return list(rows)


async def latest(session: AsyncSession, user_id: int, before: date | None = None) -> BodyWeight | None:
    """The newest measurement (strictly before `before` when given)."""
    q = select(BodyWeight).where(BodyWeight.user_id == user_id)
    if before is not None:
        q = q.where(BodyWeight.day < before)
    return await session.scalar(q.order_by(BodyWeight.day.desc()).limit(1))


async def get_day(session: AsyncSession, user_id: int, day: date) -> BodyWeight | None:
    return await session.scalar(select(BodyWeight).where(BodyWeight.user_id == user_id, BodyWeight.day == day))


# ---- writes (none of them commits) ----


@dataclass
class Saved:
    row: BodyWeight
    replaced: Decimal | None  # the same day's earlier weight it replaced
    before: BodyWeight | None  # the newest measurement of an earlier day
    profile_updated: bool  # User.weight_kg now shows this measurement


def _set_profile(user: User, weight: Decimal) -> None:
    prof.set_profile(user, {"weightKg": float(weight)})


async def upsert(
    session: AsyncSession,
    user: User,
    day: date,
    weight_kg: float | Decimal,
    measured_at: datetime,
    source: Source,
    *,
    raw_text: str | None = None,
    note: str | None = None,
) -> Saved:
    """Write the day's weight (replacing the day's earlier one) and follow it in the profile when it is the
    newest day. Select-then-write keeps it dialect-neutral; the unique (user_id, day) guards races."""
    weight = _kg(weight_kg)
    row = await get_day(session, user.id, day)
    replaced = row.weight_kg if row is not None else None
    if row is None:
        row = BodyWeight(user_id=user.id, day=day)
        session.add(row)
    row.weight_kg = weight
    row.measured_at = measured_at
    row.source = source
    row.raw_text = raw_text
    row.note = note
    await session.flush()
    newest = await session.scalar(select(func.max(BodyWeight.day)).where(BodyWeight.user_id == user.id))
    profile_updated = newest is None or day >= newest
    if profile_updated:
        _set_profile(user, weight)
    return Saved(row, replaced, await latest(session, user.id, before=day), profile_updated)


async def remove(session: AsyncSession, user: User, day: date) -> bool:
    """Delete the day's measurement; whether there was one. Deleting the newest day moves the profile weight
    to the new newest one; with none left the profile keeps its value."""
    row = await get_day(session, user.id, day)
    if row is None:
        return False
    newer = await latest(session, user.id)
    was_newest = newer is not None and newer.day == day
    await session.execute(delete(BodyWeight).where(BodyWeight.id == row.id))
    if was_newest and (now_newest := await latest(session, user.id)) is not None:
        _set_profile(user, now_newest.weight_kg)
    return True


# ---- the diary answer ----


def _days(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} день"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return f"{n} дня"
    return f"{n} дней"


async def context_line(session: AsyncSession, user_id: int, today: date) -> str:
    """'Вес тела: 84,6 (08.10), 7 дней назад 85,2, тренд −0,6 кг/нед.' or '' without measurements.
    The trend compares with the newest measurement TREND_MIN_DAYS..TREND_MAX_DAYS days older."""
    last = await latest(session, user_id, before=today + timedelta(days=1))
    if last is None:
        return ""
    line = f"Вес тела: {kg_text(last.weight_kg)} кг ({last.day:%d.%m})"
    ref = await session.scalar(
        select(BodyWeight)
        .where(
            BodyWeight.user_id == user_id,
            BodyWeight.day <= last.day - timedelta(days=TREND_MIN_DAYS),
            BodyWeight.day >= last.day - timedelta(days=TREND_MAX_DAYS),
        )
        .order_by(BodyWeight.day.desc())
        .limit(1)
    )
    if ref is not None:
        gap = (last.day - ref.day).days
        per_week = (last.weight_kg - ref.weight_kg) * 7 / gap
        trend = per_week.quantize(Decimal("0.1"), ROUND_HALF_UP)
        sign = "+" if trend > 0 else ""
        line += f", {_days(gap)} назад {kg_text(ref.weight_kg)}, тренд {sign}{kg_text(trend)} кг/нед"
    return line + "."
