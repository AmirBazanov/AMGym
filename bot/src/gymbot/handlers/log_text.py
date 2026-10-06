"""Free-text logging: message -> LLM -> preview -> user confirms -> saved.

Saving is behind a confirm button on purpose: free models make mistakes, and a wrong
set silently written to the log ruins progress charts.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from gymbot.config import Settings
from gymbot.db.models import FoodEntry
from gymbot.db.session import Sessionmaker
from gymbot.llm.openrouter import LLMError, OpenRouterClient
from gymbot.llm.schemas import ParseResult
from gymbot.services.programs import exercise_catalog
from gymbot.services.users import get_or_create_user
from gymbot.services.workouts import save_from_chat

router = Router(name="log_text")


@dataclass
class Pending:
    user_id: int
    result: ParseResult
    raw_text: str


# Parsed messages waiting for "Сохранить". In memory: a restart just means pressing again after resending.
PENDING: dict[str, Pending] = {}
MAX_PENDING = 200


def render_preview(result: ParseResult) -> str:
    if result.kind == "workout" and result.exercises:
        lines = []
        for ex in result.exercises:
            sets = ", ".join(
                (f"{s.weight_kg:g} кг × {s.reps}" if s.weight_kg is not None else f"{s.reps} повт.")
                + (" (дроп)" if s.drop_index else "")
                for s in ex.sets
            )
            lines.append(f"• {ex.exercise}: {sets}")
        return "Записать?\n" + "\n".join(lines)
    if result.kind == "food" and result.foods:
        lines = [
            f"• {f.description}{f' {f.grams:g} г' if f.grams else ''}: {f.kcal:.0f} ккал, "
            f"Б{f.protein_g:.0f} Ж{f.fat_g:.0f} У{f.carbs_g:.0f}"
            for f in result.foods
        ]
        total = sum(f.kcal for f in result.foods)
        return f"Записать еду? Всего {total:.0f} ккал\n" + "\n".join(lines)
    return result.clarification or "Не понял. Напиши, например: «присед 4х8 по 80»."


def confirm_kb(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Сохранить", callback_data=f"save:{token}"),
                InlineKeyboardButton(text="✖ Отмена", callback_data=f"drop:{token}"),
            ]
        ]
    )


@router.message(F.text & ~F.text.startswith("/"))
async def log_free_text(message: Message, settings: Settings, sessionmaker: Sessionmaker) -> None:
    text = message.text or ""
    async with sessionmaker() as session:
        catalog = await exercise_catalog(session)
    await message.bot.send_chat_action(message.chat.id, "typing")  # type: ignore[union-attr]
    try:
        result = await OpenRouterClient(settings).parse_message(text, catalog)
    except LLMError:
        await message.answer("Нейросеть сейчас недоступна, попробуй ещё раз чуть позже.")
        return
    preview = render_preview(result)
    savable = (result.kind == "workout" and result.exercises) or (result.kind == "food" and result.foods)
    if not savable:
        await message.answer(preview)
        return
    if len(PENDING) >= MAX_PENDING:
        PENDING.pop(next(iter(PENDING)))
    token = secrets.token_hex(6)
    PENDING[token] = Pending(message.from_user.id, result, text)  # type: ignore[union-attr]
    await message.answer(preview, reply_markup=confirm_kb(token))


@router.callback_query(F.data.startswith("drop:"))
async def drop(cb: CallbackQuery) -> None:
    PENDING.pop((cb.data or "").split(":", 1)[1], None)
    if cb.message:
        await cb.message.edit_text("Отменено.")  # type: ignore[union-attr]
    await cb.answer()


@router.callback_query(F.data.startswith("save:"))
async def save(cb: CallbackQuery, settings: Settings, sessionmaker: Sessionmaker) -> None:
    token = (cb.data or "").split(":", 1)[1]
    pending = PENDING.get(token)
    if pending is None or pending.user_id != cb.from_user.id:
        await cb.answer("Эта запись устарела, отправь сообщение ещё раз.", show_alert=True)
        return
    today = datetime.now(ZoneInfo(settings.timezone)).date()
    async with sessionmaker() as session:
        user = await get_or_create_user(session, cb.from_user.id, cb.from_user.full_name)
        if pending.result.kind == "workout":
            await save_from_chat(session, user, pending.result, pending.raw_text, today)
            note = "Сохранено ✅ Видно в дневнике, /undo — отменить."
        else:
            for f in pending.result.foods:
                session.add(
                    FoodEntry(
                        user_id=user.id,
                        description=f.description,
                        grams=Decimal(str(f.grams)) if f.grams else None,
                        kcal=Decimal(str(f.kcal)),
                        protein_g=Decimal(str(f.protein_g)),
                        fat_g=Decimal(str(f.fat_g)),
                        carbs_g=Decimal(str(f.carbs_g)),
                    )
                )
            note = "Еда сохранена ✅"
        await session.commit()
    PENDING.pop(token, None)
    if cb.message:
        await cb.message.edit_text(f"{render_preview(pending.result).removeprefix('Записать?')}\n\n{note}".strip())  # type: ignore[union-attr]
    await cb.answer()
