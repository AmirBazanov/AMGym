"""Factual diary answers built in code from the database, without a model (layer 1 of the diary answer).

`reply` answers a question that gymbot.services.answer_intent.classify recognized: the workout of a day
(sets and tonnage per exercise), food of a day against the norm, an exercise's last time and record. It
returns None when the question cannot be answered for sure (an exercise it does not know): then the model
answers, checked by gymbot.services.answer_check. The same numbers go to the model's summary as blocks
(`food_block`, `records_block`, gymbot.services.answer.done_block), so both paths say the same.

Drop sets are folded into their main set ("80×8 → 60×6") and are not counted as sets; their weight×reps
still counts in the tonnage, as in the advice summary (gymbot.services.advice) and the MCP history.
"Last time" for an exercise is the last day BEFORE today when there is one (mid-workout today's sets are
not "last time"), like the day plan (gymbot.services.plan).

`plan_reply` answers answer_intent.plan_question: the program day (today with the day plan's corrections,
another day with a running deload) with a weight and its reason per exercise (gymbot.services.next_weights;
dumbbells per hand), and a recovery note only while the day's groups are still recovering at its start
(never "skip the day": a program day stays). `weights_block` is the same for the model's summary.

Only reads. Days are local dates in TIMEZONE (Workout.performed_on is one already), weights in kg.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from gymbot.config import Settings
from gymbot.db.models import Exercise, User, Workout, WorkoutSet
from gymbot.services import active_workout, nutrition
from gymbot.services import next_weights as nw
from gymbot.services.advice import epley, group_label, recovery_times
from gymbot.services.answer_intent import (
    PLAN_WORDS,
    QUESTION_WORDS,
    Intent,
    PlanQuestion,
    Question,
    head_matches,
    mentions,
    resolve_day,
    same,
    synonym_targets,
    variants,
    words,
)

MAX_EXERCISES_IN_REPLY = 4  # "в жиме" with several presses in the history
RECORDS_IN_REPLY = 8  # "какой мой рекорд?" without an exercise
RECORDS_IN_CONTEXT = 10
RECORDS_MAX = 1000  # characters of the records block in the model's summary
FOOD_LIST_MAX = 12


@dataclass(frozen=True)
class Set:
    """A main set with its drops (weight None = bodyweight)."""

    weight: Decimal | None
    reps: int
    drops: tuple[tuple[Decimal | None, int], ...] = ()


Sets = list[Set]


# ---- formatting ----


def _half_up(x: float | Decimal, digits: int = 0) -> Decimal:
    return Decimal(str(x)).quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP)


def kg(x: Decimal | float) -> str:
    """60 -> '60', 17.5 -> '17,5' (as done_block has always shown weights)."""
    v = float(x)
    return str(int(v)) if v == int(v) else f"{v:g}".replace(".", ",")


def num(x: float, digits: int = 1) -> str:
    """Food and 1RM numbers: whole from 10 up, else one decimal, decimal comma; halves round up."""
    if abs(x) >= 10 or digits == 0:
        return str(int(_half_up(x)))
    return kg(_half_up(x, digits))


def _one(weight: Decimal | None, reps: int) -> str:
    return f"{kg(weight)}×{reps}" if weight is not None else f"{reps} повт."


def set_text(s: Set) -> str:
    """'80×8', '80×8 → 60×6 → 6 повт.' (drops after arrows), '12 повт.' for bodyweight."""
    return " → ".join([_one(s.weight, s.reps), *(_one(w, r) for w, r in s.drops)])


def runs(sets: Sets) -> str:
    """'20×8 ×3, 17,5×8 ×2, 15×8': equal sets in a row merged."""
    merged: list[list] = []
    for s in sets:
        if merged and merged[-1][0] == s:
            merged[-1][1] += 1
        else:
            merged.append([s, 1])
    return ", ".join(set_text(s) + (f" ×{n}" if n > 1 else "") for s, n in merged)


def tonnage(sets: Sets) -> Decimal:
    """Weight × reps of every set and drop with a weight."""
    pairs = [(s.weight, s.reps) for s in sets] + [d for s in sets for d in s.drops]
    return sum((Decimal(w) * r for w, r in pairs if w is not None), Decimal(0))


def plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]


def _sentence(text: str) -> str:
    """`text` ending with one period ("… 12 повт." stays as is, no "повт..")."""
    return text if text.endswith((".", "…")) else text + "."


def _tail(sets: Sets) -> str:
    """' (880 кг)'; nothing for bodyweight-only sets ("(0 кг)" says nothing)."""
    t = tonnage(sets)
    return f" ({kg(t)} кг)" if t else ""


def _fold(rows) -> dict[str, Sets]:  # type: ignore[no-untyped-def]
    """(name, weight, reps, drop_index) rows in order -> sets by exercise, drops folded into their set."""
    by_ex: dict[str, Sets] = {}
    for name, weight, reps, drop_index in rows:
        sets = by_ex.setdefault(name, [])
        if drop_index and sets:
            last = sets[-1]
            sets[-1] = Set(last.weight, last.reps, (*last.drops, (weight, reps)))
        else:
            sets.append(Set(weight, reps))
    return by_ex


# ---- workouts ----


async def last_training_day(session: AsyncSession, user_id: int, upto: date, before: bool = False) -> date | None:
    """The last day with a workout up to `upto` (strictly before it if `before`)."""
    bound = Workout.performed_on < upto if before else Workout.performed_on <= upto
    return await session.scalar(select(func.max(Workout.performed_on)).where(Workout.user_id == user_id, bound))


async def day_sets(session: AsyncSession, user_id: int, day: date) -> dict[str, Sets]:
    """Sets of the local day `day` by exercise, in the order they were done (all workouts of the day)."""
    rows = (
        await session.execute(
            select(Exercise.name, WorkoutSet.weight_kg, WorkoutSet.reps, WorkoutSet.drop_index)
            .select_from(Workout)
            .join(WorkoutSet, WorkoutSet.workout_id == Workout.id)
            .join(Exercise, Exercise.id == WorkoutSet.exercise_id)
            .where(Workout.user_id == user_id, Workout.performed_on == day)
            .order_by(Workout.started_at, Workout.id, WorkoutSet.set_index, WorkoutSet.drop_index)
        )
    ).all()
    return _fold(rows)


def day_head(by_ex: dict[str, Sets]) -> str:
    n_ex, n_sets = len(by_ex), sum(len(v) for v in by_ex.values())
    total = sum((tonnage(v) for v in by_ex.values()), Decimal(0))
    return (
        f"{n_ex} {plural(n_ex, 'упражнение', 'упражнения', 'упражнений')}, "
        f"{n_sets} {plural(n_sets, 'подход', 'подхода', 'подходов')}, тоннаж {kg(total)} кг"
    )


def day_lines(by_ex: dict[str, Sets]) -> list[str]:
    return [f"- {name}: {runs(sets)}{_tail(sets)}" for name, sets in by_ex.items()]


async def workout_reply(
    session: AsyncSession, user_id: int, which: str, today: date, in_progress: str = "", previous: bool = False
) -> str:
    """The workout of today / yesterday / the last training day (the one before today if `previous`, "на
    прошлой тренировке"), counted here; `in_progress` is the Mini App line, added for today."""
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
        last = await last_training_day(session, user_id, today, before=previous)
        if last is None and previous:
            last = await last_training_day(session, user_id, today)
        if last is None:
            lines.append("Тренировок в истории пока нет.")
        else:
            by_ex = await day_sets(session, user_id, last)
            label = "Сегодня" if last == today else f"Последняя тренировка — {last:%d.%m}"
            if previous and last != today:
                label = f"Прошлая тренировка — {last:%d.%m}"
            lines += [f"{label}: {day_head(by_ex)}.", *day_lines(by_ex)]
    if in_progress and which != "yesterday" and not previous:
        lines.append(in_progress)
    return "\n".join(lines)


# ---- exercises: last time and records ----


@dataclass
class ExerciseHistory:
    name: str
    days: dict[date, Sets] = field(default_factory=dict)  # oldest first
    best: tuple[float, Decimal, int, date] | None = None  # (e1RM, weight, reps, day)
    heaviest: Decimal | None = None
    most_reps: tuple[int, date] | None = None  # bodyweight: the most reps in one set

    @property
    def last_day(self) -> date:
        return max(self.days) if self.days else date.min

    def session(self, today: date, which: str = "last") -> tuple[date, Sets] | None:
        """The sets of today / yesterday, or of "last time": the last day before today, else today."""
        if which in ("today", "yesterday"):
            day = today if which == "today" else today - timedelta(days=1)
            return (day, self.days[day]) if day in self.days else None
        before = [d for d in self.days if d < today]
        day = max(before) if before else (today if today in self.days else None)
        return (day, self.days[day]) if day is not None else None


async def exercise_history(session: AsyncSession, user_id: int, upto: date) -> dict[str, ExerciseHistory]:
    """Every exercise with logged sets up to `upto`: its sets by day and its best set (1RM by Epley, drops
    included as in the advice summary), most recently done first."""
    rows = (
        await session.execute(
            select(Exercise.name, Workout.performed_on, WorkoutSet.weight_kg, WorkoutSet.reps, WorkoutSet.drop_index)
            .select_from(Workout)
            .join(WorkoutSet, WorkoutSet.workout_id == Workout.id)
            .join(Exercise, Exercise.id == WorkoutSet.exercise_id)
            .where(Workout.user_id == user_id, Workout.performed_on <= upto)
            .order_by(Workout.performed_on, Workout.started_at, Workout.id, WorkoutSet.set_index,
                      WorkoutSet.drop_index)
        )
    ).all()
    out: dict[str, ExerciseHistory] = {}
    by_day: dict[tuple[str, date], list] = {}
    for name, day, weight, reps, drop_index in rows:
        h = out.setdefault(name, ExerciseHistory(name))
        by_day.setdefault((name, day), []).append((name, weight, reps, drop_index))
        if weight is not None and reps:
            e1rm = epley(float(weight), reps)
            if h.best is None or e1rm > h.best[0]:
                h.best = (e1rm, weight, reps, day)
            h.heaviest = weight if h.heaviest is None else max(h.heaviest, weight)
        elif weight is None and not drop_index and (h.most_reps is None or reps > h.most_reps[0]):
            h.most_reps = (reps, day)
    for (name, day), day_rows in by_day.items():
        out[name].days[day] = _fold(day_rows)[name]
    return dict(sorted(out.items(), key=lambda p: p[1].last_day, reverse=True))


def last_time(h: ExerciseHistory, today: date | None = None, which: str = "last") -> str:
    """'последний раз 07.10: 60×10 ×3 (1800 кг)' (the latest day if `today` is None)."""
    found = h.session(today, which) if today is not None else (h.last_day, h.days[h.last_day])
    if found is None:
        return "в этот день подходов нет"
    day, sets = found
    return f"последний раз {day:%d.%m}: {runs(sets)}{_tail(sets)}"


def record(h: ExerciseHistory) -> str | None:
    if h.best is None:
        if h.most_reps is None:
            return None
        reps, day = h.most_reps
        return f"больше всего {reps} {plural(reps, 'повтор', 'повтора', 'повторов')} в подходе ({day:%d.%m})"
    e1rm, w, r, day = h.best
    text = f"1ПМ по Эпли {kg(_half_up(e1rm, 1))} кг ({kg(w)}×{r}, {day:%d.%m})"
    if h.heaviest is not None and h.heaviest != w:
        text += f", самый большой вес {kg(h.heaviest)} кг"
    return text


def exercise_line(h: ExerciseHistory, record_first: bool = False, today: date | None = None,
                  which: str = "last") -> str:
    rec = record(h)
    if today is not None and which in ("today", "yesterday") and h.session(today, which) is None:
        label = "сегодня" if which == "today" else "вчера"
        last = f"{label} подходов нет; {last_time(h)}"
    else:
        last = last_time(h, today, which)
    if record_first and rec:
        return _sentence(f"{_cap(h.name)}: рекорд — {rec}; {last}")
    return _sentence(f"{_cap(h.name)}: {last}") + (f" Рекорд: {rec}." if rec else "")


def records_block(history: dict[str, ExerciseHistory], done_day: date | None = None) -> str:
    """For the model: per exercise (most recent first, capped) the last day's sets and the best 1RM.

    `done_day`: the day the "Сделано …" block lists (gymbot.services.answer.done_block); exercises last done
    that day point there instead of repeating their sets, so more exercises fit in RECORDS_MAX."""
    if not history:
        return "Рекордов нет: в истории нет подходов."
    lines = ["Последний раз и рекорды по упражнениям, вся история:"]
    for h in list(history.values())[:RECORDS_IN_CONTEXT]:
        if done_day is not None and h.last_day == done_day:
            line = f"- {h.name}: последний раз {done_day:%d.%m} (подходы в блоке «Сделано»)"
        else:
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


def resolve(
    question: str, history: list[str], catalog: list[str], aliases: dict[str, list[str]]
) -> tuple[list[str], list[str]] | None:
    """(history names asked about, asked names without sets), or None when the question names something
    it does not know. History names win over the catalog and the synonyms: "в румынке" with "румынская тяга
    с гантелями" in the history is that exercise, never "румынская тяга: подходов нет". ([], []) = no
    exercise in the question at all."""
    said = mentions(question, history, aliases, synonyms=False)
    if said:
        return said, []
    asked = [*mentions(question, [n for n in catalog if n not in history], aliases, synonyms=False),
             *synonym_targets(question)]
    asked = list(dict.fromkeys(asked))
    if asked:
        found = list(dict.fromkeys(v for name in asked for v in variants(name, history)))
        if found:
            return found, []
        return [], asked
    found = head_matches(question, history, skip=QUESTION_WORDS)
    said_words = [w for w in words(question) if w not in QUESTION_WORDS and len(w) >= 3]
    named = {w for n in found for w in words(n)}
    if any(not any(same(w, x) for x in named) for w in said_words):
        return None  # names something it does not know as an exercise ("в жиме ногами")
    return found, []


async def exercise_reply(
    session: AsyncSession, user_id: int, question: str, q: Question, today: date, in_progress: str = ""
) -> str | None:
    """The asked exercise's last time and record; None for an exercise it cannot tell (the model's turn)."""
    history = await exercise_history(session, user_id, today)
    catalog, aliases = await known_exercises(session)
    resolved = resolve(question, list(history), catalog, aliases)
    if resolved is None:
        return None
    found, missing = resolved
    if not found and not missing:  # no exercise in the question
        if not q.record:
            return await workout_reply(session, user_id, q.day, today, in_progress, previous=q.previous)
        best = [h for h in history.values() if record(h) is not None][:RECORDS_IN_REPLY]
        if not best:
            return "Рекордов нет: в истории нет подходов."
        return "Рекорды из истории (1ПМ по Эпли):\n" + "\n".join(f"- {h.name}: {record(h)}" for h in best)
    which = q.day if q.day in ("today", "yesterday") else "last"
    lines = [exercise_line(history[n], q.record, today, which) for n in found[:MAX_EXERCISES_IN_REPLY]]
    lines += [f"{_cap(name)}: в истории подходов нет." for name in missing[:MAX_EXERCISES_IN_REPLY]]
    if len(found) > MAX_EXERCISES_IN_REPLY:
        lines.append(f"И ещё подходящих упражнений: {len(found) - MAX_EXERCISES_IN_REPLY}.")
    if missing:
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
        return await workout_reply(session, user.id, q.day, today, in_progress, previous=q.previous)
    return await exercise_reply(session, user.id, question, q, today, in_progress)


# ---- the program day with weights (gymbot.services.next_weights) ----

WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
PLAN_EXERCISES_MAX = 4  # "с каким весом в сгибаниях" with several matching program exercises


def day_label(day: date, today: date) -> str:
    """'Сегодня, чт 08.10' / 'Завтра, пт 09.10' / 'Пт 09.10'."""
    short = f"{WEEKDAYS[day.weekday()]} {day:%d.%m}"
    if day == today:
        return f"Сегодня, {short}"
    if day == today + timedelta(days=1):
        return f"Завтра, {short}"
    return _cap(short)


def sets_text(r: nw.DayRow) -> str:
    """'3×8–12', '3× дропсет 12-6-6', '3 подх.' (as plan.plan_text)."""
    if r.drop_reps:
        return f"{r.sets}× дропсет {'-'.join(map(str, r.drop_reps))}"
    if r.reps_min is None:
        return f"{r.sets} подх."
    reps = f"{r.reps_min}–{r.reps_max}" if r.reps_max and r.reps_max != r.reps_min else str(r.reps_min)
    return f"{r.sets}×{reps}"


def weight_text(s: nw.Suggestion) -> str:
    """'27,5 кг (reason)', '13,5 кг на руку (reason)', or the reason alone when there is no number."""
    if s.weight is None:
        return s.reason
    reason = s.reason
    if s.factor != 1 and s.base_weight is not None:
        pct = round((1 - s.factor) * 100)
        reason += f"; по плану дня −{pct} % от {kg(s.base_weight)}"
    return f"{kg(s.weight)} кг{' на руку' if s.per_hand else ''} ({reason})"


def row_line(r: nw.DayRow) -> str:
    title = _cap(r.name) + (f" (вместо «{r.program_name}»)" if r.name != r.program_name else "")
    if r.suggestion is None:
        return f"• {title} — пропуск" + (f" ({r.note})" if r.note else "")
    return f"• {title} {sets_text(r)} — {weight_text(r.suggestion)}"


def recovery_note(dw: nw.DayWeights, recovering: dict[str, datetime], tz: ZoneInfo) -> str:
    """Groups of the day that are still recovering at the start of it: '… восстановятся к пт 09.10 17:00 (48 ч
    после тренировки) — тренировка после этого времени в самый раз.' A program day is never called off."""
    groups = list(dict.fromkeys(group_label(r.name) for r in dw.rows if r.suggestion is not None))
    start = datetime.combine(dw.day, datetime.min.time(), tz)
    until: dict[datetime, list[str]] = {}
    for g in groups:
        at = recovering.get(g)
        if at is not None and at > start:
            until.setdefault(at.astimezone(tz), []).append(g)
    lines = []
    for at, names in sorted(until.items()):
        who = _cap(", ".join(names))
        verb = "восстановится" if len(names) == 1 else "восстановятся"
        when = f"{WEEKDAYS[at.weekday()]} {at:%d.%m %H:%M}"
        if at.date() == dw.day:
            lines.append(f"{who} {verb} к {when} (48 ч после тренировки) — тренировка после {at:%H:%M} в самый раз.")
        else:
            lines.append(
                f"{who} {verb} только к {when} (48 ч после тренировки): тренировка по программе остаётся; "
                "если мышцы забиты, напиши — план дня станет легче."
            )
    return "\n".join(lines)


def plan_day_text(dw: nw.DayWeights, today: date, note: str = "", only: list[str] | None = None) -> str:
    """The direct answer: the day, every exercise with sets × reps, the weight and its reason."""
    rows = [r for r in dw.rows if only is None or r.program_name in only or r.name in only]
    head = f"{day_label(dw.day, today)} — тренировка по программе (неделя {dw.week}):"
    lines = [head]
    if dw.rest:
        lines.append("План дня советует отдохнуть" + (f": {dw.summary}" if dw.summary else ".") + " Если всё же идёшь:")
    elif dw.summary:
        lines.append(dw.summary)
    lines += [row_line(r) for r in rows]
    if any(r.suggestion is not None and r.suggestion.per_hand for r in rows):
        lines.append("Гантели — вес одной гантели (на руку).")
    if note:
        lines.append(note)
    return "\n".join(lines)


def weights_block(dw: nw.DayWeights, today: date) -> str:
    """For the model: «Веса на пт 09.10 (посчитано дневником): …», the only weights it may name."""
    label = day_label(dw.day, today).lower()
    lines = [f"Веса на {label} (посчитано дневником, гантели — на руку):"]
    if dw.summary:
        lines.append(dw.summary)
    lines += [row_line(r) for r in dw.rows]
    return "\n".join(lines)


async def _plan_day(
    session: AsyncSession, user: User, settings: Settings, tz: ZoneInfo, now_utc: datetime,
    ref: nw.ProgramRef, day: date,
) -> tuple[date, nw.DayWeights] | None:
    """The day's weights; on a rest day the next training day's (within NEXT_DAY_SEARCH)."""
    dw = await nw.day_weights(session, user, settings, tz, now_utc, day, ref)
    if dw is not None:
        return day, dw
    nxt = nw.training_day_from(ref, day + timedelta(days=1))
    if nxt is None:
        return None
    dw = await nw.day_weights(session, user, settings, tz, now_utc, nxt, ref)
    return (nxt, dw) if dw is not None else None


async def next_training_day(session: AsyncSession, user_id: int, ref: nw.ProgramRef, today: date) -> date | None:
    """"Следующая тренировка": today while it is a program day without a workout yet, else the next one."""
    trained = await last_training_day(session, user_id, today) == today
    return nw.training_day_from(ref, today + timedelta(days=1) if trained else today)


async def target_day(
    session: AsyncSession, user: User, settings: Settings, tz: ZoneInfo, now_utc: datetime, pq: PlanQuestion | None
) -> tuple[date | None, nw.DayWeights] | None:
    """(the asked day or None, the training day answered) for the model's weights block: the asked day, else
    today when it is a training day without a workout yet, else the next training day."""
    ref = await nw.program_ref(session, user, settings)
    if ref is None:
        return None
    today = now_utc.astimezone(tz).date()
    asked: date | None = None
    if pq is not None and pq.when is not None:
        asked = (await next_training_day(session, user.id, ref, today) if pq.when[0] == "next"
                 else resolve_day(pq.when, today))
    if asked is None:
        trained = await last_training_day(session, user.id, today) == today
        asked_default = today + timedelta(days=1) if trained else today
        found = await _plan_day(session, user, settings, tz, now_utc, ref, asked_default)
        return (None, found[1]) if found else None
    found = await _plan_day(session, user, settings, tz, now_utc, ref, asked)
    return (asked, found[1]) if found else None


async def plan_reply(
    session: AsyncSession, user: User, settings: Settings, question: str, pq: PlanQuestion, tz: ZoneInfo,
    now_utc: datetime,
) -> str | None:
    """The program day with weights for a PlanQuestion, or None (the model answers: no program, a past date,
    an exercise that is not in the program)."""
    ref = await nw.program_ref(session, user, settings)
    if ref is None:
        return None
    today = now_utc.astimezone(tz).date()
    recovering = await recovery_times(session, user.id, today, now_utc)
    if pq.when is None:  # "с каким весом в <упражнение>": its next program day
        names = list(dict.fromkeys(
            it.name for k in range(nw.NEXT_DAY_SEARCH + 1)
            if (d := nw.program_day(ref, today + timedelta(days=k))) for it in d[2]
        ))
        found = mentions(question, names)
        if not found:
            found = head_matches(question, names, skip=PLAN_WORDS)
            said = [w for w in words(question) if w not in PLAN_WORDS and len(w) >= 3]
            named = {w for n in found for w in words(n)}
            if not found or any(not any(same(w, x) for x in named) for w in said):
                return None
        found = found[:PLAN_EXERCISES_MAX]
        for k in range(nw.NEXT_DAY_SEARCH + 1):
            d = today + timedelta(days=k)
            day = nw.program_day(ref, d)
            if day and any(it.name in found for it in day[2]):
                dw = await nw.day_weights(session, user, settings, tz, now_utc, d, ref)
                if dw is not None:
                    return plan_day_text(dw, today, recovery_note(dw, recovering, tz), only=found)
        return None
    if pq.when[0] == "next":
        asked = await next_training_day(session, user.id, ref, today)
        if asked is None:
            return None
    else:
        asked = resolve_day(pq.when, today)
        if asked is None:
            return None
    found_day = await _plan_day(session, user, settings, tz, now_utc, ref, asked)
    if found_day is None:
        return f"{day_label(asked, today)}: по программе тренировки нет, и в ближайшие дни тоже."
    day, dw = found_day
    text = plan_day_text(dw, today, recovery_note(dw, recovering, tz))
    if day != asked:
        text = f"{day_label(asked, today)} по программе отдых. Ближайшая тренировка:\n{text}"
    return text
