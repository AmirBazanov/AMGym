"""«Дела»: the weekly schedule, plans by date, deadlines, tasks, house chores, projects and notes.

The only writer of the life_* tables (gymbot.db.models.LifeRow and its subclasses); the bot (part 2), the
reminders (part 3), the API (gymbot.api.life) and the Mini App read and change them through here. No Telegram,
no FastAPI: plain functions over an AsyncSession.

- Dates (`day`, `last_done`) are local dates in TIMEZONE, times are "ЧЧ:ММ" local strings, moments
  (`done_at`, `deleted_at`, ...) UTC.
- Rows are soft-deleted (`deleted_at`): every read here skips them, so a deleted id is "not found" everywhere.
- After a write the caller commits through `commit` (or `apply` does it), which publishes the live topic
  "life" for open Mini Apps.
- `import_legacy` moves the old dashboard (data.js in HUB_DIR + hub_state) in once, at start (main.py).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gymbot.config import Settings
from gymbot.db.models import (
    LifeChore,
    LifeDeadline,
    LifeEvent,
    LifeNote,
    LifeProject,
    LifeSlot,
    LifeTask,
    User,
)
from gymbot.db.session import Sessionmaker
from gymbot.services import life_import, live
from gymbot.services.access import owner_user

log = logging.getLogger(__name__)

AREAS_SLOT = ("work", "study")
AREAS_EVENT = ("study", "work", "other")
PROJECT_KINDS = ("work", "learning", "own")
PROJECT_STATUSES = ("active", "paused", "idea", "done")
NOTE_AUTHORS = ("me", "bot")
SOURCES = ("me", "bot", "learnharness", "import")
KINDS = ("slot", "event", "deadline", "task", "chore", "project", "note")
DONE_KINDS = ("event", "deadline", "task", "chore")  # kinds with done / undone
MODELS: dict[str, type[Any]] = {
    "slot": LifeSlot, "event": LifeEvent, "deadline": LifeDeadline, "task": LifeTask, "chore": LifeChore,
    "project": LifeProject, "note": LifeNote,
}

EVENTS_BEFORE = 1  # days before today in the snapshot
EVENTS_AFTER = 14  # days after today
TASKS_DONE_DAYS = 7  # closed tasks stay in the snapshot this long
NOTES_IN_VIEW = 30
WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")


@dataclass
class AgendaItem:
    kind: str  # "slot" | "event" | "deadline" | "chore"
    id: int
    title: str
    start: str | None  # "ЧЧ:ММ"
    end: str | None
    area: str | None  # work | study | other | None
    place: str | None
    done: bool
    overdue_days: int  # chores: days past due (0 = due today); others 0


@dataclass
class LifeView:
    """What the owner has in «Дела» around `today`: the whole week's schedule, plans from yesterday to two weeks
    ahead, open deadlines, open tasks and the ones closed in the last 7 days, all chores, projects not done,
    the newest 30 notes."""

    today: date
    slots: list[LifeSlot]
    events: list[LifeEvent]
    deadlines: list[LifeDeadline]
    tasks: list[LifeTask]
    chores: list[LifeChore]
    projects: list[LifeProject]
    notes: list[LifeNote]


@dataclass(frozen=True)
class LifeSettings:
    remind_before: int  # minutes before a timed plan
    summary_time: str  # "ЧЧ:ММ" in TIMEZONE
    summary_enabled: bool


def aware(dt: datetime) -> datetime:
    """SQLite drops the offset: values are stored in UTC, read naive ones back as UTC."""
    return dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)


def _alive(model: Any, user_id: int) -> Any:
    return select(model).where(model.user_id == user_id, model.deleted_at.is_(None))


def _by_time(start: str | None) -> tuple[bool, str]:
    """Sort key: timed first by "ЧЧ:ММ" (sorts as text), then untimed."""
    return (start is None, start or "")


# ---- reading ----


async def snapshot(
    session: AsyncSession, user_id: int, today: date, *, events_from: date | None = None,
    events_to: date | None = None,
) -> LifeView:
    """The LifeView of `today` (local date). `events_from`/`events_to` widen the plans window (the API's
    ?date= far from today); by default today-1 .. today+14."""
    lo = events_from or today - timedelta(days=EVENTS_BEFORE)
    hi = events_to or today + timedelta(days=EVENTS_AFTER)
    slots = list(await session.scalars(_alive(LifeSlot, user_id)))
    slots.sort(key=lambda s: (s.weekday, s.start, s.id))
    events = list(await session.scalars(_alive(LifeEvent, user_id).where(LifeEvent.day >= lo, LifeEvent.day <= hi)))
    events.sort(key=lambda e: (e.day, *_by_time(e.start), e.id))
    deadlines = list(await session.scalars(
        _alive(LifeDeadline, user_id).where(LifeDeadline.done_at.is_(None)).order_by(LifeDeadline.day, LifeDeadline.id)
    ))
    since = datetime.combine(today - timedelta(days=TASKS_DONE_DAYS), datetime.min.time(), UTC)
    tasks = list(await session.scalars(
        _alive(LifeTask, user_id).where(LifeTask.done_at.is_(None) | (LifeTask.done_at >= since))
    ))
    # Open ones by position, then the closed ones, last closed first.
    tasks.sort(key=lambda t: (False, t.position, t.id) if t.done_at is None
               else (True, -aware(t.done_at).timestamp(), t.id))
    chores = list(await session.scalars(_alive(LifeChore, user_id).order_by(LifeChore.id)))
    projects = list(await session.scalars(_alive(LifeProject, user_id).where(LifeProject.status != "done")))
    projects.sort(key=lambda p: (PROJECT_KINDS.index(p.kind) if p.kind in PROJECT_KINDS else 9, p.id))
    notes = list(await session.scalars(
        _alive(LifeNote, user_id).order_by(LifeNote.updated_at.desc(), LifeNote.id.desc()).limit(NOTES_IN_VIEW)
    ))
    return LifeView(today, slots, events, deadlines, tasks, chores, projects, notes)


def chore_left(chore: LifeChore, today: date) -> int:
    """Days until the chore is due: every_days - days since last_done (< 0 overdue); 0 when never done."""
    if chore.last_done is None:
        return 0
    return chore.every_days - (today - chore.last_done).days


def day_agenda(view: LifeView, day: date) -> list[AgendaItem]:
    """Everything of `day`: schedule slots of its weekday, plans, deadlines, and chores due by then (from today
    on; a past day gets no chores). Timed items by start, then untimed: plans, deadlines, chores."""
    items = [
        AgendaItem("slot", s.id, s.title, s.start, s.end, s.area, s.place, False, 0)
        for s in view.slots if s.weekday == day.isoweekday()
    ]
    items += [
        AgendaItem("event", e.id, e.title, e.start, e.end, e.area, None, e.done_at is not None, 0)
        for e in view.events if e.day == day
    ]
    items += [
        AgendaItem("deadline", d.id, d.title, None, None, None, None, d.done_at is not None, 0)
        for d in view.deadlines if d.day == day
    ]
    if day >= view.today:
        for c in view.chores:
            left = chore_left(c, day)
            if left <= 0:
                items.append(AgendaItem("chore", c.id, c.title, None, None, None, None, False, -left))
    order = {"slot": 0, "event": 0, "deadline": 1, "chore": 2}
    items.sort(key=lambda i: (*_by_time(i.start), order[i.kind]))
    return items


def _day_text(day: date, today: date) -> str:
    near = {0: "сегодня", 1: "завтра", -1: "вчера"}.get((day - today).days)
    return f"{day.isoformat()} {WEEKDAYS[day.weekday()]}" + (f" ({near})" if near else "")


def _span(start: str | None, end: str | None) -> str:
    if start is None:
        return ""
    return f" {start}–{end}" if end else f" {start}"


def prompt_text(view: LifeView) -> str:
    """The view as compact text with ids, for the models (part 2): one line per row, sections in Russian."""
    t = view.today
    lines = ["Расписание (каждую неделю; slot):"]
    lines += [
        f"- #{s.id} {WEEKDAYS[s.weekday - 1]}{_span(s.start, s.end)} {s.title} [{s.area}"
        + (f", {s.tag}" if s.tag else "") + "]" + (f" ({s.place})" if s.place else "")
        + (f", журнал до {s.vld}" if s.vld else "")
        for s in view.slots
    ] or ["- (нет)"]
    lines += ["", "Планы по датам (event):"]
    lines += [
        f"- #{e.id} {_day_text(e.day, t)}{_span(e.start, e.end)} {e.title} [{e.area}]"
        + (f" проект #{e.project_id}" if e.project_id else "") + (" [сделано]" if e.done_at else "")
        for e in view.events
    ] or ["- (нет)"]
    lines += ["", "Дедлайны (deadline):"]
    lines += [
        f"- #{d.id} {_day_text(d.day, t)} {d.title}" + (f" ({d.course})" if d.course else "")
        for d in view.deadlines
    ] or ["- (нет)"]
    lines += ["", "Задачи (task):"]
    lines += [
        f"- #{x.id} {x.title}" + (f" [{x.tag}]" if x.tag else "") + (f" проект #{x.project_id}" if x.project_id else "")
        + (" [сделано]" if x.done_at else "")
        for x in view.tasks
    ] or ["- (нет)"]
    lines += ["", "Дела по дому (chore):"]
    for c in view.chores:
        left = chore_left(c, t)
        due = "пора" if left == 0 else (f"просрочено на {-left} дн." if left < 0 else f"через {left} дн.")
        last = c.last_done.isoformat() if c.last_done else "не отмечалось"
        lines.append(f"- #{c.id} {c.title}, раз в {c.every_days} дн., последний раз {last}, {due}")
    if not view.chores:
        lines.append("- (нет)")
    lines += ["", "Проекты (project):"]
    lines += [
        f"- #{p.id} {p.name} [{p.kind}, {p.status}]" + (f", следующий шаг: {p.next_step}" if p.next_step else "")
        for p in view.projects
    ] or ["- (нет)"]
    lines += ["", "Заметки (note):"]
    lines += [f"- #{n.id} {n.title}" for n in view.notes] or ["- (нет)"]
    return "\n".join(lines)


def settings_of(user: User) -> LifeSettings:
    m = user.life_summary_minute
    return LifeSettings(user.life_remind_before, f"{m // 60:02d}:{m % 60:02d}", user.life_summary_enabled)


async def commit(session: AsyncSession, user_id: int) -> None:
    """Commit the session's «Дела» changes and tell open Mini Apps (topic "life")."""
    await session.commit()
    live.publish(user_id, "life")


# ---- import of the old dashboard ----


async def import_legacy(session: AsyncSession, settings: Settings, user: User, now: datetime) -> int:
    """Move the old dashboard into the tables once: data.js from HUB_DIR with its hub_state (see
    gymbot.services.life_import). Returns rows created and commits, marking users.life_imported_at.
    Already imported, no data.js or a broken one: 0 and nothing marked (a broken file is logged), so the next
    start tries again. Rows imported before (source "import", same external_id) are never made twice."""
    if user.life_imported_at is not None:
        return 0
    src = life_import.read_data_js(settings.hub_dir)
    if src is None:
        return 0
    from gymbot.services import hub  # heavy (MCP, programs); only needed here

    state = (await hub.get_state(session, user.id))["state"]
    try:
        rows = life_import.legacy_rows(src, state, now)
    except ValueError as e:
        log.warning("life import: data.js unreadable: %s", e)
        return 0
    made: dict[str, set[str]] = {}
    for kind in {r.kind for r in rows}:
        model = MODELS[kind]
        made[kind] = set(await session.scalars(
            select(model.external_id).where(model.user_id == user.id, model.source == "import")
        ))
    created = 0
    for r in rows:
        if r.external_id in made[r.kind]:
            continue
        made[r.kind].add(r.external_id)
        session.add(MODELS[r.kind](
            user_id=user.id, source="import", external_id=r.external_id, created_at=now, updated_at=now, **r.fields
        ))
        created += 1
    user.life_imported_at = now
    await commit(session, user.id)
    return created


async def import_on_start(sessionmaker: Sessionmaker, settings: Settings, now: datetime) -> int:
    """`import_legacy` for the owner, when there is one and they were not imported yet (main.py, at start)."""
    async with sessionmaker() as session:
        user = await owner_user(session, settings)
        if user is None or user.life_imported_at is not None:
            return 0
        return await import_legacy(session, settings, user, now)
