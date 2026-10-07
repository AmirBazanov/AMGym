"""/facts: what the bot remembers about the user, with a delete button per fact.

Facts are added in the chat ("запомни: ...", or the "🧠 Запомнить" button, see handlers/log_text.py)
or in the Mini App; this list shows active ones, newest first.
"""

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from gymbot.db.models import UserFact
from gymbot.db.session import Sessionmaker
from gymbot.services import facts, live
from gymbot.services.users import get_or_create_user

router = Router(name="facts")

EMPTY = "Пока ничего не помню о тебе. Напиши «запомни: …», например «запомни: не ем творог»."
LINE_MAX = 70  # 50 facts × 200 characters would not fit one Telegram message (4096)
BUTTON_MAX = 30


def _short(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


async def _render(session: AsyncSession, telegram_id: int, name: str) -> tuple[str, InlineKeyboardMarkup | None]:
    user = await get_or_create_user(session, telegram_id, name)
    items = await facts.active_facts(session, user.id)
    if not items:
        return EMPTY, None
    lines = ["Что я помню о тебе (учитываю в разборе и советах):"]
    lines += [f"{i}. {_short(f.text, LINE_MAX)}" for i, f in enumerate(items, 1)]
    lines.append("\nНажми на факт ниже, чтобы забыть его. Добавить: «запомни: …».")
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=_short(f"🗑 {i}. {f.text}", BUTTON_MAX), callback_data=f"fact_del:{f.id}")]
            for i, f in enumerate(items, 1)
        ]
    )
    return "\n".join(lines), kb


@router.message(Command("facts"))
async def list_facts(message: Message, sessionmaker: Sessionmaker) -> None:
    async with sessionmaker() as session:
        text, kb = await _render(session, message.from_user.id, message.from_user.full_name)  # type: ignore[union-attr]
        await session.commit()
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("fact_del:"))
async def delete_fact(cb: CallbackQuery, sessionmaker: Sessionmaker) -> None:
    raw_id = (cb.data or "").split(":", 1)[1]
    async with sessionmaker() as session:
        user = await get_or_create_user(session, cb.from_user.id, cb.from_user.full_name)
        fact = await session.get(UserFact, int(raw_id)) if raw_id.isdigit() else None
        deleted = fact is not None and fact.user_id == user.id
        if deleted:
            await session.delete(fact)
            await session.flush()
        text, kb = await _render(session, cb.from_user.id, cb.from_user.full_name)
        await session.commit()
    if not deleted:
        await cb.answer("Этого факта уже нет.", show_alert=True)
        return
    live.publish(user.id, "facts", "state")  # its working weights are gone too
    if cb.message:
        await cb.message.edit_text(text, reply_markup=kb)  # type: ignore[union-attr]
    await cb.answer("Забыл.")
