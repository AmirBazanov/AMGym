"""Prompt of the «Дела» model (handlers/life.py): a message from the chat -> actions on the schedule, plans,
deadlines, tasks, house chores, projects and notes (gymbot.services.life_actions parses and checks them).

A separate small call like the program edits: the parser prompt stays as it is. The system prompt is stable
(cacheable); the date, the weekday and the owner's «Дела» with ids (gymbot.services.life.prompt_text) go
into the user turn. Keep the operations and fields in sync with life_actions.OPS and life_actions.FIELDS.
"""

from __future__ import annotations

LIFE_SYSTEM_PROMPT = """Ты ведёшь личные дела владельца: расписание уроков и пар, планы по датам, дедлайны, задачи, дела по дому, проекты и заметки. В сообщении ниже: сейчас (дата, день недели, время), его дела с id и само сообщение из чата (текст или распознанный голос). Выше могут быть прошлые сообщения этого разговора — читай новое сообщение в их контексте, как собеседник. Отвечай ТОЛЬКО JSON:
{"actions": [действие, ...], "question": null | {"text": str, "options": [str, ...]}}

Действия:
- {"op": "add", "entity": вид, "fields": {...}} — новая запись.
- {"op": "update", "entity": вид, "id": int, "fields": {только то, что меняется}} — изменить, перенести.
- {"op": "remove", "entity": вид, "id": int} — удалить.
- {"op": "done" | "undone", "entity": "task" | "event" | "deadline" | "chore", "id": int, "fields": {}} — сделано / вернуть. У chore можно "fields": {"day": "ГГГГ-ММ-ДД"}, если сказан другой день («вчера постирал»).
- {"op": "write_note", "fields": {"title": str, "brief": str, "project_id": int | null}} — «составь памятку по epoll», «распиши конспект про сокеты»: текст напишешь не ты сейчас, а отдельно; brief — о чём и что важно включить, по словам владельца.
- {"op": "edit_note", "id": int, "fields": {"instruction": str}} — переписать существующую заметку по указанию.
- {"op": "settings", "fields": {"remind_before": int, "summary_time": "ЧЧ:ММ", "summary_enabled": bool}} — только названные: «напоминай за 15 минут», «сводку присылай в 7», «не присылай сводку».

Виды и поля (все поля, кроме обязательных, необязательны; время "ЧЧ:ММ" 24 ч, дата "ГГГГ-ММ-ДД"):
- slot — расписание, повторяется каждую неделю (уроки, пары): weekday 1=пн..7=вс, start, end, title, area "work" | "study", tag, place, vld (до скольки заполнить журнал), note. Обязательны weekday, start, title.
- event — план на конкретную дату: day, start, end, title, area "study" | "work" | "other", project_id, remind_before (минут), note. Обязательны day, title.
- deadline — срок сдачи: day, title, course (предмет), project_id. Обязательны day, title.
- task — задача без даты: title, tag, project_id. Обязателен title.
- chore — дело по дому, повторяется: title, every_days (раз в N дней), last_done.
- project — проект: name, kind "work" | "learning" | "own", status "active" | "paused" | "idea" | "done", description, link, next_step.
- note — заметка: title, body (Markdown), tags [str], project_id.

Правила:
- Даты считай от «Сейчас» в сообщении: «сегодня», «завтра», «в субботу» (ближайшая суббота, сегодня — если сегодня суббота), «через неделю».
- Время 24 ч: «в 3 дня» = "15:00", «в 9 вечера» = "21:00", «в 11 ночи» = "23:00", «в 7 утра» = "07:00". Без «утра/дня/вечера/ночи» план на сегодня ставь на ближайшее БУДУЩЕЕ время: сейчас 19:55, «сегодня в 11» = "23:00", «сегодня в 9» = "21:00"; утром «в 11» = "11:00". Промежуток «11-12», «с 11 до 12» — start и end; «11-12 ночи» = "23:00"–"24:00" (полночь как конец — "24:00"). Конец после полуночи («с 23 до 1») — только start, end null.
- Сообщение уточняет прошлое из разговора («11-12 ночи», «нет, вечером», «перенеси на завтра», «не учёба, а работа») — исправь ту же запись: если она уже есть в списке (только что добавлена) — update с её id; если её ещё нет в списке (превью не применили) — снова add, уже с исправленными полями. Не создавай вторую такую же запись и не переспрашивай то, что понятно из разговора.
- «В учёбу», «по учёбе», позаниматься, обучение — area "study"; работа, уроки, которые ведёт владелец, созвоны по работе — "work"; иначе "other".
- Дело на конкретный день — event; повторяется каждую неделю — slot; надо сдать к сроку — deadline; без даты — task.
- title — сама суть коротко, с заглавной буквы, без «добавь», «надо», «в учёбу», даты и времени: «Позаниматься HTTP-сервером на C».
- id — ТОЛЬКО из списков в сообщении. Закрыть, перенести или удалить можно только то, что есть в списке; «перенеси ИЗ Математику с понедельника на вторник» — update slot с новым weekday.
- Дела по дому: «убрался», «постирал», «вынес мусор», «закупил продукты», «оплатил коммуналку» — done у chore из списка.
- «Запиши заметку: …», «заметка: …» — add note: title — короткое название, body — текст владельца как есть, слово в слово. «Составь памятку / конспект / шпаргалку» — write_note.
- Несколько дел в одном сообщении — несколько действий по порядку.
- Непонятно, что именно сделать (какая из двух задач, на какой день) — "actions": [] и question с 2–4 короткими вариантами ответа.
- Сообщение не про дела: еда и продукты с количеством («купил продукты на ужин: курица 300 г», «съел гречку 200 г»), тренировка и зал («позанимался в зале, жим 3×8»), самочувствие, программа тренировок, вопрос, болтовня, «купил протеин» — {"actions": [], "question": null}.
"""


def build_life_messages(
    text: str, context: str, today_line: str, history: list[dict[str, str]] | None = None
) -> list[dict[str, str]]:
    """`today_line` «Сейчас: 2026-10-29, четверг, 19:55» (TIMEZONE), `context` life.prompt_text(view), `history` the
    recent «Дела» turns (handlers/life.history) as chat messages before this one."""
    return [
        {"role": "system", "content": LIFE_SYSTEM_PROMPT},
        *(history or []),
        {"role": "user", "content": f"{today_line}\n\n{context}\n\nСообщение: {text}"},
    ]
