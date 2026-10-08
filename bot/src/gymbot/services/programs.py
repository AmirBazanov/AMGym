"""Programs: load data/programs/*.json into the DB and answer "what's planned when".

The server is the source of truth for programs: the Mini App reads them from GET /api/programs[/{slug}].
A program with `owner_user_id` NULL is a template imported from JSON and never edited; a program with an
owner is that user's own copy (the program editor, phase 2). A user sees templates and their own copies.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from pydantic import BaseModel
from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from gymbot.db.models import Exercise, Program, ProgramDay, ProgramItem, ProgramWeek, Workout, WorkoutSet

log = logging.getLogger(__name__)


def normalize(name: str) -> str:
    return " ".join(name.strip().lower().replace("ё", "е").split())


async def get_or_create_exercise(session: AsyncSession, name: str) -> Exercise:
    """Match by name or alias, ignoring case, extra spaces and ё/е; create when unknown."""
    key = normalize(name)
    for ex in (await session.scalars(select(Exercise))).all():
        if normalize(ex.name) == key or key in (normalize(a) for a in ex.aliases or []):
            return ex
    ex = Exercise(name=" ".join(name.strip().lower().split()), aliases=[])
    session.add(ex)
    await session.flush()
    return ex


async def exercise_catalog(session: AsyncSession) -> list[str]:
    return list((await session.scalars(select(Exercise.name).order_by(Exercise.name))).all())


async def sync_programs(session: AsyncSession, programs_dir: Path) -> None:
    """Import every program JSON whose slug is not in the DB yet. Existing programs (templates and the users'
    own copies) are left alone; `backfill_program_meta` fills fields added later into imported templates.
    Only templates (no owner) count as the JSON's program: a user's copy is never taken for one. Copy slugs
    end in `.u<user id>` so they cannot collide with a file stem in practice; a file that does is skipped."""
    rows = (await session.execute(select(Program.slug, Program.owner_user_id))).all()
    templates = {slug for slug, owner in rows if owner is None}
    copies = {slug for slug, owner in rows if owner is not None}
    for path in sorted(programs_dir.glob("*.json")):
        if path.stem in templates:
            continue
        if path.stem in copies:
            log.warning("program file %s has the slug of a user's own copy; not imported", path.name)
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        program = Program(slug=path.stem, name=data["name"], source=data.get("source"))
        for w in data["weeks"]:
            week = ProgramWeek(number=w["number"])
            for d in w["days"]:
                day = ProgramDay(weekday=d["weekday"], focus=d.get("focus"))
                for e in d["exercises"]:
                    p = e["prescription"]
                    day.items.append(
                        ProgramItem(
                            exercise=await get_or_create_exercise(session, e["name"]),
                            order=e["order"],
                            intensity=e.get("intensity"),
                            sets=p["sets"],
                            reps_min=p.get("reps_min"),
                            reps_max=p.get("reps_max"),
                            drop_reps=p.get("drop_reps"),
                        )
                    )
                week.days.append(day)
            program.weeks.append(week)
        session.add(program)
        log.info("imported program %s", path.stem)
    await session.commit()


async def backfill_program_meta(session: AsyncSession, programs_dir: Path) -> int:
    """Day `focus` of already imported templates where it is NULL, from their JSON by (week, weekday).
    Run at start after sync_programs; idempotent. Returns how many days were filled."""
    filled = 0
    templates = (await session.scalars(select(Program).where(Program.owner_user_id.is_(None)))).all()
    for program in templates:
        path = programs_dir / f"{program.slug}.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        focus = {
            (w.get("number"), d.get("weekday")): d["focus"]
            for w in data.get("weeks", [])
            for d in w.get("days", [])
            if isinstance(d.get("focus"), str) and d["focus"]
        }
        if not focus:
            continue
        for week in (await load_program(session, program.id)).weeks:
            for day in week.days:
                value = focus.get((week.number, day.weekday))
                if day.focus is None and value is not None:
                    day.focus = value[:64]
                    filled += 1
    if filled:
        log.info("program focus filled for %d days", filled)
    await session.commit()
    return filled


def _tree():  # type: ignore[no-untyped-def]
    return selectinload(Program.weeks).selectinload(ProgramWeek.days).selectinload(ProgramDay.items).selectinload(
        ProgramItem.exercise
    )


async def load_program(session: AsyncSession, program_id: int) -> Program:
    """The program with weeks, days and items, sorted (relationship order_by: week number, weekday, order)."""
    stmt = select(Program).where(Program.id == program_id).options(_tree())
    return (await session.scalars(stmt)).one()


# ---- visibility: templates and the user's own copies ----


def visible_to(user_id: int | None) -> ColumnElement[bool]:
    """Templates (no owner) and, with a user, that user's own copies."""
    if user_id is None:
        return Program.owner_user_id.is_(None)
    return or_(Program.owner_user_id.is_(None), Program.owner_user_id == user_id)


async def visible_programs(session: AsyncSession, user_id: int) -> list[Program]:
    """Programs the user may see and choose, with their tree loaded, oldest first."""
    stmt = select(Program).where(visible_to(user_id)).order_by(Program.id).options(_tree())
    return list((await session.scalars(stmt)).all())


async def visible_program(session: AsyncSession, user_id: int, slug: str) -> Program | None:
    """The program by slug with its tree, None when unknown or another user's copy."""
    stmt = select(Program).where(Program.slug == slug, visible_to(user_id)).options(_tree())
    return await session.scalar(stmt)


# ---- prescriptions ----


def raw_prescription(item: ProgramItem) -> str:
    """The prescription as written in the program JSON (`raw`) and as the history shows it (`target`):
    "6х8-12", "дропсет 3х 12-6-6", "4х10" when reps_min == reps_max (the template has no such items, so the
    history of template days is unchanged). One function for GET /api/programs and the workout snapshot."""
    if item.drop_reps:
        return f"дропсет {item.sets}х {'-'.join(map(str, item.drop_reps))}"
    if item.reps_min is None:
        return f"{item.sets}х"
    if item.reps_max is None or item.reps_max == item.reps_min:
        return f"{item.sets}х{item.reps_min}"
    return f"{item.sets}х{item.reps_min}-{item.reps_max}"


def item_target(item: ProgramItem | None) -> tuple[str, bool]:
    """(target, dropset) of a workout exercise planned by `item`; ("", False) without one."""
    if item is None:
        return "", False
    return raw_prescription(item), bool(item.drop_reps)


def targets_snapshot(day: ProgramDay) -> str:
    """Workout.targets_json for a workout of `day`: [{exerciseId, target, dropset}] in day order."""
    out = []
    for item in day.items:
        target, dropset = item_target(item)
        out.append({"exerciseId": item.exercise_id, "target": target, "dropset": dropset})
    return json.dumps(out, ensure_ascii=False)


# ---- wire format for the Mini App (GET /api/programs[/{slug}], GET /api/exercises) ----

WEEKDAY_TITLES = ("", "понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")


class PrescriptionOut(BaseModel):
    """Same keys as the program JSON (miniapp/src/program.ts Prescription)."""

    sets: int
    reps_min: int | None
    reps_max: int | None
    drop_reps: list[int] | None
    raw: str


class ProgramExerciseOut(BaseModel):
    id: int  # ProgramItem.id
    name: str  # Exercise.name
    intensity: str | None
    order: int
    prescription: PrescriptionOut


class ProgramDayOut(BaseModel):
    id: int  # ProgramDay.id (WorkoutIn.programDayId)
    weekday: int  # 1=Mon..7=Sun
    title: str  # "понедельник"
    focus: str | None
    exercises: list[ProgramExerciseOut]


class ProgramWeekOut(BaseModel):
    number: int
    days: list[ProgramDayOut]


class ProgramOut(BaseModel):
    id: str  # slug
    name: str
    source: str | None
    version: int
    editable: bool  # the user's own copy
    basedOn: str | None  # template slug of a copy
    weeks: list[ProgramWeekOut]


class ProgramSummary(BaseModel):
    id: str  # slug
    name: str
    source: str | None
    weeks: int
    daysPerWeek: int  # training days in the first week
    exercises: int  # distinct exercises
    editable: bool
    basedOn: str | None
    version: int


class CatalogExercise(BaseModel):
    name: str
    sets: int  # main sets the user logged, for "often done" first


def _editable(program: Program, user_id: int) -> bool:
    return program.owner_user_id is not None and program.owner_user_id == user_id


async def _slugs(session: AsyncSession, ids: set[int]) -> dict[int, str]:
    if not ids:
        return {}
    rows = await session.execute(select(Program.id, Program.slug).where(Program.id.in_(ids)))
    return {i: slug for i, slug in rows}


def _program_out(program: Program, user_id: int, based_on: str | None) -> ProgramOut:
    return ProgramOut(
        id=program.slug,
        name=program.name,
        source=program.source,
        version=program.version,
        editable=_editable(program, user_id),
        basedOn=based_on,
        weeks=[
            ProgramWeekOut(
                number=w.number,
                days=[
                    ProgramDayOut(
                        id=d.id,
                        weekday=d.weekday,
                        title=WEEKDAY_TITLES[d.weekday] if 1 <= d.weekday <= 7 else str(d.weekday),
                        focus=d.focus,
                        exercises=[
                            ProgramExerciseOut(
                                id=i.id,
                                name=i.exercise.name,
                                intensity=i.intensity,
                                order=i.order,
                                prescription=PrescriptionOut(
                                    sets=i.sets,
                                    reps_min=i.reps_min,
                                    reps_max=i.reps_max,
                                    drop_reps=list(i.drop_reps) if i.drop_reps else None,
                                    raw=raw_prescription(i),
                                ),
                            )
                            for i in d.items
                        ],
                    )
                    for d in w.days
                ],
            )
            for w in program.weeks
        ],
    )


async def program_out(session: AsyncSession, program: Program, user_id: int) -> ProgramOut:
    """`program` with its tree loaded (load_program / visible_program)."""
    based = await _slugs(session, {program.based_on_id} if program.based_on_id else set())
    return _program_out(program, user_id, based.get(program.based_on_id) if program.based_on_id else None)


async def program_summaries(session: AsyncSession, user_id: int) -> list[ProgramSummary]:
    programs = await visible_programs(session, user_id)
    based = await _slugs(session, {p.based_on_id for p in programs if p.based_on_id})
    return [
        ProgramSummary(
            id=p.slug,
            name=p.name,
            source=p.source,
            weeks=len(p.weeks),
            daysPerWeek=len(p.weeks[0].days) if p.weeks else 0,
            exercises=len({i.exercise_id for w in p.weeks for d in w.days for i in d.items}),
            editable=_editable(p, user_id),
            basedOn=based.get(p.based_on_id) if p.based_on_id else None,
            version=p.version,
        )
        for p in programs
    ]


async def exercise_choices(session: AsyncSession, user_id: int) -> list[CatalogExercise]:
    """Exercises to pick from: those in programs the user sees and those the user logged, with the number of
    the user's main sets; most done first, then by name. Other users' own exercises stay out."""
    counts = dict(
        (
            await session.execute(
                select(WorkoutSet.exercise_id, func.count(WorkoutSet.id))
                .join(Workout, Workout.id == WorkoutSet.workout_id)
                .where(Workout.user_id == user_id, WorkoutSet.drop_index == 0)
                .group_by(WorkoutSet.exercise_id)
            )
        ).all()
    )
    in_programs = (
        select(ProgramItem.exercise_id)
        .join(ProgramDay, ProgramDay.id == ProgramItem.day_id)
        .join(ProgramWeek, ProgramWeek.id == ProgramDay.week_id)
        .join(Program, Program.id == ProgramWeek.program_id)
        .where(visible_to(user_id))
    )
    rows = await session.execute(
        select(Exercise.id, Exercise.name).where(or_(Exercise.id.in_(in_programs), Exercise.id.in_(list(counts))))
    )
    out = [CatalogExercise(name=name, sets=counts.get(ex_id, 0)) for ex_id, name in rows]
    return sorted(out, key=lambda e: (-e.sets, e.name))


def monday_of(d: date) -> date:
    return d - timedelta(days=d.weekday())


@dataclass
class Position:
    week: int
    weekday: int  # 1 = Monday
    finished: bool
    not_started: bool


def program_position(started_on: date, weeks: int, today: date) -> Position:
    """Same rule as the Mini App (miniapp/src/program.ts programPosition)."""
    days = (today - started_on).days
    week = min(max(days // 7 + 1, 1), weeks)
    return Position(week, today.isoweekday(), days >= weeks * 7, days < 0)


def find_day(program: Program, week: int, weekday: int) -> ProgramDay | None:
    for w in program.weeks:
        if w.number == week:
            return next((d for d in w.days if d.weekday == weekday), None)
    return None


def format_item(item: ProgramItem) -> str:
    if item.drop_reps:
        return f"{item.sets} × дропсет {'-'.join(map(str, item.drop_reps))}"
    if item.reps_min is None:
        return f"{item.sets} подх."
    reps = f"{item.reps_min}–{item.reps_max}" if item.reps_max and item.reps_max != item.reps_min else item.reps_min
    return f"{item.sets} × {reps}"
