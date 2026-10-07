"""The diary answer's "done on the last training day" block, counted in code (gymbot.services.answer)."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from gymbot.db.models import User, Workout, WorkoutSet
from gymbot.services import answer
from gymbot.services.programs import get_or_create_exercise

TODAY = date(2026, 10, 7)

# The owner's real workout of 07.10 (Mini App history): 6 exercises, 24 sets, 6.5 t.
OWNER = [
    ("сгибания с гантелями на бицепс с супинацией", [(20, 8)] * 3 + [(17.5, 8)] * 2 + [(15, 8)]),
    ("сгибания с гантелями на бицепс с пронацией", [(12.5, 8), (10, 10), (10, 10)]),
    ("французский жим в блоке из-за головы", [(60, 10)] * 3 + [(60, 8), (50, 10), (40, 10)]),
    ("жим гантелей сидя", [(12.5, 12)] * 3),
    ("отведения на дельты", [(7.5, 15), (7.5, 15), (7.5, 10)]),
    ("отведения пек дек на заднюю дельту", [(35, 12), (50, 10), (50, 10)]),
]


async def _seed(s, day: date, exercises) -> int:
    user = User(telegram_id=42, name="Amir", rest_seconds=90)
    s.add(user)
    await s.flush()
    w = Workout(user_id=user.id, performed_on=day, started_at=datetime(2026, 10, 7, 14, tzinfo=UTC), source="miniapp")
    i = 0
    for name, sets in exercises:
        ex = await get_or_create_exercise(s, name)
        for weight, reps in sets:
            w.sets.append(WorkoutSet(exercise_id=ex.id, set_index=i, reps=reps, weight_kg=Decimal(str(weight))))
            i += 1
    s.add(w)
    await s.flush()
    return user.id


async def test_done_block_counts_the_owners_workout(db):
    async with db() as s:
        uid = await _seed(s, TODAY, OWNER)
        text = await answer.done_block(s, uid, TODAY)
    head, *lines = text.split("\n")
    assert head == "Сделано сегодня, из истории: 6 упр., 24 подх., тоннаж 6530 кг."
    assert lines[0] == "- сгибания с гантелями на бицепс с супинацией: 20×8 ×3, 17,5×8 ×2, 15×8 (880 кг)"
    assert lines[2] == "- французский жим в блоке из-за головы: 60×10 ×3, 60×8, 50×10, 40×10 (3180 кг)"
    assert len(lines) == 6


async def test_done_block_falls_back_to_the_last_training_day(db):
    async with db() as s:
        uid = await _seed(s, TODAY - timedelta(days=2), OWNER[:1])
        text = await answer.done_block(s, uid, TODAY)
    assert text.startswith("Сделано 05.10 (последняя тренировка), из истории: 1 упр., 6 подх.")


async def test_done_block_without_history(db):
    async with db() as s:
        user = User(telegram_id=42, name="Amir", rest_seconds=90)
        s.add(user)
        await s.flush()
        assert await answer.done_block(s, user.id, TODAY) == "В истории тренировок пока нет."
