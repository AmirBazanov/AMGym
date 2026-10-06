"""HTTP API for the Mini App, plus the built Mini App itself (miniapp/dist) on the same port."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date, datetime
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from gymbot.api.auth import InitDataError, TelegramUser, validate_init_data
from gymbot.config import Settings
from gymbot.db.models import User, UserProgram
from gymbot.db.session import Sessionmaker
from gymbot.services import workouts as ws
from gymbot.services.programs import load_program
from gymbot.services.users import active_program, get_or_create_user, set_program


class StateOut(BaseModel):
    programId: str
    startDate: date
    restSeconds: int
    history: list[ws.WorkoutOut]


class SettingsIn(BaseModel):
    programId: str | None = None
    startDate: date | None = None
    restSeconds: int | None = Field(default=None, ge=15, le=600)


def create_app(settings: Settings, sessionmaker: Sessionmaker) -> FastAPI:
    app = FastAPI(title="GymAPP API", docs_url="/api/docs", openapi_url="/api/openapi.json")
    tz = ZoneInfo(settings.timezone)

    async def get_session() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield session
            await session.commit()

    def tg_user(x_telegram_init_data: Annotated[str, Header()] = "") -> TelegramUser:
        if not x_telegram_init_data:
            if settings.dev_user_id:
                return TelegramUser(id=settings.dev_user_id, name="dev")
            raise HTTPException(401, "open the app from Telegram")
        try:
            user = validate_init_data(x_telegram_init_data, settings.bot_token)
        except InitDataError as e:
            raise HTTPException(401, f"invalid initData: {e}") from e
        if settings.allowed_user_ids and user.id not in settings.allowed_user_ids:
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
            programId=up.program.slug, startDate=up.started_on, restSeconds=user.rest_seconds, history=history
        )

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/state")
    async def get_state(session: Session, tg: TgUser) -> StateOut:
        user, up = await current(session, tg)
        return await state_for(session, user, up)

    @app.put("/api/settings")
    async def put_settings(body: SettingsIn, session: Session, tg: TgUser) -> StateOut:
        user, up = await current(session, tg)
        if body.restSeconds is not None:
            user.rest_seconds = body.restSeconds
        if body.programId or body.startDate:
            try:
                up = await set_program(
                    session, user, body.programId or up.program.slug, body.startDate or up.started_on
                )
            except LookupError as e:
                raise HTTPException(404, "unknown program") from e
        return await state_for(session, user, up)

    @app.post("/api/workouts")
    async def post_workout(body: ws.WorkoutIn, session: Session, tg: TgUser) -> ws.WorkoutOut:
        user, up = await current(session, tg)
        saved = await ws.save_from_miniapp(session, user, up, body, tz)
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

    @app.middleware("http")
    async def no_cache_index(request: Request, call_next):  # type: ignore[no-untyped-def]
        # Telegram's webview caches aggressively; always revalidate the HTML so new builds show up.
        response = await call_next(request)
        if not request.url.path.startswith(("/api", "/assets")):
            response.headers["Cache-Control"] = "no-cache"
        return response

    if settings.miniapp_dist.is_dir():
        app.mount("/", StaticFiles(directory=settings.miniapp_dist, html=True), name="miniapp")

    return app
