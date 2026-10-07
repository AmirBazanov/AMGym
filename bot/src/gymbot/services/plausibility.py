"""Sanity checks for the parser's food estimates (text and photo plates): catch "2 самсы = 2116 ккал".

ParsedFood already trusts the macros over a kcal that contradicts them; this layer catches macros that are
absurd themselves. Only model estimates go through it: label / Open Food Facts numbers (handlers/products.py)
and foods that copy a saved product's exact numbers (`exact`) are never touched.

1. `flags(food)`: what looks wrong with one food.
   - Density (needs grams): kcal per gram above DENSITY_MAX (FAT_DENSITY_MAX for fats, nuts, seeds, chocolate,
     sweets by name stems) or below DENSITY_MIN (drinks, water, broth may be ~0); P+F+C over MACROS_MAX g per g.
     Tiny items are never flagged: the excess has to be more than SLACK_KCAL (SLACK_G for macros).
   - Reference (`REFERENCES`, the parser prompt's portions glossary plus a few common foods): when the head word
     (the first one: the parser writes "самса с курицей, 2 шт") matches a reference, kcal above REF_HIGH x or
     below 1/REF_LOW of the reference for the amount. An ingredient never counts ("борщ с хлебом", "суп с
     яйцом"), nor does a head with a qualifier that makes it another food ("яйца перепелиные", "шашлык из
     овощей": MODIFIER_STEMS) or a fat or sweet stem ("хлеб с маслом"): those get the density check only.
     The amount is the food's grams (users state their own piece sizes, "манты по 90 г", "17 штук, 250 г"),
     else N pieces from ", N шт" / "палочки" / "куска" / "порции" times the reference piece, else one portion.
2. `review(result, reparse)`: on any flag, one repair round: `reparse(correction)` asks the parser again with
   the flagged answer as the previous turn and a short correction («Оценка «самса, 2 шт — 2116 ккал»
   неправдоподобна: обычно самса ≈ 300 ккал за шт (120 г). Пересчитай КБЖУ.»). A flagged item that the repair
   fixed takes the repaired numbers (repaired items are matched by head word, never by position: a reordered
   answer must not give the samsa the tea's numbers); one still flagged (or a failed repair) takes the reference scaled to its
   amount (grams or pieces, never an assumed portion: `stated`) with "Скорректировал по справочнику: …" in the
   note, else keeps the numbers with "Проверь калории —
   оценка выглядит завышенной / заниженной (…)". Saving is never blocked. Everything else of the original
   result (kind, revises, remember, unknown words, the question) stays: the correction is the bot's, not
   the user's, so the repair's `revises` must not replace another preview.
Flags are logged without any text: kind, direction, ratio, reference key.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from gymbot.llm.openrouter import LLMError, OpenRouterClient
from gymbot.llm.schemas import ParsedFood, ParseResult

log = logging.getLogger(__name__)

DENSITY_MAX = 6.5  # kcal/g: nuts ~6.5, chocolate ~5.5, chips ~5.4
FAT_DENSITY_MAX = 9.0  # pure fat
DENSITY_MIN = 0.1  # kcal/g: lettuce and cucumber ~0.15
MACROS_MAX = 1.05  # g of P+F+C per g of food: sugar and oil are 1.0 before rounding
SLACK_KCAL = 40.0  # a flag needs at least this many kcal beyond the bound (a teaspoon of sauce never flags)
SLACK_G = 5.0
REF_HIGH = 2.0  # kcal above reference x this = too high
REF_LOW = 2.5  # kcal below reference / this = too low

# Name stems (word starts, after casefold and ё -> е) of foods that may be denser than DENSITY_MAX.
FAT_STEMS = (
    "масл", "орех", "арахис", "семечк", "шоколад", "сало", "майонез", "чипс", "халв", "козинак",
    "кешью", "миндал", "фундук", "фисташ", "кедров", "пекан", "макадам", "кокос", "кунжут", "тахин", "урбеч",
    "бекон", "жир", "гхи", "смалец", "шпик",
)
# Stems of what may have ~0 kcal per gram.
DRINK_STEMS = (
    "вод", "чай", "чая", "чаю", "кофе", "американо", "эспрессо", "минерал", "газиров", "кола", "пепси", "зеро",
    "zero", "лайт", "light", "диет", "бульон", "лед", "льда", "энергетик", "стеви", "подсласт", "сукралоз",
    "лимонад", "морс", "компот", "квас", "айран", "дюшес",  # not кефир, катык: never ~0
)
# Words after the head that make it another food than its reference ("яйца перепелиные", "яблоки сушёные",
# "шашлык из овощей", "яйцо, только белок").
MODIFIER_STEMS = (
    "перепел", "сушен", "сушк", "вялен", "овощ", "гриб", "белок", "белк", "желт", "сок", "пюре", "джем",
    "варень", "повидл", "цукат", "мини",
)
_WORD = re.compile(r"[a-zа-я]+")
_PIECES = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:шт|штук|палоч|куск|кусоч|порци)")


@dataclass(frozen=True)
class Reference:
    key: str  # the name in corrections and notes
    pattern: str  # full match on one word of the description
    kcal: float  # per 100 g; macros per 100 g agree with it within 15% (ParsedFood would rewrite kcal)
    protein_g: float
    fat_g: float
    carbs_g: float
    piece_g: float  # one piece, or one usual portion (a каса) when the food is not counted in pieces
    unit: str = "шт"  # "шт" | "порция"

    @property
    def per_piece(self) -> float:
        return self.kcal * self.piece_g / 100

    def usual(self) -> str:
        """"≈ 300 ккал за шт (120 г)" / "≈ 180 ккал на 100 г" for corrections and notes."""
        if self.unit == "шт":
            return f"≈ {self.per_piece:.0f} ккал за шт ({self.piece_g:g} г)"
        return f"≈ {self.kcal:.0f} ккал на 100 г"


# Typical values; the glossary weights are the parser prompt's (gymbot.llm.prompts SYSTEM_PROMPT "Порции").
REFERENCES: tuple[Reference, ...] = (
    Reference("самса", r"самс\w*|самбус\w*", 250, 9, 13, 25, 120),
    Reference("лепёшка", r"лепешк\w*|лепешечк\w*", 260, 8.8, 1.6, 52, 250),
    Reference("плов", r"плов\w*", 180, 6, 7, 23, 300, "порция"),
    Reference("манты", r"мант\w*", 220, 10, 11, 20, 60),
    Reference("чучвара", r"чучвар\w*", 220, 10, 10, 22, 250, "порция"),
    Reference("лагман", r"лагман\w*", 130, 6, 5, 15, 300, "порция"),
    Reference("шурпа", r"шурп\w*|шорп\w*", 90, 5, 6, 4, 300, "порция"),
    Reference("мастава", r"мастав\w*", 80, 4, 3.5, 8.5, 300, "порция"),
    Reference("димлама", r"димлам\w*", 100, 6, 6, 6, 300, "порция"),
    Reference("нарын", r"нарын\w*", 200, 12, 10, 15, 250, "порция"),
    Reference("шашлык", r"шашлык\w*", 250, 20, 18.5, 1.5, 100),
    Reference("курт", r"курт(?:а|ы|ов|ом|у|е|ик|ика|ики|иков)?|курут\w*", 260, 25, 15, 3, 25),
    Reference("катык", r"катык\w*|катик\w*", 60, 3, 3.2, 4, 200, "порция"),
    Reference("казы", r"казы|кази", 400, 15, 37.5, 0, 50, "порция"),
    Reference("чак-чак", r"чакчак\w*", 450, 6, 22, 58, 50, "порция"),
    Reference("беляш", r"беляш\w*", 290, 10, 17, 24, 100),
    Reference("пельмени", r"пельмен\w*", 250, 11, 12.5, 24, 12),
    Reference("яйцо", r"яйц[оаеу]?|яйцом|яиц", 150, 12.7, 10.6, 0.7, 50),
    Reference("банан", r"банан\w*", 90, 1.2, 0.3, 21, 115),
    Reference("яблоко", r"яблок\w*", 52, 0.4, 0.4, 11.5, 180),
    Reference("хлеб", r"хлеб(?:а|у|ом|е)?", 250, 8, 3, 48, 30),
)
_REF_PATTERNS = [(ref, re.compile(ref.pattern)) for ref in REFERENCES]

Kind = Literal["density", "macros", "reference"]


@dataclass(frozen=True)
class Flag:
    kind: Kind
    high: bool  # True: the estimate is too high
    ratio: float  # estimate / bound (density, macros) or / reference
    ref: Reference | None = None
    expected_kcal: float | None = None  # reference kcal for the amount
    density: float | None = None  # kcal per gram (density)


def words(text: str) -> list[str]:
    return _WORD.findall(text.casefold().replace("ё", "е").replace("-", ""))


def _has_stem(ws: list[str], stems: tuple[str, ...]) -> bool:
    return any(w.startswith(s) for w in ws for s in stems)


def head(description: str) -> str:
    """The head word of a description: the first one ("самса с курицей, 2 шт" -> "самса")."""
    ws = words(description)
    return ws[0] if ws else ""


def same_head(a: str, b: str) -> bool:
    """Two descriptions name the same food: their head words share a stem ("самса" / "самсы", "яйцо" / "яйца")."""
    x, y = head(a), head(b)
    if not x or not y:
        return False
    n = max(3, min(5, min(len(x), len(y)) - 1))
    return x[:n] == y[:n]


def reference_for(description: str) -> Reference | None:
    """The reference of the head word; None for none, or when another word makes it another food (a fat or
    sweet stem, MODIFIER_STEMS). Other words never match: "суп с яйцом" is not an egg."""
    ws = words(description)
    if not ws or _has_stem(ws, FAT_STEMS) or _has_stem(ws[1:], MODIFIER_STEMS):
        return None
    found = [ref for ref, rx in _REF_PATTERNS if rx.fullmatch(ws[0])]
    return found[0] if len(found) == 1 else None


def pieces(description: str) -> float | None:
    """N of ", N шт" (the parser's convention for counted foods), "3 палочки", "2 куска", "2 порции"."""
    m = _PIECES.search(description.casefold())
    return float(m.group(1).replace(",", ".")) if m else None


def amount_g(food: ParsedFood, ref: Reference) -> float:
    """Grams the reference applies to: the food's grams, else N pieces (or one portion) x the reference piece.

    Grams win over pieces: "17 штук, 250 г" are small ones, and absurd grams are the density check's job."""
    if food.grams and food.grams > 0:
        return food.grams
    return (pieces(food.description) or 1) * ref.piece_g


def stated(food: ParsedFood) -> bool:
    """Whether the food has an amount (grams or ", N шт"): only then may the reference replace its numbers.
    One portion is the bot's guess: "плов, 3 касы" without grams must not become one каса."""
    return bool(food.grams and food.grams > 0) or pieces(food.description) is not None


def flags(food: ParsedFood) -> list[Flag]:
    """What looks implausible about one estimated food (see the module doc); [] when it looks fine."""
    found: list[Flag] = []
    ws = words(food.description)
    grams = food.grams or 0
    if grams > 0:
        top = FAT_DENSITY_MAX if _has_stem(ws, FAT_STEMS) else DENSITY_MAX
        density = food.kcal / grams
        if food.kcal - top * grams > SLACK_KCAL:
            found.append(Flag("density", True, density / top, density=density))
        elif not _has_stem(ws, DRINK_STEMS) and DENSITY_MIN * grams - food.kcal > SLACK_KCAL:
            found.append(Flag("density", False, density / DENSITY_MIN, density=density))
        macros = food.protein_g + food.fat_g + food.carbs_g
        if macros - MACROS_MAX * grams > SLACK_G:
            found.append(Flag("macros", True, macros / grams / MACROS_MAX))
    if (ref := reference_for(food.description)) is not None:
        expected = ref.kcal * amount_g(food, ref) / 100
        ratio = food.kcal / expected if expected else 1.0
        off = abs(food.kcal - expected) > SLACK_KCAL
        if off and ratio > REF_HIGH:
            found.append(Flag("reference", True, ratio, ref, expected))
        elif off and ratio < 1 / REF_LOW:
            found.append(Flag("reference", False, ratio, ref, expected))
    return found


def is_high(fs: list[Flag]) -> bool:
    return any(f.high for f in fs)


def correction(food: ParsedFood, fs: list[Flag]) -> str:
    """One sentence for the repair round about one flagged food."""
    amount = f" {food.grams:g} г" if food.grams else ""
    head = f"Оценка «{food.description}{amount} — {food.kcal:.0f} ккал» неправдоподобна"
    ref = next((f.ref for f in fs if f.ref is not None), None)
    if ref is not None:
        return f"{head}: обычно {ref.key} {ref.usual()}."
    density = next((f for f in fs if f.kind == "density"), None)
    if density is not None and density.density is not None:
        if density.high:
            return f"{head}: это {density.density:.1f} ккал на грамм, больше, чем у масла и орехов."
        return f"{head}: это почти ноль калорий для еды."
    return f"{head}: белков, жиров и углеводов больше, чем сам вес."


def correction_text(items: list[tuple[ParsedFood, list[Flag]]]) -> str:
    return " ".join(correction(food, fs) for food, fs in items) + " Пересчитай КБЖУ и верни ПОЛНУЮ запись."


def from_reference(food: ParsedFood, ref: Reference) -> ParsedFood:
    """The food with the reference's numbers for its amount (grams, else pieces or one portion)."""
    g = amount_g(food, ref)
    k = g / 100
    return ParsedFood(
        description=food.description,
        grams=round(g),
        kcal=round(ref.kcal * k),
        protein_g=round(ref.protein_g * k, 1),
        fat_g=round(ref.fat_g * k, 1),
        carbs_g=round(ref.carbs_g * k, 1),
    )


def _log(fs: list[Flag], stage: str) -> None:
    for f in fs:  # no description: it is the user's text in other words
        log.info(
            "food plausibility %s: %s %s x%.2f ref=%s",
            stage, f.kind, "high" if f.high else "low", f.ratio, f.ref.key if f.ref else "-",
        )


Reparse = Callable[[str], Awaitable[ParseResult]]


def _match(food: ParsedFood, repaired: list[ParsedFood], used: set[int]) -> int | None:
    """The repaired item for `food`: the first unused one with the same head word, else None (never by position:
    "[самса, чай]" may come back as "[чай, самса]")."""
    return next((j for j, r in enumerate(repaired) if j not in used and same_head(r.description, food.description)), None)


async def review(
    result: ParseResult, reparse: Reparse | None, exact: Callable[[ParsedFood], bool] | None = None
) -> ParseResult:
    """`result` with implausible food estimates repaired (see the module doc); unchanged when nothing is flagged.

    `reparse(correction)` is the parser asked again with `result` as its previous answer; None = no repair round
    (straight to the reference or the warning). `exact(food)`: the food holds a saved product's exact numbers."""
    if result.kind != "food" or not result.foods:
        return result
    checked = [[] if exact is not None and exact(f) else flags(f) for f in result.foods]
    if not any(checked):
        return result
    for fs in checked:
        _log(fs, "flag")
    repaired: list[ParsedFood] = []
    if reparse is not None:
        bad = [(f, fs) for f, fs in zip(result.foods, checked, strict=True) if fs]
        try:
            again = await reparse(correction_text(bad))
        except LLMError as e:
            log.warning("food plausibility: repair failed: %s", type(e).__name__)
        else:
            if again.kind == "food" and again.foods:
                repaired = again.foods
            else:
                log.info("food plausibility: repair answer unusable (%s, %s foods)", again.kind, len(again.foods))
    foods: list[ParsedFood] = []
    notes: list[str] = []
    high: list[str] = []
    low: list[str] = []
    used: set[int] = set()
    for food, fs in zip(result.foods, checked, strict=True):
        if not fs:
            foods.append(food)
            continue
        j = _match(food, repaired, used)
        if j is not None:
            used.add(j)
            if not (still := flags(repaired[j])):
                log.info("food plausibility: repaired")
                foods.append(repaired[j].model_copy(update={"description": food.description}))
                continue
            _log(still, "after repair")
        elif repaired:
            log.info("food plausibility: repair has no matching item")
        ref = next((f.ref for f in fs if f.ref is not None), None) or reference_for(food.description)
        if ref is not None and stated(food):
            fixed = from_reference(food, ref)
            log.info("food plausibility: reference %s used", ref.key)
            foods.append(fixed)
            notes.append(f"{food.description} {food.kcal:.0f} → {fixed.kcal:.0f} ккал (обычно {ref.key} {ref.usual()})")
        else:
            foods.append(food)
            (high if is_high(fs) else low).append(food.description)
    lines = [result.note] if result.note else []
    if notes:
        lines.append("Скорректировал по справочнику: " + "; ".join(notes) + ".")
    if high:
        lines.append(f"Проверь калории — оценка выглядит завышенной ({', '.join(high)}).")
    if low:
        lines.append(f"Проверь калории — оценка выглядит заниженной ({', '.join(low)}).")
    return result.model_copy(update={"foods": foods, "note": "\n".join(lines) or None})


def parser_reparse(
    llm: OpenRouterClient,
    text: str,
    result: ParseResult,
    history: list[tuple[str, str]] | None,
    facts: list[str] | None,
) -> Reparse:
    """The repair round over the parser itself: `result` becomes the answer to `text` in the dialog.

    The exercise catalog is left out (a correction is never a workout; the digits in it would add it)."""

    async def run(correction: str) -> ParseResult:
        turns = [*(history or []), (text, result.model_dump_json(exclude_defaults=True))]
        return await llm.parse_message(correction, [], turns, facts)

    return run
