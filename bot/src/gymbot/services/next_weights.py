"""Deterministic weight suggestions for a program day: the same number every time for the same data.

The pure part is a Python port of miniapp/src/progression.ts `suggestWeight` and miniapp/src/plan.ts
`scaleWeight` (same operation order and JS rounding: `Math.round` rounds a half up, Python's `round` does
not), so the chat and the Mini App show one number. data/progression_cases.json holds the shared test
vectors (pytest here, vitest in the Mini App).

Priority, as in the Mini App: the owner's override for the day (as is) > own history (the higher of the
record-based weight and double progression) > the owner's words (baseline, only without history). On top
of the Mini App this module adds a transfer from a related exercise when the exercise itself has no
history and no baseline (`related`):

- Equipment and movement are read from the name (`equipment`, `movement`, `grip`). Dumbbell weights are
  PER HAND everywhere: in the history and in suggestions («на руку»).
- Only whitelisted pairs convert (TRANSFER_EQUIPMENT), from the related exercise's best Epley 1RM (one
  hop, real history only): curls (with the grip factor) and the SAME press or raise across equipment
  (EQ_FACTORS), e.g. жим гантелей сидя <-> жим сидя в смите <-> жим штанги сидя. Both names must have the
  same variant words (`modifiers`: incline, front, seated/standing, close grip, румынская vs становая, …);
  unilateral and single-dumbbell exercises (болгарские, выпады, гоблет, одной рукой, концентрированные)
  never take a number from anything. Among candidates the CLOSEST name wins (most shared words), not the
  strongest lift. Squats, hinges, rows, pulldowns, triceps extensions and rear delts never convert. Cables and machines never convert to or from anything else (different resistance
  curves, stacks and pulleys): between two cable (or two machine) exercises of the same movement the
  answer is a hint «подбери по ощущениям: начни с ~X», X = the first set of the related exercise's last
  session; otherwise no number. Unknown equipment is never "the same equipment".
- Grip: a pronated (хват сверху, обратный) curl from a non-pronated one × REVERSE_GRIP.
- A transferred 1RM is an estimate: the share of it never uses the "heavy" percentage (TRANSFER_INTENSITY).

Every suggestion carries a Russian reason and its source (see Source). The day plan's weightFactor and a
deload week scale the base weight with `scale_weight` (never an override).
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gymbot.config import Settings
from gymbot.db.models import DayPlan, Exercise, Program, User, UserProgram, Workout, WorkoutSet
from gymbot.services import baselines, deload, overrides, plan
from gymbot.services import day_adjustments as dayadj
from gymbot.services.advice import NEXT_DAY_SEARCH
from gymbot.services.nutrition import _aware
from gymbot.services.programs import find_day, load_program, normalize, program_position

Source = Literal["override", "history", "baseline", "related", "hint", "none"]
Equipment = Literal["smith", "ez", "cable", "machine", "dumbbell", "barbell", "bodyweight"]

# ---- transfer constants ----

# Free-weight 1RM transfer, (from, to) -> factor; the source 1RM is per hand for dumbbells.
# - dumbbell -> barbell/EZ (curls, presses): two hands, but a bar lets both arms share and needs less
#   stabilisation, while a dumbbell curl allows full supination; a bar curl is usually 80-90 % of the two
#   dumbbells together: 2 × 0.85, the conservative middle.
# - dumbbell -> Smith press: the guided bar needs no stabilisation at all, so a little more of the sum
#   carries over: 2 × 0.9.
# - the reverse directions are the same shares inverted and rounded down (0.85 / 2, 0.8 / 2): going from a
#   bar to dumbbells loses stabilisation, so it must not overshoot.
# - barbell <-> EZ: the same bar family, the same numbers; bar <-> Smith 0.9 both ways (the Smith bar's
#   own weight and counterbalance vary by gym: stay below).
EQ_FACTORS: dict[tuple[str, str], float] = {
    ("dumbbell", "barbell"): 1.7,
    ("dumbbell", "ez"): 1.7,
    ("dumbbell", "smith"): 1.8,
    ("barbell", "dumbbell"): 0.425,
    ("ez", "dumbbell"): 0.425,
    ("smith", "dumbbell"): 0.4,
    ("barbell", "ez"): 1.0,
    ("ez", "barbell"): 1.0,
    ("barbell", "smith"): 0.9,
    ("ez", "smith"): 0.9,
    ("smith", "barbell"): 0.9,
    ("smith", "ez"): 0.9,
}
EQ_FACTOR_TEXT = {1.7: "× 2 × 0,85", 1.8: "× 2 × 0,9", 0.425: "÷ 2 × 0,85", 0.4: "÷ 2 × 0,8", 0.9: "× 0,9"}
FREE_WEIGHTS = ("dumbbell", "barbell", "ez", "smith")
HINT_EQUIPMENT = ("cable", "machine")
# Reverse (pronated) curls are 60-70 % of supinated ones (brachioradialis instead of the biceps leading):
# the middle, 0.65, from any non-pronated curl of the same or a convertible equipment.
REVERSE_GRIP = 0.65
# Movement -> equipment between which numbers transfer. Lateral raises: dumbbells to dumbbells only.
TRANSFER_EQUIPMENT: dict[str, frozenset[str]] = {
    "curl": frozenset({"dumbbell", "barbell", "ez"}),
    "overhead_press": frozenset({"dumbbell", "barbell", "smith"}),
    "bench": frozenset({"dumbbell", "barbell", "smith"}),
    "lateral_raise": frozenset({"dumbbell"}),
}
TRANSFER_INTENSITY = "medium"  # a transferred 1RM is an estimate: never the "heavy" share of it

EQUIPMENT_NAMES = {
    "smith": "смит", "ez": "EZ-гриф", "cable": "блок", "machine": "тренажёр", "dumbbell": "гантели",
    "barbell": "штанга", "bodyweight": "свой вес",
}

# ---- data (mirrors miniapp/src/store.ts and program.ts) ----


@dataclass(frozen=True)
class SetEntry:
    weight: float | None
    reps: int | None
    done: bool = True


@dataclass(frozen=True)
class ExerciseLog:
    name: str
    sets: tuple[SetEntry, ...]


@dataclass(frozen=True)
class HistoryWorkout:
    started_at: str  # ISO, UTC; oldest workout first in a history list
    exercises: tuple[ExerciseLog, ...]


@dataclass(frozen=True)
class Prescription:
    sets: int
    reps_min: int | None = None
    reps_max: int | None = None
    drop_reps: tuple[int, ...] | None = None


@dataclass(frozen=True)
class ProgramExercise:
    name: str
    intensity: str | None
    prescription: Prescription


@dataclass(frozen=True)
class Baseline:
    exercise: str
    weight_kg: float
    reps: int | None
    fact_id: int = 0


@dataclass(frozen=True)
class Override:
    exercise: str
    weight_kg: float
    date: str  # YYYY-MM-DD, local day


@dataclass(frozen=True)
class Record1rm:
    e1rm: float
    weight: float
    reps: int
    date: str


@dataclass
class Suggestion:
    weight: float | None  # None: no number (source "hint" or "none")
    reason: str
    source: Source
    per_hand: bool = False  # dumbbells: the weight of one dumbbell
    base_weight: float | None = None  # before the day plan's factor
    factor: float = 1.0
    hint_kg: float | None = None  # "hint": start around this, it is not a computed weight
    related: str | None = None  # the related exercise a transfer or hint came from
    override: bool = False


# ---- JS-compatible numbers ----


def js_round(x: float) -> int:
    """Math.round: a half goes up (Python's round goes to even)."""
    return math.floor(x + 0.5)


def clean(n: float) -> float:
    """Removes float noise (60 + 2.5 * 0.1 ...), like progression.ts clean."""
    return js_round(n * 100) / 100


def format_kg(n: float) -> str:
    """stats.ts formatKg: 60 -> '60', 17.5 -> '17,5' (one decimal, a half up)."""
    if n == int(n):
        return str(int(n))
    return str(Decimal(repr(n)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)).replace(".", ",")


def _num1(n: float) -> str:
    """Reason numbers with one decimal: 25.333 -> '25,3', 43.0 -> '43'."""
    v = float(Decimal(repr(n)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))
    return format_kg(v)


def reps_word(n: int) -> str:
    """Genitive after "для": для 1 повтора, для 8 повторов (progression.ts repsWord)."""
    return f"{n} {'повтора' if n % 10 == 1 and n % 100 != 11 else 'повторов'}"


# ---- the port of progression.ts ----


def e1rm(weight: float, reps: int) -> float:
    return weight if reps <= 1 else weight * (1 + reps / 30)


def _working_sets(w: HistoryWorkout, name: str) -> list[SetEntry]:
    ex = next((e for e in w.exercises if e.name == name), None)
    return [s for s in ex.sets if s.done and s.weight is not None] if ex else []


def best_e1rm(history: list[HistoryWorkout], name: str) -> Record1rm | None:
    best: Record1rm | None = None
    for w in history:
        for s in _working_sets(w, name):
            assert s.weight is not None
            if s.weight <= 0:
                continue
            reps = s.reps if s.reps is not None else 1
            value = e1rm(s.weight, reps)
            if best is None or value > best.e1rm:
                best = Record1rm(value, s.weight, reps, w.started_at)
    return best


def equipment_step(name: str) -> float:
    """Dumbbells go up by 1 kg, barbells and machines by 2.5 kg (progression.ts equipmentStep)."""
    return 1 if "гантел" in name.lower() else 2.5


def round_to_step(weight: float, step: float) -> float:
    return clean(js_round(weight / step) * step)


def percent_of_1rm(reps_max: int | None, intensity: str | None, reps_min: int | None = None) -> float:
    reps = reps_max if reps_max is not None else reps_min if reps_min is not None else 10
    heavy = 1 / (1 + reps / 30)
    if intensity == "heavy":
        return heavy
    if intensity == "light":
        return heavy * 0.8
    return heavy * 0.9


def last_same_session(history: list[HistoryWorkout], name: str) -> list[SetEntry] | None:
    for w in reversed(history):
        done = _working_sets(w, name)
        if done:
            return done
    return None


def progression_reps(p: Prescription) -> int | None:
    """The first drop for dropsets, else the top of the range (also progression.ts targetReps)."""
    if p.drop_reps:
        return p.drop_reps[0]
    return p.reps_max if p.reps_max is not None else p.reps_min


def double_progression(last: list[SetEntry] | None, p: Prescription, step: float) -> float | None:
    sets = [s for s in last or [] if s.weight is not None]
    if not sets:
        return None
    top = max(s.weight for s in sets if s.weight is not None)
    target = progression_reps(p)
    if target is None:
        return top
    all_at_top = all(s.weight == top and s.reps is not None and s.reps >= target for s in sets)
    return clean(top + step) if all_at_top else top


def norm_name(name: str) -> str:
    return " ".join(name.split()).lower().replace("ё", "е")


def find_baseline(baselines: list[Baseline], name: str) -> Baseline | None:
    key = norm_name(name)
    best: Baseline | None = None
    for b in baselines:
        if norm_name(b.exercise) == key and (best is None or b.fact_id > best.fact_id):
            best = b
    return best


def find_override(overrides: list[Override], name: str, day: str) -> Override | None:
    key = norm_name(name)
    hit: Override | None = None
    for o in overrides:
        if o.date == day and norm_name(o.exercise) == key:
            hit = o
    return hit


def override_reason(kg: float) -> str:
    return f"ты поставил на сегодня {format_kg(kg)} кг"


def _from_baseline(b: Baseline, exercise: ProgramExercise, step: float) -> Suggestion | None:
    reps = progression_reps(exercise.prescription)
    pct = percent_of_1rm(reps, exercise.intensity)
    mx = b.weight_kg if b.reps is None else e1rm(b.weight_kg, b.reps)
    weight = round_to_step(mx * pct, step)
    if weight <= 0:
        return None
    share = f"{js_round(pct * 100)} % для {reps_word(reps if reps is not None else 10)}"
    kg = format_kg(b.weight_kg)
    if b.reps is None:
        reason = f"по твоим словам ~{kg} кг (как максимум), {share}"
    elif b.reps == 1:
        reason = f"от твоего максимума {kg} кг, {share}"
    else:
        reason = f"по твоим словам {kg} × {b.reps} (1ПМ ≈ {format_kg(js_round(mx))} кг), {share}"
    return Suggestion(weight, reason, "baseline")


def has_history(history: list[HistoryWorkout], name: str) -> bool:
    return last_same_session(history, name) is not None or best_e1rm(history, name) is not None


def suggest_weight(
    history: list[HistoryWorkout],
    exercise: ProgramExercise,
    baselines: list[Baseline] | None = None,
    overrides: list[Override] | None = None,
    today: str | None = None,
) -> Suggestion | None:
    """progression.ts suggestWeight: override for `today` > max(record, double progression) > baseline."""
    name, p, intensity = exercise.name, exercise.prescription, exercise.intensity
    o = find_override(overrides or [], name, today) if today else None
    if o is not None:
        return Suggestion(o.weight_kg, override_reason(o.weight_kg), "override", override=True)
    last = last_same_session(history, name)
    record = best_e1rm(history, name)
    step = equipment_step(name)
    if last is None and record is None:
        b = find_baseline(baselines or [], name)
        return _from_baseline(b, exercise, step) if b else None

    best: Suggestion | None = None
    progressed = double_progression(last, p, step)
    if progressed is not None and last is not None:
        top = max(s.weight for s in last if s.weight is not None)
        target = progression_reps(p)
        if progressed > top:
            reason = f"прошлый раз {format_kg(top)} × {target} во всех подходах, +{format_kg(step)} кг"
        else:
            reason = f"как в прошлый раз {format_kg(top)} кг"
        best = Suggestion(progressed, reason, "history")
    if record is not None:
        reps = progression_reps(p)
        pct = percent_of_1rm(reps, intensity)
        from_record = round_to_step(record.e1rm * pct, step)
        if from_record > 0 and (best is None or best.weight is None or from_record > best.weight):
            reason = (
                f"от рекорда {format_kg(js_round(record.e1rm))} кг (1ПМ), {js_round(pct * 100)} % для "
                f"{reps_word(reps if reps is not None else 10)}"
            )
            best = Suggestion(from_record, reason, "history")
    return best


# ---- the port of plan.ts ----


def safe_factor(f: float | None) -> float:
    return f if f is not None and math.isfinite(f) and 0.3 <= f <= 1.5 else 1.0


def scale_weight(base: float | None, factor: float, step: float) -> float | None:
    """plan.ts scaleWeight: weight × factor on the equipment step (see there for the rules)."""
    if base is None or factor == 1:
        return base
    if base <= step and factor < 1:
        return base
    target = base * factor
    x = target / step
    n = math.ceil(x - 0.5 - 1e-9) if factor < 1 else math.floor(x + 0.5 + 1e-9)
    r = round_to_step(n * step, step)
    if factor < 1 and r >= base:
        lower = round_to_step((math.ceil(base / step - 1e-9) - 1) * step, step)
        r = lower if base - lower <= 2 * (base - target) + 1e-9 else base
    elif factor > 1 and r <= base:
        upper = round_to_step((math.floor(base / step + 1e-9) + 1) * step, step)
        r = upper if upper - base <= 2 * (target - base) + 1e-9 else base
    return min(base, max(step, r)) if factor < 1 else max(base, r)


# ---- names: equipment, movement, grip ----


def _rx(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern)


_L = r"(?<![а-яa-z])"  # word start
# Checked in order: "смит" before the bar words, EZ before "гриф", blocks before machines.
_EQUIPMENT: list[tuple[str, re.Pattern[str]]] = [
    ("smith", _rx(r"смит")),
    ("ez", _rx(_L + r"ez(?![a-z])|изогнут")),
    ("cable", _rx(r"блок|кроссовер|канат")),
    ("machine", _rx(r"тренаж|пек[\s-]?дек|хаммер|" + _L + r"гакк?(?![а-я])|машин")),
    ("dumbbell", _rx(r"гантел")),
    ("barbell", _rx(r"штанг|гриф")),
    ("bodyweight", _rx(r"подтягиван|отжиман|брусь|" + _L + r"в висе|без веса|собственн")),
]
_LEGS = _rx(r"(?:сгибан|разгибан|жим)\w*\s+(?:\w+\s+)?ног")  # leg curls, leg extensions, leg press
# Checked in order: the French press before the bench, the rear delt before the lateral raise.
_MOVEMENTS: list[tuple[str, re.Pattern[str]]] = [
    ("rear_delt", _rx(r"задн\w*\s+дельт|пек[\s-]?дек\w*\s+на\s+задн|обратн\w*\s+(?:развед|бабочк)|"
                      r"развед\w*\s+(?:\w+\s+)?в\s+наклон")),
    ("lateral_raise", _rx(r"отведен|" + _L + r"мах(?:и|ов)" + r"(?![а-я])")),
    ("triceps_extension", _rx(r"француз|разгибан|трицепс")),  # not a press "на трицепс": see movement()
    ("curl", _rx(r"сгибан|бицепс|молот")),
    ("bench", _rx(r"жим\w*\s+(?:\w+\s+){0,2}?леж|бенч|жим\w*\s+(?:\w+\s+){0,3}?(?:наклонн|под\s+углом)")),
    ("overhead_press", _rx(r"жим\w*\s+(?:\w+\s+){0,3}?(?:сидя|стоя|над\s+голов)|армейск")),
    ("row", _rx(r"тяг\w*\s+(?:\w+\s+){0,3}?(?:горизонт|к\s+поясу|в\s+наклон|нижн)")),
    ("pulldown", _rx(r"тяг\w*\s+(?:\w+\s+){0,3}?(?:вертикал|верхн)|подтягиван")),
    ("squat", _rx(r"присед")),
    ("hinge", _rx(r"румын|станов")),
]
_PRESS = _rx(_L + r"жим")
# Variant words: two names transfer only with the same set (curls ignore seated / standing).
_MODIFIERS: list[tuple[str, re.Pattern[str]]] = [
    ("incline", _rx(r"наклонн|под\s+углом|накл\w*\s+скам")),
    ("decline", _rx(r"головой\s+вниз|обратн\w*\s+наклон|отрицательн\w*\s+наклон")),
    ("front", _rx(r"фронтал")),
    ("seated", _rx(r"сидя")),
    ("standing", _rx(r"стоя|армейск")),
    ("close", _rx(r"узк")),
    ("wide", _rx(r"широк")),
    ("sumo", _rx(r"сумо")),
    ("behind", _rx(r"из[\s-]за\s+голов|за\s+голов")),
    ("overhead", _rx(r"над\s+голов")),
    ("lying", _rx(r"леж")),
    ("scott", _rx(r"скотт|парт")),
    ("arnold", _rx(r"арнольд")),
    ("hack", _rx(_L + r"(?:гакк?|хакк?)")),
    ("rdl", _rx(r"румын")),
    ("deadlift", _rx(r"станов")),
    ("deficit", _rx(r"дефицит")),
    ("pause", _rx(r"пауз")),
]
_CURL_IGNORES = frozenset({"seated", "standing"})
# One arm / one leg, or one dumbbell held in both hands: no number from (or to) anything else.
_UNILATERAL = _rx(r"болгарск|выпад|гоблет|одной\s+рук|одной\s+ног|одноруч|концентрир|сплит|пистолет|на\s+одну\s+")
_SINGLE_DUMBBELL = _rx(r"гоблет|одной\s+гантел|одну\s+гантел|гантелью")
_PRONATED = _rx(r"пронац|хват\w*\s+сверху|обратн\w*\s+хват")
_NEUTRAL = _rx(r"молот|нейтрал")


def equipment(name: str) -> str | None:
    key = normalize(name)
    return next((e for e, pattern in _EQUIPMENT if pattern.search(key)), None)


def movement(name: str) -> str | None:
    key = normalize(name)
    if _LEGS.search(key):
        return None
    press = bool(_PRESS.search(key)) and "француз" not in key  # "жим узким хватом на трицепс" is a press
    return next(
        (m for m, pattern in _MOVEMENTS if pattern.search(key) and not (press and m == "triceps_extension")), None
    )


def modifiers(name: str) -> frozenset[str]:
    """Variant words of the name (see _MODIFIERS); curls ignore seated / standing."""
    key = normalize(name)
    mods = frozenset(m for m, pattern in _MODIFIERS if pattern.search(key))
    return mods - _CURL_IGNORES if movement(name) == "curl" else mods


def unilateral(name: str) -> bool:
    return bool(_UNILATERAL.search(normalize(name)))


def grip(name: str) -> str | None:
    """Curls only: pronated / neutral / supinated (the default curl); None for other movements."""
    if movement(name) != "curl":
        return None
    key = normalize(name)
    if _PRONATED.search(key):
        return "pronated"
    if _NEUTRAL.search(key):
        return "neutral"
    return "supinated"


def per_hand(name: str) -> bool:
    """Dumbbells are logged per hand; one dumbbell in both hands (гоблет) is not «на руку»."""
    return equipment(name) == "dumbbell" and not _SINGLE_DUMBBELL.search(normalize(name))


# ---- related exercises ----


def _history_names(history: list[HistoryWorkout]) -> list[str]:
    """Exercise names in the history, most recently done first."""
    seen: dict[str, int] = {}
    for i, w in enumerate(history):
        for e in w.exercises:
            seen[e.name] = i
    return sorted(seen, key=lambda n: (-seen[n], n))


def _hand(name: str) -> str:
    return " на руку" if per_hand(name) else ""


def _shared_words(a: str, b: str) -> int:
    """Content words of `a` also in `b` (rough stems): the closest variant wins a transfer."""
    wa, wb = _name_words(a), _name_words(b)
    return sum(1 for w in wa if any(w == x or (min(len(w), len(x)) >= 4 and (w.startswith(x) or x.startswith(w)))
                                    for x in wb))


def _name_words(name: str) -> list[str]:
    return [w[:6] for w in re.findall(r"[а-яa-z0-9]+", normalize(name)) if len(w) >= 3]


def _first_set_of_last_session(history: list[HistoryWorkout], name: str) -> float | None:
    last = last_same_session(history, name)
    return last[0].weight if last else None


def related(history: list[HistoryWorkout], exercise: ProgramExercise) -> Suggestion:
    """The transfer from a related exercise (see the module doc): "related" with a weight, "hint" with a
    starting point, or "none" with a reason (no number). For an exercise without its own history."""
    name = exercise.name
    mv, eq_t, grip_t = movement(name), equipment(name), grip(name)
    no_number = "истории нет: вес по ощущениям, 2 повтора в запасе"
    if mv is None:
        return Suggestion(None, no_number, "none")
    numbers: list[tuple[tuple[Any, ...], str, float, Record1rm, float]] = []
    hints: list[tuple[tuple[Any, ...], str, float]] = []
    others: list[tuple[str, str]] = []  # (exercise, why no number)
    order = _history_names(history)
    mods_t, one_side_t, allowed = modifiers(name), unilateral(name), TRANSFER_EQUIPMENT.get(mv, frozenset())
    for src in order:
        if normalize(src) == normalize(name) or movement(src) != mv:
            continue
        rec = best_e1rm(history, src)
        if rec is None:
            continue
        eq_s, grip_s = equipment(src), grip(src)
        key = (grip_t != grip_s, eq_t != eq_s, -_shared_words(name, src), order.index(src), src)
        if one_side_t or unilateral(src):
            others.append((src, "упражнение на одну руку или ногу"))
        elif eq_t in HINT_EQUIPMENT or eq_s in HINT_EQUIPMENT or eq_t is None or eq_s is None:
            # Cables and machines: a starting point from the same kind of equipment (any variant), no number.
            first = _first_set_of_last_session(history, src) if eq_t == eq_s and eq_t in HINT_EQUIPMENT else None
            if first:
                hints.append((key, src, first))
            else:
                others.append((src, EQUIPMENT_NAMES.get(eq_s or "", "другой снаряд")))
        elif modifiers(src) != mods_t:
            others.append((src, "другой вариант упражнения"))
        elif eq_t in allowed and eq_s in allowed and (eq_t == eq_s or (eq_s, eq_t) in EQ_FACTORS):
            factor = 1.0 if eq_t == eq_s else EQ_FACTORS[(eq_s, eq_t)]  # type: ignore[index]
            g = REVERSE_GRIP if grip_t == "pronated" and grip_s != "pronated" else 1.0
            numbers.append((key, src, factor, rec, g))
        elif eq_t == eq_s:
            others.append((src, "другое упражнение"))
        else:
            others.append((src, EQUIPMENT_NAMES.get(eq_s or "", "другой снаряд")))
    if numbers:
        _, src, factor, rec, g = min(numbers, key=lambda c: c[0])
        reps = progression_reps(exercise.prescription)
        intensity = TRANSFER_INTENSITY if exercise.intensity in (None, "heavy") else exercise.intensity
        pct = percent_of_1rm(reps, intensity)
        converted = rec.e1rm * factor * g
        weight = round_to_step(converted * pct, equipment_step(name))
        if weight > 0:
            steps = " ".join(x for x in (EQ_FACTOR_TEXT.get(factor, ""), "× 0,65 (обратный хват слабее)" if g != 1 else "") if x)
            calc = f"1ПМ ≈ {_num1(rec.e1rm)}{' ' + steps if steps else ''}"
            if steps:
                calc += f" ≈ {_num1(converted)} кг"
            else:
                calc += " кг"
            reason = (
                f"по «{src}» {format_kg(rec.weight)}×{rec.reps}{_hand(src)}: {calc}, "
                f"{js_round(pct * 100)} % для {reps_word(reps if reps is not None else 10)}"
            )
            return Suggestion(weight, reason, "related", related=src)
    if hints:
        _, src, first = min(hints, key=lambda c: c[0])
        what = EQUIPMENT_NAMES[eq_t or ""]
        reason = (
            f"подбери по ощущениям: начни с ~{format_kg(first)} кг, как первый подход в «{src}» "
            f"({what}: числа разных тренажёров не сравнить)"
        )
        return Suggestion(None, reason, "hint", hint_kg=first, related=src)
    if others:
        src, what = others[0]
        reason = (
            f"подбери по ощущениям: вес из «{src}» ({what}) сюда не переносится; "
            f"начни легко, рабочий вес — с 2 повторами в запасе"
        )
        return Suggestion(None, reason, "none", related=src)
    return Suggestion(None, no_number, "none")


# ---- everything together ----


def suggest(
    history: list[HistoryWorkout],
    exercise: ProgramExercise,
    baselines: list[Baseline] | None = None,
    overrides: list[Override] | None = None,
    day: str | None = None,
    factor: float = 1.0,
    today: bool = True,
) -> Suggestion:
    """The weight for `exercise` on `day` (YYYY-MM-DD): suggest_weight, else the related transfer; the day
    plan's `factor` on top (not on an override). `today` False: an override reads «на <дд.мм>»."""
    s = suggest_weight(history, exercise, baselines, overrides, day)
    if s is None:
        s = related(history, exercise)
    if s.source == "override" and not today and day:
        s.reason = f"ты поставил на {day[8:10]}.{day[5:7]} {format_kg(s.weight or 0)} кг"
    s.per_hand = per_hand(exercise.name)
    s.base_weight = s.weight
    f = safe_factor(factor)
    if s.weight is not None and s.source != "override" and f != 1:
        s.weight = scale_weight(s.weight, f, equipment_step(exercise.name))
        s.factor = f
    return s


# ---- JSON (data/progression_cases.json uses the Mini App's camelCase shapes) ----


def history_from_json(raw: list[dict[str, Any]]) -> list[HistoryWorkout]:
    return [
        HistoryWorkout(
            w["startedAt"],
            tuple(
                ExerciseLog(e["name"], tuple(SetEntry(s.get("weight"), s.get("reps"), s.get("done", True))
                                             for s in e["sets"]))
                for e in w["exercises"]
            ),
        )
        for w in raw
    ]


def exercise_from_json(raw: dict[str, Any]) -> ProgramExercise:
    p = raw["prescription"]
    drops = p.get("drop_reps")
    return ProgramExercise(
        raw["name"], raw.get("intensity"),
        Prescription(p["sets"], p.get("reps_min"), p.get("reps_max"), tuple(drops) if drops else None),
    )


def baselines_from_json(raw: list[dict[str, Any]]) -> list[Baseline]:
    return [Baseline(b["exercise"], b["weightKg"], b.get("reps"), b.get("factId", 0)) for b in raw]


def overrides_from_json(raw: list[dict[str, Any]]) -> list[Override]:
    return [Override(o["exercise"], o["weightKg"], o["date"]) for o in raw]



# ---- a program day from the database ----


@dataclass
class DayRow:
    name: str  # the name trained: the program JSON name, or the day plan's replacement
    program_name: str
    sets: int
    reps_min: int | None
    reps_max: int | None
    drop_reps: tuple[int, ...] | None
    suggestion: Suggestion | None  # None: skipped by the day plan
    note: str | None = None  # the day plan's reason for this exercise


@dataclass
class DayWeights:
    day: date
    week: int
    weekday: int
    rows: list[DayRow]
    summary: str | None = None  # the day plan's or the deload's note
    rest: bool = False  # today's plan says rest: rows are the program as written


async def history(session: AsyncSession, user_id: int, before: date) -> list[HistoryWorkout]:
    """Workouts strictly before `before` (local days), oldest first; drops left out, as the Mini App gets them."""
    rows = (
        await session.execute(
            select(Workout.id, Workout.started_at, Exercise.name, WorkoutSet.weight_kg, WorkoutSet.reps)
            .join(WorkoutSet, WorkoutSet.workout_id == Workout.id)
            .join(Exercise, Exercise.id == WorkoutSet.exercise_id)
            .where(Workout.user_id == user_id, Workout.performed_on < before, WorkoutSet.drop_index == 0)
            .order_by(Workout.performed_on, Workout.started_at, Workout.id, WorkoutSet.set_index)
        )
    ).all()
    workouts: dict[int, tuple[str, dict[str, list[SetEntry]]]] = {}
    for wid, at, name, weight, reps in rows:
        _, by_ex = workouts.setdefault(wid, (_aware(at).isoformat(), {}))
        by_ex.setdefault(name, []).append(SetEntry(float(weight) if weight is not None else None, reps))
    return [
        HistoryWorkout(at, tuple(ExerciseLog(n, tuple(sets)) for n, sets in by_ex.items()))
        for at, by_ex in workouts.values()
    ]


@dataclass
class ProgramRef:
    program: Program
    started_on: date


async def program_ref(session: AsyncSession, user: User, settings: Settings) -> ProgramRef | None:
    """The user's latest program choice (read only, like plan._program_day)."""
    up = await session.scalar(
        select(UserProgram).where(UserProgram.user_id == user.id).order_by(UserProgram.id.desc()).limit(1)
    )
    if up is None:
        return None
    return ProgramRef(await load_program(session, up.program_id), up.started_on)


@dataclass
class _Item:
    name: str
    intensity: str | None
    sets: int
    reps_min: int | None
    reps_max: int | None
    drop_reps: tuple[int, ...] | None


def program_day(ref: ProgramRef, day: date) -> tuple[int, int, list[_Item]] | None:
    """(week, weekday, exercises) of the program on `day`; None on a rest day or outside the program."""
    pos = program_position(ref.started_on, len(ref.program.weeks), day)
    if pos.not_started or pos.finished:
        return None
    found = find_day(ref.program, pos.week, pos.weekday)
    if found is None or not found.items:
        return None
    items = [
        _Item(i.exercise.name, i.intensity, i.sets, i.reps_min, i.reps_max, tuple(i.drop_reps) if i.drop_reps else None)
        for i in found.items  # sorted by order (relationship order_by)
    ]
    return pos.week, pos.weekday, items


def training_day_from(ref: ProgramRef, start: date, days: int = NEXT_DAY_SEARCH) -> date | None:
    """The first program training day on or after `start` (within `days`)."""
    for k in range(days + 1):
        d = start + timedelta(days=k)
        if program_position(ref.started_on, len(ref.program.weeks), d).finished:
            return None
        if program_day(ref, d) is not None:
            return d
    return None


async def _today_plan(
    session: AsyncSession, user: User, settings: Settings, tz: ZoneInfo, now_utc: datetime
) -> tuple[str, str | None, list[plan.PlanExercise]] | None:
    """Today's day plan without writing anything: the stored one while built from the same inputs, else the
    rule draft in memory (the plan service builds and stores the real one, with the model, on its own)."""
    inputs = await plan.collect_inputs(session, user, settings, tz, now_utc)
    if inputs is None:
        return None
    draft = plan.rule_draft(inputs, now_utc)
    row = await session.scalar(
        select(DayPlan).where(DayPlan.user_id == user.id, DayPlan.plan_date == inputs.today)
    )
    if row is not None and row.inputs_hash == plan.inputs_hash(inputs, draft):
        exercises = [plan.PlanExercise.model_validate(e) for e in json.loads(row.exercises_json or "[]")]
        return row.readiness, row.summary if exercises else None, exercises
    if not draft.adjusted:
        return draft.readiness, None, []
    return draft.readiness, draft.summary, draft.exercises


async def day_weights(
    session: AsyncSession, user: User, settings: Settings, tz: ZoneInfo, now_utc: datetime, day: date,
    ref: ProgramRef | None = None,
) -> DayWeights | None:
    """The program day `day` with a weight for every exercise; None on a rest day or without a program.

    Today: the day plan's corrections (sets, reps, weightFactor, skip, replacement) as the Mini App applies
    them; a plan that skips everything (rest) leaves the program as written with `rest` set. Another day:
    a deload week running on that day (weights × deload.WEIGHT_FACTOR, a third fewer sets) and the manual
    adjustment of that day (gymbot.services.day_adjustments), the lighter of both, as the plan will do."""
    ref = ref or await program_ref(session, user, settings)
    if ref is None:
        return None
    found = program_day(ref, day)
    if found is None:
        return None
    week, weekday, items = found
    today = now_utc.astimezone(tz).date()
    hist = await history(session, user.id, day)
    base = [Baseline(b.exercise, b.weightKg, b.reps, b.factId) for b in await baselines.current(session, user.id)]
    over = [Override(o.exercise, o.weightKg, o.date.isoformat())
            for o in await overrides.for_day(session, user.id, day)]
    iso = day.isoformat()
    adjust: dict[str, plan.PlanExercise] = {}
    summary: str | None = None
    rest = False
    deload_factor = 1.0
    adj: dayadj.DayAdjust | None = None
    if day == today:
        got = await _today_plan(session, user, settings, tz, now_utc)
        if got is not None:
            readiness, summary, exercises = got
            if readiness == "rest" or (exercises and all(e.skip for e in exercises)):
                rest = True
            else:
                adjust = {normalize(e.name): e for e in exercises}
    else:
        if (until := await deload.active_until(session, user.id, day)) is not None:
            deload_factor, summary = deload.WEIGHT_FACTOR, deload.summary(until)
        if (adj := await dayadj.get(session, user.id, day)) is not None and not adj.empty():
            summary = " ".join(x for x in (summary, dayadj.summary(adj)) if x)
        else:
            adj = None
    rows: list[DayRow] = []
    for it in items:
        a = adjust.get(normalize(it.name))
        sets, reps_min, reps_max = it.sets, it.reps_min, it.reps_max
        if a is not None and a.skip:
            rows.append(DayRow(it.name, it.name, sets, reps_min, reps_max, it.drop_reps, None, a.reason))
            continue
        name = it.name
        factor = deload_factor
        if a is not None:
            name = (a.replaceWith or "").strip().lower() or it.name
            sets = a.sets if a.sets >= 1 else it.sets
            if not it.drop_reps:
                reps_min = a.repsMin if a.repsMin is not None and a.repsMin >= 1 else it.reps_min
                reps_max = a.repsMax if a.repsMax is not None and a.repsMax >= 1 else it.reps_max
                if reps_min is not None and reps_max is not None and reps_max < reps_min:
                    reps_max = reps_min
            factor = a.weightFactor
        else:
            if deload_factor != 1:
                sets = deload.deload_sets(it.sets)
            if adj is not None:
                sets, factor, skipped = plan.adjust_item(adj, it.name, it.sets, sets, factor)
                if skipped:
                    rows.append(DayRow(it.name, it.name, it.sets, reps_min, reps_max, it.drop_reps, None,
                                       dayadj.SKIP_REASON))
                    continue
        exercise = ProgramExercise(name, it.intensity, Prescription(sets, reps_min, reps_max, it.drop_reps))
        s = suggest(hist, exercise, base, over, iso, factor, today=day == today)
        rows.append(DayRow(name, it.name, sets, reps_min, reps_max, it.drop_reps, s, a.reason if a else None))
    return DayWeights(day, week, weekday, rows, summary, rest)
