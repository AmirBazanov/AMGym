"""The workout in progress in the Mini App: `ActiveWorkout`, one snapshot per user.

The Mini App keeps the running workout on the phone until «Завершить» (POST /api/workouts). To let the
diary answer see it ("я добавил подход, тебе видно?"), it also sends every change to
PUT /api/workouts/active (`save`) and drops it on cancel (DELETE, `clear`). The snapshot is read for
`context_line` (the diary answer), `overlap_note` (a chat workout preview warns about sets already ticked)
and `current_out` (GET /api/state, so a Mini App that lost its local copy can restore it). It never becomes
Workout/WorkoutSet/Exercise rows, so history, PRs and plan inputs ignore it. Finishing deletes it; a late
PUT for an already finished workout is ignored.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import ValidationError
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from gymbot.db.models import ActiveWorkout, Workout
from gymbot.services.overrides import kg
from gymbot.services.programs import normalize
from gymbot.services.workouts import WorkoutIn

MAX_BYTES = 64 * 1024  # stored JSON; above it the API answers 422
STALE_AFTER = timedelta(hours=6)  # not updated for longer: a forgotten workout, not one in progress
STARTED_MAX = timedelta(hours=12)  # started earlier (or on another local day): forgotten as well
LINE_MAX = 800  # characters of the context line


def _aware(dt: datetime) -> datetime:
    """SQLite drops the offset: read naive values back as UTC."""
    return dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)


def payload_of(data: WorkoutIn) -> str:
    """JSON to store. Raises ValueError: a done set without reps (as POST /api/workouts) or too large.
    Sets that are not done may have no weight or reps yet."""
    for ex in data.exercises:
        for s in ex.sets:
            if s.done and not s.reps:
                raise ValueError(f"set without reps: {ex.name}")
    payload = data.model_dump_json()
    if len(payload.encode()) > MAX_BYTES:
        raise ValueError(f"active workout is larger than {MAX_BYTES // 1024} KB")
    return payload


async def save(session: AsyncSession, user_id: int, data: WorkoutIn, now_utc: datetime | None = None) -> bool:
    """Upsert the user's snapshot; False when that workout is already finished: nothing stored and its
    snapshot, if any, removed.
    Raises ValueError like `payload_of` (not for a finished workout). Flushes, the caller commits."""
    finished = await session.scalar(
        select(Workout.id).where(Workout.user_id == user_id, Workout.client_id == data.id)
    )
    if finished is not None:  # a PUT that arrived after «Завершить» must not bring the workout back
        await clear(session, user_id, data.id)
        return False
    payload = payload_of(data)
    now = now_utc or datetime.now(UTC)
    row = await session.get(ActiveWorkout, user_id)
    if row is None:
        session.add(ActiveWorkout(user_id=user_id, client_id=data.id, payload=payload, updated_at=now))
    elif row.client_id != data.id or row.payload != payload:
        # An unchanged resend (app reopened, retry) keeps updated_at: an idle workout still goes stale.
        row.client_id, row.payload, row.updated_at = data.id, payload, now
    await session.flush()
    return True


async def clear(session: AsyncSession, user_id: int, client_id: str | None = None) -> bool:
    """Delete the user's snapshot (only when it is `client_id`'s, if given); whether there was one."""
    stmt = delete(ActiveWorkout).where(ActiveWorkout.user_id == user_id)
    if client_id is not None:
        stmt = stmt.where(ActiveWorkout.client_id == client_id)
    result = await session.execute(stmt)
    return bool(result.rowcount)  # type: ignore[attr-defined]


def _plural_sets(n: int) -> str:
    """Genitive after «из»: «из 1 подхода», «из 4 подходов»."""
    return "подхода" if n % 10 == 1 and n % 100 != 11 else "подходов"


def _clock(dt: datetime, now_local: datetime) -> str:
    return f"{dt:%H:%M}" if dt.date() == now_local.date() else f"{dt:%d.%m %H:%M}"


def fresh(row: ActiveWorkout | None, now_utc: datetime, tz: ZoneInfo) -> WorkoutIn | None:
    """The snapshot's workout while it is in progress, else None: missing, unreadable, not updated for
    STALE_AFTER or started more than STARTED_MAX ago. Not tied to the local day: a workout started at
    23:30 and still going after midnight stays restorable."""
    if row is None or now_utc - _aware(row.updated_at) > STALE_AFTER:
        return None
    try:
        data = WorkoutIn.model_validate_json(row.payload)
    except ValidationError:
        return None
    started = _aware(data.startedAt)
    if now_utc - started > STARTED_MAX:
        return None
    return data


class ActiveWorkoutOut(WorkoutIn):
    """GET /api/state: the stored workout in progress, for a Mini App that lost its local copy."""

    updatedAt: datetime


async def current_out(session: AsyncSession, user_id: int, now_utc: datetime, tz: ZoneInfo) -> ActiveWorkoutOut | None:
    """The fresh snapshot (see `fresh`) unless that workout is already in the history."""
    row = await session.get(ActiveWorkout, user_id)
    data = fresh(row, now_utc, tz)
    if row is None or data is None:
        return None
    finished = await session.scalar(
        select(Workout.id).where(Workout.user_id == user_id, Workout.client_id == row.client_id)
    )
    if finished is not None:
        return None
    return ActiveWorkoutOut(**data.model_dump(), updatedAt=_aware(row.updated_at))


def _same_exercise(a: str, b: str) -> bool:
    # Exact names only: «жим лёжа» vs «жим лёжа 30°» or «французский жим лёжа» are different exercises.
    return normalize(a) == normalize(b)


def _grouped(sets: list[tuple[float | None, int | None]]) -> str:
    """'82.5×8 ×2, 85×6': runs of equal sets merged."""
    runs: list[list[Any]] = []
    for s in sets:
        if runs and runs[-1][0] == s:
            runs[-1][1] += 1
        else:
            runs.append([s, 1])
    parts = []
    for (weight, reps), n in runs:
        one = f"{kg(weight)}×{reps}" if weight is not None else f"{reps} повт."
        parts.append(one + (f" ×{n}" if n > 1 else ""))
    return ", ".join(parts)


def overlap_note(data: WorkoutIn | None, exercises: list[str]) -> str:
    """Under a chat workout preview: the sets already ticked in the Mini App for the same exercises, so the
    user does not save them twice. '' when there is nothing in common."""
    if data is None:
        return ""
    found = []
    for ex in data.exercises:
        done = [(s.weight, s.reps) for s in ex.sets if s.done]
        if done and any(_same_exercise(ex.name, name) for name in exercises):
            name = ex.name.strip()
            found.append(f"{name[:1].lower()}{name[1:]} {_grouped(done)}")
    if not found:
        return ""
    return (
        f"В мини-аппе уже отмечено: {'; '.join(found)} — если это те же подходы, не сохраняй, "
        "они попадут в историю по «Завершить»."
    )


async def overlap_for(session: AsyncSession, user_id: int, exercises: list[str], now_utc: datetime, tz: ZoneInfo) -> str:
    return overlap_note(fresh(await session.get(ActiveWorkout, user_id), now_utc, tz), exercises)


def context_line(row: ActiveWorkout | None, now_utc: datetime, tz: ZoneInfo) -> str:
    """'Сейчас идёт тренировка в мини-аппе (начата 18:40, обновлена 19:10): жим лёжа 82.5×8, 82.5×8
    (2 из 4 подходов); тяга вертикального блока — ещё не начато.' Only done sets are listed.
    '' without a fresh snapshot (see `fresh`)."""
    data = fresh(row, now_utc, tz)
    if row is None or data is None:
        return ""
    updated = _aware(row.updated_at)
    now_local = now_utc.astimezone(tz)
    parts: list[str] = []
    done_total = 0
    for ex in data.exercises:
        done = [s for s in ex.sets if s.done]
        done_total += len(done)
        if not done:
            parts.append(f"{ex.name} — ещё не начато")
            continue
        sets = ", ".join(f"{kg(s.weight)}×{s.reps}" if s.weight is not None else f"{s.reps} повт." for s in done)
        total = len(ex.sets)
        parts.append(f"{ex.name} {sets} ({len(done)} из {total} {_plural_sets(total)})")
    when = f"обновлена {_clock(updated.astimezone(tz), now_local)}"
    if done_total:
        started = _aware(data.startedAt).astimezone(tz)
        head = f"Сейчас идёт тренировка в мини-аппе (начата {_clock(started, now_local)}, {when})"
    else:
        head = f"В мини-аппе открыта тренировка, ни один подход ещё не отмечен ({when})"
    body = "; ".join(parts) if parts else "упражнений нет"
    line = f"{head}: {body}."
    if len(line) > LINE_MAX:
        line = line[: LINE_MAX - 1].rstrip(" ,;") + "…"
    return line


async def context_for(session: AsyncSession, user_id: int, now_utc: datetime, tz: ZoneInfo) -> str:
    return context_line(await session.get(ActiveWorkout, user_id), now_utc, tz)
