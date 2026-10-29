"""Prompt for tasks and house chores of the dashboard «Дела Амира» from the chat (gymbot.services.hub_tasks,
handlers/hub.py). A separate small call like the program edits: the parser prompt stays as it is. The lists with
their ids go into the user turn; keep the action shapes in sync with hub_tasks.parse_actions."""

from __future__ import annotations

HUB_SYSTEM_PROMPT = """Ты ведёшь список задач и дел по дому владельца по его сообщению из чата. В сообщении ниже: сегодняшняя дата, задачи (id: текст) и дела по дому (id: текст, как часто, когда последний раз). Отвечай ТОЛЬКО JSON:
{"actions": [действие, ...]}

Действия:
- {"type": "add_task", "text": str} — новая задача: «добавь задачу купить билеты», «в дела: позвонить маме», «надо не забыть обновить резюме». text — сама задача коротко, с заглавной буквы, без слов «добавь», «задача», «надо».
- {"type": "complete_task", "id": str} — задача сделана: «закрой задачу про резюме», «резюме обновил, отметь».
- {"type": "reopen_task", "id": str} — вернуть сделанную задачу в работу.
- {"type": "remove_task", "id": str} — удалить задачу: «удали задачу про родителей».
- {"type": "chore_done", "id": str, "date": "ГГГГ-ММ-ДД"|null} — дело по дому сделано: «убрался», «постирал», «вынес мусор», «закупил продукты», «оплатил коммуналку». date — только если назван другой день («вчера постирал»), иначе null.

Правила:
- id — ТОЛЬКО из списков в сообщении. Задачу или дело, которых нет в списке, не выдумывай: чтобы закрыть или удалить, нужна существующая задача; новую — add_task.
- Несколько дел в одном сообщении — несколько действий по порядку.
- Сообщение не про задачи и не про дела по дому (еда, тренировка, самочувствие, вопрос, программа тренировок, «купил протеин», «оплатил абонемент») — {"actions": []}.
"""


def build_hub_messages(text: str, context: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": HUB_SYSTEM_PROMPT},
        {"role": "user", "content": f"{context}\n\nСообщение: {text}"},
    ]
