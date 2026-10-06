"""Structured output the LLM must return. The bot only trusts data that validates here."""

from typing import Literal

from pydantic import BaseModel, Field


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


class ParseResult(BaseModel):
    kind: Literal["workout", "food", "question", "unknown"]
    exercises: list[ParsedExercise] = []
    foods: list[ParsedFood] = []
    clarification: str | None = Field(default=None, description="ask the user if something is ambiguous")
