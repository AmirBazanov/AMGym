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


def test_echoed_clarification_is_replaced_with_hint():
    r = ParseResult(kind="question", clarification="три штуки")
    out = render_preview(r, source_text="  Три Штуки ")
    assert "три штуки" not in out.lower() and "Не понял" in out


def test_question_answer_shown_when_not_echo():
    r = ParseResult(kind="question", clarification="Напиши, что съел или сделал.")
    assert render_preview(r, source_text="привет") == "Напиши, что съел или сделал."


FOOD = {"description": "самса, 3 шт", "grams": 360, "kcal": 1000, "protein_g": 30, "fat_g": 60, "carbs_g": 90}


def test_note_shown_after_the_list():
    r = ParseResult(kind="food", foods=[FOOD], revises=True, note="Белок 40 → 30 г: больше теста.")
    out = render_preview(r, source_text="самса так себе")
    assert out.startswith("Записать еду?")
    assert out.endswith("\n\nБелок 40 → 30 г: больше теста.")


def test_note_that_echoes_user_is_hidden():
    r = ParseResult(kind="food", foods=[FOOD], note="Самса так себе.")
    assert "так себе" not in render_preview(r, source_text="самса так себе")


def test_no_note_no_trailing_text():
    assert render_preview(ParseResult(kind="food", foods=[FOOD])).endswith("У90")


def test_record_with_question_asks_after_list_and_note():
    r = ParseResult(kind="food", foods=[FOOD], note="Порцию взял типичную.", clarification="Косушка — это что?")
    out = render_preview(r, source_text="плов, косушку")
    assert out.startswith("Записать еду?")
    assert out.endswith("У90\n\nПорцию взял типичную.\n\nУточни: Косушка — это что?")


def test_record_question_that_echoes_user_is_hidden():
    r = ParseResult(kind="food", foods=[FOOD], clarification="косушка")
    assert "Уточни" not in render_preview(r, source_text="Косушка")
