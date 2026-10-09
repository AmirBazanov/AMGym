"""HTTP API for the Mini App, plus the built Mini App itself (miniapp/dist) on the same port."""


import asyncio
import logging
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from aiogram import Bot
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
)
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.routing import BaseRoute

from gymbot.api.auth import InitDataError, TelegramUser, validate_init_data
from gymbot.config import Settings
from gymbot.db.models import FoodEntry, Reminder, User, UserFact, UserProgram, WellbeingEntry, Workout
from gymbot.db.session import Sessionmaker
from gymbot.llm.openrouter import OpenRouterClient
from gymbot.mcp_server import MCP_PATH, BearerGuard, mcp_http
from gymbot.services import active_workout as aw
from gymbot.services import baselines as bl
from gymbot.services import body_weight as bwt
from gymbot.services import facts as fx
from gymbot.services import live, workout_events
from gymbot.services import nutrition as nut
from gymbot.services import overrides as ov
from gymbot.services import plan as day_plan
from gymbot.services import profile as prof
from gymbot.services import program_editor as pe
from gymbot.services import programs as pg
from gymbot.services import reminders as rem
from gymbot.services import wellbeing as wb
from gymbot.services import workouts as ws
from gymbot.services.access import is_allowed
from gymbot.services.programs import load_program
from gymbot.services.users import active_program, get_or_create_user, set_program

log = logging.getLogger(__name__)

HEALTH_TIMEOUT = 2.0  # seconds for the database ping in /api/health
NUTRITION_MIN_DATE = date(2000, 1, 1)


class StateOut(BaseModel):
    programId: str
    # Programs.version of the active program: the Mini App refetches GET /api/programs/{programId} only
    # when the slug or this changes.
    programVersion: int
    startDate: date
    restSeconds: int
    targets: nut.Targets
    profile: prof.Profile
    history: list[ws.WorkoutOut]
    # Working weights from the user's words in active facts, newest per exercise (gymbot.services.baselines).
    baselines: list[bl.BaselineOut]
    # Weights set from the chat for today (local TIMEZONE) only (gymbot.services.overrides).
    weightOverrides: list[ov.WeightOverrideOut]
    # The workout in progress as last sent by PUT /api/workouts/active, while fresh and not finished
    # (gymbot.services.active_workout): the Mini App restores it when it has no local one.
    activeWorkout: aw.ActiveWorkoutOut | None = None


class LiveTokenOut(BaseModel):
    token: str
    expiresIn: int


class TargetsIn(BaseModel):
    """Partial update of the daily norm: omitted keys stay, null resets."""

    kcal: int | None = Field(default=None, ge=0, le=10000)
    protein: int | None = Field(default=None, ge=0, le=1000)
    fat: int | None = Field(default=None, ge=0, le=1000)
    carbs: int | None = Field(default=None, ge=0, le=1000)


class SettingsIn(BaseModel):
    programId: str | None = None
    startDate: date | None = None
    restSeconds: int | None = Field(default=None, ge=15, le=600)
    targets: TargetsIn | None = None
    profile: prof.ProfileIn | None = None


TIME_PATTERN = rem.TIME_PATTERN
ReminderKind = Literal["text", "nutrition", "advice", "checkin"]  # = rem.KINDS
Weekday = Annotated[int, Field(ge=0, le=6)]  # 0=Mon..6=Sun in TIMEZONE


class ReminderOut(BaseModel):
    id: int
    time: str  # HH:MM in TIMEZONE
    kind: ReminderKind
    text: str | None
    enabled: bool
    weekday: int | None  # None = every day


class ReminderIn(BaseModel):
    time: str = Field(pattern=TIME_PATTERN)
    kind: ReminderKind
    text: str | None = Field(default=None, max_length=200)
    enabled: bool = True
    weekday: Weekday | None = None


class ReminderPatch(BaseModel):
    """Partial update: omitted keys stay."""

    time: str | None = Field(default=None, pattern=TIME_PATTERN)
    kind: ReminderKind | None = None
    text: str | None = Field(default=None, max_length=200)
    enabled: bool | None = None
    weekday: Weekday | None = None  # null = every day


class FactIn(BaseModel):
    text: str = Field(max_length=2000)  # checked after cleaning: 1..fx.TEXT_MAX
    category: fx.Category = "other"


class FactPatch(BaseModel):
    """Partial update: omitted keys stay."""

    text: str | None = Field(default=None, max_length=2000)
    category: fx.Category | None = None
    active: bool | None = None


def fact_text(text: str) -> str:
    try:
        return fx.checked_text(text)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e


def reminder_out(r: Reminder) -> ReminderOut:
    return ReminderOut(
        id=r.id,
        time=rem.minute_to_hhmm(r.minute_of_day),
        kind=r.kind,  # type: ignore[arg-type]
        text=r.text,
        enabled=r.enabled,
        weekday=r.weekday,
    )


def normalize_reminder_text(kind: str, text: str | None) -> str | None:
    """kind=text needs 1..200 characters of text; the other kinds build their text at send time."""
    try:
        return rem.reminder_text(kind, text)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e


def create_app(
    settings: Settings,
    sessionmaker: Sessionmaker,
    llm: OpenRouterClient | None = None,
    routers: Sequence[APIRouter] = (),
    bot: Bot | None = None,
) -> FastAPI:
    """`llm` is the process-wide client (main.py shares it with the bot); without it one is made on first use.
    `bot` is the running bot (None with RUN_BOT=false); the MCP tool send_message and the messages after a
    saved workout (new records, the deload offer: gymbot.services.workout_events) use it.

    `routers` are extra routes (the Telegram webhook); they go before the Mini App mount at "/",
    which would otherwise swallow them.
    """
    tz = ZoneInfo(settings.timezone)
    clients: list[OpenRouterClient] = [llm] if llm is not None else []

    def get_llm() -> OpenRouterClient:
        if not clients:
            clients.append(OpenRouterClient(settings))
        return clients[0]

    def schedule_baselines(fact_id: int) -> None:
        """Working weights from a saved fact, in the background (only with the shared client from main.py)."""
        bl.schedule(sessionmaker, clients[0] if clients else None, fact_id)

    mcp_routes: list[BaseRoute] = []
    mcp_lifespan = None
    if settings.mcp_token:
        mcp_routes, mcp_lifespan = mcp_http(settings, sessionmaker, get_llm, bot)
        log.info("MCP server on %s (Bearer token from MCP_TOKEN)", MCP_PATH)
    else:
        log.info("MCP_TOKEN is empty: %s is not mounted", MCP_PATH)
    app = FastAPI(
        title="GymAPP API", docs_url="/api/docs", openapi_url="/api/openapi.json", lifespan=mcp_lifespan
    )

    async def get_session() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield session
            await session.commit()

    async def tg_user(request: Request, x_telegram_init_data: Annotated[str, Header()] = "") -> TelegramUser:
        if not x_telegram_init_data:
            # Dev bypass only for direct local requests, never through the tunnel (cloudflared adds
            # cf-connecting-ip) or from another machine.
            local = request.client is not None and request.client.host in ("127.0.0.1", "::1", "testclient")
            if settings.dev_user_id and local and "cf-connecting-ip" not in request.headers:
                return TelegramUser(id=settings.dev_user_id, name="dev")
            raise HTTPException(401, "open the app from Telegram")
        try:
            user = validate_init_data(x_telegram_init_data, settings.bot_token)
        except InitDataError as e:
            raise HTTPException(401, f"invalid initData: {e}") from e
        async with sessionmaker() as session:
            if not await is_allowed(session, settings, user.id):
                raise HTTPException(403, "this is a personal app")
        return user

    Session = Annotated[AsyncSession, Depends(get_session)]
    TgUser = Annotated[TelegramUser, Depends(tg_user)]

    async def current(session: AsyncSession, tg: TelegramUser) -> tuple[User, UserProgram]:
        user = await get_or_create_user(session, tg.id, tg.name)
        up = await active_program(session, user, datetime.now(tz).date())
        return user, up

    async def state_for(session: AsyncSession, user: User, up: UserProgram) -> StateOut:
        weeks = len((await load_program(session, up.program_id)).weeks)
        history = [ws.serialize(w, up, weeks, tz) for w in await ws.list_workouts(session, user)]
        return StateOut(
            programId=up.program.slug,
            programVersion=up.program.version,
            startDate=up.started_on,
            restSeconds=user.rest_seconds,
            targets=nut.user_targets(user),
            profile=prof.user_profile(user),
            history=history,
            baselines=await bl.current(session, user.id),
            weightOverrides=await ov.for_day(session, user.id, datetime.now(tz).date()),
            activeWorkout=await aw.current_out(session, user.id, datetime.now(UTC), tz),
        )

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        """Open, cheap and touching the database: the outside uptime check (.github/workflows/uptime.yml)."""

        async def ping() -> None:
            async with sessionmaker() as session:
                await session.execute(select(1))

        try:
            await asyncio.wait_for(ping(), HEALTH_TIMEOUT)
        except Exception as e:
            log.warning("health: database check failed: %s", type(e).__name__)
            raise HTTPException(503, "database unavailable") from e
        return {"status": "ok"}

    @app.get("/api/state")
    async def get_state(session: Session, tg: TgUser) -> StateOut:
        user, up = await current(session, tg)
        out = await state_for(session, user, up)
        await session.commit()  # first visit creates the user and their program
        return out

    @app.put("/api/settings")
    async def put_settings(body: SettingsIn, session: Session, tg: TgUser) -> StateOut:
        user, up = await current(session, tg)
        if body.restSeconds is not None:
            user.rest_seconds = body.restSeconds
        if body.targets is not None:
            nut.set_targets(user, body.targets.model_dump(exclude_unset=True))
        if body.profile is not None:
            prof.set_profile(user, body.profile.model_dump(exclude_unset=True))
        if body.programId or body.startDate:
            try:
                up = await set_program(
                    session, user, body.programId or up.program.slug, body.startDate or up.started_on
                )
            except LookupError as e:
                raise HTTPException(404, "unknown program") from e
        out = await state_for(session, user, up)
        await session.commit()  # commit before answering, so the client never sees OK for a lost write
        live.publish(user.id, "state")
        if body.programId or body.startDate:
            live.publish(user.id, "plan")  # another program day; the burst merges into one event
        return out

    # Programs: the server is their source of truth (templates from data/programs and the user's own copies).
    @app.get("/api/programs")
    async def list_programs(session: Session, tg: TgUser) -> list[pg.ProgramSummary]:
        user = await get_or_create_user(session, tg.id, tg.name)
        return await pg.program_summaries(session, user.id)

    @app.get("/api/programs/{slug}")
    async def get_program(slug: str, session: Session, tg: TgUser) -> pg.ProgramOut:
        """The whole program, weeks, days and exercises sorted. Another user's copy is 404."""
        user = await get_or_create_user(session, tg.id, tg.name)
        program = await pg.visible_program(session, user.id, slug)
        if program is None:
            raise HTTPException(404, "unknown program")
        return await pg.program_out(session, program, user.id)

    @app.patch(
        "/api/programs/{slug}",
        response_model=pe.PatchOut,
        responses={404: {"description": "unknown or another user's program"},
                   409: {"description": '{detail: "version", program} or {detail: "not_active"}'},
                   422: {"description": "{detail: Russian reason}"}},
    )
    async def patch_program(slug: str, body: pe.PatchIn, tg: TgUser) -> Response | pe.PatchOut:
        """Edit the active program (gymbot.services.program_editor). A template gets the user's own copy first,
        which becomes the active program (`switchedFrom`). Own session, not get_session (it commits after the
        answer): `dryRun` rolls the whole request back, and live events go out only after the commit."""
        async with sessionmaker() as session:
            user, up = await current(session, tg)
            user_id = user.id  # a rollback expires the ORM objects
            try:
                outcome = await pe.edit_program(
                    session, user, up, slug, body.version, body.ops,
                    today=datetime.now(tz).date(), dry_run=body.dryRun,
                )
            except LookupError as e:
                raise HTTPException(404, "unknown program") from e
            except pe.EditError as e:
                raise HTTPException(422, str(e)) from e
            except pe.Conflict as e:
                if e.program is None:
                    raise HTTPException(409, e.reason) from e
                current_out = await pg.program_out(session, e.program, user_id)
                return JSONResponse(
                    status_code=409, content={"detail": e.reason, "program": current_out.model_dump(mode="json")}
                )
            if body.dryRun:
                results, switched = outcome.results, outcome.switched_from
                await session.rollback()  # no copy, no version, no edits; nothing published
                stored = await pg.visible_program(session, user_id, slug)
                assert stored is not None
                return pe.PatchOut(
                    program=await pg.program_out(session, stored, user_id), switchedFrom=switched, results=results
                )
            out = pe.PatchOut(
                program=await pg.program_out(session, outcome.program, user_id),
                switchedFrom=outcome.switched_from,
                results=outcome.results,
            )
            await session.commit()
        live.publish(user_id, "program", "plan", "state")
        return out

    @app.get("/api/exercises")
    async def list_exercises(session: Session, tg: TgUser) -> list[pg.CatalogExercise]:
        """Exercises to pick from: in the user's programs or logged by them; most logged sets first."""
        user = await get_or_create_user(session, tg.id, tg.name)
        return await pg.exercise_choices(session, user.id)

    def owner_sender(chat_id: int) -> workout_events.Send | None:
        if bot is None:
            return None

        async def send(text: str, kb: object) -> object:
            return await bot.send_message(chat_id, text, reply_markup=kb)  # type: ignore[arg-type]

        return send

    @app.post("/api/workouts")
    async def post_workout(
        body: ws.WorkoutIn, session: Session, tg: TgUser, background: BackgroundTasks
    ) -> ws.WorkoutOut:
        user, up = await current(session, tg)
        retry = await session.scalar(
            select(Workout.id).where(Workout.client_id == body.id, Workout.user_id == user.id)
        )
        try:
            saved = await ws.save_from_miniapp(session, user, up, body, tz)
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        await aw.clear(session, user.id, body.id)  # finished: no longer in progress (retries included)
        await session.commit()
        live.publish(user.id, "workouts", "state")
        if retry is None:  # records and the deload offer once, after the answer (best-effort)
            background.add_task(
                workout_events.after_save, sessionmaker, user.id, [s.id for s in saved.sets],
                owner_sender(tg.id), tz, key=f"workout:{saved.id}", programs_dir=settings.programs_dir,
            )
        w = await ws.get_workout(session, user, saved.id)
        assert w is not None
        weeks = len((await load_program(session, up.program_id)).weeks)
        return ws.serialize(w, up, weeks, tz)

    # Registered before /api/workouts/{workout_id}, which would take "active" for an id.
    @app.put("/api/workouts/active", status_code=204)
    async def put_active_workout(body: ws.WorkoutIn, session: Session, tg: TgUser) -> None:
        """Snapshot of the workout in progress for the diary answer; never touches the history."""
        user_id = (await get_or_create_user(session, tg.id, tg.name)).id
        await session.commit()  # the user row exists before the snapshot, also on a retry below
        for attempt in range(2):
            try:
                await aw.save(session, user_id, body)
                await session.commit()
                return
            except ValueError as e:
                raise HTTPException(422, str(e)) from e
            except IntegrityError:  # a concurrent first PUT inserted the row: update it instead
                await session.rollback()
                if attempt:
                    raise

    @app.delete("/api/workouts/active", status_code=204)
    async def delete_active_workout(
        session: Session, tg: TgUser, clientId: Annotated[str | None, Query(max_length=64)] = None
    ) -> None:
        """Idempotent. With clientId only that workout's snapshot goes (a late cancel keeps a newer one)."""
        user = await get_or_create_user(session, tg.id, tg.name)
        await aw.clear(session, user.id, clientId)
        await session.commit()

    @app.delete("/api/workouts/{workout_id}", status_code=204)
    async def delete_workout(workout_id: int, session: Session, tg: TgUser) -> None:
        user, _ = await current(session, tg)
        w = await ws.get_workout(session, user, workout_id)
        if w is None:
            raise HTTPException(404, "not found")
        await session.delete(w)
        await session.commit()
        live.publish(user.id, "workouts", "state")

    def nutrition_date(value: date | None, name: str) -> date:
        """Default to today in TIMEZONE; reject dates whose day bounds would overflow datetime (500)."""
        today = datetime.now(tz).date()
        if value is None:
            return today
        if not NUTRITION_MIN_DATE <= value <= today + timedelta(days=366):
            raise HTTPException(422, f"{name} out of range")
        return value

    @app.get("/api/nutrition/day")
    async def nutrition_day(session: Session, tg: TgUser, date: date | None = None) -> nut.DaySummary:
        user = await get_or_create_user(session, tg.id, tg.name)
        return await nut.day_summary(session, user, nutrition_date(date, "date"), tz)

    @app.get("/api/nutrition/week")
    async def nutrition_week(session: Session, tg: TgUser, end: date | None = None) -> nut.WeekSummary:
        user = await get_or_create_user(session, tg.id, tg.name)
        return await nut.week_summary(session, user, nutrition_date(end, "end"), tz)

    @app.delete("/api/food/{food_id}", status_code=204)
    async def delete_food(food_id: int, session: Session, tg: TgUser) -> None:
        user = await get_or_create_user(session, tg.id, tg.name)
        entry = await session.get(FoodEntry, food_id)
        if entry is None or entry.user_id != user.id:
            raise HTTPException(404, "not found")
        await session.delete(entry)
        await session.commit()
        live.publish(user.id, "nutrition")

    @app.get("/api/wellbeing")
    async def list_wellbeing(
        session: Session, tg: TgUser, days: Annotated[int, Query(ge=1, le=366)] = 14
    ) -> list[wb.WellbeingOut]:
        user = await get_or_create_user(session, tg.id, tg.name)
        entries = await wb.recent_entries(session, user, datetime.now(tz).date(), days, tz)
        return [wb.entry_out(e, tz) for e in entries]

    @app.delete("/api/wellbeing/{entry_id}", status_code=204)
    async def delete_wellbeing(entry_id: int, session: Session, tg: TgUser) -> None:
        user = await get_or_create_user(session, tg.id, tg.name)
        entry = await session.get(WellbeingEntry, entry_id)
        if entry is None or entry.user_id != user.id:
            raise HTTPException(404, "not found")
        await session.delete(entry)
        await session.commit()
        live.publish(user.id, "wellbeing", "plan")

    # Body weight, one measurement per local day (gymbot.services.body_weight). Writes publish "weight" (the
    # chart) and "state" (the profile weight follows the newest day).
    @app.get("/api/body-weight")
    async def list_body_weight(
        session: Session, tg: TgUser, days: Annotated[int, Query(ge=1, le=bwt.MAX_DAYS)] = bwt.DEFAULT_DAYS
    ) -> list[bwt.BodyWeightOut]:
        user = await get_or_create_user(session, tg.id, tg.name)
        rows = await bwt.series(session, user.id, datetime.now(tz).date(), days)
        return [bwt.entry_out(r) for r in rows]

    @app.post("/api/body-weight")
    async def post_body_weight(body: bwt.BodyWeightIn, session: Session, tg: TgUser) -> bwt.BodyWeightOut:
        user = await get_or_create_user(session, tg.id, tg.name)
        now = datetime.now(UTC)
        today = now.astimezone(tz).date()
        day = body.date or today
        if not bwt.MIN_DATE <= day <= today:
            raise HTTPException(422, f"date must be {bwt.MIN_DATE}..{today}")
        saved = await bwt.upsert(session, user, day, body.weightKg, bwt.measured_at_for(day, tz, now), "miniapp")
        out = bwt.entry_out(saved.row)
        await session.commit()
        live.publish(user.id, "weight", "state")
        return out

    @app.delete("/api/body-weight/{day}", status_code=204)
    async def delete_body_weight(day: date, session: Session, tg: TgUser) -> None:
        user = await get_or_create_user(session, tg.id, tg.name)
        if not await bwt.remove(session, user, day):
            raise HTTPException(404, "not found")
        await session.commit()
        live.publish(user.id, "weight", "state")

    # 409 only for the active-facts limit: the Mini App shows its limit message on any 409.
    @app.get("/api/facts")
    async def list_facts(session: Session, tg: TgUser) -> list[fx.FactOut]:
        user = await get_or_create_user(session, tg.id, tg.name)
        rows = await session.scalars(
            select(UserFact).where(UserFact.user_id == user.id).order_by(UserFact.created_at.desc(), UserFact.id.desc())
        )
        return [fx.fact_out(f) for f in rows]

    @app.post("/api/facts", status_code=201)
    async def create_fact(body: FactIn, response: Response, session: Session, tg: TgUser) -> fx.FactOut:
        user = await get_or_create_user(session, tg.id, tg.name)
        added = await fx.add_fact(session, user.id, fact_text(body.text), body.category)
        if added.status == "limit":
            raise HTTPException(409, f"at most {fx.MAX_ACTIVE} active facts")
        await session.commit()
        assert added.fact is not None
        live.publish(user.id, "facts")
        schedule_baselines(added.fact.id)
        if added.status == "duplicate":
            response.status_code = 200  # the same active fact exists: return it, create nothing
        return fx.fact_out(added.fact)

    async def own_fact(session: AsyncSession, tg: TelegramUser, fact_id: int) -> UserFact:
        user = await get_or_create_user(session, tg.id, tg.name)
        f = await session.get(UserFact, fact_id)
        if f is None or f.user_id != user.id:
            raise HTTPException(404, "not found")
        return f

    @app.patch("/api/facts/{fact_id}")
    async def patch_fact(fact_id: int, body: FactPatch, session: Session, tg: TgUser) -> fx.FactOut:
        f = await own_fact(session, tg, fact_id)
        changes = body.model_dump(exclude_unset=True)
        text = fact_text(changes["text"]) if changes.get("text") is not None else f.text
        active = changes["active"] if changes.get("active") is not None else f.active
        if active and await fx.find_duplicate(session, f.user_id, text, exclude_id=f.id) is not None:
            raise HTTPException(422, "the same fact is already active")
        if active and not f.active and await fx.count_active(session, f.user_id) >= fx.MAX_ACTIVE:
            raise HTTPException(409, f"at most {fx.MAX_ACTIVE} active facts")
        if text != f.text:
            await bl.reset(session, f)  # the old weights came from the old text
        f.text, f.active = text, active
        if changes.get("category") is not None:
            f.category = changes["category"]
        await session.commit()
        live.publish(f.user_id, "facts", "state")  # a changed or deactivated fact moves working weights
        if f.active and f.baselines_at is None:
            schedule_baselines(f.id)
        return fx.fact_out(f)

    @app.delete("/api/facts/{fact_id}", status_code=204)
    async def delete_fact(fact_id: int, session: Session, tg: TgUser) -> None:
        f = await own_fact(session, tg, fact_id)
        await session.delete(f)  # its baselines go too (ORM cascade)
        await session.commit()
        live.publish(f.user_id, "facts", "state")

    async def today_plan(session: AsyncSession, tg: TelegramUser, force: bool) -> day_plan.DayPlanOut:
        now = day_plan.utcnow()
        user = await get_or_create_user(session, tg.id, tg.name)
        await active_program(session, user, now.astimezone(tz).date())  # first visit starts a program
        await session.commit()  # no write lock while the model thinks
        built = await day_plan.get_or_build(session, user, settings, get_llm(), tz, now, force=force)
        if built is None:
            raise HTTPException(404, "not a training day")
        return built.out

    @app.get("/api/plan/today")
    async def get_today_plan(session: Session, tg: TgUser) -> day_plan.DayPlanOut:
        return await today_plan(session, tg, force=False)

    @app.post("/api/plan/today/regenerate")
    async def regenerate_today_plan(session: Session, tg: TgUser) -> day_plan.DayPlanOut:
        return await today_plan(session, tg, force=True)

    @app.get("/api/reminders")
    async def list_reminders(session: Session, tg: TgUser) -> list[ReminderOut]:
        user = await get_or_create_user(session, tg.id, tg.name)
        rows = await session.scalars(
            select(Reminder).where(Reminder.user_id == user.id).order_by(Reminder.minute_of_day, Reminder.id)
        )
        return [reminder_out(r) for r in rows]

    @app.post("/api/reminders", status_code=201)
    async def create_reminder(body: ReminderIn, session: Session, tg: TgUser) -> ReminderOut:
        user = await get_or_create_user(session, tg.id, tg.name)
        count = await session.scalar(select(func.count()).select_from(Reminder).where(Reminder.user_id == user.id))
        if (count or 0) >= rem.MAX_PER_USER:
            raise HTTPException(409, f"at most {rem.MAX_PER_USER} reminders")
        minute = rem.hhmm_to_minute(body.time)
        r = Reminder(
            user_id=user.id,
            minute_of_day=minute,
            kind=body.kind,
            text=normalize_reminder_text(body.kind, body.text),
            enabled=body.enabled,
            weekday=body.weekday,
            # A time that has already passed today fires from tomorrow, not right away.
            last_sent_on=rem.initial_last_sent(minute, datetime.now(UTC), tz),
        )
        session.add(r)
        await session.commit()
        live.publish(user.id, "reminders")
        return reminder_out(r)

    async def own_reminder(session: AsyncSession, tg: TelegramUser, reminder_id: int) -> Reminder:
        user = await get_or_create_user(session, tg.id, tg.name)
        r = await session.get(Reminder, reminder_id)
        if r is None or r.user_id != user.id:
            raise HTTPException(404, "not found")
        return r

    @app.patch("/api/reminders/{reminder_id}")
    async def patch_reminder(reminder_id: int, body: ReminderPatch, session: Session, tg: TgUser) -> ReminderOut:
        r = await own_reminder(session, tg, reminder_id)
        changes = body.model_dump(exclude_unset=True)
        kind = changes.get("kind") or r.kind
        text = changes.get("text", r.text)
        r.text = normalize_reminder_text(kind, text)
        r.kind = kind
        rearm = False
        if changes.get("time") is not None:
            minute = rem.hhmm_to_minute(changes["time"])
            rearm = minute != r.minute_of_day
            r.minute_of_day = minute
        if "weekday" in changes:  # null is a real value here: every day
            rearm = rearm or changes["weekday"] != r.weekday
            r.weekday = changes["weekday"]
        if changes.get("enabled") is not None:
            rearm = rearm or (changes["enabled"] and not r.enabled)
            r.enabled = changes["enabled"]
        if rearm:
            # Same rule as on create: a time already passed today waits for tomorrow.
            r.last_sent_on = rem.initial_last_sent(r.minute_of_day, datetime.now(UTC), tz)
        await session.commit()
        live.publish(r.user_id, "reminders")
        return reminder_out(r)

    @app.delete("/api/reminders/{reminder_id}", status_code=204)
    async def delete_reminder(reminder_id: int, session: Session, tg: TgUser) -> None:
        r = await own_reminder(session, tg, reminder_id)
        await session.delete(r)
        await session.commit()
        live.publish(r.user_id, "reminders")

    live_key = live.signing_key(settings.bot_token)
    live.install_log_redaction()  # the stream URL carries the token; uvicorn logs request paths

    @app.post("/api/live/token")
    async def live_token(session: Session, tg: TgUser) -> LiveTokenOut:
        """A short-lived token for GET /api/live (EventSource cannot send the initData header)."""
        user = await get_or_create_user(session, tg.id, tg.name)
        await session.commit()
        return LiveTokenOut(token=live.make_token(live_key, user.id), expiresIn=live.TOKEN_TTL)

    @app.get("/api/live", response_class=StreamingResponse)
    async def live_stream(token: Annotated[str, Query(max_length=200)] = "") -> StreamingResponse:
        """Server-Sent Events: `hello`, then `change` with {"topics": [...]}, `: ping` every 20 s.
        No session or initData dependency: the stream stays open for hours."""
        user_id = live.verify_token(live_key, token)
        if user_id is None:
            raise HTTPException(401, "invalid or expired live token")
        return StreamingResponse(
            live.events(user_id),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.middleware("http")
    async def no_cache_index(request: Request, call_next):  # type: ignore[no-untyped-def]
        # Telegram's webview caches aggressively; always revalidate the HTML so new builds show up.
        response = await call_next(request)
        if not request.url.path.startswith(("/api", "/assets")):
            response.headers["Cache-Control"] = "no-cache"
        return response

    for router in routers:
        app.include_router(router)

    app.router.routes.extend(mcp_routes)  # before the Mini App mount at "/", which swallows every path
    if settings.mcp_token:
        app.add_middleware(BearerGuard, token=settings.mcp_token)

    if settings.miniapp_dist.is_dir():  # last: the mount at "/" catches every path
        app.mount("/", StaticFiles(directory=settings.miniapp_dist, html=True), name="miniapp")

    return app
