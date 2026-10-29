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
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select
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

if TYPE_CHECKING:
    from gymbot.services.life_actions import Act

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


# ---- writing (the caller validated the fields with life_actions.validate_fields, then commits with `commit`) ----


def _defaults(kind: str) -> dict[str, Any]:
    return {
        "slot": {"area": "work"}, "event": {"area": "other"}, "chore": {"every_days": 7},
        "project": {"kind": "own", "status": "active"}, "note": {"body": "", "tags": [], "author": "me"},
    }.get(kind, {})


async def get(session: AsyncSession, user_id: int, kind: str, row_id: int) -> Any | None:
    """The user's live (not deleted) row of `kind`, or None."""
    model = MODELS[kind]
    return await session.scalar(_alive(model, user_id).where(model.id == row_id))


async def _check_project(session: AsyncSession, user_id: int, fields: dict[str, Any]) -> None:
    pid = fields.get("project_id")
    if pid is not None and await get(session, user_id, "project", pid) is None:
        raise ValueError(f"Нет проекта #{pid}")


def _check_span(row: Any, fields: dict[str, Any]) -> None:
    """An update moving only the start or the end: the same rule as life_actions.fix_span, on the merged values."""
    start, end = fields.get("start", getattr(row, "start", None)), fields.get("end", getattr(row, "end", None))
    if start and end and end <= start:
        fields["end"] = "24:00" if end == "00:00" else None  # life_actions.MIDNIGHT (it imports this module)


async def add(
    session: AsyncSession, user_id: int, kind: str, fields: dict[str, Any], now: datetime, *, source: str = "me"
) -> Any:
    """A new row of `kind` (flushed, so it has its id). Unset fields take their defaults (slot area work, event
    area other, chore every 7 days, project own/active, note by me); a task goes last. ValueError: no such
    project."""
    await _check_project(session, user_id, fields)
    values = _defaults(kind) | fields
    if kind == "task" and "position" not in values:
        last = await session.scalar(select(func.max(LifeTask.position)).where(LifeTask.user_id == user_id))
        values["position"] = (last or 0) + 1
    row = MODELS[kind](user_id=user_id, source=source, created_at=now, updated_at=now, **values)
    session.add(row)
    await session.flush()
    return row


async def update(
    session: AsyncSession, user_id: int, kind: str, row_id: int, fields: dict[str, Any], now: datetime
) -> Any | None:
    """Change the given fields; None when there is no such live row. A plan moved (day or start) is reminded
    again, a deadline moved gets its eve reminder again. ValueError (nothing changed): no such project, the end
    before the start."""
    row = await get(session, user_id, kind, row_id)
    if row is None:
        return None
    await _check_project(session, user_id, fields)
    _check_span(row, fields)
    for key, value in fields.items():
        setattr(row, key, value)
    if kind == "event" and {"day", "start"} & fields.keys():
        row.reminded_at = None
    if kind == "deadline" and "day" in fields:
        row.reminded_on = None
    row.updated_at = now
    return row


async def remove(session: AsyncSession, user_id: int, kind: str, row_id: int, now: datetime) -> Any | None:
    """Soft delete; None when there is no such live row."""
    row = await get(session, user_id, kind, row_id)
    if row is not None:
        row.deleted_at = row.updated_at = now
    return row


async def set_done(
    session: AsyncSession, user_id: int, kind: str, row_id: int, done: bool, now: datetime, day: date | None = None
) -> Any | None:
    """Mark a task, plan or deadline done (`done_at`, kept when already set) or not; a chore done on `day` (local
    date; now's date when None) or never (`last_done` cleared). None when there is no such live row."""
    if kind not in DONE_KINDS:
        raise ValueError(f"«{kind}» нельзя отметить сделанным")
    row = await get(session, user_id, kind, row_id)
    if row is None:
        return None
    if kind == "chore":
        row.last_done = (day or now.date()) if done else None
    elif not done:
        row.done_at = None
    elif row.done_at is None:
        row.done_at = now
    row.updated_at = now
    return row


def set_settings(user: User, fields: dict[str, Any]) -> LifeSettings:
    """remind_before (minutes), summary_time "ЧЧ:ММ", summary_enabled on the user's row."""
    if "remind_before" in fields:
        user.life_remind_before = fields["remind_before"]
    if "summary_time" in fields:
        hours, minutes = fields["summary_time"].split(":")
        user.life_summary_minute = int(hours) * 60 + int(minutes)
    if "summary_enabled" in fields:
        user.life_summary_enabled = fields["summary_enabled"]
    return settings_of(user)


_ADDED = {"slot": "в расписание", "event": "план", "deadline": "дедлайн", "task": "задачу", "chore": "дело по дому",
          "project": "проект", "note": "заметку"}
_IN = {"slot": "в расписании", "event": "в планах", "deadline": "в дедлайнах", "task": "в задачах",
       "chore": "в делах по дому", "project": "в проектах", "note": "в заметках"}


def _name(row: Any) -> str:
    return getattr(row, "title", None) or getattr(row, "name", None) or f"#{row.id}"


def _when(row: Any, today: date) -> str:
    """« — сегодня в 23:00–24:00», « — каждый чт в 10:00» for plans, deadlines and the schedule; "" otherwise:
    the done message shows when, so a wrong day or hour is seen at once."""
    start, end = getattr(row, "start", None), getattr(row, "end", None)
    at = f" в {start}" + (f"–{end}" if end else "") if start else ""
    day = getattr(row, "day", None)
    if isinstance(day, date):
        near = {0: "сегодня", 1: "завтра", 2: "послезавтра", -1: "вчера"}.get((day - today).days)
        return f" — {near or f'{WEEKDAYS[day.weekday()]} {day:%d.%m}'}{at}"
    weekday = getattr(row, "weekday", None)
    if isinstance(weekday, int):
        return f" — каждый {WEEKDAYS[weekday - 1]}{at}"
    return ""


async def apply(
    session: AsyncSession, user: User, acts: list[Act], now: datetime, *, today: date | None = None,
    source: str = "me",
) -> list[str]:
    """Carry out validated actions (life_actions.parse / validate_fields) for `user` (a row of this session):
    one Russian line per action, what was done or why not (an id gone meanwhile, no such project). Commits and
    publishes "life" when anything changed. `today` (local) is the day a chore is done when the action has none;
    `source` marks new rows. write_note / edit_note are skipped: the bot writes the note and sends add / update."""
    today = today or now.date()
    lines: list[str] = []
    changed = False
    for a in acts:
        kind = a.entity or ""
        try:
            if a.op == "add":
                row = await add(session, user.id, kind, a.fields, now, source=source)
                lines.append(f"Добавил {_ADDED[kind]} «{_name(row)}»{_when(row, today)}")
            elif a.op == "settings":
                set_settings(user, a.fields)
                lines.append("Обновил настройки напоминаний")
            elif a.op in ("update", "remove", "done", "undone") and a.id is not None:
                if a.op == "update":
                    row = await update(session, user.id, kind, a.id, a.fields, now)
                    verb = "Изменил"
                elif a.op == "remove":
                    row = await remove(session, user.id, kind, a.id, now)
                    verb = "Удалил"
                else:
                    day = a.fields.get("day", today)
                    row = await set_done(session, user.id, kind, a.id, a.op == "done", now, day)
                    verb = "Отметил сделанным" if a.op == "done" else "Снял отметку:"
                if row is None:
                    lines.append(f"Не нашёл #{a.id} {_IN.get(kind, '')}")
                    continue
                lines.append(f"{verb} «{_name(row)}»{_when(row, today) if a.op == 'update' else ''}")
            else:
                log.warning("life.apply: %s is not applied here", a.op)
                continue
            changed = True
        except ValueError as e:
            lines.append(str(e))
    if changed:
        await commit(session, user.id)
    return lines


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
