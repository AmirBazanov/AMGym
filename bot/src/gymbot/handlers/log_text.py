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

Unknown words (gymbot.services.food_lookup). The model lists words it could not identify in
`unknown_terms` (food or an unclear message) instead of inventing macros. For up to LOOKUP_TERMS of them
the bot searches Open Food Facts and Wikipedia (Tavily with a key), asks the model for up to 3 variants
with macros per piece or portion, and adds "Не знаю «курт». Варианты:" with a button per variant
(`lookup:<token>:<i>`) and "Другое" (a hint to answer in text) under the preview; the rest stay a question.
A tap adds the variant to the record (or makes a record of an unclear message) with the count or grams
from the phrase ("5 маленьких куртов" = 5 × one piece), replaces the preview in place and updates the
dialog context like a revision; the record still waits for "Сохранить". Nothing found = the model's
question as before. Variants of a preview that was saved, cancelled or revised are stale.

Voice messages (handlers/voice.py) go through the same process_text with the transcript as the text.
Each message of a chain has two forms: the clean text (Exchange.texts), which the model sees, and
the raw form (Exchange.raws), which is stored: the same text for typed messages, "[voice] <transcript>"
for voice ones. So the marker is on every voice line of a chain and only there
("самса\n[voice] три штуки"), and the model never sees it.
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from gymbot.config import Settings
from gymbot.db.models import FoodEntry
from gymbot.db.session import Sessionmaker
from gymbot.handlers.plan import send_after_wellbeing
from gymbot.llm.openrouter import LLMError, OpenRouterClient
from gymbot.llm.schemas import REMEMBER_MAX, ParsedFood, ParsedWellbeing, ParseResult
from gymbot.services import facts, food_lookup
from gymbot.services.programs import exercise_catalog
from gymbot.services.users import get_or_create_user
from gymbot.services.wellbeing import wellbeing_entry
from gymbot.services.workouts import save_from_chat

log = logging.getLogger(__name__)

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


@dataclass
class Variant:
    term: str  # the unknown word as the model returned it
    option: food_lookup.Option  # one piece or one usual portion (the button)
    amount: food_lookup.Amount  # count or grams from the user's phrase
    food: ParsedFood  # the option with that amount (what a tap adds)


@dataclass
class Lookup:
    """Variants for unknown words under a preview (see the module docstring)."""

    user_id: int
    result: ParseResult  # what the preview shows now
    exchange: Exchange  # the dialog the preview belongs to
    variants: list[Variant]  # numbered by position in the buttons; never reordered
    asked: list[str]  # unknown words without variants: they stay a question
    source_text: str  # the message the preview answers (never echoed back)
    prefix: str = ""  # "Распознал: «…»" of a voice message
    record: bool = False  # the preview's record is in PENDING under the same token
    resolved: set[str] = field(default_factory=set)  # words a variant was picked for


# Parsed messages waiting for "Сохранить". In memory: a restart just means pressing again after resending.
PENDING: dict[str, Pending] = {}
MAX_PENDING = 200

# Last exchange per Telegram user id, see the module docstring.
CONTEXT: dict[int, Exchange] = {}
CONTEXT_TTL = timedelta(minutes=15)
MAX_CHAIN = 3  # messages of a chain sent to the model as one turn; raw_text keeps them all

# Offered facts by preview token; a record preview shares its token with PENDING.
FACTS: dict[str, PendingFact] = {}

# Variants offered for unknown words, by preview token (shared with PENDING when there is a record).
LOOKUPS: dict[str, Lookup] = {}
LOOKUP_TERMS = 2  # words looked up per message; the rest stay in the question

HINT = "Не понял. Напиши, например: «присед 4х8 по 80»."
OTHER_HINT = "Напиши текстом, что это было, например «курт — сушёный сыр, 10 г за штуку»."
LOOKUP_STALE = "Эти варианты уже не действуют: запись сохранена, отменена или исправлена."

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


def _question(terms: list[str]) -> str | None:
    return ", ".join(f"«{t}»" for t in terms) + " — это что?" if terms else None


def _amount_hint(amt: food_lookup.Amount) -> str:
    """" на 5 шт" or " на 200 г" after "Варианты" when the phrase had an amount."""
    if amt.grams:
        return f" на {amt.grams:g} г"
    return f" на {amt.count:g} шт" if amt.count != 1 else ""


def _label(n: int, option: food_lookup.Option) -> str:
    short = option.name.split(" (")[0][:24]
    return f"{n} · {short} {option.portion_g:.0f} г · {option.kcal:.0f} ккал"


def render_lookup(lk: Lookup) -> str:
    """The preview with variant blocks; the model's question about the words is replaced by them."""
    result = lk.result.model_copy(update={"clarification": _question(lk.asked)})
    head = render_preview(result, lk.source_text) if result.is_record() else (result.clarification or "")
    blocks = []
    for term in dict.fromkeys(v.term for v in lk.variants if v.term not in lk.resolved):
        numbered = [(i, v) for i, v in enumerate(lk.variants, 1) if v.term == term]
        lines = [
            f"{i}. {v.option.name}, {v.option.portion_g:.0f} г: {v.option.kcal:.0f} ккал, "
            f"Б{v.option.protein_g:.0f} Ж{v.option.fat_g:.0f} У{v.option.carbs_g:.0f}"
            + (f" ({v.option.note})" if v.option.note else "")
            for i, v in numbered
        ]
        hint = _amount_hint(numbered[0][1].amount)
        blocks.append(f"Не знаю «{term}». Варианты{hint}:\n" + "\n".join(lines))
    if blocks:
        blocks.append("Выбери кнопкой или напиши, что это.")
    return "\n\n".join(part for part in (head, *blocks) if part)


def lookup_keyboard(token: str, lk: Lookup, *, fact: bool) -> InlineKeyboardMarkup | None:
    """Save/Cancel first (when there is a record), then a row per open variant, "Другое", "🧠 Запомнить"."""
    kb = keyboard(token, record=lk.record, fact=False)
    rows = list(kb.inline_keyboard) if kb else []
    open_variants = [(i, v) for i, v in enumerate(lk.variants) if v.term not in lk.resolved]
    for i, v in open_variants:
        rows.append([InlineKeyboardButton(text=_label(i + 1, v.option), callback_data=f"lookup:{token}:{i}")])
    if open_variants:
        other = InlineKeyboardButton(text="Другое", callback_data=f"lookup:{token}:other")
        drop_btn = InlineKeyboardButton(text="✖ Отмена", callback_data=f"drop:{token}")
        rows.append([other] if lk.record else [other, drop_btn])
    if fact:
        rows.append([InlineKeyboardButton(text="🧠 Запомнить", callback_data=f"remember:{token}")])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


async def _variants(result: ParseResult, text: str, settings: Settings, llm: OpenRouterClient) -> list[Variant]:
    """Variants for the first LOOKUP_TERMS unknown words; any failure = no variants (the old question)."""
    terms = result.unknown_terms[:LOOKUP_TERMS]
    try:
        found = await asyncio.gather(
            *(
                food_lookup.find_options(t, text, llm, llm.http, tavily_key=settings.tavily_api_key)
                for t in terms
            )
        )
    except Exception as e:  # noqa: BLE001 - the lookup is an extra: it must never cost the user the preview
        log.warning("food lookup failed: %s", type(e).__name__)
        return []
    variants = []
    for term, options in zip(terms, found, strict=True):
        amt = food_lookup.amount(text, term)
        variants += [Variant(term, o, amt, food_lookup.as_food(o, amt)) for o in options]
    return variants


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
    variants: list[Variant] = []
    if (
        result.kind in ("food", "unknown")
        and result.unknown_terms
        # A question about a pending record keeps that record; variants there would edit another preview.
        and (result.is_record() or prev is None or prev.token is None)
    ):
        await message.bot.send_chat_action(message.chat.id, "typing")  # type: ignore[union-attr]
        variants = await _variants(result, text, settings, llm)
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
    asked = result.unknown_terms[LOOKUP_TERMS:] + [
        t for t in result.unknown_terms[:LOOKUP_TERMS] if all(v.term != t for v in variants)
    ]
    if not result.is_record():  # the model asks a clarifying question
        if prev is None:
            exchange = Exchange([text], [raw], result, message.date)
        elif prev.token:  # keep the record the question is about
            q = prev.question
            question = Question([*(q.texts if q else []), text], [*(q.raws if q else []), raw], result)
            exchange = Exchange(prev.texts, prev.raws, prev.result, message.date, prev.token, question)
            variants = []  # see above: the record belongs to another preview
        else:  # no record yet: keep collecting the answers
            exchange = Exchange([*prev.texts, text], [*prev.raws, raw], result, message.date)
        _remember(user_id, exchange)
        if variants:  # an unclear message with variants: a tap makes it a record
            token = _new_token(LOOKUPS)
            lk = Lookup(user_id, result, exchange, variants, asked, text, prefix)
            LOOKUPS[token] = lk
            await message.answer(prefix + render_lookup(lk), reply_markup=lookup_keyboard(token, lk, fact=False))
        elif not offer:  # with an offer the reply has been sent above
            await message.answer(preview)
        return
    texts, raws = [text], [raw]
    fact = PendingFact(user_id, offer, raw, standalone=False) if offer else None
    if prev is not None:
        if prev.token is None:  # answers to the model's questions before any record
            texts, raws = [*prev.texts, text], [*prev.raws, raw]
        elif is_revision(prev.result, result):
            PENDING.pop(prev.token, None)  # replaced by this preview, see the module docstring
            LOOKUPS.pop(prev.token, None)
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
    if variants:
        if len(LOOKUPS) >= MAX_PENDING:
            LOOKUPS.pop(next(iter(LOOKUPS)))
        lk = Lookup(user_id, result, exchange, variants, asked, text, prefix, record=True)
        LOOKUPS[token] = lk
        preview = prefix + render_lookup(lk)
        markup = lookup_keyboard(token, lk, fact=fact is not None)
    else:
        markup = keyboard(token, record=True, fact=fact is not None)
    if fact is not None:
        preview += _offer_line(fact.text)
    await message.answer(preview, reply_markup=markup)


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
    lk = LOOKUPS.get(token)
    if lk is not None and lk.user_id == cb.from_user.id:
        LOOKUPS.pop(token, None)
        if CONTEXT.get(cb.from_user.id) is lk.exchange:  # an unclear message with variants: the dialog ends
            del CONTEXT[cb.from_user.id]
    if cb.message:
        await cb.message.edit_text("Отменено.")  # type: ignore[union-attr]
    await cb.answer()


@router.callback_query(F.data.startswith("save:"))
async def save(
    cb: CallbackQuery, settings: Settings, sessionmaker: Sessionmaker, llm: OpenRouterClient | None = None
) -> None:
    token = (cb.data or "").split(":", 1)[1]
    # pop, not get: updates run concurrently, a double tap must not save the sets twice.
    pending = PENDING.pop(token, None)
    if pending is None or pending.user_id != cb.from_user.id:
        if pending is not None:
            PENDING[token] = pending  # someone else's preview: leave it to its owner
        await cb.answer("Эта запись уже сохранена или устарела.", show_alert=True)
        return
    today = pending.sent_at.astimezone(ZoneInfo(settings.timezone)).date()
    try:
        note = await _save(pending, cb, today, sessionmaker)
    except Exception:
        PENDING[token] = pending  # let the user press again
        raise
    _forget(cb.from_user.id, token)
    LOOKUPS.pop(token, None)  # the variants are moot once the record is saved
    if cb.message:
        saved = pending.result.model_copy(update={"clarification": None})  # the question is moot now
        shown = re.sub(r"^Записать( еду| самочувствие)?\?\s*", "", render_preview(saved))
        offered = FACTS.get(token)  # not remembered yet: keep its button
        text = f"{shown}\n\n{note}".strip() + (_offer_line(offered.text) if offered else "")
        await cb.message.edit_text(  # type: ignore[union-attr]
            text, reply_markup=keyboard(token, record=False, fact=offered is not None, cancel=False)
        )
    await cb.answer()
    if pending.result.kind == "wellbeing" and cb.message:
        # On a training day the plan may change: send it right after "Самочувствие сохранено ✅".
        await send_after_wellbeing(cb.message, cb.from_user.id, settings, sessionmaker, llm)  # type: ignore[arg-type]


@router.callback_query(F.data.startswith("lookup:"))
async def pick(cb: CallbackQuery) -> None:
    """A variant for an unknown word: add it to the record and redraw the preview (still not saved)."""
    _, token, choice = ((cb.data or "") + "::").split(":")[:3]
    user_id = cb.from_user.id
    # pop, not get: a double tap must not add the variant twice.
    lk = LOOKUPS.pop(token, None)
    if lk is None or lk.user_id != user_id:
        if lk is not None:
            LOOKUPS[token] = lk  # someone else's preview: leave it to its owner
        await cb.answer(LOOKUP_STALE, show_alert=True)
        return
    if lk.record and token not in PENDING or not lk.record and CONTEXT.get(user_id) is not lk.exchange:
        await cb.answer(LOOKUP_STALE, show_alert=True)  # saved, cancelled or answered by a newer message
        return
    if choice == "other":
        LOOKUPS[token] = lk
        await cb.answer(OTHER_HINT, show_alert=True)
        return
    try:
        variant = lk.variants[int(choice)]
    except (ValueError, IndexError):
        LOOKUPS[token] = lk
        await cb.answer()
        return
    if variant.term in lk.resolved:
        LOOKUPS[token] = lk
        await cb.answer(f"Для «{variant.term}» вариант уже выбран.")
        return
    stem = food_lookup.stem(variant.term)
    # The model may have listed the word anyway (with made-up numbers): the variant replaces it.
    foods = [f for f in lk.result.foods if stem not in food_lookup.normalize(f.description)]
    resolved = lk.resolved | {variant.term}
    note = f"{variant.option.name}: {variant.option.note}" if variant.option.note else None
    result = lk.result.model_copy(
        update={
            "kind": "food",
            "foods": [*foods, variant.food],
            "unknown_terms": [t for t in lk.result.unknown_terms if t not in resolved],
            "clarification": _question(lk.asked),
            "note": "\n".join(n for n in (lk.result.note, note) if n) or None,
        }
    )
    old = PENDING.get(token) if lk.record else None
    raw_text, sent_at = (old.raw_text, old.sent_at) if old else (lk.exchange.raw_text, lk.exchange.at)
    if old is None and len(PENDING) >= MAX_PENDING:
        PENDING.pop(next(iter(PENDING)))
    PENDING[token] = Pending(user_id, result, raw_text, sent_at)
    # The dialog goes on from the picked record, like after a revision; a newer dialog is left alone.
    ex = CONTEXT.get(user_id)
    if ex is not None and (ex.token == token if lk.record else ex is lk.exchange):
        lk.exchange = Exchange(ex.texts, ex.raws, result, ex.at, token, ex.question if lk.record else None)
        _remember(user_id, lk.exchange)
    lk.result, lk.resolved, lk.record = result, resolved, True
    if any(v.term not in resolved for v in lk.variants):
        LOOKUPS[token] = lk
    offered = FACTS.get(token)
    if cb.message:
        text = lk.prefix + render_lookup(lk) + (_offer_line(offered.text) if offered else "")
        await cb.message.edit_text(  # type: ignore[union-attr]
            text, reply_markup=lookup_keyboard(token, lk, fact=offered is not None)
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
    if cb.message:  # the record's buttons (and open variants) stay while it is not saved
        lk = LOOKUPS.get(token)
        markup = (
            lookup_keyboard(token, lk, fact=False) if lk is not None
            else keyboard(token, record=token in PENDING, fact=False)
        )
        await cb.message.edit_reply_markup(reply_markup=markup)  # type: ignore[union-attr]
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
