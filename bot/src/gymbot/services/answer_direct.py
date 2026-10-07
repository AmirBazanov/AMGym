"""Factual diary answers built in code from the database, without a model (layer 1 of the diary answer).

`reply` answers a question that gymbot.services.answer_intent.classify recognized: the workout of a day
(sets and tonnage per exercise), food of a day against the norm, an exercise's last time and record. It
returns None when the question cannot be answered for sure (an exercise it does not know): then the model
answers, checked by gymbot.services.answer_check. The same numbers go to the model's summary as blocks
(`food_block`, `records_block`, gymbot.services.answer.done_block), so both paths say the same.

Only reads. Days are local dates in TIMEZONE (Workout.performed_on is one already), weights in kg.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from gymbot.db.models import Exercise, User, Workout, WorkoutSet
from gymbot.services import active_workout, nutrition
from gymbot.services.advice import epley
from gymbot.services.answer_intent import (
    QUESTION_WORDS,
    Intent,
    Question,
    head_matches,
    mentions,
    same,
    words,
)

Sets = list[tuple[Decimal | None, int]]

MAX_EXERCISES_IN_REPLY = 4  # "в жиме" with several presses in the history
RECORDS_IN_REPLY = 8  # "какой мой рекорд?" without an exercise
RECORDS_IN_CONTEXT = 10
RECORDS_MAX = 1000  # characters of the records block in the model's summary
FOOD_LIST_MAX = 12


# ---- formatting ----


def kg(x: Decimal | float) -> str:
    """60 -> '60', 17.5 -> '17,5' (as done_block has always shown weights)."""
    v = float(x)
    return str(int(v)) if v == int(v) else f"{v:g}".replace(".", ",")


def num(x: float, digits: int = 1) -> str:
    """Food and 1RM numbers: whole from 10 up, else one decimal, decimal comma."""
    if abs(x) >= 10 or digits == 0:
        return str(round(x))
    v = round(x, digits)
    return kg(v)


def runs(sets: Sets) -> str:
    """'20×8 ×3, 17,5×8 ×2, 15×8': equal sets in a row merged."""
    merged: list[list] = []
    for s in sets:
        if merged and merged[-1][0] == s:
            merged[-1][1] += 1
        else:
            merged.append([s, 1])
    out = []
    for (w, r), n in merged:
        one = f"{kg(w)}×{r}" if w is not None else f"{r} повт."
        out.append(one + (f" ×{n}" if n > 1 else ""))
    return ", ".join(out)


def tonnage(sets: Sets) -> Decimal:
    return sum((Decimal(w) * r for w, r in sets if w is not None), Decimal(0))


def plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]


# ---- workouts ----


async def last_training_day(session: AsyncSession, user_id: int, upto: date) -> date | None:
    return await session.scalar(
        select(func.max(Workout.performed_on)).where(Workout.user_id == user_id, Workout.performed_on <= upto)
    )


async def day_sets(session: AsyncSession, user_id: int, day: date) -> dict[str, Sets]:
    """Sets of the local day `day` by exercise, in the order they were done (all workouts of the day)."""
    rows = (
        await session.execute(
            select(Exercise.name, WorkoutSet.weight_kg, WorkoutSet.reps)
            .select_from(Workout)
            .join(WorkoutSet, WorkoutSet.workout_id == Workout.id)
            .join(Exercise, Exercise.id == WorkoutSet.exercise_id)
            .where(Workout.user_id == user_id, Workout.performed_on == day)
            .order_by(Workout.started_at, Workout.id, WorkoutSet.set_index)
        )
    ).all()
    by_ex: dict[str, Sets] = {}
    for name, weight, reps in rows:
        by_ex.setdefault(name, []).append((weight, reps))
    return by_ex


def day_head(by_ex: dict[str, Sets]) -> str:
    n_ex, n_sets = len(by_ex), sum(len(v) for v in by_ex.values())
    total = sum((tonnage(v) for v in by_ex.values()), Decimal(0))
    return (
        f"{n_ex} {plural(n_ex, 'упражнение', 'упражнения', 'упражнений')}, "
        f"{n_sets} {plural(n_sets, 'подход', 'подхода', 'подходов')}, тоннаж {kg(total)} кг"
    )


def day_lines(by_ex: dict[str, Sets]) -> list[str]:
    return [f"- {name}: {runs(sets)} ({kg(tonnage(sets))} кг)" for name, sets in by_ex.items()]


async def workout_reply(
    session: AsyncSession, user_id: int, which: str, today: date, in_progress: str = ""
) -> str:
    """The workout of today / yesterday / the last training day, counted here; `in_progress` is the Mini
    App line (gymbot.services.active_workout), added for today and the last day."""
    wanted = {"today": today, "yesterday": today - timedelta(days=1)}.get(which)
    lines: list[str] = []
    if wanted is not None:
        by_ex = await day_sets(session, user_id, wanted)
        label = "Сегодня" if which == "today" else f"Вчера ({wanted:%d.%m})"
        if by_ex:
            lines += [f"{label}: {day_head(by_ex)}.", *day_lines(by_ex)]
        else:
            lines.append(f"{label} в истории тренировки нет.")
            last = await last_training_day(session, user_id, today)
            if last is not None:
                prev = await day_sets(session, user_id, last)
                when = "сегодня" if last == today else f"{last:%d.%m}"
                lines += [f"Последняя тренировка — {when}: {day_head(prev)}.", *day_lines(prev)]
            else:
                lines.append("Тренировок в истории пока нет.")
    else:
        last = await last_training_day(session, user_id, today)
        if last is None:
            lines.append("Тренировок в истории пока нет.")
        else:
            by_ex = await day_sets(session, user_id, last)
            label = "Сегодня" if last == today else f"Последняя тренировка — {last:%d.%m}"
            lines += [f"{label}: {day_head(by_ex)}.", *day_lines(by_ex)]
    if in_progress and which != "yesterday":
        lines.append(in_progress)
    return "\n".join(lines)


# ---- exercises: last time and records ----


@dataclass
class ExerciseHistory:
    name: str
    last_day: date = date.min
    last_sets: Sets = field(default_factory=list)
    best: tuple[float, Decimal, int, date] | None = None  # (e1RM, weight, reps, day)
    heaviest: Decimal | None = None


async def exercise_history(session: AsyncSession, user_id: int, upto: date) -> dict[str, ExerciseHistory]:
    """Every exercise with logged sets up to `upto`: its last day's sets and its best set (1RM by Epley,
    as in the advice summary), most recently done first."""
    rows = (
        await session.execute(
            select(Exercise.name, Workout.performed_on, WorkoutSet.weight_kg, WorkoutSet.reps)
            .select_from(Workout)
            .join(WorkoutSet, WorkoutSet.workout_id == Workout.id)
            .join(Exercise, Exercise.id == WorkoutSet.exercise_id)
            .where(Workout.user_id == user_id, Workout.performed_on <= upto)
            .order_by(Workout.performed_on, Workout.started_at, Workout.id, WorkoutSet.set_index)
        )
    ).all()
    out: dict[str, ExerciseHistory] = {}
    for name, day, weight, reps in rows:
        h = out.setdefault(name, ExerciseHistory(name))
        if day > h.last_day:
            h.last_day, h.last_sets = day, []
        h.last_sets.append((weight, reps))
        if weight is not None and reps:
            e1rm = epley(float(weight), reps)
            if h.best is None or e1rm > h.best[0]:
                h.best = (e1rm, weight, reps, day)
            h.heaviest = weight if h.heaviest is None else max(h.heaviest, weight)
    return dict(sorted(out.items(), key=lambda p: p[1].last_day, reverse=True))


def last_time(h: ExerciseHistory) -> str:
    sets = h.last_sets
    weighted = tonnage(sets)
    tail = f" ({kg(weighted)} кг)" if weighted else ""
    return f"последний раз {h.last_day:%d.%m}: {runs(sets)}{tail}"


def record(h: ExerciseHistory) -> str | None:
    if h.best is None:
        return None
    e1rm, w, r, day = h.best
    text = f"1ПМ по Эпли {kg(round(e1rm, 1))} кг ({kg(w)}×{r}, {day:%d.%m})"
    if h.heaviest is not None and h.heaviest != w:
        text += f", самый большой вес {kg(h.heaviest)} кг"
    return text


def exercise_line(h: ExerciseHistory, record_first: bool = False) -> str:
    rec = record(h)
    if record_first and rec:
        return f"{_cap(h.name)}: рекорд — {rec}; {last_time(h)}."
    return f"{_cap(h.name)}: {last_time(h)}." + (f" Рекорд: {rec}." if rec else "")


def records_block(history: dict[str, ExerciseHistory]) -> str:
    """For the model: per exercise (most recent first, capped) the last day's sets and the best 1RM."""
    if not history:
        return "Рекордов нет: в истории нет подходов."
    lines = ["Последний раз и рекорды по упражнениям, вся история:"]
    for h in list(history.values())[:RECORDS_IN_CONTEXT]:
        line = f"- {h.name}: {last_time(h)}"
        if rec := record(h):
            line += f"; рекорд {rec}"
        if len("\n".join([*lines, line])) > RECORDS_MAX:
            break
        lines.append(line)
    if len(history) > len(lines) - 1:
        lines.append(f"- и ещё упражнений: {len(history) - (len(lines) - 1)}")
    return "\n".join(lines)


async def known_exercises(session: AsyncSession) -> tuple[list[str], dict[str, list[str]]]:
    """All exercise names (the catalog) and their aliases."""
    rows = (await session.execute(select(Exercise.name, Exercise.aliases))).all()
    return [n for n, _ in rows], {n: list(a or []) for n, a in rows}


async def exercise_reply(
    session: AsyncSession, user_id: int, question: str, q: Question, today: date, in_progress: str = ""
) -> str | None:
    """The asked exercise's last time and record; None for an exercise it cannot tell (the model's turn)."""
    history = await exercise_history(session, user_id, today)
    catalog, aliases = await known_exercises(session)
    found = mentions(question, [*history, *catalog], aliases)
    if not found:
        said = [w for w in words(question) if w not in QUESTION_WORDS and len(w) >= 3]
        found = head_matches(question, history, skip=QUESTION_WORDS)
        named = {w for n in found for w in words(n)}
        if any(not any(same(w, x) for x in named) for w in said):
            return None  # names something it does not know as an exercise ("в жиме ногами")
    if not found:  # no exercise in the question
        if not q.record:
            return await workout_reply(session, user_id, q.day, today, in_progress)
        if not history:
            return "Рекордов нет: в истории нет подходов с весом."
        best = [h for h in history.values() if h.best is not None][:RECORDS_IN_REPLY]
        if not best:
            return "Рекордов нет: в истории нет подходов с весом."
        return "Рекорды из истории (1ПМ по Эпли):\n" + "\n".join(f"- {h.name}: {record(h)}" for h in best)
    lines = []
    for name in found[:MAX_EXERCISES_IN_REPLY]:
        h = history.get(name)
        lines.append(exercise_line(h, q.record) if h else f"{_cap(name)}: в истории подходов нет.")
    if len(found) > MAX_EXERCISES_IN_REPLY:
        lines.append(f"И ещё подходящих упражнений: {len(found) - MAX_EXERCISES_IN_REPLY}.")
    if not any(name in history for name in found):
        lines.append("Подходы можно записать, просто написав их в чат.")
    return "\n".join(lines)


# ---- food ----

_MACRO_NAMES = (("kcal", "ккал", ""), ("protein", "Б", " г"), ("fat", "Ж", " г"), ("carbs", "У", " г"))
_ASKED = (
    ("protein", ("бел",), "Белка"),
    ("fat", ("жир",), "Жиров"),
    ("carbs", ("углевод",), "Углеводов"),
    ("kcal", ("ккал", "калор"), "Калорий"),
)


def _macros(values: dict[str, float | None]) -> str:
    parts = []
    for key, label, unit in _MACRO_NAMES:
        v = values.get(key)
        if v is None:
            continue
        parts.append(f"{num(v, 0)} ккал" if key == "kcal" else f"{label} {num(v)}{unit}")
    return ", ".join(parts)


def _left(rem: nutrition.Remaining) -> str:
    """'1050 ккал, Б 60 г, перебор Ж 5 г, У 150 г' (only macros with a norm)."""
    parts = []
    for key, label, _unit in _MACRO_NAMES:
        v = getattr(rem, key)
        if v is None:
            continue
        amount = f"{num(abs(v), 0)} ккал" if key == "kcal" else f"{label} {num(abs(v))} г"
        parts.append(f"перебор {amount}" if round(v) < 0 else amount)
    return ", ".join(parts)


def _norm(t: nutrition.Targets) -> str:
    values = t.model_dump()
    if all(v is None for v in values.values()):
        return "норма КБЖУ не задана"
    return "норма " + _macros(values)


def food_block(s: nutrition.DaySummary) -> str:
    """For the model: the day's food totals, the norm and what is left, counted here."""
    norm = _norm(s.targets)
    if not s.entries:
        return f"Еды сегодня ({s.date:%d.%m}) в дневнике нет; {norm}."
    line = f"Еда сегодня ({s.date:%d.%m}), из дневника: {_macros(s.totals.model_dump())}; {norm}"
    left = _left(s.remaining)
    return line + (f"; до нормы осталось: {left}." if left else ".")


def _asked_macro(question: str) -> tuple[str, str] | None:
    ws = words(question)
    for key, stems, title in _ASKED:
        if any(w.startswith(st) for w in ws for st in stems):
            return key, title
    return None


def food_reply(s: nutrition.DaySummary, question: str, q: Question, today: date) -> str:
    label = "Сегодня" if s.date == today else f"Вчера ({s.date:%d.%m})"
    norm = _norm(s.targets)
    if not s.entries:
        return f"{label} еды в дневнике нет; {norm}."
    lines = []
    asked = _asked_macro(question)
    if asked is not None:
        key, title = asked
        eaten, target, rem = getattr(s.totals, key), getattr(s.targets, key), getattr(s.remaining, key)
        unit = " ккал" if key == "kcal" else " г"
        digits = 0 if key == "kcal" else 1
        if target is None or rem is None:
            lines.append(f"{title} съедено {num(eaten, digits)}{unit}, норма не задана.")
        elif round(rem) >= 0:
            lines.append(f"{title} осталось {num(rem, digits)}{unit}: съедено {num(eaten, digits)} из {target}{unit}.")
        else:
            lines.append(f"{title} перебор {num(-rem, digits)}{unit}: съедено {num(eaten, digits)} из {target}{unit}.")
    n = len(s.entries)
    lines.append(f"{label} съедено: {_macros(s.totals.model_dump())} ({n} {plural(n, 'запись', 'записи', 'записей')}).")
    lines.append(_cap(norm) + ".")
    if left := _left(s.remaining):
        lines.append(f"До нормы осталось: {left}.")
    if q.listing:
        for e in s.entries[:FOOD_LIST_MAX]:
            lines.append(f"- {e.time} {e.description}, {num(e.kcal, 0)} ккал")
        if n > FOOD_LIST_MAX:
            lines.append(f"- и ещё записей: {n - FOOD_LIST_MAX}")
    return "\n".join(lines)


# ---- entry point ----


async def reply(
    session: AsyncSession, user: User, question: str, q: Question, tz: ZoneInfo, now_utc: datetime
) -> str | None:
    """The answer to a classified question from the database, or None (the model answers)."""
    today = now_utc.astimezone(tz).date()
    if q.intent is Intent.FOOD:
        day = today - timedelta(days=1) if q.day == "yesterday" else today
        return food_reply(await nutrition.day_summary(session, user, day, tz), question, q, today)
    in_progress = await active_workout.context_for(session, user.id, now_utc, tz)
    if q.intent is Intent.WORKOUT:
        return await workout_reply(session, user.id, q.day, today, in_progress)
    return await exercise_reply(session, user.id, question, q, today, in_progress)
