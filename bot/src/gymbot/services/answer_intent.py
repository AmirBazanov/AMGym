"""Factual diary questions recognized without a model, and exercise names found in free Russian text.

`classify` is deliberately conservative: it returns an intent only for questions about logged data
("сколько сегодня тоннаж", "сколько белка осталось", "мой рекорд в приседе") and None for anything that
asks for advice or the plan ("сколько подходов делать", "что сегодня по плану", "как думаешь…"); None
sends the question to the model (gymbot.services.answer). Words are compared after `programs.normalize`
(lower case, ё -> е).

`mentions` finds exercise names in inflected text ("в жиме лёжа", "во французском жиме"): every content
word of a name, cut to a rough stem, must occur in order within a short window of the text; longer names
win and consume their words, so "французский жим" never also counts as "жим лёжа". The main lifts have
declension-tolerant SYNONYMS ("в приседе", "румынка", "становая"). `head_matches` is the loose fallback
for a bare head word ("в жиме", "в отведениях"): every history exercise that has that word.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum

from gymbot.services.programs import normalize


class Intent(Enum):
    WORKOUT = "workout"  # today's / yesterday's / last workout: sets, tonnage
    FOOD = "food"  # food today / yesterday, KBJU left to the norm
    EXERCISE = "exercise"  # an exercise's last time, record, 1RM


@dataclass(frozen=True)
class Question:
    intent: Intent
    day: str  # "today" | "yesterday" | "last"
    record: bool = False  # asks for a record / 1RM / max rather than the last time
    listing: bool = False  # "что я ел" (the entries), not only the totals
    previous: bool = False  # "в прошлый раз", "на прошлой тренировке": a day before today


def _rx(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern)


_W = r"(?<![а-яa-z0-9])"  # word start (\b is fine too, but this also works next to digits)
_E = r"(?![а-яa-z0-9])"  # word end
_ME = r"(?:я\s+|ты\s+|мне\s+)?"
_WHEN = r"(?:(?:сегодня|вчера|всего|уже|там)\s+)?"
_SLOT = _ME + _WHEN + _ME + _WHEN  # "сколько я сегодня", "сколько сегодня я", "сколько всего сегодня"

# Advice, plan and future: never a factual question, whatever else matches.
_ADVICE = _rx(
    _W + r"(?:надо|нужно|стоит|лучше|следующ\w*|завтра|план\w*|посовет\w*|совет\w*|подтян\w*|думаешь|считаешь|"
    r"можно|должен|должна|ставить|поставить|делать|сделать|брать|взять|жать|выжать|добавить|прибавить|"
    r"увелич\w*|уменьш\w*|рекоменд\w*|почему|зачем|недел\w*|месяц\w*|прогресс\w*|будет|буду|норм\w* ли|"
    r"програм\w*|правильно)" + _E
)
# "сколько калорий в шашлыке": a question about a food, not about the diary.
_ABOUT_A_FOOD = _rx(_W + r"(?:в|во)\s+(?!день|сумме|итоге|целом|общем|норм|прошл|последн|этот|тот)[а-яa-z]+")

_WORKOUT = [
    _rx(_W + r"тоннаж\w*"),
    _rx(_W + r"что\s+" + _SLOT + r"(?:с?делал\w*|сделано|потренил\w*|тренировал\w*|было\s+на\s+тренировк\w*)" + _E),
    _rx(_W + r"сколько\s+" + _SLOT + r"(?:с?делал\w*\s+)?(?:всего\s+)?(?:подход\w*|упражнени\w*|сет\w*)" + _E),
    _rx(_W + r"как\s+" + _SLOT + r"(?:потренил\w*|потренировал\w*|позанимал\w*|отзанимал\w*|сходил\w* в зал)"),
    _rx(_W + r"итог\w*\s+(?:сегодняшней\s+|вчерашней\s+|последней\s+|прошлой\s+)?тренировк\w*"),
    _rx(_W + r"(?:что|как)\s+(?:было\s+)?(?:на\s+)?(?:прошлой|последней|вчерашней|сегодняшней)\s+тренировк\w*"),
]
_MACRO = r"(?:ккал|калори\w*|кбжу|бжу|бел(?:ок|ка|ку|ке)|жир\w*|углевод\w*)"
_FOOD = [
    _rx(_W + r"сколько\s+" + _SLOT + r"(?:съел\w*|поел\w*|ел|ела|наел\w*)" + _E),
    _rx(_W + r"сколько\s+" + _SLOT + _MACRO + r"\s+" + _SLOT + r"(?:съел\w*|набрал\w*|получил\w*|осталось|остается|"
        r"сегодня|вчера|за\s+день|за\s+сегодня|до\s+нормы)" + _E),
    _rx(_W + r"сколько\s+" + _SLOT + r"(?:осталось|остается|не\s*хватает|недобрал\w*)\s+" + _SLOT + _MACRO),
    _rx(_W + r"сколько\s+" + _SLOT + r"(?:набрал\w*|съел\w*)\s+" + _SLOT + _MACRO),
]
_FOOD_LIST = _rx(_W + r"что\s+" + _SLOT + r"(?:съел\w*|поел\w*|ел|ела)" + _E)
_RECORD = _rx(_W + r"(?:рекорд\w*|1\s*-?\s*пм|пм|1\s*rm|одноповтор\w*|максимум\w*|макс|разов\w+\s+максимум\w*)" + _E)
_LAST_TIME = _rx(_W + r"(?:в|на)\s+(?:прошл\w+|последн\w+)\s+(?:раз|тренировк\w*)" + _E)
_PREVIOUS = _rx(_W + r"(?:прошл\w+|предыдущ\w+)\s+(?:раз|тренировк\w*)" + _E)
_LIFTED = _rx(_W + r"(?:сколько|какой\s+вес|с\s+каким\s+весом)\s+" + _SLOT + r"(?:жал\w*|пожал\w*|выжал\w*|отжал\w*|присел\w*|"
              r"приседал\w*|тянул\w*|потянул\w*|поднимал\w*|поднял\w*|с?делал\w*|работал\w*)" + _E)


def _day(text: str) -> str:
    if re.search(_W + r"(?:вчера|вчерашн\w*)" + _E, text):
        return "yesterday"
    if re.search(_W + r"(?:сегодня|сегодняшн\w*)" + _E, text):
        return "today"
    return "last"


def classify(text: str) -> Question | None:
    """The factual intent of a question, or None (the model answers). See the module doc."""
    t = normalize(text).replace("?", " ").strip()
    if not t or _ADVICE.search(t):
        return None
    day = _day(t)
    previous = bool(_LAST_TIME.search(t) or _PREVIOUS.search(t))
    if _RECORD.search(t) or (_LAST_TIME.search(t) and not any(p.search(t) for p in _WORKOUT)) or _LIFTED.search(t):
        return Question(Intent.EXERCISE, day, record=bool(_RECORD.search(t)), previous=previous)
    if any(p.search(t) for p in _WORKOUT):
        return Question(Intent.WORKOUT, day, previous=previous)
    if any(p.search(t) for p in _FOOD) or _FOOD_LIST.search(t):
        if _ABOUT_A_FOOD.search(t):
            return None
        return Question(Intent.FOOD, "yesterday" if day == "yesterday" else "today", listing=bool(_FOOD_LIST.search(t)))
    return None


# ---- exercise names in text ----

_TOKEN = re.compile(r"[а-яa-z0-9]+")
_ENDINGS = sorted(
    (
        "иями", "ями", "ами", "ого", "его", "ому", "ему", "ыми", "ими", "ией", "ий", "ый", "ой",
        "ей", "ая", "яя", "ое", "ее", "ую", "юю", "ом", "ем", "ам", "ям", "ах", "ях", "ов", "ев",
        "ия", "ие", "ию", "ии", "ы", "и", "а", "я", "е", "у", "ю", "о", "ь", "й",
    ),
    key=len,
    reverse=True,
)
_STOP = {"для", "под", "над", "при", "без", "или", "из", "за", "со", "на", "в", "во", "с", "к", "по", "и", "от", "до"}
WINDOW = 2  # extra words allowed between the words of one name


def stem(word: str) -> str:
    """A rough Russian stem: one case ending cut, at least 3 letters kept."""
    for e in _ENDINGS:
        if word.endswith(e) and len(word) - len(e) >= 3:
            return word[: -len(e)]
    return word


def same(a: str, b: str, shortest: int = 4) -> bool:
    """Stems equal, or one a prefix of the other when both have `shortest`+ letters ("отведен" ~ "отведени")."""
    if a == b:
        return True
    return min(len(a), len(b)) >= shortest and (a.startswith(b) or b.startswith(a))


def words(text: str) -> list[str]:
    """Content-word stems of `text` in order (prepositions dropped)."""
    return [stem(w) for w in _TOKEN.findall(normalize(text)) if w not in _STOP]


# Main lifts said in any case form -> the catalog name; searched (not fullmatched) in normalize()d text.
SYNONYMS: list[tuple[re.Pattern[str], str]] = [
    (_rx(_W + r"(?:жим\w*\s+(?:штанг\w*\s+)?леж\w*|жим\w*\s+(?:на|в)\s+горизонт\w*|горизонтальн\w*\s+жим\w*|бенч\w*)"),
     "жим лёжа"),
    (_rx(_W + r"(?:румынск\w*|румынк\w*|рдл)" + _E), "румынская тяга"),
    (_rx(_W + r"(?:станов\w*)" + _E), "становая тяга"),
    # Not "гакк-приседания" / "хакк-присед": a machine, another exercise.
    (_rx(_W + r"(?<!гакк[\s-])(?<!хакк[\s-])(?<!гак[\s-])(?<!хак[\s-])(?:присед\w*)" + _E), "присед со штангой"),
    (_rx(_W + r"(?:тяг\w*\s+(?:верхн\w*|вертикальн\w*)\s+блок\w*|верхн\w*\s+тяг\w*|вертикальн\w*\s+тяг\w*)"),
     "тяга вертикального блока"),
    (_rx(_W + r"(?:тяг\w*\s+(?:нижн\w*|горизонтальн\w*)\s+блок\w*|горизонтальн\w*\s+тяг\w*)"),
     "тяга горизонтального блока"),
]


def _find(name_words: list[str], text_words: list[str], used: set[int]) -> list[int] | None:
    """Positions of `name_words` in order within len + WINDOW words of the text, skipping used ones."""
    if not name_words:
        return None
    span = len(name_words) + WINDOW
    # A one-word name ("шраги", "планка") must not match any word it happens to prefix ("разве" ~ "разведения").
    shortest = 5 if len(name_words) == 1 else 4
    for start, w in enumerate(text_words):
        if start in used or not same(name_words[0], w, shortest):
            continue
        pos = [start]
        for nw in name_words[1:]:
            nxt = next(
                (i for i in range(pos[-1] + 1, min(start + span, len(text_words))) if i not in used
                 and same(nw, text_words[i])),
                None,
            )
            if nxt is None:
                break
            pos.append(nxt)
        if len(pos) == len(name_words):
            return pos
    return None


def mentions(
    text: str, names: Iterable[str], aliases: dict[str, list[str]] | None = None, synonyms: bool = True
) -> list[str]:
    """Exercise names (from `names`) said in `text`, in the order of the text; see the module doc.

    `aliases` maps a name to other spellings of it. A synonym hit gives its catalog name even when that
    name is not in `names` (the caller then knows the exercise but may have no sets of it).
    """
    tw = words(text)
    spellings: list[tuple[str, list[str]]] = []
    for name in dict.fromkeys(names):
        for s in [name, *(aliases or {}).get(name, [])]:
            if ws := words(s):
                spellings.append((name, ws))
    spellings.sort(key=lambda p: len(p[1]), reverse=True)
    used: set[int] = set()
    found: list[tuple[int, str]] = []
    for name, ws in spellings:
        if any(n == name for _, n in found):
            continue
        pos = _find(ws, tw, used)
        if pos is not None:
            used.update(pos)
            found.append((pos[0], name))
    t = normalize(text)
    by_key = {normalize(n): n for n in names}
    for pattern, target in SYNONYMS if synonyms else []:
        m = pattern.search(t)
        if m is None:
            continue
        # The words the synonym covered, if a longer name has not taken them already.
        first = len(words(t[: m.start()]))
        covered = set(range(first, first + max(1, len(words(m.group(0))))))
        if covered & used:
            continue
        used.update(covered)
        found.append((first, by_key.get(normalize(target), target)))
    return list(dict.fromkeys(n for _, n in sorted(found)))


def synonym_targets(text: str) -> list[str]:
    """Catalog names of the main lifts said in `text` in any form ("в румынке" -> "румынская тяга")."""
    t = normalize(text)
    return [target for pattern, target in SYNONYMS if pattern.search(t)]


def contains_all(name: str, other: str) -> bool:
    """Whether every content word of `name` is in `other` ("присед со штангой" in "приседания со штангой")."""
    ws, ow = words(name), words(other)
    return bool(ws) and all(any(same(w, o) for o in ow) for w in ws)


def variants(name: str, history: Iterable[str]) -> list[str]:
    """History names of the same exercise as `name`: equal, a more specific name with all its words, or the
    same main lift by SYNONYMS ("румынская тяга" -> "румынская тяга с гантелями", "присед со штангой" ->
    "приседания со штангой", "жим лёжа" -> "жим штанги лёжа")."""
    key = normalize(name)
    patterns = [p for p, target in SYNONYMS if normalize(target) == key or p.search(key)]
    return [
        h for h in dict.fromkeys(history)
        if normalize(h) == key or contains_all(name, h) or any(p.search(normalize(h)) for p in patterns)
    ]


def head_matches(text: str, names: Iterable[str], skip: Iterable[str] = ()) -> list[str]:
    """`names` sharing the most content words with `text` (other than `skip` stems): "в жиме" -> every
    *жим*, "во французском жиме" -> only the French press."""
    skip_set = set(skip)
    tw = [w for w in words(text) if w not in skip_set and len(w) >= 3]
    scored = [(sum(any(same(a, b) for a in words(n)) for b in tw), n) for n in dict.fromkeys(names)]
    best = max((score for score, _ in scored), default=0)
    return [n for score, n in scored if score and score == best]


# Question words that never name an exercise (stems, see `words`): kept out of head_matches.
_QUESTION_WORDS = (
    "сколько", "какой", "какая", "какие", "каким", "какую", "мой", "моя", "мои", "меня", "мне",
    "был", "было", "были", "весом", "вес", "рекорд", "рекорды", "прошлый", "прошлой", "последний",
    "последней", "раз", "сегодня", "вчера", "жал", "пожал", "выжал", "отжал", "присел", "приседал",
    "тянул", "поднимал", "поднял", "делал", "сделал", "работал", "пм", "1пм", "rm", "1rm",
    "максимум", "макс", "одноповторный", "разовый", "тренировке", "тренировка", "подходов",
    "подходы", "подход", "повторов", "килограмм", "кг", "все", "всего", "там", "уже", "была", "это",
    "этот", "тот",
)
QUESTION_WORDS = {stem(w) for w in _QUESTION_WORDS}
