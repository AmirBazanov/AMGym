"""/plan: today's program day adjusted to wellbeing, food and recovery (gymbot.services.plan).

The same text follows a saved wellbeing record on a training day when the plan changed (log_text.save).
"""

import logging
from zoneinfo import ZoneInfo

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo

from gymbot.config import Settings
from gymbot.db.session import Sessionmaker
from gymbot.llm.openrouter import OpenRouterClient
from gymbot.services import plan
from gymbot.services.users import active_program, get_or_create_user

log = logging.getLogger(__name__)
router = Router(name="plan")

NO_TRAINING = "Сегодня по программе тренировки нет. Расписание недели — в дневнике."


def open_diary_kb(settings: Settings) -> InlineKeyboardMarkup | None:
    if not settings.miniapp_url:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Открыть дневник", web_app=WebAppInfo(url=settings.miniapp_url))]]
    )


async def build_for(
    telegram_id: int,
    name: str | None,
    settings: Settings,
    sessionmaker: Sessionmaker,
    llm: OpenRouterClient | None,
    *,
    force: bool = False,
    start_program: bool = True,
) -> plan.Built | None:
    """Today's plan for a Telegram user; `start_program` starts the default program like /today does."""
    tz = ZoneInfo(settings.timezone)
    now = plan.utcnow()
    async with sessionmaker() as session:
        user = await get_or_create_user(session, telegram_id, name)
        if start_program:
            await active_program(session, user, now.astimezone(tz).date())
        await session.commit()  # no write lock while the model thinks
        return await plan.get_or_build(session, user, settings, llm, tz, now, force=force)


@router.message(Command("plan"))
async def show_plan(
    message: Message, settings: Settings, sessionmaker: Sessionmaker, llm: OpenRouterClient | None = None
) -> None:
    built = await build_for(message.from_user.id, message.from_user.full_name, settings, sessionmaker, llm)  # type: ignore[union-attr]
    if built is None:
        await message.answer(NO_TRAINING)
        return
    await message.answer(plan.plan_text(built), reply_markup=open_diary_kb(settings))


async def send_after_wellbeing(
    answer_to: Message, telegram_id: int, settings: Settings, sessionmaker: Sessionmaker, llm: OpenRouterClient | None
) -> None:
    """Rebuild today's plan after a wellbeing record and send it if it differs from the program.

    Never raises: the record is already saved, a failing plan must not look like a failed save.
    """
    try:
        # Not forced: the new record changes the inputs hash anyway, an unchanged state reuses the plan.
        built = await build_for(telegram_id, None, settings, sessionmaker, llm, start_program=False)
        if built is not None and built.out.adjusted:
            await answer_to.answer(plan.plan_text(built), reply_markup=open_diary_kb(settings))
    except Exception:
        log.exception("plan after wellbeing failed")
