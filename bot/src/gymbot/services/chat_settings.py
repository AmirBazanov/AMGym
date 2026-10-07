"""Mini App settings from the chat: "норма 2800 ккал", "напоминай про креатин в 9", "поставь сегодня жим 85".

Flow (handlers/chat_settings.py):
1. `is_settings_request` — a conservative regex on the text, checked BEFORE the parser (whose prompt is at
   its size limit). Only imperative settings vocabulary routes here; sets and food stay with the parser.
2. One small `complete_json` call with SETTINGS_SYSTEM_PROMPT and `prompt_context` (today, programs,
   reminders, exercise catalog) -> {"actions": [...]}; `parse_actions` validates each action on its own
   (out-of-range values are dropped with a note, never fixed up).
3. `resolve` turns valid actions into a `Plan`: catalog exercises, the user's reminders, the program and
   its Monday start, old values for the preview. No valid action at all -> the parser gets the text.
4. The user presses "✅ Применить" -> `apply` writes the whole plan in one transaction.

Weekdays: actions use 1=Mon..7=Sun (what people say), the reminders table 0=Mon..6=Sun with one row per
weekday, so "по будням" makes five rows. Times are local (TIMEZONE), weights in kg.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, TypeAdapter, ValidationError, field_validator, model_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from gymbot.db.models import Program, ProgramWeek, Reminder, User, UserProgram
from gymbot.services import baselines, overrides
from gymbot.services import nutrition as nut
from gymbot.services import reminders as rem
from gymbot.services.programs import monday_of, normalize, program_position
from gymbot.services.users import active_program, set_program

# ---- routing ----

_NUM_WORD = (
    r"од(?:ин|на)|два|две|три|четыре|пять|шесть|семь|восемь|девять|десять|\w+надцать|двадцать|тридцать|"
    r"сорок|пятьдесят|шестьдесят|семьдесят|восемьдесят|девяносто|сто|двести|триста|четыреста|\w+сот|"
    r"полтора|полторы"
)
_NUMBER = rf"(?:\d|(?<!\w)(?:{_NUM_WORD})(?!\w))"
_IMPERATIVE = (
    r"(?<!\w)(?:постав(?:ь|ьте)|выстав(?:ь|ьте|и|ите)|установи(?:те)?|задай(?:те)?|измени(?:те)?|"
    r"поменяй(?:те)?|смени(?:те)?|сделай(?:те)?|подними(?:те)?|снизь(?:те)?|понизь(?:те)?|увеличь(?:те)?|"
    r"уменьши(?:те)?|обнови(?:те)?)(?!\w)"
)
_MACRO = r"бел(?:ок|ка|ку|ком|ки|ков)|жир(?:ы|ов|ам|а)?|углевод\w*|угли|ккал|калори\w*|кбжу"
_MACRO_RX = re.compile(rf"(?:{_MACRO})")
_FILLER = {"и", "г", "гр", "грамм", "граммов", "грамма", "в", "на", "а"}
# A bare "жиры 80 углеводы 350" is a food label as often as a norm: it routes only with one of these.
_GOAL = r"норм(?:а|у|ы)|цел(?:ь|и)|в\s+день|на\s+день|в\s+сутки"

# Norm: "норма 2800 ккал", "цель белок 170", "белок 170 в день"; the goal word and a macro or a number.
NORM = re.compile(
    rf"(?<!\w)(?:{_GOAL})(?!\w).*?(?:\d|(?<!\w)(?:{_MACRO})(?!\w))|(?<!\w)(?:{_MACRO})(?!\w).*?(?<!\w)(?:{_GOAL})(?!\w)",
    re.DOTALL,
)
SET_MACRO = re.compile(rf"{_IMPERATIVE}.*?(?<!\w)(?:{_MACRO})(?!\w).*?{_NUMBER}", re.DOTALL)
# Reminders: only the imperative ("напомни", "напоминай"), or the noun with a set-verb ("убери напоминание");
# "бот напомнил", "напоминалка сработала" are not commands.
_REMINDER_VERB = (
    r"(?<!\w)(?:постав(?:ь|ьте)|созда(?:й|йте)|добав(?:ь|ьте)|сдела(?:й|йте)|убер(?:и|ите)|удал(?:и|ите)|"
    r"выключ(?:и|ите)|отключ(?:и|ите)|включ(?:и|ите)|верн(?:и|ите)|поменя(?:й|йте)|перенес(?:и|ите)|"
    r"измени(?:те)?|сним(?:и|ите)|отмени(?:те)?)(?!\w)"
)
REMINDER = re.compile(
    rf"(?<!\w)(?:напомни(?:те)?|напоминай(?:те)?)(?!\w)|"
    rf"{_REMINDER_VERB}.*?(?<!\w)напоминани(?:е|я|й|ями?)(?!\w)|(?<!\w)напоминани(?:е|я)(?!\w).*?{_REMINDER_VERB}",
    re.DOTALL,
)
# "напомни, что я ел вчера", "напомни мой вес": a question about the diary, not a reminder.
REMIND_QUESTION = re.compile(
    r"(?<!\w)напомни(?:те)?\W*(?:мне\W+)?(?:что|какой|какая|какие|какое|каким|сколько|мою|мой|мои|мое|где|когда|"
    r"как|чем|кто|зачем|почему)(?!\w)"
)
# Rest timer: "таймер отдыха 2 минуты", or a set-verb with "таймер" / "отдых".
REST = re.compile(
    rf"(?<!\w)таймер\w*\s+отдых\w*|{_IMPERATIVE}.*?(?<!\w)(?:таймер\w*|отдых\w*)|(?<!\w)таймер\w*.*?{_IMPERATIVE}",
    re.DOTALL,
)
# Program: an imperative start verb within three words of "программ…" ("начни программу заново",
# "перенеси старт программы"), or "перенеси старт".
_START = (
    r"(?:начн(?:и|ите)|запуст(?:и|ите)|переключ(?:и|ите|ись|итесь)|смен(?:и|ите)|поменя(?:й|йте)|"
    r"перенес(?:и|ите)|сбрось(?:те)?|обнули(?:те)?|постав(?:ь|ьте)|выбер(?:и|ите))"
)
_NEAR = r"(?:\W+\w+){0,3}?\W+"  # up to three words in between, not a long sentence
PROGRAM = re.compile(
    rf"(?<!\w){_START}{_NEAR}программ\w*|(?<!\w)программ\w*{_NEAR}{_START}(?!\w)|"
    rf"(?<!\w)перенес(?:и|ите)\s+(?:старт|начало)"
)
# A weight for today needs a set-verb, "сегодня" and a number, and no reps: "сегодня жим 85 на 8" is a log.
OVERRIDE_VERB = re.compile(
    r"(?<!\w)(?:постав(?:ь|ьте)|выстав(?:ь|ьте|и|ите)|установи(?:те)?|сделай(?:те)?\s+вес\w*|"
    r"работа(?:ю|ем)\s+(?:сегодня\s+)?с)(?!\w)"
)
TODAY = re.compile(r"(?<!\w)сегодня(?!\w)")
# Rep slang: "пятерка" = a set of five.
_REP_SLANG = r"(?:двойк|тройк|троек|четверк|пятерк|пятерок|шестерк|семерк|восьмерк|девятк|десятк)\w*"
REPS = re.compile(
    rf"\d\s*(?:кг\s*)?[xх×*]\s*\d|"
    rf"(?:\d|(?<!\w)(?:{_NUM_WORD})(?!\w))\s*(?:кг\s*|кило\w*\s*)?(?:на|по)\s+{_NUMBER}|"
    rf"(?<!\w)(?:повтор\w*|подход\w*|раз|раза|сет|сета|сетов|сеты|сделал\w*|выполнил\w*|{_REP_SLANG})(?!\w)"
)
# Stricter, for dropping the parser's workout as a mere echo of "поставь сегодня жим 85" (handlers/
# chat_settings.Staged.fake_workout): also "на 8", "получилось 6", any past "жал".
DROP_REPS = re.compile(
    REPS.pattern + rf"|(?<!\w)на\s+{_NUMBER}|(?<!\w)получил\w*\s+{_NUMBER}|(?<!\w)(?:по|вы|от)?жал(?!уйст)\w*"
)
def _only_macros(norm: str) -> bool:
    """'жиры 80 углеводы 350 в день': macro names, numbers, filler and a goal word, at least two macros."""
    if not re.search(rf"(?<!\w)(?:{_GOAL})(?!\w)", norm):
        return False
    stripped = re.sub(rf"(?<!\w)(?:{_GOAL})(?!\w)", " ", norm)
    words = re.findall(r"[a-zа-я]+|\d+(?:[.,]\d+)?", stripped)
    macros = [w for w in words if _MACRO_RX.fullmatch(w)]
    numbers = [w for w in words if w[0].isdigit() or re.fullmatch(_NUM_WORD, w)]
    rest = [w for w in words if w not in macros and w not in numbers and w not in _FILLER]
    return len(macros) >= 2 and bool(numbers) and not rest


def is_settings_request(text: str) -> bool:
    """Whether the text looks like a settings command (conservative; a question never is). Routing does not
    take the message away from the parser: see handlers/chat_settings.py."""
    if "?" in text:
        return False
    norm = normalize(text)
    if NORM.search(norm) or SET_MACRO.search(norm) or _only_macros(norm):
        return True
    if REMINDER.search(norm) and not REMIND_QUESTION.search(norm) or REST.search(norm) or PROGRAM.search(norm):
        return True
    return bool(
        OVERRIDE_VERB.search(norm) and TODAY.search(norm) and re.search(_NUMBER, norm) and not REPS.search(norm)
    )


# ---- actions from the model ----

Kind = Literal["text", "nutrition", "advice", "checkin"]


class TargetsAction(BaseModel):
    type: Literal["targets"]
    kcal: int | None = Field(default=None, ge=800, le=6000)
    # Daily norms, not a portion: "белок 20 жиры 8" is a food label, never a target.
    protein: int | None = Field(default=None, ge=40, le=500)
    fat: int | None = Field(default=None, ge=20, le=500)
    carbs: int | None = Field(default=None, ge=50, le=500)

    @model_validator(mode="after")
    def _some(self) -> TargetsAction:
        if self.changes() == {}:
            raise ValueError("no target given")
        return self

    def changes(self) -> dict[str, int]:
        return {k: v for k in ("kcal", "protein", "fat", "carbs") if (v := getattr(self, k)) is not None}


class ReminderAddAction(BaseModel):
    type: Literal["reminder_add"]
    time: str = Field(pattern=rem.TIME_PATTERN)
    kind: Kind
    text: str | None = Field(default=None, max_length=rem.TEXT_MAX)
    weekdays: list[Annotated[int, Field(ge=1, le=7)]] = Field(default_factory=list)

    @field_validator("time", mode="before")
    @classmethod
    def _pad(cls, v: Any) -> Any:
        return f"0{v}" if isinstance(v, str) and re.fullmatch(r"[0-9]:[0-5][0-9]", v) else v

    @model_validator(mode="after")
    def _text(self) -> ReminderAddAction:
        self.text = rem.reminder_text(self.kind, self.text)  # ValueError for kind=text without text
        self.weekdays = sorted(set(self.weekdays))
        if len(self.weekdays) == 7:
            self.weekdays = []
        return self


class ReminderChangeAction(BaseModel):
    type: Literal["reminder_delete", "reminder_disable", "reminder_enable"]
    ids: list[int] = Field(default_factory=list)
    kind: Kind | None = None
    about: str | None = Field(default=None, max_length=200)

    @field_validator("kind", mode="before")
    @classmethod
    def _kind(cls, v: Any) -> Any:
        return v if v in rem.KINDS else None  # an unknown kind is just not a filter


class OneOffAction(BaseModel):
    """A reminder for one date ("напомни завтра в 9"): not supported, answered with ONE_OFF."""

    type: Literal["one_off_reminder"]


class WeightAction(BaseModel):
    type: Literal["weight"]
    said: str = Field(min_length=1, max_length=100)
    exercise: str | None = Field(default=None, max_length=200)
    weight_kg: float = Field(ge=overrides.WEIGHT_RANGE[0], le=overrides.WEIGHT_RANGE[1])


class ProgramAction(BaseModel):
    type: Literal["program"]
    program: str | None = Field(default=None, max_length=200)
    start_date: date | None = None

    @model_validator(mode="after")
    def _some(self) -> ProgramAction:
        if not self.program and self.start_date is None:
            raise ValueError("nothing to change")
        return self


class RestAction(BaseModel):
    type: Literal["rest"]
    seconds: int = Field(ge=15, le=600)


Action = Annotated[
    TargetsAction | ReminderAddAction | ReminderChangeAction | OneOffAction | WeightAction | ProgramAction
    | RestAction,
    Field(discriminator="type"),
]
_ACTION = TypeAdapter(Action)

ONE_OFF = "Пока умею только регулярные напоминания: каждый день или по дням недели."
INVALID = {
    "targets": "норма: ккал от 800 до 6000, белок от 40, жиры от 20, углеводы от 50 до 500 г",
    "reminder_add": "напоминание: не понял время (ЧЧ:ММ), дни или текст",
    "weight": "вес: от 1 до 500 кг",
    "rest": "таймер отдыха: от 15 секунд до 10 минут",
    "program": "программа: не понял, что поменять",
}


def parse_actions(data: Any) -> tuple[list[Any], list[str]]:
    """Valid actions and notes about the rejected ones (each action is validated on its own)."""
    items = data.get("actions") if isinstance(data, dict) else None
    valid: list[Any] = []
    notes: list[str] = []
    for item in items if isinstance(items, list) else []:
        try:
            valid.append(_ACTION.validate_python(item))
        except (ValidationError, ValueError):
            kind = item.get("type") if isinstance(item, dict) else None
            if isinstance(kind, str) and kind in INVALID:
                notes.append(f"Не применю — {INVALID[kind]}.")
    return valid, list(dict.fromkeys(notes))


# ---- what the model is told ----

WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
WEEKDAYS_FULL = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")
KIND_LABEL = {"nutrition": "остаток КБЖУ", "advice": "совет", "checkin": "вопрос о самочувствии"}
TARGET_LABEL = {"kcal": ("ккал", ""), "protein": ("белок", " г"), "fat": ("жиры", " г"), "carbs": ("углеводы", " г")}


@dataclass
class Snapshot:
    """What the command is resolved against; loaded once per message."""

    user: User
    today: date  # local day of the message
    current: UserProgram
    programs: list[Program]
    reminders: list[Reminder]
    catalog: list[str]
    today_weights: dict[str, float]  # today's overrides by exercise name


async def load_snapshot(session: AsyncSession, user: User, today: date) -> Snapshot:
    current = await active_program(session, user, today)
    programs = list(await session.scalars(select(Program).order_by(Program.id)))
    reminders = list(
        await session.scalars(
            select(Reminder).where(Reminder.user_id == user.id).order_by(Reminder.minute_of_day, Reminder.id)
        )
    )
    weights = {o.exercise: o.weightKg for o in await overrides.for_day(session, user.id, today)}
    return Snapshot(user, today, current, programs, reminders, await baselines.catalog(session), weights)


def _day(d: date) -> str:
    return f"{WEEKDAYS[d.weekday()]} {d:%d.%m}"


def _days(weekdays: list[int | None]) -> str:
    """Reminder rows' weekdays (0=Mon..6=Sun, None = daily) in words."""
    if None in weekdays or not weekdays:
        return "каждый день"
    days = sorted({w for w in weekdays if w is not None})
    if days == [0, 1, 2, 3, 4]:
        return "по будням"
    if days == [5, 6]:
        return "по выходным"
    if len(days) == 7:
        return "каждый день"
    return ", ".join(WEEKDAYS[d] for d in days)


def _what(kind: str, text: str | None) -> str:
    return f"«{text}»" if kind == "text" else KIND_LABEL.get(kind, kind)


def reminder_groups(rows: list[Reminder]) -> list[tuple[str, list[Reminder]]]:
    """Rows of one reminder said once ("по будням" = five rows): (label, rows) in time order."""
    groups: dict[tuple, list[Reminder]] = {}
    for r in rows:
        groups.setdefault((r.minute_of_day, r.kind, r.text, r.enabled), []).append(r)
    out = []
    for (minute, kind, text, enabled), rs in groups.items():
        label = f"{rem.minute_to_hhmm(minute)} {_days([r.weekday for r in rs])}: {_what(kind, text)}"
        out.append((label + ("" if enabled else " (выключено)"), rs))
    return out


def prompt_context(snap: Snapshot) -> str:
    today = snap.today
    lines = [f"Сегодня {today.isoformat()}, {WEEKDAYS_FULL[today.weekday()]}."]
    lines.append("Программы (id — название): " + "; ".join(f"{p.slug} — {p.name}" for p in snap.programs))
    lines.append(f"Текущая: {snap.current.program.slug}, старт {snap.current.started_on.isoformat()}.")
    if snap.reminders:
        rows = [
            f"id={r.id}: {rem.minute_to_hhmm(r.minute_of_day)} "
            f"{'каждый день' if r.weekday is None else WEEKDAYS[r.weekday]}, {r.kind}"
            + (f" «{r.text}»" if r.text else "") + ("" if r.enabled else ", выключено")
            for r in snap.reminders
        ]
        lines.append("Напоминания: " + "; ".join(rows))
    else:
        lines.append("Напоминаний нет.")
    lines.append("Каталог упражнений: " + (", ".join(snap.catalog) or "пусто"))
    return "\n".join(lines)


# ---- the plan shown in the preview ----


@dataclass
class NewReminder:
    minute: int
    kind: str
    text: str | None
    weekdays: list[int | None]  # 0=Mon..6=Sun, [None] = daily


@dataclass
class Plan:
    """Resolved changes; `lines` is the preview (and the summary after "Применить"), `notes` what is skipped."""

    day: date  # local day the weights are for
    targets: dict[str, int] = field(default_factory=dict)
    rest: int | None = None
    program: tuple[str, date] | None = None  # (slug, Monday it starts)
    weights: dict[int, tuple[str, float]] = field(default_factory=dict)  # exercise id -> (catalog name, kg)
    said_weights: dict[str, float] = field(default_factory=dict)  # every matched weight, also unchanged ones
    new_reminders: list[NewReminder] = field(default_factory=list)
    reminder_ops: dict[int, str] = field(default_factory=dict)  # reminder id -> delete | disable | enable
    lines: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def empty(self) -> bool:
        return not (
            self.targets or self.rest is not None or self.program or self.weights or self.new_reminders
            or self.reminder_ops
        )

    def for_miniapp(self) -> bool:
        """Whether the result is something to look at in the Mini App (the diary button after applying)."""
        return bool(self.targets or self.rest is not None or self.program or self.weights)

    def live_topics(self) -> list[Any]:
        """What the open Mini App refetches after applying (gymbot.services.live)."""
        topics: list[Any] = []
        if self.targets or self.rest is not None or self.program or self.weights:
            topics.append("state")
        if self.program:
            topics.append("plan")  # another program day
        if self.new_reminders or self.reminder_ops:
            topics.append("reminders")
        return topics


def _mmss(seconds: int) -> str:
    return f"{seconds // 60}:{seconds % 60:02d}"


def _close(a: str, b: str) -> bool:
    n = 0
    for x, y in zip(a, b, strict=False):
        if x != y:
            break
        n += 1
    return n >= max(3, min(len(a), len(b)) - 2)


# Words that say what to do, not what about: "выпить креатин" is about «креатин».
_ABOUT_STOP = {
    "выпить", "выпей", "выпивать", "пить", "попить", "принять", "прими", "принимать", "добить", "добей",
    "сделать", "сделай", "забыть", "забудь", "про", "для", "мне", "это", "мой", "мою", "мои", "моё", "мое",
    "напоминание", "напоминания", "напоминаний", "напомнить", "напомни", "каждый", "день", "утром", "вечером",
}


def _about_matches(about: str, r: Reminder) -> bool:
    """Every significant word of `about` is in the reminder: 'креатин' ~ «Выпей креатин» but 'витамин д'
    !~ «Выпей креатин»; 'кбжу'/'белок' ~ the nutrition reminder."""
    words = [w for w in re.findall(r"[a-zа-я0-9]{3,}", normalize(about)) if w not in _ABOUT_STOP]
    if not words:
        return False
    if r.kind != "text":
        label = {"nutrition": "кбжу белок калории еда питание", "advice": "совет советы",
                 "checkin": "самочувствие сон чекин"}.get(r.kind, "")
        target = label.split()
    else:
        target = re.findall(r"[a-zа-я0-9]{3,}", normalize(r.text or ""))
    return all(any(_close(w, x) for x in target) for w in words)


def _find_program(name: str | None, programs: list[Program]) -> Program | None:
    if not name:
        return None
    key = normalize(name)
    for p in programs:
        if key in (normalize(p.slug), normalize(p.name)):
            return p
    hits = [p for p in programs if key in normalize(p.name) or baselines.shares_word(name, p.name)]
    return hits[0] if len(hits) == 1 else None


def covering(rows: list[Reminder], minute: int, kind: str, text: str | None, weekday: int | None) -> list[Reminder]:
    """Reminders that already fire at `minute` with this kind and text on `weekday` (None = every day):
    the same weekday, or a daily one."""
    return [
        r for r in rows
        if r.minute_of_day == minute and r.kind == kind and r.text == text
        and (r.weekday is None or (weekday is not None and r.weekday == weekday))
    ]


async def _weeks(session: AsyncSession, program_id: int) -> int:
    n = await session.scalar(select(func.count()).select_from(ProgramWeek).where(ProgramWeek.program_id == program_id))
    return n or 1


async def resolve(session: AsyncSession, snap: Snapshot, actions: list[Any]) -> Plan:
    plan = Plan(day=snap.today)
    user = snap.user
    targets: dict[str, int] = {}
    weights: dict[str, float] = {}
    unmatched: list[str] = []
    adds: list[ReminderAddAction] = []
    ops: dict[int, str] = {}
    missing: list[str] = []
    program: ProgramAction | None = None
    rest: int | None = None
    for a in actions:
        if isinstance(a, TargetsAction):
            targets.update(a.changes())
        elif isinstance(a, RestAction):
            rest = a.seconds
        elif isinstance(a, ProgramAction):
            program = a
        elif isinstance(a, WeightAction):
            name = overrides.match(a.said, a.exercise, snap.catalog)
            if name is None:
                unmatched.append(a.said)
            else:
                weights.pop(name, None)  # the last one said wins, in its place
                weights[name] = round(a.weight_kg, 2)
        elif isinstance(a, ReminderAddAction):
            adds.append(a)
        elif isinstance(a, OneOffAction):
            if ONE_OFF not in plan.notes:
                plan.notes.append(ONE_OFF)
        elif isinstance(a, ReminderChangeAction):
            op = a.type.removeprefix("reminder_")
            mine = {r.id: r for r in snap.reminders}
            chosen = [mine[i] for i in a.ids if i in mine]
            if not chosen and a.kind and a.kind != "text":
                chosen = [r for r in snap.reminders if r.kind == a.kind]
            if not chosen and a.about:
                chosen = [r for r in snap.reminders if _about_matches(a.about, r)]
            if op == "disable":
                chosen = [r for r in chosen if r.enabled]
            elif op == "enable":
                chosen = [r for r in chosen if not r.enabled]
            if not chosen:
                what = a.about or (KIND_LABEL.get(a.kind or "", "") if a.kind else "")
                missing.append(f"«{what}»" if what else "")
            for r in chosen:
                if ops.get(r.id) != "delete":
                    ops[r.id] = op

    # norm
    old = nut.user_targets(user).model_dump()
    changed = {k: v for k, v in targets.items() if old.get(k) != v}
    if changed:
        plan.targets = changed
        parts = [
            f"{TARGET_LABEL[k][0]} {old[k] if old.get(k) is not None else '—'} → {v}{TARGET_LABEL[k][1]}"
            for k, v in changed.items()
        ]
        plan.lines.append("Норма КБЖУ: " + ", ".join(parts))
    elif targets:
        plan.notes.append("Норма КБЖУ уже такая.")

    # rest timer
    if rest is not None:
        if rest != user.rest_seconds:
            plan.rest = rest
            plan.lines.append(f"Таймер отдыха: {_mmss(user.rest_seconds)} → {_mmss(rest)}")
        else:
            plan.notes.append(f"Таймер отдыха уже {_mmss(rest)}.")

    # program and start
    if program is not None:
        target = snap.current.program
        if program.program:
            found = _find_program(program.program, snap.programs)
            if found is None:
                plan.notes.append(f"Не нашёл программу «{program.program}».")
                target = None
            else:
                target = found
        if target is not None:
            start = monday_of(program.start_date) if program.start_date else snap.current.started_on
            if not snap.today - timedelta(days=366) <= start <= snap.today + timedelta(days=92):
                plan.notes.append(f"Старт {start:%d.%m.%Y} слишком далеко от сегодня, не меняю.")
            elif target.id == snap.current.program_id and start == snap.current.started_on:
                plan.notes.append(f"Программа «{target.name}» и так идёт со старта {_day(start)}.")
            else:
                plan.program = (target.slug, start)
                pos = program_position(start, await _weeks(session, target.id), snap.today)
                where = f"начнётся {_day(start)}" if pos.not_started else f"сегодня неделя {pos.week}"
                was = (
                    f"было с {_day(snap.current.started_on)}" if target.id == snap.current.program_id
                    else f"вместо «{snap.current.program.name}»"
                )
                plan.lines.append(f"Программа «{target.name}»: старт с {_day(start)} ({where}; {was})")

    # weights for today
    plan.said_weights = dict(weights)
    if weights:
        ids = await overrides.exercise_ids(session, list(weights))
        for name, kg in weights.items():
            before = snap.today_weights.get(name)
            if before == kg:
                plan.notes.append(f"{name}: на сегодня уже {overrides.kg(kg)} кг.")
                continue
            plan.weights[ids[name]] = (name, kg)
            was = f" (было {overrides.kg(before)})" if before is not None else ""
            plan.lines.append(f"Вес на сегодня ({_day(snap.today)}): {name} {overrides.kg(kg)} кг{was}")
    if unmatched:
        plan.notes.append(
            "Нет в программе, пропускаю: " + ", ".join(f"«{s}»" for s in dict.fromkeys(unmatched)) + "."
        )

    # reminders: a time already covered by one (a daily one covers every weekday) is not added again;
    # covered only by switched-off ones -> they are switched on instead.
    planned: list[tuple[int, str, str | None, int | None]] = []
    room = rem.MAX_PER_USER - len(snap.reminders) + sum(1 for op in ops.values() if op == "delete")
    for a in adds:
        minute = rem.hhmm_to_minute(a.time)
        wanted: list[int | None] = [d - 1 for d in a.weekdays] or [None]
        label = f"{a.time} {_days(wanted)}: {_what(a.kind, a.text)}"
        days: list[int | None] = []
        enable: list[Reminder] = []
        for d in wanted:
            cover = covering(snap.reminders, minute, a.kind, a.text, d)
            if any(r.enabled and ops.get(r.id) not in ("delete", "disable") for r in cover):
                continue
            if cover and all(ops.get(r.id) != "delete" for r in cover):
                enable += [r for r in cover if not r.enabled]
                continue
            if any(p[:3] == (minute, a.kind, a.text) and p[3] in (d, None) for p in planned):
                continue
            days.append(d)
        for r in enable:
            ops[r.id] = "enable"
        if not days:
            if not enable:
                plan.notes.append(f"Такое напоминание уже есть: {label}.")
            continue
        if len(days) > room:
            plan.notes.append(f"Не добавлю {label}: напоминаний будет больше {rem.MAX_PER_USER}.")
            continue
        room -= len(days)
        planned += [(minute, a.kind, a.text, d) for d in days]
        plan.new_reminders.append(NewReminder(minute, a.kind, a.text, days))
        plan.lines.append(f"Новое напоминание: {a.time} {_days(days)}: {_what(a.kind, a.text)}")
    verbs = {"delete": "Удалить напоминание", "disable": "Выключить напоминание", "enable": "Включить напоминание"}
    for op in ("delete", "disable", "enable"):
        rows = [r for r in snap.reminders if ops.get(r.id) == op]
        for label, _rs in reminder_groups(rows):
            plan.lines.append(f"{verbs[op]}: {label}")
    plan.reminder_ops = ops
    for what in dict.fromkeys(missing):
        plan.notes.append(f"Не нашёл напоминание {what}.".replace("  ", " ").replace(" .", "."))
    return plan


async def apply(session: AsyncSession, user: User, plan: Plan, tz: ZoneInfo, now_utc: datetime | None = None) -> list[str]:
    """Write the plan (the caller commits once). Returns notes about what could not be done any more."""
    now = now_utc or datetime.now(UTC)
    notes: list[str] = []
    if plan.targets:
        nut.set_targets(user, dict(plan.targets))
    if plan.rest is not None:
        user.rest_seconds = plan.rest
    if plan.program is not None:
        try:
            await set_program(session, user, *plan.program)
        except LookupError:
            notes.append("Программа пропала из списка, старт не изменён.")
    if plan.weights and now.astimezone(tz).date() != plan.day:
        names = ", ".join(name for name, _kg in plan.weights.values())
        notes.append(f"Вес на {plan.day:%d.%m} не выставлен ({names}): тот день уже прошёл, повтори команду.")
    else:
        for exercise_id, (_name, kg) in plan.weights.items():
            await overrides.upsert(session, user.id, exercise_id, plan.day, kg)
    for rid, op in plan.reminder_ops.items():
        r = await session.get(Reminder, rid)
        if r is None or r.user_id != user.id:
            continue  # deleted in the Mini App meanwhile
        if op == "delete":
            await session.delete(r)
        elif op == "disable":
            r.enabled = False
        elif not r.enabled:
            r.enabled = True
            r.last_sent_on = rem.initial_last_sent(r.minute_of_day, now, tz)
    if plan.new_reminders:
        await session.flush()  # deletions above free room
        count = await session.scalar(select(func.count()).select_from(Reminder).where(Reminder.user_id == user.id)) or 0
        for new in plan.new_reminders:
            await session.flush()
            if count + len(new.weekdays) > rem.MAX_PER_USER:
                notes.append(f"Напоминание на {rem.minute_to_hhmm(new.minute)} не добавлено: их уже {count}.")
                continue
            rows = list(await session.scalars(select(Reminder).where(Reminder.user_id == user.id)))
            days = [wd for wd in new.weekdays if not covering(rows, new.minute, new.kind, new.text, wd)]
            if not days:  # added in the Mini App meanwhile
                notes.append(f"Напоминание на {rem.minute_to_hhmm(new.minute)} уже есть, не дублирую.")
                continue
            count += len(days)
            for wd in days:
                session.add(
                    Reminder(
                        user_id=user.id, minute_of_day=new.minute, kind=new.kind, text=new.text, weekday=wd,
                        enabled=True, last_sent_on=rem.initial_last_sent(new.minute, now, tz),
                    )
                )
    await session.flush()
    return notes
