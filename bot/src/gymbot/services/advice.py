"""AI advice on nutrition, training and recovery: a compact Russian summary of the user's data for the LLM.

The summary never contains secrets or identifiers (telegram_id, name): only the profile, the norm,
aggregated food and training numbers and the current program.

Nutrition is averaged over the 7 completed local days before today (today is not over yet and would
inflate the shortfall); today's intake so far is a separate line. Training and wellbeing (sleep, energy,
pains, the last note; gymbot.services.wellbeing) cover the last 14 local days including today.
build_context only reads: it never creates users or programs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gymbot.config import Settings
from gymbot.db.models import Exercise, User, UserProgram, Workout, WorkoutSet
from gymbot.llm.openrouter import OpenRouterClient
from gymbot.llm.prompts import ADVICE_DISCLAIMER, build_advice_messages
from gymbot.services.nutrition import day_summary, user_targets, week_summary
from gymbot.services.profile import GOAL_NAMES
from gymbot.services.programs import find_day, format_item, load_program, program_position
from gymbot.services.wellbeing import context_lines as wellbeing_lines

CONTEXT_MAX = 1800  # room for the wellbeing block (~450) next to 8 exercises and the program
FOOD_DAYS = 7
WORKOUT_DAYS = 14
MAX_EXERCISES = 8
MAX_PLAN_ITEMS = 6
ABOUT_IN_CONTEXT = 300
FEW_FOOD_DAYS = 4  # fewer days with food than this: ask to log more
TELEGRAM_MAX = 4096


def _n(x: float | Decimal) -> str:
    """82.5 -> '82.5', 60.0 -> '60', 2449.6 -> '2450' (large numbers rounded to whole)."""
    v = float(x)
    if abs(v) >= 100 or v == int(v):
        return str(round(v))
    return f"{v:.1f}"


def epley(weight: float, reps: int) -> float:
    """Estimated one-rep max; a single is the 1RM itself."""
    return weight if reps == 1 else weight * (1 + reps / 30)


# ---- profile and norm ----


def _profile_lines(user: User, today: date) -> list[str]:
    facts = []
    if user.birth_year:
        facts.append(f"{today.year - user.birth_year} лет")
    if user.weight_kg is not None:
        facts.append(f"вес {_n(user.weight_kg)} кг")
    if user.height_cm:
        facts.append(f"рост {user.height_cm} см")
    if user.weight_kg is not None and user.height_cm:
        facts.append(f"ИМТ {float(user.weight_kg) / (user.height_cm / 100) ** 2:.1f}")
    lines = []
    if facts or user.goal:
        line = "Профиль: " + (", ".join(facts) if facts else "без данных о теле")
        if user.goal:
            line += f". Цель: {GOAL_NAMES.get(user.goal, user.goal)}"
        lines.append(line + ".")
    else:
        lines.append("Профиль не заполнен: запиши вес, рост, год рождения и цель в настройках дневника.")
    if user.about:
        about = " ".join(user.about.split())
        if len(about) > ABOUT_IN_CONTEXT:
            about = about[: ABOUT_IN_CONTEXT - 1] + "…"
        lines.append(f"О себе: {about}")
    return lines


def _targets_line(user: User) -> str:
    t = user_targets(user)
    if t.kcal is None and t.protein is None:
        return "Норма КБЖУ не задана."
    parts = [f"{t.kcal} ккал" if t.kcal is not None else None]
    parts += [f"{name} {v} г" for name, v in (("Б", t.protein), ("Ж", t.fat), ("У", t.carbs)) if v is not None]
    return "Норма в день: " + ", ".join(p for p in parts if p) + "."


# ---- nutrition ----


def _gap(actual: float, target: int | None, what: str, label: str) -> str | None:
    """'недобор 350 ккал' / 'перебор белка 20 г' (`what` is a format with {n}), 'белок в норме', None without a norm."""
    if target is None:
        return None
    diff = actual - target
    if round(diff) == 0:
        return f"{label} в норме"
    return f"{'перебор' if diff > 0 else 'недобор'} " + what.format(n=_n(abs(diff)))


async def _food_lines(session: AsyncSession, user: User, today: date, tz: ZoneInfo) -> list[str]:
    week = await week_summary(session, user, today - timedelta(days=1), tz)
    days = [d for d in week.days if d.entries]
    t = week.targets
    lines = []
    if days:
        avg = {k: sum(getattr(d, k) for d in days) / len(days) for k in ("kcal", "protein", "fat", "carbs")}
        line = (
            f"Питание за {FOOD_DAYS} дней до сегодня: записи в {len(days)} из {FOOD_DAYS} дней; "
            f"в среднем за день с записями {_n(avg['kcal'])} ккал, Б {_n(avg['protein'])} г, "
            f"Ж {_n(avg['fat'])} г, У {_n(avg['carbs'])} г"
        )
        gaps = [g for g in (_gap(avg["kcal"], t.kcal, "{n} ккал", "ккал"), _gap(avg["protein"], t.protein, "белка {n} г", "белок")) if g]
        if gaps:
            line += "; к норме: " + ", ".join(gaps)
        lines.append(line + ".")
        if len(days) < FEW_FOOD_DAYS:
            lines.append("Дней с записями мало: запиши всё съеденное ещё хотя бы за несколько дней подряд.")
    else:
        lines.append(f"Питание за {FOOD_DAYS} дней: записей нет, запиши всё съеденное хотя бы за 3 дня.")
    today_sum = await day_summary(session, user, today, tz)
    if today_sum.entries:
        line = f"Сегодня пока: {_n(today_sum.totals.kcal)} ккал, Б {_n(today_sum.totals.protein)} г"
        left = [
            f"{_n(v)} {unit}"
            for v, unit in ((today_sum.remaining.kcal, "ккал"), (today_sum.remaining.protein, "г белка"))
            if v is not None and round(v) > 0
        ]
        if left:
            line += "; до нормы осталось " + ", ".join(left)
        lines.append(line + ".")
    return lines


# ---- training ----


@dataclass
class _ExerciseStats:
    name: str
    last_key: tuple[date, int] = (date.min, 0)  # (performed_on, workout id) of the latest session
    last_sets: list[tuple[float | None, int]] = field(default_factory=list)
    best_e1rm: float = 0.0


def _last_time(ex: _ExerciseStats) -> str:
    weighted = [(w, r) for w, r in ex.last_sets if w is not None]
    when = f"{ex.last_key[0]:%d.%m}"
    if weighted:
        w, r = max(weighted)
        return f"прошлый раз {when}: {len(ex.last_sets)} подх., лучший {_n(w)}×{r}"
    reps = "/".join(str(r) for _, r in ex.last_sets)
    return f"прошлый раз {when}: {len(ex.last_sets)} подх. по {reps} без веса"


async def _training(session: AsyncSession, user: User, today: date) -> tuple[str, list[str]]:
    """(summary line, per-exercise lines, most recent first)."""
    start = today - timedelta(days=WORKOUT_DAYS - 1)
    rows = (
        await session.execute(
            select(Workout.id, Workout.performed_on, Exercise.name, WorkoutSet.weight_kg, WorkoutSet.reps,
                   WorkoutSet.drop_index)
            .join(WorkoutSet, WorkoutSet.workout_id == Workout.id)
            .join(Exercise, Exercise.id == WorkoutSet.exercise_id)
            .where(Workout.user_id == user.id, Workout.performed_on >= start, Workout.performed_on <= today)
            .order_by(Workout.performed_on, Workout.id, WorkoutSet.set_index)
        )
    ).all()
    if not rows:
        empty = f"Тренировок за {WORKOUT_DAYS} дней нет: запиши тренировки с весом и повторами (в дневнике или в чат)."
        return empty, []
    workouts: set[int] = set()
    volume = Decimal(0)
    stats: dict[str, _ExerciseStats] = {}
    for wid, day, name, weight, reps, drop_index in rows:
        workouts.add(wid)
        if weight is not None:
            volume += Decimal(weight) * reps
        ex = stats.setdefault(name, _ExerciseStats(name))
        if weight is not None and reps:
            ex.best_e1rm = max(ex.best_e1rm, epley(float(weight), reps))
        if drop_index:  # drops are part of their main set; "last time" shows main sets only
            continue
        key = (day, wid)
        if key > ex.last_key:
            ex.last_key, ex.last_sets = key, []
        if key == ex.last_key:
            ex.last_sets.append((float(weight) if weight is not None else None, reps))
    last_day = max(day for _, day, *_ in rows)
    summary = (
        f"Тренировки за {WORKOUT_DAYS} дней: {len(workouts)}, последняя {last_day:%d.%m}, "
        f"объём {_n(volume)} кг (вес×повторы)."
    )
    lines = []
    for ex in sorted(stats.values(), key=lambda e: e.last_key, reverse=True):
        if not ex.last_sets:
            continue
        line = f"- {ex.name}: {_last_time(ex)}"
        if ex.best_e1rm:
            line += f"; 1ПМ по Эпли {_n(ex.best_e1rm)} кг"
        lines.append(line)
    return summary, lines


# ---- program ----


async def _program_line(session: AsyncSession, user: User, today: date) -> str | None:
    # Not users.active_program(): that one creates a program on first use, and this module only reads.
    up = await session.scalar(
        select(UserProgram).where(UserProgram.user_id == user.id).order_by(UserProgram.id.desc()).limit(1)
    )
    if up is None:
        return None
    program = await load_program(session, up.program_id)
    weeks = len(program.weeks)
    pos = program_position(up.started_on, weeks, today)
    if pos.not_started:
        return f"Программа «{program.name}» начнётся {up.started_on:%d.%m}."
    if pos.finished:
        return f"Программа «{program.name}» пройдена."
    line = f"Программа «{program.name}»: неделя {pos.week} из {weeks}"
    day = find_day(program, pos.week, pos.weekday)
    if day is None:
        return line + ", сегодня день отдыха."
    items = sorted(day.items, key=lambda i: i.order)
    plan = ", ".join(f"{i.exercise.name} {format_item(i)}" for i in items[:MAX_PLAN_ITEMS])
    if len(items) > MAX_PLAN_ITEMS:
        plan += f" и ещё {len(items) - MAX_PLAN_ITEMS}"
    return line + f", сегодня по плану: {plan}."


# ---- public API ----


async def build_context(
    session: AsyncSession, user: User, settings: Settings, tz: ZoneInfo, now_utc: datetime
) -> str:
    """Compact Russian summary (<= CONTEXT_MAX characters) of the user's data for the advice prompt."""
    today = now_utc.astimezone(tz).date()
    head = [*_profile_lines(user, today), _targets_line(user), *await _food_lines(session, user, today, tz)]
    training, exercises = await _training(session, user, today)
    program = await _program_line(session, user, today)
    tail = [*await wellbeing_lines(session, user, today, tz), *([program] if program else [])]
    # Drop exercise lines (least recent first) until the summary fits.
    for n in range(min(len(exercises), MAX_EXERCISES), -1, -1):
        shown = exercises[:n]
        if len(exercises) > n:
            shown.append(f"- и ещё упражнений: {len(exercises) - n}")
        text = "\n".join([*head, training, *shown, *tail])
        if len(text) <= CONTEXT_MAX:
            return text
    return text[: CONTEXT_MAX - 1] + "…"


async def generate(
    session: AsyncSession,
    user: User,
    settings: Settings,
    llm: OpenRouterClient,
    tz: ZoneInfo,
    now_utc: datetime,
) -> str:
    """Advice text for the chat. Raises LLMError when no model answered."""
    context = await build_context(session, user, settings, tz, now_utc)
    text = await llm.complete_text(build_advice_messages(context))
    if ADVICE_DISCLAIMER.lower() not in text.lower():
        text = f"{text}\n\n{ADVICE_DISCLAIMER}"
    if len(text) > TELEGRAM_MAX:
        text = text[: TELEGRAM_MAX - len(ADVICE_DISCLAIMER) - 3].rstrip() + "…\n\n" + ADVICE_DISCLAIMER
    return text
