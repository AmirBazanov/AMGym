"""Prompt for turning a free-text chat message into ParseResult JSON.

Keep the prompt short (free models have small context and weak instruction following)
and keep examples in sync with tests/test_llm_parse.py.
"""

SYSTEM_PROMPT = """Ты парсер дневника тренировок и питания. Отвечай ТОЛЬКО JSON без пояснений.
Схема:
{"kind": "workout"|"food"|"question"|"unknown",
 "exercises": [{"exercise": str, "sets": [{"reps": int, "weight_kg": float|null, "drop_index": int}]}],
 "foods": [{"description": str, "grams": float|null, "kcal": float, "protein_g": float, "fat_g": float, "carbs_g": float}],
 "clarification": str|null}
Правила:
- "3 по 10" или "3х10" = три подхода по 10 повторений, каждый подход отдельным элементом sets.
- Вес в кг. "60" рядом с упражнением = weight_kg 60. Без веса = null.
- Дропсет "12-6-6 с 20 кг" = подходы drop_index 0,1,2; если вес снижения не указан, weight_kg null.
- Название упражнения выбирай из каталога, если оно там есть: {catalog}
- Для еды оценивай КБЖУ по стандартным таблицам на указанный вес; если веса нет, бери типичную порцию.
- Если не понятно, что имел в виду пользователь, kind="unknown" и вопрос в clarification.
"""

EXAMPLES: list[tuple[str, str]] = [
    (
        "сделал жим лёжа 3 по 10 на 60",
        ('{"kind":"workout","exercises":[{"exercise":"жим лёжа","sets":['
        '{"reps":10,"weight_kg":60,"drop_index":0},{"reps":10,"weight_kg":60,"drop_index":0},'
        '{"reps":10,"weight_kg":60,"drop_index":0}]}],"foods":[],"clarification":null}'),
    ),
    (
        "съел 200г куриной грудки и 150г риса",
        ('{"kind":"food","exercises":[],"foods":['
        '{"description":"куриная грудка","grams":200,"kcal":330,"protein_g":62,"fat_g":7,"carbs_g":0},'
        '{"description":"рис варёный","grams":150,"kcal":195,"protein_g":4,"fat_g":0.5,"carbs_g":42}],'
        '"clarification":null}'),
    ),
]


def build_messages(text: str, catalog: list[str]) -> list[dict[str, str]]:
    msgs = [{"role": "system", "content": SYSTEM_PROMPT.replace("{catalog}", ", ".join(catalog) or "пусто")}]
    for user, assistant in EXAMPLES:
        msgs += [{"role": "user", "content": user}, {"role": "assistant", "content": assistant}]
    msgs.append({"role": "user", "content": text})
    return msgs
