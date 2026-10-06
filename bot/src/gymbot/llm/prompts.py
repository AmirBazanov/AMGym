"""Prompt for turning a free-text chat message into ParseResult JSON.

Keep the prompt short (free models have small context and weak instruction following)
and keep examples in sync with tests/test_llm_parse.py.

Examples are sent as chat turns, so the model reads them as the start of the conversation.
The last example must not be a record: otherwise a bare follow-up like "три штуки" without
real history would look like a correction of that example.
"""

SYSTEM_PROMPT = """Ты парсер дневника тренировок и питания. Отвечай ТОЛЬКО JSON без пояснений.
Схема:
{"kind": "workout"|"food"|"question"|"unknown",
 "exercises": [{"exercise": str, "sets": [{"reps": int, "weight_kg": float|null, "drop_index": int}]}],
 "foods": [{"description": str, "grams": float|null, "kcal": float, "protein_g": float, "fat_g": float, "carbs_g": float}],
 "clarification": str|null, "revises": bool}
Правила:
- "3 по 10" или "3х10" = три подхода по 10 повторений, каждый подход отдельным элементом sets.
- Вес в кг. "60" рядом с упражнением = weight_kg 60. Без веса = null.
- Дропсет "12-6-6 с 20 кг" = подходы drop_index 0,1,2; если вес снижения не указан, weight_kg null.
- Название упражнения выбирай из каталога, если оно там есть: {catalog}
- Для еды оценивай КБЖУ по стандартным таблицам на указанный вес; если веса нет, бери типичную порцию.
- Еда в штуках ("3 самсы", "2 яйца", "три штуки") = N типичных штук: grams = N × вес одной штуки, КБЖУ на весь вес, в description допиши ", N шт".
- Если сообщение уточняет или исправляет предыдущую запись (количество, вес, название, подходы, "нет, четыре", "три штуки"), верни ПОЛНУЮ исправленную запись того же kind и "revises": true.
- Новая еда или новое упражнение = только новая запись и "revises": false.
- Вопрос или фраза не для записи: kind="question", в clarification короткий ответ по теме дневника. Не повторяй текст пользователя.
- Если не понятно, что записать, kind="unknown" и уточняющий вопрос в clarification.
"""

EXAMPLES: list[tuple[str, str]] = [
    (
        "сделал жим лёжа 3 по 10 на 60",
        ('{"kind":"workout","exercises":[{"exercise":"жим лёжа","sets":['
        '{"reps":10,"weight_kg":60,"drop_index":0},{"reps":10,"weight_kg":60,"drop_index":0},'
        '{"reps":10,"weight_kg":60,"drop_index":0}]}],"foods":[],"clarification":null,"revises":false}'),
    ),
    (
        "съел 200г куриной грудки и 150г риса",
        ('{"kind":"food","exercises":[],"foods":['
        '{"description":"куриная грудка","grams":200,"kcal":330,"protein_g":62,"fat_g":7,"carbs_g":0},'
        '{"description":"рис варёный","grams":150,"kcal":195,"protein_g":4,"fat_g":0.5,"carbs_g":42}],'
        '"clarification":null,"revises":false}'),
    ),
    (
        "съел 3 яйца",
        ('{"kind":"food","exercises":[],"foods":['
        '{"description":"яйцо куриное, 3 шт","grams":165,"kcal":259,"protein_g":21,"fat_g":19,"carbs_g":1}],'
        '"clarification":null,"revises":false}'),
    ),
    (
        "нет, четыре",
        ('{"kind":"food","exercises":[],"foods":['
        '{"description":"яйцо куриное, 4 шт","grams":220,"kcal":345,"protein_g":28,"fat_g":25,"carbs_g":1.5}],'
        '"clarification":null,"revises":true}'),
    ),
    (
        "привет",
        ('{"kind":"question","exercises":[],"foods":[],'
        '"clarification":"Привет! Напиши, что сделал или съел, например «жим 3х10 на 60».","revises":false}'),
    ),
]


def build_messages(
    text: str, catalog: list[str], history: list[tuple[str, str]] | None = None
) -> list[dict[str, str]]:
    """Chat messages for the model: system prompt, few-shot examples, then the real dialog.

    `history` is a list of (user text, assistant JSON) turns from this user's recent dialog;
    it goes right before `text`, so the model can treat `text` as a correction of it.
    """
    msgs = [{"role": "system", "content": SYSTEM_PROMPT.replace("{catalog}", ", ".join(catalog) or "пусто")}]
    for user, assistant in [*EXAMPLES, *(history or [])]:
        msgs += [{"role": "user", "content": user}, {"role": "assistant", "content": assistant}]
    msgs.append({"role": "user", "content": text})
    return msgs
