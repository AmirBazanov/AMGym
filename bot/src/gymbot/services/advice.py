"""AI advice on nutrition, training and recovery: a compact Russian summary of the user's data for the LLM.

The summary never contains secrets or identifiers (telegram_id, name): only the profile, the norm,
aggregated food and training numbers and the current program.

Nutrition is averaged over the 7 completed local days before today (today is not over yet and would
inflate the shortfall); today's intake so far is a separate line. Training and wellbeing (sleep, energy,
pains, the last note; gymbot.services.wellbeing) cover the last 14 local days including today.
Active user facts (gymbot.services.facts) follow the profile, newest first, cut to FACTS_IN_CONTEXT, then
the working weights the user named in them (gymbot.services.baselines.context_line, <= CONTEXT_CHARS).

The muscle load block (`muscle_load`) is counted here, not by the model: main sets per muscle group
(gymbot.services.plan.muscle_group, unmapped exercises are «прочее») over LOAD_DAYS local days and the
groups still recovering (>= RECOVERY_MIN_SETS main sets less than RECOVERY_HOURS ago) with the local time
they recover, so "what next" advice does not load the arms the day after an arms day, while the program's
next arms day 48 h later stays as planned. The program line names today's program day and the next training
day with its muscle groups (and its exercises when today is a rest day).
build_context only reads: it never creates users or programs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from gymbot.config import Settings
from gymbot.db.models import Exercise, Program, ProgramDay, User, UserProgram, Workout, WorkoutSet
from gymbot.llm.openrouter import OpenRouterClient
from gymbot.llm.prompts import ADVICE_DISCLAIMER, build_advice_messages, format_facts
from gymbot.services import baselines
from gymbot.services.facts import active_facts
from gymbot.services.nutrition import _aware, day_summary, user_targets, week_summary
from gymbot.services.plan import muscle_group
from gymbot.services.profile import GOAL_NAMES
from gymbot.services.programs import find_day, format_item, load_program, program_position
from gymbot.services.tg_format import plain
from gymbot.services.tg_html import to_html
from gymbot.services.wellbeing import context_lines as wellbeing_lines

FACTS_IN_CONTEXT = 400  # newest active facts that fit; the rest of the summary matters more
# Room for facts (~400), wellbeing (~450), the muscle load (~350) and two program days (~700) next to the
# exercise lines, which are dropped first. ~1100 tokens: far below Groq's 8000 TPM per model.
CONTEXT_MAX = 2900
FOOD_DAYS = 7
WORKOUT_DAYS = 14
MAX_EXERCISES = 8
MAX_PLAN_ITEMS = 6
ABOUT_IN_CONTEXT = 300
FEW_FOOD_DAYS = 4  # fewer days with food than this: ask to log more
TELEGRAM_MAX = 4096
LOAD_DAYS = 7
RECOVERY_HOURS = 48
RECOVERY_MIN_SETS = 3  # fewer main sets (a warm-up, a test set) in RECOVERY_HOURS need no rest
GROUP_NAMES = {
    "biceps": "бицепс", "triceps": "трицепс", "shoulders": "плечи", "chest": "грудь", "back": "спина",
    "legs": "ноги", "abs": "пресс",
}
MAIN_GROUPS = ("biceps", "triceps", "shoulders", "chest", "back", "legs")  # always listed, even with 0
OTHER = "прочее"  # exercises muscle_group does not know
WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
NEXT_DAY_SEARCH = 14  # days ahead to look for the next training day


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


# ---- muscle load ----


def group_label(name: str) -> str:
    """The Russian muscle group of an exercise for the load block, «прочее» when unknown."""
    group = muscle_group(name)
    return GROUP_NAMES[group] if group else OTHER


def _ago(day: date, today: date) -> str:
    n = (today - day).days
    return {0: "сегодня", 1: "вчера", 2: "позавчера"}.get(n, f"{n} дн. назад")


@dataclass
class _GroupLoad:
    sets: int = 0
    last_day: date | None = None
    recent_sets: int = 0  # main sets less than RECOVERY_HOURS ago
    recent_at: datetime | None = None  # the latest of those sessions (UTC)


async def muscle_load(session: AsyncSession, user_id: int, today: date, now_utc: datetime, tz: ZoneInfo) -> str:
    """Main sets per muscle group over LOAD_DAYS local days up to today and the groups still recovering:
    'Нагрузка по группам мышц за 7 дней: бицепс — 9 подх., последний раз 07.10 (вчера); …; грудь — 0.
    Восстанавливаются (48 ч после тренировки): бицепс, трицепс — до пт 09.10 17:00. Отдохнули: …'"""
    rows = (
        await session.execute(
            select(Workout.started_at, Workout.performed_on, Exercise.name, func.count(WorkoutSet.id))
            .join(WorkoutSet, WorkoutSet.workout_id == Workout.id)
            .join(Exercise, Exercise.id == WorkoutSet.exercise_id)
            .where(Workout.user_id == user_id, Workout.performed_on >= today - timedelta(days=LOAD_DAYS - 1),
                   Workout.performed_on <= today, WorkoutSet.drop_index == 0)
            .group_by(Workout.id, Workout.started_at, Workout.performed_on, Exercise.name)
        )
    ).all()
    head = f"Нагрузка по группам мышц за {LOAD_DAYS} дней"
    if not rows:
        return f"{head}: тренировок нет."
    loads = {label: _GroupLoad() for label in [*(GROUP_NAMES[g] for g in MAIN_GROUPS), GROUP_NAMES["abs"], OTHER]}
    for at, day, name, n in rows:
        load = loads[group_label(name)]
        load.sets += n
        load.last_day = max(load.last_day or day, day)
        if now_utc - (at := _aware(at)) < timedelta(hours=RECOVERY_HOURS):
            load.recent_sets += n
            load.recent_at = max(load.recent_at or at, at)
    main = {GROUP_NAMES[g] for g in MAIN_GROUPS}
    parts = [
        f"{label} — {load.sets} подх., последний раз {load.last_day:%d.%m} ({_ago(load.last_day, today)})"
        if load.last_day else f"{label} — 0"
        for label, load in loads.items()
        if label in main or load.sets
    ]
    lines = [f"{head} (основные подходы): " + "; ".join(parts) + "."]
    until: dict[str, list[str]] = {}  # "до пт 09.10 17:00" -> groups, in the order of the block
    for label, load in loads.items():
        if label != OTHER and load.recent_sets >= RECOVERY_MIN_SETS and load.recent_at is not None:
            free = (load.recent_at + timedelta(hours=RECOVERY_HOURS)).astimezone(tz)
            until.setdefault(f"до {WEEKDAYS[free.weekday()]} {free:%d.%m %H:%M}", []).append(label)
    if until:
        recovering = [label for labels in until.values() for label in labels]
        rested = [label for label in loads if label in main and label not in recovering]
        groups = "; ".join(f"{', '.join(labels)} — {when}" for when, labels in until.items())
        line = f"Восстанавливаются ({RECOVERY_HOURS} ч после тренировки): {groups}."
        lines.append(line + (f" Отдохнули: {', '.join(rested)}." if rested else ""))
    return "\n".join(lines)


async def recovery_times(session: AsyncSession, user_id: int, today: date, now_utc: datetime) -> dict[str, datetime]:
    """Muscle groups still recovering -> when they recover (UTC): >= RECOVERY_MIN_SETS main sets less than
    RECOVERY_HOURS ago, the same rule as the «Восстанавливаются» line of `muscle_load` («прочее» never)."""
    rows = (
        await session.execute(
            select(Workout.started_at, Exercise.name, func.count(WorkoutSet.id))
            .join(WorkoutSet, WorkoutSet.workout_id == Workout.id)
            .join(Exercise, Exercise.id == WorkoutSet.exercise_id)
            .where(Workout.user_id == user_id, Workout.performed_on >= today - timedelta(days=LOAD_DAYS - 1),
                   Workout.performed_on <= today, WorkoutSet.drop_index == 0)
            .group_by(Workout.id, Workout.started_at, Exercise.name)
        )
    ).all()
    loads: dict[str, _GroupLoad] = {}
    for at, name, n in rows:
        label = group_label(name)
        if label == OTHER or now_utc - (at := _aware(at)) >= timedelta(hours=RECOVERY_HOURS):
            continue
        load = loads.setdefault(label, _GroupLoad())
        load.recent_sets += n
        load.recent_at = max(load.recent_at or at, at)
    return {
        label: load.recent_at + timedelta(hours=RECOVERY_HOURS)
        for label, load in loads.items()
        if load.recent_sets >= RECOVERY_MIN_SETS and load.recent_at is not None
    }


# ---- program ----


def _day_plan(day: ProgramDay) -> str:
    items = sorted(day.items, key=lambda i: i.order)
    plan = ", ".join(f"{i.exercise.name} {format_item(i)}" for i in items[:MAX_PLAN_ITEMS])
    if len(items) > MAX_PLAN_ITEMS:
        plan += f" и ещё {len(items) - MAX_PLAN_ITEMS}"
    return plan


def next_training_day(program: Program, started_on: date, today: date) -> tuple[date, ProgramDay] | None:
    """The first program day with exercises after `today` (within NEXT_DAY_SEARCH days), None after the end."""
    weeks = len(program.weeks)
    for k in range(1, NEXT_DAY_SEARCH + 1):
        d = today + timedelta(days=k)
        pos = program_position(started_on, weeks, d)
        if pos.finished:
            return None
        if pos.not_started:
            continue
        day = find_day(program, pos.week, pos.weekday)
        if day is not None and day.items:
            return d, day
    return None


def _next_line(program: Program, started_on: date, today: date, items: bool = True) -> str:
    """' Следующая тренировка по программе: пт 09.10 (бицепс, трицепс): <exercises>.'; without `items` (today
    is a training day: today's plan is the next session) only the date and the groups."""
    found = next_training_day(program, started_on, today)
    if found is None:
        return ""
    d, day = found
    groups = list(dict.fromkeys(group_label(i.exercise.name) for i in sorted(day.items, key=lambda i: i.order)))
    line = f" Следующая тренировка по программе: {WEEKDAYS[d.weekday()]} {d:%d.%m} ({', '.join(groups)})"
    return line + (f": {_day_plan(day)}." if items else ".")


async def _program_line(
    session: AsyncSession, user: User, today: date, today_plan: bool = True
) -> str | None:
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
        return f"Программа «{program.name}» начнётся {up.started_on:%d.%m}.{_next_line(program, up.started_on, today)}"
    if pos.finished:
        return f"Программа «{program.name}» пройдена."
    line = f"Программа «{program.name}»: неделя {pos.week} из {weeks}"
    day = find_day(program, pos.week, pos.weekday)
    if day is None or not day.items:
        return line + f", сегодня день отдыха.{_next_line(program, up.started_on, today)}"
    following = _next_line(program, up.started_on, today, items=False)
    if not today_plan:
        return line + f", сегодня день тренировки (план на сегодня ниже).{following}"
    return line + f", сегодня по плану: {_day_plan(day)}.{following}"


# ---- public API ----


async def build_context(
    session: AsyncSession,
    user: User,
    settings: Settings,
    tz: ZoneInfo,
    now_utc: datetime,
    *,
    for_answer: bool = False,
) -> str:
    """Compact Russian summary (<= CONTEXT_MAX characters) of the user's data for the advice prompt.

    `for_answer`: the diary answer (gymbot.services.answer) adds its own counted blocks, so the per-exercise
    lines (its records block holds them with the whole history) and today's program day (its adjusted plan
    follows) are left out: the same numbers twice only cost tokens."""
    today = now_utc.astimezone(tz).date()
    facts = format_facts([f.text for f in await active_facts(session, user.id)], FACTS_IN_CONTEXT)
    weights = baselines.context_line(await baselines.current(session, user.id))
    head = [
        *_profile_lines(user, today),
        *([facts + "."] if facts else []),
        *([weights] if weights else []),
        _targets_line(user),
        *await _food_lines(session, user, today, tz),
    ]
    training, exercises = await _training(session, user, today)
    if for_answer:
        exercises = []
    load = await muscle_load(session, user.id, today, now_utc, tz)
    program = await _program_line(session, user, today, today_plan=not for_answer)
    wellbeing = await wellbeing_lines(session, user, today, tz)
    keep = [load, *([program] if program else [])]  # the prompts' rules rest on these: dropped last
    # Drop exercise lines (least recent first) until the summary fits.
    for n in range(min(len(exercises), MAX_EXERCISES), -1, -1):
        shown = exercises[:n]
        if len(exercises) > n:
            shown.append(f"- и ещё упражнений: {len(exercises) - n}")
        text = "\n".join([*head, training, *shown, load, *wellbeing, *keep[1:]])
        if len(text) <= CONTEXT_MAX:
            return text
    # Still too long: whole lines go, facts first, then working weights and wellbeing; the muscle load and
    # the program line (next training day) stay, and the rest is cut at a line end, never mid-line.
    optional = [x for x in (facts + "." if facts else None, weights) if x]
    head = [x for x in head if x not in optional]
    lines = [*head, training, *wellbeing]
    while lines and len("\n".join([*lines, *keep])) > CONTEXT_MAX:
        lines.pop()
    text = "\n".join([*lines, *keep])
    return text if len(text) <= CONTEXT_MAX else text[: CONTEXT_MAX - 1] + "…"


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
    text = plain(await llm.complete_text(build_advice_messages(context), purpose="advice"))
    if ADVICE_DISCLAIMER.lower() not in text.lower():
        text = f"{text}\n\n{ADVICE_DISCLAIMER}"
    if len(text) > TELEGRAM_MAX:
        text = text[: TELEGRAM_MAX - len(ADVICE_DISCLAIMER) - 3].rstrip() + "…\n\n" + ADVICE_DISCLAIMER
    return text


ADVICE_HEADERS = ("Питание", "Тренировки", "Восстановление")  # the three blocks of ADVICE_SYSTEM_PROMPT


def html(text: str) -> str:
    """Telegram HTML of `generate`'s plain text: the block headers bold, «- » items as «• », the rest
    escaped (gymbot.services.tg_html)."""
    lines = to_html(text).split("\n")
    return "\n".join(
        f"<b>{line}</b>" if line.strip().rstrip(":") in ADVICE_HEADERS else line for line in lines
    )
