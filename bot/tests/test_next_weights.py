"""Weight suggestions (gymbot.services.next_weights) against the shared vectors in
data/progression_cases.json (the Mini App runs the same file in vitest), plus the name classifiers."""

import json
from pathlib import Path

import pytest

from gymbot.services import next_weights as nw

CASES = json.loads((Path(__file__).parents[2] / "data" / "progression_cases.json").read_text(encoding="utf-8"))["cases"]


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_shared_vectors(case):
    exp = case["expected"]
    if case["kind"] == "roundToStep":
        assert nw.round_to_step(case["input"]["weight"], case["input"]["step"]) == exp["weight"]
        return
    if case["kind"] == "scaleWeight":
        i = case["input"]
        assert nw.scale_weight(i["base"], i["factor"], i["step"]) == exp["weight"]
        return
    s = nw.suggest(
        nw.history_from_json(case["history"]),
        nw.exercise_from_json(case["exercise"]),
        nw.baselines_from_json(case["baselines"]),
        nw.overrides_from_json(case["overrides"]),
        case["today"],
        case["factor"],
    )
    assert (s.weight, s.source) == (exp["weight"], exp["source"]), s.reason
    if "baseWeight" in exp:
        assert s.base_weight == exp["baseWeight"]
    if "perHand" in exp:
        assert s.per_hand is exp["perHand"]
    if "hintKg" in exp:
        assert s.hint_kg == exp["hintKg"]
    for part in exp.get("reasonContains", []):
        assert part in s.reason, s.reason


def test_vectors_cover_every_source_and_kind():
    assert len(CASES) >= 25
    assert {c["expected"].get("source") for c in CASES if c["kind"] == "suggest"} == {
        "override", "history", "baseline", "related", "hint", "none"
    }
    assert {c["kind"] for c in CASES} == {"suggest", "roundToStep", "scaleWeight"}


def test_same_input_same_output():
    case = next(c for c in CASES if c["id"] == "owner-ez-underhand-from-dumbbells")
    args = (nw.history_from_json(case["history"]), nw.exercise_from_json(case["exercise"]))
    assert len({(s.weight, s.reason) for s in (nw.suggest(*args, day=case["today"]) for _ in range(5))}) == 1


@pytest.mark.parametrize(
    ("name", "equipment", "movement", "grip"),
    [
        ("сгибания с гантелями на бицепс с супинацией", "dumbbell", "curl", "supinated"),
        ("сгибания с гантелями на бицепс с пронацией", "dumbbell", "curl", "pronated"),
        ("сгибания на бицепс с ez грифом хватом снизу", "ez", "curl", "supinated"),
        ("сгибания на бицепс с EZ грифом хватом сверху", "ez", "curl", "pronated"),
        ("молотки с гантелями", "dumbbell", "curl", "neutral"),
        ("подъём штанги на бицепс", "barbell", "curl", "supinated"),
        ("французский жим в блоке из-за головы", "cable", "triceps_extension", None),
        ("французский жим лёжа", None, "triceps_extension", None),
        ("жим гантелей сидя", "dumbbell", "overhead_press", None),
        ("жим сидя в смите", "smith", "overhead_press", None),
        ("жим лёжа", None, "bench", None),
        ("жим штанги лёжа", "barbell", "bench", None),
        ("отведения на дельты", None, "lateral_raise", None),
        ("отведения пек дек на заднюю дельту", "machine", "rear_delt", None),
        ("отведения пек-дек на заднюю дельту", "machine", "rear_delt", None),
        ("тяга вертикального блока", "cable", "pulldown", None),
        ("тяга горизонтального блока", "cable", "row", None),
        ("подтягивания", "bodyweight", "pulldown", None),
        ("присед со штангой", "barbell", "squat", None),
        ("румынская тяга", None, "hinge", None),
        ("сгибания ног в тренажёре", "machine", None, None),
        ("разгибания ног", None, None, None),
        ("жим ногами", None, None, None),
    ],
)
def test_name_classifiers(name, equipment, movement, grip):
    assert (nw.equipment(name), nw.movement(name), nw.grip(name)) == (equipment, movement, grip)


def test_js_rounding_differs_from_python_round():
    assert round(12.5) == 12 and nw.js_round(12.5) == 13
    assert nw.format_kg(17.5) == "17,5" and nw.format_kg(60.0) == "60"


def test_related_transfer_prefers_same_grip_then_same_equipment():
    h = nw.history_from_json([{"startedAt": "2026-10-05T15:00:00Z", "exercises": [
        {"name": "сгибания с гантелями на бицепс с супинацией", "sets": [{"weight": 20, "reps": 8}]},
        {"name": "сгибания на бицепс с ez грифом хватом снизу", "sets": [{"weight": 30, "reps": 10}]},
    ]}])
    target = nw.ProgramExercise("сгибания на бицепс с ez грифом хватом сверху", "medium", nw.Prescription(3, 8, 12))
    s = nw.related(h, target)  # both differ in grip: the same equipment (EZ) wins over the dumbbells
    assert s.related == "сгибания на бицепс с ez грифом хватом снизу"
