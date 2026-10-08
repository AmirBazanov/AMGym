"""JSON schemas for Claude's structured outputs (`output_config.format`), one per JSON call type.

Built from pydantic models with the SDK's `transform_schema` (additionalProperties false on every object;
numeric and string limits move into the description). A single-value Literal becomes an enum first: the
transform keeps `enum` but would demote `const` to description text. The schemas only shape the answer:
the callers keep validating it as before (ParseResult, chat_settings.parse_actions, baselines.parse_answer,
plan.apply_refinement, food_lookup.suggest), so a schema that drifts from them costs a rejected answer, not
bad data. The models below mirror the prompts in gymbot.llm.prompts; keep them in sync.
"""

from __future__ import annotations

from typing import Any, Literal

from anthropic import transform_schema
from pydantic import BaseModel

from gymbot.llm.schemas import ParsedFood, ParsedLabel, ParseResult


def _const_to_enum(node: Any) -> Any:
    if isinstance(node, dict):
        out = {k: _const_to_enum(v) for k, v in node.items() if k != "const"}
        if "const" in node:
            out["enum"] = [node["const"]]
        return out
    if isinstance(node, list):
        return [_const_to_enum(v) for v in node]
    return node


def schema_of(model: type[BaseModel]) -> dict[str, Any]:
    return transform_schema(_const_to_enum(model.model_json_schema()))


# ---- food photo (prompts.VISION_SYSTEM) ----


class VisionAnswer(BaseModel):
    foods: list[ParsedFood] = []
    note: str | None = None
    label: ParsedLabel | None = None


# ---- settings from the chat (prompts.SETTINGS_SYSTEM_PROMPT); every key the prompt names is required ----


class _Targets(BaseModel):
    type: Literal["targets"]
    kcal: int | None
    protein: int | None
    fat: int | None
    carbs: int | None


class _ReminderAdd(BaseModel):
    type: Literal["reminder_add"]
    time: str
    kind: Literal["text", "nutrition", "advice", "checkin"]
    text: str | None
    weekdays: list[int]


class _OneOff(BaseModel):
    type: Literal["one_off_reminder"]


class _ReminderChange(BaseModel):
    type: Literal["reminder_delete", "reminder_disable", "reminder_enable"]
    ids: list[int]
    kind: str | None
    about: str | None


class _Weight(BaseModel):
    type: Literal["weight"]
    said: str
    exercise: str | None
    weight_kg: float


class _Program(BaseModel):
    type: Literal["program"]
    program: str | None
    start_date: str | None


class _Rest(BaseModel):
    type: Literal["rest"]
    seconds: int


class SettingsAnswer(BaseModel):
    actions: list[_Targets | _ReminderAdd | _OneOff | _ReminderChange | _Weight | _Program | _Rest]


# ---- working weights from a fact (prompts.BASELINE_SYSTEM_PROMPT) ----


class _Lift(BaseModel):
    said: str
    exercise: str | None
    weight_kg: float
    reps: int | None


class _Profile(BaseModel):
    height_cm: int | None
    weight_kg: float | None
    age: int | None
    birth_year: int | None


class BaselineAnswer(BaseModel):
    lifts: list[_Lift]
    profile: _Profile


# ---- unknown dish (prompts.LOOKUP_SYSTEM_PROMPT) ----


class _Option(BaseModel):
    name: str
    portion_g: float
    kcal: float
    protein_g: float
    fat_g: float
    carbs_g: float
    note: str


class LookupAnswer(BaseModel):
    options: list[_Option]


# ---- day plan refinement (prompts.PLAN_SYSTEM_PROMPT; mirrors services.plan.PlanExercise) ----


class _PlanExercise(BaseModel):
    name: str
    sets: int
    repsMin: int | None
    repsMax: int | None
    weightFactor: float
    skip: bool
    replaceWith: str | None
    reason: str | None


class PlanAnswer(BaseModel):
    summary: str | None
    exercises: list[_PlanExercise]


PARSE = schema_of(ParseResult)
PHOTO = schema_of(VisionAnswer)
SETTINGS = schema_of(SettingsAnswer)
BASELINES = schema_of(BaselineAnswer)
LOOKUP = schema_of(LookupAnswer)
PLAN = schema_of(PlanAnswer)
