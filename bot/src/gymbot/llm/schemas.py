"""Structured output the LLM must return. The bot only trusts data that validates here."""

from typing import Literal

from pydantic import BaseModel, Field, model_validator


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


class ParseResult(BaseModel):
    kind: Literal["workout", "food", "question", "unknown"]
    exercises: list[ParsedExercise] = []
    foods: list[ParsedFood] = []
    clarification: str | None = Field(default=None, description="ask the user if something is ambiguous")
    revises: bool = Field(default=False, description="the message corrects the previous record of the dialog")
    note: str | None = Field(default=None, description="what was changed and why, or why the estimate is such")
