"""Answers to questions in the chat ("с каким весом жать?", "что у меня сегодня?") from the user's diary.

The parser answers a question in one short line without any data. For kind="question" the chat handler
asks again here with the advice summary (gymbot.services.advice: profile, facts, food, training with the
last sets and 1RM, wellbeing, program) plus today's adjusted plan (gymbot.services.plan), the weights set
for today from the chat (gymbot.services.overrides), the workout in progress in the Mini App
(gymbot.services.active_workout) and the last questions and answers of the dialog. Only reads, except that
the plan service stores today's plan.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from gymbot.config import Settings
from gymbot.db.models import Exercise, User, Workout, WorkoutSet
from gymbot.llm.openrouter import OpenRouterClient
from gymbot.llm.prompts import build_answer_messages
from gymbot.services import active_workout, advice, overrides, plan
from gymbot.services.users import active_program

ANSWER_MAX = 1500  # characters sent to the chat; the prompt asks for far less
NO_TRAINING = "Сегодня тренировки по программе нет."
DONE_MAX = 900  # characters of the "done on the last training day" block


def _kg(x: Decimal | float) -> str:
    return f"{float(x):g}".replace(".", ",") if float(x) != int(x) else str(int(x))


def _runs(sets: list[tuple[Decimal | None, int]]) -> str:
    """'20×8 ×3, 17,5×8 ×2, 15×8': equal sets in a row merged."""
    runs: list[list] = []
    for s in sets:
        if runs and runs[-1][0] == s:
            runs[-1][1] += 1
        else:
            runs.append([s, 1])
    out = []
    for (w, r), n in runs:
        one = f"{_kg(w)}×{r}" if w is not None else f"{r} повт."
        out.append(one + (f" ×{n}" if n > 1 else ""))
    return ", ".join(out)


async def done_block(session: AsyncSession, user_id: int, today: date) -> str:
    """What the history holds for the last training day up to today, counted here (sets, tonnage per
    exercise), so the model never has to add numbers up or guess them from the 14-day summary."""
    day = await session.scalar(
        select(func.max(Workout.performed_on)).where(Workout.user_id == user_id, Workout.performed_on <= today)
    )
    if day is None:
        return "В истории тренировок пока нет."
    rows = (
        await session.execute(
            select(Exercise.name, WorkoutSet.weight_kg, WorkoutSet.reps)
            .select_from(Workout)
            .join(WorkoutSet, WorkoutSet.workout_id == Workout.id)
            .join(Exercise, Exercise.id == WorkoutSet.exercise_id)
            .where(Workout.user_id == user_id, Workout.performed_on == day)
            .order_by(Workout.started_at, Workout.id, WorkoutSet.set_index)
        )
    ).all()
    by_ex: dict[str, list[tuple[Decimal | None, int]]] = {}
    for name, weight, reps in rows:
        by_ex.setdefault(name, []).append((weight, reps))
    total = sum((Decimal(w) * r for sets in by_ex.values() for w, r in sets if w is not None), Decimal(0))
    label = "сегодня" if day == today else f"{day:%d.%m} (последняя тренировка)"
    head = (
        f"Сделано {label}, из истории: {len(by_ex)} упр., {sum(len(v) for v in by_ex.values())} подх., "
        f"тоннаж {_kg(total)} кг."
    )
    lines = [head]
    for name, sets in by_ex.items():
        tonnage = sum((Decimal(w) * r for w, r in sets if w is not None), Decimal(0))
        lines.append(f"- {name}: {_runs(sets)} ({_kg(tonnage)} кг)")
    text = "\n".join(lines)
    return text if len(text) <= DONE_MAX else text[: DONE_MAX - 1] + "…"


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
    weights = overrides.context_line(await overrides.for_day(session, user.id, now_utc.astimezone(tz).date()))
    in_progress = await active_workout.context_for(session, user.id, now_utc, tz)
    done = await done_block(session, user.id, now_utc.astimezone(tz).date())
    tail = "".join(f"\n{line}" for line in (weights, in_progress) if line)
    return f"{summary}\n{done}\nПлан на сегодня ({now_utc.astimezone(tz):%d.%m}):\n{today}{tail}"


async def answer(
    llm: OpenRouterClient, context: str, question: str, dialog: list[tuple[str, str]] | None = None
) -> str:
    """Answer for the chat; `dialog` is (question, answer) pairs, oldest first. Raises LLMError."""
    text = await llm.complete_text(build_answer_messages(context, question, dialog or []))
    if len(text) > ANSWER_MAX:
        text = text[: ANSWER_MAX - 1].rstrip() + "…"
    return text
