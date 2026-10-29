"""Workouts for the owner's personal dashboard «Дела Амира» (GET /api/hub, static site on GitHub Pages).

Read only, never creates rows: the owner as in the MCP server (gymbot.services.access.owner_user), the
program through next_weights.program_ref / day_weights, so the week shows the day plan, the deload and the
manual day adjustments the way the Mini App does. No food, wellbeing or body weight here.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gymbot.config import Settings
from gymbot.db.models import Exercise, ProgramDay, User, Workout, WorkoutSet
from gymbot.mcp_server import num, set_text
from gymbot.services import day_adjustments as dayadj
from gymbot.services import deload
from gymbot.services import next_weights as nw
from gymbot.services.access import owner_user
from gymbot.services.nutrition import _aware
from gymbot.services.programs import find_day, monday_of

LOG_DAYS = 90
PLAN_EXERCISES = 5  # exercises named in a plan day's note
NOTE_MAX = 200
DEFAULT_TITLE = "Тренировка"
REST_NOTE = "Отдых по плану дня"


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]


def _short(text: str) -> str:
    return text if len(text) <= NOTE_MAX else text[: NOTE_MAX - 1].rstrip() + "…"


def _sets(sets: list[str]) -> str:
    """Runs of equal sets collapse: ["60×5"] * 5 -> "60×5×5"; different ones joined with " + "."""
    runs: list[list[Any]] = []
    for s in sets:
        if runs and runs[-1][0] == s:
            runs[-1][1] += 1
        else:
            runs.append([s, 1])
    return " + ".join(s if n == 1 else f"{s}×{n}" for s, n in runs)


def _tonnage(kg: Decimal) -> str:
    """3200 -> «3,2 т», 850 -> «850 кг»."""
    if kg >= 1000:
        return f"{num(kg / 1000)} т".replace(".", ",")
    return f"{num(kg)} кг".replace(".", ",")


async def _changes(session: AsyncSession, user_id: int, day: date) -> str | None:
    """«разгрузка: веса −15 %», «веса −20 %, без ног»: the deload and the day adjustment only. Not
    DayWeights.summary: today it is the day plan's note, which names sleep and pains, and the dashboard is
    public."""
    parts = []
    if await deload.active_until(session, user_id, day) is not None:
        parts.append(f"разгрузка: веса −{round((1 - deload.WEIGHT_FACTOR) * 100)} %")
    if (adj := await dayadj.get(session, user_id, day)) is not None and not adj.empty():
        parts.append(dayadj.describe(adj))
    return ", ".join(parts) or None


def _plan_note(names: list[str], changes: str | None) -> str:
    """Up to PLAN_EXERCISES names, fewer when the note would pass NOTE_MAX: the changes are never cut."""
    tail = f" · {changes}" if changes else ""
    for n in range(min(len(names), PLAN_EXERCISES), 0, -1):
        note = _cap(", ".join(names[:n]) + (", …" if len(names) > n else "") + tail)
        if len(note) <= NOTE_MAX:
            return note
    return _short(_cap(names[0] + ", …" + tail))


async def plan_week(
    session: AsyncSession, settings: Settings, user: User, tz: ZoneInfo, now_utc: datetime
) -> list[dict[str, Any]]:
    """One item per training day of the current week (Mon..Sun in TIMEZONE)."""
    ref = await nw.program_ref(session, user, settings)
    if ref is None:
        return []
    monday = monday_of(now_utc.astimezone(tz).date())
    out: list[dict[str, Any]] = []
    for k in range(7):
        day = monday + timedelta(days=k)
        dw = await nw.day_weights(session, user, settings, tz, now_utc, day, ref)
        if dw is None:
            continue
        program_day = find_day(ref.program, dw.week, dw.weekday)
        title = (program_day.focus if program_day else None) or DEFAULT_TITLE
        names = [r.name for r in dw.rows if r.suggestion is not None]
        if dw.rest or not names:
            note = REST_NOTE
        else:
            note = _plan_note(names, await _changes(session, user.id, day))
        # No "time": program days have none, and the dashboard expects the key left out.
        out.append({"day": dw.weekday, "title": title, "note": note})
    return out


async def workout_log(session: AsyncSession, user_id: int, today: date) -> list[dict[str, Any]]:
    """Saved workouts of the last LOG_DAYS local days; workouts without sets are left out."""
    rows = (
        await session.execute(
            select(Workout.id, Workout.performed_on, Workout.started_at, Workout.finished_at, ProgramDay.focus,
                   Exercise.name, WorkoutSet.weight_kg, WorkoutSet.reps, WorkoutSet.drop_index)
            .join(WorkoutSet, WorkoutSet.workout_id == Workout.id)
            .join(Exercise, Exercise.id == WorkoutSet.exercise_id)
            .outerjoin(ProgramDay, ProgramDay.id == Workout.program_day_id)
            .where(Workout.user_id == user_id, Workout.performed_on >= today - timedelta(days=LOG_DAYS - 1))
            .order_by(Workout.performed_on, Workout.id, WorkoutSet.set_index)
        )
    ).all()
    workouts: dict[int, dict[str, Any]] = {}
    exercises: defaultdict[int, dict[str, list[str]]] = defaultdict(dict)
    for wid, day, started, finished, focus, name, weight, reps, drop_index in rows:
        w = workouts.setdefault(
            wid, {"date": day, "title": focus or DEFAULT_TITLE, "started": started, "finished": finished,
                  "volume": Decimal(0)}
        )
        if weight is not None:
            w["volume"] += Decimal(weight) * reps
        sets = exercises[wid].setdefault(name, [])
        if drop_index and sets:  # same as mcp_server.training_summary
            sets[-1] += " → " + set_text(weight, reps)
        else:
            sets.append(set_text(weight, reps))
    out = []
    for wid, w in workouts.items():
        parts = [", ".join(f"{name} {_sets(sets)}" for name, sets in exercises[wid].items())]
        if w["volume"]:
            parts.append("тоннаж " + _tonnage(w["volume"]))
        if w["finished"] is not None:
            minutes = int((_aware(w["finished"]) - _aware(w["started"])).total_seconds() // 60)
            if minutes > 0:
                parts.append(f"{minutes} мин")
        out.append({"date": w["date"].isoformat(), "title": w["title"], "note": _cap(" · ".join(parts))})
    return out


async def build(session: AsyncSession, settings: Settings, tz: ZoneInfo, now_utc: datetime) -> dict[str, Any]:
    """The whole GET /api/hub answer; empty lists until the owner has reached the bot or the Mini App."""
    user = await owner_user(session, settings)
    if user is None:
        return {"workouts": {"plan": [], "log": []}}
    return {
        "workouts": {
            "plan": await plan_week(session, settings, user, tz, now_utc),
            "log": await workout_log(session, user.id, now_utc.astimezone(tz).date()),
        }
    }
