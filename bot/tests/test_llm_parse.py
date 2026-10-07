import pytest

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
        "плов, касушку и пол лепёшки": False,
        "три куриные самсы": False,
        "нет, четыре": True,
        "самса была так себе, белка поменьше": True,
        "съел 3 манты, они у нас крупные, по 90 г": False,
        "гречка 200 г и 2 чапчуки": False,
        "спал 6 часов, болит левое плечо, сил мало": False,
        "сколько белка в 100 г творога?": False,
        "запиши в мини-ап мою программу и выставь рабочие веса на сегодня": False,
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


def test_regional_portions_example():
    r = example("плов, касушку и пол лепёшки")
    assert r.kind == "food" and r.clarification is None and r.note is None
    by_name = {f.description: f.grams for f in r.foods}
    assert by_name == {"плов, каса": 300, "лепёшка, 0.5 шт": 125}
    for word in ("каса", "касушка", "лепёшка ~250", "манты", "шашлык", "чучвара", "курт", "не заменяй другим блюдом"):
        assert word in SYSTEM_PROMPT


def test_unknown_word_rule_in_prompt():
    assert "не придумывай" in SYSTEM_PROMPT.lower() and "распознавания" in SYSTEM_PROMPT
    assert "грамотно" in SYSTEM_PROMPT


def test_wellbeing_example_and_rule():
    r = example("спал 6 часов, болит левое плечо, сил мало")
    assert r.kind == "wellbeing" and r.wellbeing is not None and not r.foods and not r.exercises
    w = r.wellbeing
    assert w.sleep_hours == 6 and w.energy == 2
    assert [p.place for p in w.pains] == ["левое плечо"]
    assert '"wellbeing"' in SYSTEM_PROMPT and "сил мало" in SYSTEM_PROMPT
    assert len(SYSTEM_PROMPT) < 3000  # keep the prompt compact for small free models


def test_wellbeing_out_of_range_values_are_clamped_not_rejected():
    from gymbot.llm.schemas import ParsedWellbeing

    w = ParsedWellbeing.model_validate(
        {"sleep_hours": 30, "sleep_quality": 0, "energy": 7, "mood": "3",
         "pains": [{"place": " колено ", "severity": 9}, {"place": "", "severity": 2}, {"place": "спина"}]}
    )
    assert w.sleep_hours is None and w.sleep_quality == 1 and w.energy == 5 and w.mood == 3
    assert [(p.place, p.severity) for p in w.pains] == [("колено", 5), ("спина", None)]
    assert ParsedWellbeing.model_validate({"sleep_hours": 7.5, "pains": None}).pains == []
    for shape in ("плечо", ["плечо"], {"place": "плечо"}):
        assert [p.place for p in ParsedWellbeing.model_validate({"pains": shape}).pains] == ["плечо"]
    assert ParsedWellbeing.model_validate({"pains": [3, None, {"severity": 2}]}).pains == []


def test_remember_example_and_rule():
    r = example("съел 3 манты, они у нас крупные, по 90 г")
    assert r.kind == "food" and r.foods[0].grams == 270 and r.remember and "90" in r.remember
    others = [u for u, a in EXAMPLES if ParseResult.model_validate_json(a).remember]
    assert others == ["съел 3 манты, они у нас крупные, по 90 г"]  # one example only
    assert EXAMPLES[0][0] != others[0] and EXAMPLES[-1][0] != others[0]
    assert '"remember"' in SYSTEM_PROMPT


def test_remember_is_tolerant():
    assert ParseResult.model_validate({"kind": "food", "remember": "  самса   ~150 г "}).remember == "самса ~150 г"
    for bad in ("", "   ", 5, ["x"], {"a": 1}, None):
        assert ParseResult.model_validate({"kind": "food", "remember": bad}).remember is None
    assert ParseResult.model_validate({"kind": "food", "remember": "x" * 300}).remember is None


def test_facts_go_into_system_prompt_only():
    plain = build_messages("две самсы", ["жим лёжа"])
    msgs = build_messages("две самсы", ["жим лёжа"], facts=["самса ~150 г", "не ест творог"])
    assert "Факты о пользователе" in msgs[0]["content"] and "самса ~150 г; не ест творог" in msgs[0]["content"]
    assert "Факты о пользователе" not in plain[0]["content"]
    assert msgs[1:] == plain[1:]  # examples and the text are untouched
    assert build_messages("x", [], facts=[])[0] == build_messages("x", [])[0]


def test_facts_line_is_capped():
    from gymbot.llm.prompts import FACTS_MAX_CHARS, format_facts

    line = format_facts([f"факт номер {i} " + "x" * 180 for i in range(50)])
    assert line.startswith("Факты о пользователе: факт номер 0 ") and len(line) <= FACTS_MAX_CHARS
    assert format_facts([]) == ""


def test_schema_line_in_prompt_names_every_model_field():
    # The schema line of the prompt and ParseResult must not drift apart.
    for name in ParseResult.model_fields:
        assert f'"{name}"' in SYSTEM_PROMPT, name


def test_unknown_word_example_asks_instead_of_inventing():
    r = example("гречка 200 г и 2 чапчуки")
    assert r.kind == "food" and r.unknown_terms == ["чапчук"]
    assert [f.description for f in r.foods] == ["гречка варёная"]  # no made-up macros for the unknown word
    assert all("чапчук" not in f.description for f in r.foods)
    assert r.clarification and "чапчук" in r.clarification
    users = [u for u, _ in EXAMPLES]
    assert users.index("гречка 200 г и 2 чапчуки") < len(users) - 1  # the last example is not a record
    assert [u for u, a in EXAMPLES if ParseResult.model_validate_json(a).unknown_terms] == [
        "гречка 200 г и 2 чапчуки"
    ]  # one example only


def test_unknown_terms_default_is_empty():
    assert ParseResult.model_validate({"kind": "food"}).unknown_terms == []


def test_unknown_terms_accepts_a_string():
    assert ParseResult.model_validate({"kind": "food", "unknown_terms": "курт"}).unknown_terms == ["курт"]


@pytest.mark.parametrize("bad", [None, 123, 1.5, {"a": "курт"}, True])
def test_unknown_terms_junk_becomes_empty_list(bad):
    assert ParseResult.model_validate({"kind": "food", "unknown_terms": bad}).unknown_terms == []


def test_unknown_terms_are_cleaned_deduped_and_capped():
    raw = ["  курт ", "Курт", "", "x" * 41, "a", "b", "c"]
    assert ParseResult.model_validate({"kind": "food", "unknown_terms": raw}).unknown_terms == ["курт", "a", "b"]


def test_unknown_terms_drops_non_strings_and_keeps_limit_length():
    raw = [None, 5, ["курт"], "x" * 40, "кутаб"]
    got = ParseResult.model_validate({"kind": "food", "unknown_terms": raw}).unknown_terms
    assert got == ["x" * 40, "кутаб"]


def test_unknown_terms_are_stripped_of_quotes_and_punctuation():
    got = ParseResult.model_validate({"kind": "food", "unknown_terms": ["«курт»", "кутаб?", " тандыр   гошт ."]})
    assert got.unknown_terms == ["курт", "кутаб", "тандыр гошт"]


def test_food_without_kcal_becomes_an_unknown_term():
    # Live: gpt-oss listed an unknown dish with null macros; that must not reject the whole answer.
    r = ParseResult.model_validate(
        {"kind": "food", "unknown_terms": "гульчатай", "foods": [
            {"description": "гульчатай, 2 шт", "grams": None, "kcal": None, "protein_g": None,
             "fat_g": None, "carbs_g": None},
            {"description": "чай", "kcal": 2, "protein_g": 0, "fat_g": 0, "carbs_g": 0},
        ]}
    )
    assert [f.description for f in r.foods] == ["чай"]
    assert r.unknown_terms == ["гульчатай"]


def test_miniapp_setup_example_is_second_to_last_and_not_a_record():
    from gymbot.llm.prompts import MINIAPP_SETUP_ANSWER

    text, answer = EXAMPLES[-2]
    assert text == "запиши в мини-ап мою программу и выставь рабочие веса на сегодня"
    assert EXAMPLES[-1][0] == "привет"  # the last example stays a plain question
    parsed = ParseResult.model_validate_json(answer)
    assert parsed.kind == "question" and not parsed.exercises and not parsed.foods
    assert parsed.clarification == MINIAPP_SETUP_ANSWER and "Сегодня" in parsed.clarification


def test_miniapp_setup_example_is_sent_to_the_model():
    msgs = build_messages("привет", [])
    users = [m["content"] for m in msgs if m["role"] == "user"]
    assert "запиши в мини-ап мою программу и выставь рабочие веса на сегодня" in users


def test_baseline_messages_carry_catalog_and_fact():
    from gymbot.llm.prompts import build_baseline_messages

    msgs = build_baseline_messages("жим лёжа 90 на 8", ["жим лёжа", "румынская тяга"])
    assert [m["role"] for m in msgs] == ["system", "user"]
    assert "жим лёжа, румынская тяга" in msgs[0]["content"] and "{catalog}" not in msgs[0]["content"]
    assert msgs[1]["content"] == "Факт: жим лёжа 90 на 8"
    assert "пусто" in build_baseline_messages("x", [])[0]["content"]
