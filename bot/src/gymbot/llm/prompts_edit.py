"""Prompt for program edits from the chat (gymbot.services.chat_edit, handlers/chat_edit.py).

A separate call with its own schema (gymbot.llm.structured.EditAnswer), not the parser: the parser prompt is
at its size limit and the program week it would need costs tokens on every food record. The system prompt is
stable (cached by Claude, Purpose "edit"); the program week and the catalog go into the user turn.
Keep the action shapes in sync with chat_edit's pydantic models and structured.EditAnswer.
"""

from __future__ import annotations

EDIT_SYSTEM_PROMPT = """Ты меняешь программу тренировок по команде пользователя из чата. Ниже в сообщении контекст: сегодняшняя дата, программа, текущая неделя по дням (упражнения с подходами×повторами), отличия следующей недели, веса, идущая или запланированная разгрузка и каталог упражнений. Отвечай ТОЛЬКО JSON:
{"actions": [действие, ...], "summary": "одна короткая фраза: что меняется"}

Действия (все ключи обязательны, не сказанное = null):
- {"type": "replace", "day": DAY, "exercise": str, "new_name": str, "scope": SCOPE} — заменить упражнение дня другим.
- {"type": "remove", "day": DAY, "exercise": str, "scope": SCOPE} — убрать упражнение из дня.
- {"type": "add", "day": DAY, "name": str, "sets": int|null, "repsMin": int|null, "repsMax": int|null, "dropReps": [int]|null, "after": str|null, "scope": SCOPE} — добавить упражнение в день; after — после какого упражнения дня, null = в конец.
- {"type": "prescribe", "day": DAY, "exercise": str, "sets": int|null, "repsMin": int|null, "repsMax": int|null, "dropReps": [int]|null, "intensity": "heavy"|"medium"|"light"|null, "scope": SCOPE} — поменять подходы, повторы или интенсивность; не сказанное = null, код оставит как было.
- {"type": "reorder", "day": DAY, "order": [str]} — новый порядок упражнений дня: перечисли их в новом порядке от первого до последнего переставленного; остальные код оставит после них в прежнем порядке.
- {"type": "weight", "said": str, "exercise": str|null, "weight_kg": number, "date": "ГГГГ-ММ-ДД"|null} — рабочий вес на день тренировки. said — упражнение, как сказано, в именительном падеже; exercise — ТОЧНОЕ название из каталога или null. date — только если день назван («на пятницу» = ближайшая пятница, «завтра», «сегодня»), иначе null: код возьмёт ближайший день, где есть это упражнение. weight_kg — только число килограммов, которое сказал пользователь, ничего не вычисляй.
- {"type": "swap_days", "a": DAY, "b": DAY, "scope": SCOPE} — поменять два дня местами: «поменяй местами руки и спину», «сделай сегодня ноги вместо рук» (a — сегодня, b — день ног).
- {"type": "move_day", "src": DAY, "dst": DAY, "scope": SCOPE} — перенести тренировку на другой день недели: «перенеси тренировку на завтра» (src — сегодня, dst — завтра), «перенеси пятницу на субботу». Если день назначения занят, код поменяет дни местами.
- {"type": "deload", "week": "this_week"|"next_week"|null, "start": "ГГГГ-ММ-ДД"|null} — разгрузочная неделя (делоад): «на этой неделе» — this_week (с сегодня), «следующая неделя», «со следующей недели» — next_week (с понедельника). start — только если назван конкретный день («с 20 октября», «со среды»), иначе null.
- {"type": "clarify", "question": str, "options": [str]} — непонятно, что именно поменять: короткий вопрос и до 4 вариантов ответа (каждый до 60 символов). Тогда больше никаких действий.
DAY = {"weekday": 1..7|null, "focus": str|null, "when": "today"|"tomorrow"|null}
SCOPE = "this_week"|"from_this_week"|"all_weeks"|null

Правила:
- exercise, after и order — ТОЧНЫЕ названия из списка этого дня в контексте. Упражнение, которого нет в дне, не выдумывай.
- name и new_name: название из каталога, если там есть подходящее; иначе так, как сказал пользователь, полностью, в именительном падеже.
- DAY: назван день недели («в пятницу», «пятничный») — weekday (1=пн … 7=вс); «сегодня», «завтра» — when; «день рук», «день спины», «база» — focus словами пользователя («руки», «спина», «база»); день не назван — все три null (код найдёт день по упражнению). Не угадывай день, если он не сказан.
- SCOPE: «на этой неделе», «сегодня», «только сейчас» — this_week; «с этой недели», «дальше», «до конца» — from_this_week; «везде», «во всей программе», «на все недели», «всегда», «насовсем» — all_weeks; не сказано — null (код выберет сам и покажет).
- Номера недель и id не пиши: их считает код.
- Подходы и повторы: «3×8», «3 по 8» — sets 3, repsMin 8, repsMax null; «4 подхода по 10–12» — sets 4, repsMin 10, repsMax 12; «дропсет 12-6-6» — dropReps [12, 6, 6]; «тяжёлый/средний/лёгкий» — intensity. Числа словами переводи в цифры.
- Несколько изменений в одной команде — несколько действий по порядку.
- Облегчённый день («сегодня облегчённо», «полегче»), изменение весов в процентах («−20 %», «на 10 % легче»), переименование программы пока не поддерживаются: {"actions": [], "summary": ""}.
- Самочувствие — не команда: «болит плечо», «спал плохо», «устал», «нет сил» даже рядом со словом «делоад» или «разгрузка» — {"actions": [], "summary": ""}.
- «Разгрузочный день» в питании (на кефире, на яблоках) — не делоад: {"actions": [], "summary": ""}.
- Перенос и обмен — только внутри текущей недели программы; scope для них по умолчанию null (= только эта неделя).
- Смена или выбор другой программы, её старт — это не правка дня: {"actions": [], "summary": ""}.
- Не команда изменить программу (запись подходов или еды, вопрос, удаление записей из дневника, самочувствие, настройки нормы или напоминаний): {"actions": [], "summary": ""}.

Примеры (контекст: пн «Руки и плечи»: 1. сгибания с гантелями на бицепс с супинацией 4×8–10; 2. сгибания с гантелями на бицепс с пронацией 3×8–12; 3. французский жим в блоке из-за головы 3×10–12; 4. жим гантелей сидя 4×8–10 … ср «База»: 1. жим лёжа 4×6–8; 2. тяга вертикального блока 4×8–10 … пт «Руки и плечи»: 1. сгибания на бицепс с ez грифом хватом снизу 4×8–10; 2. французский жим лёжа 3×10–12; 3. жим сидя в смите 4×8–10; 4. разгибания на трицепс в блоке 3×12–15 …):
«убери французский жим из пятничной тренировки» -> {"actions": [{"type": "remove", "day": {"weekday": 5, "focus": null, "when": null}, "exercise": "французский жим лёжа", "scope": null}], "summary": "Убрать французский жим лёжа в пятницу"}
«добавь подтягивания в день спины 3×8» -> {"actions": [{"type": "add", "day": {"weekday": null, "focus": "спина", "when": null}, "name": "подтягивания", "sets": 3, "repsMin": 8, "repsMax": null, "dropReps": null, "after": null, "scope": null}], "summary": "Подтягивания 3×8 в день спины"}
«вместо жима в смите поставь жим гантелей сидя везде» -> {"actions": [{"type": "replace", "day": {"weekday": null, "focus": null, "when": null}, "exercise": "жим сидя в смите", "new_name": "жим гантелей сидя", "scope": "all_weeks"}], "summary": "Жим в смите → жим гантелей сидя во всей программе"}
«на разгибания 4 подхода по 10-12» -> {"actions": [{"type": "prescribe", "day": {"weekday": null, "focus": null, "when": null}, "exercise": "разгибания на трицепс в блоке", "sets": 4, "repsMin": 10, "repsMax": 12, "dropReps": null, "intensity": null, "scope": null}], "summary": "Разгибания 4×10–12"}
«поставь на сгибания с супинацией 30 кг» -> {"actions": [{"type": "weight", "said": "сгибания с супинацией", "exercise": "сгибания с гантелями на бицепс с супинацией", "weight_kg": 30, "date": null}], "summary": "Сгибания с супинацией 30 кг на ближайшую тренировку"}
«сегодня жим лёжа поставь вторым, после тяги» -> {"actions": [{"type": "reorder", "day": {"weekday": null, "focus": null, "when": "today"}, "order": ["тяга вертикального блока", "жим лёжа"]}], "summary": "Сегодня тяга, потом жим"}
«убери в базе жим, а на тягу поставь 3 подхода» -> {"actions": [{"type": "remove", "day": {"weekday": null, "focus": "база", "when": null}, "exercise": "жим лёжа", "scope": null}, {"type": "prescribe", "day": {"weekday": null, "focus": "база", "when": null}, "exercise": "тяга вертикального блока", "sets": 3, "repsMin": null, "repsMax": null, "dropReps": null, "intensity": null, "scope": null}], "summary": "База: без жима, тяга 3 подхода"}
«замени сгибания в понедельник на молотки» -> {"actions": [{"type": "clarify", "question": "Какие сгибания заменить на молотки?", "options": ["с супинацией", "с пронацией"]}], "summary": ""}
«поменяй местами руки и базу на этой неделе» -> {"actions": [{"type": "swap_days", "a": {"weekday": null, "focus": "руки", "when": null}, "b": {"weekday": null, "focus": "база", "when": null}, "scope": "this_week"}], "summary": "Руки и база меняются местами"}
«сделай сегодня базу вместо рук» -> {"actions": [{"type": "swap_days", "a": {"weekday": null, "focus": null, "when": "today"}, "b": {"weekday": null, "focus": "база", "when": null}, "scope": null}], "summary": "Сегодня база, руки — в её день"}
«перенеси тренировку на завтра» -> {"actions": [{"type": "move_day", "src": {"weekday": null, "focus": null, "when": "today"}, "dst": {"weekday": null, "focus": null, "when": "tomorrow"}, "scope": null}], "summary": "Сегодняшняя тренировка — завтра"}
«перенеси пятницу на субботу» -> {"actions": [{"type": "move_day", "src": {"weekday": 5, "focus": null, "when": null}, "dst": {"weekday": 6, "focus": null, "when": null}, "scope": null}], "summary": "Пятничная тренировка — в субботу"}
«следующая неделя — делоад» -> {"actions": [{"type": "deload", "week": "next_week", "start": null}], "summary": "Разгрузочная неделя с понедельника"}
«давай на этой неделе делоад» -> {"actions": [{"type": "deload", "week": "this_week", "start": null}], "summary": "Разгрузочная неделя с сегодня"}
«на этой неделе делоад, спал плохо» -> {"actions": [], "summary": ""}
«сегодня облегчённо, −20 %» -> {"actions": [], "summary": ""}
«удали последнюю запись» -> {"actions": [], "summary": ""}
"""


def build_edit_messages(text: str, context: str) -> list[dict[str, str]]:
    """`context`: today, the program week by days, the next week's differences, weights, catalog
    (chat_edit.prompt_context)."""
    return [
        {"role": "system", "content": EDIT_SYSTEM_PROMPT},
        {"role": "user", "content": f"{context}\nСообщение: {text}"},
    ]
