"""HTTP API for the Mini App, plus the built Mini App itself (miniapp/dist) on the same port."""


import logging
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from aiogram import Bot
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.routing import BaseRoute

from gymbot.api.auth import InitDataError, TelegramUser, validate_init_data
from gymbot.config import Settings
from gymbot.db.models import FoodEntry, Reminder, User, UserFact, UserProgram, WellbeingEntry
from gymbot.db.session import Sessionmaker
from gymbot.llm.openrouter import OpenRouterClient
from gymbot.mcp_server import MCP_PATH, BearerGuard, mcp_http
from gymbot.services import facts as fx
from gymbot.services import nutrition as nut
from gymbot.services import plan as day_plan
from gymbot.services import profile as prof
from gymbot.services import reminders as rem
from gymbot.services import wellbeing as wb
from gymbot.services import workouts as ws
from gymbot.services.access import is_allowed
from gymbot.services.programs import load_program
from gymbot.services.users import active_program, get_or_create_user, set_program

log = logging.getLogger(__name__)

NUTRITION_MIN_DATE = date(2000, 1, 1)


class StateOut(BaseModel):
    programId: str
    startDate: date
    restSeconds: int
    targets: nut.Targets
    profile: prof.Profile
    history: list[ws.WorkoutOut]


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
    `bot` is the running bot (None with RUN_BOT=false); the MCP tool send_message uses it.

    `routers` are extra routes (the Telegram webhook); they go before the Mini App mount at "/",
    which would otherwise swallow them.
    """
    tz = ZoneInfo(settings.timezone)
    clients: list[OpenRouterClient] = [llm] if llm is not None else []

    def get_llm() -> OpenRouterClient:
        if not clients:
            clients.append(OpenRouterClient(settings))
        return clients[0]

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
            startDate=up.started_on,
            restSeconds=user.rest_seconds,
            targets=nut.user_targets(user),
            profile=prof.user_profile(user),
            history=history,
        )

    @app.get("/api/health")
    async def health() -> dict[str, str]:
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
        return out

    @app.post("/api/workouts")
    async def post_workout(body: ws.WorkoutIn, session: Session, tg: TgUser) -> ws.WorkoutOut:
        user, up = await current(session, tg)
        try:
            saved = await ws.save_from_miniapp(session, user, up, body, tz)
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        await session.commit()
        w = await ws.get_workout(session, user, saved.id)
        assert w is not None
        weeks = len((await load_program(session, up.program_id)).weeks)
        return ws.serialize(w, up, weeks, tz)

    @app.delete("/api/workouts/{workout_id}", status_code=204)
    async def delete_workout(workout_id: int, session: Session, tg: TgUser) -> None:
        user, _ = await current(session, tg)
        w = await ws.get_workout(session, user, workout_id)
        if w is None:
            raise HTTPException(404, "not found")
        await session.delete(w)
        await session.commit()

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
        f.text, f.active = text, active
        if changes.get("category") is not None:
            f.category = changes["category"]
        await session.commit()
        return fx.fact_out(f)

    @app.delete("/api/facts/{fact_id}", status_code=204)
    async def delete_fact(fact_id: int, session: Session, tg: TgUser) -> None:
        f = await own_fact(session, tg, fact_id)
        await session.delete(f)
        await session.commit()

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
        return reminder_out(r)

    @app.delete("/api/reminders/{reminder_id}", status_code=204)
    async def delete_reminder(reminder_id: int, session: Session, tg: TgUser) -> None:
        r = await own_reminder(session, tg, reminder_id)
        await session.delete(r)
        await session.commit()

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
