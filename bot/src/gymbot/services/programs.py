"""Ready-made programs: load data/programs/*.json into the DB and answer "what's planned when"."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from gymbot.db.models import Exercise, Program, ProgramDay, ProgramItem, ProgramWeek

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
    """Import every program JSON whose slug is not in the DB yet. Existing programs are left alone."""
    known = set((await session.scalars(select(Program.slug))).all())
    for path in sorted(programs_dir.glob("*.json")):
        if path.stem in known:
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        program = Program(slug=path.stem, name=data["name"], source=data.get("source"))
        for w in data["weeks"]:
            week = ProgramWeek(number=w["number"])
            for d in w["days"]:
                day = ProgramDay(weekday=d["weekday"])
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


async def load_program(session: AsyncSession, program_id: int) -> Program:
    stmt = (
        select(Program)
        .where(Program.id == program_id)
        .options(
            selectinload(Program.weeks)
            .selectinload(ProgramWeek.days)
            .selectinload(ProgramDay.items)
            .selectinload(ProgramItem.exercise)
        )
    )
    return (await session.scalars(stmt)).one()


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
