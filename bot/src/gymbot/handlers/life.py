"""«Дела» from the chat: the schedule, plans by date, deadlines, tasks, house chores, projects and notes said in
words (text or voice) get a preview with «✅ Применить» (gymbot.services.life, gymbot.services.life_actions).
/life (and the old /hub) opens the Mini App on its «Дела» section.

Three ways in from handlers/log_text.process_text:
- `stage`: quick words (gymbot.services.life_gate) before the program edits; a message the model turned into
  no action and no question goes on as usual (food, a workout, a program edit);
- `handle_parsed`: the parser called the message kind="life": the model without the gate, and «Не понял, что
  сделать в делах» with an example when it finds nothing;
- `send_suggested`: the diary answer's last line «ДЕЛА: <команда>» (gymbot.services.answer), staged under it.

Notes the bot writes («составь памятку по epoll»): the model returns write_note {title, brief}; a second call
(gymbot.llm.prompts_note, purpose "advice") writes the Markdown and the preview shows its start with
«Сохранить». While that draft is open, an instruction («добавь пример», «короче») rewrites it (`on_text`, asked
first by process_text). edit_note of a saved note goes the same way with the note's text as the draft.

Only the owner (gymbot.services.access.owner_user). Nothing is written until the button: previews live in
memory (PENDING) for TTL and the first tap takes them, so a second tap or a restart answers STALE.
"""

from __future__ import annotations

import logging
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo

from gymbot.config import Settings
from gymbot.db.session import Sessionmaker
from gymbot.llm.openrouter import OpenRouterClient
from gymbot.llm.prompts_life import build_life_messages
from gymbot.llm.prompts_note import build_note_messages
from gymbot.services import life, saved_edits
from gymbot.services import life_actions as la
from gymbot.services.access import owner_user
from gymbot.services.chat_edit import is_edit_command
from gymbot.services.chat_settings import is_settings_request
from gymbot.services.life_gate import is_life_command

log = logging.getLogger(__name__)
router = Router(name="life")

STALE = "Устарело, повтори команду"
NOT_SET = "Мини-апп не настроен: нет адреса (MINIAPP_URL или PUBLIC_URL)."
OPEN = "Дела: расписание, планы, задачи, дом, проекты и заметки."
NOT_UNDERSTOOD = (
    "Не понял, что сделать в делах. Напиши, например: «в учёбу добавь завтра в 11 позаниматься HTTP-сервером "
    "на C», «добавь задачу купить билеты» или «вынес мусор»."
)
FAILED = "Не получилось разобрать, попробуй ещё раз чуть позже."
SAVE_FAILED = "Не получилось сохранить, попробуй ещё раз"
NOT_OWNER = "«Дела» ведёт только владелец бота."
SUGGESTED_FAILED = "Не получилось подготовить изменение в делах («{command}»), напиши его сам."
DRAFT_HINT = "Пока не сохранил, можно поправить словами: «добавь пример», «короче»."
TTL = timedelta(minutes=15)
MAX_PENDING = 100
PREVIEW_CHARS = 600  # of a note draft in the preview
WEEKDAY_NAMES = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class NoteDraft:
    """A note the bot wrote: new (write_note: `note_id` None) or a saved one rewritten (edit_note)."""

    title: str
    brief: str  # what the note is about: the model's brief, or the saved note's title
    body: str
    note_id: int | None = None
    project_id: int | None = None

    def act(self) -> la.Act:
        if self.note_id is not None:
            return la.Act("update", "note", self.note_id, {"body": self.body})
        fields: dict[str, Any] = {"title": self.title, "body": self.body, "author": "bot"}
        if self.project_id is not None:
            fields["project_id"] = self.project_id
        return la.Act("add", "note", None, fields)


@dataclass
class LifePending:
    user_id: int  # Telegram id
    text: str  # what the model saw (with the answers to its question)
    raw: str  # how the message is kept (voice: "[voice] …"), logged with the applied actions
    acts: list[la.Act]  # without write_note / edit_note: their result is `note_draft`
    lines: list[str]  # preview lines of `acts`
    notes: list[str]  # dropped actions
    sent_at: datetime
    question: dict | None = None  # the model's {"text", "options"} when it found nothing to do
    note_draft: NoteDraft | None = None

    def all_acts(self) -> list[la.Act]:
        return [*self.acts, self.note_draft.act()] if self.note_draft is not None else list(self.acts)

    def ready(self) -> bool:
        return bool(self.acts) or self.note_draft is not None


PENDING: dict[str, LifePending] = {}


# ---- the Mini App button ----


def life_url(settings: Settings, section: str = "life") -> str | None:
    base = settings.miniapp_url or settings.public_url
    if not base:
        return None
    return f"{base}{'&' if '?' in base else '?'}section={section}"


def open_kb(settings: Settings, section: str = "life") -> InlineKeyboardMarkup | None:
    """An inline web_app button (the Mini App gets Telegram initData) on the «Дела» section."""
    url = life_url(settings, section)
    if url is None:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Открыть дела", web_app=WebAppInfo(url=url))]])


@router.message(Command("life", "hub"))
async def cmd_life(message: Message, settings: Settings) -> None:
    kb = open_kb(settings)
    await message.answer(OPEN if kb else NOT_SET, reply_markup=kb)


# ---- staging ----


def local_day(at: datetime, settings: Settings) -> date:
    return at.astimezone(ZoneInfo(settings.timezone)).date()


def today_line(today: date) -> str:
    return f"Сегодня: {today.isoformat()}, {WEEKDAY_NAMES[today.weekday()]}"


async def _write(llm: OpenRouterClient, brief: str, draft: str | None, instruction: str | None) -> str:
    """The note's Markdown from the writing model, cut to what a note may hold; ValueError when empty."""
    messages = build_note_messages(brief, draft, instruction)
    body = (await llm.complete_text(messages, purpose="advice", keep_code=True)).strip()
    if not body:
        raise ValueError("empty note")
    return body[: la.BODY_MAX]


async def _draft(acts: list[la.Act], view: life.LifeView, llm: OpenRouterClient) -> NoteDraft | None:
    """The note of the first write_note / edit_note, written by the model; None when there is none."""
    for a in acts:
        if a.op == "write_note":
            body = await _write(llm, a.fields["brief"], None, None)
            return NoteDraft(a.fields["title"], a.fields["brief"], body, None, a.fields.get("project_id"))
        if a.op == "edit_note":
            row = next(n for n in view.notes if n.id == a.id)  # parse checked the id against the view
            body = await _write(llm, row.title, row.body or "(пусто)", a.fields["instruction"])
            return NoteDraft(row.title, row.title, body, row.id)
    return None


async def _stage(
    tg_id: int, at: datetime, text: str, raw: str, settings: Settings, sessionmaker: Sessionmaker,
    llm: OpenRouterClient,
) -> tuple[LifePending | None, str | None]:
    """The model's take on `text`: (the pending preview or question, None); (None, why) otherwise: None when it
    found nothing, the notes about the actions it dropped («Дата … дальше 400 дней»), NOT_OWNER for anyone but
    the owner (no model call), FAILED on any error."""
    try:
        today = local_day(at, settings)
        async with sessionmaker() as session:
            owner = await owner_user(session, settings)
            if owner is None or owner.telegram_id != tg_id:
                return None, NOT_OWNER
            view = await life.snapshot(session, owner.id, today)
        messages = build_life_messages(text, life.prompt_text(view), today_line(today))
        data = await llm.complete_json(messages, prefer="actions", purpose="json")
        parsed, notes, question = la.parse(data, view, today)
        acts = [a for a in parsed if a.op not in ("write_note", "edit_note")]
        draft = await _draft(parsed, view, llm)
        if len(parsed) - len(acts) > 1:
            notes.append("За раз пишу одну заметку, остальные попроси отдельно.")
        pending = LifePending(tg_id, text, raw, acts, la.describe(acts, view, today), notes, at, question, draft)
    except Exception as e:  # noqa: BLE001 - a failing «Дела» path must never cost the user a record
        log.warning("life command failed (%s)", type(e).__name__)
        return None, FAILED
    if not pending.ready() and pending.question is None:
        return None, "\n".join(pending.notes) or None
    return pending, None


async def stage(
    message: Message, text: str, raw: str, settings: Settings, sessionmaker: Sessionmaker, llm: OpenRouterClient,
    *, forced: bool = False,
) -> LifePending | None:
    """The «Дела» preview of `text` for the owner, when it has the gate's words (or `forced`: the parser said
    kind="life") and the model found an action or asks a question; None otherwise or on any error."""
    tg = message.from_user
    if tg is None or not (forced or is_life_command(text)):
        return None
    pending, _ = await _stage(tg.id, message.date, text, raw, settings, sessionmaker, llm)
    return pending


async def handle_parsed(
    message: Message, text: str, raw: str, settings: Settings, sessionmaker: Sessionmaker, llm: OpenRouterClient,
    *, prefix: str = "",
) -> None:
    """The parser called the message kind="life": the preview, or why there is none."""
    tg = message.from_user
    assert tg is not None
    pending, why = await _stage(tg.id, message.date, text, raw, settings, sessionmaker, llm)
    if pending is None:
        await message.answer(prefix + (why or NOT_UNDERSTOOD))
        return
    await send(message, pending, prefix=prefix)


async def send_suggested(
    message: Message, command: str, raw: str, settings: Settings, sessionmaker: Sessionmaker, llm: OpenRouterClient
) -> None:
    """The preview of «ДЕЛА: <команда>» from the diary answer, sent under that answer. `message` is the user's
    message the answer replied to; `raw` its stored text."""
    tg = message.from_user
    assert tg is not None
    pending, why = await _stage(tg.id, message.date, command, raw, settings, sessionmaker, llm)
    if pending is None:
        if why != NOT_OWNER:  # not the owner: the answer alone, the suggestion was not for them
            await message.answer(SUGGESTED_FAILED.format(command=command) if why in (None, FAILED) else why)
        return
    log.info("«Дела» change suggested by the diary answer: %r", command)
    await send(message, pending)


# ---- the preview ----


def _bullets(lines: list[str]) -> str:
    return "\n".join(f"• {line}" for line in lines)


def _draft_text(draft: NoteDraft) -> str:
    body = draft.body if len(draft.body) <= PREVIEW_CHARS else draft.body[: PREVIEW_CHARS - 1].rstrip() + "…"
    head = f"Заметка «{draft.title}» после правки:" if draft.note_id is not None else f"Памятка: «{draft.title}»"
    return f"{head}\n\n{body}\n\n{DRAFT_HINT}"


def render(pending: LifePending) -> str:
    parts: list[str] = []
    if pending.lines:
        parts.append(pending.lines[0] if len(pending.lines) == 1 else "В дела:\n" + _bullets(pending.lines))
    if pending.note_draft is not None:
        parts.append(_draft_text(pending.note_draft))
    if not pending.ready() and pending.question is not None:
        parts.append(pending.question["text"])
    if pending.notes:
        parts.append("\n".join(pending.notes))
    return "\n\n".join(parts)


def keyboard(token: str, pending: LifePending) -> InlineKeyboardMarkup:
    drop = InlineKeyboardButton(text="Отмена", callback_data=f"ldrop:{token}")
    if not pending.ready():  # the model's question: its options
        options = pending.question["options"] if pending.question else []
        buttons = [InlineKeyboardButton(text=o, callback_data=f"lq:{token}:{n}") for n, o in enumerate(options)]
        rows = [buttons[k : k + 2] for k in range(0, len(buttons), 2)]
        return InlineKeyboardMarkup(inline_keyboard=[*rows, [drop]])
    label = "Сохранить" if pending.note_draft is not None and not pending.acts else "✅ Применить"
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=label, callback_data=f"lapply:{token}"), drop]]
    )


def _store(pending: LifePending) -> str:
    now = utcnow()
    for t in [t for t, p in PENDING.items() if now - p.sent_at > TTL]:
        PENDING.pop(t, None)
    while len(PENDING) >= MAX_PENDING:
        PENDING.pop(next(iter(PENDING)))
    if pending.note_draft is not None:  # one open draft per user: the newest is the one words rewrite
        for t in [t for t, p in PENDING.items() if p.user_id == pending.user_id and p.note_draft is not None]:
            PENDING.pop(t, None)
    token = secrets.token_urlsafe(8)
    PENDING[token] = pending
    return token


async def send(message: Message, pending: LifePending, *, prefix: str = "") -> None:
    """The preview with «✅ Применить» («Сохранить» for a note alone), or the model's question with its options."""
    token = _store(pending)
    await message.answer(prefix + render(pending), reply_markup=keyboard(token, pending))


# ---- the open note draft rewritten by words ----

_REWRITE = re.compile(
    r"^[\s,.!]*(?:(?:а|и|ну|давай|можешь|пожалуйста|теперь|ещё|еще)[\s,]+)*"
    r"(?:добав|допиш|убер|удали|сократ|короч|покороч|подробн|длинн|расширь|расшир|упрост|проще|перепиш|передела|"
    r"исправ|замен|помен|сделай|оставь|без\s|больше|меньше|раскро|поясн|объясн|привед|пример|переведи|распиши|"
    r"уточни|разбей|структур)",
    re.IGNORECASE,
)
_NOTE_WORDS = re.compile(r"заметк|памятк|конспект|шпаргалк|черновик|текст", re.IGNORECASE)
_AMOUNT = re.compile(r"\d+\s*(?:г|гр|грамм\w*|кг|шт\w*|ккал|мл|раз|подход\w*)(?![а-яё])", re.IGNORECASE)
# Gym words: «замени жим на гантели» is about the program, whatever the edit gate thinks of it.
_GYM = re.compile(
    r"жим|присед|тяг[аиуе]|подтяг|отжим|сгибан|разгибан|выпад|упражнен|трениров|программ|подход|гантел|штанг|"
    r"тренаж|(?<![а-яё])зал(?![а-яё])|(?<![а-яё])(?:день|дня|дне)\s+(?:рук|ног|спин|груд|плеч)",
    re.IGNORECASE,
)


def is_rewrite(text: str, today: date) -> bool:
    """«добавь пример», «короче», «убери про select»: an instruction for the open draft. Not a record with
    amounts, and, unless it names the note, not a command of another path: «Дела» («добавь задачу …»), a
    program edit or gym words («убери французский жим из дня рук», «замени жим на гантели»), a settings
    request, a saved-record edit («удали самсу»)."""
    if len(text) > 300 or _AMOUNT.search(text) or not _REWRITE.search(text):
        return False
    if _NOTE_WORDS.search(text):
        return True
    return not (
        _GYM.search(text) or is_life_command(text) or is_edit_command(text) or is_settings_request(text)
        or saved_edits.detect(text, today) is not None
    )


def open_draft(tg_id: int, now: datetime) -> tuple[str, LifePending] | None:
    """The user's open note draft (the newest one within TTL)."""
    found = [(t, p) for t, p in PENDING.items()
             if p.user_id == tg_id and p.note_draft is not None and now - p.sent_at <= TTL]
    return max(found, key=lambda tp: tp[1].sent_at) if found else None


async def on_text(
    message: Message, text: str, llm: OpenRouterClient, *, dialog_at: datetime | None = None, prefix: str = ""
) -> bool:
    """Rewrite the open note draft by `text` when it reads as an instruction for it and the draft is newer than
    the parser's open dialog (`dialog_at`); True when the message was used here."""
    tg = message.from_user
    if tg is None:
        return False
    found = open_draft(tg.id, message.date)
    if found is None or (dialog_at is not None and dialog_at > found[1].sent_at):
        return False
    if not is_rewrite(text, message.date.date()):  # the UTC date: only for the saved-edit words' day check
        return False
    token, pending = found
    draft = pending.note_draft
    assert draft is not None
    try:
        body = await _write(llm, draft.brief, draft.body, text)
    except Exception as e:  # noqa: BLE001 - the draft stays as it was, the user is told
        log.warning("note rewrite failed (%s)", type(e).__name__)
        await message.answer(prefix + FAILED)
        return True
    PENDING.pop(token, None)  # the old preview's buttons stop working
    pending.note_draft = NoteDraft(draft.title, draft.brief, body, draft.note_id, draft.project_id)
    pending.sent_at = message.date
    await send(message, pending, prefix=prefix)
    return True


# ---- buttons ----


def _token(cb: CallbackQuery) -> str:
    parts = (cb.data or "").split(":")
    return parts[1] if len(parts) > 1 else ""


def _take(cb: CallbackQuery) -> LifePending | None:
    """Pop the caller's own live preview: the first tap takes it, a second one finds nothing."""
    token = _token(cb)
    pending = PENDING.get(token)
    if pending is None or pending.user_id != cb.from_user.id:
        return None
    PENDING.pop(token, None)
    return pending if utcnow() - pending.sent_at <= TTL else None


@router.callback_query(F.data.startswith("lapply:"))
async def on_apply(cb: CallbackQuery, settings: Settings, sessionmaker: Sessionmaker) -> None:
    pending = _take(cb)
    if pending is None or not pending.ready():
        await cb.answer(STALE, show_alert=True)
        return
    now = utcnow().replace(microsecond=0)
    async with sessionmaker() as session:
        owner = await owner_user(session, settings)
        if owner is None or owner.telegram_id != pending.user_id:
            await cb.answer(STALE, show_alert=True)
            return
        try:
            done = await life.apply(session, owner, pending.all_acts(), now, today=local_day(now, settings))
        except Exception as e:  # noqa: BLE001 - the tap gets an answer; the preview stays taken (no double write)
            log.warning("«Дела» apply failed (%s)", type(e).__name__)
            await cb.answer(SAVE_FAILED, show_alert=True)
            return
    log.info("«Дела» from the chat %r: %s", pending.raw, [(a.op, a.entity) for a in pending.all_acts()])
    await cb.answer("Готово")
    if cb.message:
        text = "Готово:\n" + _bullets(done) if done else "Менять нечего."
        await cb.message.edit_text(text, reply_markup=open_kb(settings))  # type: ignore[union-attr]


@router.callback_query(F.data.startswith("lq:"))
async def on_option(cb: CallbackQuery, settings: Settings, sessionmaker: Sessionmaker, llm: OpenRouterClient) -> None:
    """An option of the model's question: ask it again with the answer."""
    pending = _take(cb)
    try:
        n = int((cb.data or "").split(":")[2])
    except (IndexError, ValueError):
        n = -1
    options = pending.question["options"] if pending is not None and pending.question else []
    if pending is None or not 0 <= n < len(options):
        await cb.answer(STALE, show_alert=True)
        return
    await cb.answer()  # the model takes a moment; the button stops spinning now
    text = f"{pending.text}\nУточнение: {options[n]}"
    again, why = await _stage(pending.user_id, utcnow(), text, pending.raw, settings, sessionmaker, llm)
    if cb.message is None:
        return
    if again is None:
        await cb.message.edit_text(why or NOT_UNDERSTOOD)  # type: ignore[union-attr]
        return
    token = _store(again)
    await cb.message.edit_text(render(again), reply_markup=keyboard(token, again))  # type: ignore[union-attr]


@router.callback_query(F.data.startswith("ldrop:"))
async def on_drop(cb: CallbackQuery) -> None:
    token = _token(cb)
    pending = PENDING.get(token)
    if pending is not None and pending.user_id == cb.from_user.id:
        PENDING.pop(token, None)
    await cb.answer()
    if cb.message:
        await cb.message.edit_text("Отменено.")  # type: ignore[union-attr]
