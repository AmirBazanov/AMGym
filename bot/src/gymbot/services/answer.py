"""Answers to questions in the chat ("с каким весом жать?", "что у меня сегодня?") from the user's diary.

The parser answers a question in one short line without any data. For kind="question" the chat handler
asks again here with the advice summary (gymbot.services.advice: profile, facts, food, training with the
last sets and 1RM, wellbeing, program) plus today's adjusted plan (gymbot.services.plan) and the last
questions and answers of the dialog. Only reads, except that the plan service stores today's plan.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from gymbot.config import Settings
from gymbot.db.models import User
from gymbot.llm.openrouter import OpenRouterClient
from gymbot.llm.prompts import build_answer_messages
from gymbot.services import advice, plan
from gymbot.services.users import active_program

ANSWER_MAX = 1500  # characters sent to the chat; the prompt asks for far less
NO_TRAINING = "Сегодня тренировки по программе нет."


async def build_context(
    session: AsyncSession,
    user: User,
    settings: Settings,
    llm: OpenRouterClient | None,
    tz: ZoneInfo,
    now_utc: datetime,
) -> str:
    """The advice summary and today's plan. Starts the default program like /plan; commits."""
    await active_program(session, user, now_utc.astimezone(tz).date())
    await session.commit()
    summary = await advice.build_context(session, user, settings, tz, now_utc)
    built = await plan.get_or_build(session, user, settings, llm, tz, now_utc)
    today = plan.plan_text(built) if built is not None else NO_TRAINING
    return f"{summary}\nПлан на сегодня ({now_utc.astimezone(tz):%d.%m}):\n{today}"


async def answer(
    llm: OpenRouterClient, context: str, question: str, dialog: list[tuple[str, str]] | None = None
) -> str:
    """Answer for the chat; `dialog` is (question, answer) pairs, oldest first. Raises LLMError."""
    text = await llm.complete_text(build_answer_messages(context, question, dialog or []))
    if len(text) > ANSWER_MAX:
        text = text[: ANSWER_MAX - 1].rstrip() + "…"
    return text
