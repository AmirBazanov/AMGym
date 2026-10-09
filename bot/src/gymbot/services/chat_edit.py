"""Program edits from the chat: "убери французский жим из дня рук", "поставь на сгибания 30 кг", "поменяй
местами руки и спину", "перенеси тренировку на завтра", "следующая неделя — делоад", "сегодня облегчённо, −20 %", "в пятницу без ног".

Plan: docs/plans/2026-10-09-chat-program-control.md (stages 1–3). Flow (handlers/chat_edit.py):
1. `is_edit_command` — a conservative regex, checked in process_text before saved_edits (a question, a past
   tense verb, saved-record words and settings vocabulary never route here).
2. One `complete_json(purpose="edit")` call with EDIT_SYSTEM_PROMPT (gymbot.llm.prompts_edit) and
   `prompt_context` (today, the current program week by days, the next week, weights, catalog). The model
   answers with intentions by name (`parse_actions`, each validated on its own), never with ids or weeks.
3. `compile_ops` resolves days (weekday, today/tomorrow, focus or the exercises of the day), exercises (only
   within that day) and scopes (weeks) into the program editor's PATCH ops (gymbot.services.program_editor)
   and weights for a day (WeightOverride). A swap or a move of days is one `move_day` op (the editor swaps
   when the target weekday is taken); a deload is a start date for gymbot.services.deload; a lighter day
   ("−20 %", "на подход меньше", "без ног") is a one-day adjustment for gymbot.services.day_adjustments, an
   input of the day plan (merged into the day's existing one; "верни как было" clears it). Ambiguity is a
   `Clarify` with buttons, never a guess.
4. `preview` dry-runs `edit_program` (rolled back) for the skipped weeks; "✅ Применить" -> `apply` runs the
   same `edit_program` with the version seen in the preview, then the overrides, the day adjustments and the
   deload, in one transaction.

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

from gymbot.db.models import Exercise, User, UserProgram, WeightOverride, Workout
from gymbot.services import baselines, deload, overrides
from gymbot.services import chat_settings as cs
from gymbot.services import day_adjustments as dayadj
from gymbot.services import program_editor as pe
from gymbot.services.advice import NEXT_DAY_SEARCH
from gymbot.services.plan import LIGHT_FACTOR, adjust_item, muscle_group
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
    r"(?<!\w)(?:сделал\w*|выполнил\w*|(?:по|вы|от)?жал(?!уйст)\w*|получил\w*|был[аио]?|съел\w*|выпил\w*|"
    r"тренировал\w*|потрен\w*|занимал\w*|позанимал\w*)(?!\w)"
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
# A deload week: "следующая неделя — делоад", "давай на этой неделе разгрузку". Not "облегчённо" (stage 3).
_DELOAD = re.compile(r"(?<!\w)(?:делоад\w*|дилоад\w*|разгрузк\w*|разгрузочн\w*)(?!\w)")
_DELOAD_WHEN = re.compile(r"(?<!\w)(?:недел\w*|с\s+понедельник\w*)(?!\w)")
# How the user feels ("на этой неделе делоад, спал плохо", "перенеси тренировку на завтра, болит спина"): a
# wellbeing record for the parser, never a program edit.
_WELLBEING = re.compile(
    r"(?<!\w)(?:бол(?:ит|ят|ел\w*|ело|ь|и|ью)|болезн\w*|спал\w*|сплю|выспал\w*|недосып\w*|сон|сна|сном|"
    r"устал\w*|усталост\w*|энерги\w*|сил|сил[аыу]|разбит\w*|самочувств\w*|простыл\w*|заболел\w*|"
    r"ноет|ныть|тянет|потянул\w*|травм\w*|простуд\w*|температур\w*|чувству\w*|плохо|стал\w*|дыш\w*|пульс\w*)(?!\w)"
)
# A diet's unloading days ("на этой неделе разгрузочные дни по еде") are food, not a deload week.
_DELOAD_FOOD = re.compile(
    r"(?<!\w)(?:ед[аеуы]|едой|питани\w*|кефир\w*|яблок\w*|углевод\w*|калори\w*|диет\w*|голод\w*)(?!\w)"
)
# A lighter day ("сегодня облегчённо, −20 %", "сегодня полегче", "на завтра на подход меньше", "в пятницу без
# ног"): one day of the plan (gymbot.services.day_adjustments), without a day it is today.
_LIGHTER = re.compile(r"(?<!\w)(?:облегч\w*|полегч\w*|легче|лайтов\w*)(?!\w)")
_PERCENT = re.compile(r"\d+(?:[.,]\d+)?\s*(?:%|процент\w*)")
_MINUS = re.compile(r"(?:[−–-]|(?<!\w)минус\s+)\s*\d")
_FEWER_SETS = re.compile(
    r"(?<!\w)(?:на\s+(?:\d+|один|одн\w+|два|две|три)\s+)?подход\w*\s+меньше|"
    r"(?<!\w)меньше\s+(?:на\s+(?:\d+|один|одн\w+|два|две|три)\s+)?подход\w*|(?<!\w)минус\s+(?:\d+\s+)?подход\w*"
)
_GROUP_WORD = r"(?:ног|рук|спин|груд|плеч|бицепс|трицепс|пресс|дельт)\w*"
_WITHOUT_GROUP = re.compile(rf"(?<!\w)без\s+{_GROUP_WORD}(?!\w)")
# "без жима", "без приседа", "без становой тяги": an exercise word, never "без зала", "без тренировки"
_WITHOUT_EXERCISE = re.compile(
    r"(?<!\w)без\s+(?:\w+\s+)?(?:жим|присед|тяг|сгибан|разгибан|подтягиван|отжиман|мах|выпад|планк|становой|"
    r"румынск|французск|отведени|разведени|гиперэкстенз|скручивани|кроссовер|пулловер)\w*"
)
# A bare "полегче" needs a day, a percent or an imperative: "дышится легче" is how the user feels
_LIGHT_IMPERATIVE = re.compile(
    r"(?<!\w)(?:сделай(?:те)?|давай(?:те)?|пусть|постав(?:ь|ьте)|план\w*|тренировк\w*|вес|веса|весов|нагрузк\w*|"
    r"подход\w*)(?!\w)"
)
_WEIGHT_WORD = re.compile(r"(?<!\w)(?:вес|веса|весов|весам|рабоч\w*)(?!\w)")
_WHEN = re.compile(rf"(?<!\w)(?:сегодня|завтра|послезавтра|{_WEEKDAY})(?!\w)")
# "как облегчить завтрашнюю тренировку" is a question for the advice, "жим шёл легче" a log
_ASKS = re.compile(
    r"^(?:а\s+)?(?:как|что|почему|зачем|можно|сколько|какой|какая|какие|каким|когда|стоит|нужно|надо|если)(?!\w)|"
    r"(?<!\w)(?:можно|стоит|надо|нужно)(?!\w)"
)
_PAST_MORE = re.compile(r"(?<!\w)(?:шел|шла|шло|шли|пошл\w*|далс\w*|дала\w*|казал\w*|показал\w*)(?!\w)")
_LOG_NUMBERS = re.compile(r"\d\s*(?:кг\s*)?[xх×*]\s*\d|\d\s*(?:кг\s*)?(?:на|по)\s+\d")
MAX_SETS_NUMBER = 10  # a bigger number outside a percent is a weight or reps: a log
# "верни как было сегодня", "отмени поправку на пятницу": the day's adjustment goes (before _PAST: "было")
_UNDO_ADJUST = re.compile(
    r"(?<!\w)(?:отмени(?:те)?|убери(?:те)?|сними(?:те)?)\s+(?:\w+\s+)?(?:поправк\w*|облегчени\w*)(?!\w)"
)
# A bare "верни как было" needs a day and nothing about settings or saved records ("верни как было вчера")
_AS_IT_WAS = re.compile(r"(?<!\w)верни(?:те)?\s+(?:все\s+|всё\s+)?как\s+было(?!\w)")
_ADJUST_WORD = re.compile(r"(?<!\w)(?:поправк\w*|облегч\w*)")
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


def _branch_lighter(norm: str) -> bool:
    """(e) a lighter day: "сегодня облегчённо, −20 %", "на 10 % легче", "на завтра на подход меньше", "в
    пятницу без ног", "без ног" (not a question, a log or food)."""
    if _ASKS.search(norm) or _PAST_MORE.search(norm) or _KG.search(norm) or _LOG_NUMBERS.search(norm):
        return False
    if _DELOAD_FOOD.search(norm):
        return False
    if any(int(n) > MAX_SETS_NUMBER for n in re.findall(r"\d+", _PERCENT.sub(" ", norm))):
        return False
    lighter = _LIGHTER.search(norm)
    percent = _PERCENT.search(norm)
    if percent and not (lighter or _MINUS.search(norm) or _WEIGHT_WORD.search(norm)):
        return False  # "сегодня 20 % жира" is a measurement
    if _FEWER_SETS.search(norm) or _WITHOUT_GROUP.search(norm) or _WITHOUT_EXERCISE.search(norm):
        return True
    if percent:
        return True
    return bool(lighter and explicit_day_or_imperative(norm))


def explicit_day_or_imperative(norm: str) -> bool:
    """A day ("сегодня", "в пятницу"), a percent or an imperative ("сделай", "давай", "тренировка"): a bare
    "легче" without one is how the user feels, never a command."""
    return bool(_WHEN.search(norm) or _PERCENT.search(norm) or _LIGHT_IMPERATIVE.search(norm))


def _undo_adjust(text: str, norm: str) -> bool:
    if _FOOD.search(norm):
        return False
    if _UNDO_ADJUST.search(norm):
        return True
    if not _AS_IT_WAS.search(norm) or _RECORDS.search(norm):
        return False
    if _ADJUST_WORD.search(norm):
        return True
    return bool(_WHEN.search(norm)) and not cs.REMINDER.search(norm) and not cs.is_settings_request(text)


def is_edit_command(text: str) -> bool:
    """Whether the text looks like a command to change the program or a day's weight (conservative). The
    message is only taken from the other paths when the model finds a valid action (handlers/chat_edit.py)."""
    if "?" in text:
        return False
    norm = normalize(text)
    if _undo_adjust(text, norm):
        return True
    if _PAST.search(norm) or _RECORDS.search(norm) or _FOOD.search(norm):
        return False
    structure = _STRUCTURE.search(_PER_DAY.sub(" ", norm)) or _DAY_NAME.search(norm)
    if cs.REMINDER.search(norm) or cs.REST.search(norm) or cs.PROGRAM.search(norm) and _PROGRAM_START.search(norm):
        return False
    if (cs.NORM.search(norm) or cs.SET_MACRO.search(norm)) and not structure:  # "белок 170 в день"
        return False
    if _WELLBEING.search(norm):
        return False
    deload_word = _DELOAD.search(norm)
    if deload_word and _DELOAD_FOOD.search(norm):
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
    # (c) a deload week without a verb: "следующая неделя — делоад", "давай на этой неделе делоад" (how the
    # user feels and a diet are excluded above)
    if deload_word and _DELOAD_WHEN.search(norm):
        return True
    # (d) a prescription without a verb: "на разгибания 4 подхода по 10–12"
    if _branch_set(norm):
        return True
    # (e) a lighter day (how the user feels is excluded above: it is wellbeing, the plan follows it itself)
    return _branch_lighter(norm)


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

    @field_validator("day", "a", "b", "src", "dst", mode="before", check_fields=False)
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


class SwapDaysA(_Action):
    """Two days of a week change places ("поменяй местами руки и спину", "сделай сегодня ноги вместо рук")."""

    type: Literal["swap_days"]
    a: DayRef = Field(default_factory=DayRef)
    b: DayRef = Field(default_factory=DayRef)
    scope: Scope | None = None


class MoveDayA(_Action):
    """A day goes to another weekday; a day already there takes its place ("перенеси тренировку на завтра")."""

    type: Literal["move_day"]
    src: DayRef = Field(default_factory=DayRef)
    dst: DayRef = Field(default_factory=DayRef)
    scope: Scope | None = None


class DeloadA(_Action):
    """A deload week (gymbot.services.deload): this week = from today, next week = from next Monday."""

    type: Literal["deload"]
    week: Literal["this_week", "next_week"] | None = None
    start: dt.date | None = None  # "ГГГГ-ММ-ДД" when a day is named ("с 20 октября")

    @field_validator("week", mode="before")
    @classmethod
    def _week(cls, v: Any) -> Any:
        return v if v in ("this_week", "next_week") else None


class AdjustDayA(_Action):
    """One day lighter ("сегодня облегчённо, −20 %", "на завтра на подход меньше", "в пятницу без ног"); all
    of weight_factor, sets_delta and skip empty ("сегодня полегче") is the plan's light day."""

    type: Literal["adjust_day"]
    day: DayRef = Field(default_factory=DayRef)
    weight_factor: float | None = None  # 0.8 for "−20 %": only from a percent said
    sets_delta: int | None = None  # -1 for "на подход меньше"
    skip: list[Annotated[str, Field(min_length=1, max_length=pe.NAME_MAX)]] | None = Field(default=None, max_length=10)
    note: str | None = Field(default=None, max_length=dayadj.NOTE_MAX)

    @field_validator("weight_factor")
    @classmethod
    def _factor(cls, v: float | None) -> float | None:
        if v is None or v == 1:
            return None
        lo, hi = dayadj.FACTOR_RANGE
        if not lo <= v <= hi:
            raise ValueError("only lighter")
        return round(v, 2)

    @field_validator("sets_delta")
    @classmethod
    def _sets(cls, v: int | None) -> int | None:
        if v is None or v == 0:
            return None
        lo, hi = dayadj.SETS_DELTA_RANGE
        if not lo <= v <= hi:
            raise ValueError("only fewer sets")
        return v

    @field_validator("skip", mode="before")
    @classmethod
    def _skip(cls, v: Any) -> Any:
        if isinstance(v, str):
            v = [v]
        if not isinstance(v, list):
            return None
        return [x.strip() for x in v if isinstance(x, str) and x.strip()] or None


class ClearDayA(_Action):
    """The day's adjustment goes ("верни как было сегодня", "отмени поправку на пятницу")."""

    type: Literal["clear_day"]
    day: DayRef = Field(default_factory=DayRef)


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
    ReplaceA | RemoveA | AddA | PrescribeA | ReorderA | WeightA | SwapDaysA | MoveDayA | DeloadA | AdjustDayA
    | ClearDayA | ClarifyA,
    Field(discriminator="type"),
]
_ACTION: TypeAdapter[Any] = TypeAdapter(Action)
MAX_ACTIONS = 10
MAX_OPTIONS = 6  # clarify buttons, two per row

INVALID = {
    "add": f"добавление: подходов от 1 до {pe.MAX_SETS}, повторов от 1 до {pe.MAX_REPS}",
    "prescribe": f"подходы и повторы: подходов от 1 до {pe.MAX_SETS}, повторов от 1 до {pe.MAX_REPS}",
    "weight": "вес: от 1 до 500 кг",
    "adjust_day": "поправка дня: только легче — вес от −1 до −70 %, подходов меньше на 1–5",
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
    id: int  # ProgramDay.id
    week: int
    weekday: int  # 1 = Monday
    focus: str | None
    items: list[ItemView]

    @property
    def label(self) -> str:
        return WEEKDAYS[self.weekday - 1] + (f" «{self.focus}»" if self.focus else "")

    @property
    def title(self) -> str:
        """«Руки и плечи», or the first exercises for a day without a label."""
        if self.focus:
            return f"«{self.focus}»"
        names = [i.name for i in self.items[:2]]
        return "«" + ", ".join(names) + ("…" if len(self.items) > 2 else "") + "»"


def date_of(started_on: date, week: int, weekday: int) -> date | None:
    """The calendar day of (week, weekday): program weeks are 7-day blocks from `started_on`, which is not
    always a Monday, so the weekday is looked up inside the block."""
    first = started_on + timedelta(days=7 * (week - 1))
    return next((d for k in range(7) if (d := first + timedelta(days=k)).isoweekday() == weekday), None)


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
    done: set[int] = field(default_factory=set)  # ProgramDay ids with a workout in the current cycle (✓)
    deload: tuple[date, date] | None = None  # a running or scheduled deload: first and last day
    # Dates of the current program week with a workout logged in the chat (no program day: its ✓ is by date)
    done_dates: set[date] = field(default_factory=set)
    adjustments: dict[date, dayadj.DayAdjust] = field(default_factory=dict)  # today..+NEXT_DAY_SEARCH

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

    def date_of(self, week: int, weekday: int) -> date | None:
        return date_of(self.started_on, week, weekday)


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
            days[d.weekday] = DayView(d.id, w.number, d.weekday, d.focus, items)
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
    day_ids = [d.id for w in program.weeks for d in w.days]
    done = set(
        (
            await session.scalars(
                select(Workout.program_day_id).where(
                    Workout.user_id == user.id,
                    Workout.program_day_id.in_(day_ids),
                    Workout.performed_on >= up.started_on,
                )
            )
        ).all()
    )
    pos = program_position(up.started_on, len(program.weeks), today)
    week_start = up.started_on + timedelta(days=7 * (pos.week - 1))
    done_dates = set(
        (
            await session.scalars(
                select(Workout.performed_on).where(
                    Workout.user_id == user.id,
                    Workout.program_day_id.is_(None),
                    Workout.performed_on >= week_start,
                    Workout.performed_on < week_start + timedelta(days=7),
                )
            )
        ).all()
    )
    st = await deload.get_state(session, user.id)
    running = None
    if deload.pending(st, today):
        assert st is not None and st.started_on is not None and st.until is not None
        running = (st.started_on, st.until)
    adjustments = await dayadj.between(session, user.id, today, today + timedelta(days=NEXT_DAY_SEARCH))
    return Snapshot(
        user.id, today, program.slug, program.name, program.version, program.owner_user_id is None,
        up.started_on, weeks, catalog, await exercise_catalog(session), ids, weights,
        {d for d in done if d is not None}, running, done_dates, adjustments,
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
    if snap.deload is not None:
        first, last = snap.deload
        state = "идёт" if first <= today else "запланирована"
        lines.append(f"Разгрузочная неделя {state}: {deload.span(first, last)}.")
    if snap.adjustments:
        adjs = [f"{_when_words(snap, d)} ({_date(d)}): {dayadj.describe(a)}" for d, a in sorted(snap.adjustments.items())]
        lines.append("Поправки дня: " + "; ".join(adjs) + ".")
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
    """What a clarify button fixes: action `action`'s day (week, weekday; "day2" for the second day of a swap
    or move) or item (ProgramItem.id)."""

    action: int
    what: Literal["day", "day2", "item", "after"]
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
class DeloadChange:
    first: date
    last: date
    replaces: tuple[date, date] | None = None  # the running or scheduled deload the preview saw


@dataclass
class AdjustChange:
    """A day's adjustment to store (`adj`) or to clear (`adj` None); `before` is what the preview saw."""

    day: date
    adj: dayadj.DayAdjust | None
    before: dayadj.DayAdjust | None
    line: str
    default: bool = False  # nothing was said but "полегче": the plan's light day


DELOAD_LINE = "Разгрузочная неделя "  # preview lines that stay when the program part cannot be applied
WEIGHT_LINE = "Вес "


@dataclass
class EditPlan:
    """Resolved changes. `lines` is the preview (and the summary after "Применить"), `notes` what is
    skipped; `ops` are program_editor PATCH ops against `slug` at `version`."""

    slug: str
    version: int
    name: str
    forks: bool  # the active program is a template: the first edit makes the user's copy
    today: date  # the day the plan was compiled for (the dry run's current program week)
    ops: list[dict[str, Any]] = field(default_factory=list)
    op_labels: list[str] = field(default_factory=list)  # per op, for the skipped weeks
    weights: list[WeightChange] = field(default_factory=list)
    deload: DeloadChange | None = None
    adjusts: list[AdjustChange] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    move_notes: list[str] = field(default_factory=list)  # warnings about moved days, shown after `notes`
    # A swap or a move is one op, kept apart and appended after every other op (`finish`): the other ops are
    # resolved against the days as they are now, so they must run before the days change places.
    move_op: dict[str, Any] | None = None
    move_label: str = ""
    clarify: Clarify | None = None
    copy_name: str | None = None  # set by `preview` when a copy would be made

    def finish(self) -> EditPlan:
        """Put the swap or move after every other op (called once, at the end of compile_ops)."""
        if self.move_op is not None:
            self.ops.append(self.move_op)
            self.op_labels.append(self.move_label)
        return self

    def ready(self) -> bool:
        return self.clarify is None and bool(self.ops or self.move_op or self.weights or self.deload or self.adjusts)

    def live_topics(self) -> list[Any]:
        topics: list[Any] = []
        if self.ops or self.move_op:
            topics += ["program", "plan", "state"]
        if self.weights and "state" not in topics:
            topics.append("state")
        if (self.deload is not None or self.adjusts) and "plan" not in topics:
            topics.append("plan")
        return topics

    def drop_ops(self, note: str) -> None:
        """The program part cannot be applied (the dry run failed): keep only the weights and the deload."""
        keep = {c.line for c in self.adjusts}
        self.lines = [ln for ln in self.lines if ln.startswith((WEIGHT_LINE, DELOAD_LINE)) or ln in keep]
        self.ops, self.op_labels, self.move_notes = [], [], []
        self.move_op = None
        self.notes.insert(0, note)


DEFAULT_SETS, DEFAULT_REPS = 3, (8, 12)
NEW_EXERCISE = "новое упражнение, истории нет"
SKIP_REASON = {pe.NO_ITEM: "там другое упражнение", pe.NO_DAY: "нет этого дня", pe.PAST_WEEK: "неделя уже прошла"}
DELOAD_AHEAD = 28  # days: the latest start of a deload set from the chat


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
    snap: Snapshot,
    n: int,
    ref: DayRef,
    exercise: str | None,
    picks: dict[tuple[int, str], Any],
    about: str | None,
    key: Literal["day", "day2"] = "day",
) -> DayView:
    if (fixed := picks.get((n, key))) is not None:
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
                [Pick(n, key, (d.week, d.weekday)) for d in options])
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


def _deload(snap: Snapshot, a: DeloadA, plan: EditPlan) -> None:
    today = snap.today
    if a.start is not None:
        first = a.start
    elif a.week == "next_week":
        first = today + timedelta(days=7 - today.weekday())  # next Monday
    else:
        first = today
    if first < today:
        plan.notes.append(f"Разгрузку с {_date(first)} не начать: этот день уже прошёл.")
        return
    if first > today + timedelta(days=DELOAD_AHEAD):
        plan.notes.append(f"Разгрузку можно запланировать не дальше чем на {DELOAD_AHEAD} дней вперёд.")
        return
    replaced = ""
    if snap.deload is not None:
        start, end = snap.deload
        if start <= today:
            plan.notes.append(f"Разгрузочная неделя уже идёт: {deload.span(start, end)}. Отменить: /deload")
            return
        if start == first:
            plan.notes.append(f"Разгрузочная неделя уже запланирована: {deload.span(start, end)}.")
            return
        replaced = f"; вместо запланированной {deload.span(start, end)}"
    last = first + timedelta(days=deload.DELOAD_DAYS - 1)
    pct = round((1 - deload.WEIGHT_FACTOR) * 100)
    plan.deload = DeloadChange(first, last, snap.deload)
    plan.lines = [ln for ln in plan.lines if not ln.startswith(DELOAD_LINE)]  # the last one said wins
    plan.lines.append(
        f"{DELOAD_LINE}{deload.span(first, last)} ({deload.DELOAD_DAYS} дней): веса −{pct} %, "
        f"подходов на треть меньше{replaced}"
    )


def _when_words(snap: Snapshot, d: date) -> str:
    """«сегодня», «завтра», «в пятницу»."""
    if d == snap.today:
        return "сегодня"
    if d == snap.today + timedelta(days=1):
        return "завтра"
    return _IN_WEEKDAY[d.weekday()]


ADJUST_DAYS = 7  # a lighter day is set for a day of the coming week


def _adjust_date(snap: Snapshot, ref: DayRef) -> date:
    """The calendar day of a one-day adjustment: today/tomorrow, the next such weekday (today included), the
    first training day of the coming week with that label, or today when no day is said."""
    today = snap.today
    if ref.when is not None:
        return today + timedelta(days=1 if ref.when == "tomorrow" else 0)
    if ref.weekday is not None:
        return today + timedelta(days=(ref.weekday - today.isoweekday()) % 7)
    if ref.focus:
        days = [(d, pd) for k in range(ADJUST_DAYS) if (pd := snap.day_on(d := today + timedelta(days=k))) and pd.items]
        views = [pd for _, pd in days]
        hits = [pd for pd in views if _focus_match(ref.focus, pd.focus)] or _by_composition(ref.focus, views)
        found = next((d for d, pd in days if any(pd is h for h in hits)), None)
        if found is None:
            raise _Need(note=f"Не нашёл на ближайшей неделе день «{ref.focus}».")
        return found
    return today


def _said_groups(said: str) -> set[str]:
    """Muscle groups when `said` is only group words ("ноги", "руки", "грудь и плечи"), else empty."""
    words = [w for w in _words(said) if w not in ("без", "все", "всё")]
    groups: set[str] = set()
    for w in words:
        hit = next((gs for stem, gs in _FOCUS_GROUPS if w.startswith(stem)), None)
        if hit is None:
            return set()
        groups |= hit
    return groups


def _skips(day: DayView, said: list[str], plan: EditPlan, when: str) -> tuple[set[str], set[str]]:
    """Exercise names and muscle groups of `day` that `said` names; notes for what is not there."""
    exercises: set[str] = set()
    groups: set[str] = set()
    for s in said:
        hits, sure = resolve_item(day, s)
        if hits and sure:
            exercises |= {h.name for h in hits}
            continue
        if gs := _said_groups(s):
            if not any(muscle_group(i.name) in gs for i in day.items):
                plan.notes.append(f"{when.capitalize()} нет упражнений на {s}.")
                continue
            groups |= gs
            continue
        if hits:  # "без сгибаний": every curl of the day
            exercises |= {h.name for h in hits}
            continue
        plan.notes.append(f"В дне {day.label} нет «{s}». Там: {', '.join(i.name for i in day.items)}.")
    return exercises, groups


def _adjust(snap: Snapshot, a: AdjustDayA, plan: EditPlan) -> None:
    d = _adjust_date(snap, a.day)
    when = _when_words(snap, d)
    day = snap.day_on(d)
    if day is None or not day.items:
        raise _Need(note=f"{when.capitalize()} ({_date(d)}) по программе тренировки нет — облегчать нечего.")
    pending = next((c for c in plan.adjusts if c.day == d), None)
    before = snap.adjustments.get(d)
    base = pending.adj if pending is not None and pending.adj is not None else before
    exercises, groups = _skips(day, a.skip or [], plan, when)
    new = dayadj.DayAdjust(a.weight_factor, a.sets_delta, sorted(exercises), sorted(groups), a.note)
    default = ""
    if new.empty():
        if a.skip:  # nothing named was found: the notes say so
            return
        if base is not None and (base.weight_factor is not None or base.sets_delta is not None):
            plan.notes.append(f"На {_date(d)} уже: {dayadj.describe(base)}.")
            return
        new = dayadj.DayAdjust(LIGHT_FACTOR, -1, note=a.note)  # "сегодня полегче": the plan's light day
        default = " (по умолчанию, как лёгкий день)"
    result = base.merged(new) if base is not None else new
    if result == before:
        plan.notes.append(f"На {_date(d)} уже: {dayadj.describe(result)}.")
        return
    skipped = [i for i in day.items if adjust_item(result, i.name, i.sets, i.sets, 1.0)[2]]
    if skipped and len(skipped) == len(day.items):
        plan.notes.append(f"{when.capitalize()} не останется ни одного упражнения — по сути день отдыха.")
    was = f" (было: {dayadj.describe(before)})" if before is not None else ""
    line = f"{when.capitalize()} ({_date(d)}): {dayadj.describe(result)}{default}{was}"
    if pending is not None:
        plan.adjusts.remove(pending)
        plan.lines = [ln for ln in plan.lines if ln != pending.line]
    plan.adjusts.append(AdjustChange(d, result, before, line, bool(default)))
    plan.lines.append(line)


def _clear_day(snap: Snapshot, a: ClearDayA, plan: EditPlan) -> None:
    d = _adjust_date(snap, a.day)
    when = _when_words(snap, d)
    before = snap.adjustments.get(d)
    pending = next((c for c in plan.adjusts if c.day == d), None)
    if pending is not None:
        plan.adjusts.remove(pending)
        plan.lines = [ln for ln in plan.lines if ln != pending.line]
    if before is None:
        if pending is None:
            plan.notes.append(f"На {when} ({_date(d)}) поправок нет, план и так по программе.")
        return
    line = f"{when.capitalize()} ({_date(d)}): убрать поправку ({dayadj.describe(before)}), план как по программе"
    plan.adjusts.append(AdjustChange(d, None, before, line))
    plan.lines.append(line)


def _slot(
    snap: Snapshot,
    n: int,
    ref: DayRef,
    picks: dict[tuple[int, str], Any],
    key: Literal["day", "day2"],
    week: int | None,
    about: str | None,
) -> tuple[int, int, DayView | None]:
    """(week, weekday, the day there or None for a rest day) for one side of a swap or a move. A weekday is
    taken in `week` (the other side's week when that one is today/tomorrow) or the current week; a label
    must name a training day."""
    if picks.get((n, key)) is None and (ref.when is not None or ref.weekday is not None):
        if ref.when is not None:
            d = snap.today + timedelta(days=1 if ref.when == "tomorrow" else 0)
            pos = snap.position(d)
            if pos.not_started or pos.finished:
                state = "ещё не началась" if pos.not_started else "уже пройдена"
                raise _Need(note=f"Программа {state}: переносить нечего.")
            w, wd = pos.week, pos.weekday
        else:
            w, wd = week or snap.week, ref.weekday or 1
        day = snap.day(w, wd)
        return w, wd, day if day is not None and day.items else None
    day = _pick_day(snap, n, ref, None, picks, about, key)
    return day.week, day.weekday, day


def _when_week(snap: Snapshot, ref: DayRef) -> int | None:
    if ref.when is None:
        return None
    return snap.position(snap.today + timedelta(days=1 if ref.when == "tomorrow" else 0)).week


def _move(snap: Snapshot, n: int, a: SwapDaysA | MoveDayA, plan: EditPlan, picks: dict[tuple[int, str], Any]) -> None:
    """swap_days and move_day -> one move_day op (the editor swaps when the target weekday is taken)."""
    if plan.move_op is not None:
        raise _Need(note="За раз переношу или меняю местами одну пару дней: следующий перенос — отдельной командой.")
    first, second = (a.a, a.b) if isinstance(a, SwapDaysA) else (a.src, a.dst)
    week = _when_week(snap, first) or _when_week(snap, second)
    wa, da, day_a = _slot(snap, n, first, picks, "day", week, None)
    wb, db, day_b = _slot(snap, n, second, picks, "day2", week or wa, None)
    if wa != wb:
        raise _Need(note="Дни в разных неделях программы: переношу и меняю местами только внутри одной недели.")
    if da == db:
        raise _Need(note="Это один и тот же день, менять нечего.")
    date_a, date_b = snap.date_of(wa, da), snap.date_of(wa, db)

    def when(d: date | None, weekday: int) -> str:
        return _date(d) if d is not None else WEEKDAYS[weekday - 1]

    if day_a is None and day_b is None:
        raise _Need(note=f"{when(date_a, da).capitalize()} и {when(date_b, db)} по программе тренировок нет.")
    if isinstance(a, MoveDayA) and day_a is None:
        raise _Need(note=f"{when(date_a, da).capitalize()} по программе тренировки нет, переносить нечего.")
    if day_a is None:  # "сделай сегодня ноги" on a rest day: the other day comes here
        assert day_b is not None
        src, dst, src_date, dst_date, other = day_b, da, date_b, date_a, None
    else:
        src, dst, src_date, dst_date, other = day_a, db, date_a, date_b, day_b
    all_weeks = snap.all_weeks()
    weeks = scope_weeks(a.scope or "this_week", src.week, all_weeks)
    where = scope_words(weeks, src.week, all_weeks)
    plan.move_op = _op("move_day", src, weeks, toWeekday=dst)
    src_when, dst_when = when(src_date, src.weekday), when(dst_date, dst)
    if other is None:
        plan.move_label = f"перенос {WEEKDAYS[src.weekday - 1]} → {WEEKDAYS[dst - 1]}"
        plan.lines.append(f"{src.title}: {src_when} → {dst_when} ({where})")
    else:
        plan.move_label = f"обмен {WEEKDAYS[src.weekday - 1]} ⇄ {WEEKDAYS[dst - 1]}"
        plan.lines.append(f"{src_when}: {other.title} вместо {src.title} ({where})")
        plan.lines.append(f"{dst_when}: {src.title} вместо {other.title} ({where})")
        if isinstance(a, MoveDayA):
            plan.move_notes.append(f"{dst_when.capitalize()} уже занято ({other.title}) — поменяю дни местами.")
    for day, to, to_weekday in ((src, dst_date, dst), (other, src_date, src.weekday)):
        if day is None:
            continue
        if day.id in snap.done:
            plan.move_notes.append(
                f"{day.title} уже отмечен ✓ — отметка переедет вместе с днём на {when(to, to_weekday)}."
            )
        elif to is not None and to < snap.today:
            plan.move_notes.append(f"{day.title} окажется на {_date(to)} — этот день уже прошёл.")
    for d in (src_date, dst_date):  # a chat workout has no program day: the Mini App places its ✓ by date
        if d is not None and d in snap.done_dates and src.week == snap.week:
            plan.move_notes.append(
                f"{_date(d).capitalize()} уже отмечен сделанным по записи из чата — отметка останется на дате."
            )


def compile_ops(snap: Snapshot, actions: list[Any], picks: dict[tuple[int, str], Any] | None = None) -> EditPlan:
    """Actions -> PATCH ops, weights, preview lines and notes; the first ambiguity stops with `clarify`
    (answered by a Pick, then compiled again from the start)."""
    picks = picks or {}
    plan = EditPlan(snap.slug, snap.version, snap.name, snap.template, snap.today)
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
        if isinstance(a, DeloadA):
            _deload(snap, a, plan)
            continue
        try:
            if isinstance(a, AdjustDayA):
                _adjust(snap, a, plan)
                continue
            if isinstance(a, ClearDayA):
                _clear_day(snap, a, plan)
                continue
            if isinstance(a, SwapDaysA | MoveDayA):
                _move(snap, n, a, plan, picks)
                continue
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
    return plan.finish()


def only_guesses(plan: EditPlan, actions: list[Any], text: str) -> bool:
    """Whether the plan holds nothing the user clearly asked for: a lighter day made of defaults only ("легче"
    with no day, percent or imperative is how the user feels) or a clear of a day with no adjustment. Then the
    text goes on as usual (the parser, the wellbeing log)."""
    if plan.clarify is not None:
        return False
    if not plan.ready():
        return bool(actions) and all(isinstance(a, ClearDayA) for a in actions)
    if plan.ops or plan.move_op or plan.weights or plan.deload:
        return False
    return all(c.default for c in plan.adjusts) and not explicit_day_or_imperative(normalize(text))


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
        outcome = await pe.edit_program(
            session, user, up, plan.slug, plan.version, plan.ops, today=plan.today, dry_run=True
        )
        skipped = skip_lines(plan, outcome.results)
        copy_name = outcome.program.name if outcome.switched_from else None
    finally:
        await session.rollback()
    plan.lines += skipped
    plan.copy_name = copy_name


async def _move_overrides(
    session: AsyncSession, user_id: int, started_on: date, ops: list[dict[str, Any]], outcome: pe.Outcome
) -> None:
    """Day weights (WeightOverride, by date) follow the days a move_day moved: the moved day's exercises from
    its old date to the new one and, in a swap, the other day's back. A weight already set for that exercise
    on the target date stays (the moved one is dropped)."""
    program = outcome.program
    moves: list[tuple[date, date, set[int]]] = []  # from, to, exercises of the day that moved
    for n, op in enumerate(ops):
        if op.get("op") != "move_day":
            continue
        result = next((r for r in outcome.results if r.op == n), None)
        for w in result.weeks if result is not None else []:
            a, b = date_of(started_on, w, op["weekday"]), date_of(started_on, w, op["toWeekday"])
            if a is None or b is None:
                continue
            for pw in program.weeks:
                if pw.number != w:
                    continue
                for d in pw.days:
                    if d.weekday == op["toWeekday"]:  # the moved day, now on toWeekday
                        moves.append((a, b, {i.exercise_id for i in d.items}))
                    elif d.weekday == op["weekday"]:  # the day it swapped with
                        moves.append((b, a, {i.exercise_id for i in d.items}))
    if not moves:
        return
    days = {d for a, b, _ in moves for d in (a, b)}
    rows = (
        await session.scalars(
            select(WeightOverride).where(WeightOverride.user_id == user_id, WeightOverride.day.in_(days))
        )
    ).all()
    moving = [(r, to) for r in rows for a, to, exs in moves if r.day == a and r.exercise_id in exs]
    staying = {(r.exercise_id, r.day) for r in rows} - {(r.exercise_id, r.day) for r, _ in moving}
    new = [(r.exercise_id, to, r.weight_kg) for r, to in moving if (r.exercise_id, to) not in staying]
    for r, _ in moving:
        await session.delete(r)
    await session.flush()  # the unique (user, exercise, day) must be free before the rows come back
    for exercise_id, to, kg in new:
        session.add(WeightOverride(user_id=user_id, exercise_id=exercise_id, day=to, weight_kg=kg))
    await session.flush()


async def apply(
    session: AsyncSession,
    user: User,
    up: UserProgram,
    plan: EditPlan,
    today: date,
    raw_text: str,
    now: dt.datetime | None = None,
) -> list[str]:
    """Write the plan (the caller commits once): the program ops through edit_program with the version of
    the preview, then the weights, then the deload. Raises EditError / Conflict (nothing is written then).
    Returns notes."""
    notes: list[str] = []
    started_on = up.started_on
    outcome = None
    if plan.ops:
        outcome = await pe.edit_program(session, user, up, plan.slug, plan.version, plan.ops, today=today)
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
    if outcome is not None:
        await _move_overrides(session, user.id, started_on, plan.ops, outcome)
    for c in list(plan.adjusts):
        now_there = await dayadj.get(session, user.id, c.day)
        if c.day < today or now_there != c.before:
            why = "тот день уже прошёл" if c.day < today else "поправка изменилась после предпросмотра"
            notes.append(f"Поправку на {_date(c.day)} не трогаю: {why}, повтори команду.")
            plan.lines = [ln for ln in plan.lines if ln != c.line]
            plan.adjusts.remove(c)
            continue
        if c.adj is None:
            await dayadj.clear(session, user.id, c.day)
        else:
            await dayadj.upsert(session, user.id, c.day, c.adj, raw_text, now=now)
        log.info("day adjustment from chat: user %s, %s, %s, text %r", user.id, c.day, c.adj, raw_text)
    if plan.deload is not None:
        st = await deload.get_state(session, user.id)
        now_there = (st.started_on, st.until) if st is not None and deload.pending(st, today) else None
        if now_there != plan.deload.replaces:  # started or cancelled elsewhere (/deload) since the preview
            notes.append("Разгрузка изменилась после предпросмотра (/deload), её не трогаю — повтори команду.")
            plan.lines = [ln for ln in plan.lines if not ln.startswith(DELOAD_LINE)]
            plan.deload = None
            return notes
        until = await deload.start(
            session, user.id, today, now or dt.datetime.now(dt.UTC), start_on=plan.deload.first
        )
        if plan.deload.first < today:  # the preview was made before midnight: it starts today instead
            notes.append(f"Разгрузка начнётся сегодня, до {_date(until)}.")
        log.info("deload from chat: user %s, %s..%s, text %r", user.id, plan.deload.first, until, raw_text)
    return notes
