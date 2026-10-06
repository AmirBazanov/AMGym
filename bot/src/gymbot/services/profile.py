"""User profile for AI advice: body, age, goal and free-text notes. Wire format mirrors miniapp/src/api.ts."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from gymbot.db.models import User

Goal = Literal["mass", "cut", "strength", "health"]
GOAL_NAMES: dict[str, str] = {
    "mass": "набор массы",
    "cut": "сушка (снижение жира)",
    "strength": "сила",
    "health": "здоровье и форма",
}
MIN_BIRTH_YEAR = 1930
MIN_AGE = 10
ABOUT_MAX = 500


class Profile(BaseModel):
    weightKg: float | None = None
    heightCm: int | None = None
    birthYear: int | None = None
    goal: Goal | None = None
    about: str | None = None


class ProfileIn(BaseModel):
    """Partial update: omitted keys stay, null resets."""

    weightKg: float | None = Field(default=None, ge=30, le=300)
    heightCm: int | None = Field(default=None, ge=120, le=250)
    birthYear: int | None = None
    goal: Goal | None = None
    about: str | None = Field(default=None, max_length=ABOUT_MAX)

    @field_validator("birthYear")
    @classmethod
    def _birth_year(cls, v: int | None) -> int | None:
        # The upper bound moves every year, so it cannot be a static Field(le=...).
        latest = datetime.now(UTC).year - MIN_AGE
        if v is not None and not MIN_BIRTH_YEAR <= v <= latest:
            raise ValueError(f"birthYear must be {MIN_BIRTH_YEAR}..{latest}")
        return v

    @field_validator("about")
    @classmethod
    def _about(cls, v: str | None) -> str | None:
        v = (v or "").strip()
        return v or None


def user_profile(user: User) -> Profile:
    return Profile(
        weightKg=float(user.weight_kg) if user.weight_kg is not None else None,
        heightCm=user.height_cm,
        birthYear=user.birth_year,
        goal=user.goal,  # type: ignore[arg-type]
        about=user.about,
    )


_COLUMNS = {"weightKg": "weight_kg", "heightCm": "height_cm", "birthYear": "birth_year", "goal": "goal", "about": "about"}


def set_profile(user: User, changes: dict[str, Any]) -> None:
    """Apply a partial update (keys of ProfileIn): only the given keys change, None resets."""
    for key, value in changes.items():
        if key == "weightKg" and value is not None:
            value = Decimal(str(round(value, 1)))  # SQLite does not round Numeric(5,1) itself
        setattr(user, _COLUMNS[key], value)
