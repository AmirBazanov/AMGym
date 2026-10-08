"""Edit or delete saved records from the chat, always behind a confirm button.

"удали самсу" -> "Удалить? • самса, 3 шт — 840 ккал (08.10 13:20)" [🗑 Удалить / ✖ Отмена];
"самса была 2, а не 3" -> "Исправить? самса, 3 шт — 840 ккал → самса, 2 шт — 560 ккал" [✅ Исправить / ✖ Отмена].
Several candidates -> up to MAX_CHOICES buttons; a tap shows that one's preview in place.

process_text (handlers/log_text.py) calls `handle` before the parser, only while no preview or model question
is open (an open preview is revised by the dialog flow there, unchanged). Matching, the new values and the
writes are in gymbot.services.saved_edits; this module keeps the offers in memory (by token, TTL) and owns
the buttons. The router is attached to log_text.router like body_weight's, so main.py needs no change.
"""

from __future__ import annotations

import contextlib
import logging
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

from gymbot.config import Settings
from gymbot.db.models import User
from gymbot.db.session import Sessionmaker
from gymbot.handlers import products as product_cards
from gymbot.llm.openrouter import LLMError, OpenRouterClient
from gymbot.llm.schemas import ParseResult
from gymbot.services import facts, live, plausibility, workout_events
from gymbot.services import saved_edits as se
from gymbot.services.programs import exercise_catalog
from gymbot.services.users import get_or_create_user

log = logging.getLogger(__name__)

router = Router(name="saved_edits")

TTL = timedelta(minutes=15)
MAX_PENDING = 200
LABEL_MAX = 60  # characters of a candidate button

STALE = "Это предложение устарело: запись уже изменена или удалена, или прошло больше 15 минут."
TOO_OLD = f"Записи старше {se.MAX_DAYS} дней из чата не меняю — это можно в дневнике."
UNAVAILABLE = "Нейросеть сейчас недоступна, попробуй ещё раз чуть позже."
NOT_UNDERSTOOD = "Не понял, как исправить. Напиши, например, «самса была 2, а не 3»."
SAME = "Так и записано, менять нечего."
WHAT = (
    "Не понял, что именно. Например: «удали последнюю еду», «убери самсу», «удали последний подход», "
    "«самса была 2, а не 3»."
)
NAME_EXERCISE = "Назови упражнение, например «в жиме было 85, а не 80»."
SOFT_HINT = "Если это новая запись, напиши, например, «съел бутерброд без сыра»."


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class Offer:
    """A delete or edit preview waiting for its button."""

    user_id: int  # Telegram id
    unit: se.Unit
    action: se.Action
    after: ParseResult | None  # new values of an edit
    raw: str  # the command as stored in the [edit] trail ("[voice] ..." for voice)
    at: datetime  # when it was offered (UTC), for TTL
    soft: bool = False


@dataclass
class Choice:
    """Candidates shown as buttons."""

    user_id: int
    units: list[se.Unit]
    intent: se.Intent
    text: str
    raw: str
    at: datetime


OFFERS: dict[str, Offer] = {}
CHOICES: dict[str, Choice] = {}
# What each chat saved last (log_text.save): "не 3, а 2" right after "Сохранить" fixes that record.
LAST_SAVED: dict[int, se.LastSaved] = {}
LAST_SAVED_WINDOW = timedelta(minutes=15)  # from the previewed message to the correction


def remember_saved(tg_id: int, kind: str, raw_text: str, sent_at: datetime) -> None:
    LAST_SAVED.pop(tg_id, None)
    if len(LAST_SAVED) >= MAX_PENDING:
        LAST_SAVED.pop(next(iter(LAST_SAVED)))
    LAST_SAVED[tg_id] = se.LastSaved(kind, raw_text, sent_at)


def _last_saved(tg_id: int, now: datetime) -> se.LastSaved | None:
    last = LAST_SAVED.get(tg_id)
    return last if last is not None and timedelta(0) <= now - last.sent_at <= LAST_SAVED_WINDOW else None

Send = Callable[[str, InlineKeyboardMarkup | None], Awaitable[object]]


def _token(store: dict) -> str:
    if len(store) >= MAX_PENDING:
        store.pop(next(iter(store)))
    return secrets.token_hex(6)


def confirm_kb(token: str, action: se.Action) -> InlineKeyboardMarkup:
    ok = "🗑 Удалить" if action == "delete" else "✅ Исправить"
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=ok, callback_data=f"fixok:{token}"),
        InlineKeyboardButton(text="✖ Отмена", callback_data=f"fixno:{token}"),
    ]])


def _short(text: str) -> str:
    return text if len(text) <= LABEL_MAX else text[: LABEL_MAX - 1] + "…"


def choice_kb(token: str, units: list[se.Unit], tz: ZoneInfo) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=_short(f"{i}. {se.label(u, tz)}"), callback_data=f"fixpick:{token}:{i - 1}")]
        for i, u in enumerate(units, 1)
    ]
    rows.append([InlineKeyboardButton(text="✖ Отмена", callback_data=f"fixno:{token}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _day_text(day, today) -> str:  # type: ignore[no-untyped-def]
    if day == today:
        return "за сегодня"
    if day == today - timedelta(days=1):
        return "за вчера"
    return f"за {day:%d.%m}"


def not_found(intent: se.Intent, found: se.Found, today) -> str:  # type: ignore[no-untyped-def]
    if intent.action == "edit" and intent.whole_workout and not intent.words:
        return NAME_EXERCISE
    if intent.words:
        what = "«" + " ".join(intent.words) + "»"
    elif intent.meal:
        what = intent.meal
    elif intent.kind == "food":
        what = "еду"
    elif intent.kind == "wellbeing":
        what = "самочувствие"
    elif intent.kind == "workout":
        what = "тренировку" if intent.whole_workout else "подходы"
    else:
        return WHAT
    where = _day_text(found.day, today) if intent.day is not None else (
        "за последние две недели" if intent.latest else "за сегодня и вчера"
    )
    if intent.pair is not None:
        what += f" со значением {intent.pair[1]:g}"
    return f"Не нашёл {what} {where}. Если это было в другой день, назови его: «вчерашний плов», «за 06.10»."


async def handle(
    message: Message, text: str, raw: str, prefix: str, settings: Settings, sessionmaker: Sessionmaker,
    llm: OpenRouterClient,
) -> bool:
    """Reply to an edit or delete command about saved records; False = not one, the caller goes on."""
    tz = ZoneInfo(settings.timezone)
    now = message.date
    today = now.astimezone(tz).date()
    intent = se.detect(text, today)
    if intent is None:
        return False
    tg_id = message.from_user.id  # type: ignore[union-attr]
    async with sessionmaker() as session:
        user_id = await session.scalar(select(User.id).where(User.telegram_id == tg_id))
        found = (
            await se.find(session, user_id, intent, now, tz, _last_saved(tg_id, now))
            if user_id is not None else se.Found([], day=intent.day or today, explain=se.explainable(intent))
        )
    if found.too_old:
        await message.answer(prefix + TOO_OLD)
        return True
    if not found.units:
        if not found.explain:  # a new record or not about the diary after all: the parser's
            return False
        await message.answer(prefix + not_found(intent, found, today))
        return True
    log.info("saved %s: %s candidate(s)", intent.action, len(found.units))
    if len(found.units) > 1:
        units = found.units[: se.MAX_CHOICES]
        token = _token(CHOICES)
        CHOICES[token] = Choice(tg_id, units, intent, text, raw, utcnow())
        head = "Что удалить?" if intent.action == "delete" else "Что исправить?"
        await message.answer(prefix + head, reply_markup=choice_kb(token, units, tz))
        return True

    async def send(reply: str, kb: InlineKeyboardMarkup | None) -> object:
        return await message.answer(prefix + reply, reply_markup=kb)

    async def typing() -> None:
        with contextlib.suppress(Exception):
            await message.bot.send_chat_action(message.chat.id, "typing")  # type: ignore[union-attr]

    await _offer(found.units[0], intent, text, raw, tg_id, settings, sessionmaker, llm, send, typing)
    return True


async def _reestimate(
    unit: se.Unit, text: str, tg_id: int, sessionmaker: Sessionmaker, llm: OpenRouterClient,
    typing: Callable[[], Awaitable[None]] | None = None,
) -> ParseResult | None:
    """The parser's revision of the saved record: it sees the record as the previous turn, like a preview."""
    async with sessionmaker() as session:
        catalog = await exercise_catalog(session)
        known = await facts.prompt_facts(session, tg_id)
    history = [se.history_turn(unit)]
    result = await llm.parse_message(text, catalog, history, known)
    if result.kind == "food" and unit.kind == "food":  # an implausible estimate: a repair round, then the reference
        products = await product_cards.parser_products(sessionmaker, tg_id, text)
        reparse = plausibility.parser_reparse(llm, text, result, history, known)

        async def repair(correction: str) -> ParseResult:
            if typing is not None:  # a second model call the user waits for
                await typing()
            return await reparse(correction)

        result = await plausibility.review(result, repair, exact=lambda f: product_cards.is_exact(f, products))
    return se.edited(unit, result)


async def _offer(
    unit: se.Unit, intent: se.Intent, text: str, raw: str, tg_id: int, settings: Settings,
    sessionmaker: Sessionmaker, llm: OpenRouterClient | None, send: Send, typing: Callable[[], Awaitable[None]],
) -> None:
    """The delete or edit preview of one unit with its confirm button."""
    tz = ZoneInfo(settings.timezone)
    after = None
    if intent.action == "edit":
        after = se.swap(unit, intent, text)
        if after is None:
            if llm is None:
                await send(UNAVAILABLE, None)
                return
            await typing()
            try:
                after = await _reestimate(unit, text, tg_id, sessionmaker, llm, typing)
            except LLMError:
                await send(UNAVAILABLE, None)
                return
        if after is None:
            await send(NOT_UNDERSTOOD, None)
            return
        if se.unchanged(unit, after):
            await send(SAME, None)
            return
        preview = se.edit_preview(unit, after, tz)
        if after.note and after.note.strip():
            preview += f"\n\n{after.note.strip()}"
    else:
        preview = se.delete_preview(unit, tz)
    token = _token(OFFERS)
    OFFERS[token] = Offer(tg_id, unit, intent.action, after, raw, utcnow(), soft=intent.soft)
    await send(preview, confirm_kb(token, intent.action))


def _take(store: dict, token: str, user_id: int):  # type: ignore[no-untyped-def]
    """Pop the caller's own fresh item (pop: a double tap must not apply twice); someone else's stays."""
    item = store.pop(token, None)
    if item is not None and item.user_id != user_id:
        store[token] = item
        return None
    if item is not None and utcnow() - item.at > TTL:
        return None
    return item


@router.callback_query(F.data.startswith("fixpick:"))
async def pick(
    cb: CallbackQuery, settings: Settings, sessionmaker: Sessionmaker, llm: OpenRouterClient | None = None
) -> None:
    _, token, index = ((cb.data or "") + "::").split(":")[:3]
    choice = _take(CHOICES, token, cb.from_user.id)
    if choice is None:
        await cb.answer(STALE, show_alert=True)
        return
    try:
        unit = choice.units[int(index)]
    except (ValueError, IndexError):
        CHOICES[token] = choice
        await cb.answer()
        return
    await cb.answer()  # the edit may wait for the model: do not keep the button spinning

    async def send(reply: str, kb: InlineKeyboardMarkup | None) -> object:
        return await cb.message.edit_text(reply, reply_markup=kb)  # type: ignore[union-attr]

    async def typing() -> None:
        with contextlib.suppress(Exception):
            await cb.bot.send_chat_action(cb.message.chat.id, "typing")  # type: ignore[union-attr]

    if cb.message is None:
        return
    await _offer(
        unit, choice.intent, choice.text, choice.raw, cb.from_user.id, settings, sessionmaker, llm, send, typing
    )


@router.callback_query(F.data.startswith("fixok:"))
async def confirm(cb: CallbackQuery, settings: Settings, sessionmaker: Sessionmaker) -> None:
    token = (cb.data or "").split(":", 1)[1]
    offer = _take(OFFERS, token, cb.from_user.id)
    if offer is None:
        await cb.answer(STALE, show_alert=True)
        return
    tz = ZoneInfo(settings.timezone)
    try:
        async with sessionmaker() as session:
            user = await get_or_create_user(session, cb.from_user.id, cb.from_user.full_name)
            raised = await se.apply(
                session, user.id, offer.unit, offer.action, offer.after, offer.raw, utcnow(), tz
            )
            await session.commit()
    except se.Stale:
        if cb.message:
            await cb.message.edit_text(STALE)  # type: ignore[union-attr]
        await cb.answer(STALE, show_alert=True)
        return
    except Exception:
        OFFERS[token] = offer  # let the user press again
        raise
    live.publish(user.id, *se.TOPICS[offer.unit.kind])  # type: ignore[arg-type]
    if cb.message:
        if offer.action == "delete":
            shown = se.delete_preview(offer.unit, tz).split("\n", 1)[-1]
            text = f"Удалено ✅\n{shown}"
        else:
            assert offer.after is not None
            shown = se.edit_preview(offer.unit, offer.after, tz).split("\n", 1)[-1]
            text = f"Исправлено ✅\n{shown}"
        await cb.message.edit_text(text)  # type: ignore[union-attr]
    await cb.answer()
    if raised and cb.message:  # a corrected set may be a new record (no deload offer for a fix)
        message = cb.message

        async def send(text: str, kb: InlineKeyboardMarkup | None) -> object:
            return await message.answer(text, reply_markup=kb)  # type: ignore[union-attr]

        await workout_events.after_save(sessionmaker, user.id, raised, send, tz, check_deload=False)


@router.callback_query(F.data.startswith("fixno:"))
async def cancel(cb: CallbackQuery) -> None:
    token = (cb.data or "").split(":", 1)[1]
    offer = _take(OFFERS, token, cb.from_user.id)
    choice = _take(CHOICES, token, cb.from_user.id) if offer is None else None
    if offer is None and choice is None:
        await cb.answer(STALE, show_alert=True)
        return
    if cb.message:
        soft = offer is not None and offer.soft
        await cb.message.edit_text("Отменено." + (f" {SOFT_HINT}" if soft else ""))  # type: ignore[union-attr]
    await cb.answer()
