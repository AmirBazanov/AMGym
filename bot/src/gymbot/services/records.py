"""Personal records: what a just-saved workout beat, for a line in the chat and a toast in the Mini App.

Only main sets count (drops are excluded). A set is compared with every other main set of the same exercise
already in the diary: earlier workouts and the same workout's earlier sets (a chat workout grows message by
message). The first workout of an exercise is never a record: the exercise needs sets in another workout.

Per exercise at most one record, the first that holds:
- "e1rm": the best new 1RM by Epley (the same formula as gymbot.services.advice.epley) above the old best;
- "weight": the heaviest new weight above the old heaviest;
- "reps": a set not dominated by any old one, i.e. no old set had weight >= its weight and reps >= its reps
  (so 90×8 after 90×7 and 100×5 is a record: the most reps at 90 kg or more);
- "bw_reps": bodyweight sets (no weight or 0 kg): the most reps in a set above the old most.
Every comparison is strict: a tie is not a record.

`announce` does the rest after a save: the chat message (at most MAX_LINES lines), the live event "records"
for the Mini App, and an in-process guard so the same key (a Mini App workout id) is announced once.
`recent_line` replays the history for the diary answer: records of the last RECENT_DAYS days.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Collection, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gymbot.db.models import Exercise, Workout, WorkoutSet
from gymbot.services import live

log = logging.getLogger(__name__)

MAX_LINES = 5
RECENT_DAYS = 14
RECENT_MAX = 5  # records in the diary answer's line
ANNOUNCED_MAX = 500  # keys remembered by the idempotency guard

Kind = Literal["e1rm", "weight", "reps", "bw_reps"]
Send = Callable[[str], Awaitable[object]]


def e1rm(weight: float, reps: int) -> float:
    """Epley, as gymbot.services.advice.epley (not imported: advice pulls in the plan)."""
    return weight if reps == 1 else weight * (1 + reps / 30)


@dataclass(frozen=True)
class Lift:
    """A main set; weight None or 0 = bodyweight."""

    weight: float | None
    reps: int

    @property
    def kg(self) -> float:
        return self.weight or 0.0

    @property
    def weighted(self) -> bool:
        return self.kg > 0

    @property
    def e1rm(self) -> float:
        return e1rm(self.kg, self.reps)


@dataclass(frozen=True)
class Record:
    exercise: str
    weight: float | None  # the record set
    reps: int
    kind: Kind
    old: float  # previous best: 1RM, weight or reps (for "reps": the most reps at this weight or more)
    new: float
    old_weight: float | None = None  # "reps": the weight of that previous best set

    @property
    def set_text(self) -> str:
        return f"{_kg(self.weight)}×{self.reps}" if self.weight else f"{self.reps} повт."

    def wire(self) -> dict[str, Any]:
        """The Mini App's toast data (live event "records")."""
        return {
            "exercise": self.exercise,
            "weight": self.weight,
            "reps": self.reps,
            "kind": self.kind,
            "text": f"{self.exercise} {self.set_text}",
        }


def best_record(exercise: str, history: Sequence[Lift], new: Sequence[Lift]) -> Record | None:
    """The record `new` sets set against `history` (both main sets of one exercise), or None.

    An empty `history` (the first time) is never a record."""
    old_w = [x for x in history if x.weighted and x.reps > 0]
    new_w = [x for x in new if x.weighted and x.reps > 0]
    if old_w and new_w:
        old_best = max(x.e1rm for x in old_w)
        top = max(new_w, key=lambda x: (x.e1rm, x.kg))
        if round(top.e1rm, 2) > round(old_best, 2):
            return Record(exercise, top.kg, top.reps, "e1rm", old_best, top.e1rm)
        old_max = max(x.kg for x in old_w)
        heavy = max(new_w, key=lambda x: (x.kg, x.reps))
        if heavy.kg > old_max:
            return Record(exercise, heavy.kg, heavy.reps, "weight", old_max, heavy.kg)
        for x in sorted(new_w, key=lambda x: (x.e1rm, x.kg), reverse=True):
            prev = [o for o in old_w if o.kg >= x.kg]
            best = max(prev, key=lambda o: (o.reps, o.kg), default=None)
            if best is not None and x.reps > best.reps:
                return Record(exercise, x.kg, x.reps, "reps", best.reps, x.reps, best.kg)
    old_bw = [x.reps for x in history if not x.weighted]
    new_bw = [x for x in new if not x.weighted and x.reps > 0]
    if old_bw and new_bw:
        top = max(new_bw, key=lambda x: x.reps)
        if top.reps > max(old_bw):
            return Record(exercise, None, top.reps, "bw_reps", max(old_bw), top.reps)
    return None


# ---- text ----


def _kg(x: float | Decimal | None) -> str:
    """92.5 -> '92,5', 60.0 -> '60'."""
    v = float(x or 0)
    return str(int(v)) if v == int(v) else f"{v:g}".replace(".", ",")


def _pair(old: float, new: float) -> tuple[str, str]:
    """Whole kilograms, one decimal when both round to the same number (never «110 → 110»)."""
    a, b = round(old), round(new)
    if a != b:
        return str(a), str(b)
    return _kg(round(old, 1)), _kg(round(new, 1))


def _reps_word(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "повтор"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return "повтора"
    return "повторов"


def detail(r: Record) -> str:
    if r.kind == "e1rm":
        old, new = _pair(r.old, r.new)
        return f"1ПМ {old} → {new} кг"
    if r.kind == "weight":
        return f"максимальный вес {_kg(r.old)} → {_kg(r.new)} кг"
    if r.kind == "reps":
        return f"{r.reps} {_reps_word(r.reps)} с {_kg(r.weight)} кг (было {_kg(r.old_weight)}×{int(r.old)})"
    return f"было {int(r.old)} {_reps_word(int(r.old))}"


def line(r: Record) -> str:
    """«🏆 Новый рекорд: жим лёжа 92,5×6 — 1ПМ 110 → 113 кг»."""
    return f"🏆 Новый рекорд: {r.exercise} {r.set_text} — {detail(r)}"


def message_text(records: Sequence[Record]) -> str:
    return "\n".join(line(r) for r in records[:MAX_LINES])


# ---- database ----


def _lift(weight: Decimal | None, reps: int) -> Lift:
    return Lift(float(weight) if weight is not None else None, reps)


async def find_new(session: AsyncSession, user_id: int, set_ids: Collection[int]) -> list[Record]:
    """Records set by the main sets `set_ids` (the user's), in their order; at most one per exercise."""
    if not set_ids:
        return []
    new_rows = (
        await session.execute(
            select(WorkoutSet.id, WorkoutSet.workout_id, WorkoutSet.exercise_id, Exercise.name,
                   WorkoutSet.weight_kg, WorkoutSet.reps)
            .join(Workout, Workout.id == WorkoutSet.workout_id)
            .join(Exercise, Exercise.id == WorkoutSet.exercise_id)
            .where(Workout.user_id == user_id, WorkoutSet.id.in_(list(set_ids)), WorkoutSet.drop_index == 0)
            .order_by(Workout.started_at, Workout.id, WorkoutSet.set_index)
        )
    ).all()
    if not new_rows:
        return []
    ids = {r.id for r in new_rows}
    by_ex: dict[int, tuple[str, list[Lift], set[int]]] = {}
    for r in new_rows:
        name, lifts, workouts = by_ex.setdefault(r.exercise_id, (r.name, [], set()))
        lifts.append(_lift(r.weight_kg, r.reps))
        workouts.add(r.workout_id)
    old_rows = (
        await session.execute(
            select(WorkoutSet.id, WorkoutSet.workout_id, WorkoutSet.exercise_id, WorkoutSet.weight_kg, WorkoutSet.reps)
            .join(Workout, Workout.id == WorkoutSet.workout_id)
            .where(Workout.user_id == user_id, WorkoutSet.exercise_id.in_(list(by_ex)), WorkoutSet.drop_index == 0)
        )
    ).all()
    history: dict[int, list[Lift]] = {ex: [] for ex in by_ex}
    elsewhere: set[int] = set()  # exercises with sets in another workout: not their first time
    for r in old_rows:
        if r.id in ids:
            continue
        history[r.exercise_id].append(_lift(r.weight_kg, r.reps))
        if r.workout_id not in by_ex[r.exercise_id][2]:
            elsewhere.add(r.exercise_id)
    out = []
    for ex_id, (name, lifts, _) in by_ex.items():
        if ex_id not in elsewhere:
            continue
        if (rec := best_record(name, history[ex_id], lifts)) is not None:
            out.append(rec)
    return out


_announced: OrderedDict[str, None] = OrderedDict()


def claim(key: str) -> bool:
    """True the first time `key` is seen in this process (a retried save is not announced twice)."""
    if key in _announced:
        return False
    _announced[key] = None
    while len(_announced) > ANNOUNCED_MAX:
        _announced.popitem(last=False)
    return True


async def announce(
    session: AsyncSession, user_id: int, set_ids: Collection[int], send: Send | None, *, key: str | None = None
) -> list[Record]:
    """After the commit: find the records, publish them to the Mini App and send the chat lines.

    Never raises; returns the records found (empty on a repeated `key` or any failure)."""
    try:
        if key is not None and not claim(key):
            return []
        records = await find_new(session, user_id, set_ids)
    except Exception:
        log.exception("records: detection failed")
        return []
    if not records:
        return []
    shown = records[:MAX_LINES]
    live.publish_records(user_id, [r.wire() for r in shown])
    if send is not None:
        try:
            await send(message_text(shown))
        except Exception:
            log.warning("records: sending the message failed", exc_info=True)
    return records


# ---- the diary answer ----


@dataclass(frozen=True)
class Dated:
    day: date
    record: Record


async def recent(session: AsyncSession, user_id: int, today: date, days: int = RECENT_DAYS) -> list[Dated]:
    """Records set on local days today-days+1..today, replaying the history workout by workout; newest first."""
    rows = (
        await session.execute(
            select(Workout.id, Workout.performed_on, WorkoutSet.exercise_id, Exercise.name,
                   WorkoutSet.weight_kg, WorkoutSet.reps)
            .join(WorkoutSet, WorkoutSet.workout_id == Workout.id)
            .join(Exercise, Exercise.id == WorkoutSet.exercise_id)
            .where(Workout.user_id == user_id, Workout.performed_on <= today, WorkoutSet.drop_index == 0)
            .order_by(Workout.performed_on, Workout.started_at, Workout.id, WorkoutSet.set_index)
        )
    ).all()
    first = today - timedelta(days=days - 1)
    history: dict[int, list[Lift]] = {}
    out: list[Dated] = []
    i = 0
    while i < len(rows):  # one (workout, exercise) group at a time, in workout order
        wid = rows[i].id
        group: dict[int, tuple[str, date, list[Lift]]] = {}
        while i < len(rows) and rows[i].id == wid:
            r = rows[i]
            group.setdefault(r.exercise_id, (r.name, r.performed_on, []))[2].append(_lift(r.weight_kg, r.reps))
            i += 1
        for ex_id, (name, day, lifts) in group.items():
            old = history.setdefault(ex_id, [])
            if day >= first and (rec := best_record(name, old, lifts)) is not None:
                out.append(Dated(day, rec))
            old.extend(lifts)
    out.sort(key=lambda d: d.day, reverse=True)
    return out


def recent_text(found: Sequence[Dated], days: int = RECENT_DAYS) -> str:
    """«Рекорды за 14 дней: жим лёжа 92,5×6 (1ПМ 110 → 113 кг, 05.10); …» (newest first, RECENT_MAX)."""
    if not found:
        return f"Новых рекордов за {days} дней нет."
    parts = [f"{d.record.exercise} {d.record.set_text} ({detail(d.record)}, {d.day:%d.%m})" for d in found[:RECENT_MAX]]
    more = f" и ещё {len(found) - RECENT_MAX}" if len(found) > RECENT_MAX else ""
    return f"Рекорды за {days} дней: " + "; ".join(parts) + more + "."


async def recent_line(session: AsyncSession, user_id: int, today: date) -> str:
    return recent_text(await recent(session, user_id, today))
