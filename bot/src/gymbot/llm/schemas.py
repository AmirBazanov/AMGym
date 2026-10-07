"""Structured output the LLM must return. The bot only trusts data that validates here."""

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

REMEMBER_MAX = 200  # also the fact length limit (gymbot.services.facts.TEXT_MAX)
UNKNOWN_TERMS_MAX = 3  # words the bot may look up per message (gymbot.services.food_lookup)
UNKNOWN_TERM_LEN = 40
_PIECES_SUFFIX = re.compile(r",?\s*\d+(?:[.,]\d+)?\s*шт\.?\s*$")


class ParsedSet(BaseModel):
    reps: int = Field(ge=1, le=100)
    weight_kg: float | None = Field(default=None, ge=0, le=500)
    drop_index: int = Field(default=0, ge=0, le=5)


class ParsedExercise(BaseModel):
    exercise: str = Field(description="exercise name as close to the user's catalog as possible")
    sets: list[ParsedSet]


class ParsedFood(BaseModel):
    description: str
    grams: float | None = None
    kcal: float = Field(ge=0)
    protein_g: float = Field(ge=0)
    fat_g: float = Field(ge=0)
    carbs_g: float = Field(ge=0)

    @model_validator(mode="after")
    def _kcal_from_macros(self) -> "ParsedFood":
        """Free models often return kcal that contradict their own macros; trust the macros (4/9/4)."""
        computed = 4 * self.protein_g + 9 * self.fat_g + 4 * self.carbs_g
        if computed > 0 and abs(self.kcal - computed) > 0.15 * computed:
            self.kcal = round(computed)
        return self


def _scale(value: Any) -> int | None:
    """A 1..5 score; out-of-range numbers are clamped: one bad number must not reject the whole answer."""
    if value is None or value == "":
        return None
    try:
        n = round(float(value))
    except (TypeError, ValueError):
        return None
    return min(max(n, 1), 5)


class ParsedPain(BaseModel):
    place: str
    severity: int | None = None  # 1..5

    @field_validator("place", mode="before")
    @classmethod
    def _strip(cls, v: Any) -> str:
        return " ".join(str(v or "").split())

    @field_validator("severity", mode="before")
    @classmethod
    def _severity(cls, v: Any) -> int | None:
        return _scale(v)


class ParsedWellbeing(BaseModel):
    sleep_hours: float | None = None
    sleep_quality: int | None = None  # 1..5
    energy: int | None = None  # 1..5
    mood: int | None = None  # 1..5
    pains: list[ParsedPain] = []
    note: str | None = None

    @field_validator("sleep_hours", mode="before")
    @classmethod
    def _hours(cls, v: Any) -> float | None:
        try:
            h = float(v)
        except (TypeError, ValueError):
            return None
        return round(h, 1) if 0 <= h <= 24 else None

    @field_validator("sleep_quality", "energy", "mood", mode="before")
    @classmethod
    def _scales(cls, v: Any) -> int | None:
        return _scale(v)

    @field_validator("pains", mode="before")
    @classmethod
    def _pains(cls, v: Any) -> list:
        """Accept "плечо" and ["плечо"] as well as [{"place": "плечо"}]; drop pains without a place."""
        items = [v] if isinstance(v, (str, dict)) else v if isinstance(v, list) else []
        items = [{"place": p} if isinstance(p, str) else p for p in items]
        return [p for p in items if isinstance(p, dict) and str(p.get("place") or "").strip()]

    @field_validator("note", mode="before")
    @classmethod
    def _note(cls, v: Any) -> str | None:
        return (" ".join(str(v).split()) or None) if v is not None else None

    def is_empty(self) -> bool:
        return not (
            self.sleep_hours is not None or self.sleep_quality or self.energy or self.mood or self.pains or self.note
        )


class ParseResult(BaseModel):
    kind: Literal["workout", "food", "wellbeing", "question", "unknown"]
    exercises: list[ParsedExercise] = []
    foods: list[ParsedFood] = []
    wellbeing: ParsedWellbeing | None = None
    clarification: str | None = Field(default=None, description="ask the user if something is ambiguous")
    revises: bool = Field(default=False, description="the message corrects the previous record of the dialog")
    note: str | None = Field(default=None, description="what was changed and why, or why the estimate is such")
    remember: str | None = Field(default=None, description="a lasting fact about the user to offer remembering")
    unknown_terms: list[str] = Field(
        default=[], description="words or dishes the model could not identify (the bot looks them up)"
    )

    @model_validator(mode="before")
    @classmethod
    def _foods_without_numbers(cls, data: Any) -> Any:
        """A food with kcal null is the model admitting it does not know the dish (live: gpt-oss lists
        "гульчатай" with null macros): move it to unknown_terms instead of rejecting the whole answer."""
        if not isinstance(data, dict) or not isinstance(data.get("foods"), list):
            return data
        known, unknown = [], []
        for f in data["foods"]:
            if isinstance(f, dict) and f.get("kcal") is None:
                unknown.append(_PIECES_SUFFIX.sub("", str(f.get("description") or "")))
            else:
                known.append(f)
        if not unknown:
            return data
        terms = data.get("unknown_terms")
        terms = [terms] if isinstance(terms, str) else terms if isinstance(terms, list) else []
        return {**data, "foods": known, "unknown_terms": [*terms, *unknown]}

    @field_validator("remember", mode="before")
    @classmethod
    def _remember(cls, v: Any) -> str | None:
        """Short text or None; anything else is dropped, never rejected (that would fail the whole answer)."""
        if not isinstance(v, str):
            return None
        text = " ".join(v.split())
        return text if 0 < len(text) <= REMEMBER_MAX else None

    @field_validator("unknown_terms", mode="before")
    @classmethod
    def _unknown_terms(cls, v: Any) -> list[str]:
        """Accept a string or a list; strip, dedupe, cap. Junk is dropped, never rejected."""
        items = [v] if isinstance(v, str) else v if isinstance(v, list) else []
        terms: list[str] = []
        for item in items:
            term = " ".join(str(item).split()).strip(" .,!?«»\"'") if isinstance(item, str) else ""
            if term and len(term) <= UNKNOWN_TERM_LEN and term.casefold() not in {t.casefold() for t in terms}:
                terms.append(term)
        return terms[:UNKNOWN_TERMS_MAX]

    def is_record(self) -> bool:
        """Whether this is something to save (the rest is an answer or a clarifying question)."""
        if self.kind == "workout":
            return bool(self.exercises)
        if self.kind == "food":
            return bool(self.foods)
        if self.kind == "wellbeing":
            return self.wellbeing is not None and not self.wellbeing.is_empty()
        return False


# ---- Food photo (OpenRouterClient.parse_photo) ----

_LABEL_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")


def _label_number(v: Any) -> float | None:
    """A label number: 12.5, "12,5", "12,5 г"; None when missing, negative or unreadable (never rejected:
    gymbot.services.products decides whether the label is usable)."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int | float):
        return float(v) if v >= 0 else None
    if isinstance(v, str) and (m := _LABEL_NUMBER.search(v)) and not v.strip().startswith("-"):
        return float(m.group().replace(",", "."))
    return None


class LabelPer100(BaseModel):
    kcal: float | None = None
    protein_g: float | None = None
    fat_g: float | None = None
    carbs_g: float | None = None

    @field_validator("kcal", "protein_g", "fat_g", "carbs_g", mode="before")
    @classmethod
    def _number(cls, v: Any) -> float | None:
        return _label_number(v)


class ParsedLabel(BaseModel):
    """What the vision model read off a package: numbers as printed, per 100 g. Lenient on purpose."""

    name: str | None = None
    brand: str | None = None
    per100: LabelPer100 = LabelPer100()
    net_weight_g: float | None = None
    serving_g: float | None = None

    @field_validator("name", "brand", mode="before")
    @classmethod
    def _text(cls, v: Any) -> str | None:
        text = " ".join(str(v).split())[:200] if isinstance(v, str | int | float) and not isinstance(v, bool) else ""
        return text or None

    @field_validator("net_weight_g", "serving_g", mode="before")
    @classmethod
    def _weight(cls, v: Any) -> float | None:
        n = _label_number(v)
        return n if n else None  # 0 = unknown

    @field_validator("per100", mode="before")
    @classmethod
    def _per100(cls, v: Any) -> Any:
        return v if isinstance(v, (dict, LabelPer100)) else {}


class PhotoParse(BaseModel):
    """A food photo: `result` (kind="food", foods estimated from a plate) or `label` (a package)."""

    result: ParseResult
    label: ParsedLabel | None = None
