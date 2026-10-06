"""Free-text logging: message -> LLM -> preview -> user confirms -> saved.

Saving is behind a confirm button on purpose: free models make mistakes, and a wrong
set silently written to the log ruins progress charts.

Dialog context. Each user's last parsed exchange (texts, result, time) is kept in memory for
CONTEXT_TTL and sent to the model as history with the next message, so "три штуки" after
"три куриные самсы" corrects that record instead of being parsed on its own. Rules:
- The next record either revises the previous one or is new (see is_revision: the model's
  `revises` flag, or for food a common name, because free models set the flag unreliably).
- A revision replaces the previous preview: the old "Сохранить" button stops working, otherwise
  the user could save both the wrong and the corrected record. Its raw_text is the chain of texts
  ("три куриные самсы\nтри штуки").
- A new record leaves the previous preview alone (both can be saved), its raw_text is its own text,
  and it becomes the only history for the next message.
- A clarifying question from the model (kind="unknown") about a pending record keeps that record:
  the model then sees both turns (the record and its question), and the next message still has to
  pass is_revision to replace the preview. Without a record (the first message was unclear),
  the answers just continue the chain.
- raw_text keeps the whole chain; only the history sent to the model is cut to MAX_CHAIN messages.
- Updates run concurrently: if the context changed or its preview was saved / cancelled while the
  model was thinking, the parsed message is treated as a new record.
- Save or cancel of the preview the context belongs to ends the dialog: the context is deleted,
  so the next record is parsed fresh. Pressing an older preview does not touch a newer context.
- Off-topic questions (kind="question") do not take part: the context stays as it was.

Voice messages (handlers/voice.py) go through the same process_text with the transcript as the text.
Each message of a chain has two forms: the clean text (Exchange.texts), which the model sees, and
the raw form (Exchange.raws), which is stored: the same text for typed messages, "[voice] <transcript>"
for voice ones. So the marker is on every voice line of a chain and only there
("самса\n[voice] три штуки"), and the model never sees it.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from datetime import date, datetime, timedelta
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
    sent_at: datetime  # when the message was sent: a set written at 23:59 belongs to that day


@dataclass
class Question:
    """Messages after a record that the model answered with a clarifying question (see Exchange)."""

    texts: list[str]
    raws: list[str]
    result: ParseResult


@dataclass
class Exchange:
    """One user's dialog about one record (see the module docstring)."""

    texts: list[str]  # every message behind `result`, oldest first, as the model sees them
    raws: list[str]  # the same messages as stored: "[voice] ..." for voice ones; joined into raw_text
    result: ParseResult  # the record, or the model's clarifying question while there is no record yet
    at: datetime  # send time of the last message (Telegram, UTC)
    token: str | None = None  # preview of the record in PENDING
    # Messages after the record that the model answered with a clarifying question, and that answer.
    question: Question | None = None

    @property
    def raw_text(self) -> str:
        return "\n".join(self.raws)

    def all_texts(self) -> list[str]:
        return [*self.texts, *(self.question.texts if self.question else [])]

    def all_raws(self) -> list[str]:
        return [*self.raws, *(self.question.raws if self.question else [])]

    def history(self) -> list[tuple[str, str]]:
        """(user text, assistant JSON) turns for the model; only the last MAX_CHAIN messages of each."""
        turns = [(self.texts, self.result)]
        if self.question:
            turns.append((self.question.texts, self.question.result))
        return [("\n".join(texts[-MAX_CHAIN:]), result.model_dump_json()) for texts, result in turns]


# Parsed messages waiting for "Сохранить". In memory: a restart just means pressing again after resending.
PENDING: dict[str, Pending] = {}
MAX_PENDING = 200

# Last exchange per Telegram user id, see the module docstring.
CONTEXT: dict[int, Exchange] = {}
CONTEXT_TTL = timedelta(minutes=15)
MAX_CHAIN = 3  # messages of a chain sent to the model as one turn; raw_text keeps them all

HINT = "Не понял. Напиши, например: «присед 4х8 по 80»."


def _norm(text: str) -> str:
    return " ".join(text.casefold().split()).strip(" .,!?…")


def _note(result: ParseResult, source_text: str) -> str:
    """The model's explanation of an estimate, as a paragraph under the list (never an echo)."""
    note = (result.note or "").strip()
    return f"\n\n{note}" if note and _norm(note) != _norm(source_text) else ""


def render_preview(result: ParseResult, source_text: str = "") -> str:
    """Preview of a parsed message. `source_text` is the user's text: it is never echoed back.

    For a record the model's note (what it changed and why) goes after the list, so the question
    "Записать?" stays the first line.
    """
    if result.kind == "workout" and result.exercises:
        lines = []
        for ex in result.exercises:
            sets = ", ".join(
                (f"{s.weight_kg:g} кг × {s.reps}" if s.weight_kg is not None else f"{s.reps} повт.")
                + (" (дроп)" if s.drop_index else "")
                for s in ex.sets
            )
            lines.append(f"• {ex.exercise}: {sets}")
        return "Записать?\n" + "\n".join(lines) + _note(result, source_text)
    if result.kind == "food" and result.foods:
        lines = [
            f"• {f.description}{f' {f.grams:g} г' if f.grams else ''}: {f.kcal:.0f} ккал, "
            f"Б{f.protein_g:.0f} Ж{f.fat_g:.0f} У{f.carbs_g:.0f}"
            for f in result.foods
        ]
        total = sum(f.kcal for f in result.foods)
        return f"Записать еду? Всего {total:.0f} ккал\n" + "\n".join(lines) + _note(result, source_text)
    answer = (result.clarification or "").strip()
    if not answer or _norm(answer) == _norm(source_text):
        return HINT
    return answer


_PIECES = re.compile(r",?\s*\d+\s*шт\.?$")


def _names(result: ParseResult) -> set[str]:
    if result.kind == "workout":
        raw = [e.exercise for e in result.exercises]
    elif result.kind == "food":
        raw = [f.description for f in result.foods]
    else:
        return set()
    return {_PIECES.sub("", " ".join(n.casefold().split())).strip() for n in raw} - {""}


def is_revision(prev: ParseResult, new: ParseResult) -> bool:
    """Whether `new` corrects `prev` rather than being a separate record.

    The model's `revises` flag decides. For food a common name ("самса, 1 шт" -> "самса, 3 шт")
    counts too. Not for workouts: the same exercise sent twice is usually the next set, not a fix.
    """
    if new.revises:
        return True
    return prev.kind == new.kind == "food" and bool(_names(prev) & _names(new))


def recent_exchange(user_id: int, now: datetime) -> Exchange | None:
    ex = CONTEXT.get(user_id)
    if ex is not None and now - ex.at > CONTEXT_TTL:
        del CONTEXT[user_id]
        return None
    return ex


def _remember(user_id: int, ex: Exchange) -> None:
    CONTEXT.pop(user_id, None)  # re-insert: dict order = eviction order
    if len(CONTEXT) >= MAX_PENDING:
        CONTEXT.pop(next(iter(CONTEXT)))
    CONTEXT[user_id] = ex


def _forget(user_id: int, token: str) -> None:
    ex = CONTEXT.get(user_id)
    if ex is not None and ex.token == token:
        del CONTEXT[user_id]


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
async def log_free_text(
    message: Message, settings: Settings, sessionmaker: Sessionmaker, llm: OpenRouterClient
) -> None:
    await process_text(message, message.text or "", settings, sessionmaker, llm)


async def process_text(
    message: Message,
    text: str,
    settings: Settings,
    sessionmaker: Sessionmaker,
    llm: OpenRouterClient,
    *,
    raw_text: str | None = None,
    prefix: str = "",
) -> None:
    """Parse `text`, reply with a preview and a confirm button (or the model's answer), keep the context.

    `text` is what the model and the dialog context see: the typed text or a clean voice transcript.
    `raw_text` is how this one message is stored (default: `text`); voice passes "[voice] <transcript>".
    In a chain each message keeps its own form, so a typed "самса" corrected by voice "три штуки" is
    saved as "самса\n[voice] три штуки", and the history sent to the model has no marker at all.
    /undo groups sets by equal raw_text, which still holds: all sets of one record share it.
    `prefix` goes before every reply, e.g. "Распознал: «...»\n\n" so the user sees what was heard.
    """
    raw = text if raw_text is None else raw_text
    user_id = message.from_user.id  # type: ignore[union-attr]
    async with sessionmaker() as session:
        catalog = await exercise_catalog(session)
    prev = recent_exchange(user_id, message.date)
    await message.bot.send_chat_action(message.chat.id, "typing")  # type: ignore[union-attr]
    try:
        result = await llm.parse_message(text, catalog, prev.history() if prev else None)
    except LLMError:
        await message.answer(prefix + "Нейросеть сейчас недоступна, попробуй ещё раз чуть позже.")
        return
    preview = prefix + render_preview(result, source_text=text)
    if result.kind == "question":
        await message.answer(preview)
        return
    # Updates are handled concurrently: while the model was thinking, the previous preview may have
    # been saved, cancelled or replaced by another message. Then this is not its continuation.
    if prev is not None and (CONTEXT.get(user_id) is not prev or (prev.token and prev.token not in PENDING)):
        prev = None
    savable = (result.kind == "workout" and result.exercises) or (result.kind == "food" and result.foods)
    if not savable:  # the model asks a clarifying question
        if prev is None:
            exchange = Exchange([text], [raw], result, message.date)
        elif prev.token:  # keep the record the question is about
            q = prev.question
            asked = Question([*(q.texts if q else []), text], [*(q.raws if q else []), raw], result)
            exchange = Exchange(prev.texts, prev.raws, prev.result, message.date, prev.token, asked)
        else:  # no record yet: keep collecting the answers
            exchange = Exchange([*prev.texts, text], [*prev.raws, raw], result, message.date)
        _remember(user_id, exchange)
        await message.answer(preview)
        return
    texts, raws = [text], [raw]
    if prev is not None:
        if prev.token is None:  # answers to the model's questions before any record
            texts, raws = [*prev.texts, text], [*prev.raws, raw]
        elif is_revision(prev.result, result):
            PENDING.pop(prev.token, None)  # replaced by this preview, see the module docstring
            texts, raws = [*prev.all_texts(), text], [*prev.all_raws(), raw]
    if len(PENDING) >= MAX_PENDING:
        PENDING.pop(next(iter(PENDING)))
    token = secrets.token_hex(6)
    exchange = Exchange(texts, raws, result, message.date, token)
    PENDING[token] = Pending(user_id, result, exchange.raw_text, message.date)
    _remember(user_id, exchange)
    await message.answer(preview, reply_markup=confirm_kb(token))


@router.callback_query(F.data.startswith("drop:"))
async def drop(cb: CallbackQuery) -> None:
    token = (cb.data or "").split(":", 1)[1]
    pending = PENDING.get(token)
    if pending is not None and pending.user_id == cb.from_user.id:
        PENDING.pop(token, None)
        _forget(cb.from_user.id, token)
    if cb.message:
        await cb.message.edit_text("Отменено.")  # type: ignore[union-attr]
    await cb.answer()


@router.callback_query(F.data.startswith("save:"))
async def save(cb: CallbackQuery, settings: Settings, sessionmaker: Sessionmaker) -> None:
    token = (cb.data or "").split(":", 1)[1]
    # pop, not get: updates run concurrently, a double tap must not save the sets twice.
    pending = PENDING.pop(token, None)
    if pending is None or pending.user_id != cb.from_user.id:
        await cb.answer("Эта запись уже сохранена или устарела.", show_alert=True)
        return
    today = pending.sent_at.astimezone(ZoneInfo(settings.timezone)).date()
    try:
        note = await _save(pending, cb, today, sessionmaker)
    except Exception:
        PENDING[token] = pending  # let the user press again
        raise
    _forget(cb.from_user.id, token)
    if cb.message:
        shown = re.sub(r"^Записать( еду)?\?\s*", "", render_preview(pending.result))
        await cb.message.edit_text(f"{shown}\n\n{note}".strip())  # type: ignore[union-attr]
    await cb.answer()


async def _save(pending: Pending, cb: CallbackQuery, today: date, sessionmaker: Sessionmaker) -> str:
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
                        raw_text=pending.raw_text,
                        eaten_at=pending.sent_at,  # the message time, not the time of the tap
                    )
                )
            note = "Еда сохранена ✅"
        await session.commit()
    return note
