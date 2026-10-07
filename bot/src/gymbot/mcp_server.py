"""MCP server on /mcp of the FastAPI app: Streamable HTTP, stateless, JSON responses, static Bearer token.

Mounted by api/app.py only when MCP_TOKEN is set (see docs/reference/mcp-python-sdk.md for why routes are
added with `routes.extend`, why the session manager runs in the app lifespan and why Host is checked).
The app is personal, so every tool acts as the owner (gymbot.services.access.owner_user); the token
identifies the client, not a person. Read tools never create rows. Write tools are narrow: targets,
facts, reminders, today's plan and notes; no shell, no arbitrary SQL writes.

Tool docstrings are in Russian: they are the descriptions the model reads. Answers are compact JSON text.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import time
import tomllib
from collections import defaultdict
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute
from starlette.types import ASGIApp, Receive, Scope, Send

from gymbot.config import ROOT, Settings
from gymbot.db.models import Exercise, Reminder, User, UserFact, UserProgram, Workout, WorkoutSet
from gymbot.db.session import Sessionmaker
from gymbot.llm.openrouter import OpenRouterClient, routes_from
from gymbot.services import facts as fx
from gymbot.services import nutrition as nut
from gymbot.services import plan as day_plan
from gymbot.services import profile as prof
from gymbot.services import readonly_sql as rsql
from gymbot.services import reminders as rem
from gymbot.services import wellbeing as wb
from gymbot.services.access import owner_user
from gymbot.services.advice import epley
from gymbot.services.programs import find_day, format_item, load_program, program_position
from gymbot.services.users import active_program

log = logging.getLogger(__name__)

MCP_PATH = "/mcp"
MESSAGE_MAX = 4000
NOTE_PREFIX = "[mcp] "
_STARTED = time.monotonic()  # imported at startup by api/app.py: close enough to the process start

READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)
REMOVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False)
OUTSIDE = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True)

INSTRUCTIONS = (
    "Личный дневник зала одного владельца: тренировки, питание (КБЖУ), самочувствие, программа, факты о нём. "
    "Все инструменты работают от имени владельца. Вес в кг, даты и время — локальные в часовом поясе сервера. "
    "Инструменты записи меняют реальные данные: вызывай их только по явной просьбе владельца."
)


class BearerGuard:
    """Pure ASGI middleware: 401 unless `Authorization: Bearer <token>` matches (only under /mcp)."""

    def __init__(self, app: ASGIApp, token: str, prefix: str = MCP_PATH) -> None:
        self.app = app
        self.token = token.encode()
        self.prefix = prefix

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] == "http" and (path == self.prefix or path.startswith(self.prefix + "/")):
            auth = dict(scope["headers"]).get(b"authorization", b"")
            scheme, _, value = auth.partition(b" ")
            if scheme.lower() != b"bearer" or not hmac.compare_digest(value.strip(), self.token):
                response = JSONResponse({"error": "unauthorized"}, 401, headers={"WWW-Authenticate": "Bearer"})
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def allowed_hosts(public_url: str) -> list[str]:
    """Host headers the MCP transport accepts: local runs, and the public domain Caddy passes through."""
    hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    if public_url and (netloc := urlsplit(public_url).netloc):
        hosts.append(netloc)
    return hosts


# ---- formatting ----


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)


def num(x: float | Decimal | None) -> float | int | None:
    """82.0 -> 82, 82.46 -> 82.5: short numbers in answers."""
    if x is None:
        return None
    v = round(float(x), 1)
    return int(v) if v.is_integer() else v


def set_text(weight: Decimal | None, reps: int) -> str:
    return f"{num(weight)}×{reps}" if weight is not None else f"×{reps}"


def _local(dt: datetime, tz: ZoneInfo) -> datetime:
    return nut._aware(dt).astimezone(tz)


def _uptime(seconds: float) -> str:
    minutes = int(seconds // 60)
    days, minutes = divmod(minutes, 24 * 60)
    hours, minutes = divmod(minutes, 60)
    return (f"{days} д " if days else "") + f"{hours} ч {minutes} мин"


async def _git_commit() -> str | None:
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", str(ROOT), "rev-parse", "--short", "HEAD",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=3)
    except (OSError, TimeoutError):
        return None
    if proc.returncode != 0:
        return None
    return out.decode().strip() or None


def _package_version() -> str | None:
    try:
        with (ROOT / "bot" / "pyproject.toml").open("rb") as f:
            return tomllib.load(f)["project"]["version"]
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        return None


# ---- the server ----


def build_mcp(
    settings: Settings,
    sessionmaker: Sessionmaker,
    get_llm: Callable[[], OpenRouterClient],
    bot: Bot | None,
) -> MCPServer:
    """A fresh server with all tools (one per FastAPI app: its session manager runs only once)."""
    mcp = MCPServer("gymapp", instructions=INSTRUCTIONS, version=_package_version() or "")
    tz = ZoneInfo(settings.timezone)
    engine: AsyncEngine = sessionmaker.kw["bind"]
    version_cache: dict[str, str | None] = {}

    def tool(annotations: ToolAnnotations) -> Callable[[Any], Any]:
        return mcp.tool(annotations=annotations, structured_output=False)

    @asynccontextmanager
    async def owner_session() -> AsyncIterator[tuple[AsyncSession, User]]:
        async with sessionmaker() as session:
            user = await owner_user(session, settings)
            if user is None:
                raise ToolError("Владелец ещё не писал боту и не открывал дневник: данных нет.")
            yield session, user

    def today() -> date:
        return datetime.now(tz).date()

    # ---- read ----

    @tool(READ)
    async def nutrition_summary(days: Annotated[int, Field(ge=1, le=90)] = 7) -> str:
        """Питание за последние `days` дней (включая сегодня): по дням ккал и БЖУ (белки, жиры, углеводы, г),
        число записей и остаток до нормы (отрицательный = перебор); норма; средние по дням с записями;
        все записи последнего дня, где что-то было съедено."""
        end = today()
        day_list = [end - timedelta(days=i) for i in range(days - 1, -1, -1)]
        start_utc, _ = nut.local_day_bounds(day_list[0], tz)
        _, end_utc = nut.local_day_bounds(end, tz)
        async with owner_session() as (session, user):
            entries = await nut._entries(session, user, start_utc, end_utc)
            targets = nut.user_targets(user)
        by_day: dict[date, list[Any]] = defaultdict(list)
        for e in entries:
            by_day[_local(e.eaten_at, tz).date()].append(e)
        target_values = targets.model_dump()
        rows = []
        for d in day_list:
            totals = nut._sum(by_day[d])
            row: dict[str, Any] = {"date": d, "entries": len(by_day[d]), **{k: num(v) for k, v in totals.items()}}
            if by_day[d] and any(v is not None for v in target_values.values()):
                row["left"] = {k: num(Decimal(t) - totals[k]) for k, t in target_values.items() if t is not None}
            rows.append(row)
        logged = [d for d in day_list if by_day[d]]
        avg = None
        if logged:
            sums = nut._sum([e for d in logged for e in by_day[d]])
            avg = {k: num(v / len(logged)) for k, v in sums.items()}
        last = None
        if logged:
            last = {
                "date": logged[-1],
                "entries": [
                    {
                        "id": e.id,
                        "time": _local(e.eaten_at, tz).strftime("%H:%M"),
                        "what": e.description,
                        "grams": num(e.grams),
                        "kcal": num(e.kcal),
                        "protein": num(e.protein_g),
                        "fat": num(e.fat_g),
                        "carbs": num(e.carbs_g),
                    }
                    for e in by_day[logged[-1]]
                ],
            }
        return dumps(
            {
                "targets": target_values,
                "days": rows,
                "days_logged": len(logged),
                "average_per_logged_day": avg,
                "last_day": last,
            }
        )

    @tool(READ)
    async def training_summary(days: Annotated[int, Field(ge=1, le=365)] = 14) -> str:
        """Тренировки за последние `days` дней: дата, источник (miniapp/chat), упражнения с подходами
        «вес×повторы» (дропы через «→»), объём (сумма вес×повторы, кг). Плюс рекорды расчётного 1ПМ
        (формула Эпли) по каждому упражнению: за период и за всё время, с подходом и датой."""
        start = today() - timedelta(days=days - 1)
        async with owner_session() as (session, user):
            period = (
                await session.execute(
                    select(Workout.id, Workout.performed_on, Workout.source, Exercise.name, WorkoutSet.weight_kg,
                           WorkoutSet.reps, WorkoutSet.drop_index)
                    .join(WorkoutSet, WorkoutSet.workout_id == Workout.id)
                    .join(Exercise, Exercise.id == WorkoutSet.exercise_id)
                    .where(Workout.user_id == user.id, Workout.performed_on >= start)
                    .order_by(Workout.performed_on, Workout.id, WorkoutSet.set_index)
                )
            ).all()
            weighted = (
                await session.execute(
                    select(Exercise.name, WorkoutSet.weight_kg, WorkoutSet.reps, Workout.performed_on)
                    .join(WorkoutSet, WorkoutSet.exercise_id == Exercise.id)
                    .join(Workout, Workout.id == WorkoutSet.workout_id)
                    .where(Workout.user_id == user.id, WorkoutSet.weight_kg.is_not(None), WorkoutSet.reps > 0)
                )
            ).all()
        workouts: dict[int, dict[str, Any]] = {}
        for wid, day, source, name, weight, reps, drop_index in period:
            w = workouts.setdefault(wid, {"date": day, "source": source, "exercises": {}, "volume": Decimal(0)})
            if weight is not None:
                w["volume"] += Decimal(weight) * reps
            sets: list[str] = w["exercises"].setdefault(name, [])
            if drop_index and sets:
                sets[-1] += " → " + set_text(weight, reps)
            else:
                sets.append(set_text(weight, reps))
        best_all: dict[str, tuple[float, str, date]] = {}
        best_period: dict[str, tuple[float, str, date]] = {}
        for name, weight, reps, day in weighted:
            rec = (epley(float(weight), reps), set_text(weight, reps), day)
            if name not in best_all or rec[0] > best_all[name][0]:
                best_all[name] = rec
            if day >= start and (name not in best_period or rec[0] > best_period[name][0]):
                best_period[name] = rec

        def record(r: tuple[float, str, date] | None) -> dict[str, Any] | None:
            return {"e1rm": num(r[0]), "set": r[1], "date": r[2]} if r else None

        return dumps(
            {
                "period": {"from": start, "to": today()},
                "workouts": [
                    {"date": w["date"], "source": w["source"], "volume_kg": num(w["volume"]),
                     "exercises": [{"name": n, "sets": s} for n, s in w["exercises"].items()]}
                    for w in workouts.values()
                ],
                "e1rm_records": [
                    {"exercise": name, "period": record(best_period.get(name)), "all_time": record(best_all[name])}
                    for name in sorted(best_all)
                ],
            }
        )

    @tool(READ)
    async def wellbeing_summary(days: Annotated[int, Field(ge=1, le=365)] = 14) -> str:
        """Самочувствие за последние `days` дней: записи (сон, ч; качество сна, энергия, настроение — шкала 1..5;
        боли «место тяжесть/5»; заметка), от новых к старым, и сводка: средние, ночи короче 7 ч,
        как часто и когда последний раз упоминалась каждая боль."""
        async with owner_session() as (session, user):
            entries = [wb.entry_out(e, tz) for e in await wb.recent_entries(session, user, today(), days, tz)]
        out = []
        pains: dict[str, dict[str, Any]] = {}
        for e in entries:
            item: dict[str, Any] = {"id": e.id, "date": e.date, "time": e.notedAt.astimezone(tz).strftime("%H:%M")}
            for key, value in (("sleep_h", e.sleepHours), ("sleep_quality", e.sleepQuality),
                               ("energy", e.energy), ("mood", e.mood), ("note", e.note)):
                if value is not None:
                    item[key] = num(value) if isinstance(value, float) else value
            if e.pains:
                item["pains"] = [p.place + (f" {p.severity}/5" if p.severity else "") for p in e.pains]
            for p in e.pains:
                key = " ".join(p.place.casefold().split())
                agg = pains.setdefault(key, {"place": p.place, "mentions": 0, "last": e.date})
                agg["mentions"] += 1
            out.append(item)

        def avg(values: list[float | int | None]) -> float | int | None:
            present = [float(v) for v in values if v is not None]
            return num(sum(present) / len(present)) if present else None

        sleep = [e.sleepHours for e in entries if e.sleepHours is not None]
        summary = {
            "entries": len(entries),
            "avg_sleep_h": avg(list(sleep)),
            "nights_under_7h": sum(1 for h in sleep if h < wb.SHORT_SLEEP),
            "avg_sleep_quality": avg([e.sleepQuality for e in entries]),
            "avg_energy": avg([e.energy for e in entries]),
            "avg_mood": avg([e.mood for e in entries]),
            "pains": list(pains.values()),
        }
        return dumps({"days": days, "summary": summary, "entries": out})

    @tool(READ)
    async def program_status() -> str:
        """Текущая программа: название, неделя и день недели, план на сегодня по программе и, если уже построен,
        скорректированный план дня (под сон, боли, питание). Ничего не строит и не вызывает модель:
        `fresh=false` значит, что входные данные изменились и план стоит перестроить (regenerate_plan)."""
        now = day_plan.utcnow()
        local_today = now.astimezone(tz).date()
        async with owner_session() as (session, user):
            up = await session.scalar(
                select(UserProgram).where(UserProgram.user_id == user.id).order_by(UserProgram.id.desc()).limit(1)
            )
            if up is None:
                return dumps({"program": None, "note": "Программа не выбрана (выбирается в дневнике)."})
            program = await load_program(session, up.program_id)
            weeks = len(program.weeks)
            pos = program_position(up.started_on, weeks, local_today)
            out: dict[str, Any] = {
                "program": program.name,
                "slug": program.slug,
                "started_on": up.started_on,
                "weeks": weeks,
                "today": local_today,
            }
            if pos.not_started:
                out["status"] = "not_started"
                return dumps(out)
            if pos.finished:
                out["status"] = "finished"
                return dumps(out)
            out |= {"status": "active", "week": pos.week, "weekday": pos.weekday}
            day = find_day(program, pos.week, pos.weekday)
            if day is None:
                out["planned"] = "день отдыха"
                return dumps(out)
            names = day_plan.day_names(settings.programs_dir, program.slug, pos.week, pos.weekday)
            out["planned"] = [
                f"{names.get(i.order, i.exercise.name)} {format_item(i)}"
                for i in sorted(day.items, key=lambda i: i.order)
            ]
            inputs = await day_plan.collect_inputs(session, user, settings, tz, now)
            if inputs is None:
                return dumps(out)
            draft = day_plan.rule_draft(inputs, now)
            out["rules"] = {"readiness": draft.readiness, "summary": draft.summary}
            row = await day_plan._stored(session, user.id, inputs.today)
            if row is None:
                out["adjusted_plan"] = None
                return dumps(out)
            built = day_plan.Built(day_plan.plan_out(row, inputs), inputs.items)
            out["adjusted_plan"] = {
                "readiness": built.out.readiness,
                "adjusted": built.out.adjusted,
                "text": day_plan.plan_text(built),
                "built_at": _local(row.created_at, tz).strftime("%Y-%m-%d %H:%M"),
                "fresh": row.inputs_hash == day_plan.inputs_hash(inputs, draft),
            }
        return dumps(out)

    @tool(READ)
    async def profile_and_facts() -> str:
        """Профиль владельца (вес, рост, год рождения, цель, заметки), дневная норма КБЖУ, отдых между подходами,
        активные факты о нём (с id для deactivate_fact) и напоминания (с id; weekday 0=пн..6=вс, null = ежедневно)."""
        async with owner_session() as (session, user):
            facts = await fx.active_facts(session, user.id)
            reminders = (
                await session.scalars(
                    select(Reminder).where(Reminder.user_id == user.id).order_by(Reminder.minute_of_day, Reminder.id)
                )
            ).all()
            return dumps(
                {
                    "name": user.name,
                    "profile": prof.user_profile(user).model_dump(exclude_none=True),
                    "targets": nut.user_targets(user).model_dump(),
                    "rest_seconds": user.rest_seconds,
                    "facts": [{"id": f.id, "text": f.text, "category": f.category} for f in facts],
                    "reminders": [
                        {"id": r.id, "time": rem.minute_to_hhmm(r.minute_of_day), "kind": r.kind, "text": r.text,
                         "weekday": r.weekday, "enabled": r.enabled}
                        for r in reminders
                    ],
                }
            )

    @tool(READ)
    async def query(sql: str, limit: Annotated[int, Field(ge=1, le=rsql.MAX_LIMIT)] = 200) -> str:
        """Произвольный SELECT к базе дневника, только чтение (отдельное read-only подключение).
        Разрешён один оператор SELECT или WITH ... SELECT, без `;`, PRAGMA и ATTACH; не больше `limit` строк.
        Таблицы и колонки — в инструменте schema. Время в базе в UTC, вес в кг; данные одного владельца."""
        try:
            rows = await rsql.run_select(engine, sql, limit)
        except rsql.QueryError as e:
            raise ToolError(f"Запрос отклонён: {e}") from e
        return dumps({"columns": rows.columns, "rows": rows.rows, "truncated": rows.truncated})

    @tool(READ)
    async def schema() -> str:
        """Схема базы: CREATE TABLE и CREATE INDEX всех таблиц дневника в диалекте текущей базы (для query)."""
        return rsql.schema_ddl(engine)

    # ---- write ----

    @tool(WRITE)
    async def set_targets(
        kcal: Annotated[int | None, Field(ge=0, le=10000)] = None,
        protein: Annotated[int | None, Field(ge=0, le=1000)] = None,
        fat: Annotated[int | None, Field(ge=0, le=1000)] = None,
        carbs: Annotated[int | None, Field(ge=0, le=1000)] = None,
    ) -> str:
        """Изменить дневную норму: ккал и белки/жиры/углеводы в граммах. null (не передан) = не менять.
        Возвращает норму после изменения."""
        changes = {k: v for k, v in {"kcal": kcal, "protein": protein, "fat": fat, "carbs": carbs}.items()
                   if v is not None}
        if not changes:
            raise ToolError("Ничего не задано: передай хотя бы одно из kcal, protein, fat, carbs.")
        async with owner_session() as (session, user):
            nut.set_targets(user, changes)
            await session.commit()
            return dumps({"targets": nut.user_targets(user).model_dump()})

    async def save_fact(text: str, category: str | None, source: str) -> str:
        try:
            cleaned = fx.checked_text(text)
        except ValueError as e:
            raise ToolError(f"Факт не сохранён: {e}") from e
        async with owner_session() as (session, user):
            added = await fx.add_fact(session, user.id, cleaned, category, source_text=source)
            if added.status == "limit":
                raise ToolError(f"Активных фактов уже {fx.MAX_ACTIVE}: сначала отключи ненужные (deactivate_fact).")
            await session.commit()
            assert added.fact is not None
            f = added.fact
            return dumps({"status": added.status, "fact": {"id": f.id, "text": f.text, "category": f.category}})

    @tool(WRITE)
    async def add_fact(
        text: Annotated[str, Field(max_length=2000)],
        category: fx.Category | None = None,
    ) -> str:
        """Запомнить долгосрочный факт о владельце (предпочтения, аллергии, порции, режим, ограничения), до 200 символов.
        category: food | training | health | schedule | other; не передана — угадывается по тексту.
        Такой же активный факт не дублируется (status=duplicate)."""
        return await save_fact(text, category, "[mcp] add_fact")

    @tool(REMOVE)
    async def deactivate_fact(id: int) -> str:
        """Отключить факт по id (из profile_and_facts): он перестанет учитываться, но не удалится."""
        async with owner_session() as (session, user):
            f = await session.get(UserFact, id)
            if f is None or f.user_id != user.id:
                raise ToolError(f"Факта с id {id} нет.")
            was_active = f.active
            f.active = False
            await session.commit()
            return dumps({"id": f.id, "text": f.text, "active": False, "was_active": was_active})

    @tool(WRITE)
    async def set_reminder(
        time: Annotated[str, Field(pattern=rem.TIME_PATTERN, description="ЧЧ:ММ, местное время")],
        kind: Literal["text", "nutrition", "advice", "checkin"],
        text: Annotated[str | None, Field(max_length=rem.TEXT_MAX)] = None,
        weekday: Annotated[int | None, Field(ge=0, le=6)] = None,
        enabled: bool = True,
        id: int | None = None,
    ) -> str:
        """Создать напоминание или, если передан id, заменить существующее. Бот пришлёт его в Telegram в `time`
        (ЧЧ:ММ, местное время), каждый день или только в `weekday` (0=пн..6=вс). kind: text — свой текст
        (`text` обязателен), nutrition — остаток КБЖУ на день, advice — совет модели, checkin — вопрос о самочувствии.
        Время, уже прошедшее сегодня, сработает с завтрашнего дня."""
        try:
            body = rem.reminder_text(kind, text)
        except ValueError as e:
            raise ToolError(f"Напоминание не сохранено: {e}") from e
        minute = rem.hhmm_to_minute(time)
        async with owner_session() as (session, user):
            if id is None:
                count = await session.scalar(
                    select(func.count()).select_from(Reminder).where(Reminder.user_id == user.id)
                )
                if (count or 0) >= rem.MAX_PER_USER:
                    raise ToolError(f"Напоминаний уже {rem.MAX_PER_USER}: удали ненужные (delete_reminder).")
                r = Reminder(user_id=user.id)
                session.add(r)
            else:
                found = await session.get(Reminder, id)
                if found is None or found.user_id != user.id:
                    raise ToolError(f"Напоминания с id {id} нет.")
                r = found
            r.minute_of_day, r.kind, r.text, r.weekday, r.enabled = minute, kind, body, weekday, enabled
            # Same rule as the API: a time already passed today waits for tomorrow.
            r.last_sent_on = rem.initial_last_sent(minute, datetime.now(UTC), tz)
            await session.commit()
            return dumps({"id": r.id, "time": rem.minute_to_hhmm(minute), "kind": kind, "text": body,
                          "weekday": weekday, "enabled": enabled})

    @tool(REMOVE)
    async def delete_reminder(id: int) -> str:
        """Удалить напоминание по id (из profile_and_facts)."""
        async with owner_session() as (session, user):
            r = await session.get(Reminder, id)
            if r is None or r.user_id != user.id:
                raise ToolError(f"Напоминания с id {id} нет.")
            await session.delete(r)
            await session.commit()
        return dumps({"deleted": id})

    @tool(OUTSIDE)
    async def regenerate_plan() -> str:
        """Заново построить план на сегодня под сон, боли, питание и восстановление (вызывает языковую модель,
        может занять до минуты) и сохранить его — его увидят мини-апп и /plan. Ошибка, если сегодня не тренировочный день."""
        now = day_plan.utcnow()
        async with owner_session() as (session, user):
            await active_program(session, user, now.astimezone(tz).date())  # first use starts a program, as in the API
            await session.commit()  # no write lock while the model thinks
            built = await day_plan.get_or_build(session, user, settings, get_llm(), tz, now, force=True)
        if built is None:
            raise ToolError("Сегодня не тренировочный день по программе: плана нет.")
        return dumps({"readiness": built.out.readiness, "adjusted": built.out.adjusted,
                      "text": day_plan.plan_text(built)})

    @tool(WRITE)
    async def log_note(text: Annotated[str, Field(max_length=2000)]) -> str:
        """Сохранить короткую заметку (до 194 символов) как факт категории other с префиксом «[mcp]»:
        она попадёт в контекст парсера и советов. Для важного и долгосрочного лучше add_fact с категорией."""
        if not fx.clean(text):
            raise ToolError("Пустая заметка.")
        return await save_fact(NOTE_PREFIX + fx.clean(text), "other", "[mcp] log_note")

    # ---- delivery and service ----

    @tool(OUTSIDE)
    async def send_message(
        text: Annotated[str, Field(max_length=MESSAGE_MAX)],
        html: Annotated[bool, Field(description="Текст в HTML-разметке Telegram: <b>, <i>, <u>, <s>, <code>, <a href>; символы < > & в обычном тексте экранируй как &lt; &gt; &amp;")] = False,
    ) -> str:
        """Отправить владельцу сообщение в Telegram от имени бота, до 4000 символов. html=true включает
        HTML-разметку Telegram (жирные заголовки, курсив); если Telegram её отклонит, сообщение уйдёт как простой текст."""
        if not text.strip():
            raise ToolError("Пустое сообщение.")
        if bot is None:
            raise ToolError("Бот не запущен в этом процессе (RUN_BOT=false): отправить сообщение нельзя.")
        async with sessionmaker() as session:
            user = await owner_user(session, settings)
        chat_id = user.telegram_id if user else (settings.allowed_user_ids[0] if settings.allowed_user_ids else None)
        if chat_id is None:
            raise ToolError("Владелец ещё не писал боту: некому отправить.")
        parse_mode = "HTML" if html else None
        try:
            sent = await bot.send_message(chat_id, text, parse_mode=parse_mode)
        except TelegramAPIError as e:
            if parse_mode is None:
                raise ToolError(f"Telegram не принял сообщение: {e}") from e
            # Broken markup: deliver the content anyway rather than lose the report.
            try:
                sent = await bot.send_message(chat_id, text, parse_mode=None)
            except TelegramAPIError as e2:
                raise ToolError(f"Telegram не принял сообщение: {e2}") from e2
            return dumps({"sent": True, "message_id": sent.message_id, "html": False, "note": "разметка отклонена, отправлено как текст"})
        return dumps({"sent": True, "message_id": sent.message_id, "html": html})

    @tool(READ)
    async def service_status() -> str:
        """Состояние сервиса: версия и коммит, режим бота, аптайм процесса, число маршрутов языковой модели,
        время сервера в его часовом поясе, база данных и её размер."""
        if "commit" not in version_cache:
            version_cache["commit"] = await _git_commit()
        try:
            size = await rsql.database_size(engine)
        except Exception:  # status must answer even if the size is unavailable
            log.exception("database size")
            size = None
        uptime = time.monotonic() - _STARTED
        return dumps(
            {
                "version": _package_version(),
                "commit": version_cache["commit"],
                "bot": settings.bot_mode if settings.run_bot and bot is not None else "off",
                "uptime": _uptime(uptime),
                "uptime_s": int(uptime),
                "llm_routes": len(routes_from(settings)),
                "server_time": datetime.now(tz).isoformat(timespec="seconds"),
                "timezone": settings.timezone,
                "db": {"dialect": engine.url.get_backend_name(),
                       "size_mb": round(size / 1_048_576, 2) if size is not None else None},
            }
        )

    return mcp


def mcp_http(
    settings: Settings,
    sessionmaker: Sessionmaker,
    get_llm: Callable[[], OpenRouterClient],
    bot: Bot | None = None,
) -> tuple[list[BaseRoute], Callable[[Any], AbstractAsyncContextManager[None]]]:
    """Routes for /mcp and the lifespan that runs the MCP session manager (enter it once per app)."""
    mcp = build_mcp(settings, sessionmaker, get_llm, bot)
    sub = mcp.streamable_http_app(
        streamable_http_path=MCP_PATH,
        json_response=True,
        stateless_http=True,
        transport_security=TransportSecuritySettings(
            allowed_hosts=allowed_hosts(settings.public_url), allowed_origins=[]
        ),
    )

    @asynccontextmanager
    async def lifespan(_app: Any) -> AsyncIterator[None]:
        # A mounted sub-app's own lifespan never runs, so the host app enters the session manager.
        async with mcp.session_manager.run():
            yield

    return list(sub.routes), lifespan
