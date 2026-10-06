from datetime import date
from zoneinfo import ZoneInfo

from sqlalchemy import select

from gymbot.db.models import Workout, WorkoutSet
from gymbot.llm.schemas import ParseResult
from gymbot.services import workouts as ws
from gymbot.services.programs import load_program
from gymbot.services.users import active_program, get_or_create_user

TZ = ZoneInfo("Europe/Moscow")


def parsed(*exercises) -> ParseResult:
    return ParseResult.model_validate({"kind": "workout", "exercises": list(exercises)})


async def test_save_appends_to_one_chat_workout_per_day(db):
    async with db() as s:
        user = await get_or_create_user(s, 1)
        r1 = parsed({"exercise": "Жим", "sets": [{"reps": 10, "weight_kg": 60}, {"reps": 9, "weight_kg": 60}]})
        r2 = parsed({"exercise": "Жим", "sets": [{"reps": 8, "weight_kg": 60}]})
        w1 = await ws.save_from_chat(s, user, r1, "жим 2х", date(2026, 10, 5))
        w2 = await ws.save_from_chat(s, user, r2, "жим ещё", date(2026, 10, 5))
        w3 = await ws.save_from_chat(s, user, r2, "жим вчера", date(2026, 10, 4))
        await s.commit()
        assert w1.id == w2.id != w3.id
        sets = (await s.scalars(select(WorkoutSet).where(WorkoutSet.workout_id == w1.id).order_by(WorkoutSet.set_index))).all()
        assert [x.set_index for x in sets] == [0, 1, 2]
        assert [x.reps for x in sets] == [10, 9, 8]


async def test_serialize_places_by_date_and_folds_drops(db):
    async with db() as s:
        user = await get_or_create_user(s, 1)
        up = await active_program(s, user, date(2026, 10, 7))
        weeks = len((await load_program(s, up.program_id)).weeks)
        # Wednesday of the second program week
        performed = date.fromordinal(up.started_on.toordinal() + 7 + 2)
        r = parsed(
            {
                "exercise": "Жим",
                "sets": [
                    {"reps": 10, "weight_kg": 60},
                    {"reps": 8, "weight_kg": 50, "drop_index": 1},
                    {"reps": 9, "weight_kg": 60},
                ],
            }
        )
        w = await ws.save_from_chat(s, user, r, "txt", performed)
        await s.commit()
        loaded = await ws.get_workout(s, user, w.id)
        out = ws.serialize(loaded, up, weeks, TZ)
    assert out.source == "chat"
    assert out.programId == up.program.slug
    assert (out.week, out.weekday) == (2, 3)
    assert len(out.exercises) == 1
    assert [(x.weight, x.reps) for x in out.exercises[0].sets] == [(60, 10), (60, 9)]


async def test_delete_last_chat_sets_only_last_message(db):
    async with db() as s:
        user = await get_or_create_user(s, 1)
        today = date(2026, 10, 5)
        await ws.save_from_chat(s, user, parsed({"exercise": "A", "sets": [{"reps": 5}]}), "first", today)
        await ws.save_from_chat(
            s, user, parsed({"exercise": "B", "sets": [{"reps": 6}, {"reps": 7}]}), "second", today
        )
        await s.commit()
        assert await ws.delete_last_chat_sets(s, user) == 2
        await s.commit()
        left = (await s.scalars(select(WorkoutSet))).all()
        assert [x.raw_text for x in left] == ["first"]
        assert len((await s.scalars(select(Workout))).all()) == 1


async def test_delete_last_chat_sets_removes_empty_workout(db):
    async with db() as s:
        user = await get_or_create_user(s, 1)
        await ws.save_from_chat(s, user, parsed({"exercise": "A", "sets": [{"reps": 5}]}), "only", date(2026, 10, 5))
        await s.commit()
    async with db() as s:  # fresh session, as each bot command gets one
        user = await get_or_create_user(s, 1)
        assert await ws.delete_last_chat_sets(s, user) == 1
        await s.commit()
    async with db() as s:
        assert (await s.scalars(select(Workout))).all() == []
        assert await ws.delete_last_chat_sets(s, user) == 0
