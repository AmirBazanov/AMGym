"""Training log: save workouts from the Mini App or from chat text, and serialize them for the Mini App."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from gymbot.db.models import ProgramDay, ProgramItem, ProgramWeek, User, UserProgram, Workout, WorkoutSet
from gymbot.llm.schemas import ParseResult
from gymbot.services.programs import find_day, get_or_create_exercise, load_program, program_position

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


class WorkoutOut(WorkoutIn):
    source: str = "miniapp"
    clientId: str | None = None  # the Mini App's own id, so it can match its offline queue


def _aware(dt: datetime) -> datetime:
    """SQLite drops the offset: store UTC, read naive values back as UTC."""
    return dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)


def _item_target(item: ProgramItem | None) -> tuple[str, bool]:
    if item is None:
        return "", False
    if item.drop_reps:
        return f"дропсет {item.sets}х {'-'.join(map(str, item.drop_reps))}", True
    return f"{item.sets}х{item.reps_min}-{item.reps_max}", False


def _workout_options():  # type: ignore[no-untyped-def]
    return (
        selectinload(Workout.sets).selectinload(WorkoutSet.exercise),
        selectinload(Workout.program_day).selectinload(ProgramDay.week).selectinload(ProgramWeek.program),
        selectinload(Workout.program_day).selectinload(ProgramDay.items).selectinload(ProgramItem.exercise),
    )


def serialize(w: Workout, up: UserProgram, weeks: int, tz: ZoneInfo) -> WorkoutOut:
    if w.program_day is not None:
        program_slug = w.program_day.week.program.slug
        week, weekday = w.program_day.week.number, w.program_day.weekday
        items = {i.exercise_id: i for i in w.program_day.items}
    else:
        # Chat workouts have no planned day: place them by date within the active program.
        pos = program_position(up.started_on, weeks, w.performed_on)
        program_slug, week, weekday, items = up.program.slug, pos.week, pos.weekday, {}
    exercises: list[ExerciseDTO] = []
    by_ex: dict[int, ExerciseDTO] = {}
    for s in w.sets:
        if s.drop_index:  # drops are folded into their main set in the Mini App model
            continue
        ex = by_ex.get(s.exercise_id)
        if ex is None:
            target, dropset = _item_target(items.get(s.exercise_id))
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


async def save_from_miniapp(
    session: AsyncSession, user: User, up: UserProgram, data: WorkoutIn, tz: ZoneInfo
) -> Workout:
    existing = await session.scalar(
        select(Workout).where(Workout.client_id == data.id, Workout.user_id == user.id)
    )
    if existing is not None:  # retry of an already saved workout
        return existing
    program = await load_program(session, up.program_id)
    day = find_day(program, data.week, data.weekday) if program.slug == data.programId else None
    started = _aware(data.startedAt)
    w = Workout(
        user_id=user.id,
        performed_on=started.astimezone(tz).date(),
        started_at=started,
        finished_at=_aware(data.finishedAt) if data.finishedAt else datetime.now(UTC),
        source="miniapp",
        client_id=data.id,
        program_day_id=day.id if day else None,
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
