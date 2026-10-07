"""Weights for today set from the chat ("поставь сегодня жим 85"): `WeightOverride` rows.

One row per user, exercise and local day (TIMEZONE); a second command for the same exercise and day
replaces the weight (`upsert`). The exercise is always one of the program exercises
(gymbot.services.baselines.catalog): `match` never creates one. The Mini App reads today's rows from
/api/state `weightOverrides` and starts those exercises with them; the diary answer sees `context_line`.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from gymbot.db.models import Exercise, WeightOverride
from gymbot.services.baselines import match_exercise, shares_word
from gymbot.services.programs import normalize

WEIGHT_RANGE = (1.0, 500.0)

# Short gym names that match_exercise does not know because they are ambiguous in general (facts), but in
# a command for today the usual meaning is safe: the preview shows the exact catalog name before applying.
# fullmatch on normalize() text -> catalog names in order of preference.
SHORT_NAMES: list[tuple[re.Pattern[str], tuple[str, ...]]] = [
    (re.compile(r"жим|жим штанги"), ("жим лёжа",)),
    (re.compile(r"тяг[аиуе] блока"), ("тяга вертикального блока",)),
    (re.compile(r"тяг[аиуе] (нижнего|горизонтального) блока"), ("тяга горизонтального блока",)),
    (re.compile(r"присед\w*"), ("присед со штангой",)),
    (re.compile(r"румынск\w*( тяг\w*)?|румынк\w*"), ("румынская тяга",)),
    (re.compile(r"французск\w*( жим\w*)?"), ("французский жим лёжа",)),
]


class WeightOverrideOut(BaseModel):
    """Mini App wire format (/api/state `weightOverrides`), mirrors miniapp/src/store.ts."""

    exercise: str  # exact program exercise name
    weightKg: float
    date: date  # local day in TIMEZONE


def _by_key(catalog: list[str]) -> dict[str, str]:
    return {normalize(c): c for c in catalog}


def _words_in(said: str, pick: str) -> bool:
    """Every word of `said` (3+ letters) has a word of the same stem in `pick`: "тягу блока" in
    "тяга вертикального блока". Looser than baselines.same_stem: endings of a command are any case."""
    words = re.findall(r"[a-zа-я]{3,}", normalize(pick))
    said_words = re.findall(r"[a-zа-я]{3,}", normalize(said))
    if not said_words:
        return False

    def close(a: str, b: str) -> bool:
        n = 0
        for x, y in zip(a, b, strict=False):
            if x != y:
                break
            n += 1
        return n >= max(3, min(len(a), len(b)) - 2)

    return all(any(close(s, w) for w in words) for s in said_words)


def match(said: str | None, pick: str | None, catalog: list[str]) -> str | None:
    """Catalog name for an exercise in a command: as said (exact, baselines.SYNONYMS, SHORT_NAMES), then the
    model's pick when it is a catalog name sharing a word with what was said; else None."""
    if isinstance(said, str) and said.strip():
        name = match_exercise(said, catalog)
        if name is not None:
            return name
        key = normalize(said).strip(" .,;:!?«»\"'()")
        by_key = _by_key(catalog)
        for pattern, targets in SHORT_NAMES:
            if pattern.fullmatch(key):
                found = next((by_key[normalize(t)] for t in targets if normalize(t) in by_key), None)
                if found is not None:
                    return found
    chosen = match_exercise(pick, catalog)
    if chosen is None or not isinstance(said, str):
        return None
    if shares_word(said, chosen) or _words_in(said, chosen):
        return chosen
    return None


async def exercise_ids(session: AsyncSession, names: list[str]) -> dict[str, int]:
    rows = await session.execute(select(Exercise.name, Exercise.id).where(Exercise.name.in_(names)))
    return {name: id_ for name, id_ in rows}


async def for_day(session: AsyncSession, user_id: int, day: date) -> list[WeightOverrideOut]:
    rows = await session.execute(
        select(WeightOverride, Exercise.name)
        .join(Exercise, Exercise.id == WeightOverride.exercise_id)
        .where(WeightOverride.user_id == user_id, WeightOverride.day == day)
        .order_by(WeightOverride.id)
    )
    return [WeightOverrideOut(exercise=name, weightKg=float(o.weight_kg), date=o.day) for o, name in rows]


async def upsert(session: AsyncSession, user_id: int, exercise_id: int, day: date, weight_kg: float) -> None:
    """Set the weight for (user, exercise, day); select-then-write keeps it dialect-neutral."""
    row = await session.scalar(
        select(WeightOverride).where(
            WeightOverride.user_id == user_id, WeightOverride.exercise_id == exercise_id, WeightOverride.day == day
        )
    )
    weight = Decimal(str(round(weight_kg, 2)))
    if row is None:
        session.add(WeightOverride(user_id=user_id, exercise_id=exercise_id, day=day, weight_kg=weight))
    else:
        row.weight_kg = weight
    await session.flush()


async def clear(session: AsyncSession, user_id: int, exercise_id: int, day: date) -> bool:
    """Remove the weight for (user, exercise, day); whether there was one."""
    result = await session.execute(
        delete(WeightOverride).where(
            WeightOverride.user_id == user_id, WeightOverride.exercise_id == exercise_id, WeightOverride.day == day
        )
    )
    return bool(result.rowcount)  # type: ignore[attr-defined]


def kg(x: float) -> str:
    return str(int(x)) if x == int(x) else f"{x:g}"


def context_line(items: list[WeightOverrideOut]) -> str:
    """'Веса на сегодня, выставленные в чате: жим лёжа 85 кг, …' or '' without overrides."""
    if not items:
        return ""
    return "Веса на сегодня, выставленные в чате: " + ", ".join(f"{o.exercise} {kg(o.weightKg)} кг" for o in items) + "."

