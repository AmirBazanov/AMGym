"""Answers to questions in the chat ("с каким весом жать?", "сколько сегодня тоннаж?") from the user's diary.

The parser answers a question in one short line without any data. For kind="question" the chat handler
asks again here (`respond`), in three layers against made-up numbers:

1. A factual question (gymbot.services.answer_intent.classify: the day's workout and tonnage, food and
   KBJU left, an exercise's last time and record) is answered in code from the database
   (gymbot.services.answer_direct), no model at all.
2. Anything else goes to the model with the advice summary (gymbot.services.advice: profile, facts, food,
   training with the last sets and 1RM, wellbeing, program), the day's workout, food and per-exercise
   records counted here, today's adjusted plan (gymbot.services.plan), the weights set for today from the
   chat (gymbot.services.overrides), the workout in progress in the Mini App (gymbot.services.active_workout)
   and the last questions and answers of the dialog. Its answer is checked in code
   (gymbot.services.answer_check): a claim about the past that the summary does not hold gets one
   regeneration with a correction, then an honest fallback that shows the counted block.
3. The model answers at ANSWER_TEMPERATURE, and the prompt says to admit what the diary lacks.

Only reads, except that the plan service stores today's plan and the user row is created on first use.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from gymbot.config import Settings
from gymbot.db.models import User
from gymbot.db.session import Sessionmaker
from gymbot.llm.openrouter import LLMError, OpenRouterClient
from gymbot.llm.prompts import build_answer_messages
from gymbot.services import active_workout, advice, nutrition, overrides, plan
from gymbot.services import answer_direct as direct
from gymbot.services.answer_check import Evidence, correction, violations
from gymbot.services.answer_intent import classify, mentions
from gymbot.services.programs import normalize
from gymbot.services.users import active_program, get_or_create_user

log = logging.getLogger(__name__)

ANSWER_MAX = 1500  # characters sent to the chat; the prompt asks for far less
NO_TRAINING = "Сегодня тренировки по программе нет."
DONE_MAX = 900  # characters of the "done on the last training day" block
ANSWER_TEMPERATURE = 0.2  # low: the answer retells the summary, it does not invent


async def done_block(session: AsyncSession, user_id: int, today: date) -> str:
    """What the history holds for the last training day up to today, counted here (sets, tonnage per
    exercise), so the model never has to add numbers up or guess them from the 14-day summary."""
    day = await direct.last_training_day(session, user_id, today)
    if day is None:
        return "В истории тренировок пока нет."
    by_ex = await direct.day_sets(session, user_id, day)
    total = sum((direct.tonnage(sets) for sets in by_ex.values()), Decimal(0))
    label = "сегодня" if day == today else f"{day:%d.%m} (последняя тренировка)"
    head = (
        f"Сделано {label}, из истории: {len(by_ex)} упр., {sum(len(v) for v in by_ex.values())} подх., "
        f"тоннаж {direct.kg(total)} кг."
    )
    text = "\n".join([head, *direct.day_lines(by_ex)])
    return text if len(text) <= DONE_MAX else text[: DONE_MAX - 1] + "…"


@dataclass
class Context:
    """The model's summary (`text`) and what the answer check and the fallback need from it."""

    text: str
    done: str
    food: str
    history: dict[str, direct.ExerciseHistory]
    in_progress: list[str] = field(default_factory=list)  # exercise names of the workout in progress
    catalog: list[str] = field(default_factory=list)
    aliases: dict[str, list[str]] = field(default_factory=dict)

    def evidence(self, question: str, dialog: list[tuple[str, str]]) -> Evidence:
        """Numbers: the summary plus what the user said (never the bot's earlier answers: they may hold the
        very claim being checked). Exercises: only the logged ones and the workout in progress; a name the
        user asked about is no proof that it was done ("жим лёжа на прошлой неделе?")."""
        said = "\n".join([question, *(q for q, _ in dialog)])
        names = [*self.catalog, *self.history]
        return Evidence(f"{self.text}\n{said}", {*self.history, *self.in_progress}, names, self.aliases)


async def gather(
    session: AsyncSession,
    user: User,
    settings: Settings,
    llm: OpenRouterClient | None,
    tz: ZoneInfo,
    now_utc: datetime,
) -> Context:
    """The advice summary, the counted blocks and today's plan. Starts the default program like /plan;
    commits."""
    today_local = now_utc.astimezone(tz).date()
    await active_program(session, user, today_local)
    await session.commit()
    summary = await advice.build_context(session, user, settings, tz, now_utc)
    built = await plan.get_or_build(session, user, settings, llm, tz, now_utc)
    today = plan.plan_text(built) if built is not None else NO_TRAINING
    weights = overrides.context_line(await overrides.for_day(session, user.id, today_local))
    in_progress = await active_workout.context_for(session, user.id, now_utc, tz)
    done = await done_block(session, user.id, today_local)
    food = direct.food_block(await nutrition.day_summary(session, user, today_local, tz))
    history = await direct.exercise_history(session, user.id, today_local)
    catalog, aliases = await direct.known_exercises(session)
    tail = "".join(f"\n{line}" for line in (weights, in_progress) if line)
    text = (
        f"{summary}\n{done}\n{food}\n{direct.records_block(history)}\n"
        f"План на сегодня ({now_utc.astimezone(tz):%d.%m}):\n{today}{tail}"
    )
    running = mentions(in_progress, catalog, aliases) if in_progress else []
    return Context(text, done, food, history, running, catalog, aliases)


async def build_context(
    session: AsyncSession,
    user: User,
    settings: Settings,
    llm: OpenRouterClient | None,
    tz: ZoneInfo,
    now_utc: datetime,
) -> str:
    """The model's summary text (see `gather`). Starts the default program like /plan; commits."""
    return (await gather(session, user, settings, llm, tz, now_utc)).text


def _cut(text: str) -> str:
    return text if len(text) <= ANSWER_MAX else text[: ANSWER_MAX - 1].rstrip() + "…"


async def answer(
    llm: OpenRouterClient, context: str, question: str, dialog: list[tuple[str, str]] | None = None
) -> str:
    """The model's answer for the chat, unchecked; `dialog` is (question, answer) pairs, oldest first.
    Raises LLMError."""
    messages = build_answer_messages(context, question, dialog or [])
    return _cut(await llm.complete_text(messages, temperature=ANSWER_TEMPERATURE))


@dataclass
class Reply:
    text: str
    layer: str  # DIRECT | LLM | RETRIED | FALLBACK
    flags: list[str] = field(default_factory=list)  # claims the check did not find in the summary


DIRECT, LLM, RETRIED, FALLBACK = "direct", "llm", "llm-retry", "fallback"
FALLBACK_HEAD = "В дневнике этого нет. Вот что есть:"
_FOOD_WORDS = re.compile(
    r"ккал|калор|бел[оке]|жир|углевод|бжу|\d\s*г(?![а-яa-z])|съел|поел|(?<![а-яa-z])(?:ел|ед[аыу])(?![а-яa-z])|питани"
)


def fallback(ctx: Context, question: str, flags: list[str]) -> str:
    """The honest answer when the model keeps adding data: the block the question is about."""
    q = normalize(question)
    if _FOOD_WORDS.search(q) or any(_FOOD_WORDS.search(normalize(f)) for f in flags):
        block = ctx.food
    elif asked := (direct.resolve(question, list(ctx.history), ctx.catalog, ctx.aliases) or ([], []))[0]:
        block = "\n".join(direct.exercise_line(ctx.history[n]) for n in asked[: direct.MAX_EXERCISES_IN_REPLY])
    else:
        block = ctx.done
    return f"{FALLBACK_HEAD}\n{block}"


async def checked_answer(
    llm: OpenRouterClient, ctx: Context, question: str, dialog: list[tuple[str, str]] | None = None
) -> Reply:
    """The model's answer after the check (see the module doc). Raises LLMError if the first call fails."""
    dialog = dialog or []
    messages = build_answer_messages(ctx.text, question, dialog)
    text = _cut(await llm.complete_text(messages, temperature=ANSWER_TEMPERATURE))
    evidence = ctx.evidence(question, dialog)
    flags = violations(text, evidence)
    if not flags:
        return Reply(text, LLM)
    log.warning("diary answer: not in the summary %s, asking again", flags)
    retry = [*messages, {"role": "assistant", "content": text}, {"role": "user", "content": correction(flags)}]
    try:
        again = _cut(await llm.complete_text(retry, temperature=ANSWER_TEMPERATURE))
    except LLMError as e:
        log.warning("diary answer: the regeneration failed (%s), honest fallback", e)
        return Reply(fallback(ctx, question, flags), FALLBACK, flags)
    still = violations(again, evidence)
    if not still:
        return Reply(again, RETRIED, flags)
    log.warning("diary answer: still not in the summary %s, honest fallback", still)
    return Reply(fallback(ctx, question, still), FALLBACK, still)


async def respond(
    sessionmaker: Sessionmaker,
    telegram_id: int,
    full_name: str,
    question: str,
    dialog: list[tuple[str, str]],
    settings: Settings,
    llm: OpenRouterClient,
    now_utc: datetime,
) -> Reply:
    """The diary answer to `question`: from the database when it is a factual one, else the checked model
    answer. Raises LLMError when the model is needed and no route answers."""
    tz = ZoneInfo(settings.timezone)
    async with sessionmaker() as session:
        user = await get_or_create_user(session, telegram_id, full_name)
        await session.commit()  # no write lock while the model thinks
        q = classify(question)
        if q is not None and (text := await direct.reply(session, user, question, q, tz, now_utc)) is not None:
            log.info("diary answer: %s from the database", q.intent.value)
            return Reply(text, DIRECT)
        ctx = await gather(session, user, settings, llm, tz, now_utc)
    return await checked_answer(llm, ctx, question, dialog)
