"""/llm: the LLM routes for the owner, since the server has no shell for us.

`/llm` shows the routes in the order they are tried with their state (ok / rate-limited / out of credits /
key rejected), the route that answered last, today's usage per route (local TIMEZONE day) and Claude's cost this
month. `/llm test` makes one tiny call to the first route and reports its latency, tokens and cost, or the
error class. Counters live in memory (gymbot.llm.stats) and reset on restart. Never shows keys or content.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from gymbot.config import Settings
from gymbot.db.session import Sessionmaker
from gymbot.handlers.log_text import keep_typing
from gymbot.llm.openrouter import (
    AUTH_FAILED,
    NO_CREDITS,
    OK,
    OVERLOADED,
    OpenRouterClient,
    Probe,
    Route,
    RouteStatus,
)
from gymbot.services import backup

router = Router(name="llm_status")

OWNER_ONLY = "Состояние нейросетей видит только владелец."
NO_ROUTES = "Нейросети не настроены: нет ни одного ключа (ANTHROPIC_API_KEY, GROQ_API_KEY, OPENROUTER_API_KEY)."
MONTHS = ("январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь", "ноябрь",
          "декабрь")


def _n(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def _usd(value: float) -> str:
    return f"${value:.4f}" if value < 1 else f"${value:.2f}"


def _state(st: RouteStatus, tz: ZoneInfo) -> str:
    until = f" до {st.until.astimezone(tz):%H:%M}" if st.until else ""
    if st.state == OK:
        return "ок"
    if st.state == NO_CREDITS:
        return f"нет кредитов{until}"
    if st.state == AUTH_FAILED:
        return "ключ не принят, выключен до перезапуска"
    if st.state == OVERLOADED:
        return f"перегружен, пауза{until}"
    return f"лимит запросов, пауза{until}"


def _routes(llm: OpenRouterClient, routes: list[Route], tz: ZoneInfo) -> list[str]:
    return [f"{i}. {r.name}: {_state(llm.status(r), tz)}" for i, r in enumerate(routes, 1)]


def status_text(llm: OpenRouterClient, settings: Settings) -> str:
    tz = ZoneInfo(settings.timezone)
    if not llm.routes and not llm.vision_routes:
        return NO_ROUTES
    lines = ["Нейросети по порядку (текст):", *_routes(llm, llm.routes, tz)]
    if llm.vision_routes:
        lines += ["Фото:", *_routes(llm, llm.vision_routes, tz)]
    if settings.anthropic_api_key and not settings.anthropic_enabled:
        lines.append("Claude выключен (ANTHROPIC_ENABLED=false).")
    stats = llm.stats
    last = stats.last
    lines.append(
        f"Последним ответил {last.route} в {last.at.astimezone(tz):%H:%M}." if last else "Ответов ещё не было."
    )
    day, today, month_cost = stats.snapshot()
    lines.append(f"Сегодня ({day:%d.%m}):")
    if not today:
        lines.append("вызовов не было")
    for name, c in today.items():
        cache = f", из кэша {_n(c.cache_read)}, в кэш {_n(c.cache_write)}" if c.cache_read or c.cache_write else ""
        fails = f", ошибок {c.failures}" if c.failures else ""
        lines.append(
            f"{name}: вызовов {c.calls}{fails}, вход {_n(c.input)}{cache}, выход {_n(c.output)}, {_usd(c.cost)}"
        )
    lines.append(f"Claude за {MONTHS[day.month - 1]}: {_usd(month_cost.get('anthropic', 0.0))}.")
    lines.append(f"Счётчики в памяти с {stats.started.astimezone(tz):%d.%m %H:%M}, обнуляются при перезапуске.")
    return "\n".join(lines)


def probe_text(p: Probe) -> str:
    if p.route is None:
        return NO_ROUTES
    took = f"{p.seconds:.1f}".replace(".", ",")
    if p.error is not None:
        return f"Проверка {p.route}: ошибка {p.error} за {took} с."
    text = f"Проверка {p.route}: ответ «{p.answer}» за {took} с"
    if u := p.usage:
        text += f", вход {_n(u.prompt)}, выход {_n(u.output)}, {_usd(u.cost)}"
    return text + "."


@router.message(Command("llm"))
async def llm_status(
    message: Message,
    command: CommandObject,
    settings: Settings,
    sessionmaker: Sessionmaker,
    llm: OpenRouterClient,
) -> None:
    # The middleware lets in every ALLOWED_USER_IDS id; costs and route health are the owner's business.
    async with sessionmaker() as session:
        owner = await backup.owner_chat_id(session, settings)
    if message.from_user is None or message.from_user.id != owner:
        await message.answer(OWNER_ONLY)
        return
    if (command.args or "").strip().lower() == "test":
        async with keep_typing(message):
            probe = await llm.probe()
        await message.answer(probe_text(probe))
        return
    await message.answer(status_text(llm, settings))
