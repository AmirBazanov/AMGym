from gymbot.llm.openrouter import extract_json
from gymbot.llm.prompts import EXAMPLES, SYSTEM_PROMPT, build_messages
from gymbot.llm.schemas import ParseResult


def test_examples_validate():
    for _, answer in EXAMPLES:
        ParseResult.model_validate_json(answer)


def test_extract_json_from_prose():
    data = extract_json('Конечно! {"kind": "unknown", "clarification": "что?"} Надеюсь, помог')
    assert ParseResult.model_validate(data).kind == "unknown"


def test_catalog_in_prompt():
    msgs = build_messages("жим 3х10", ["жим лёжа"])
    assert "жим лёжа" in msgs[0]["content"] and msgs[-1]["content"] == "жим 3х10"


def test_last_example_is_not_a_record():
    # Examples are sent as chat turns: if the last one were a record, a bare "три штуки"
    # without real history would look like a correction of that example.
    last = ParseResult.model_validate_json(EXAMPLES[-1][1])
    assert last.kind == "question" and not last.foods and not last.exercises
    assert last.clarification and last.clarification.lower() != EXAMPLES[-1][0].lower()


def test_pieces_rule_and_example():
    assert "штук" in SYSTEM_PROMPT
    pieces = [
        ParseResult.model_validate_json(a).foods[0]
        for _, a in EXAMPLES
        if ParseResult.model_validate_json(a).foods and "шт" in ParseResult.model_validate_json(a).foods[0].description
    ]
    assert pieces, "need an example with food counted in pieces"
    assert all(f.grams and f.grams > 0 for f in pieces)


def test_correction_and_no_echo_rules_in_prompt():
    assert "ПОЛНУЮ" in SYSTEM_PROMPT
    assert "не повторяй" in SYSTEM_PROMPT.lower()


def test_without_history_text_follows_examples():
    msgs = build_messages("три штуки", [])
    assert msgs[-2] == {"role": "assistant", "content": EXAMPLES[-1][1]}
    assert msgs[-1] == {"role": "user", "content": "три штуки"}


def test_history_goes_between_examples_and_text():
    prev = '{"kind":"food"}'
    msgs = build_messages("три штуки", [], history=[("три куриные самсы", prev)])
    assert msgs[-4] == {"role": "assistant", "content": EXAMPLES[-1][1]}
    assert msgs[-3:] == [
        {"role": "user", "content": "три куриные самсы"},
        {"role": "assistant", "content": prev},
        {"role": "user", "content": "три штуки"},
    ]


def test_only_corrections_revise():
    flags = {user: ParseResult.model_validate_json(a).revises for user, a in EXAMPLES}
    assert flags == {
        "сделал жим лёжа 3 по 10 на 60": False,
        "съел 200г куриной грудки и 150г риса": False,
        "три куриные самсы": False,
        "нет, четыре": True,
        "самса была так себе, белка поменьше": True,
        "сколько белка в 100 г творога?": False,
        "привет": False,
    }
    assert all('"revises":' in a and '"note":' in a for _, a in EXAMPLES)  # explicit in every example
    assert '"revises"' in SYSTEM_PROMPT and '"note"' in SYSTEM_PROMPT


def example(user: str) -> ParseResult:
    return ParseResult.model_validate_json(dict(EXAMPLES)[user])


def test_quality_comment_example_adjusts_without_asking():
    before, after = example("нет, четыре"), example("самса была так себе, белка поменьше")
    assert after.kind == "food" and after.revises and not after.clarification
    assert after.foods[0].grams == before.foods[0].grams  # same portion, different estimate
    assert after.foods[0].protein_g < before.foods[0].protein_g
    assert after.note and str(round(before.foods[0].protein_g)) in after.note  # says what changed
    assert "без чисел" in SYSTEM_PROMPT and "не спрашивай" in SYSTEM_PROMPT.lower()


def test_plain_records_have_no_note():
    for user in ("сделал жим лёжа 3 по 10 на 60", "три куриные самсы", "нет, четыре"):
        assert example(user).note is None


def test_nutrition_question_example_answers_with_numbers():
    q = example("сколько белка в 100 г творога?")
    assert q.kind == "question" and q.clarification
    assert any(ch.isdigit() for ch in q.clarification) and len(q.clarification) > 40


def test_kcal_is_recomputed_when_it_contradicts_macros():
    from gymbot.llm.schemas import ParsedFood

    # The plov case from a live run: 14/30/67 is ~594 kcal, the model said 485.
    off = ParsedFood(description="плов", kcal=485, protein_g=14, fat_g=30, carbs_g=67)
    assert off.kcal == 594
    close = ParsedFood(description="грудка", kcal=330, protein_g=62, fat_g=7, carbs_g=0)
    assert close.kcal == 330  # within 15 %: the model's number stays
    zero = ParsedFood(description="вода", kcal=0, protein_g=0, fat_g=0, carbs_g=0)
    assert zero.kcal == 0
