"""User profile for AI advice: body, age, goal and free-text notes. Wire format mirrors miniapp/src/api.ts."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

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


BODY_KEYS = ("weightKg", "heightCm", "birthYear")  # also filled from facts, see fill_from_fact


def _value(key: str, value: Any) -> Any:
    if key == "weightKg" and value is not None:
        return Decimal(str(round(value, 1)))  # SQLite does not round Numeric(5,1) itself
    return value


def set_profile(user: User, changes: dict[str, Any]) -> None:
    """Apply a partial update (keys of ProfileIn): only the given keys change, None resets."""
    for key, value in changes.items():
        setattr(user, _COLUMNS[key], _value(key, value))


async def fill_from_fact(session: AsyncSession, user_id: int, values: dict[str, Any]) -> list[str]:
    """Fill EMPTY weight, height, birth year (keys of BODY_KEYS) from a fact; returns the filled keys.

    A set value is never overwritten: the profile is the user's own (Mini App), a fact only fills gaps.
    Each column is a conditional UPDATE (`... WHERE column IS NULL`), so a value the user sets in the
    Mini App meanwhile wins. Does not commit.
    """
    filled = []
    for key in BODY_KEYS:
        value = values.get(key)
        if value is None:
            continue
        column = _COLUMNS[key]
        result = await session.execute(
            update(User)
            .where(User.id == user_id, getattr(User, column).is_(None))
            .values({column: _value(key, value)})
            .execution_options(synchronize_session=False)
        )
        if result.rowcount:  # type: ignore[attr-defined]
            filled.append(key)
    return filled
