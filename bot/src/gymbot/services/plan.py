"""Adaptive day plan: today's program day adjusted to wellbeing, food, recovery and user facts.

Flow: collect_inputs (program day, wellbeing today and yesterday, yesterday's food against the norm,
workouts of 7 days, active facts) -> rule_draft (deterministic rules, no model) -> refine_with_llm (only
when the rules made the day light or rest, a pain touches today's exercises, or there are training or
health facts) -> stored in day_plans per user and local day.

The stored plan is reused while `inputs_hash` matches: the hash covers the inputs, the rule draft (it
depends on the time: a session older than 48 h stops counting) and PLAN_VERSION. A fallback to the
draft (model down, invalid answer) is cached too; "regenerate" (force) is the explicit retry.

Names in the plan are exactly the names of the program JSON (data/programs/<slug>.json), which the Mini
App matches with ===, never the model's spelling. `summary` is only the reason: the Mini App and /plan
put "Сегодня лучше отдохнуть: " / "План скорректирован: " in front themselves.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import re
from collections import defaultdict
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from gymbot.config import Settings
from gymbot.db.models import DayPlan, Exercise, User, UserProgram, Workout, WorkoutSet
from gymbot.llm.openrouter import LLMError, OpenRouterClient
from gymbot.llm.prompts import build_plan_messages, format_facts
from gymbot.services import live
from gymbot.services.facts import active_facts
from gymbot.services.nutrition import _aware, day_summary
from gymbot.services.programs import find_day, load_program, normalize, program_position
from gymbot.services.wellbeing import parse_pains, recent_entries

log = logging.getLogger(__name__)

PLAN_VERSION = 2  # bump when the rules change: stored plans are rebuilt (2: the model can only lighten)
SHORT_SLEEP_REST, SHORT_SLEEP_LIGHT = 4, 6  # hours
LOW_KCAL = 0.7  # yesterday's kcal below this share of the norm: light day
LIGHT_FACTOR, PAIN_FACTOR, MILD_PAIN_FACTOR, SORE_FACTOR = 0.9, 0.7, 0.85, 0.85
SORE_HOURS, HEAVY_SETS = 48, 6  # a group trained this recently with this many sets may still be sore
WORKOUT_DAYS = 7
MODEL_FACT_CATEGORIES = ("training", "health")

Readiness = Literal["normal", "light", "rest"]


def utcnow() -> datetime:
    """Current time; the API and /plan read it here, so tests can pin it."""
    return datetime.now(UTC)


# ---- wire format (mirrors miniapp/src/api.ts DayPlan) ----


class PlanExercise(BaseModel):
    name: str
    sets: int
    repsMin: int | None
    repsMax: int | None
    weightFactor: float
    skip: bool
    replaceWith: str | None
    reason: str | None


class DayPlanOut(BaseModel):
    date: date
    week: int  # program week and weekday (1=Mon..7=Sun) the plan was built for
    weekday: int
    adjusted: bool
    readiness: Readiness
    summary: str | None
    exercises: list[PlanExercise]


# ---- inputs ----


@dataclass
class DayItem:
    order: int
    name: str  # as in the program JSON
    sets: int
    reps_min: int | None
    reps_max: int | None
    drop_reps: list[int] | None = None

    @property
    def dropset(self) -> bool:
        return bool(self.drop_reps)


@dataclass
class Pain:
    place: str
    severity: int | None


@dataclass
class RecentSets:
    at: datetime  # when the workout started (UTC)
    name: str
    sets: int  # main sets, drops not counted
    day: date | None = None  # Workout.performed_on, the local day in TIMEZONE


@dataclass
class PlanInputs:
    today: date
    items: list[DayItem]
    week: int = 1  # program week and weekday (1=Mon) of `items`
    weekday: int = 1
    sleep_hours: float | None = None  # newest value of today's and yesterday's wellbeing
    energy: int | None = None
    pains: list[Pain] = field(default_factory=list)
    texts: list[str] = field(default_factory=list)  # wellbeing notes and messages, for "забиты" etc.
    kcal_yesterday: float | None = None  # None: nothing logged yesterday
    kcal_target: int | None = None
    recent: list[RecentSets] = field(default_factory=list)  # earlier days only: today's sets must not move the plan
    facts: list[tuple[str, str]] = field(default_factory=list)  # (text, category), newest first


def day_names(programs_dir: Path, slug: str, week: int, weekday: int) -> dict[int, str]:
    """order -> exercise name as written in data/programs/<slug>.json (the Mini App's source)."""
    path = programs_dir / f"{slug}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    for w in data.get("weeks", []):
        if w.get("number") == week:
            for d in w.get("days", []):
                if d.get("weekday") == weekday:
                    return {e["order"]: e["name"] for e in d.get("exercises", [])}
    return {}


async def _program_day(
    session: AsyncSession, user: User, settings: Settings, today: date
) -> tuple[list[DayItem], int, int] | None:
    """(items, week, weekday) of today's program day, None on a rest day or outside the program."""
    # Read-only like advice._program_line: the API and /plan start a program first (users.active_program).
    up = await session.scalar(
        select(UserProgram).where(UserProgram.user_id == user.id).order_by(UserProgram.id.desc()).limit(1)
    )
    if up is None:
        return None
    program = await load_program(session, up.program_id)
    pos = program_position(up.started_on, len(program.weeks), today)
    if pos.not_started or pos.finished:
        return None
    day = find_day(program, pos.week, pos.weekday)
    if day is None:
        return None
    names = day_names(settings.programs_dir, program.slug, pos.week, pos.weekday)
    items = [
        DayItem(i.order, names.get(i.order, i.exercise.name), i.sets, i.reps_min, i.reps_max,
                list(i.drop_reps) if i.drop_reps else None)
        for i in sorted(day.items, key=lambda i: i.order)
    ]
    return items, pos.week, pos.weekday


async def collect_inputs(
    session: AsyncSession, user: User, settings: Settings, tz: ZoneInfo, now_utc: datetime
) -> PlanInputs | None:
    """Everything the plan depends on; None when today is not a training day of the user's program."""
    today = now_utc.astimezone(tz).date()
    found = await _program_day(session, user, settings, today)
    if found is None:
        return None
    items, week, weekday = found
    entries = await recent_entries(session, user, today, 2, tz)  # today and yesterday, newest first
    pains: dict[str, Pain] = {}
    for e in entries:  # the newest mention of a place wins
        for p in parse_pains(e.pains):
            pains.setdefault(normalize(p.place), Pain(p.place, p.severity))
    yesterday = await day_summary(session, user, today - timedelta(days=1), tz)
    rows = (
        await session.execute(
            select(Workout.started_at, Workout.performed_on, Exercise.name, func.count(WorkoutSet.id))
            .join(WorkoutSet, WorkoutSet.workout_id == Workout.id)
            .join(Exercise, Exercise.id == WorkoutSet.exercise_id)
            .where(Workout.user_id == user.id, Workout.performed_on >= today - timedelta(days=WORKOUT_DAYS - 1),
                   Workout.performed_on < today, WorkoutSet.drop_index == 0)
            .group_by(Workout.id, Workout.started_at, Workout.performed_on, Exercise.name)
            .order_by(Workout.started_at, Exercise.name)
        )
    ).all()
    return PlanInputs(
        today=today,
        items=items,
        week=week,
        weekday=weekday,
        sleep_hours=next((float(e.sleep_hours) for e in entries if e.sleep_hours is not None), None),
        energy=next((e.energy for e in entries if e.energy is not None), None),
        pains=list(pains.values()),
        texts=[t for e in entries for t in (e.note, e.raw_text) if t],
        kcal_yesterday=yesterday.totals.kcal if yesterday.entries else None,
        kcal_target=user.kcal_target,
        recent=[RecentSets(_aware(at), name, n, day) for at, day, name, n in rows],
        facts=[(f.text, f.category) for f in await active_facts(session, user.id)],
    )


# ---- keyword maps: pain places, muscle groups, equipment ----


def _key(text: str) -> str:
    return " ".join(text.casefold().replace("ё", "е").split())


# Pain place -> region; checked in order: "предплечье" contains "плеч".
_REGIONS = [
    ("elbow", re.compile(r"локт|локот|запяст|кист|предплеч")),
    ("shoulder", re.compile(r"плеч|дельт")),
    ("knee", re.compile(r"колен|ног|бедр|голен|стоп")),
    ("lower_back", re.compile(r"поясниц|спин|крестц")),
]
# Region -> exercises that load it.
_LOADS = {
    "elbow": re.compile(r"французск|сгибани|бицепс|трицепс|разгибани|жим узким"),
    "shoulder": re.compile(r"(?<!французский )жим(?! ног)|отведени|разведени|тяга к подбородку|армейск|махи"),
    "knee": re.compile(r"присед|выпад|жим ног|румынск|гак|разгибани[а-я]* ног|сгибани[а-я]* ног"),
    "lower_back": re.compile(r"станов|румынск|наклон|присед|гиперэкстенз"),
}
_LEG_CURL = re.compile(r"(сгибани|разгибани)[а-я]* ног")


def pain_region(place: str) -> str | None:
    key = _key(place)
    return next((region for region, pattern in _REGIONS if pattern.search(key)), None)


def loads(place: str, exercise: str) -> bool:
    """Whether `exercise` loads the painful `place` ("левое плечо" -> presses, raises)."""
    region = pain_region(place)
    if region is None:
        return False
    name = _key(exercise)
    if region == "elbow" and _LEG_CURL.search(name):
        return False
    return bool(_LOADS[region].search(name))


# Muscle groups for soreness and for checking replacements; legs first ("сгибания ног" is not biceps).
_GROUPS = [
    ("legs", re.compile(r"ног|квадр|бедр|ягодиц|присед|выпад|румынск|гак|икр")),
    ("triceps", re.compile(r"трицепс|французск|разгибани|жим узким")),
    ("biceps", re.compile(r"бицепс|сгибани|молотк|предплеч")),
    ("shoulders", re.compile(r"дельт|плеч|жим[а-я ]* сидя|жим стоя|армейск|отведени|тяга к подбородку|махи")),
    ("chest", re.compile(r"груд|жим[а-я ]* лежа|разведени|отжимани|кроссовер|бабочк")),
    ("back", re.compile(r"спин|широч|тяга|подтягивани|пуловер")),
]
_EQUIPMENT = [
    ("cable", re.compile(r"блок|кроссовер")),
    ("machine", re.compile(r"смит|тренаж|пек дек|гак")),
    ("barbell", re.compile(r"штанг|гриф|ez")),
    ("dumbbell", re.compile(r"гантел")),
]
_SORE = re.compile(r"забит|крепатур|болят|ноют|гудят|ломит")


def muscle_group(name: str) -> str | None:
    key = _key(name)
    return next((g for g, pattern in _GROUPS if pattern.search(key)), None)


def _equipment(name: str) -> str | None:
    key = _key(name)
    return next((e for e, pattern in _EQUIPMENT if pattern.search(key)), None)


def valid_replacement(original: str, replacement: str) -> bool:
    """Same muscle group (known), and the same equipment when both name one."""
    group = muscle_group(replacement)
    if group is None or group != muscle_group(original) or normalize(replacement) == normalize(original):
        return False
    eq_o, eq_r = _equipment(original), _equipment(replacement)
    return not (eq_o and eq_r and eq_o != eq_r)


def _sore_groups(inputs: PlanInputs, now_utc: datetime) -> dict[str, date]:
    """Groups reported sore ("бицепс забит") that were trained hard in the last SORE_HOURS -> last session date."""
    reported: set[str] = set()
    for text in inputs.texts:
        key = _key(text)
        if _SORE.search(key):
            reported |= {g for g, pattern in _GROUPS if pattern.search(key)}
    for p in inputs.pains:  # "бицепс" as a pain place is a sore muscle, not a joint
        if pain_region(p.place) is None and (g := muscle_group(p.place)):
            reported.add(g)
    out: dict[str, date] = {}
    for g in reported:
        recent = [r for r in inputs.recent if now_utc - r.at < timedelta(hours=SORE_HOURS) and muscle_group(r.name) == g]
        if sum(r.sets for r in recent) >= HEAVY_SETS:
            last = max(recent, key=lambda r: r.at)
            out[g] = last.day or last.at.date()
    return out


# ---- rules ----


def _n(x: float) -> str:
    return str(round(x)) if x == round(x) else f"{x:.1f}"


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]


def _pain_label(p: Pain) -> str:
    return f"болит {p.place}" + (f" ({p.severity}/5)" if p.severity else "")


def rule_readiness(inputs: PlanInputs) -> tuple[Readiness, list[str]]:
    """normal / light / rest by fixed rules, with the reasons ("спал 5 ч")."""
    rest: list[str] = []
    light: list[str] = []
    if inputs.sleep_hours is not None:
        if inputs.sleep_hours < SHORT_SLEEP_REST:
            rest.append(f"спал {_n(inputs.sleep_hours)} ч")
        elif inputs.sleep_hours < SHORT_SLEEP_LIGHT:
            light.append(f"спал {_n(inputs.sleep_hours)} ч")
    if inputs.energy == 1:
        rest.append("энергия 1/5")
    elif inputs.energy == 2:
        light.append("энергия 2/5")
    if inputs.kcal_yesterday is not None and inputs.kcal_target and inputs.kcal_yesterday < LOW_KCAL * inputs.kcal_target:
        light.append(f"вчера {round(inputs.kcal_yesterday)} из {inputs.kcal_target} ккал")
    for p in inputs.pains:
        if not any(loads(p.place, it.name) for it in inputs.items):
            continue  # a knee on an arms day changes nothing
        if p.severity is not None and p.severity >= 4:
            rest.append(_pain_label(p))
        elif p.severity is None or p.severity >= 2:  # the parser rarely sets severity: unknown = moderate
            light.append(_pain_label(p))
    if rest:
        return "rest", rest + light
    if light:
        return "light", light
    return "normal", []


@dataclass
class Draft:
    readiness: Readiness
    summary: str | None
    exercises: list[PlanExercise]
    protected: frozenset[int] = frozenset()  # positions lightened for pain: the model may not make them heavier
    adjusted: bool = False


def _changed(e: PlanExercise, it: DayItem) -> bool:
    reps_changed = not it.dropset and (e.repsMin, e.repsMax) != (it.reps_min, it.reps_max)
    return e.skip or e.replaceWith is not None or e.weightFactor != 1 or e.sets != it.sets or reps_changed


def _is_adjusted(readiness: str, exercises: list[PlanExercise], items: list[DayItem]) -> bool:
    return readiness != "normal" or any(_changed(e, it) for e, it in zip(exercises, items, strict=True))


def rule_draft(inputs: PlanInputs, now_utc: datetime) -> Draft:
    readiness, reasons = rule_readiness(inputs)
    sore = _sore_groups(inputs, now_utc)
    exercises: list[PlanExercise] = []
    protected: set[int] = set()
    for idx, it in enumerate(inputs.items):
        sets, factor, skip, reason = it.sets, 1.0, readiness == "rest", None
        if readiness == "light":
            sets, factor = max(min(it.sets, 2), it.sets - 1), LIGHT_FACTOR
        for p in inputs.pains:
            if loads(p.place, it.name):
                if p.severity is not None and p.severity >= 4:
                    skip = True
                else:
                    factor = min(factor, MILD_PAIN_FACTOR if p.severity == 1 else PAIN_FACTOR)
                reason = reason or _pain_label(p)
                protected.add(idx)
        group = muscle_group(it.name)
        if group in sore:
            factor = min(factor, SORE_FACTOR)
            reason = reason or f"мышцы не восстановились после {sore[group]:%d.%m}"
        exercises.append(
            PlanExercise(
                name=it.name, sets=sets,
                repsMin=None if it.dropset else it.reps_min, repsMax=None if it.dropset else it.reps_max,
                weightFactor=round(factor, 2), skip=skip, replaceWith=None, reason=reason,
            )
        )
    adjusted = _is_adjusted(readiness, exercises, inputs.items)
    summary = None
    if readiness == "rest":
        summary = _cap(", ".join(reasons)) + ". Сон и восстановление сегодня важнее зала."
    elif readiness == "light":
        summary = _cap(", ".join(reasons)) + f". Вес −{round((1 - LIGHT_FACTOR) * 100)} %, на подход меньше."
    elif adjusted:
        notes = list(dict.fromkeys(e.reason for e in exercises if e.reason))
        summary = _cap("; ".join(notes)) + " — эти упражнения легче."
    return Draft(readiness, summary, exercises, frozenset(protected), adjusted)


def needs_model(inputs: PlanInputs, draft: Draft) -> bool:
    return (
        draft.readiness != "normal"
        or bool(draft.protected)
        or any(category in MODEL_FACT_CATEGORIES for _, category in inputs.facts)
    )


# ---- the model ----

_PREFIX = re.compile(r"^\s*(сегодня лучше отдохнуть|план скорректирован|сегодня)\s*[:—–-]\s*", re.IGNORECASE)


def _text(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    return text[:limit] if text else None


def _int(value: Any, lo: int, hi: int, default: int | None) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    n = round(value)
    return default if n < lo else min(n, hi)


def _factor(value: Any, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return default
    return round(min(max(float(value), 0.3), 1.5), 2)


def _not_above(value: int | None, limit: int | None) -> int | None:
    return limit if value is None or (limit is not None and value > limit) else value


def apply_refinement(draft: Draft, data: Any, inputs: PlanInputs) -> Draft:
    """The model's answer checked against the draft; any structural mismatch returns the draft itself.

    Names always stay the program's; readiness is the rules' one; a rest day stays all skipped. The
    model can only make the day lighter, never heavier (enforced here, not in the prompt):
    - sets <= the draft's;
    - weightFactor <= the draft's on a light day and on exercises lightened for pain, else <= 1.0
      (never above the program);
    - on exercises lightened for pain reps do not go above the draft's, skipped ones stay skipped;
    - a replacement must be in the same muscle group and equipment and must not load a painful place.
    """
    if not isinstance(data, dict):
        return draft
    items = data.get("exercises")
    if not isinstance(items, list) or len(items) != len(draft.exercises):
        return draft
    for d, item in zip(draft.exercises, items, strict=True):
        if not isinstance(item, dict) or normalize(str(item.get("name") or "")) != normalize(d.name):
            return draft
    summary = draft.summary
    if (text := _text(data.get("summary"), 300)) and (text := _PREFIX.sub("", text).strip()):
        summary = _cap(text)
    if draft.readiness == "rest":
        return replace(draft, summary=summary)
    out: list[PlanExercise] = []
    for idx, (d, item, it) in enumerate(zip(draft.exercises, items, inputs.items, strict=True)):
        if d.skip:
            out.append(d)
            continue
        protected = idx in draft.protected
        reps_min = reps_max = None
        if not it.dropset:
            reps_min = _int(item.get("repsMin"), 1, 50, d.repsMin)
            reps_max = _int(item.get("repsMax"), 1, 50, d.repsMax)
            if protected:
                reps_min = _not_above(reps_min, d.repsMin)
                reps_max = _not_above(reps_max, d.repsMax)
            if reps_min is not None and reps_max is not None and reps_max < reps_min:
                reps_max = reps_min
        cap = d.weightFactor if protected or draft.readiness != "normal" else 1.0
        factor = min(_factor(item.get("weightFactor"), d.weightFactor), cap)
        replacement = _text(item.get("replaceWith"), 80)
        if replacement and (
            not valid_replacement(d.name, replacement) or any(loads(p.place, replacement) for p in inputs.pains)
        ):
            log.info("plan: replacement %r for %r dropped", replacement, d.name)
            replacement = None
        out.append(
            PlanExercise(
                name=d.name,
                sets=min(_int(item.get("sets"), 1, 10, d.sets) or d.sets, d.sets),
                repsMin=reps_min,
                repsMax=reps_max,
                weightFactor=factor,
                skip=item.get("skip") is True,
                replaceWith=replacement,
                reason=_text(item.get("reason"), 120) or d.reason,
            )
        )
    return Draft(draft.readiness, summary, out, draft.protected, _is_adjusted(draft.readiness, out, inputs.items))


def _fmt_reps(it: DayItem) -> str:
    if it.dropset:
        return f"{it.sets} × дропсет {'-'.join(map(str, it.drop_reps or []))}"
    if it.reps_min is None:
        return f"{it.sets} подх."
    reps = f"{it.reps_min}–{it.reps_max}" if it.reps_max and it.reps_max != it.reps_min else str(it.reps_min)
    return f"{it.sets} × {reps}"


def model_context(inputs: PlanInputs, draft: Draft) -> str:
    """Compact Russian summary for the model: no names or ids, only the training-relevant numbers."""
    lines = [f"Сегодня {inputs.today:%d.%m}, день программы:"]
    lines += [f"{i}. {it.name}: {_fmt_reps(it)}" for i, it in enumerate(inputs.items, 1)]
    well = []
    if inputs.sleep_hours is not None:
        well.append(f"сон {_n(inputs.sleep_hours)} ч")
    if inputs.energy is not None:
        well.append(f"энергия {inputs.energy}/5")
    if inputs.pains:
        well.append("боли: " + ", ".join(p.place + (f" ({p.severity}/5)" if p.severity else "") for p in inputs.pains))
    lines.append("Самочувствие (сегодня и вчера): " + ("; ".join(well) if well else "записей нет") + ".")
    if inputs.kcal_yesterday is not None:
        target = f" из {inputs.kcal_target}" if inputs.kcal_target else ""
        lines.append(f"Питание вчера: {round(inputs.kcal_yesterday)}{target} ккал.")
    if inputs.recent:
        recent = [f"{r.at:%d.%m} {r.name} {r.sets} подх." for r in inputs.recent[-10:]]
        lines.append(f"Тренировки за {WORKOUT_DAYS} дней: " + "; ".join(recent) + ".")
    if facts := format_facts([t for t, _ in inputs.facts], 400):
        lines.append(facts + ".")
    labels = {"normal": "обычный день", "light": "лёгкий день", "rest": "отдых"}
    lines.append(f"Правила: {labels[draft.readiness]}" + (f" ({draft.summary})" if draft.summary else "") + ".")
    return "\n".join(lines)


async def refine_with_llm(llm: OpenRouterClient, inputs: PlanInputs, draft: Draft) -> Draft:
    """The draft refined by the model, or the draft itself when the model fails or answers nonsense."""
    template = json.dumps(
        {"summary": draft.summary, "exercises": [e.model_dump() for e in draft.exercises]}, ensure_ascii=False
    )
    try:
        data = await llm.complete_json(build_plan_messages(model_context(inputs, draft), template))
    except LLMError as e:
        log.warning("plan: model failed, using the rule draft: %s", e)
        return draft
    return apply_refinement(draft, data, inputs)


# ---- storage ----


def inputs_hash(inputs: PlanInputs, draft: Draft) -> str:
    payload = {
        "v": PLAN_VERSION,
        "inputs": asdict(inputs),
        "draft": [draft.readiness, draft.summary, [e.model_dump() for e in draft.exercises]],
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def plan_out(row: DayPlan, inputs: PlanInputs) -> DayPlanOut:
    """`inputs` are the ones the row was built from (same hash): they give the program week and weekday."""
    exercises = [PlanExercise.model_validate(e) for e in json.loads(row.exercises_json or "[]")]
    return DayPlanOut(
        date=row.plan_date,
        week=inputs.week,
        weekday=inputs.weekday,
        adjusted=bool(exercises),
        readiness=row.readiness,  # type: ignore[arg-type]
        summary=row.summary if exercises else None,
        exercises=exercises,
    )


@dataclass
class Built:
    out: DayPlanOut
    items: list[DayItem]  # the program day, for texts that show unadjusted exercises


# One build per user at a time (one process): a second request waits and then finds the stored plan.
_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)


async def _stored(session: AsyncSession, user_id: int, day: date) -> DayPlan | None:
    return await session.scalar(select(DayPlan).where(DayPlan.user_id == user_id, DayPlan.plan_date == day))


async def get_or_build(
    session: AsyncSession,
    user: User,
    settings: Settings,
    llm: OpenRouterClient | None,
    tz: ZoneInfo,
    now_utc: datetime,
    force: bool = False,
) -> Built | None:
    """Today's plan: the stored one if built from the same inputs (unless `force`), else rebuilt and saved.

    None when today is not a training day. Commits: before the model call (no transaction is held while
    the model thinks) and after saving the plan. Without `llm` only the rules are used.
    """
    user_id = user.id  # `user` expires on the rollback below; never touch it after that
    inputs = await collect_inputs(session, user, settings, tz, now_utc)
    if inputs is None:
        return None
    draft = rule_draft(inputs, now_utc)
    key = inputs_hash(inputs, draft)
    async with _locks[user_id]:
        row = await _stored(session, user_id, inputs.today)
        if row is not None and row.inputs_hash == key and not force:
            return Built(plan_out(row, inputs), inputs.items)
        await session.commit()
        final = draft
        if llm is not None and needs_model(inputs, draft):
            final = await refine_with_llm(llm, inputs, draft)
        row = await _stored(session, user_id, inputs.today)
        if row is None:
            row = DayPlan(user_id=user_id, plan_date=inputs.today)
            session.add(row)
        row.readiness = final.readiness
        row.summary = final.summary if final.adjusted else None
        row.exercises_json = json.dumps(
            [e.model_dump() for e in final.exercises] if final.adjusted else [], ensure_ascii=False
        )
        row.inputs_hash = key
        row.created_at = now_utc
        try:
            await session.commit()
            live.publish(user_id, "plan")
        except IntegrityError:  # another process saved today's plan first: use theirs
            await session.rollback()
            row = await _stored(session, user_id, inputs.today)
            assert row is not None
        return Built(plan_out(row, inputs), inputs.items)


# ---- chat text ----


def _sets_text(sets: int, reps_min: int | None, reps_max: int | None, it: DayItem) -> str:
    if it.dropset:
        return f"{sets}× дропсет {'-'.join(map(str, it.drop_reps or []))}"
    if reps_min is None:
        return f"{sets} подх."
    reps = f"{reps_min}–{reps_max}" if reps_max and reps_max != reps_min else str(reps_min)
    return f"{sets}×{reps}"


def plan_text(built: Built) -> str:
    """The plan for the chat: «Сегодня: лёгкая версия. … • Жим лёжа 3×8–12, вес −10 %»."""
    out, items = built.out, built.items
    if not out.adjusted:
        lines = ["Сегодня по программе, без поправок:"]
        lines += [f"• {_cap(it.name)} {_sets_text(it.sets, it.reps_min, it.reps_max, it)}" for it in items]
        return "\n".join(lines)
    if out.readiness == "rest":
        return f"Сегодня лучше отдохнуть: {out.summary}" if out.summary else "Сегодня лучше отдохнуть."
    head = "Сегодня: лёгкая версия." if out.readiness == "light" else "Сегодня: план с поправками."
    lines = [f"{head} {out.summary}" if out.summary else head]
    for e, it in zip(out.exercises, items, strict=False):
        if e.skip:
            lines.append(f"• {_cap(e.name)} — пропуск" + (f" ({e.reason})" if e.reason else ""))
            continue
        line = f"• {_cap(e.replaceWith or e.name)} {_sets_text(e.sets, e.repsMin, e.repsMax, it)}"
        extras = []
        if e.weightFactor != 1:
            pct = round((e.weightFactor - 1) * 100)
            extras.append(f"вес {'−' if pct < 0 else '+'}{abs(pct)} %")
        if e.replaceWith:
            extras.append(f"вместо «{e.name}»")
        if extras:
            line += ", " + ", ".join(extras)
        if e.reason:
            line += f" — {e.reason}"
        lines.append(line)
    return "\n".join(lines)
