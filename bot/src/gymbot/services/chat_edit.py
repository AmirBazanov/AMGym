"""Program edits from the chat: "убери французский жим из дня рук", "поставь на сгибания 30 кг".

Plan: docs/plans/2026-10-09-chat-program-control.md (stage 1). Flow (handlers/chat_edit.py):
1. `is_edit_command` — a conservative regex, checked in process_text before saved_edits (a question, a past
   tense verb, saved-record words and settings vocabulary never route here).
2. One `complete_json(purpose="edit")` call with EDIT_SYSTEM_PROMPT (gymbot.llm.prompts_edit) and
   `prompt_context` (today, the current program week by days, the next week, weights, catalog). The model
   answers with intentions by name (`parse_actions`, each validated on its own), never with ids or weeks.
3. `compile_ops` resolves days (weekday, today/tomorrow, focus or the exercises of the day), exercises (only
   within that day) and scopes (weeks) into the program editor's PATCH ops (gymbot.services.program_editor)
   and weights for a day (WeightOverride). Ambiguity is a `Clarify` with buttons, never a guess.
4. `preview` dry-runs `edit_program` (rolled back) for the skipped weeks; "✅ Применить" -> `apply` runs the
   same `edit_program` with the version seen in the preview, then the overrides, in one transaction.

A message the model turned into at least one valid action is consumed by this path; with no valid action the
text goes on as usual (saved edits, settings, the parser). Weights in kg, days are local dates (TIMEZONE).
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gymbot.db.models import Exercise, User, UserProgram, WeightOverride
from gymbot.services import baselines, overrides
from gymbot.services import chat_settings as cs
from gymbot.services import program_editor as pe
from gymbot.services.advice import NEXT_DAY_SEARCH
from gymbot.services.plan import muscle_group
from gymbot.services.programs import Position, exercise_catalog, load_program, normalize, program_position
from gymbot.services.users import active_program

log = logging.getLogger(__name__)

# ---- routing ----

_EDIT_VERB = re.compile(
    r"(?<!\w)(?:убер(?:и|ите)|удали(?:те)?|добав(?:ь|ьте)|замени(?:те)?|поменяй(?:те)?|постав(?:ь|ьте)|"
    r"перенес(?:и|ите)|сделай(?:те)?|верни(?:те)?)(?!\w)"
)
_MOVE_VERB = re.compile(r"(?<!\w)(?:перенес(?:и|ите)|поменяй(?:те)?|сделай(?:те)?)(?!\w)")
_INSTEAD = re.compile(r"(?<!\w)вместо(?!\w)")
_WEEKDAY = r"понедельник\w*|вторник\w*|сред[уаеы]|четверг\w*|пятниц\w*|суббот\w*|воскресень\w*"
_WEEKDAY_ADJ = r"понедельничн\w*|вторничн\w*|средов\w*|четвергов\w*|пятничн\w*|субботн\w*|воскресн\w*"
_STRUCTURE = re.compile(
    r"(?<!\w)(?:день|дня|дне|дню|днем|дни|дней|программ\w*|недел\w*|местами)(?!\w)|"
    r"(?<!\w)из\s+(?:\w+\s+)?тренировк"
)
# A weekday names a program day ("убери жим в пятницу") as often as a saved record's day ("удали ужин в
# понедельник"): it only routes to the edit model, saved_edits still gets the text the model declines.
_DAY_NAME = re.compile(rf"(?<!\w)(?:{_WEEKDAY}|{_WEEKDAY_ADJ})(?!\w)")
_WORKOUT = re.compile(r"(?<!\w)тренировк\w*")
_PER_DAY = re.compile(r"(?<!\w)(?:в|на)\s+(?:день|сутки)(?=\s*(?:$|[,.;:!)]|(?:и|а|но)\s))")  # "белок 170 в день" is a norm
_SET_WEIGHT = re.compile(r"(?<!\w)(?:постав(?:ь|ьте)|выстав(?:ь|ьте|и|ите))(?!\w)")
_KG = re.compile(r"\d+(?:[.,]\d+)?\s*(?:кг|кило\w*)(?!\w)")
# "на разгибания 4 подхода по 10–12": a leading "на <exercise>" (accusative: a log says "на брусьях", "на
# жиме"), sets or a rep range, small numbers only (a weight makes it a log).
_LEAD_NA = re.compile(r"^(?:(?:постав(?:ь|ьте)|сделай(?:те)?|пусть)\s+)?на\s+(?P<word>[a-zа-я]+)")
_SETS = re.compile(r"\d+\s*подход\w*|\d+\s*[-–]\s*\d+|\d+\s*[xх×*]\s*\d+")
_LOCATIVE = re.compile(r"(?:е|ах|ях)$")
SMALL_NUMBER = 30  # reps and sets; a bigger number in a "на …" message is a weight, so a log
# Never a program edit: something done or eaten, saved records ("удали запись за понедельник", "удали
# вчерашнюю тренировку" belong to saved_edits).
_PAST = re.compile(
    r"(?<!\w)(?:сделал\w*|выполнил\w*|(?:по|вы|от)?жал(?!уйст)\w*|получил\w*|был[аио]?|съел\w*|выпил\w*)(?!\w)"
)
# Food: a meal, grams or kcal, or a common food ("замени рис на гречку в понедельник" is about the diary).
_FOOD = re.compile(
    r"(?<!\w)(?:завтрак\w*|обед\w*|ужин\w*|перекус\w*|ккал|калори\w*|рис\w*|греч\w*|курин\w*|куриц\w*|"
    r"яйц\w*|яич\w*|овсян\w*|творог\w*|хлеб\w*|самс\w*|макарон\w*|картош\w*|картофел\w*|суп|супа|супу|супом|салат\w*|"
    r"мяс\w*|рыб\w*|кефир\w*|молок\w*|сыр|сыра|сыром|банан\w*|яблок\w*)(?!\w)|\d\s*(?:г|гр|грамм\w*)(?!\w)"
)
_RECORDS = re.compile(
    r"(?<!\w)(?:запис\w*|вчера\w*|позавчера\w*|сегодняшн\w*|за\s+(?:понедельник|вторник|среду|четверг|пятницу|"
    r"субботу|воскресенье))(?!\w)"
)
# Program start or switch ("перенеси старт программы", "начни программу заново") stays with settings.
_PROGRAM_START = re.compile(
    r"(?<!\w)(?:старт\w*|начал\w*|заново|сначала|с\s+понедельника|с\s+\d|переключ\w*|выбер\w*|запуст\w*|"
    r"начни(?:те)?|друг\w*|смени(?:те)?)(?!\w)"
)


def _branch_set(norm: str) -> bool:
    m = _LEAD_NA.match(norm)
    if m is None or _LOCATIVE.search(m["word"]) or not _SETS.search(norm) or _KG.search(norm):
        return False
    return all(int(n) <= SMALL_NUMBER for n in re.findall(r"\d+", norm))


def is_edit_command(text: str) -> bool:
    """Whether the text looks like a command to change the program or a day's weight (conservative). The
    message is only taken from the other paths when the model finds a valid action (handlers/chat_edit.py)."""
    if "?" in text:
        return False
    norm = normalize(text)
    if _PAST.search(norm) or _RECORDS.search(norm) or _FOOD.search(norm):
        return False
    structure = _STRUCTURE.search(_PER_DAY.sub(" ", norm)) or _DAY_NAME.search(norm)
    if cs.REMINDER.search(norm) or cs.REST.search(norm) or cs.PROGRAM.search(norm) and _PROGRAM_START.search(norm):
        return False
    if (cs.NORM.search(norm) or cs.SET_MACRO.search(norm)) and not structure:  # "белок 170 в день"
        return False
    verb = _EDIT_VERB.search(norm)
    instead = _INSTEAD.search(norm)
    # (a) an imperative and a word about the program's structure ("вместо" counts once: verb or structure)
    if verb and (structure or instead or (_MOVE_VERB.search(norm) and _WORKOUT.search(norm))):
        return True
    if instead and structure:
        return True
    # (b) a weight for a day: "поставь на сгибания 30 кг" (no reps: "поставь 80 кг на 8" is not one)
    # "поставь сегодня жим 85 кг" is a settings command (chat_settings owns weights for today).
    if _SET_WEIGHT.search(norm) and _KG.search(norm) and not cs.REPS.search(norm) and not cs.is_settings_request(text):
        return True
    # (c) a lighter week or day ("следующая неделя — делоад") waits for stage 2: today it is mostly wellbeing
    # ("сегодня облегчённо потренируюсь, болит плечо") and belongs to the parser.
    # (d) a prescription without a verb: "на разгибания 4 подхода по 10–12"
    return _branch_set(norm)


# ---- actions from the model ----

Scope = Literal["this_week", "from_this_week", "all_weeks"]
_NAME = Annotated[str, Field(min_length=1, max_length=pe.NAME_MAX)]


class DayRef(BaseModel):
    """A day as said: a weekday, "today"/"tomorrow", or a label ("руки", "спина"); all None: by the exercise."""

    model_config = ConfigDict(extra="ignore")

    weekday: int | None = Field(default=None, ge=1, le=7)
    focus: str | None = Field(default=None, max_length=100)
    when: Literal["today", "tomorrow"] | None = None

    @field_validator("when", mode="before")
    @classmethod
    def _when(cls, v: Any) -> Any:
        return v if v in ("today", "tomorrow") else None

    @field_validator("focus", mode="before")
    @classmethod
    def _focus(cls, v: Any) -> Any:
        return v.strip() or None if isinstance(v, str) else v


class _Action(BaseModel):
    model_config = ConfigDict(extra="ignore")

    @field_validator("scope", mode="before", check_fields=False)
    @classmethod
    def _scope(cls, v: Any) -> Any:
        return v if v in ("this_week", "from_this_week", "all_weeks") else None

    @field_validator("day", mode="before", check_fields=False)
    @classmethod
    def _day(cls, v: Any) -> Any:
        return {} if v is None else v


class ReplaceA(_Action):
    type: Literal["replace"]
    day: DayRef = Field(default_factory=DayRef)
    exercise: _NAME
    new_name: _NAME
    scope: Scope | None = None


class RemoveA(_Action):
    type: Literal["remove"]
    day: DayRef = Field(default_factory=DayRef)
    exercise: _NAME
    scope: Scope | None = None


_Sets = Annotated[int, Field(ge=1, le=pe.MAX_SETS)]
_Reps = Annotated[int, Field(ge=1, le=pe.MAX_REPS)]


class _Rx(_Action):
    sets: _Sets | None = None
    repsMin: _Reps | None = None
    repsMax: _Reps | None = None
    dropReps: Annotated[list[_Reps], Field(min_length=pe.DROPS_MIN, max_length=pe.DROPS_MAX)] | None = None

    @model_validator(mode="after")
    def _range(self) -> _Rx:
        if self.repsMin is None and self.repsMax is not None:
            self.repsMin = self.repsMax
        if self.repsMin is not None and self.repsMax is not None and self.repsMin > self.repsMax:
            raise ValueError("reps range reversed")
        return self


class AddA(_Rx):
    type: Literal["add"]
    day: DayRef = Field(default_factory=DayRef)
    name: _NAME
    after: str | None = Field(default=None, max_length=pe.NAME_MAX)
    scope: Scope | None = None


class PrescribeA(_Rx):
    type: Literal["prescribe"]
    day: DayRef = Field(default_factory=DayRef)
    exercise: _NAME
    intensity: pe.Intensity | None = None
    scope: Scope | None = None

    @field_validator("intensity", mode="before")
    @classmethod
    def _intensity(cls, v: Any) -> Any:
        return v if v in ("heavy", "medium", "light") else None

    @model_validator(mode="after")
    def _some(self) -> PrescribeA:
        if self.sets is None and self.repsMin is None and self.dropReps is None and self.intensity is None:
            raise ValueError("nothing to change")
        return self


class ReorderA(_Action):
    type: Literal["reorder"]
    day: DayRef = Field(default_factory=DayRef)
    order: list[_NAME] = Field(min_length=1, max_length=pe.MAX_ITEMS)


class WeightA(_Action):
    type: Literal["weight"]
    said: str = Field(min_length=1, max_length=100)
    exercise: str | None = Field(default=None, max_length=pe.NAME_MAX)
    weight_kg: float = Field(ge=overrides.WEIGHT_RANGE[0], le=overrides.WEIGHT_RANGE[1])
    date: dt.date | None = None  # "ГГГГ-ММ-ДД"; None: the nearest program day with the exercise


class ClarifyA(_Action):
    type: Literal["clarify"]
    question: str = Field(min_length=1, max_length=300)
    options: list[Annotated[str, Field(min_length=1, max_length=60)]] = Field(default_factory=list)

    @field_validator("options", mode="before")
    @classmethod
    def _options(cls, v: Any) -> Any:
        if not isinstance(v, list):
            return []
        return [o for o in v if isinstance(o, str) and 1 <= len(o.strip()) <= 60][:MAX_OPTIONS]


Action = Annotated[
    ReplaceA | RemoveA | AddA | PrescribeA | ReorderA | WeightA | ClarifyA, Field(discriminator="type")
]
_ACTION: TypeAdapter[Any] = TypeAdapter(Action)
MAX_ACTIONS = 10
MAX_OPTIONS = 6  # clarify buttons, two per row

INVALID = {
    "add": f"добавление: подходов от 1 до {pe.MAX_SETS}, повторов от 1 до {pe.MAX_REPS}",
    "prescribe": f"подходы и повторы: подходов от 1 до {pe.MAX_SETS}, повторов от 1 до {pe.MAX_REPS}",
    "weight": "вес: от 1 до 500 кг",
}


_RX_KEYS = ("sets", "repsMin", "repsMax", "dropReps", "intensity")


def parse_actions(data: Any) -> tuple[list[Any], list[str]]:
    """Valid actions and notes about the rejected ones (each action is validated on its own)."""
    items = data.get("actions") if isinstance(data, dict) else None
    valid: list[Any] = []
    notes: list[str] = []
    for item in (items if isinstance(items, list) else [])[:MAX_ACTIONS]:
        try:
            valid.append(_ACTION.validate_python(item))
        except (ValidationError, ValueError):
            kind = item.get("type") if isinstance(item, dict) else None
            if kind == "prescribe" and not any(item.get(k) is not None for k in _RX_KEYS):
                continue  # nothing to change: no note about limits
            if isinstance(kind, str) and kind in INVALID:
                notes.append(f"Не применю — {INVALID[kind]}.")
    return valid, list(dict.fromkeys(notes))


# ---- the program as the command sees it (plain data: no ORM objects outlive the session) ----

WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
WEEKDAYS_FULL = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")
_IN_WEEKDAY = ("в понедельник", "во вторник", "в среду", "в четверг", "в пятницу", "в субботу", "в воскресенье")


@dataclass(frozen=True)
class ItemView:
    id: int
    exercise_id: int
    name: str
    order: int
    sets: int
    reps_min: int | None
    reps_max: int | None
    drop_reps: tuple[int, ...] | None
    intensity: str | None


@dataclass
class DayView:
    week: int
    weekday: int  # 1 = Monday
    focus: str | None
    items: list[ItemView]

    @property
    def label(self) -> str:
        return WEEKDAYS[self.weekday - 1] + (f" «{self.focus}»" if self.focus else "")


@dataclass
class Snapshot:
    """What a command is resolved against; loaded once per message (and again for the preview)."""

    user_id: int
    today: date  # local day of the message
    slug: str
    name: str
    version: int
    template: bool  # the first edit makes the user's copy
    started_on: date
    weeks: dict[int, dict[int, DayView]]  # week number -> weekday -> day
    catalog: list[str]  # program exercises the user sees (baselines.catalog)
    known: list[str]  # every exercise name (logged ones too): a name outside it is a new exercise
    exercise_ids: dict[str, int]  # catalog name -> Exercise.id
    weights: dict[tuple[date, str], float] = field(default_factory=dict)  # overrides, today..+NEXT_DAY_SEARCH

    def position(self, d: date) -> Position:
        return program_position(self.started_on, len(self.weeks), d)

    @property
    def week(self) -> int:
        """The current program week (clamped to the program before its start and after its end)."""
        return self.position(self.today).week

    def day(self, week: int, weekday: int) -> DayView | None:
        return self.weeks.get(week, {}).get(weekday)

    def day_on(self, d: date) -> DayView | None:
        pos = self.position(d)
        if pos.not_started or pos.finished:
            return None
        return self.day(pos.week, pos.weekday)

    def all_weeks(self) -> list[int]:
        return sorted(self.weeks)


async def load_snapshot(session: AsyncSession, user: User, today: date) -> Snapshot:
    up: UserProgram = await active_program(session, user, today)
    program = await load_program(session, up.program_id)
    weeks: dict[int, dict[int, DayView]] = {}
    for w in program.weeks:
        days = weeks.setdefault(w.number, {})
        for d in w.days:
            items = [
                ItemView(
                    i.id, i.exercise_id, i.exercise.name, i.order, i.sets, i.reps_min, i.reps_max,
                    tuple(i.drop_reps) if i.drop_reps else None, i.intensity,
                )
                for i in sorted(d.items, key=lambda i: i.order)
            ]
            days[d.weekday] = DayView(w.number, d.weekday, d.focus, items)
    catalog = await baselines.catalog(session, user.id)
    ids = await overrides.exercise_ids(session, catalog)
    rows = await session.execute(
        select(WeightOverride.day, Exercise.name, WeightOverride.weight_kg)
        .join(Exercise, Exercise.id == WeightOverride.exercise_id)
        .where(
            WeightOverride.user_id == user.id,
            WeightOverride.day >= today,
            WeightOverride.day <= today + timedelta(days=NEXT_DAY_SEARCH),
        )
    )
    weights = {(day, name): float(kg) for day, name, kg in rows}
    return Snapshot(
        user.id, today, program.slug, program.name, program.version, program.owner_user_id is None,
        up.started_on, weeks, catalog, await exercise_catalog(session), ids, weights,
    )


def rx_text(sets: int, reps_min: int | None, reps_max: int | None, drop_reps: tuple[int, ...] | list[int] | None) -> str:
    """"4×8–10", "3×10", "3× дропсет 12-6-6"."""
    if drop_reps:
        return f"{sets}× дропсет {'-'.join(map(str, drop_reps))}"
    if reps_min is None:
        return f"{sets} подх."
    if reps_max is None or reps_max == reps_min:
        return f"{sets}×{reps_min}"
    return f"{sets}×{reps_min}–{reps_max}"


def _item_rx(i: ItemView) -> str:
    return rx_text(i.sets, i.reps_min, i.reps_max, i.drop_reps)


def _day_line(day: DayView) -> str:
    items = "; ".join(f"{i.order}. {i.name} {_item_rx(i)}" for i in day.items)
    return f" {day.label}: {items}"


def _same_week(a: dict[int, DayView], b: dict[int, DayView]) -> list[int]:
    """Weekdays that differ between two weeks (names and prescriptions)."""
    def sig(d: DayView | None) -> Any:
        return None if d is None else [(i.name, _item_rx(i)) for i in d.items]

    return [wd for wd in sorted(set(a) | set(b)) if sig(a.get(wd)) != sig(b.get(wd))]


def prompt_context(snap: Snapshot) -> str:
    today = snap.today
    pos = snap.position(today)
    kind = "шаблон" if snap.template else "моя копия"
    lines = [f"Сегодня {today.isoformat()}, {WEEKDAYS_FULL[today.weekday()]}."]
    state = " (ещё не началась)" if pos.not_started else " (пройдена)" if pos.finished else ""
    lines.append(f"Программа «{snap.name}» ({kind}), неделя {pos.week} из {len(snap.weeks)}{state}.")
    current = snap.weeks.get(pos.week, {})
    lines.append(f"Неделя {pos.week} (текущая):")
    for wd in sorted(current):
        mark = " (сегодня)" if wd == today.isoweekday() and not (pos.not_started or pos.finished) else ""
        lines.append(_day_line(current[wd]) + mark)
    if pos.week + 1 in snap.weeks:
        nxt = snap.weeks[pos.week + 1]
        diff = _same_week(current, nxt)
        if not diff:
            lines.append(f"Неделя {pos.week + 1} (следующая): как неделя {pos.week}.")
        else:
            lines.append(f"Неделя {pos.week + 1} (следующая), отличия:")
            lines += [_day_line(nxt[wd]) if wd in nxt else f" {WEEKDAYS[wd - 1]}: отдых" for wd in diff]
    todays = [f"{name} {overrides.kg(kg)} кг" for (d, name), kg in snap.weights.items() if d == today]
    if todays:
        lines.append("Веса на сегодня: " + ", ".join(todays) + ".")
    lines.append("Каталог упражнений: " + (", ".join(snap.catalog) or "пусто"))
    return "\n".join(lines)


# ---- resolution ----


def _close(a: str, b: str) -> bool:
    n = 0
    for x, y in zip(a, b, strict=False):
        if x != y:
            break
        n += 1
    return n >= max(3, min(len(a), len(b)) - 2)


def _words(text: str) -> list[str]:
    return re.findall(r"[a-zа-я0-9]{3,}", normalize(text))


def _words_in(said: str, name: str) -> bool:
    """Every word of `said` has a word of the same stem in `name` ("французский жим" in «французский жим в
    блоке из-за головы»)."""
    said_words, words = _words(said), _words(name)
    return bool(said_words) and all(any(_close(s, w) for w in words) for s in said_words)


def resolve_item(day: DayView, said: str) -> tuple[list[ItemView], bool]:
    """Items of `day` that `said` names and whether the match is exact (a catalog name, synonym or short
    gym name) rather than by words."""
    names = [i.name for i in day.items]
    name = overrides.match(said, None, names)
    if name is not None:
        return [i for i in day.items if i.name == name], True
    hits = [i for i in day.items if _words_in(said, i.name)]
    if not hits:
        hits = [i for i in day.items if baselines.shares_word(said, i.name)]
    return hits, False


# Day labels said in a command -> muscle groups (gymbot.services.plan.muscle_group) for days without a
# matching `focus`: "день рук" is the day with the most biceps and triceps exercises.
_FOCUS_GROUPS: list[tuple[str, frozenset[str]]] = [
    ("рук", frozenset({"biceps", "triceps"})),
    ("бицеп", frozenset({"biceps"})),
    ("трицеп", frozenset({"triceps"})),
    ("плеч", frozenset({"shoulders"})),
    ("дельт", frozenset({"shoulders"})),
    ("груд", frozenset({"chest"})),
    ("спин", frozenset({"back"})),
    ("ног", frozenset({"legs"})),
    ("пресс", frozenset({"abs"})),
]


def _focus_match(said: str, focus: str | None) -> bool:
    if not focus:
        return False
    words = _words(focus)
    return any(any(_close(s, w) for w in words) for s in _words(said))


def _by_composition(said: str, days: list[DayView]) -> list[DayView]:
    groups: set[str] = set()
    for w in _words(said):
        for stem, gs in _FOCUS_GROUPS:
            if w.startswith(stem):
                groups |= gs
    if not groups:
        return []
    counts = [(sum(muscle_group(i.name) in groups for i in d.items), d) for d in days]
    best = max((n for n, _ in counts), default=0)
    return [d for n, d in counts if n == best] if best else []


def resolve_day(snap: Snapshot, ref: DayRef, exercise: str | None = None) -> list[DayView]:
    """Candidate days for `ref` (one = resolved). today/tomorrow by the date, a weekday in the current week,
    a label by `focus` and then by the exercises; several candidates narrowed by `exercise`."""
    if ref.when is not None:
        d = snap.today + timedelta(days=1 if ref.when == "tomorrow" else 0)
        day = snap.day_on(d)
        return [day] if day is not None and day.items else []
    week = snap.week
    if ref.weekday is not None:
        day = snap.day(week, ref.weekday)
        return [day] if day is not None and day.items else []
    candidates = [d for _, d in sorted(snap.weeks.get(week, {}).items()) if d.items]
    if ref.focus:
        candidates = [d for d in candidates if _focus_match(ref.focus, d.focus)] or _by_composition(
            ref.focus, candidates
        )
    if exercise and len(candidates) > 1:
        found = [(d, *resolve_item(d, exercise)) for d in candidates]
        exact = [d for d, hits, sure in found if sure and hits]
        loose = [d for d, hits, _ in found if hits]
        candidates = exact or loose or candidates
    return candidates


def scope_weeks(scope: str | None, week: int, weeks: list[int]) -> list[int]:
    """Port of miniapp programEdit.scopeWeeks: this week, from it to the end, or all weeks."""
    if scope == "all_weeks":
        return sorted(weeks)
    if scope == "from_this_week":
        return [n for n in sorted(weeks) if n >= week]
    return [week]


def format_weeks(weeks: list[int]) -> str:
    """Port of miniapp programEdit.formatWeeks: [1, 2, 3, 5] -> "1–3, 5"."""
    ns = sorted(set(weeks))
    parts: list[str] = []
    i = 0
    while i < len(ns):
        j = i
        while j + 1 < len(ns) and ns[j + 1] == ns[j] + 1:
            j += 1
        parts += [f"{ns[i]}–{ns[j]}"] if j - i >= 2 else [str(n) for n in ns[i : j + 1]]
        i = j + 1
    return ", ".join(parts)


def scope_words(weeks: list[int], week: int, all_weeks: list[int]) -> str:
    if weeks == [week]:
        return f"только неделя {week}"
    if weeks == sorted(all_weeks):
        return "все недели"
    return f"недели {format_weeks(weeks)}"


# ---- the plan shown in the preview ----


@dataclass(frozen=True)
class Pick:
    """What a clarify button fixes: action `action`'s day (week, weekday) or item (ProgramItem.id)."""

    action: int
    what: Literal["day", "item", "after"]
    value: Any


@dataclass
class Clarify:
    question: str
    options: list[str]  # button labels
    picks: list[Pick | None]  # None: the model's own question, answered by asking the model again


@dataclass
class WeightChange:
    exercise_id: int
    name: str
    day: date
    kg: float


@dataclass
class EditPlan:
    """Resolved changes. `lines` is the preview (and the summary after "Применить"), `notes` what is
    skipped; `ops` are program_editor PATCH ops against `slug` at `version`."""

    slug: str
    version: int
    name: str
    forks: bool  # the active program is a template: the first edit makes the user's copy
    ops: list[dict[str, Any]] = field(default_factory=list)
    op_labels: list[str] = field(default_factory=list)  # per op, for the skipped weeks
    weights: list[WeightChange] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    clarify: Clarify | None = None
    copy_name: str | None = None  # set by `preview` when a copy would be made

    def ready(self) -> bool:
        return self.clarify is None and bool(self.ops or self.weights)

    def live_topics(self) -> list[Any]:
        return ["program", "plan", "state"] if self.ops else ["state"]

    def drop_ops(self, note: str) -> None:
        """The program part cannot be applied (the dry run failed): keep only the weights."""
        self.lines = [ln for ln in self.lines if ln.startswith("Вес ")]
        self.ops, self.op_labels = [], []
        self.notes.insert(0, note)


DEFAULT_SETS, DEFAULT_REPS = 3, (8, 12)
NEW_EXERCISE = "новое упражнение, истории нет"
SKIP_REASON = {pe.NO_ITEM: "там другое упражнение", pe.NO_DAY: "нет этого дня"}


def _canonical(snap: Snapshot, name: str) -> tuple[str, bool]:
    """The catalog spelling of an exercise name and whether it is known (logged or in a program)."""
    clean = " ".join(name.split())
    found = overrides.match(clean, None, snap.catalog) or baselines.match_exercise(clean, snap.known)
    if found is not None:
        return found, True
    return clean, any(normalize(k) == normalize(clean) for k in snap.known)


class _Need(Exception):
    """Resolution stops: a question for the user or a note (the action is skipped)."""

    def __init__(self, clarify: Clarify | None = None, note: str | None = None) -> None:
        super().__init__(note or (clarify.question if clarify else ""))
        self.clarify = clarify
        self.note = note


def _pick_day(
    snap: Snapshot, n: int, ref: DayRef, exercise: str | None, picks: dict[tuple[int, str], Any], about: str | None
) -> DayView:
    if (fixed := picks.get((n, "day"))) is not None:
        day = snap.day(*fixed)
        if day is None:
            raise _Need(note="Этого дня в программе больше нет, повтори команду.")
        return day
    days = resolve_day(snap, ref, exercise)
    if len(days) == 1:
        return days[0]
    if not days and (ref.when is not None or ref.weekday is not None):
        if ref.when is not None:
            what = "сегодня" if ref.when == "today" else "завтра"
        else:
            what = _IN_WEEKDAY[(ref.weekday or 1) - 1]
        raise _Need(note=f"{what.capitalize()} по программе тренировки нет.")
    options = days or [d for _, d in sorted(snap.weeks.get(snap.week, {}).items()) if d.items]
    if not options:
        raise _Need(note="На этой неделе в программе нет тренировок.")
    options = options[:MAX_OPTIONS]
    what = f" для «{about}»" if about else ""
    raise _Need(
        Clarify(f"Какой день{what}?", [d.label for d in options],
                [Pick(n, "day", (d.week, d.weekday)) for d in options])
    )


def _pick_item(
    day: DayView, n: int, said: str, picks: dict[tuple[int, str], Any], what: Literal["item", "after"] = "item"
) -> ItemView:
    if (fixed := picks.get((n, what))) is not None:
        item = next((i for i in day.items if i.id == fixed), None)
        if item is None:
            raise _Need(note="Этого упражнения в дне больше нет, повтори команду.")
        return item
    hits, _ = resolve_item(day, said)
    if len(hits) == 1:
        return hits[0]
    if not hits:
        names = ", ".join(i.name for i in day.items)
        raise _Need(note=f"В дне {day.label} нет «{said}». Там: {names}.")
    hits = hits[:MAX_OPTIONS]
    raise _Need(
        Clarify(f"Какое упражнение {day.label}: «{said}»?", [i.name for i in hits],
                [Pick(n, what, i.id) for i in hits])
    )


def _op(kind: str, day: DayView, weeks: list[int], **fields: Any) -> dict[str, Any]:
    return {"op": kind, "week": day.week, "weekday": day.weekday, "weeks": weeks, **fields}


def _weight(snap: Snapshot, a: WeightA, plan: EditPlan) -> None:
    name = overrides.match(a.said, a.exercise, snap.catalog)
    if name is None:  # "сгибания с супинацией": the one catalog name with all the words said
        hits = [c for c in snap.catalog if _words_in(a.said, c)]
        name = hits[0] if len(hits) == 1 else None
    if name is None or name not in snap.exercise_ids:
        plan.notes.append(f"Нет в программе, пропускаю: «{a.said}».")
        return
    last = snap.today + timedelta(days=NEXT_DAY_SEARCH)
    if a.date is not None:
        if not snap.today <= a.date <= last:
            plan.notes.append(f"Вес на {a.date:%d.%m} не ставлю: можно с сегодня на {NEXT_DAY_SEARCH} дней вперёд.")
            return
        day = a.date
    else:
        found = None
        for k in range(NEXT_DAY_SEARCH + 1):
            d = snap.today + timedelta(days=k)
            pd = snap.day_on(d)
            if pd is not None and any(i.name == name for i in pd.items):
                found = d
                break
        if found is None:
            plan.notes.append(f"«{name}» нет в программе на ближайшие {NEXT_DAY_SEARCH} дней, вес не ставлю.")
            return
        day = found
    kg = round(a.weight_kg, 2)
    plan.weights = [w for w in plan.weights if (w.name, w.day) != (name, day)]  # the last one said wins
    plan.lines = [ln for ln in plan.lines if not ln.startswith(f"Вес на {_date(day)}: {name} ")]
    before = snap.weights.get((day, name))
    if before == kg:
        plan.notes.append(f"{name}: на {_date(day)} уже {overrides.kg(kg)} кг.")
        return
    plan.weights.append(WeightChange(snap.exercise_ids[name], name, day, kg))
    was = f" (было {overrides.kg(before)})" if before is not None else ""
    plan.lines.append(f"Вес на {_date(day)}: {name} {overrides.kg(kg)} кг{was}")


def _date(d: date) -> str:
    return f"{WEEKDAYS[d.weekday()]} {d:%d.%m}"


def compile_ops(snap: Snapshot, actions: list[Any], picks: dict[tuple[int, str], Any] | None = None) -> EditPlan:
    """Actions -> PATCH ops, weights, preview lines and notes; the first ambiguity stops with `clarify`
    (answered by a Pick, then compiled again from the start)."""
    picks = picks or {}
    plan = EditPlan(snap.slug, snap.version, snap.name, snap.template)
    all_weeks = snap.all_weeks()
    temp = 0
    for n, a in enumerate(actions):
        if isinstance(a, ClarifyA):
            if a.options:
                plan.clarify = Clarify(a.question, list(a.options), [None] * len(a.options))
            else:
                plan.notes.append(a.question)
            return plan
        if isinstance(a, WeightA):
            _weight(snap, a, plan)
            continue
        try:
            said = getattr(a, "exercise", None)
            about = said or getattr(a, "name", None) or (a.order[0] if isinstance(a, ReorderA) else None)
            day = _pick_day(snap, n, a.day, said, picks, about)
            scope = getattr(a, "scope", None) or ("all_weeks" if isinstance(a, ReplaceA) else "this_week")
            weeks = scope_weeks(scope, day.week, all_weeks)
            where = scope_words(weeks, day.week, all_weeks)
            if isinstance(a, ReplaceA):
                item = _pick_item(day, n, a.exercise, picks)
                new, known = _canonical(snap, a.new_name)
                if normalize(new) == normalize(item.name):
                    plan.notes.append(f"В дне {day.label} уже стоит {item.name}.")
                    continue
                if any(normalize(i.name) == normalize(new) for i in day.items):
                    plan.notes.append(f"«{new}» уже есть в дне {day.label}.")
                    continue
                plan.ops.append(_op("replace", day, weeks, itemId=item.id, name=new))
                plan.op_labels.append(f"{item.name} → {new}")
                tail = "" if known else f"; {NEW_EXERCISE}"
                plan.lines.append(f"{day.label}: {item.name} → {new} ({where}{tail})")
            elif isinstance(a, RemoveA):
                item = _pick_item(day, n, a.exercise, picks)
                plan.ops.append(_op("remove", day, weeks, itemId=item.id))
                plan.op_labels.append(f"убрать {item.name}")
                plan.lines.append(f"{day.label}: убрать {item.name} ({where})")
            elif isinstance(a, AddA):
                new, known = _canonical(snap, a.name)
                if any(normalize(i.name) == normalize(new) for i in day.items):
                    plan.notes.append(f"«{new}» уже есть в дне {day.label}.")
                    continue
                defaults = []
                sets = a.sets
                if sets is None:
                    sets = DEFAULT_SETS
                    defaults.append("подходы")
                drops = list(a.dropReps) if a.dropReps else None
                reps_min, reps_max = a.repsMin, a.repsMax
                if drops is None and reps_min is None:
                    reps_min, reps_max = DEFAULT_REPS
                    defaults.append("повторы")
                rx = rx_text(sets, reps_min, reps_max, drops)
                if defaults:
                    rx += " (по умолчанию)" if len(defaults) == 2 else f" ({defaults[0]} по умолчанию)"
                position, place = len(day.items) + 1, "в конец"
                if a.after:
                    after = _pick_item(day, n, a.after, picks, "after")
                    position, place = after.order + 1, f"после «{after.name}»"
                temp += 1
                fields: dict[str, Any] = {"tempId": f"c{temp}", "name": new, "position": position, "sets": sets}
                if drops is not None:
                    fields["dropReps"] = drops
                else:
                    fields |= {"repsMin": reps_min, "repsMax": reps_max if reps_max is not None else reps_min}
                plan.ops.append(_op("add", day, weeks, **fields))
                plan.op_labels.append(f"добавить {new}")
                tail = "" if known else f"; {NEW_EXERCISE}"
                plan.lines.append(f"{day.label}: добавить {new} {rx} {place} ({where}{tail})")
            elif isinstance(a, PrescribeA):
                item = _pick_item(day, n, a.exercise, picks)
                sets = a.sets or item.sets
                fields = {"itemId": item.id, "sets": sets}
                if a.dropReps:
                    fields["dropReps"] = list(a.dropReps)
                    new_rx = rx_text(sets, None, None, a.dropReps)
                elif a.repsMin is not None:
                    reps_max = a.repsMax if a.repsMax is not None else a.repsMin
                    fields |= {"repsMin": a.repsMin, "repsMax": reps_max}
                    new_rx = rx_text(sets, a.repsMin, reps_max, None)
                elif item.drop_reps:
                    fields["dropReps"] = list(item.drop_reps)
                    new_rx = rx_text(sets, None, None, item.drop_reps)
                else:
                    fields |= {"repsMin": item.reps_min, "repsMax": item.reps_max}
                    new_rx = rx_text(sets, item.reps_min, item.reps_max, None)
                if a.intensity is not None:
                    fields["intensity"] = a.intensity
                old_rx = _item_rx(item)
                if new_rx == old_rx and (a.intensity is None or a.intensity == item.intensity):
                    plan.notes.append(f"{item.name} в дне {day.label} уже {old_rx}.")
                    continue
                plan.ops.append(_op("prescribe", day, weeks, **fields))
                plan.op_labels.append(item.name)
                plan.lines.append(f"{day.label}: {item.name} {old_rx} → {new_rx} ({where})")
            elif isinstance(a, ReorderA):
                weeks = [day.week]
                ordered: list[ItemView] = []
                for said_name in a.order:
                    hits, _ = resolve_item(day, said_name)
                    hits = [h for h in hits if h not in ordered]
                    if len(hits) != 1:
                        raise _Need(note=f"Не понял порядок: «{said_name}» в дне {day.label}.")
                    ordered.append(hits[0])
                ordered += [i for i in day.items if i not in ordered]
                if [i.id for i in ordered] == [i.id for i in day.items]:
                    plan.notes.append(f"В дне {day.label} порядок уже такой.")
                    continue
                plan.ops.append(_op("reorder", day, weeks, itemIds=[i.id for i in ordered]))
                plan.op_labels.append("порядок")
                names = ", ".join(f"{k}. {i.name}" for k, i in enumerate(ordered, 1))
                plan.lines.append(f"{day.label}: порядок {names} (только неделя {day.week})")
        except _Need as need:
            if need.clarify is not None:
                plan.clarify = need.clarify
                return plan
            if need.note:
                plan.notes.append(need.note)
    return plan


def skip_lines(plan: EditPlan, results: list[pe.OpResult]) -> list[str]:
    """«французский жим лёжа → жим гантелей: недели 3–5 пропущу — там другое упражнение»."""
    out = []
    for r in results:
        by_reason: dict[str, list[int]] = {}
        for s in r.skipped:
            by_reason.setdefault(s.reason, []).append(s.week)
        for reason, weeks in by_reason.items():
            label = plan.op_labels[r.op] if r.op < len(plan.op_labels) else "правка"
            word = "неделю" if len(weeks) == 1 else "недели"
            out.append(f"{label}: {word} {format_weeks(weeks)} пропущу — {SKIP_REASON.get(reason, reason)}")
    return out


async def preview(session: AsyncSession, user: User, up: UserProgram, plan: EditPlan) -> None:
    """Dry-run the ops (everything rolled back): skipped weeks go into `plan.lines`, the copy's name into
    `plan.copy_name`. Raises EditError / Conflict like edit_program; `user` and `up` are expired after it."""
    if not plan.ops:
        return
    try:
        outcome = await pe.edit_program(session, user, up, plan.slug, plan.version, plan.ops, dry_run=True)
        skipped = skip_lines(plan, outcome.results)
        copy_name = outcome.program.name if outcome.switched_from else None
    finally:
        await session.rollback()
    plan.lines += skipped
    plan.copy_name = copy_name


async def apply(
    session: AsyncSession, user: User, up: UserProgram, plan: EditPlan, today: date, raw_text: str
) -> list[str]:
    """Write the plan (the caller commits once): the program ops through edit_program with the version of
    the preview, then the weights. Raises EditError / Conflict (nothing is written then). Returns notes."""
    notes: list[str] = []
    if plan.ops:
        await pe.edit_program(session, user, up, plan.slug, plan.version, plan.ops)
        log.info(
            "program edit from chat: user %s, %s v%s, ops %s, text %r",
            user.id, plan.slug, plan.version, plan.ops, raw_text,
        )
    for w in plan.weights:
        if w.day < today:
            notes.append(f"Вес на {w.day:%d.%m} не выставлен ({w.name}): тот день уже прошёл, повтори команду.")
            plan.lines = [ln for ln in plan.lines if not ln.startswith(f"Вес на {_date(w.day)}: {w.name} ")]
            continue
        await overrides.upsert(session, user.id, w.exercise_id, w.day, w.kg)
    if plan.weights and not plan.ops:
        log.info("weights from chat: user %s, %s, text %r", user.id, [(w.name, w.day, w.kg) for w in plan.weights], raw_text)
    return notes
