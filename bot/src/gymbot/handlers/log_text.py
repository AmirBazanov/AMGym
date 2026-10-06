"""Free-text logging: message -> LLM -> preview -> user confirms -> saved.

Saving is behind a confirm button on purpose: free models make mistakes, and a wrong
set silently written to the log ruins progress charts.

Dialog context. Each user's last parsed exchange (texts, result, time) is kept in memory for
CONTEXT_TTL and sent to the model as history with the next message, so "три штуки" after
"три куриные самсы" corrects that record instead of being parsed on its own. Rules:
- The next record either revises the previous one or is new (see is_revision: the model's
  `revises` flag, or for food a common name, because free models set the flag unreliably;
  wellbeing after wellbeing is always a revision: one "how do I feel" record per dialog).
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

Facts (gymbot.services.facts). "запомни: <факт>" is caught by a regexp before the model and offered
as "Запомнить? «…»" with its own button. The model may also return `remember` (a lasting fact in an
ordinary message): the preview gets a "Запомнить: «…»" line and a separate "🧠 Запомнить" row under
"Сохранить / Отмена" (the same token, kept in FACTS). A fact is saved only by that button, independently
of the record: saving the record first keeps the button, a revision carries the offer to the new preview,
"Отмена" drops both. A fact that is already active is not offered again. Active facts go to the model
with every message.

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
from gymbot.llm.schemas import REMEMBER_MAX, ParsedWellbeing, ParseResult
from gymbot.services import facts
from gymbot.services.programs import exercise_catalog
from gymbot.services.users import get_or_create_user
from gymbot.services.wellbeing import wellbeing_entry
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


@dataclass
class PendingFact:
    """A fact offered for "🧠 Запомнить" (see the module docstring)."""

    user_id: int
    text: str
    source_text: str  # the message it came from, "[voice] ..." for voice
    standalone: bool  # True: the preview offers only the fact; False: it sits under a record preview
    answer: str = ""  # the model's reply shown above a standalone offer, kept when the fact is saved


# Parsed messages waiting for "Сохранить". In memory: a restart just means pressing again after resending.
PENDING: dict[str, Pending] = {}
MAX_PENDING = 200

# Last exchange per Telegram user id, see the module docstring.
CONTEXT: dict[int, Exchange] = {}
CONTEXT_TTL = timedelta(minutes=15)
MAX_CHAIN = 3  # messages of a chain sent to the model as one turn; raw_text keeps them all

# Offered facts by preview token; a record preview shares its token with PENDING.
FACTS: dict[str, PendingFact] = {}

HINT = "Не понял. Напиши, например: «присед 4х8 по 80»."

# "запомни: ...", "запомни, что ...", "Запомните — ..."; not "запомнилось".
REMEMBER_CMD = re.compile(
    r"^\s*запомни(?:те)?(?=$|[\s,:;.!—–-])[\s,:;.!—–-]*(?:пожалуйста(?=$|[\s,:])[\s,:]*)?(?:что(?=$|[\s,:])[\s,:]*)?(?P<fact>.*)$",
    re.IGNORECASE | re.DOTALL,
)
REMEMBER_HINT = "Напиши, что запомнить, например «запомни: не ем творог»."
REMEMBER_TOO_LONG = f"Слишком длинно: факт до {REMEMBER_MAX} символов. Сократи и пришли ещё раз."


def _norm(text: str) -> str:
    return " ".join(text.casefold().split()).strip(" .,!?…")


def _tail(result: ParseResult, source_text: str) -> str:
    """Paragraphs under a record's list: the model's note, then its question about an unclear word.

    Neither is shown if it only repeats the user's text.
    """
    parts = [
        f"{prefix}{text}"
        for prefix, value in (("", result.note), ("Уточни: ", result.clarification))
        if (text := (value or "").strip()) and _norm(text) != _norm(source_text)
    ]
    return "".join(f"\n\n{p}" for p in parts)


def _wellbeing_lines(w: ParsedWellbeing) -> list[str]:
    lines = []
    if w.sleep_hours is not None:
        lines.append(f"Сон {w.sleep_hours:g} ч" + (f" (качество {w.sleep_quality}/5)" if w.sleep_quality else ""))
    elif w.sleep_quality:
        lines.append(f"Качество сна {w.sleep_quality}/5")
    lines += [f"{label} {v}/5" for label, v in (("Энергия", w.energy), ("Настроение", w.mood)) if v]
    if w.pains:
        lines.append("Боли: " + ", ".join(p.place + (f" ({p.severity}/5)" if p.severity else "") for p in w.pains))
    if w.note:
        lines.append(f"Заметка: {w.note}")
    return [f"• {line}" for line in lines]


def render_preview(result: ParseResult, source_text: str = "") -> str:
    """Preview of a parsed message. `source_text` is the user's text: it is never echoed back.

    For a record the model's note (what it changed and why) and its question about an unclear word
    ("Уточни: …") go after the list, so "Записать?" stays the first line.
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
        return "Записать?\n" + "\n".join(lines) + _tail(result, source_text)
    if result.kind == "food" and result.foods:
        lines = [
            f"• {f.description}{f' {f.grams:g} г' if f.grams else ''}: {f.kcal:.0f} ккал, "
            f"Б{f.protein_g:.0f} Ж{f.fat_g:.0f} У{f.carbs_g:.0f}"
            for f in result.foods
        ]
        total = sum(f.kcal for f in result.foods)
        return f"Записать еду? Всего {total:.0f} ккал\n" + "\n".join(lines) + _tail(result, source_text)
    if result.is_record() and result.wellbeing is not None:
        lines = _wellbeing_lines(result.wellbeing)
        return "Записать самочувствие?\n" + "\n".join(lines) + _tail(result, source_text)
    answer = (result.clarification or "").strip()
    if not answer or _norm(answer) == _norm(source_text):
        return HINT
    return answer


_PIECES = re.compile(r",?\s*\d+(?:[.,]\d+)?\s*шт\.?$")


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
    Wellbeing after wellbeing always revises: the model returns the full merged state (sleep, pains...),
    so the new preview replaces the old one. Wellbeing never revises another kind and vice versa, whatever
    the flag: live runs showed `revises=true` on a fresh wellbeing message, which would drop a food preview.
    """
    if "wellbeing" in (prev.kind, new.kind):
        return prev.kind == new.kind
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


def keyboard(token: str, *, record: bool, fact: bool, cancel: bool = True) -> InlineKeyboardMarkup | None:
    """Buttons of a preview: "Сохранить / Отмена" for a record, "🧠 Запомнить" for an offered fact.

    With a record the fact button is a separate second row, so the first row never changes. A fact-only
    preview gets "Отмена" next to it unless `cancel` is False (under a saved record it would say "Отменено").
    """
    save = InlineKeyboardButton(text="✅ Сохранить", callback_data=f"save:{token}")
    drop = InlineKeyboardButton(text="✖ Отмена", callback_data=f"drop:{token}")
    remember = InlineKeyboardButton(text="🧠 Запомнить", callback_data=f"remember:{token}")
    rows = []
    if record:
        rows.append([save, drop])
    if fact:
        rows.append([remember] if record or not cancel else [remember, drop])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def confirm_kb(token: str) -> InlineKeyboardMarkup:
    kb = keyboard(token, record=True, fact=False)
    assert kb is not None
    return kb


def _offer_line(fact: str) -> str:
    return f"\n\nЗапомнить: «{fact}»"


def _new_token(store: dict) -> str:
    if len(store) >= MAX_PENDING:
        store.pop(next(iter(store)))
    return secrets.token_hex(6)


async def _offer_command(message: Message, fact_text: str, raw: str, prefix: str) -> None:
    """Reply to "запомни: ...": the fact with a "🧠 Запомнить" button, or a hint."""
    fact = facts.clean(fact_text).rstrip(" .!")
    if not fact:
        await message.answer(prefix + REMEMBER_HINT)
        return
    if len(fact) > REMEMBER_MAX:
        await message.answer(prefix + REMEMBER_TOO_LONG)
        return
    token = _new_token(FACTS)
    FACTS[token] = PendingFact(message.from_user.id, fact, raw, standalone=True)  # type: ignore[union-attr]
    await message.answer(f"{prefix}Запомнить? «{fact}»", reply_markup=keyboard(token, record=False, fact=True))


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
    if m := REMEMBER_CMD.match(text):
        await _offer_command(message, m["fact"], raw, prefix)
        return
    async with sessionmaker() as session:
        catalog = await exercise_catalog(session)
        known = await facts.prompt_facts(session, user_id)
    prev = recent_exchange(user_id, message.date)
    await message.bot.send_chat_action(message.chat.id, "typing")  # type: ignore[union-attr]
    try:
        result = await llm.parse_message(text, catalog, prev.history() if prev else None, known)
    except LLMError:
        await message.answer(prefix + "Нейросеть сейчас недоступна, попробуй ещё раз чуть позже.")
        return
    # The model tends to repeat facts it was given: offer only new ones.
    offer = result.remember
    if offer and facts.normalize(offer) in {facts.normalize(k) for k in known}:
        offer = None
    preview = prefix + render_preview(result, source_text=text)
    if not result.is_record() and offer:  # a fact in a question or an unclear message: offer it alone
        answer = preview if preview != prefix + HINT else ""
        token = _new_token(FACTS)
        FACTS[token] = PendingFact(user_id, offer, raw, standalone=True, answer=answer)
        shown = answer + _offer_line(offer) if answer else f"{prefix}Запомнить? «{offer}»"
        await message.answer(shown, reply_markup=keyboard(token, record=False, fact=True))
        if result.kind == "question":
            return
    elif result.kind == "question":
        await message.answer(preview)
        return
    # Updates are handled concurrently: while the model was thinking, the previous preview may have
    # been saved, cancelled or replaced by another message. Then this is not its continuation.
    if prev is not None and (CONTEXT.get(user_id) is not prev or (prev.token and prev.token not in PENDING)):
        prev = None
    if not result.is_record():  # the model asks a clarifying question
        if prev is None:
            exchange = Exchange([text], [raw], result, message.date)
        elif prev.token:  # keep the record the question is about
            q = prev.question
            asked = Question([*(q.texts if q else []), text], [*(q.raws if q else []), raw], result)
            exchange = Exchange(prev.texts, prev.raws, prev.result, message.date, prev.token, asked)
        else:  # no record yet: keep collecting the answers
            exchange = Exchange([*prev.texts, text], [*prev.raws, raw], result, message.date)
        _remember(user_id, exchange)
        if not offer:  # with an offer the reply has been sent above
            await message.answer(preview)
        return
    texts, raws = [text], [raw]
    fact = PendingFact(user_id, offer, raw, standalone=False) if offer else None
    if prev is not None:
        if prev.token is None:  # answers to the model's questions before any record
            texts, raws = [*prev.texts, text], [*prev.raws, raw]
        elif is_revision(prev.result, result):
            PENDING.pop(prev.token, None)  # replaced by this preview, see the module docstring
            carried = FACTS.pop(prev.token, None)  # the offer moves to the new preview
            fact = fact or carried
            texts, raws = [*prev.all_texts(), text], [*prev.all_raws(), raw]
    token = _new_token(PENDING)
    exchange = Exchange(texts, raws, result, message.date, token)
    PENDING[token] = Pending(user_id, result, exchange.raw_text, message.date)
    _remember(user_id, exchange)
    if fact is not None:
        if len(FACTS) >= MAX_PENDING:
            FACTS.pop(next(iter(FACTS)))
        FACTS[token] = fact
        preview += _offer_line(fact.text)
    await message.answer(preview, reply_markup=keyboard(token, record=True, fact=fact is not None))


@router.callback_query(F.data.startswith("drop:"))
async def drop(cb: CallbackQuery) -> None:
    token = (cb.data or "").split(":", 1)[1]
    pending = PENDING.get(token)
    if pending is not None and pending.user_id == cb.from_user.id:
        PENDING.pop(token, None)
        _forget(cb.from_user.id, token)
    offered = FACTS.get(token)
    if offered is not None and offered.user_id == cb.from_user.id:
        FACTS.pop(token, None)
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
        saved = pending.result.model_copy(update={"clarification": None})  # the question is moot now
        shown = re.sub(r"^Записать( еду| самочувствие)?\?\s*", "", render_preview(saved))
        offered = FACTS.get(token)  # not remembered yet: keep its button
        text = f"{shown}\n\n{note}".strip() + (_offer_line(offered.text) if offered else "")
        await cb.message.edit_text(  # type: ignore[union-attr]
            text, reply_markup=keyboard(token, record=False, fact=offered is not None, cancel=False)
        )
    await cb.answer()


@router.callback_query(F.data.startswith("remember:"))
async def remember(cb: CallbackQuery, sessionmaker: Sessionmaker) -> None:
    token = (cb.data or "").split(":", 1)[1]
    # pop, not get: a double tap must not add the fact twice.
    offered = FACTS.pop(token, None)
    if offered is None or offered.user_id != cb.from_user.id:
        if offered is not None:
            FACTS[token] = offered  # someone else's preview: leave it
        await cb.answer("Этот факт уже сохранён или предложение устарело.", show_alert=True)
        return
    try:
        async with sessionmaker() as session:
            user = await get_or_create_user(session, cb.from_user.id, cb.from_user.full_name)
            added = await facts.add_fact(session, user.id, offered.text, source_text=offered.source_text)
            await session.commit()
    except Exception:
        FACTS[token] = offered  # let the user press again
        raise
    if added.status == "limit":
        FACTS[token] = offered
        await cb.answer(
            f"Фактов уже {facts.MAX_ACTIVE}: удали лишние в /facts или в дневнике.", show_alert=True
        )
        return
    done = (
        f"Запомнил: «{offered.text}» ✅ Все факты: /facts"
        if added.status == "created"
        else f"Это я уже помню: «{offered.text}»"
    )
    if offered.standalone:
        if cb.message:
            text = f"{offered.answer}\n\n{done}" if offered.answer else done
            await cb.message.edit_text(text)  # type: ignore[union-attr]
        await cb.answer()
        return
    if cb.message:  # the record's buttons stay while it is not saved
        await cb.message.edit_reply_markup(  # type: ignore[union-attr]
            reply_markup=keyboard(token, record=token in PENDING, fact=False)
        )
    await cb.answer(done)


async def _save(pending: Pending, cb: CallbackQuery, today: date, sessionmaker: Sessionmaker) -> str:
    async with sessionmaker() as session:
        user = await get_or_create_user(session, cb.from_user.id, cb.from_user.full_name)
        if pending.result.kind == "workout":
            await save_from_chat(session, user, pending.result, pending.raw_text, today)
            note = "Сохранено ✅ Видно в дневнике, /undo — отменить."
        elif pending.result.kind == "wellbeing":
            assert pending.result.wellbeing is not None  # is_record() was checked before the preview
            session.add(wellbeing_entry(user.id, pending.result.wellbeing, pending.raw_text, pending.sent_at))
            note = "Самочувствие сохранено ✅"
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
