"""Mini App settings from the chat with a "✅ Применить" preview (gymbot.services.chat_settings).

Settings never consume a message. process_text (handlers/log_text.py) asks `stage_settings` first and then
runs the parser on the same text as always:
- the parser found a record: its preview as usual, and the settings preview as a second message when it
  has changes (unless the record is only the command's own weights, Staged.fake_workout);
- no record (question, small talk, unclear): the settings preview or notes instead of the parser's reply;
- nothing staged (not a command, no action, any error): the parser's path untouched.
Nothing is written until "✅ Применить"; previews live in memory (SETTINGS, by token) for TTL; a restart,
an older preview or a second tap answers STALE.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from gymbot.config import Settings
from gymbot.db.session import Sessionmaker
from gymbot.handlers.plan import open_diary_kb
from gymbot.llm.openrouter import OpenRouterClient
from gymbot.llm.prompts import build_settings_messages
from gymbot.llm.schemas import ParseResult
from gymbot.services import chat_settings as cs
from gymbot.services import overrides
from gymbot.services.programs import normalize
from gymbot.services.users import get_or_create_user

log = logging.getLogger(__name__)
router = Router(name="chat_settings")

STALE = "Устарело, повтори команду"
MAX_PENDING = 200
TTL = timedelta(minutes=15)  # like the parser's dialog context (log_text.CONTEXT_TTL)


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class SettingsPending:
    user_id: int  # Telegram id
    plan: cs.Plan
    sent_at: datetime


SETTINGS: dict[str, SettingsPending] = {}


def keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="✅ Применить", callback_data=f"sapply:{token}"),
            InlineKeyboardButton(text="Отмена", callback_data=f"sdrop:{token}"),
        ]]
    )


def _bullets(lines: list[str]) -> str:
    return "\n".join(f"• {line}" for line in lines)


def render(plan: cs.Plan) -> str:
    notes = "\n".join(plan.notes)
    if plan.empty():
        return notes
    return "Применить?\n" + _bullets(plan.lines) + (f"\n\n{notes}" if notes else "")


@dataclass
class Staged:
    """What the settings model made of a message; nothing is sent or stored until `send_staged`."""

    plan: cs.Plan | None  # None: only rejected values (notes)
    notes: list[str]
    catalog: list[str]

    def ready(self) -> bool:
        """A preview with changes to apply."""
        return self.plan is not None and not self.plan.empty()

    def fake_workout(self, result: ParseResult, text: str) -> bool:
        """Whether the parser's workout only repeats the weights for today ("поставь сегодня жим 85" parsed
        as a set of жим 85): every exercise is a weight of this command with the same kg, and the text has
        no sign of reps (DROP_REPS). Anything else keeps the workout preview."""
        weights = self.plan.said_weights if self.plan is not None else {}
        if result.kind != "workout" or not result.exercises or not weights:
            return False
        if cs.DROP_REPS.search(normalize(text)):
            return False
        for ex in result.exercises:
            kg = weights.get(overrides.match(ex.exercise, ex.exercise, self.catalog) or "")
            if kg is None or any(st.weight_kg is None or abs(st.weight_kg - kg) > 0.01 for st in ex.sets):
                return False
        return True


async def stage_settings(
    message: Message, text: str, settings: Settings, sessionmaker: Sessionmaker, llm: OpenRouterClient
) -> Staged | None:
    """The settings model's take on `text` when it looks like a settings command; None when it does not,
    when the model found nothing, or on any error. Never replies and never decides the message's fate:
    process_text runs the parser on the same text anyway (see handlers/log_text.py)."""
    if not cs.is_settings_request(text):
        return None
    tg = message.from_user
    assert tg is not None
    today = message.date.astimezone(ZoneInfo(settings.timezone)).date()
    try:
        async with sessionmaker() as session:
            user = await get_or_create_user(session, tg.id, tg.full_name)
            snap = await cs.load_snapshot(session, user, today)
            context = cs.prompt_context(snap)
            await session.commit()  # no write lock while the model thinks
            await message.bot.send_chat_action(message.chat.id, "typing")  # type: ignore[union-attr]
            data = await llm.complete_json(build_settings_messages(text, context), prefer="actions")
            actions, invalid = cs.parse_actions(data)
            if cs.REPS.search(normalize(text)):  # "жим 80 8 раз": sets done, not a weight for today
                actions = [a for a in actions if not isinstance(a, cs.WeightAction)]
            plan = await cs.resolve(session, snap, actions) if actions else None
    except Exception as e:  # noqa: BLE001 - a failing settings path must never cost the user a record
        log.warning("settings command failed (%s), the parser only", type(e).__name__)
        return None
    if plan is not None:
        plan.notes = [*invalid, *plan.notes]
    if plan is None and not invalid:
        return None
    return Staged(plan, invalid if plan is None else plan.notes, snap.catalog)


async def send_staged(message: Message, staged: Staged, *, prefix: str = "") -> None:
    """The settings preview with "✅ Применить" (or just the notes when there is nothing to apply)."""
    if not staged.ready():
        await message.answer(prefix + ("\n".join(staged.notes) or "Менять нечего."))
        return
    assert staged.plan is not None
    if len(SETTINGS) >= MAX_PENDING:
        SETTINGS.pop(next(iter(SETTINGS)))
    token = secrets.token_hex(6)
    SETTINGS[token] = SettingsPending(message.from_user.id, staged.plan, message.date)  # type: ignore[union-attr]
    await message.answer(prefix + render(staged.plan), reply_markup=keyboard(token))


def _take(cb: CallbackQuery) -> SettingsPending | None:
    """Pop the caller's own preview (pop: a double tap must not apply twice); someone else's stays."""
    token = (cb.data or "").split(":", 1)[1]
    pending = SETTINGS.pop(token, None)
    if pending is not None and pending.user_id != cb.from_user.id:
        SETTINGS[token] = pending
        return None
    if pending is not None and utcnow() - pending.sent_at > TTL:
        return None  # an old preview: the state it showed may have changed
    return pending


@router.callback_query(F.data.startswith("sapply:"))
async def apply_settings(cb: CallbackQuery, settings: Settings, sessionmaker: Sessionmaker) -> None:
    pending = _take(cb)
    if pending is None:
        await cb.answer(STALE, show_alert=True)
        return
    token = (cb.data or "").split(":", 1)[1]
    try:
        async with sessionmaker() as session:
            user = await get_or_create_user(session, cb.from_user.id, cb.from_user.full_name)
            notes = await cs.apply(session, user, pending.plan, ZoneInfo(settings.timezone), utcnow())
            await session.commit()
    except Exception:
        SETTINGS[token] = pending  # let the user press again
        raise
    done = "Готово ✅\n" + _bullets(pending.plan.lines) + ("\n\n" + "\n".join(notes) if notes else "")
    if cb.message:
        markup = open_diary_kb(settings) if pending.plan.for_miniapp() else None
        await cb.message.edit_text(done, reply_markup=markup)  # type: ignore[union-attr]
    await cb.answer()


@router.callback_query(F.data.startswith("sdrop:"))
async def drop_settings(cb: CallbackQuery) -> None:
    if _take(cb) is None:
        await cb.answer(STALE, show_alert=True)
        return
    if cb.message:
        await cb.message.edit_text("Отменено.")  # type: ignore[union-attr]
    await cb.answer()
