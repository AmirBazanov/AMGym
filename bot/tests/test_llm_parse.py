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


def test_only_the_correction_example_revises():
    flags = {user: ParseResult.model_validate_json(a).revises for user, a in EXAMPLES}
    assert flags == {"сделал жим лёжа 3 по 10 на 60": False, "съел 200г куриной грудки и 150г риса": False,
                     "съел 3 яйца": False, "нет, четыре": True, "привет": False}
    assert all('"revises":' in a for _, a in EXAMPLES)  # the field is explicit in every example
    assert '"revises"' in SYSTEM_PROMPT
