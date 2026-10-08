"""Training log: save workouts from the Mini App or from chat text, and serialize them for the Mini App."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from gymbot.db.models import (
    Program,
    ProgramDay,
    ProgramWeek,
    User,
    UserProgram,
    Workout,
    WorkoutSet,
)
from gymbot.llm.schemas import ParseResult
from gymbot.services.programs import (
    find_day,
    get_or_create_exercise,
    item_target,
    load_program,
    program_position,
    targets_snapshot,
)

# ---- Mini App wire format (mirrors miniapp/src/store.ts) ----


class SetDTO(BaseModel):
    weight: float | None = Field(default=None, ge=0, le=1000)
    reps: int | None = Field(default=None, ge=0, le=1000)
    done: bool = True


class ExerciseDTO(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    target: str = ""
    dropset: bool = False
    sets: list[SetDTO]


class WorkoutIn(BaseModel):
    id: str = Field(min_length=1, max_length=64)  # client-generated
    programId: str
    week: int
    weekday: int = Field(ge=1, le=7)
    startedAt: datetime
    finishedAt: datetime | None = None
    exercises: list[ExerciseDTO]
    # ProgramDay.id the workout was prepared from (GET /api/programs/{slug} days[].id). Wins over
    # programId/week/weekday when the day belongs to the active program or its template.
    programDayId: int | None = None


class WorkoutOut(WorkoutIn):
    source: str = "miniapp"
    clientId: str | None = None  # the Mini App's own id, so it can match its offline queue


def _aware(dt: datetime) -> datetime:
    """SQLite drops the offset: store UTC, read naive values back as UTC."""
    return dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)


def _workout_options():  # type: ignore[no-untyped-def]
    return (
        selectinload(Workout.sets).selectinload(WorkoutSet.exercise),
        selectinload(Workout.program_day).selectinload(ProgramDay.week).selectinload(ProgramWeek.program),
        selectinload(Workout.program_day).selectinload(ProgramDay.items),
    )


def _targets(w: Workout) -> dict[int, tuple[str, bool]]:
    """exercise_id -> (target, dropset) from the workout's snapshot; the live day items only for a row
    without one (every workout with a day got it in migration 0014 or on save)."""
    if w.targets_json is not None:
        try:
            snapshot = json.loads(w.targets_json)
            return {int(t["exerciseId"]): (str(t["target"]), bool(t["dropset"])) for t in snapshot}
        except (ValueError, TypeError, KeyError):
            pass
    if w.program_day is None:
        return {}
    return {i.exercise_id: item_target(i) for i in w.program_day.items}


def serialize(w: Workout, up: UserProgram, weeks: int, tz: ZoneInfo) -> WorkoutOut:
    if w.program_day is not None:
        program_slug = w.program_day.week.program.slug
        week, weekday = w.program_day.week.number, w.program_day.weekday
    else:
        # Chat workouts have no planned day: place them by date within the active program.
        pos = program_position(up.started_on, weeks, w.performed_on)
        program_slug, week, weekday = up.program.slug, pos.week, pos.weekday
    targets = _targets(w)
    exercises: list[ExerciseDTO] = []
    by_ex: dict[int, ExerciseDTO] = {}
    for s in w.sets:
        if s.drop_index:  # drops are folded into their main set in the Mini App model
            continue
        ex = by_ex.get(s.exercise_id)
        if ex is None:
            target, dropset = targets.get(s.exercise_id, ("", False))
            ex = ExerciseDTO(name=s.exercise.name, target=target, dropset=dropset, sets=[])
            by_ex[s.exercise_id] = ex
            exercises.append(ex)
        ex.sets.append(SetDTO(weight=float(s.weight_kg) if s.weight_kg is not None else None, reps=s.reps))
    return WorkoutOut(
        id=str(w.id),
        programId=program_slug,
        week=week,
        weekday=weekday,
        startedAt=_aware(w.started_at),
        finishedAt=_aware(w.finished_at) if w.finished_at else None,
        exercises=exercises,
        programDayId=w.program_day_id,
        source=w.source,
        clientId=w.client_id,
    )


async def list_workouts(session: AsyncSession, user: User) -> list[Workout]:
    stmt = (
        select(Workout)
        .where(Workout.user_id == user.id)
        .order_by(Workout.started_at, Workout.id)
        .options(*_workout_options())
    )
    return list((await session.scalars(stmt)).all())


async def get_workout(session: AsyncSession, user: User, workout_id: int) -> Workout | None:
    stmt = select(Workout).where(Workout.id == workout_id, Workout.user_id == user.id).options(*_workout_options())
    return await session.scalar(stmt)


async def _planned_day(session: AsyncSession, up: UserProgram, data: WorkoutIn) -> ProgramDay | None:
    """The active program's day a Mini App workout belongs to, None when it is not one of them.

    1. `programDayId` of a day of the active program, or of its template (then the copy's day made from it,
       by base_day_id): the day the workout was prepared from, even if the day has moved since.
    2. Else the old rule: slug, week and weekday in the active program. The template's slug of an active
       copy counts as the copy (an offline queue or a workout in progress from before the copy was made).
    """
    program = await load_program(session, up.program_id)
    template_slug = (
        await session.scalar(select(Program.slug).where(Program.id == program.based_on_id))
        if program.based_on_id
        else None
    )
    if data.programDayId is not None:
        days = {d.id: d for w in program.weeks for d in w.days}
        if data.programDayId in days:
            return days[data.programDayId]
        if program.based_on_id is not None:
            # Copy days made from that template day; normally one, but a copied week may share the base:
            # the closest to the workout's week and weekday wins, then the oldest row.
            made_from = [(w.number, d) for w in program.weeks for d in w.days if d.base_day_id == data.programDayId]
            if made_from:
                return min(
                    made_from,
                    key=lambda wd: (abs(wd[0] - data.week), abs(wd[1].weekday - data.weekday), wd[1].id),
                )[1]
    if data.programId == program.slug or (template_slug is not None and data.programId == template_slug):
        return find_day(program, data.week, data.weekday)
    return None


async def save_from_miniapp(
    session: AsyncSession, user: User, up: UserProgram, data: WorkoutIn, tz: ZoneInfo
) -> Workout:
    existing = await session.scalar(
        select(Workout).where(Workout.client_id == data.id, Workout.user_id == user.id)
    )
    if existing is not None:  # retry of an already saved workout
        return existing
    day = await _planned_day(session, up, data)
    started = _aware(data.startedAt)
    w = Workout(
        user_id=user.id,
        performed_on=started.astimezone(tz).date(),
        started_at=started,
        finished_at=_aware(data.finishedAt) if data.finishedAt else datetime.now(UTC),
        source="miniapp",
        client_id=data.id,
        program_day_id=day.id if day else None,
        targets_json=targets_snapshot(day) if day else None,
    )
    idx = 0
    for ex in data.exercises:
        exercise = await get_or_create_exercise(session, ex.name)
        for s in ex.sets:
            if not s.done:
                continue
            if not s.reps:
                raise ValueError(f"set without reps: {ex.name}")
            w.sets.append(
                WorkoutSet(
                    exercise_id=exercise.id,
                    set_index=idx,
                    reps=s.reps,
                    weight_kg=Decimal(str(s.weight)) if s.weight is not None else None,
                )
            )
            idx += 1
    session.add(w)
    await session.flush()
    return w


async def save_from_chat(
    session: AsyncSession, user: User, result: ParseResult, raw_text: str, today: date
) -> Workout:
    """Append parsed sets to today's chat workout (one per day), creating it when needed."""
    w = await session.scalar(
        select(Workout)
        .where(Workout.user_id == user.id, Workout.performed_on == today, Workout.source == "chat")
        .options(selectinload(Workout.sets))
    )
    if w is None:
        w = Workout(user_id=user.id, performed_on=today, source="chat", started_at=datetime.now(UTC))
        session.add(w)
        await session.flush()
        await session.refresh(w, ["sets"])
    idx = max((s.set_index for s in w.sets), default=-1) + 1
    for ex in result.exercises:
        exercise = await get_or_create_exercise(session, ex.exercise)
        for s in ex.sets:
            w.sets.append(
                WorkoutSet(
                    exercise_id=exercise.id,
                    set_index=idx,
                    reps=s.reps,
                    weight_kg=Decimal(str(s.weight_kg)) if s.weight_kg is not None else None,
                    drop_index=s.drop_index,
                    raw_text=raw_text,
                )
            )
            idx += 1
    w.finished_at = datetime.now(UTC)
    await session.flush()
    return w


async def delete_last_chat_sets(session: AsyncSession, user: User) -> int:
    """/undo: remove the sets added by the latest chat message. Returns how many were removed.

    The latest message is the trailing run of set_index in the chat workout save_from_chat stamped last
    (finished_at). Not the highest set id: an edit from the chat (gymbot.services.saved_edits) may insert
    sets in the middle of a workout, or of an older one."""
    latest = await session.scalar(
        select(Workout.id)
        .where(Workout.user_id == user.id, Workout.source == "chat")
        .order_by(func.coalesce(Workout.finished_at, Workout.started_at).desc(), Workout.id.desc())
        .limit(1)
    )
    last = await session.scalar(
        select(WorkoutSet).where(WorkoutSet.workout_id == latest).order_by(WorkoutSet.set_index.desc()).limit(1)
    ) if latest is not None else None
    if last is None:
        return 0
    sets = (
        await session.scalars(
            select(WorkoutSet).where(WorkoutSet.workout_id == last.workout_id).order_by(WorkoutSet.set_index.desc())
        )
    ).all()
    batch = []
    for st in sets:  # the latest message's sets are the trailing run with the same raw_text
        if st.raw_text != last.raw_text:
            break
        batch.append(st)
    for s in batch:
        await session.delete(s)
    await session.flush()
    workout = await session.get(
        Workout, last.workout_id, options=[selectinload(Workout.sets)], populate_existing=True
    )
    if workout is not None and not workout.sets:
        await session.delete(workout)
    return len(batch)
