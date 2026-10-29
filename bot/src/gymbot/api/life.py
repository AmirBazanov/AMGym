"""«Дела» for the Mini App: /api/life/* over gymbot.services.life.

- GET /api/life/view?date=ГГГГ-ММ-ДД: the snapshot (gymbot.services.life.LifeView) around today plus the agenda
  of `date` (default today, both local in TIMEZONE). Plans are loaded from the earlier of today/date minus a day
  to the later of them plus 14 days, so the agenda of a far date has its plans too; `today` stays today.
- POST /api/life/{kind} (201), PATCH /api/life/{kind}/{id}, DELETE /api/life/{kind}/{id} (204, soft) for
  slots | events | deadlines | tasks | chores | projects | notes; POST .../{id}/done and .../{id}/undone for
  tasks, events, deadlines, chores (a chore's done takes an optional {"day"}); GET /api/life/notes/{id} with
  the body; PUT /api/life/settings.
- JSON in camelCase, dates "ГГГГ-ММ-ДД", times "ЧЧ:ММ", empty = null. Bodies take the same keys as the
  objects without id and the computed ones (done, doneAt, left, updatedAt). A bad or unknown field: 422 with
  a Russian message; another user's, deleted or missing id: 404.

Auth is the caller's `current_user` dependency (initData, see gymbot.api.app.create_app): it opens and commits
its own session, so every handler here opens one more and only uses the user's id.
No `from __future__ import annotations` here: FastAPI must see the local Annotated aliases of make_router.
"""

import re
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Response
from pydantic import BaseModel

from gymbot.config import Settings
from gymbot.db.models import User
from gymbot.db.session import Sessionmaker
from gymbot.services import life
from gymbot.services import life_actions as la

PATHS = {"slots": "slot", "events": "event", "deadlines": "deadline", "tasks": "task", "chores": "chore",
         "projects": "project", "notes": "note"}
DONE_PATHS = ("tasks", "events", "deadlines", "chores")


def _iso(dt: datetime | None) -> str | None:
    return None if dt is None else life.aware(dt).strftime("%Y-%m-%dT%H:%M:%SZ")


def _snake(key: str) -> str:
    return re.sub(r"[A-Z]", lambda m: "_" + m.group(0).lower(), key)


# ---- objects ----


class SlotOut(BaseModel):
    id: int
    weekday: int
    start: str
    end: str | None
    title: str
    area: str
    tag: str | None
    place: str | None
    vld: str | None
    note: str | None


class EventOut(BaseModel):
    id: int
    day: date
    start: str | None
    end: str | None
    title: str
    area: str
    projectId: int | None
    remindBefore: int | None
    done: bool
    note: str | None


class DeadlineOut(BaseModel):
    id: int
    day: date
    title: str
    course: str | None
    projectId: int | None
    done: bool


class TaskOut(BaseModel):
    id: int
    title: str
    tag: str | None
    projectId: int | None
    done: bool
    doneAt: str | None


class ChoreOut(BaseModel):
    id: int
    title: str
    everyDays: int
    lastDone: date | None
    left: int  # days until due, < 0 overdue (life.chore_left)


class ProjectOut(BaseModel):
    id: int
    name: str
    kind: str
    status: str
    description: str | None
    link: str | None
    nextStep: str | None


class NoteOut(BaseModel):
    id: int
    title: str
    tags: list[str]
    author: str
    projectId: int | None
    updatedAt: str


class NoteFullOut(NoteOut):
    body: str


class AgendaOut(BaseModel):
    kind: str
    id: int
    title: str
    start: str | None
    end: str | None
    area: str | None
    place: str | None
    done: bool
    overdueDays: int


class SettingsOut(BaseModel):
    remindBefore: int
    summaryTime: str
    summaryEnabled: bool


class ViewUserOut(BaseModel):
    name: str | None  # who is signed in (the Mini App shows it after a /web sign-in)


class ViewOut(BaseModel):
    today: date
    user: ViewUserOut
    settings: SettingsOut
    slots: list[SlotOut]
    events: list[EventOut]
    deadlines: list[DeadlineOut]
    tasks: list[TaskOut]
    chores: list[ChoreOut]
    projects: list[ProjectOut]
    notes: list[NoteOut]
    agenda: list[AgendaOut]


def slot_out(r: Any) -> SlotOut:
    return SlotOut(id=r.id, weekday=r.weekday, start=r.start, end=r.end, title=r.title, area=r.area, tag=r.tag,
                   place=r.place, vld=r.vld, note=r.note)


def event_out(r: Any) -> EventOut:
    return EventOut(id=r.id, day=r.day, start=r.start, end=r.end, title=r.title, area=r.area,
                    projectId=r.project_id, remindBefore=r.remind_before, done=r.done_at is not None, note=r.note)


def deadline_out(r: Any) -> DeadlineOut:
    return DeadlineOut(id=r.id, day=r.day, title=r.title, course=r.course, projectId=r.project_id,
                       done=r.done_at is not None)


def task_out(r: Any) -> TaskOut:
    return TaskOut(id=r.id, title=r.title, tag=r.tag, projectId=r.project_id, done=r.done_at is not None,
                   doneAt=_iso(r.done_at))


def chore_out(r: Any, today: date) -> ChoreOut:
    return ChoreOut(id=r.id, title=r.title, everyDays=r.every_days, lastDone=r.last_done,
                    left=life.chore_left(r, today))


def project_out(r: Any) -> ProjectOut:
    return ProjectOut(id=r.id, name=r.name, kind=r.kind, status=r.status, description=r.description, link=r.link,
                      nextStep=r.next_step)


def note_out(r: Any) -> NoteOut:
    return NoteOut(id=r.id, title=r.title, tags=list(r.tags or []), author=r.author, projectId=r.project_id,
                   updatedAt=_iso(r.updated_at) or "")


def note_full_out(r: Any) -> NoteFullOut:
    return NoteFullOut(**note_out(r).model_dump(), body=r.body)


def settings_out(user: User) -> SettingsOut:
    s = life.settings_of(user)
    return SettingsOut(remindBefore=s.remind_before, summaryTime=s.summary_time, summaryEnabled=s.summary_enabled)


def agenda_out(i: life.AgendaItem) -> AgendaOut:
    return AgendaOut(kind=i.kind, id=i.id, title=i.title, start=i.start, end=i.end, area=i.area, place=i.place,
                     done=i.done, overdueDays=i.overdue_days)


def row_out(kind: str, row: Any, today: date) -> dict[str, Any]:
    """One object of `kind` as JSON (a note with its body)."""
    out: BaseModel
    if kind == "chore":
        out = chore_out(row, today)
    else:
        out = {"slot": slot_out, "event": event_out, "deadline": deadline_out, "task": task_out,
               "project": project_out, "note": note_full_out}[kind](row)
    return out.model_dump(mode="json")


def view_out(view: life.LifeView, user: User, day: date) -> ViewOut:
    return ViewOut(
        today=view.today, user=ViewUserOut(name=user.name), settings=settings_out(user),
        slots=[slot_out(r) for r in view.slots], events=[event_out(r) for r in view.events],
        deadlines=[deadline_out(r) for r in view.deadlines], tasks=[task_out(r) for r in view.tasks],
        chores=[chore_out(r, view.today) for r in view.chores], projects=[project_out(r) for r in view.projects],
        notes=[note_out(r) for r in view.notes], agenda=[agenda_out(i) for i in life.day_agenda(view, day)],
    )


# ---- routes ----


def make_router(
    sessionmaker: Sessionmaker, settings: Settings, current_user: Callable[..., Awaitable[User]]
) -> APIRouter:
    """`current_user`: the dependency that checks initData (401/403) and returns the user's row."""
    tz = ZoneInfo(settings.timezone)
    router = APIRouter(prefix="/api/life", tags=["life"])
    Owner = Annotated[User, Depends(current_user)]
    JsonBody = Annotated[dict[str, Any], Body()]

    def today() -> date:
        return datetime.now(tz).date()

    def kind_of(path: str) -> str:
        if path not in PATHS:
            raise HTTPException(404, "Not Found")
        return PATHS[path]

    def fields_of(kind: str, body: dict[str, Any], *, partial: bool) -> dict[str, Any]:
        try:
            return la.validate_fields(kind, {_snake(k): v for k, v in body.items()}, today(), partial=partial)
        except ValueError as e:
            raise HTTPException(422, str(e)) from e

    def not_found(row: Any) -> Any:
        if row is None:
            raise HTTPException(404, "Not Found")
        return row

    @router.get("/view")
    async def get_view(user: Owner, day: Annotated[date | None, Query(alias="date")] = None) -> ViewOut:
        now = today()
        day = day or now
        if abs((day - now).days) > la.DATE_RANGE:
            raise HTTPException(422, f"Дата дальше {la.DATE_RANGE} дней от сегодня")
        async with sessionmaker() as session:
            row = not_found(await session.get(User, user.id))
            view = await life.snapshot(
                session, user.id, now,
                events_from=min(now, day) - timedelta(days=life.EVENTS_BEFORE),
                events_to=max(now, day) + timedelta(days=life.EVENTS_AFTER),
            )
            return view_out(view, row, day)

    @router.put("/settings")
    async def put_settings(body: JsonBody, user: Owner) -> SettingsOut:
        fields = fields_of("settings", body, partial=True)
        async with sessionmaker() as session:
            row = not_found(await session.get(User, user.id))
            life.set_settings(row, fields)
            out = settings_out(row)
            await life.commit(session, user.id)
        return out

    @router.get("/notes/{row_id}")
    async def get_note(row_id: int, user: Owner) -> NoteFullOut:
        async with sessionmaker() as session:
            return note_full_out(not_found(await life.get(session, user.id, "note", row_id)))

    @router.post("/{path}", status_code=201)
    async def create(path: str, body: JsonBody, user: Owner) -> dict[str, Any]:
        kind = kind_of(path)
        fields = fields_of(kind, body, partial=False)
        async with sessionmaker() as session:
            try:
                row = await life.add(session, user.id, kind, fields, datetime.now(UTC))
            except ValueError as e:
                raise HTTPException(422, str(e)) from e
            out = row_out(kind, row, today())
            await life.commit(session, user.id)
        return out

    @router.patch("/{path}/{row_id}")
    async def patch(path: str, row_id: int, body: JsonBody, user: Owner) -> dict[str, Any]:
        kind = kind_of(path)
        fields = fields_of(kind, body, partial=True)
        async with sessionmaker() as session:
            try:
                row = not_found(await life.update(session, user.id, kind, row_id, fields, datetime.now(UTC)))
            except ValueError as e:
                raise HTTPException(422, str(e)) from e
            out = row_out(kind, row, today())
            await life.commit(session, user.id)
        return out

    @router.delete("/{path}/{row_id}", status_code=204)
    async def delete(path: str, row_id: int, user: Owner) -> Response:
        kind = kind_of(path)
        async with sessionmaker() as session:
            not_found(await life.remove(session, user.id, kind, row_id, datetime.now(UTC)))
            await life.commit(session, user.id)
        return Response(status_code=204)

    async def mark(path: str, row_id: int, user: User, done: bool, body: dict[str, Any] | None) -> dict[str, Any]:
        if path not in DONE_PATHS:
            raise HTTPException(404, "Not Found")
        kind = PATHS[path]
        day = None
        if kind == "chore" and done and body and body.get("day") is not None:
            try:
                day = la.check_date(body["day"], today())
            except ValueError as e:
                raise HTTPException(422, str(e)) from e
            if day > today():
                raise HTTPException(422, "Дата в будущем")
        async with sessionmaker() as session:
            row = not_found(await life.set_done(
                session, user.id, kind, row_id, done, datetime.now(UTC), day or today()
            ))
            out = row_out(kind, row, today())
            await life.commit(session, user.id)
        return out

    @router.post("/{path}/{row_id}/done")
    async def done(
        path: str, row_id: int, user: Owner, body: Annotated[dict[str, Any] | None, Body()] = None
    ) -> dict[str, Any]:
        return await mark(path, row_id, user, True, body)

    @router.post("/{path}/{row_id}/undone")
    async def undone(path: str, row_id: int, user: Owner) -> dict[str, Any]:
        return await mark(path, row_id, user, False, None)

    return router
