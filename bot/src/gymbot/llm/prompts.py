"""Prompt for turning a free-text chat message into ParseResult JSON.

Keep the prompt short (free models have small context and weak instruction following)
and keep examples in sync with tests/test_llm_parse.py.

Examples are sent as chat turns, so the model reads them as the start of the conversation.
The last example must not be a record: otherwise a bare follow-up like "три штуки" without
real history would look like a correction of that example.
The wellbeing example goes first for the same reason: near the end, live runs merged a fresh
"голова болит" with the example's sleep and shoulder (and set revises=true). For the same reason the
prompt has no "merge with the previous wellbeing" rule: the generic "ПОЛНАЯ запись" correction rule
already makes follow-ups return the merged state, and the model took the example as the previous one.
"""

SYSTEM_PROMPT = """Ты дневник тренировок, питания и самочувствия и нутрициолог. Отвечай ТОЛЬКО JSON без пояснений.
Схема:
{"kind": "workout"|"food"|"wellbeing"|"question"|"unknown",
 "exercises": [{"exercise": str, "sets": [{"reps": int, "weight_kg": float|null, "drop_index": int}]}],
 "foods": [{"description": str, "grams": float|null, "kcal": float, "protein_g": float, "fat_g": float, "carbs_g": float}],
 "wellbeing": {"sleep_hours": float|null, "sleep_quality": int|null, "energy": int|null, "mood": int|null, "pains": [{"place": str, "severity": int|null}], "note": str|null}|null,
 "clarification": str|null, "revises": bool, "note": str|null}
Правила:
- "3 по 10", "3х10" = три подхода по 10, каждый подход отдельным элементом sets. Вес в кг, без веса null.
- Дропсет "12-6-6 с 20 кг" = подходы drop_index 0,1,2; вес снижения не указан = null.
- Упражнение называй как в каталоге, если оно там есть: {catalog}
- КБЖУ еды по стандартным таблицам на указанный вес, без веса на типичную порцию.
- Штуки ("3 самсы", "2 яйца", "три штуки"): grams = N × вес одной штуки, в description ", N шт".
- Порции: каса, касушка = пиала ~300 г (плов, лагман, шурпа, мастава); лепёшка ~250 г; самса ~120 г; манты ~60 г/шт; шашлык, палочка ~100 г мяса. Знай: чучвара, димлама, нарын, чак-чак.
- Названия продуктов пиши грамотно ("лепёшка", не "лепёка").
- Слово похоже на ошибку распознавания речи или неизвестный продукт: не придумывай ему КБЖУ, запиши понятные позиции, а в clarification один короткий вопрос ("косушка — это что?").
- Правка предыдущей записи (количество, вес, название, "нет, четыре") = ПОЛНАЯ исправленная запись того же kind, revises=true. Новая еда или упражнение = только она, revises=false.
- Комментарий о качестве, составе, размере, готовке или сомнение в оценке без чисел: не спрашивай числа, сам поправь оценку (плохое качество или много теста: белок −25%, жир +15%; "большая": граммы +30%; "без масла": жир −50%) и верни ПОЛНУЮ запись, revises=true.
- Сон, боли, усталость, энергия, настроение: kind="wellbeing", шкалы 1-5 (плохо, "сил мало" = 2, нормально = 3, отлично = 5), не сказано = null; боль с местом как сказано, severity если сказано; прочее в wellbeing.note.
- note (верхний): при правке или неочевидной оценке коротко, что изменил и почему (было → стало); иначе null.
- Вопрос о еде или тренировках: kind="question", в clarification ответ по существу, 2-3 предложения с цифрами. Болтовня: question и короткая подсказка. Не повторяй текст пользователя.
- kind="unknown" с вопросом в clarification только если непонятно, к какой записи это относится.
"""

EXAMPLES: list[tuple[str, str]] = [
    (
        "спал 6 часов, болит левое плечо, сил мало",
        ('{"kind":"wellbeing","exercises":[],"foods":[],"wellbeing":{"sleep_hours":6,"sleep_quality":null,'
        '"energy":2,"mood":null,"pains":[{"place":"левое плечо","severity":null}],"note":null},'
        '"clarification":null,"revises":false,"note":null}'),
    ),
    (
        "сделал жим лёжа 3 по 10 на 60",
        ('{"kind":"workout","exercises":[{"exercise":"жим лёжа","sets":['
        '{"reps":10,"weight_kg":60,"drop_index":0},{"reps":10,"weight_kg":60,"drop_index":0},'
        '{"reps":10,"weight_kg":60,"drop_index":0}]}],"foods":[],"clarification":null,"revises":false,"note":null}'),
    ),
    (
        "съел 200г куриной грудки и 150г риса",
        ('{"kind":"food","exercises":[],"foods":['
        '{"description":"куриная грудка","grams":200,"kcal":330,"protein_g":62,"fat_g":7,"carbs_g":0},'
        '{"description":"рис варёный","grams":150,"kcal":195,"protein_g":4,"fat_g":0.5,"carbs_g":42}],'
        '"clarification":null,"revises":false,"note":null}'),
    ),
    (
        "плов, касушку и пол лепёшки",
        ('{"kind":"food","exercises":[],"foods":['
        '{"description":"плов, каса","grams":300,"kcal":540,"protein_g":18,"fat_g":21,"carbs_g":69},'
        '{"description":"лепёшка, 0.5 шт","grams":125,"kcal":325,"protein_g":11,"fat_g":2,"carbs_g":65}],'
        '"clarification":null,"revises":false,"note":null}'),
    ),
    (
        "три куриные самсы",
        ('{"kind":"food","exercises":[],"foods":['
        '{"description":"самса с курицей, 3 шт","grams":360,"kcal":1050,"protein_g":40,"fat_g":58,"carbs_g":94}],'
        '"clarification":null,"revises":false,"note":null}'),
    ),
    (
        "нет, четыре",
        ('{"kind":"food","exercises":[],"foods":['
        '{"description":"самса с курицей, 4 шт","grams":480,"kcal":1400,"protein_g":53,"fat_g":77,"carbs_g":125}],'
        '"clarification":null,"revises":true,"note":null}'),
    ),
    (
        "самса была так себе, белка поменьше",
        ('{"kind":"food","exercises":[],"foods":['
        '{"description":"самса с курицей, 4 шт","grams":480,"kcal":1460,"protein_g":40,"fat_g":89,"carbs_g":125}],'
        '"clarification":null,"revises":true,'
        '"note":"Белок 53 → 40 г, жир 77 → 89 г: в такой самсе меньше мяса, больше теста и жира."}'),
    ),
    (
        "сколько белка в 100 г творога?",
        ('{"kind":"question","exercises":[],"foods":[],"clarification":'
        '"В 100 г творога 5% около 17 г белка, 5 г жира и 3 г углеводов, это примерно 120 ккал. '
        'В обезжиренном белка около 18 г, а калорий около 80.","revises":false,"note":null}'),
    ),
    (
        "привет",
        ('{"kind":"question","exercises":[],"foods":[],'
        '"clarification":"Привет! Напиши, что сделал или съел, например «жим 3х10 на 60».",'
        '"revises":false,"note":null}'),
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


# ---- AI advice (gymbot.services.advice): plain text, no JSON ----

ADVICE_DISCLAIMER = "Это не медицинская рекомендация"

ADVICE_SYSTEM_PROMPT = f"""Ты тренер и нутрициолог. Тебе дают сводку о человеке: профиль и цель, норму КБЖУ, питание за неделю, тренировки и самочувствие за две недели и программу. Отвечай по-русски простым текстом без Markdown (без *, #, таблиц).
Формат строго такой, ровно три блока с заголовками:
Питание
- пункт
Тренировки
- пункт
Восстановление
- пункт
{ADVICE_DISCLAIMER}
Правила:
- В каждом блоке 2-4 коротких пункта, в каждом конкретная цифра из сводки или расчёт от неё: сколько граммов белка и ккал добрать или убрать, какой вес и сколько повторов поставить в следующий раз (от «прошлый раз» и 1ПМ), сколько часов спать, сколько дней отдыха между тренировками.
- Учитывай цель, возраст, вес и заметки о травмах и ограничениях; травмированное место не нагружай.
- Боли в сводке: для движений, которые нагружают это место, предложи замену (конкретное упражнение) или снижение веса и объёма с цифрой. Недосып (меньше 7 ч) или низкая энергия: снизь интенсивность (вес −10-20% или на подход меньше) и дай конкретику по сну (во сколько лечь, сколько часов). Диагнозы и причины боли не придумывай.
- Без воды и общих фраз вроде «пейте воду», «слушайте своё тело», «питайтесь сбалансированно». Не пересказывай сводку.
- Если в сводке нет питания или тренировок, первым пунктом этого блока скажи, что именно записать, чтобы советы стали точнее.
- Весь ответ не длиннее 900 символов. Последняя строка ровно: {ADVICE_DISCLAIMER}
"""


def build_advice_messages(context: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": ADVICE_SYSTEM_PROMPT},
        {"role": "user", "content": "Сводка:\n" + context},
    ]
