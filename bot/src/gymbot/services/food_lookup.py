"""Unknown dish -> web lookup -> 2-3 variants with macros for the user to pick.

When the parser does not know a word ("кутаб", "тандыр-гошт"), it returns it in
ParseResult.unknown_terms instead of inventing macros. The bot then:
  1. `lookup(term, http)`: searches sources that need no key, concurrently:
     - Open Food Facts, the classic search (world.openfoodfacts.org/cgi/search.pl) and the newer
       search service (search.openfoodfacts.org; ru.openfoodfacts.org answered 503 when this was written):
       products with macros per 100 g;
     - ru.wikipedia.org: what the dish is (opensearch, then the intro of the article);
     - Tavily web search, only when TAVILY_API_KEY is set.
     A failed source is logged as a warning (no stack) and skipped; results are cached per process.
  2. `suggest(term, sources, llm, phrase)`: the LLM turns the excerpts into up to 3 variants with macros
     for one piece or one usual portion (LOOKUP_SYSTEM_PROMPT); every variant is validated on its own.
  3. `amount(phrase, term)` + `as_food(option, amount)`: the count ("5 маленьких куртов") or grams
     ("тандыр-гошт 200 г") from the user's phrase applied to the picked variant.
The handler (gymbot.handlers.log_text) shows the variants as buttons; nothing is saved without "Сохранить".
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from gymbot.llm.openrouter import LLMError
from gymbot.llm.prompts import build_lookup_messages
from gymbot.llm.schemas import ParsedFood

log = logging.getLogger(__name__)

USER_AGENT = "GymAPP/0.1 (personal diary)"  # Open Food Facts and Wikimedia ask for a descriptive one
TIMEOUT = 8.0
PAGE_SIZE = 5
SNIPPET_MAX = 400
MAX_OPTIONS = 3
CACHE_MAX = 200
NO_SOURCE_NOTE = "оценка без источника"

OFF_SEARCH_URL = "https://world.openfoodfacts.org/cgi/search.pl"
OFF_SEARCH2_URL = "https://search.openfoodfacts.org/search"
OFF_PRODUCT_URL = "https://world.openfoodfacts.org/product/"
WIKI_API_URL = "https://ru.wikipedia.org/w/api.php"
TAVILY_URL = "https://api.tavily.com/search"


class Macros(BaseModel):
    kcal: float = Field(ge=0)
    protein_g: float = Field(ge=0)
    fat_g: float = Field(ge=0)
    carbs_g: float = Field(ge=0)


@dataclass(frozen=True)
class Source:
    title: str
    snippet: str
    per100: Macros | None
    url: str


class Option(BaseModel):
    """One variant for one piece or one usual portion."""

    name: str = Field(min_length=1, max_length=60)
    portion_g: float = Field(gt=0, le=3000)  # multiplied by the count from the phrase, so never 0
    kcal: float = Field(ge=0, le=10000)
    protein_g: float = Field(ge=0, le=1000)
    fat_g: float = Field(ge=0, le=1000)
    carbs_g: float = Field(ge=0, le=1000)
    note: str = ""

    @field_validator("name", mode="before")
    @classmethod
    def _name(cls, v: Any) -> Any:
        return " ".join(v.split()) if isinstance(v, str) else v

    @field_validator("note", mode="before")
    @classmethod
    def _note(cls, v: Any) -> str:
        return " ".join(v.split())[:120] if isinstance(v, str) else ""

    @model_validator(mode="after")
    def _kcal_from_macros(self) -> Option:
        """Same rule as ParsedFood, so the button shows the kcal the record will get."""
        computed = 4 * self.protein_g + 9 * self.fat_g + 4 * self.carbs_g
        if computed > 0 and abs(self.kcal - computed) > 0.15 * computed:
            self.kcal = round(computed)
        return self


class JSONModel(Protocol):
    async def complete_json(self, messages: list[dict[str, str]], *, purpose: str = "json") -> dict: ...


# ---- terms and amounts ----

_TRAILING_VOWEL = re.compile(r"[аяоеёуюыиэйь]$")


def normalize(term: str) -> str:
    return " ".join(term.casefold().replace("ё", "е").split())


def stem(term: str) -> str:
    """First word without a trailing vowel: "косушка" -> "косушк", so "косушку" and "косушки" match too."""
    words = normalize(term).split()
    if not words:
        return ""
    first = words[0]
    return _TRAILING_VOWEL.sub("", first) if len(first) > 3 else first


_NUM_WORDS = {
    "один": 1, "одна": 1, "одну": 1, "одно": 1, "два": 2, "две": 2, "пара": 2, "пару": 2, "три": 3,
    "четыре": 4, "пять": 5, "шесть": 6, "семь": 7, "восемь": 8, "девять": 9, "десять": 10,
    "пол": 0.5, "полтора": 1.5, "полторы": 1.5,
}  # fmt: skip
_NUM = r"\d+(?:[.,]\d+)?|" + "|".join(sorted(_NUM_WORDS, key=len, reverse=True))
# "5 маленьких солёных куртов": up to two adjectives between the number and the word.
_ADJ = r"(?:[^\W\d]+(?:ых|их|ый|ий|ой|ая|яя|ое|ее|ые|ие|ую|юю|ого|его)\s+){0,2}"
_GRAMS = r"(?:г|гр|грамм[^\W\d]*)(?![^\W\d])"


@dataclass(frozen=True)
class Amount:
    count: float = 1  # pieces or portions
    grams: float | None = None  # an explicit weight wins over the count


def _number(s: str) -> float:
    return _NUM_WORDS.get(s) or float(s.replace(",", "."))


def amount(phrase: str, term: str) -> Amount:
    """How much of `term` the phrase mentions: "5 маленьких куртов" -> 5 pieces, "тандыр-гошт 200 г" -> 200 g."""
    text = normalize(phrase)
    st = stem(term)
    if not st:
        return Amount()
    word = rf"\b{re.escape(st)}[\w-]*"
    # "2 кутаба по 150 г": pieces of a known weight.
    each = rf"\b(?P<n>{_NUM})\s+{_ADJ}{word}[^\d\n]{{0,15}}?\bпо\s+(?P<g>\d+(?:[.,]\d+)?)\s*{_GRAMS}"
    if (m := re.search(each, text)) and (g := _number(m["n"]) * _number(m["g"])) > 0:
        return Amount(grams=g)
    patterns_grams = (
        rf"{word}[^\d\n]{{0,25}}?\b(?P<n>\d+(?:[.,]\d+)?)\s*{_GRAMS}",
        rf"\b(?P<n>\d+(?:[.,]\d+)?)\s*{_GRAMS}\s+{_ADJ}{word}",
    )
    for p in patterns_grams:
        if (m := re.search(p, text)) and (g := _number(m["n"])) > 0:
            return Amount(grams=g)
    patterns_count = (
        rf"\b(?P<n>{_NUM})\s+{_ADJ}{word}",
        rf"{word}\s+(?P<n>{_NUM})\s*(?:шт|штук[^\W\d]*)",
    )
    for p in patterns_count:
        if (m := re.search(p, text)) and (n := _number(m["n"])) > 0:
            return Amount(count=n)
    return Amount()


def as_food(option: Option, amt: Amount) -> ParsedFood:
    """The picked variant as a record line: N pieces ("курт, 5 шт") or the weight from the phrase."""
    if amt.grams:
        k, grams, description = amt.grams / option.portion_g, amt.grams, option.name
    else:
        k, grams = amt.count, option.portion_g * amt.count
        description = option.name if amt.count == 1 else f"{option.name}, {amt.count:g} шт"
    return ParsedFood(
        description=description,
        grams=round(grams),
        kcal=round(option.kcal * k),
        protein_g=round(option.protein_g * k, 1),
        fat_g=round(option.fat_g * k, 1),
        carbs_g=round(option.carbs_g * k, 1),
    )


# ---- sources ----


def _float(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f >= 0 else None


def _per100(nutriments: Any) -> Macros | None:
    """Macros per 100 g when all four are there (values may come as strings)."""
    if not isinstance(nutriments, dict):
        return None
    keys = ("energy-kcal_100g", "proteins_100g", "fat_100g", "carbohydrates_100g")
    values = [_float(nutriments.get(k)) for k in keys]
    if any(v is None for v in values):
        return None
    kcal, protein, fat, carbs = values
    return Macros(kcal=kcal, protein_g=protein, fat_g=fat, carbs_g=carbs)  # type: ignore[arg-type]


def _off_sources(products: Any, term: str) -> list[Source]:
    st = stem(term)
    sources = []
    for p in products if isinstance(products, list) else []:
        name = " ".join(str(p.get("product_name") or "").split()) if isinstance(p, dict) else ""
        if not name or st not in normalize(name):  # the search also returns unrelated products
            continue
        quantity = str(p.get("quantity") or "").strip()
        code = str(p.get("code") or "").strip()
        sources.append(
            Source(
                title=f"Open Food Facts: {name}",
                snippet=f"упаковка {quantity}" if quantity else "",
                per100=_per100(p.get("nutriments")),
                url=f"{OFF_PRODUCT_URL}{code}" if code else "",
            )
        )
    return sources


async def _get_json(http: httpx.AsyncClient, url: str, params: dict) -> Any:
    resp = await http.get(url, params=params, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


async def _off_classic(term: str, http: httpx.AsyncClient) -> list[Source]:
    params = {
        "search_terms": term, "search_simple": 1, "action": "process", "json": 1, "page_size": PAGE_SIZE,
        "fields": "code,product_name,nutriments,quantity",
    }  # fmt: skip
    data = await _get_json(http, OFF_SEARCH_URL, params)
    return _off_sources(data.get("products"), term)


async def _off_search(term: str, http: httpx.AsyncClient) -> list[Source]:
    params = {"q": term, "page_size": PAGE_SIZE, "fields": "code,product_name,nutriments,quantity"}
    data = await _get_json(http, OFF_SEARCH2_URL, params)
    return _off_sources(data.get("hits"), term)


async def _wikipedia(term: str, http: httpx.AsyncClient) -> list[Source]:
    data = await _get_json(
        http, WIKI_API_URL, {"action": "opensearch", "search": term, "limit": 3, "format": "json"}
    )
    titles, urls = (data[1], data[3]) if isinstance(data, list) and len(data) >= 4 else ([], [])
    st = stem(term)
    # Only articles about the word itself: "Курт" yes, "Куртуа, Тибо" (a person) no.
    picked = [(t, u) for t, u in zip(titles, urls, strict=False) if "," not in t and normalize(t).startswith(st)][:2]
    if not picked:
        return []
    params = {
        "action": "query", "prop": "extracts", "exintro": 1, "explaintext": 1, "exchars": SNIPPET_MAX,
        "redirects": 1, "format": "json", "titles": "|".join(t for t, _ in picked),
    }  # fmt: skip
    pages = (await _get_json(http, WIKI_API_URL, params)).get("query", {}).get("pages", {})
    extracts = {p.get("title"): " ".join(str(p.get("extract") or "").split()) for p in pages.values()}
    return [
        Source(title=f"Википедия: {t}", snippet=extracts[t][:SNIPPET_MAX], per100=None, url=u)
        for t, u in picked
        if extracts.get(t)
    ]


async def _tavily(term: str, http: httpx.AsyncClient, key: str) -> list[Source]:
    body = {"api_key": key, "query": f"{term} блюдо калорийность", "max_results": PAGE_SIZE, "include_answer": True}
    resp = await http.post(TAVILY_URL, json=body, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    sources = []
    if answer := " ".join(str(data.get("answer") or "").split()):
        sources.append(Source(title="Поиск: ответ", snippet=answer[:SNIPPET_MAX], per100=None, url=""))
    for r in data.get("results") or []:
        if isinstance(r, dict) and (content := " ".join(str(r.get("content") or "").split())):
            sources.append(
                Source(str(r.get("title") or "Поиск"), content[:SNIPPET_MAX], None, str(r.get("url") or ""))
            )
    return sources


def _brief(e: BaseException) -> str:
    """Error text for a warning: no URL with parameters, no body, no stack."""
    if isinstance(e, httpx.HTTPStatusError):
        return f"HTTP {e.response.status_code}"
    return type(e).__name__


_CACHE: dict[str, list[Source]] = {}


async def lookup(term: str, http: httpx.AsyncClient, *, tavily_key: str = "") -> list[Source]:
    """Sources about `term`; [] when nothing was found or every source failed. Cached per process."""
    key = normalize(term)
    if not key:
        return []
    if key in _CACHE:
        return _CACHE[key]
    calls: dict[str, Awaitable[list[Source]]] = {
        "openfoodfacts": _off_classic(term, http),
        "openfoodfacts-search": _off_search(term, http),
        "wikipedia": _wikipedia(term, http),
    }
    if tavily_key:
        calls["tavily"] = _tavily(term, http, tavily_key)
    results = await asyncio.gather(*calls.values(), return_exceptions=True)
    sources: list[Source] = []
    seen: set[str] = set()
    failed = 0
    for name, result in zip(calls, results, strict=True):
        if isinstance(result, BaseException):
            if not isinstance(result, Exception):
                raise result  # cancellation
            log.warning("food lookup %s failed for a term: %s", name, _brief(result))
            failed += 1
            continue
        for s in result:
            # The same product is often listed under two barcodes: same name and numbers = one source.
            ident = f"{s.title}|{s.per100}" if s.per100 else s.url or s.title
            if ident not in seen:
                seen.add(ident)
                sources.append(s)
    if failed < len(calls):  # a network blip must not be cached as "nothing found"
        if len(_CACHE) >= CACHE_MAX:
            _CACHE.pop(next(iter(_CACHE)))
        _CACHE[key] = sources
    return sources


# ---- variants ----

SOURCES_MAX = 8


def format_sources(sources: list[Source]) -> str:
    """Numbered excerpts for the model; products with macros first (they carry the numbers)."""
    ordered = sorted(sources, key=lambda s: s.per100 is None)[:SOURCES_MAX]
    lines = []
    for i, s in enumerate(ordered, 1):
        parts = [s.title]
        if s.snippet:
            parts.append(s.snippet[:300])
        if m := s.per100:
            parts.append(
                f"на 100 г: {m.kcal:g} ккал, Б{m.protein_g:g} Ж{m.fat_g:g} У{m.carbs_g:g}"
            )
        lines.append(f"{i}. " + " — ".join(parts))
    return "\n".join(lines)


async def suggest(term: str, sources: list[Source], llm: JSONModel, phrase: str = "") -> list[Option]:
    """Up to MAX_OPTIONS variants for `term` from the sources (or the model's own knowledge without them).

    Each variant is validated on its own: one bad item drops only itself. The model's failure = [].
    """
    messages = build_lookup_messages(term, phrase, format_sources(sources))
    try:
        data = await llm.complete_json(messages, purpose="lookup")
    except LLMError as e:
        log.warning("food lookup: no variants from the model: %s", type(e).__name__)
        return []
    items = data.get("options") if isinstance(data, dict) else None
    options: list[Option] = []
    for item in items if isinstance(items, list) else []:
        try:
            option = Option.model_validate(item)
        except ValidationError:
            continue
        if any(normalize(o.name) == normalize(option.name) for o in options):
            continue
        if not sources:
            option.note = NO_SOURCE_NOTE
        options.append(option)
        if len(options) == MAX_OPTIONS:
            break
    return options


async def find_options(
    term: str, phrase: str, llm: JSONModel, http: httpx.AsyncClient, *, tavily_key: str = ""
) -> list[Option]:
    """What the handler calls: no sources = no variants (it may be a misheard word, ask the user instead)."""
    sources = await lookup(term, http, tavily_key=tavily_key)
    if not sources:
        return []
    return await suggest(term, sources, llm, phrase)
