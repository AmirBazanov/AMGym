from gymbot.handlers.log_text import render_preview
from gymbot.llm.schemas import ParseResult


def test_workout_preview_weights_and_drop_marker():
    r = ParseResult.model_validate(
        {
            "kind": "workout",
            "exercises": [
                {
                    "exercise": "жим лёжа",
                    "sets": [
                        {"reps": 10, "weight_kg": 80},
                        {"reps": 8, "weight_kg": 62.5},
                        {"reps": 12, "weight_kg": 50, "drop_index": 1},
                    ],
                },
                {"exercise": "подтягивания", "sets": [{"reps": 12}]},
            ],
        }
    )
    out = render_preview(r)
    assert out.startswith("Записать?")
    assert "• жим лёжа: 80 кг × 10, 62.5 кг × 8, 50 кг × 12 (дроп)" in out
    assert out.count("(дроп)") == 1
    assert "• подтягивания: 12 повт." in out


def test_food_preview_total_kcal():
    r = ParseResult.model_validate(
        {
            "kind": "food",
            "foods": [
                {"description": "овсянка", "grams": 200, "kcal": 300, "protein_g": 10, "fat_g": 5, "carbs_g": 50},
                {"description": "яйцо", "kcal": 70.4, "protein_g": 6, "fat_g": 5, "carbs_g": 0},
            ],
        }
    )
    out = render_preview(r)
    assert "Всего 370 ккал" in out
    assert "овсянка 200 г: 300 ккал, Б10 Ж5 У50" in out
    assert "яйцо: 70 ккал" in out


def test_unknown_shows_clarification():
    assert render_preview(ParseResult(kind="unknown", clarification="Сколько подходов?")) == "Сколько подходов?"


def test_unknown_without_clarification_has_hint():
    assert "присед" in render_preview(ParseResult(kind="unknown"))


def test_empty_workout_falls_back():
    assert "Не понял" in render_preview(ParseResult(kind="workout"))
