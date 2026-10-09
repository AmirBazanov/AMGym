"""Program edits from the chat with a "✅ Применить" preview (gymbot.services.chat_edit).

process_text (handlers/log_text.py) asks `stage_edit` before saved edits, settings and the parser. A command
the model turned into at least one valid action is answered here and nowhere else (the preview, a clarifying
question with buttons, or notes); anything else (not a command, no valid action, any error) returns None and
the text goes on as usual.

Nothing is written until "✅ Применить": it runs program_editor.edit_program with the version the preview was
built against (a change in between, from the Mini App or another command, answers CHANGED), then the weights,
the day adjustments ("сегодня −20 %", gymbot.services.day_adjustments) and the deload, and publishes "program", "plan", "state" (only what changed) so the open Mini App reloads. Clarify buttons fix the day or the
exercise and rebuild the preview without the model; the model's own question is answered by asking it again
with the chosen option. Previews live in memory (EDITS, by token) for TTL; a restart, an older preview or a
second tap answers STALE.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.exc import IntegrityError

from gymbot.config import Settings
from gymbot.db.session import Sessionmaker
from gymbot.handlers.plan import open_diary_kb
from gymbot.llm.openrouter import OpenRouterClient
from gymbot.llm.prompts_edit import build_edit_messages
from gymbot.services import chat_edit as ce
from gymbot.services import live
from gymbot.services import program_editor as pe
from gymbot.services import saved_edits as se
from gymbot.services.tg_html import bold_head, escape, send_html
from gymbot.services.users import active_program, get_or_create_user

log = logging.getLogger(__name__)
router = Router(name="chat_edit")

STALE = "Устарело, повтори команду"
CHANGED = "Программа изменилась (в мини-аппе или другой командой), повтори команду."
MAX_PENDING = 200
TTL = timedelta(minutes=15)


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class EditPending:
    user_id: int  # Telegram id
    text: str  # what the model saw
    raw: str  # how the message is kept (voice: "[voice] …"), logged with the applied ops
    actions: list[Any]
    sent_at: datetime
    picks: dict[tuple[int, str], Any] = field(default_factory=dict)
    plan: ce.EditPlan | None = None
    notes: list[str] = field(default_factory=list)  # rejected actions (parse_actions)


EDITS: dict[str, EditPending] = {}


def _local_day(at: datetime, settings: Settings) -> date:
    return at.astimezone(ZoneInfo(settings.timezone)).date()


def _bullets(lines: list[str]) -> str:
    return "\n".join(f"• {line}" for line in lines)


def render(plan: ce.EditPlan, notes: list[str]) -> str:
    """Plain text of the preview, a clarifying question, or only notes; the first line is the heading."""
    all_notes = [*notes, *plan.notes, *plan.move_notes]
    if plan.clarify is not None:
        return plan.clarify.question
    if not plan.ready():
        return "\n".join(all_notes) or "Менять нечего."
    lines = list(plan.lines)
    if plan.ops and plan.forks:
        name = plan.copy_name or f"{plan.name}{pe.COPY_MARK}"
        lines.insert(0, f"Создам твою копию «{name}», оригинал останется.")
    tail = "\n\n" + "\n".join(all_notes) if all_notes else ""
    return "Что изменю:\n" + _bullets(lines) + tail


def apply_kb(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="✅ Применить", callback_data=f"eapply:{token}"),
            InlineKeyboardButton(text="Отмена", callback_data=f"edrop:{token}"),
        ]]
    )


def clarify_kb(token: str, options: list[str]) -> InlineKeyboardMarkup:
    buttons = [InlineKeyboardButton(text=o, callback_data=f"eclar:{token}:{n}") for n, o in enumerate(options)]
    rows = [buttons[k : k + 2] for k in range(0, len(buttons), 2)]
    rows.append([InlineKeyboardButton(text="Отмена", callback_data=f"edrop:{token}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _markup(token: str, plan: ce.EditPlan) -> InlineKeyboardMarkup | None:
    if plan.clarify is not None:
        return clarify_kb(token, plan.clarify.options)
    return apply_kb(token) if plan.ready() else None


async def _build(pending: EditPending, today: date, sessionmaker: Sessionmaker) -> ce.EditPlan:
    """Compile the pending actions against the program as stored now and dry-run them for the preview."""
    async with sessionmaker() as session:
        user = await get_or_create_user(session, pending.user_id)
        snap = await ce.load_snapshot(session, user, today)
        plan = ce.compile_ops(snap, pending.actions, pending.picks)
        if plan.clarify is not None or not plan.ops:
            await session.commit()  # active_program may have created the first choice
            return plan
        up = await active_program(session, user, today)
        try:
            await ce.preview(session, user, up, plan)  # rolled back, whatever happens
        except pe.EditError as e:
            plan.drop_ops(f"Не получится: {e}")
        except pe.Conflict:
            plan.drop_ops(CHANGED)
    return plan


async def _ask_model(text: str, today: date, tg_id: int, sessionmaker: Sessionmaker, llm: OpenRouterClient) -> Any:
    async with sessionmaker() as session:
        user = await get_or_create_user(session, tg_id)
        snap = await ce.load_snapshot(session, user, today)
        context = ce.prompt_context(snap)
        await session.commit()  # no write lock while the model thinks
    return await llm.complete_json(build_edit_messages(text, context), prefer="actions", purpose="edit")


async def stage_edit(
    message: Message, text: str, raw: str, settings: Settings, sessionmaker: Sessionmaker, llm: OpenRouterClient
) -> EditPending | None:
    """The edit model's take on `text` when it looks like a program edit and the model found at least one
    valid action; None otherwise or on any error (the caller goes on with the text as usual)."""
    if not ce.is_edit_command(text):
        return None
    tg = message.from_user
    assert tg is not None
    today = _local_day(message.date, settings)
    try:
        data = await _ask_model(text, today, tg.id, sessionmaker, llm)
        actions, invalid = ce.parse_actions(data)
        if not actions:
            return None
        pending = EditPending(tg.id, text, raw, actions, message.date, notes=invalid)
        pending.plan = await _build(pending, today, sessionmaker)
    except Exception as e:  # noqa: BLE001 - a failing edit path must never cost the user a record
        log.warning("program edit command failed (%s), the usual path", type(e).__name__)
        return None
    plan = pending.plan
    if ce.only_guesses(plan, actions, text):
        return None  # "дышится легче" read as a default light day, "верни как было" with nothing to clear
    if plan.clarify is None and not plan.ready() and se.detect(text, today) is not None:
        # Nothing to apply ("удали ужин в понедельник": no «ужин» in Monday's program) and the text reads as a
        # saved-record edit: that path gets it, the notes would only hide it.
        return None
    return pending


def _store(pending: EditPending) -> str:
    if len(EDITS) >= MAX_PENDING:
        EDITS.pop(next(iter(EDITS)))
    token = secrets.token_hex(6)
    EDITS[token] = pending
    return token


async def send_edit(message: Message, pending: EditPending, *, prefix: str = "") -> None:
    """The preview with "✅ Применить", a clarifying question with buttons, or notes."""
    plan = pending.plan
    assert plan is not None
    body = render(plan, pending.notes)
    markup = None
    if plan.clarify is not None or plan.ready():
        markup = _markup(_store(pending), plan)
    await send_html(message.answer, escape(prefix) + bold_head(body), prefix + body, reply_markup=markup)


def _token(cb: CallbackQuery) -> str:
    return (cb.data or "").split(":")[1] if ":" in (cb.data or "") else ""


def _own(cb: CallbackQuery, token: str) -> EditPending | None:
    """The caller's own live preview (left in EDITS); someone else's or an old one is None."""
    pending = EDITS.get(token)
    if pending is None or pending.user_id != cb.from_user.id:
        return None
    if utcnow() - pending.sent_at > TTL:
        EDITS.pop(token, None)
        return None
    return pending


def _take(cb: CallbackQuery) -> EditPending | None:
    """Pop the caller's own preview (a double tap must not apply twice); someone else's stays."""
    token = _token(cb)
    pending = _own(cb, token)
    if pending is not None:
        EDITS.pop(token, None)
    return pending


async def _edit(cb: CallbackQuery, body: str, markup: InlineKeyboardMarkup | None = None) -> None:
    if cb.message:
        await send_html(cb.message.edit_text, bold_head(body), body, reply_markup=markup)  # type: ignore[union-attr]


@router.callback_query(F.data.startswith("eapply:"))
async def apply_edit(cb: CallbackQuery, settings: Settings, sessionmaker: Sessionmaker) -> None:
    pending = _take(cb)
    if pending is None or pending.plan is None or not pending.plan.ready():
        await cb.answer(STALE, show_alert=True)
        return
    plan = pending.plan
    now = utcnow()
    today = _local_day(now, settings)
    try:
        async with sessionmaker() as session:
            user = await get_or_create_user(session, cb.from_user.id, cb.from_user.full_name)
            user_id = user.id
            up = await active_program(session, user, today)
            notes = await ce.apply(session, user, up, plan, today, pending.raw, now)
            await session.commit()
    except (pe.Conflict, IntegrityError):  # IntegrityError: the day's adjustment was stored meanwhile
        await _edit(cb, CHANGED)
        await cb.answer()
        return
    except pe.EditError as e:
        await _edit(cb, f"Не получилось: {e}")
        await cb.answer()
        return
    except Exception:
        EDITS[_token(cb)] = pending  # let the user press again
        raise
    live.publish(user_id, *plan.live_topics())
    done = "Готово:\n" + _bullets(plan.lines) + ("\n\n" + "\n".join(notes) if notes else "")
    await _edit(cb, done, open_diary_kb(settings))
    await cb.answer()


@router.callback_query(F.data.startswith("eclar:"))
async def clarify(cb: CallbackQuery, settings: Settings, sessionmaker: Sessionmaker, llm: OpenRouterClient) -> None:
    token = _token(cb)
    pending = _own(cb, token)
    plan = pending.plan if pending is not None else None
    try:
        n = int((cb.data or "").split(":")[2])
    except (IndexError, ValueError):
        n = -1
    if pending is None or plan is None or plan.clarify is None or not 0 <= n < len(plan.clarify.options):
        await cb.answer(STALE, show_alert=True)
        return
    EDITS.pop(token, None)  # a second tap while this one works answers STALE
    await cb.answer()  # the model or the dry run takes a moment; the button stops spinning now
    pick = plan.clarify.picks[n]
    today = _local_day(utcnow(), settings)
    try:
        if pick is not None:
            pending.picks[(pick.action, pick.what)] = pick.value
        else:  # the model's own question: ask it again with the answer
            pending.text = f"{pending.text}\nУточнение: {plan.clarify.options[n]}"
            actions, invalid = ce.parse_actions(
                await _ask_model(pending.text, today, pending.user_id, sessionmaker, llm)
            )
            pending.actions, pending.picks, pending.notes = actions, {}, invalid
            if not actions:
                await _edit(cb, "\n".join(invalid) or "Не понял, что поменять. Напиши команду целиком.")
                return
        pending.plan = await _build(pending, today, sessionmaker)
    except Exception as e:  # noqa: BLE001 - the user gets an answer, not a spinning button
        log.warning("program edit clarify failed (%s)", type(e).__name__)
        await _edit(cb, "Не получилось, повтори команду.")
        return
    pending.sent_at = utcnow()
    if pending.plan.clarify is not None or pending.plan.ready():
        EDITS[token] = pending
    await _edit(cb, render(pending.plan, pending.notes), _markup(token, pending.plan))


@router.callback_query(F.data.startswith("edrop:"))
async def drop_edit(cb: CallbackQuery) -> None:
    if _take(cb) is None:
        await cb.answer(STALE, show_alert=True)
        return
    await _edit(cb, "Отменено.")
    await cb.answer()
