"""Edit or delete records that are already saved, from the chat: "удали самсу", "самса была 2, а не 3".

Flow (handlers/saved_edits.py, called from process_text only while no preview or model question is open):
1. `detect` — a conservative regex on the text, before the parser. Strong signals: a delete or fix verb
   ("удали", "убери", "сотри", "исправь", "поправь", "измени") or "N, а не M" / "было N, а не M". Weak ones:
   "не M, а N" and "<блюдо> без <чего-то>"; they route only when they name something saved (see `find`),
   otherwise the parser gets the text as before. A settings command ("убери напоминание") never routes here.
2. `find` — deterministic matching against the user's saved rows: the named day ("вчерашний плов", "в
   понедельник", "за 06.10") or today, falling back to the newest earlier day that has a match. Name words
   match food descriptions and exercise names by a loose common prefix; "последн…" picks the newest;
   the old value of "а не M" narrows candidates (sets of 80 kg, the 350 g plov, 5 hours of sleep). Nothing
   older than MAX_DAYS local days is ever loaded, from any branch.
3. The handler shows a preview (delete: what goes; edit: before -> after) with a confirm button, or up to
   MAX_CHOICES candidates as buttons. An edit's new values come from the parser, given the saved record as
   the previous turn of the dialog (like a revision of a preview); workout and sleep corrections with
   "N, а не M" are applied deterministically, free models rewrite sets unreliably.
4. `apply` reloads the rows by id with the owner's user id, checks they still are what the preview showed
   (`Unit.before`) and the age guard, then writes. Edited rows keep their history: raw_text gets
   "\\n[edit] <text>". For workout sets the trail goes to every set of the workout with the same original
   raw_text, so /undo (which removes the trailing run of equal raw_text) still treats one message as one.

Units: a food entry, a group of entries saved from one message (a meal, "последнюю еду"), the sets of one
exercise in one workout, the last set (with its drops), a whole workout, a wellbeing entry.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from gymbot.db.models import FoodEntry, WellbeingEntry, Workout, WorkoutSet
from gymbot.llm.schemas import ParsedExercise, ParsedFood, ParsedPain, ParsedSet, ParsedWellbeing, ParseResult
from gymbot.services.chat_settings import is_settings_request
from gymbot.services.nutrition import _aware, local_day_bounds
from gymbot.services.programs import get_or_create_exercise, normalize
from gymbot.services.wellbeing import parse_pains

Kind = Literal["food", "workout", "wellbeing"]
Action = Literal["delete", "edit"]
Level = Literal["entry", "group", "exercise", "set", "workout", "wellbeing"]

MAX_DAYS = 14  # records older than this many local days are never touched from the chat
MAX_CHOICES = 5
MAX_WORDS = 12  # longer messages are stories, not commands
SOFT_MINUTES = 60  # "<блюдо> без <чего-то>" edits only food saved this recently
EDIT_MARK = "[edit]"

# ---- intent ----

_DELETE = re.compile(r"(?<!\w)(?:удали|убери|сотри|удалить|убрать|стереть)(?:те)?(?!\w)")
_FIX = re.compile(r"(?<!\w)(?:исправь|поправь|измени|исправить|поправить)(?:те)?(?!\w)")
_NUM_WORDS = {
    "ноль": 0, "один": 1, "одна": 1, "одну": 1, "два": 2, "две": 2, "три": 3, "четыре": 4, "пять": 5,
    "шесть": 6, "семь": 7, "восемь": 8, "девять": 9, "десять": 10, "полтора": 1.5, "полторы": 1.5,
}
_NUM = r"(?:\d+(?:[.,]\d+)?|" + "|".join(_NUM_WORDS) + r")"
# "самса была 2, а не 3", "в плове было 250 г, а не 350", "жим 85 а не 80": the new value, then the old one.
_WAS_NOT = re.compile(
    rf"(?<!\w)(?P<new>{_NUM})(?!\w)(?:\s*[a-zа-я.]{{0,12}})?\s*,?\s*а\s+не\s+(?P<old>{_NUM})(?!\w)"
)
_WAS_NOT_WORDS = re.compile(r"(?<!\w)(?:был|была|было|были)(?!\w).*?(?<!\w)а\s+не(?!\w)")
# "в жиме не 80, а 85", "не самса, а беляш": weak, see the module docstring.
_NOT_BUT = re.compile(r"(?:^|[\s,])не\s+(?P<old>[^,]{1,30}?)\s*,?\s+а\s+(?P<new>[^,]{1,30})$")
_WITHOUT = re.compile(r"^(?P<name>[a-zа-я][a-zа-я\s-]{1,40}?)\s+без\s+[a-zа-я]")
_RECORD_VERB = re.compile(
    r"(?<!\w)(?:съел\w*|поел\w*|выпил\w*|доел\w*|перекусил\w*|сделал\w*|пожал\w*|выжал\w*|отжал\w*|присел\w*|"
    r"подтянул\w*|пробежал\w*|позанимал\w*|спал\w*|поспал\w*)(?!\w)"
)
_LATEST = re.compile(r"(?<!\w)последн\w*")
_FOOD_HINT = re.compile(r"(?<!\w)(?:еда|еду|еды|ед[еы]|ккал|калори\w*|перекус\w*|напит\w*)(?!\w)")
_MEALS = {"завтрак": (4, 11), "обед": (11, 16), "ужин": (16, 28), "полдник": (14, 18)}
_MEAL = re.compile(r"(?<!\w)(завтрак|обед|ужин|полдник)\w*")
_WORKOUT_WORD = re.compile(r"(?<!\w)тренировк\w*")
_SET_WORD = re.compile(r"(?<!\w)(?:подход\w*|сет|сета|сеты)(?!\w)")
_WELLBEING_HINT = re.compile(
    r"(?<!\w)(?:сон|сна|сну|спал\w*|поспал\w*|энерги\w*|настроени\w*|самочувстви\w*|бол(?!ьш)[ьиело]\w*)(?!\w)"
)
_SLEEP = re.compile(r"(?<!\w)(?:сон|сна|сну|спал\w*|поспал\w*)(?!\w)")
_BODY_WEIGHT = re.compile(r"(?<!\w)(?:вес|весил\w*|взвеш\w*)(?!\w)")
_WEEKDAYS = ("понедельн", "вторн", "сред", "четверг", "пятниц", "суббот", "воскресен")
_WEEKDAY = re.compile(r"(?<!\w)(?:в|во|за)\s+(понедельник|вторник|среду|четверг|пятницу|субботу|воскресенье)(?!\w)")
_WEEKDAY_ADJ = re.compile(r"(?<!\w)(понедельничн|вторничн|средов|четвергов|пятничн|субботн|воскресн)\w*")
_DATE = re.compile(r"(?<!\w)(?:за|от)\s+(\d{1,2})\.(\d{1,2})(?!\d)")
_STOP = re.compile(
    r"^(?:удали\w*|убери\w*|убрат\w*|сотри\w*|стерет\w*|исправ\w*|поправ\w*|измени\w*|пожалуйста|был|была|было|"
    r"были|последн\w*|вчера\w*|сегодня\w*|позавчера\w*|запис\w*|штук\w*|грамм\w*|кило\w*|раз|раза|подход\w*|"
    r"сет|сета|сеты|тренировк\w*|упражнени\w*|мой|мою|мои|моё|мое|это|эту|этот|эти|там|тут|весь|всю|все|всё|"
    r"только|его|ее|её|них|вес|весил\w*|ккал|калори\w*|порци\w*|мне|меня|еда|еду|еды|часов|часа|час|минут\w*|"
    r"тоже|ещё|еще|потом|уже|утром|днем|днём|вечером|ночью|сон|сна|сну|спал\w*|поспал\w*|энерги\w*|"
    r"настроени\w*|самочувстви\w*|бол[ьиело]\w*|завтрак\w*|обед\w*|ужин\w*|полдник\w*|перекус\w*|"
    r"понедельн\w*|вторн\w*|сред\w*|четверг\w*|пятниц\w*|суббот\w*|воскрес\w*|средов\w*|"
    r"как|что|где|когда|зачем|почему|можно|можешь|нужно|надо|давай|плиз|свой|свою|себе|который|которую)$"
)


@dataclass(frozen=True)
class Intent:
    action: Action
    weak: bool  # "не M, а N" or "<блюдо> без …": no match = the parser's message, silently
    words: tuple[str, ...]  # name words to match against saved names
    day: date | None  # the named local day, None = today with a fallback (see `find`)
    latest: bool  # "последн…"
    meal: str | None  # "завтрак" | "обед" | "ужин" | "полдник"
    kind: Kind | None  # a kind word ("еду", "подход", "сон"); None = by name
    whole_workout: bool  # "тренировку"
    sets_only: bool  # "подход": the last set, not all sets of an exercise
    pair: tuple[float, float] | None  # (new, old) of "N, а не M"
    soft: bool = False  # "<блюдо> без …": only food saved in the last SOFT_MINUTES
    record_verb: bool = False  # "съел …": with nothing found the parser gets it


def _num(s: str) -> float | None:
    s = s.strip()
    if s in _NUM_WORDS:
        return float(_NUM_WORDS[s])
    try:
        return float(s.replace(",", "."))
    except ValueError:
        return None


def _named_day(norm: str, today: date) -> date | None:
    if re.search(r"(?<!\w)позавчера\w*", norm):
        return today - timedelta(days=2)
    if re.search(r"(?<!\w)вчера\w*", norm):
        return today - timedelta(days=1)
    if re.search(r"(?<!\w)сегодня\w*", norm):
        return today
    if m := _DATE.search(norm):
        day, month = int(m[1]), int(m[2])
        for year in (today.year, today.year - 1):
            try:
                d = date(year, month, day)
            except ValueError:
                return None
            if d <= today:
                return d
        return None
    m = _WEEKDAY.search(norm) or _WEEKDAY_ADJ.search(norm)
    if m:
        stem = next(i for i, s in enumerate(_WEEKDAYS) if m[1].startswith(s[:4]))
        return today - timedelta(days=(today.weekday() - stem) % 7)
    return None


def _words(text: str) -> tuple[str, ...]:
    found = re.findall(r"[a-zа-я]{3,}", text)
    return tuple(dict.fromkeys(w for w in found if not _STOP.match(w) and w not in _NUM_WORDS))


def detect(text: str, today: date) -> Intent | None:
    """The edit or delete command in `text`, or None: then the message is not about saved records."""
    if "?" in text or is_settings_request(text):
        return None
    norm = normalize(text)
    if len(norm.split()) > MAX_WORDS:
        return None
    delete = bool(_DELETE.search(norm))
    pair = None
    if m := _WAS_NOT.search(norm):
        new, old = _num(m["new"]), _num(m["old"])
        pair = (new, old) if new is not None and old is not None else None
    fix = bool(_FIX.search(norm) or pair or _WAS_NOT_WORDS.search(norm))
    weak = soft = False
    name_part = norm
    if not delete and not fix:
        if (m := _NOT_BUT.search(norm)) and len(norm.split()) <= 7:
            weak, old_s, new_s = True, m["old"], m["new"]
            old, new = _num(old_s), _num(new_s)
            pair = (new, old) if new is not None and old is not None else None
        elif (m := _WITHOUT.match(norm)) and len(norm.split()) <= 5 and not re.search(r"\d", norm):
            weak = soft = True
            name_part = m["name"]
        else:
            return None
    meal = m[1] if (m := _MEAL.search(norm)) else None
    if _WELLBEING_HINT.search(norm):
        kind: Kind | None = "wellbeing"
    elif _WORKOUT_WORD.search(norm) or _SET_WORD.search(norm):
        kind = "workout"
    elif meal or _FOOD_HINT.search(norm):
        kind = "food"
    else:
        kind = None
    record_verb = bool(_RECORD_VERB.search(norm))
    words = _words(name_part)
    if weak and record_verb:  # "съел бутерброд без сыра" is a new record
        return None
    if kind is None and not words and _BODY_WEIGHT.search(norm):  # body weight is upserted per day: skip
        return None
    return Intent(
        action="delete" if delete else "edit",
        weak=weak,
        words=words,
        day=_named_day(norm, today),
        latest=bool(_LATEST.search(norm)),
        meal=meal,
        kind=kind,
        whole_workout=bool(_WORKOUT_WORD.search(norm)),
        # "удали подход жима" is one set; "в жиме было 3 подхода" edits the exercise, "последний подход" one set
        sets_only=bool(_SET_WORD.search(norm)) and (delete or bool(_LATEST.search(norm))),
        pair=pair,
        soft=soft,
        record_verb=record_verb,
    )


def mentions_body_weight(text: str) -> bool:
    return bool(_BODY_WEIGHT.search(normalize(text)))


def mentions_sleep(text: str) -> bool:
    return bool(_SLEEP.search(normalize(text)))


# ---- units ----


@dataclass
class Unit:
    """One thing a command may target; `before` is what the preview shows and `apply` checks again."""

    kind: Kind
    level: Level
    ids: tuple[int, ...]  # FoodEntry / WorkoutSet / WellbeingEntry ids, in display order
    at: datetime  # UTC; newest first when several match
    day: date  # local day
    before: ParseResult
    workout_id: int | None = None
    score: int = 0
    raw_texts: tuple[str | None, ...] = ()  # stored raw_text of the rows (meal words are matched there)
    names: tuple[str, ...] = field(default=())  # words the name matching uses


def _food(e: FoodEntry) -> ParsedFood:
    return ParsedFood(
        description=e.description,
        grams=float(e.grams) if e.grams is not None else None,
        kcal=float(e.kcal),
        protein_g=float(e.protein_g),
        fat_g=float(e.fat_g),
        carbs_g=float(e.carbs_g),
    )


def _food_record(rows: list[FoodEntry]) -> ParseResult:
    foods = []
    for e in rows:
        f = _food(e)
        f.kcal = float(e.kcal)  # as saved: the kcal-from-macros check must not "change" a stored row
        foods.append(f)
    return ParseResult(kind="food", foods=foods)


def _sets_record(sets: list[WorkoutSet]) -> ParseResult:
    """Sets (ordered by set_index) as exercises in first-seen order."""
    by: dict[int, ParsedExercise] = {}
    for s in sets:
        ex = by.setdefault(s.exercise_id, ParsedExercise(exercise=s.exercise.name, sets=[]))
        # model_construct: Mini App sets may exceed the parser's limits (reps <= 100); shown as saved
        ex.sets.append(
            ParsedSet.model_construct(
                reps=s.reps, weight_kg=float(s.weight_kg) if s.weight_kg is not None else None,
                drop_index=s.drop_index,
            )
        )
    return ParseResult(kind="workout", exercises=list(by.values()))


def _wellbeing_record(e: WellbeingEntry) -> ParseResult:
    w = ParsedWellbeing(
        sleep_hours=float(e.sleep_hours) if e.sleep_hours is not None else None,
        sleep_quality=e.sleep_quality,
        energy=e.energy,
        mood=e.mood,
        pains=[ParsedPain(place=p.place, severity=p.severity) for p in parse_pains(e.pains)],
        note=e.note,
    )
    return ParseResult(kind="wellbeing", wellbeing=w)


def _name_words(name: str) -> tuple[str, ...]:
    return tuple(re.findall(r"[a-zа-я]{3,}", normalize(name)))


def _close(a: str, b: str) -> bool:
    """Loose same-word check for Russian endings: "самсу"/"самса", "плове"/"плов", "жиме"/"жим"."""
    n = 0
    for x, y in zip(a, b, strict=False):
        if x != y:
            break
        n += 1
    return n >= max(3, min(len(a), len(b)) - 2)


def _score(words: tuple[str, ...], names: tuple[str, ...]) -> int:
    return sum(1 for w in words if any(_close(w, n) for n in names))


def original(raw: str | None) -> str | None:
    """raw_text without the "[edit] …" trail: rows saved from one message stay one group after an edit."""
    return re.split(rf"(?:^|\n){re.escape(EDIT_MARK)} ", raw, maxsplit=1)[0] or None if raw else raw


def _local(dt: datetime, tz: ZoneInfo) -> datetime:
    return _aware(dt).astimezone(tz)


def _has_value(unit: Unit, old: float) -> bool:
    """Whether the unit holds the old value of "N, а не M" (narrows "жим" to the sets of 80 kg)."""
    r = unit.before
    if unit.kind == "food":
        return any(
            f.grams == old or f.kcal == old or re.search(rf"(?<![\d.]){old:g}\s*шт", f.description) for f in r.foods
        )
    if unit.kind == "workout":
        return any(s.weight_kg == old or s.reps == old for ex in r.exercises for s in ex.sets)
    w = r.wellbeing
    return w is not None and old in (w.sleep_hours, w.energy, w.mood, w.sleep_quality)


@dataclass
class Found:
    units: list[Unit]
    too_old: bool = False
    day: date | None = None  # the day searched (for "не нашёл за …")


async def _load(session: AsyncSession, user_id: int, first: date, last: date, tz: ZoneInfo) -> list[Unit]:
    """Every candidate unit of local days first..last, all kinds and levels."""
    start, _ = local_day_bounds(first, tz)
    _, end = local_day_bounds(last, tz)
    units: list[Unit] = []
    foods = list(
        await session.scalars(
            select(FoodEntry)
            .where(FoodEntry.user_id == user_id, FoodEntry.eaten_at >= start, FoodEntry.eaten_at < end)
            .order_by(FoodEntry.eaten_at, FoodEntry.id)
        )
    )
    groups: dict[tuple[datetime, str | None], list[FoodEntry]] = {}
    for e in foods:
        at = _aware(e.eaten_at)
        units.append(
            Unit("food", "entry", (e.id,), at, _local(at, tz).date(), _food_record([e]),
                 raw_texts=(e.raw_text,), names=_name_words(e.description))
        )
        groups.setdefault((at, original(e.raw_text)), []).append(e)
    for (at, raw), rows in groups.items():
        units.append(
            Unit("food", "group", tuple(e.id for e in rows), at, _local(at, tz).date(), _food_record(rows),
                 raw_texts=(raw,), names=tuple(w for e in rows for w in _name_words(e.description)))
        )
    workouts = list(
        await session.scalars(
            select(Workout)
            .where(Workout.user_id == user_id, Workout.performed_on >= first, Workout.performed_on <= last)
            .options(selectinload(Workout.sets).selectinload(WorkoutSet.exercise))
            .order_by(Workout.started_at, Workout.id)
        )
    )
    for w in workouts:
        sets = sorted(w.sets, key=lambda s: s.set_index)
        if not sets:
            continue
        at = max(_aware(s.created_at) for s in sets)
        units.append(Unit("workout", "workout", tuple(s.id for s in sets), at, w.performed_on, _sets_record(sets),
                          workout_id=w.id, raw_texts=tuple(s.raw_text for s in sets)))
        by_ex: dict[int, list[WorkoutSet]] = {}
        for s in sets:
            by_ex.setdefault(s.exercise_id, []).append(s)
        for ex_sets in by_ex.values():
            ex = ex_sets[0].exercise
            names = tuple(dict.fromkeys(w2 for n in [ex.name, *(ex.aliases or [])] for w2 in _name_words(n)))
            ex_at = max(_aware(s.created_at) for s in ex_sets)
            # the exercise's sets; ordered within the workout by set_index, so the last one is the newest
            ex_at = ex_at + timedelta(microseconds=sets.index(ex_sets[-1]))
            units.append(Unit("workout", "exercise", tuple(s.id for s in ex_sets), ex_at, w.performed_on,
                              _sets_record(ex_sets), workout_id=w.id, names=names,
                              raw_texts=tuple(s.raw_text for s in ex_sets)))
            # the exercise's last set with its drops
            i = len(ex_sets) - 1
            while i > 0 and ex_sets[i].drop_index:
                i -= 1
            last = ex_sets[i:]
            units.append(Unit("workout", "set", tuple(s.id for s in last), ex_at, w.performed_on,
                              _sets_record(last), workout_id=w.id, names=names,
                              raw_texts=tuple(s.raw_text for s in last)))
    wellbeing = list(
        await session.scalars(
            select(WellbeingEntry)
            .where(WellbeingEntry.user_id == user_id, WellbeingEntry.noted_at >= start, WellbeingEntry.noted_at < end)
            .order_by(WellbeingEntry.noted_at, WellbeingEntry.id)
        )
    )
    for e in wellbeing:
        at = _aware(e.noted_at)
        units.append(Unit("wellbeing", "wellbeing", (e.id,), at, _local(at, tz).date(), _wellbeing_record(e),
                          raw_texts=(e.raw_text,)))
    return units


def _in_meal(unit: Unit, meal: str, tz: ZoneInfo) -> bool:
    if any(raw and meal in normalize(raw) for raw in unit.raw_texts):
        return True
    lo, hi = _MEALS[meal]
    hour = _local(unit.at, tz).hour
    return lo <= hour < hi or lo <= hour + 24 < hi


def _select(units: list[Unit], intent: Intent, tz: ZoneInfo, now: datetime) -> list[Unit]:
    """Candidates of one day's (or the window's) units for the command, best first; [] = nothing matches."""
    kind = intent.kind
    if kind == "wellbeing":
        picked = [u for u in units if u.kind == "wellbeing"]
        if intent.pair:
            picked = [u for u in picked if _has_value(u, intent.pair[1])] or picked
        if intent.action == "edit":
            return picked[-1:]  # one "how do I feel" record per day is the usual case: the newest
        return picked
    if kind == "workout" and intent.whole_workout and not intent.words:
        if intent.action == "edit":
            return []  # which exercise? the handler asks to name it
        return [u for u in units if u.level == "workout"]
    if kind == "food" and not intent.words:
        groups = [u for u in units if u.level == "group"]
        if intent.meal:
            return [u for u in groups if _in_meal(u, intent.meal, tz)]
        return groups if intent.latest else []
    if kind == "workout" and intent.sets_only and not intent.words:
        sets = [u for u in units if u.level == "set"]
        return sets if intent.latest else []
    if not intent.words:
        return []
    workout_level: Level = "set" if intent.sets_only else "exercise"
    levels: set[Level] = {"entry"} if kind == "food" else {workout_level} if kind == "workout" else {
        "entry", workout_level
    }
    scored = []
    for u in units:
        if u.level not in levels:
            continue
        u.score = _score(intent.words, u.names)
        if u.score:
            scored.append(u)
    if intent.soft:
        recent = now - timedelta(minutes=SOFT_MINUTES)
        scored = [u for u in scored if u.kind == "food" and u.at >= recent]
    if not scored:
        return []
    best = max(u.score for u in scored)
    picked = [u for u in scored if u.score == best]
    if intent.pair:
        picked = [u for u in picked if _has_value(u, intent.pair[1])] or ([] if intent.weak else picked)
    return picked


async def find(session: AsyncSession, user_id: int, intent: Intent, now: datetime, tz: ZoneInfo) -> Found:
    """Candidate units, newest first: one = the target, several = buttons, none = not found."""
    today = now.astimezone(tz).date()
    oldest = today - timedelta(days=MAX_DAYS)
    if intent.day is not None and intent.day < oldest:
        return Found([], too_old=True, day=intent.day)
    if intent.day is not None:
        picked = _select(await _load(session, user_id, intent.day, intent.day, tz), intent, tz, now)
        day = intent.day
    else:
        # today, then yesterday (a late workout corrected the next morning); "последн…" looks back MAX_DAYS
        first = oldest if intent.latest else today - timedelta(days=1)
        units = await _load(session, user_id, first, today, tz)
        picked, day = [], today
        for d in sorted({u.day for u in units} | {today}, reverse=True):
            picked = _select([u for u in units if u.day == d], intent, tz, now)
            if picked:
                day = d
                break
    picked.sort(key=lambda u: u.at, reverse=True)
    if intent.latest and picked:
        picked = picked[:1]
    return Found(picked, day=day)


# ---- rendering ----


def _kcal(x: float) -> str:
    return f"{x:.0f}"


def food_line(f: ParsedFood) -> str:
    grams = f" {f.grams:g} г" if f.grams and "шт" not in f.description else ""
    return f"{f.description}{grams} — {_kcal(f.kcal)} ккал"


def _set_text(s: ParsedSet) -> str:
    base = f"{s.weight_kg:g} кг × {s.reps}" if s.weight_kg is not None else f"{s.reps} повт."
    return base + (" (дроп)" if s.drop_index else "")


def sets_line(ex: ParsedExercise) -> str:
    return f"{ex.exercise}: " + ", ".join(_set_text(s) for s in ex.sets)


def wellbeing_parts(w: ParsedWellbeing) -> list[str]:
    parts = []
    if w.sleep_hours is not None:
        parts.append(f"сон {w.sleep_hours:g} ч")
    if w.sleep_quality:
        parts.append(f"качество сна {w.sleep_quality}/5")
    parts += [f"{label} {v}/5" for label, v in (("энергия", w.energy), ("настроение", w.mood)) if v]
    if w.pains:
        parts.append("боли: " + ", ".join(p.place + (f" ({p.severity}/5)" if p.severity else "") for p in w.pains))
    if w.note:
        parts.append(f"заметка: {w.note}")
    return parts


def when(unit: Unit, tz: ZoneInfo) -> str:
    if unit.kind == "workout":
        return f"{unit.day:%d.%m}"
    return f"{_local(unit.at, tz):%d.%m %H:%M}"


def tonnage(r: ParseResult) -> float:
    return sum((s.weight_kg or 0) * s.reps for ex in r.exercises for s in ex.sets)


def label(unit: Unit, tz: ZoneInfo) -> str:
    """One line for a candidate button or a delete preview."""
    r = unit.before
    if unit.kind == "food":
        return "; ".join(food_line(f) for f in r.foods) + f" ({when(unit, tz)})"
    if unit.kind == "wellbeing":
        return "самочувствие: " + (", ".join(wellbeing_parts(r.wellbeing)) if r.wellbeing else "—") + \
            f" ({when(unit, tz)})"
    if unit.level == "workout":
        n = sum(len(ex.sets) for ex in r.exercises)
        return f"тренировка {when(unit, tz)}: упражнений {len(r.exercises)}, подходов {n}"
    return "; ".join(sets_line(ex) for ex in r.exercises) + f" ({when(unit, tz)})"


def delete_preview(unit: Unit, tz: ZoneInfo) -> str:
    r = unit.before
    if unit.level == "workout":
        n = sum(len(ex.sets) for ex in r.exercises)
        lines = [f"• {ex.exercise}: подходов {len(ex.sets)}" for ex in r.exercises]
        return (
            f"Удалить тренировку {when(unit, tz)}?\n" + "\n".join(lines)
            + f"\nВсего: упражнений {len(r.exercises)}, подходов {n}, тоннаж {tonnage(r):.0f} кг"
        )
    if unit.kind == "food":
        lines = [f"• {food_line(f)}" for f in r.foods]
        return "Удалить?\n" + "\n".join(lines) + f"\n({when(unit, tz)})"
    if unit.kind == "wellbeing":
        parts = wellbeing_parts(r.wellbeing) if r.wellbeing else []
        return f"Удалить самочувствие ({when(unit, tz)})?\n• " + ", ".join(parts)
    return "Удалить?\n" + "\n".join(f"• {sets_line(ex)}" for ex in r.exercises) + f"\n({when(unit, tz)})"


def edit_preview(unit: Unit, after: ParseResult, tz: ZoneInfo) -> str:
    r = unit.before
    if unit.kind == "food":
        old = "; ".join(food_line(f) for f in r.foods)
        new = "; ".join(food_line(f) for f in after.foods)
        return f"Исправить? ({when(unit, tz)})\n{old}\n→ {new}"
    if unit.kind == "wellbeing":
        old_parts = wellbeing_parts(r.wellbeing) if r.wellbeing else []
        new_parts = wellbeing_parts(after.wellbeing) if after.wellbeing else []
        return (
            f"Исправить самочувствие? ({when(unit, tz)})\n" + (", ".join(old_parts) or "—")
            + "\n→ " + (", ".join(new_parts) or "—")
        )
    old_ex, new_ex = r.exercises[0], after.exercises[0]
    return f"Исправить? ({when(unit, tz)})\n{sets_line(old_ex)}\n→ {sets_line(new_ex)}"


# ---- the new values of an edit ----


def history_turn(unit: Unit) -> tuple[str, str]:
    """The saved record as the previous turn of the dialog: a synthetic message and the record's JSON.
    Synthetic, not the stored raw_text: that may hold other foods of the meal and "[voice]"/"[edit]" marks."""
    r = unit.before
    if unit.kind == "food":
        text = ", ".join(f"{f.description}" + (f" {f.grams:g} г" if f.grams else "") for f in r.foods)
    elif unit.kind == "workout":
        text = "; ".join(sets_line(ex) for ex in r.exercises)
    else:
        text = ", ".join(wellbeing_parts(r.wellbeing)) if r.wellbeing else ""
    return text, r.model_dump_json(exclude_defaults=True)


def swap(unit: Unit, intent: Intent, text: str) -> ParseResult | None:
    """"N, а не M" without the model: workout weights (or reps) of M become N, sleep of M hours becomes N."""
    if intent.pair is None:
        return None
    new, old = intent.pair
    r = unit.before
    try:
        if unit.kind == "workout" and len(r.exercises) == 1:
            ex = r.exercises[0]
            field_ = "weight_kg" if any(s.weight_kg == old for s in ex.sets) else (
                "reps" if any(s.reps == old for s in ex.sets) else None
            )
            if field_ is None or (field_ == "reps" and new != int(new)):
                return None
            value = new if field_ == "weight_kg" else int(new)
            sets = [
                ParsedSet.model_validate({**s.model_dump(), field_: value}) if getattr(s, field_) == old else s
                for s in ex.sets
            ]
            return r.model_copy(update={"exercises": [ParsedExercise(exercise=ex.exercise, sets=sets)]})
        w0 = r.wellbeing
        if unit.kind == "wellbeing" and w0 is not None and mentions_sleep(text) and w0.sleep_hours == old:
            w = ParsedWellbeing.model_validate({**w0.model_dump(), "sleep_hours": new})
            return r.model_copy(update={"wellbeing": w}) if w.sleep_hours is not None else None
    except ValidationError:
        return None
    return None


def edited(unit: Unit, result: ParseResult) -> ParseResult | None:
    """The model's answer as the unit's new values, or None when it is not a usable revision of it."""
    r = unit.before
    if result.kind != unit.kind or not result.is_record():
        return None
    if unit.kind == "food":
        foods = result.foods
        if len(r.foods) == 1 and len(foods) > 1:  # one entry corrected: keep the food that is still it
            names = _name_words(r.foods[0].description)
            best = max(foods, key=lambda f: _score(_name_words(f.description), names))
            if not _score(_name_words(best.description), names):
                return None
            foods = [best]
        return ParseResult(kind="food", foods=foods, note=result.note)
    if unit.kind == "workout":
        if len(r.exercises) != 1 or not result.exercises:
            return None
        names = _name_words(r.exercises[0].exercise)
        ex = max(result.exercises, key=lambda e: _score(_name_words(e.exercise), names))
        if len(result.exercises) > 1 and not _score(_name_words(ex.exercise), names):
            return None
        return ParseResult(kind="workout", exercises=[ex], note=result.note)
    return ParseResult(kind="wellbeing", wellbeing=result.wellbeing, note=result.note)


def unchanged(unit: Unit, after: ParseResult) -> bool:
    return after.model_dump(exclude={"note"}) == unit.before.model_dump(exclude={"note"})


# ---- apply ----


class Stale(Exception):
    """The rows changed, went away or aged out since the preview."""


def _trail(raw: str | None, edit_raw: str) -> str:
    mark = f"{EDIT_MARK} {edit_raw}"
    return f"{raw}\n{mark}" if raw else mark


def _check(rows: list, unit: Unit, record: ParseResult, days: list[date], today: date) -> None:
    if len(rows) != len(unit.ids):
        raise Stale
    if any(d < today - timedelta(days=MAX_DAYS) for d in days):
        raise Stale
    if record.model_dump() != unit.before.model_dump():
        raise Stale


async def _food_rows(session: AsyncSession, user_id: int, ids: tuple[int, ...]) -> list[FoodEntry]:
    rows = {e.id: e for e in await session.scalars(
        select(FoodEntry).where(FoodEntry.user_id == user_id, FoodEntry.id.in_(ids))
    )}
    return [rows[i] for i in ids if i in rows]


async def _set_rows(session: AsyncSession, user_id: int, ids: tuple[int, ...]) -> list[WorkoutSet]:
    rows = {s.id: s for s in await session.scalars(
        select(WorkoutSet).join(Workout)
        .where(Workout.user_id == user_id, WorkoutSet.id.in_(ids))
        .options(selectinload(WorkoutSet.exercise), selectinload(WorkoutSet.workout))
    )}
    return [rows[i] for i in ids if i in rows]


async def apply(
    session: AsyncSession, user_id: int, unit: Unit, action: Action, after: ParseResult | None,
    edit_raw: str, now: datetime, tz: ZoneInfo,
) -> None:
    """Delete or update the unit's rows (no commit). Raises Stale if they are not what the preview showed."""
    today = now.astimezone(tz).date()
    if unit.kind == "food":
        rows = await _food_rows(session, user_id, unit.ids)
        _check(rows, unit, _food_record(rows) if rows else ParseResult(kind="food"),
                     [_local(e.eaten_at, tz).date() for e in rows], today)
        if action == "delete":
            for e in rows:
                await session.delete(e)
            return
        assert after is not None
        first = rows[0]
        trail = _trail(first.raw_text, edit_raw)
        for e, f in zip(rows, after.foods, strict=False):
            _set_food(e, f)
            e.raw_text = _trail(e.raw_text, edit_raw)
        for f in after.foods[len(rows):]:
            e = FoodEntry(user_id=user_id, eaten_at=first.eaten_at, raw_text=trail, description="", kcal=0,
                          protein_g=0, fat_g=0, carbs_g=0)
            _set_food(e, f)
            session.add(e)
        for e in rows[len(after.foods):]:
            await session.delete(e)
        return
    if unit.kind == "wellbeing":
        rows = list(await session.scalars(
            select(WellbeingEntry).where(WellbeingEntry.user_id == user_id, WellbeingEntry.id.in_(unit.ids))
        ))
        _check(rows, unit, _wellbeing_record(rows[0]) if rows else ParseResult(kind="wellbeing"),
                     [_local(e.noted_at, tz).date() for e in rows], today)
        e = rows[0]
        if action == "delete":
            await session.delete(e)
            return
        assert after is not None and after.wellbeing is not None
        w = after.wellbeing
        pains = [{"place": p.place, "severity": p.severity} for p in w.pains]
        e.sleep_hours = Decimal(str(w.sleep_hours)) if w.sleep_hours is not None else None
        e.sleep_quality, e.energy, e.mood, e.note = w.sleep_quality, w.energy, w.mood, w.note
        e.pains = json.dumps(pains, ensure_ascii=False) if pains else None
        e.raw_text = _trail(e.raw_text, edit_raw)
        return
    sets = await _set_rows(session, user_id, unit.ids)
    sets.sort(key=lambda s: s.set_index)
    _check(sets, unit, _sets_record(sets) if sets else ParseResult(kind="workout"),
                 [s.workout.performed_on for s in sets], today)
    workout = await session.get(
        Workout, unit.workout_id, options=[selectinload(Workout.sets)], populate_existing=True
    )
    if workout is None or workout.user_id != user_id:
        raise Stale
    if action == "delete":
        if unit.level == "workout":
            await session.delete(workout)
            return
        for s in sets:
            await session.delete(s)
        await session.flush()
        left = await session.get(
            Workout, unit.workout_id, options=[selectinload(Workout.sets)], populate_existing=True
        )
        if left is not None and not left.sets:  # like /undo: no empty workout stays behind
            await session.delete(left)
        return
    assert after is not None and len(after.exercises) == 1
    new_ex = after.exercises[0]
    exercise = await get_or_create_exercise(session, new_ex.exercise)
    originals = {s.raw_text for s in sets if s.raw_text}
    for s in workout.sets:  # the whole message keeps one raw_text, see the module docstring
        if s.raw_text in originals and s.id not in {x.id for x in sets}:
            s.raw_text = _trail(s.raw_text, edit_raw)
    for s, p in zip(sets, new_ex.sets, strict=False):
        s.reps, s.drop_index, s.exercise_id = p.reps, p.drop_index, exercise.id
        s.weight_kg = Decimal(str(p.weight_kg)) if p.weight_kg is not None else None
        s.raw_text = _trail(s.raw_text, edit_raw)
    extra = new_ex.sets[len(sets):]
    if extra:
        last_index = sets[-1].set_index
        for s in workout.sets:  # make room right after the exercise's sets
            if s.set_index > last_index:
                s.set_index += len(extra)
        for i, p in enumerate(extra, 1):
            workout.sets.append(WorkoutSet(
                exercise_id=exercise.id, set_index=last_index + i, reps=p.reps, drop_index=p.drop_index,
                weight_kg=Decimal(str(p.weight_kg)) if p.weight_kg is not None else None,
                raw_text=sets[-1].raw_text,
            ))
    for s in sets[len(new_ex.sets):]:
        await session.delete(s)


def _set_food(e: FoodEntry, f: ParsedFood) -> None:
    e.description = f.description
    e.grams = Decimal(str(f.grams)) if f.grams else None
    e.kcal = Decimal(str(f.kcal))
    e.protein_g = Decimal(str(f.protein_g))
    e.fat_g = Decimal(str(f.fat_g))
    e.carbs_g = Decimal(str(f.carbs_g))


TOPICS: dict[Kind, tuple[str, ...]] = {
    "food": ("nutrition",),
    "workout": ("workouts", "state"),
    "wellbeing": ("wellbeing", "plan"),
}
