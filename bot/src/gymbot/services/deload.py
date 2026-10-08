"""Auto deload: notice that progress stalled (or that it is time), offer a deload week in the chat once,
and while it runs make the adaptive day plan lighter (gymbot.services.plan: weights × WEIGHT_FACTOR, sets
cut by a third).

Detection (`evaluate`, on a saved workout and on /plan), for the user's current program:
- main lifts: exercises of the program with >= MAIN_MIN_SESSIONS sessions (local days) in MAIN_WINDOW days;
- a stall: the best 1RM (Epley) of the last 3 sessions not above the best of the 3 before by more than
  TOLERANCE, or the reps at the same top weight falling 2 sessions in a row (3 sessions);
- a deload is suggested when >= STALLED_LIFTS main lifts stall, or >= WEEKS_WITHOUT weeks passed since the
  last deload (or the program start) while training regularly, or the wellbeing log has >= LOW_ENTRIES
  low-energy / poor-sleep entries in LOW_DAYS days.
Only sessions after the last deload count: its lighter weights would look like a stall.

The program's own deload weeks are respected: a week marked `"deload": true` in its JSON, or whose items
are all of "light" intensity, counts as a deload (the current program has none). While one runs or starts
within PROGRAM_SOON days nothing is offered, and a passed one resets the count like ours.

Offer cadence (`DeloadState.ask_after`): an offer is sent at most once per LATER (an ignored offer is not
repeated on every workout); «Позже» asks again in LATER, «Нет» in NO; after a deload AFTER_DELOAD passes
from its end. Never while a deload runs. Every function takes the time as an argument (tests pin it).
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gymbot.db.models import DeloadState, Exercise, Program, User, UserProgram, Workout, WorkoutSet
from gymbot.services.programs import load_program, program_position
from gymbot.services.wellbeing import recent_entries

DELOAD_DAYS = 7
WEIGHT_FACTOR = 0.85
SETS_SHARE = 2 / 3
MAIN_WINDOW, MAIN_MIN_SESSIONS = 28, 3
STALL_SESSIONS = 3
HISTORY_DAYS = 16 * 7  # sessions older than this are not compared
TOLERANCE = 0.01
STALLED_LIFTS = 2
WEEKS_WITHOUT = 6
REGULAR_DAYS, REGULAR_WINDOW = 6, 28  # training days in the window for "regularly"
LOW_ENTRIES, LOW_DAYS = 3, 7
LOW_ENERGY, LOW_SLEEP_QUALITY, SHORT_SLEEP = 2, 2, 6.0  # <= energy, <= quality, < hours
PROGRAM_SOON = 14
LATER, NO, AFTER_DELOAD = timedelta(days=3), timedelta(days=14), timedelta(days=14)
WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")

QUESTION = "Сделать разгрузочную неделю? Веса −15 %, подходов на треть меньше."


def deload_sets(sets: int) -> int:
    """A third fewer sets, rounded up: 6 -> 4, 4 -> 3, 3 -> 2, 1 -> 1."""
    return max(1, math.ceil(sets * SETS_SHARE - 1e-9))


def summary(until: date) -> str:
    """The day plan's reason: «Разгрузочная неделя до пт 16.10: веса −15 %, подходов меньше.»"""
    pct = round((1 - WEIGHT_FACTOR) * 100)
    return f"Разгрузочная неделя до {WEEKDAYS[until.weekday()]} {until:%d.%m}: веса −{pct} %, подходов меньше."


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)


# ---- state ----


async def get_state(session: AsyncSession, user_id: int) -> DeloadState | None:
    return await session.get(DeloadState, user_id)


async def _state(session: AsyncSession, user_id: int) -> DeloadState:
    st = await get_state(session, user_id)
    if st is None:
        st = DeloadState(user_id=user_id)
        session.add(st)
    return st


def active(st: DeloadState | None, today: date) -> bool:
    return st is not None and st.started_on is not None and st.until is not None and st.started_on <= today <= st.until


async def active_until(session: AsyncSession, user_id: int, today: date) -> date | None:
    """The last day of the deload running on `today`, else None (read by the day plan)."""
    st = await get_state(session, user_id)
    return st.until if active(st, today) else None


async def start(session: AsyncSession, user_id: int, today: date, now: datetime) -> date:
    """Start a deload week today (no commit); returns its last day."""
    st = await _state(session, user_id)
    st.started_on, st.until = today, today + timedelta(days=DELOAD_DAYS - 1)
    st.ask_after = datetime.combine(st.until, datetime.min.time(), UTC) + AFTER_DELOAD
    st.updated_at = now
    return st.until


async def cancel(session: AsyncSession, user_id: int, today: date, now: datetime) -> bool:
    """Stop the running deload (no commit). Begun today: as if it never was; else it ended yesterday."""
    st = await get_state(session, user_id)
    if not active(st, today):
        return False
    assert st is not None and st.started_on is not None
    if st.started_on >= today:
        st.started_on = st.until = None
    else:
        st.until = today - timedelta(days=1)
    st.ask_after = now + NO  # just said no to it
    st.updated_at = now
    return True


async def postpone(session: AsyncSession, user_id: int, now: datetime, refuse: bool) -> None:
    """«Позже» (ask again in LATER) or «Нет» (in NO); no commit."""
    st = await _state(session, user_id)
    st.ask_after = now + (NO if refuse else LATER)
    st.updated_at = now


def due(st: DeloadState | None, today: date, now: datetime) -> bool:
    """An offer may be sent now: no deload running and the snooze is over."""
    if active(st, today):
        return False
    ask_after = _aware(st.ask_after) if st is not None else None
    return ask_after is None or now >= ask_after


# ---- detection ----


@dataclass(frozen=True)
class SessionStat:
    day: date
    best_e1rm: float
    top_weight: float
    reps_at_top: int


def _e1rm(weight: float, reps: int) -> float:
    """Epley, as gymbot.services.advice.epley (not imported: advice pulls in the plan, which imports this)."""
    return weight if reps == 1 else weight * (1 + reps / 30)


def session_stat(day: date, sets: list[tuple[float, int]]) -> SessionStat:
    """`sets`: (weight > 0, reps > 0) main sets of one exercise on one day."""
    top = max(w for w, _ in sets)
    return SessionStat(day, max(_e1rm(w, r) for w, r in sets), top, max(r for w, r in sets if w == top))


def stalled(sessions: list[SessionStat]) -> bool:
    """Sessions oldest first. No 1RM growth over the last 3 against the 3 before (within TOLERANCE), or
    reps at the same top weight falling 2 sessions in a row."""
    n = STALL_SESSIONS
    if len(sessions) >= 2 * n:
        last = max(s.best_e1rm for s in sessions[-n:])
        before = max(s.best_e1rm for s in sessions[-2 * n:-n])
        if last <= before * (1 + TOLERANCE):
            return True
    if len(sessions) >= n:
        a, b, c = sessions[-n:]
        if a.top_weight == b.top_weight == c.top_weight and a.reps_at_top > b.reps_at_top > c.reps_at_top:
            return True
    return False


def program_deload_weeks(
    program: Program, programs_dir: Path | None = None, json_slug: str | None = None
) -> set[int]:
    """Week numbers the program itself makes a deload: `"deload": true` in its JSON, or only light items.

    The JSON is `<json_slug or program.slug>.json`. A user's own copy has no file of its own: pass its
    template's slug (`template_slug`), or nothing is read from JSON for it (week numbers never change in a
    copy, so the template's deload weeks are the copy's)."""
    out = {
        w.number for w in program.weeks
        if (items := [i for d in w.days for i in d.items]) and all(i.intensity == "light" for i in items)
    }
    slug = json_slug or (program.slug if program.owner_user_id is None else None)
    if programs_dir is not None and slug is not None:
        try:
            data = json.loads((programs_dir / f"{slug}.json").read_text(encoding="utf-8"))
            out |= {w.get("number") for w in data.get("weeks", []) if isinstance(w, dict) and w.get("deload") is True}
        except (OSError, ValueError, AttributeError):
            pass
    return {n for n in out if isinstance(n, int)}


async def template_slug(session: AsyncSession, program: Program) -> str | None:
    """The slug of the JSON file behind `program`: its own for a template, the template's for a user's copy
    (None when that template is gone)."""
    if program.owner_user_id is None:
        return program.slug
    if program.based_on_id is None:
        return None
    return await session.scalar(
        select(Program.slug).where(Program.id == program.based_on_id, Program.owner_user_id.is_(None))
    )


@dataclass
class Verdict:
    suggest: bool = False
    stalls: list[str] = field(default_factory=list)  # names of stalled main lifts
    main_lifts: list[str] = field(default_factory=list)
    weeks: int | None = None  # full weeks since the last deload or the program start, when that rule fired
    low_entries: int = 0  # when the wellbeing rule fired
    program_deload: bool = False  # the program's own deload week runs or is near: nothing is offered

    def reasons(self) -> list[str]:
        out = []
        if len(self.stalls) >= STALLED_LIFTS:
            names = self.stalls[:3]
            joined = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " и " + names[-1]
            out.append(f"Похоже, прогресс встал: {joined} {STALL_SESSIONS} тренировки без роста.")
        if self.weeks is not None:
            out.append(f"Уже {self.weeks} {_weeks_word(self.weeks)} без разгрузки.")
        if self.low_entries:
            n = self.low_entries
            times = "раз" if n % 10 in (0, 5, 6, 7, 8, 9) or 11 <= n % 100 <= 14 or n % 10 == 1 and n % 100 != 11 else "раза"
            out.append(f"За неделю {n} {times} мало сна или сил.")
        return out


def _weeks_word(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "неделя"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return "недели"
    return "недель"


def offer_text(v: Verdict) -> str:
    return " ".join([*v.reasons(), QUESTION])


async def evaluate(
    session: AsyncSession, user: User, today: date, tz: ZoneInfo, programs_dir: Path | None = None
) -> Verdict:
    """Whether a deload is due by the rules in the module doc (reads only)."""
    v = Verdict()
    st = await get_state(session, user.id)
    last_deload_end = st.until if st is not None and st.until is not None and st.until < today else None
    up = await session.scalar(
        select(UserProgram).where(UserProgram.user_id == user.id).order_by(UserProgram.id.desc()).limit(1)
    )
    main_ids: set[int] = set()
    reference: date | None = last_deload_end
    if up is not None:
        program = await load_program(session, up.program_id)
        main_ids = {i.exercise_id for w in program.weeks for d in w.days for i in d.items}
        own = program_deload_weeks(program, programs_dir, await template_slug(session, program))
        weeks = len(program.weeks)
        if own:
            for ahead in range(PROGRAM_SOON + 1):
                pos = program_position(up.started_on, weeks, today + timedelta(days=ahead))
                if not pos.finished and not pos.not_started and pos.week in own:
                    v.program_deload = True
                    break
            passed = [up.started_on + timedelta(days=7 * n - 1) for n in own]  # the week's last day
            passed = [d for d in passed if d < today]
            if passed:
                reference = max(filter(None, [reference, max(passed)]))
        if up.started_on <= today:
            reference = max(filter(None, [reference, up.started_on]))
    cutoff = max(filter(None, [last_deload_end, today - timedelta(days=HISTORY_DAYS)]))
    rows = (
        await session.execute(
            select(WorkoutSet.exercise_id, Exercise.name, Workout.performed_on, WorkoutSet.weight_kg, WorkoutSet.reps)
            .join(Workout, Workout.id == WorkoutSet.workout_id)
            .join(Exercise, Exercise.id == WorkoutSet.exercise_id)
            .where(Workout.user_id == user.id, Workout.performed_on > cutoff, Workout.performed_on <= today,
                   WorkoutSet.drop_index == 0)
            .order_by(Workout.performed_on, WorkoutSet.set_index)
        )
    ).all()
    by_ex: dict[int, tuple[str, dict[date, list[tuple[float, int]]]]] = {}
    training_days: set[date] = set()
    for ex_id, name, day, weight, reps in rows:
        training_days.add(day)
        if ex_id not in main_ids or weight is None or weight <= 0 or reps <= 0:
            continue
        by_ex.setdefault(ex_id, (name, {}))[1].setdefault(day, []).append((float(weight), reps))
    window = today - timedelta(days=MAIN_WINDOW - 1)
    for name, days in by_ex.values():
        if sum(1 for d in days if d >= window) < MAIN_MIN_SESSIONS:
            continue
        v.main_lifts.append(name)
        if stalled([session_stat(d, sets) for d, sets in sorted(days.items())]):
            v.stalls.append(name)
    regular = sum(1 for d in training_days if d >= today - timedelta(days=REGULAR_WINDOW - 1)) >= REGULAR_DAYS
    if reference is not None and regular and (today - reference).days >= WEEKS_WITHOUT * 7:
        v.weeks = (today - reference).days // 7
    low = 0
    for e in await recent_entries(session, user, today, LOW_DAYS, tz):
        if (
            (e.energy is not None and e.energy <= LOW_ENERGY)
            or (e.sleep_quality is not None and e.sleep_quality <= LOW_SLEEP_QUALITY)
            or (e.sleep_hours is not None and float(e.sleep_hours) < SHORT_SLEEP)
        ):
            low += 1
    if low >= LOW_ENTRIES:
        v.low_entries = low
    v.suggest = not v.program_deload and bool(v.reasons())
    return v


async def maybe_offer(
    session: AsyncSession, user_id: int, now: datetime, tz: ZoneInfo, programs_dir: Path | None = None
) -> str | None:
    """The offer text when a deload is suggested and an offer is due; marks it sent (caller commits)."""
    today = now.astimezone(tz).date()
    st = await get_state(session, user_id)
    if not due(st, today, now):
        return None
    user = await session.get(User, user_id)
    if user is None:
        return None
    v = await evaluate(session, user, today, tz, programs_dir)
    if not v.suggest:
        return None
    st = await _state(session, user_id)
    st.offered_at, st.ask_after, st.updated_at = now, now + LATER, now
    return offer_text(v)


# ---- chat ----


def offer_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="✅ Да, неделю", callback_data="deload:yes"),
            InlineKeyboardButton(text="Позже", callback_data="deload:later"),
            InlineKeyboardButton(text="Нет", callback_data="deload:no"),
        ]]
    )


def status_text(st: DeloadState | None, v: Verdict, today: date) -> str:
    """/deload: the running deload, or the last one and what the detection sees."""
    if active(st, today):
        assert st is not None and st.until is not None
        return summary(st.until).replace("Разгрузочная неделя до", "Идёт разгрузочная неделя до", 1)
    lines = ["Разгрузки сейчас нет."]
    if st is not None and st.started_on is not None and st.until is not None:
        lines.append(f"Последняя: {st.started_on:%d.%m}–{st.until:%d.%m}.")
    if v.program_deload:
        lines.append("В программе скоро своя разгрузочная неделя.")
    if v.reasons():
        lines += v.reasons()
    elif v.main_lifts:
        lines.append(f"Основные упражнения растут ({len(v.main_lifts)}): разгрузка пока не нужна.")
    else:
        lines.append("Для оценки мало тренировок: нужно 3 сессии упражнения за 4 недели.")
    return "\n".join(lines)
