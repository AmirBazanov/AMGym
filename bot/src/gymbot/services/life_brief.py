"""«Дела» as short text for the diary answer (gymbot.services.answer): the block in the model's summary
(`context_block`) and a day's agenda appended to the database answer of «что у меня завтра?» (`day_text`).
Read only; the owner's rows through gymbot.services.life.
"""

from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from gymbot.config import Settings
from gymbot.db.models import User
from gymbot.services import life
from gymbot.services.access import owner_user

TASKS_MAX = 10
DEADLINES_DAYS = 14
NOTES_MAX = 15
_KIND = {"deadline": "дедлайн: ", "chore": "дом: "}


async def owner_view(
    session: AsyncSession, settings: Settings, user: User, today: date, *, until: date | None = None
) -> life.LifeView | None:
    """The owner's LifeView (plans up to `until` when it is further than two weeks); None for anyone else."""
    owner = await owner_user(session, settings)
    if owner is None or owner.id != user.id:
        return None
    return await life.snapshot(session, user.id, today, events_to=until)


def _day_label(day: date, today: date) -> str:
    near = {0: "Сегодня", 1: "Завтра", 2: "Послезавтра"}.get((day - today).days)
    stamp = f"{life.WEEKDAYS[day.weekday()]} {day:%d.%m}"
    return f"{near} ({stamp})" if near else stamp


def item_text(item: life.AgendaItem) -> str:
    """«10:00–11:30 ИЗ Математика (Восход)», «дедлайн: Лаба 1», «дом: Уборка (просрочено на 2 дн.)»."""
    at = f"{item.start}–{item.end} " if item.start and item.end else (f"{item.start} " if item.start else "")
    place = f" ({item.place})" if item.place else ""
    late = f" (просрочено на {item.overdue_days} дн.)" if item.kind == "chore" and item.overdue_days else ""
    done = " [сделано]" if item.done else ""
    return f"{at}{_KIND.get(item.kind, '')}{item.title}{place}{late}{done}"


def day_text(view: life.LifeView, day: date) -> str:
    """The day's agenda as bullet lines under «Дела на завтра (пт 30.10):», '' when the day is free."""
    items = life.day_agenda(view, day)
    if not items:
        return ""
    stamp = f"{life.WEEKDAYS[day.weekday()]} {day:%d.%m}"
    near = {0: "сегодня", 1: "завтра", 2: "послезавтра"}.get((day - view.today).days)
    head = f"Дела на {near} ({stamp})" if near else f"Дела на {stamp}"
    return f"{head}:\n" + "\n".join(f"• {item_text(i)}" for i in items)


def context_block(view: life.LifeView) -> str:
    """«Дела» for the answer model: today's and tomorrow's agenda, open tasks, deadlines within two weeks,
    learning projects with their next step, note titles."""
    today = view.today
    lines = ["Дела (раздел «Дела»: расписание, планы, дедлайны, задачи, дом, проекты, заметки):"]
    for day in (today, today + timedelta(days=1)):
        items = life.day_agenda(view, day)
        lines.append(f"{_day_label(day, today)}: " + ("; ".join(item_text(i) for i in items) or "свободно"))
    tasks = [t.title for t in view.tasks if t.done_at is None]
    more = f" и ещё {len(tasks) - TASKS_MAX}" if len(tasks) > TASKS_MAX else ""
    lines.append("Задачи: " + ("; ".join(tasks[:TASKS_MAX]) + more if tasks else "нет"))
    soon = [d for d in view.deadlines if d.day <= today + timedelta(days=DEADLINES_DAYS)]
    lines.append("Дедлайны до двух недель: " + (
        "; ".join(f"{d.day:%d.%m} {d.title}" + (f" ({d.course})" if d.course else "") for d in soon) or "нет"
    ))
    learning = [p for p in view.projects if p.kind == "learning" and p.status == "active"]
    if learning:
        lines.append("Обучение: " + "; ".join(
            p.name + (f" — следующий шаг: {p.next_step}" if p.next_step else "") for p in learning
        ))
    if view.notes:
        lines.append("Заметки: " + "; ".join(n.title for n in view.notes[:NOTES_MAX]))
    return "\n".join(lines)
