"""Packaged products: exact numbers from a barcode (Open Food Facts) or a label photo, and the user's memory of them.

- `off_product(code, http)`: the product by barcode from Open Food Facts (None when OFF does not know it).
- `check_label(per100)` / `from_label(...)`: numbers the vision model read off a package; a label whose kcal
  disagree with 4P+9F+4C by more than LABEL_TOLERANCE is "unclear" (the handler asks for another photo).
- `combine(off, label, code)`: OFF wins; a readable label that differs on kcal by more than DIFF_NOTE adds a note.
- `parse_amount(text)` + `grams_for(product, amount)`: "50 г", "1 скуп", "2 порции", "половина", "вся пачка".
- `match(text, products)`: a deterministic match of a short message ("съел тот же батончик", "snickers 50 г",
  "протеин 1 скуп") against saved products. Conservative: any word that is neither the product's name, an
  amount nor a filler word sends the message to the parser instead (which gets `prompt_line` as context).
- `remember(...)`: upsert after "Сохранить"; `updated_at` doubles as "last eaten".
Numbers are per 100 g as printed; a food built from them is never re-estimated (`food_for`).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gymbot.db.models import Product, User
from gymbot.llm.schemas import LabelPer100, ParsedFood, ParsedLabel
from gymbot.services.food_lookup import TIMEOUT, USER_AGENT

log = logging.getLogger(__name__)

OFF_PRODUCT_API = "https://world.openfoodfacts.org/api/v2/product/{code}.json"
OFF_FIELDS = (
    "product_name,product_name_ru,brands,nutriments,quantity,product_quantity,product_quantity_unit,"
    "serving_size,serving_quantity"
)
KJ_PER_KCAL = 4.184
LABEL_TOLERANCE = 0.20  # label kcal vs 4P+9F+4C
DIFF_NOTE = 0.15  # OFF vs label kcal per 100 g
PROMPT_LINE_MAX = 300  # "Мои продукты" for the parser, only products the message names
LIST_MAX = 30  # /products and the matcher look at the newest products only
LABEL_NAME = "Продукт с этикетки"  # a label without a readable name; never merged with another such product
BARE_GRAMS_MIN = 15  # "snickers 2" = two packages, "протеин 30" = 30 g


@dataclass(frozen=True)
class ProductInfo:
    name: str
    brand: str | None
    kcal: float | None  # per 100 g; None = unknown (an OFF entry without nutrition facts)
    protein_g: float | None
    fat_g: float | None
    carbs_g: float | None
    net_weight_g: float | None = None
    serving_g: float | None = None
    barcode: str | None = None
    source: str = "off"  # off | label | manual
    product_id: int | None = None  # the saved row, when it came from the user's products
    aliases: str = ""  # extra names, comma-separated (saved rows only)

    @property
    def complete(self) -> bool:
        return None not in (self.kcal, self.protein_g, self.fat_g, self.carbs_g)

    @property
    def title(self) -> str:
        """"<brand> <name>", without repeating a brand the name already has."""
        if self.brand and self.brand.casefold() not in self.name.casefold():
            return f"{self.brand} {self.name}"
        return self.name


# ---- Open Food Facts ----


def _float(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    try:
        f = float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        return None
    return f if f >= 0 else None


def _positive(v: Any) -> float | None:
    f = _float(v)
    return f if f else None


def _first_brand(brands: Any) -> str | None:
    first = str(brands or "").split(",")[0]
    return " ".join(first.split()) or None


def product_from_off(data: Any, code: str) -> ProductInfo | None:
    """A ProductInfo from an OFF v2 product answer; None when the product is not there."""
    if not isinstance(data, dict) or data.get("status") != 1 or not isinstance(p := data.get("product"), dict):
        return None
    n = p.get("nutriments") if isinstance(p.get("nutriments"), dict) else {}
    kcal = _float(n.get("energy-kcal_100g"))
    if kcal is None and (kj := _float(n.get("energy-kj_100g"))) is not None:
        kcal = round(kj / KJ_PER_KCAL, 1)
    name = " ".join(str(p.get("product_name_ru") or p.get("product_name") or "").split()) or f"Продукт {code}"
    return _sane(ProductInfo(
        name=name[:200],
        brand=_first_brand(p.get("brands")),
        kcal=kcal,
        protein_g=_float(n.get("proteins_100g")),
        fat_g=_float(n.get("fat_100g")),
        carbs_g=_float(n.get("carbohydrates_100g")),
        net_weight_g=_positive(p.get("product_quantity")),
        serving_g=_positive(p.get("serving_quantity")),
        barcode=code,
        source="off",
    ))


async def off_product(code: str, http: httpx.AsyncClient) -> ProductInfo | None:
    """The product by barcode; None when OFF does not know it (404 / status 0). Network errors raise."""
    resp = await http.get(
        OFF_PRODUCT_API.format(code=code),
        params={"fields": OFF_FIELDS},
        headers={"User-Agent": USER_AGENT},
        timeout=TIMEOUT,
    )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return product_from_off(resp.json(), code)


# ---- Label ----


def check_label(per100: LabelPer100) -> bool:
    """Whether the numbers look like a real label: all four, plausible, kcal ≈ 4P+9F+4C within LABEL_TOLERANCE."""
    values = (per100.kcal, per100.protein_g, per100.fat_g, per100.carbs_g)
    if any(v is None for v in values):
        return False
    kcal, p, f, c = values  # type: ignore[misc]
    if kcal > 950 or p + f + c > 105:
        return False
    computed = 4 * p + 9 * f + 4 * c
    if computed == 0:
        return kcal <= 5  # water, diet drinks: nothing to compare with
    return abs(kcal - computed) <= LABEL_TOLERANCE * computed  # symmetric: 20 % above or below the macros


def _sane(product: ProductInfo) -> ProductInfo:
    """No serving as big as the package: "serving 100 g" / "serving 51 g" on a 51 g bar is not a serving."""
    if product.serving_g and product.net_weight_g and product.serving_g >= product.net_weight_g:
        return replace(product, serving_g=None)
    return product


def from_label(label: ParsedLabel, code: str | None = None) -> ProductInfo:
    return _sane(ProductInfo(
        name=label.name or LABEL_NAME,
        brand=label.brand,
        kcal=label.per100.kcal,
        protein_g=label.per100.protein_g,
        fat_g=label.per100.fat_g,
        carbs_g=label.per100.carbs_g,
        net_weight_g=label.net_weight_g,
        serving_g=label.serving_g,
        barcode=code,
        source="label",
    ))


@dataclass(frozen=True)
class Decision:
    product: ProductInfo | None = None
    note: str | None = None
    problem: str | None = None  # the reply instead of a card


LABEL_UNCLEAR = (
    "Не разобрал этикетку: числа не сходятся. Сфотографируй таблицу пищевой ценности ближе и ровнее "
    "или напиши КБЖУ на 100 г словами."
)
NO_NUMBERS = "Нашёл «{name}» по штрихкоду, но без КБЖУ. Сфотографируй таблицу пищевой ценности."
NOT_FOUND = "Штрихкод {code} не нашёл в базе. Сфотографируй таблицу пищевой ценности на упаковке."


def combine(off: ProductInfo | None, label: ParsedLabel | None, code: str | None) -> Decision:
    """What a packaged-product photo gives: OFF first, the label as the fallback and as a cross-check.

    An empty Decision means "no package here": the photo is a plate (or nothing) for the usual food flow.
    """
    label_ok = label is not None and check_label(label.per100)
    if off is not None and off.complete:
        note = None
        if label_ok and off.kcal:
            assert label is not None and label.per100.kcal is not None
            if abs(label.per100.kcal - off.kcal) > DIFF_NOTE * off.kcal:
                note = (
                    f"На этикетке {label.per100.kcal:g} ккал на 100 г, в Open Food Facts {off.kcal:g}. "
                    "Записываю по базе; если на упаковке другое, напиши КБЖУ словами."
                )
        if label is not None:
            off = replace(
                off,
                net_weight_g=off.net_weight_g or label.net_weight_g,
                serving_g=off.serving_g or label.serving_g,
            )
        return Decision(product=_sane(off), note=note)
    if label is not None:
        if not label_ok:
            return Decision(problem=LABEL_UNCLEAR)
        product = from_label(label, code)
        if off is not None:  # OFF knows the name and weights, the label the numbers
            product = replace(
                product,
                name=label.name or off.name,
                brand=label.brand or off.brand,
                net_weight_g=product.net_weight_g or off.net_weight_g,
                serving_g=product.serving_g or off.serving_g,
            )
        return Decision(product=_sane(product))
    if off is not None:
        return Decision(problem=NO_NUMBERS.format(name=off.title))
    return Decision()


# ---- Amounts ----


@dataclass(frozen=True)
class Amount:
    grams: float | None = None
    servings: float | None = None
    packages: float | None = None
    bare: float | None = None  # a number without a unit

    @property
    def empty(self) -> bool:
        return self.grams is None and self.servings is None and self.packages is None and self.bare is None


_TOKEN = re.compile(r"\d+(?:[.,]\d+)?|½|[a-zа-я]+")
_NUMBER_WORDS = {
    "один": 1, "одна": 1, "одну": 1, "одно": 1, "два": 2, "две": 2, "три": 3, "четыре": 4, "пять": 5,
    "полтора": 1.5, "полторы": 1.5, "пол": 0.5, "половина": 0.5, "половину": 0.5, "половинку": 0.5,
    "половинка": 0.5, "½": 0.5, "четверть": 0.25, "треть": 1 / 3,
}  # fmt: skip
_WHOLE = {"вся", "всю", "весь", "целую", "целый", "целиком", "целая"}
_GRAM_UNITS = re.compile(r"^(?:г|гр|грамм\w*|g|gr|мл|ml)$")
_SERVING_UNITS = re.compile(r"^(?:порци\w*|скуп\w*|scoop\w*|serving\w*|мерн\w*|ложк\w*)$")
_PACKAGE_UNITS = re.compile(r"^(?:упаковк\w*|пачк\w*|пачек|батончик\w*|банк[аиуе]?|банок|бутылк\w*|бутылок|шт|штук\w*)$")
_FILLERS = {
    "съел", "съела", "сьел", "поел", "поела", "выпил", "выпила", "скушал", "доел", "перекусил", "съем", "ел",
    "тот", "та", "то", "те", "того", "ту", "той", "такой", "такую", "такого", "такая", "же", "ещё", "еще",
    "и", "мой", "мою", "моего", "моих", "мои", "из", "по", "на", "в", "за", "с", "ну", "вот", "это",
    "запиши", "добавь", "сегодня", "сейчас",
}  # fmt: skip
_SAME = re.compile(r"\b(?:тот|ту|того|той|та|то|такой|такую|такого|такая)\s+же\b")
_KEY_STOP = {"и", "с", "со", "в", "для", "без", "на", "из", "по", "г", "мл", "кг", "л", "шт"}


def tokens(text: str) -> list[str]:
    return _TOKEN.findall(text.casefold().replace("ё", "е"))


def _number(tok: str) -> float | None:
    if tok in _NUMBER_WORDS:
        return float(_NUMBER_WORDS[tok])
    if tok[0].isdigit():
        return float(tok.replace(",", "."))
    return None


@dataclass
class _Scan:
    amount: Amount
    used: set[int] = field(default_factory=set)  # token indexes taken by the amount or fillers


def _scan(toks: list[str]) -> _Scan:
    """Amount and filler tokens of a message; whatever is left is the product's name (or foreign words)."""
    grams = servings = packages = bare = None
    implicit = False  # packages=1 came from a bare package word: a fraction later in the text overrides it
    used: set[int] = set()
    i = 0
    while i < len(toks):
        tok = toks[i]
        n = _number(tok)
        nxt = toks[i + 1] if i + 1 < len(toks) else ""
        # "пол порции", "2 скупа", "50 г", "половину батончика", "1.5 пачки"
        if n is not None and _GRAM_UNITS.match(nxt) and grams is None:
            grams, used = n, used | {i, i + 1}
            i += 2
            continue
        if n is not None and _SERVING_UNITS.match(nxt) and servings is None:
            servings, used = n, used | {i, i + 1}
            i += 2
            continue
        if n is not None and _PACKAGE_UNITS.match(nxt) and packages is None:
            packages, used = n, used | {i, i + 1}  # the unit may also be the product's name: still checked
            i += 2
            continue
        if n is not None:
            if tok[0].isdigit():
                bare = n if bare is None else bare
            elif packages is None or implicit:  # "половина" alone (also "батончик, половину"): of the package
                packages, implicit = n, False
            used.add(i)
        elif tok in _WHOLE:
            packages = 1.0 if packages is None else packages
            used.add(i)
        elif _SERVING_UNITS.match(tok) and servings is None:
            servings = 1.0
            used.add(i)
        elif _PACKAGE_UNITS.match(tok) and packages is None:
            packages, implicit = 1.0, True  # "тот же батончик" = one; not marked used: may be the product's name
        elif _GRAM_UNITS.match(tok) or tok in _FILLERS:
            used.add(i)
        i += 1
    return _Scan(Amount(grams, servings, packages, bare), used)


def parse_amount(text: str) -> tuple[Amount, list[str]]:
    """The amount in `text` and the words left over (neither amount nor filler)."""
    toks = tokens(text)
    scan = _scan(toks)
    return scan.amount, [t for i, t in enumerate(toks) if i not in scan.used and not _PACKAGE_UNITS.match(t)]


def grams_for(product: ProductInfo, amount: Amount) -> float | None:
    """Grams eaten, or None when the amount needs a weight the product does not have (then ask)."""
    if amount.grams is not None:
        return amount.grams
    if amount.servings is not None:
        return amount.servings * product.serving_g if product.serving_g else None
    if amount.packages is not None:
        return amount.packages * product.net_weight_g if product.net_weight_g else None
    if amount.bare is not None:
        if amount.bare >= BARE_GRAMS_MIN:
            return amount.bare
        unit = product.net_weight_g or product.serving_g
        return amount.bare * unit if unit else None
    return None


def food_for(product: ProductInfo, grams: float) -> ParsedFood:
    """The food entry for `grams` of the product: per-100 g numbers as printed, never re-estimated.

    model_construct skips ParsedFood's kcal-from-macros fix: a label may legitimately differ from 4/9/4
    (fiber, polyols), and these numbers are the label's, not a model's guess.
    """
    k = grams / 100
    return ParsedFood.model_construct(
        description=f"{product.title}, {grams:g} г",
        grams=round(grams, 1),
        kcal=round((product.kcal or 0) * k, 1),
        protein_g=round((product.protein_g or 0) * k, 1),
        fat_g=round((product.fat_g or 0) * k, 1),
        carbs_g=round((product.carbs_g or 0) * k, 1),
    )


# ---- The user's products ----


def info(row: Product) -> ProductInfo:
    return ProductInfo(
        name=row.name,
        brand=row.brand,
        kcal=float(row.kcal_100g),
        protein_g=float(row.protein_100g),
        fat_g=float(row.fat_100g),
        carbs_g=float(row.carbs_100g),
        net_weight_g=float(row.net_weight_g) if row.net_weight_g is not None else None,
        serving_g=float(row.serving_g) if row.serving_g is not None else None,
        barcode=row.barcode,
        source=row.source,
        product_id=row.id,
        aliases=row.aliases or "",
    )


async def user_products(session: AsyncSession, telegram_id: int, limit: int = LIST_MAX) -> list[ProductInfo]:
    """The user's products, last eaten first."""
    rows = await session.scalars(
        select(Product)
        .join(User, User.id == Product.user_id)
        .where(User.telegram_id == telegram_id)
        .order_by(Product.updated_at.desc(), Product.id.desc())
        .limit(limit)
    )
    return [info(r) for r in rows]


def _dec(v: float | None, places: str = "0.1") -> Decimal | None:
    return None if v is None else Decimal(str(v)).quantize(Decimal(places))


async def remember(session: AsyncSession, user_id: int, product: ProductInfo, alias: str | None = None) -> Product:
    """Insert or refresh the product (by id, barcode, or brand + name) and mark it as just eaten."""
    row = None
    if product.product_id is not None:
        row = await session.get(Product, product.product_id)
        row = row if row is not None and row.user_id == user_id else None
    if row is None and product.barcode:
        row = await session.scalar(select(Product).where(Product.user_id == user_id, Product.barcode == product.barcode))
    if row is None and product.name != LABEL_NAME:
        for r in await session.scalars(select(Product).where(Product.user_id == user_id)):
            if r.name.casefold() == product.name.casefold() and (r.brand or "").casefold() == (product.brand or "").casefold():
                row = r
                break
    now = datetime.now(UTC)
    if row is None:
        row = Product(user_id=user_id, created_at=now, aliases=None)
        session.add(row)
    if product.product_id is None or row.id is None:  # new numbers from OFF or a label; memory keeps its own
        row.name = product.name[:200]
        row.brand = product.brand[:200] if product.brand else None
        row.barcode = product.barcode or row.barcode
        row.kcal_100g = _dec(product.kcal) or Decimal(0)
        row.protein_100g = _dec(product.protein_g) or Decimal(0)
        row.fat_100g = _dec(product.fat_g) or Decimal(0)
        row.carbs_100g = _dec(product.carbs_g) or Decimal(0)
        row.net_weight_g = _dec(product.net_weight_g) if product.net_weight_g else row.net_weight_g
        row.serving_g = _dec(product.serving_g) if product.serving_g else row.serving_g
        row.source = product.source
    if alias:
        known = [a.strip() for a in (row.aliases or "").split(",") if a.strip()]
        if alias.casefold() not in {a.casefold() for a in known}:
            row.aliases = ", ".join([*known, alias])[:500]
    row.updated_at = now  # explicitly: also "last eaten" when nothing else changed
    await session.flush()
    return row


async def delete(session: AsyncSession, telegram_id: int, product_id: int) -> bool:
    row = await session.scalar(
        select(Product).join(User, User.id == Product.user_id).where(
            Product.id == product_id, User.telegram_id == telegram_id
        )
    )
    if row is None:
        return False
    await session.delete(row)
    return True


# ---- Matching a message against the user's products ----


def same_word(a: str, b: str) -> bool:
    """One word in two forms: "батончик" / "батончика", "протеин" / "протеином"; not "батон" / "батончик"."""
    if a == b:
        return True
    short, long_ = sorted((a, b), key=len)
    return len(short) >= 4 and long_.startswith(short) and len(long_) - len(short) <= 2


def keys(product: ProductInfo) -> set[str]:
    words = tokens(" ".join(filter(None, (product.name, product.brand, product.aliases.replace(",", " ")))))
    return {w for w in words if len(w) >= 3 and not w[0].isdigit() and w not in _KEY_STOP}


@dataclass(frozen=True)
class Match:
    products: list[ProductInfo]  # one = that product; several = ask which (newest first)
    amount: Amount


def match(text: str, products: list[ProductInfo]) -> Match | None:
    """The saved product a short message is about, or None (then the parser handles the message).

    `products` newest first. Fires only when every word is the product's name, an amount or a filler.
    "тот же ..." picks the newest of several matches, or the newest product at all when no name matched.
    Two different products in one message ("сникерс и марс") are the parser's job.
    """
    if not products:
        return None
    toks = tokens(text)
    scan = _scan(toks)
    same = bool(_SAME.search(" ".join(toks)))
    hits: dict[int, set[int]] = {}  # product index -> token indexes naming it
    for pi, product in enumerate(products):
        pk = keys(product)
        hit = {ti for ti, t in enumerate(toks) if len(t) >= 3 and any(same_word(t, k) for k in pk)}
        if hit:
            hits[pi] = hit
    named = set().union(*hits.values()) if hits else set()
    leftover = [t for ti, t in enumerate(toks) if ti not in scan.used and ti not in named]
    # A package word ("батончик") that is no product's name is still an amount, not a foreign word.
    leftover = [t for t in leftover if not _PACKAGE_UNITS.match(t)]
    if leftover:
        return None
    if not hits:
        return Match([products[0]], scan.amount) if same else None
    best = max(len(h) for h in hits.values())
    top = [pi for pi, h in hits.items() if len(h) == best]
    if len(top) == 1:
        return Match([products[top[0]]], scan.amount)
    if len({frozenset(hits[pi]) for pi in top}) > 1:
        return None  # different words name different products: several foods in one message
    if same:
        return Match([products[min(top)]], scan.amount)
    return Match([products[pi] for pi in sorted(top)], scan.amount)


def mentioned(text: str, products: list[ProductInfo]) -> list[ProductInfo]:
    """Products whose name, brand or alias the text mentions (for the parser's context line)."""
    toks = [t for t in tokens(text) if len(t) >= 3]
    found = []
    for p in products:
        pk = keys(p)
        if any(same_word(t, k) for t in toks for k in pk):
            found.append(p)
    return found


def _brief(p: ProductInfo) -> str:
    extra = [f"упаковка {p.net_weight_g:g} г" if p.net_weight_g else "", f"порция {p.serving_g:g} г" if p.serving_g else ""]
    tail = ", ".join(e for e in extra if e)
    return (
        f"{p.title} на 100 г {p.kcal:g} ккал Б{p.protein_g:g} Ж{p.fat_g:g} У{p.carbs_g:g}"
        + (f", {tail}" if tail else "")
    )


def prompt_line(text: str, products: list[ProductInfo]) -> str:
    """'Мои продукты: ...' for the parser with the products the message names, within PROMPT_LINE_MAX; '' if none."""
    line = ""
    for p in mentioned(text, products):
        candidate = f"{line}; {_brief(p)}" if line else f"Мои продукты (точные числа с упаковки): {_brief(p)}"
        if len(candidate) > PROMPT_LINE_MAX:
            break
        line = candidate
    return line
