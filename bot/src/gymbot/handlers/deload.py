"""/deload and the deload offer's buttons (gymbot.services.deload).

The offer («Сделать разгрузочную неделю?») comes after a saved workout or /plan with [✅ Да, неделю / Позже /
Нет]. /deload shows the status: a running deload can be cancelled; otherwise one can be started after a
confirmation. Starting or cancelling changes today's plan: "plan" is published for the Mini App.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from gymbot.config import Settings
from gymbot.db.session import Sessionmaker
from gymbot.services import deload, live, workout_events
from gymbot.services.users import get_or_create_user

log = logging.getLogger(__name__)
router = Router(name="deload")

STARTED = "Разгрузочная неделя до {until} ✅ Веса −15 %, подходов на треть меньше. План на сегодня: /plan"
LATER_TEXT = "Хорошо, спрошу через 3 дня."
NO_TEXT = "Хорошо, не буду предлагать 2 недели. Начать самому: /deload"
CANCELLED = "Разгрузка отменена, план снова как в программе."
NOT_RUNNING = "Разгрузка сейчас не идёт."


def utcnow() -> datetime:
    """Current time; tests pin it here."""
    return datetime.now(UTC)


def _day(until: date) -> str:
    return f"{deload.WEEKDAYS[until.weekday()]} {until:%d.%m}"


def status_keyboard(running: bool) -> InlineKeyboardMarkup:
    if running:
        button = InlineKeyboardButton(text="Отменить разгрузку", callback_data="deload:cancel")
    else:
        button = InlineKeyboardButton(text="Начать разгрузку", callback_data="deload:ask")
    return InlineKeyboardMarkup(inline_keyboard=[[button]])


def confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="✅ Да, неделю", callback_data="deload:yes"),
            InlineKeyboardButton(text="Отмена", callback_data="deload:keep"),
        ]]
    )


@router.message(Command("deload"))
async def show_status(message: Message, settings: Settings, sessionmaker: Sessionmaker) -> None:
    tz = ZoneInfo(settings.timezone)
    now = utcnow()
    today = now.astimezone(tz).date()
    async with sessionmaker() as session:
        user = await get_or_create_user(session, message.from_user.id, message.from_user.full_name)  # type: ignore[union-attr]
        await session.commit()
        st = await deload.get_state(session, user.id)
        verdict = await deload.evaluate(session, user, today, tz, settings.programs_dir)
    running = deload.active(st, today)
    await message.answer(deload.status_text(st, verdict, today), reply_markup=status_keyboard(running))


@router.callback_query(F.data.startswith("deload:"))
async def on_button(cb: CallbackQuery, settings: Settings, sessionmaker: Sessionmaker) -> None:
    action = (cb.data or "").split(":", 1)[1]
    tz = ZoneInfo(settings.timezone)
    now = utcnow()
    today = now.astimezone(tz).date()
    if action == "ask":
        until = today + timedelta(days=deload.DELOAD_DAYS - 1)
        if cb.message:
            await cb.message.edit_text(  # type: ignore[union-attr]
                f"Начать разгрузочную неделю до {_day(until)}? Веса −15 %, подходов на треть меньше.",
                reply_markup=confirm_keyboard(),
            )
        await cb.answer()
        return
    if action == "keep":
        if cb.message:
            await cb.message.edit_text("Хорошо, без разгрузки.")  # type: ignore[union-attr]
        await cb.answer()
        return
    publish = False
    async with sessionmaker() as session:
        user = await get_or_create_user(session, cb.from_user.id, cb.from_user.full_name)
        if action == "yes":
            st = await deload.get_state(session, user.id)
            if deload.active(st, today):
                assert st is not None and st.until is not None
                text = f"Разгрузочная неделя уже идёт до {_day(st.until)}."
            else:
                until = await deload.start(session, user.id, today, now)
                text = STARTED.format(until=_day(until))
                publish = True
        elif action in ("later", "no"):
            await deload.postpone(session, user.id, now, refuse=action == "no")
            text = LATER_TEXT if action == "later" else NO_TEXT
        elif action == "cancel":
            publish = await deload.cancel(session, user.id, today, now)
            text = CANCELLED if publish else NOT_RUNNING
        else:
            await cb.answer()
            return
        await session.commit()
    if publish:
        live.publish(user.id, "plan")
    if cb.message:
        await cb.message.edit_text(text)  # type: ignore[union-attr]
    await cb.answer()


async def offer_after_plan(message: Message, settings: Settings, sessionmaker: Sessionmaker) -> None:
    """/plan computes the deload too: the offer follows the plan when it is suggested and due. Never raises."""
    try:
        async with sessionmaker() as session:
            user = await get_or_create_user(session, message.from_user.id, message.from_user.full_name)  # type: ignore[union-attr]
            await session.commit()

        async def send(text: str, kb: InlineKeyboardMarkup | None) -> object:
            return await message.answer(text, reply_markup=kb)

        await workout_events.offer_deload(
            sessionmaker, user.id, send, ZoneInfo(settings.timezone), utcnow(), settings.programs_dir
        )
    except Exception:
        log.exception("deload offer after /plan failed")
