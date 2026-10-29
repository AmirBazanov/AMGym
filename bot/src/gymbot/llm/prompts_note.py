"""Prompt for the body of a «Дела» note the bot writes (handlers/life.py): «составь памятку по epoll» (the «Дела»
model returns write_note with a brief), a rewrite of the open draft («добавь пример», «короче») and edit_note
of a saved note. Plain text call (OpenRouterClient.complete_text, purpose "advice"); the answer is the
Markdown body as is."""

from __future__ import annotations

NOTE_SYSTEM_PROMPT = """Ты пишешь заметки для личной базы знаний владельца: памятки, конспекты, шпаргалки. Он программист и студент, пишет по-русски.
Отвечай только текстом заметки в Markdown, без вступления и без слов о себе («вот памятка», «надеюсь, поможет»).
- Первая строка — заголовок «# …».
- По существу и точно: определения, как это работает, шаги, подводные камни. Код — в блоках ``` с языком, короткими рабочими примерами.
- Коротко: до 3500 символов, списки вместо длинных абзацев. Не выдумывай API, флаги и цифры, в которых не уверен.
- Правка черновика: верни ПОЛНЫЙ новый текст заметки с учётом указания, остальное сохрани."""


def build_note_messages(brief: str, draft: str | None, instruction: str | None) -> list[dict[str, str]]:
    """A new note on `brief`; with `draft` (the current text) and `instruction`, that text rewritten."""
    if draft is None:
        user = f"Напиши заметку: {brief}"
    else:
        user = f"Заметка: {brief}\n\nТекущий текст:\n{draft}\n\nПерепиши по указанию: {instruction or 'улучши'}"
    return [{"role": "system", "content": NOTE_SYSTEM_PROMPT}, {"role": "user", "content": user}]
