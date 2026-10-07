"""Post-check of the model's diary answer: claims about logged data must come from the summary (layer 2).

The model once answered «сколько сегодня по тоннажу?» with a bench press and 1800 kg that were nowhere in
the history. `violations` finds such claims in code, without another model:

- Only clauses about the PAST are checked: a clause with a past cue ("сделал", "было", "вчера", "в прошлый
  раз", "тоннаж", "съел", "осталось", "рекорд"). A sentence whose cues are all past is past as a whole; with
  mixed cues each clause takes the state of the nearest cue before it. List items ("- жим: 60×10") take the
  state of the line before the list. A future or advice cue ("ставь", "попробуй", "следующий", "план",
  "рабочий вес", "нужно", "бы"…) makes a clause exempt, so "Сегодня по плану жим 4×8–10, ставь 82,5" passes.
  «Сегодня» alone is no cue: it is as often about the plan as about what was done.
- In a past clause: numbers with a unit or pattern (кг, т, ккал, г, подходы, повторы, упражнения, N×M,
  "тоннаж N") must occur in the evidence (the summary, the question and the user's own earlier turns, never
  the bot's earlier answers), up to the precision they are written with: "6,5 т" matches 6530, "25" matches
  24.7, "17,5" matches "17.5". Percentages, signed numbers (+2,5 кг) and ranges (8–10) are advice and skipped.
- In a past clause: exercise names (catalog, aliases, the main lifts in any case form) must be among the
  exercises with logged sets, in the workout in progress or named by the user.

Bare numbers (dates, times, counts without a unit) are never checked: false alarms cost a regeneration,
a missed claim is still bounded by the prompt.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal

from gymbot.services.answer_intent import mentions, variants
from gymbot.services.programs import normalize

_W = r"(?<![а-яa-z0-9])"
_E = r"(?![а-яa-z0-9])"

_PAST = re.compile(
    _W + r"(?:с?делал\w*|сделано|сделан[аыо]?|выполнил\w*|выполнен\w*|жал[аи]?|пожал\w*|выжал\w*|отжал\w*|"
    r"присел\w*|приседал\w*|тянул\w*|потянул\w*|поднял\w*|поднимал\w*|съел\w*|съедено|поел\w*|наел\w*|набрал\w*|"
    r"было|был|была|были|вчера|позавчера|тоннаж\w*|рекорд\w*|осталось|остается|прошл\w*\s+раз\w*|"
    r"прошл\w+\s+тренировк\w*|последн\w+\s+(?:раз|тренировк\w*)|в\s+истории|по\s+истории|в\s+дневнике|"
    r"по\s+дневнику|итог\w*|потренил\w*|тренировал\w*|занимал\w*)" + _E
)
_FUTURE = re.compile(
    _W + r"(?:постав\w*|ставь\w*|ставить|став[ия]м?|попроб\w*|следующ\w*|рабоч\w+\s+вес\w*|начни\w*|начинай\w*|"
    r"начать|добавь\w*|добавлять|добавить|прибавь\w*|прибавить|прибавлять|сделай\w*|делай\w*|сделать|делать|"
    r"можно|нужно|надо|стоит|цел[ьи]\w*|план(?:е|у|а|ом|ы)?|программ\w*|завтра|будет|будешь|буд[уе]м?|давай|"
    r"рекоменд\w*|лучше|возьми|бери|бер[её]шь|увелич\w*|сниз\w*|снижай|уменьш\w*|добер\w*|добрать|держи|оставь|"
    r"оставляй|если|бы|целься|жми|присядь|подними|сможешь|получится|должн\w*|советую|предлагаю|дальше|потом|"
    r"съешь|съесть|поешь|доешь|доесть|на\s+ужин|на\s+обед|на\s+завтрак|перекус\w*|примерно|рабоч\w*)" + _E
)
# "около 60 г", "≈ 6,5 т": a rounded figure, matched within HEDGE_TOLERANCE.
_HEDGE = re.compile(r"(?:около|приблизительно|порядка|почти|где-то|≈|~)\s*$")
HEDGE_TOLERANCE = 0.05

_SENTENCE = re.compile(r"(?<!упр\.)(?<!подх\.)(?<!повт\.)(?<=[.!?…])\s+")
_CLAUSE = re.compile(r",\s+|;\s*|\s+[—–-]\s+|:\s+|\s+(?:а|но|однако|поэтому|зато|так что)\s+")
_NEGATED = re.compile(_W + r"(?:не|нет|ни)" + _E)  # "жим лёжа ты не делал": an exercise named as absent
_ITEM = re.compile(r"^\s*(?:[-–—•*]|\d+[.)])\s+")

_NUM = r"\d+(?:[ \u00a0\u202f]\d{3})*(?:[.,]\d+)?"
_UNIT = (
    r"кг|килограмм\w*|т|тонн\w*|ккал|калори\w*|кал|г|гр|грамм\w*|подход\w*|подх|сет\w*|повтор\w*|повт|"
    r"упражнени\w*|упр"
)
_PAIR = re.compile(rf"(?P<a>{_NUM})\s*[×xх*]\s*(?P<b>{_NUM})")
_WITH_UNIT = re.compile(rf"(?P<n>{_NUM})\s*(?P<u>{_UNIT})(?![а-яa-z0-9])")
_TONNAGE = re.compile(rf"тоннаж\w*\D{{0,15}}?(?P<n>{_NUM})")
_EVIDENCE_NUM = re.compile(rf"{_NUM}|\d+(?:[.,]\d+)?")
_SIGNED = re.compile(r"[+−]\s*$|(?:^|[\s(])[-–]\s*$")  # "+2,5", "−10": a change, not a value
_RANGE_LEFT = re.compile(r"\d\s*[-–—]\s*$")  # "8–" before the number: the end of a range
_RANGE_RIGHT = re.compile(r"^\s*(?:[-–—]|до)\s*\d")  # the start of a range ("8–10", "60 до 70")


@dataclass
class Evidence:
    """What a past claim may rest on. `text`: the summary, the question and the user's earlier turns;
    `history`: exercise names with logged sets or in the workout in progress; `names`, `aliases`: the
    catalog for finding exercise names in the answer."""

    text: str
    history: set[str]
    names: list[str] = field(default_factory=list)
    aliases: dict[str, list[str]] = field(default_factory=dict)
    _values: list[float] | None = None

    def values(self) -> list[float]:
        if self._values is None:
            found = {_value(m.group(0)) for m in _EVIDENCE_NUM.finditer(self.text)}
            # "6 530" may also be two numbers: keep the parts too, more evidence is only more lenient.
            found |= {_value(p) for m in _EVIDENCE_NUM.finditer(self.text) for p in re.split(r"[ \u00a0\u202f]", m.group(0))}
            self._values = sorted(found)
        return self._values


def _value(token: str) -> float:
    return float(re.sub(r"[ \u00a0\u202f]", "", token).replace(",", "."))


def _unit_of(token: str, scale: int = 1) -> Decimal:
    """The precision a number is written with: '17,5' -> 0.1, '60' -> 1, '1800' -> 100, '6,5' t -> 100 kg."""
    t = re.sub(r"[ \u00a0\u202f]", "", token).replace(",", ".")
    if "." in t:
        return Decimal(1).scaleb(-len(t.split(".")[1])) * scale
    zeros = len(t) - len(t.rstrip("0"))
    if scale == 1 and len(t) >= 3 and zeros:
        return Decimal(10) ** min(zeros, len(t) - 2)  # 880 -> 10, 1800 -> 100, 60 -> 1
    return Decimal(scale)


def supported(token: str, values: list[float], scale: int = 1, hedged: bool = False) -> bool:
    """Whether the number `token` (times `scale`, 1000 for tonnes) is some evidence value rounded to the
    precision it is written with; `hedged` ("около 60") also takes values within HEDGE_TOLERANCE."""
    v = Decimal(str(_value(token))) * scale
    if v == 0:  # "0 подходов" says there is nothing
        return True
    if hedged and any(abs(float(v) - c) <= HEDGE_TOLERANCE * max(abs(float(v)), abs(c)) for c in values):
        return True
    unit = _unit_of(token, scale)
    for c in values:
        rounded = (Decimal(str(c)) / unit).quantize(Decimal(1), rounding="ROUND_HALF_UP") * unit
        if rounded == v:
            return True
    return False


def _state(clause: str) -> str | None:
    if _FUTURE.search(clause):
        return "future"
    if _PAST.search(clause):
        return "past"
    return None


def past_clauses(answer: str) -> list[str]:
    """The clauses of `answer` that state something about the past (see the module doc), normalized."""
    out: list[str] = []
    header: str | None = None
    for raw in answer.splitlines():
        line = normalize(raw)
        if not line:
            continue
        item = bool(_ITEM.match(line))
        carry = header if item else None
        cur: str | None = None
        for sentence in _SENTENCE.split(line):
            clauses = [c for c in _CLAUSE.split(sentence) if c.strip()]
            states = [_state(c) for c in clauses]
            if "past" in states and "future" not in states:
                states = ["past"] * len(clauses)
            cur = carry
            for clause, state in zip(clauses, states, strict=True):
                if state is not None:
                    cur = state
                if cur == "past":
                    out.append(clause)
            carry = None
        if not item:
            header = cur
    return out


def _skipped(clause: str, start: int, end: int) -> bool:
    """A change (+2,5), a percentage or a range end: advice, not a value from the diary."""
    before, after = clause[:start], clause[end:]
    return bool(
        _SIGNED.search(before) or after.lstrip().startswith("%") or _RANGE_LEFT.search(before)
        or _RANGE_RIGHT.match(after)
    )


def _numbers(clause: str) -> list[tuple[str, list[tuple[str, int]], bool]]:
    """(the claim as written, [(number, scale)], hedged) for every number with a unit or pattern."""
    claims: list[tuple[str, list[tuple[str, int]], bool]] = []

    def hedged(start: int) -> bool:
        return bool(_HEDGE.search(clause[max(0, start - 16):start]))
    taken: list[tuple[int, int]] = []

    def free(a: int, b: int) -> bool:
        return all(b <= x or a >= y for x, y in taken)

    for m in _PAIR.finditer(clause):
        taken.append((m.start(), m.end()))
        if not _skipped(clause, m.start(), m.end()):  # "4×8–10" (sets × a rep range) is a prescription
            claims.append((m.group(0), [(m["a"], 1), (m["b"], 1)], hedged(m.start())))
    for m in _WITH_UNIT.finditer(clause):
        if not free(m.start(), m.end()):
            continue
        taken.append((m.start(), m.end()))
        if _skipped(clause, m.start(), m.end("n")):
            continue
        unit = m["u"]
        scale = 1000 if unit == "т" or unit.startswith("тонн") else 1
        claims.append((m.group(0), [(m["n"], scale)], hedged(m.start())))
    for m in _TONNAGE.finditer(clause):
        if free(m.start("n"), m.end("n")) and not _skipped(clause, m.start("n"), m.end("n")):
            claims.append((m.group(0), [(m["n"], 1)], hedged(m.start("n"))))
    return claims


def violations(answer: str, ev: Evidence) -> list[str]:
    """The claims in `answer` about the past that the evidence does not hold, as written (deduplicated)."""
    flags: list[str] = []
    history = {normalize(n) for n in ev.history}
    values = ev.values()
    for clause in past_clauses(answer):
        for name in [] if _NEGATED.search(clause) else mentions(clause, ev.names, ev.aliases):
            if normalize(name) not in history and not variants(name, ev.history):
                flags.append(name)
        for written, numbers, hedge in _numbers(clause):
            if not all(supported(n, values, scale, hedge) for n, scale in numbers):
                flags.append(written)
    return list(dict.fromkeys(flags))


def correction(flags: list[str]) -> str:
    """The user turn appended for the one regeneration."""
    return f"В ответе есть данные, которых нет в сводке: {', '.join(flags)}. Ответь только по сводке."
