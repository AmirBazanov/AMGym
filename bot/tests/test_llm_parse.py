from gymbot.llm.openrouter import extract_json
from gymbot.llm.prompts import EXAMPLES, build_messages
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
