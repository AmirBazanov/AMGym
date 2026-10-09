"""Auto deload: stall detection, the offer cadence, the lighter day plan, /deload and its buttons, the
migration. Every time is passed in or pinned; nothing here reads the real clock.
"""

import json
import math
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from alembic import command
from sqlalchemy import func, inspect, select, text
from test_plan import ARMS, LEGS, MON, MSK, _build, _user_on_program, answer, inputs
from test_plan import NOW as PLAN_NOW

from gymbot.db import migrate
from gymbot.db.models import (
    DayPlan,
    DeloadState,
    ProgramDay,
    ProgramItem,
    ProgramWeek,
    User,
    WellbeingEntry,
    Workout,
    WorkoutSet,
)
from gymbot.db.session import make_engine
from gymbot.handlers import deload as hd
from gymbot.services import deload, live, plan, workout_events
from gymbot.services.deload import SessionStat
from gymbot.services.programs import get_or_create_exercise, load_program
from gymbot.services.users import get_or_create_user

TZ = ZoneInfo("Europe/Moscow")
TODAY = date(2026, 10, 8)  # a Thursday
NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)  # 15:00 in Moscow, still TODAY
BENCH, SQUAT = "жим лёжа", "присед со штангой"  # both in data/programs/arms_specialization_8w.json
ODD_DAYS = (1, 3, 5, 7, 9, 11)  # six sessions, days before TODAY


def aware(dt: datetime | None) -> datetime | None:
    """SQLite hands datetimes back naive (UTC)."""
    return deload._aware(dt)


# ---- pure ----


@pytest.mark.parametrize(("sets", "expected"), [(1, 1), (2, 2), (3, 2), (4, 3), (5, 4), (6, 4), (8, 6)])
def test_deload_sets(sets, expected):
    assert deload.deload_sets(sets) == expected
    assert deload.deload_sets(sets) <= sets


def test_deload_sets_never_below_one_and_never_more():
    for n in range(1, 13):
        got = deload.deload_sets(n)
        assert 1 <= got <= n and got == max(1, math.ceil(n * 2 / 3 - 1e-9))


def test_summary_text():
    assert deload.summary(date(2026, 10, 16)) == "Разгрузочная неделя до пт 16.10: веса −15 %, подходов меньше."
    assert deload.summary(date(2026, 10, 11)).startswith("Разгрузочная неделя до вс 11.10:")


def stat(i: int, e1rm: float, top: float = 80, reps: int = 8) -> SessionStat:
    return SessionStat(date(2026, 9, 1) + timedelta(days=3 * i), e1rm, top, reps)


def test_session_stat():
    s = deload.session_stat(date(2026, 10, 1), [(80, 10), (80, 8), (70, 12)])
    assert (s.day, s.top_weight, s.reps_at_top) == (date(2026, 10, 1), 80, 10)
    assert s.best_e1rm == pytest.approx(80 * (1 + 10 / 30))
    assert deload.session_stat(date(2026, 10, 1), [(100, 1)]).best_e1rm == 100  # a single is the 1RM itself


@pytest.mark.parametrize(
    ("e1rms", "expected"),
    [
        ([100, 100, 100, 100, 100, 100], True),  # flat
        ([100, 100, 100, 100, 100, 100.5], True),  # +0.5 %: within the tolerance
        ([100, 100, 100, 100, 100, 101], True),  # exactly +1 %: still a stall
        ([100, 100, 100, 100, 100, 101.2], False),  # +1.2 %
        ([100, 100, 100, 102, 102, 102], False),  # +2 %
        ([110, 105, 100, 100, 100, 100], True),  # going down
        ([100, 100, 100, 100, 100], False),  # five sessions: nothing to compare yet
        ([100, 100, 100], False),
    ],
)
def test_stalled_by_one_rep_max(e1rms, expected):
    assert deload.stalled([stat(i, e) for i, e in enumerate(e1rms)]) is expected


def test_stalled_compares_the_best_of_each_triple():
    # one good session among the last three counts: 100 -> 103 is growth
    assert deload.stalled([stat(i, e) for i, e in enumerate([100, 99, 98, 90, 103, 95])]) is False


@pytest.mark.parametrize(
    ("sessions", "expected"),
    [
        ([(80, 10), (80, 9), (80, 8)], True),  # reps falling 2 sessions in a row at the same weight
        ([(80, 10), (80, 9), (80, 9)], False),  # not strictly falling
        ([(80, 10), (80, 10), (80, 8)], False),
        ([(80, 10), (80, 9), (82.5, 8)], False),  # the weight changed
        ([(80, 10), (82.5, 9), (82.5, 8)], False),
        ([(80, 10), (80, 9)], False),  # fewer than 3 sessions
        ([(80, 8)], False),
        ([], False),
    ],
)
def test_stalled_by_falling_reps(sessions, expected):
    # growing 1RM keeps the first rule quiet; only the reps rule can answer
    stats = [stat(i, 100 + i, w, r) for i, (w, r) in enumerate(sessions)]
    assert deload.stalled(stats) is expected


def test_falling_reps_rule_applies_after_six_sessions_too():
    stats = [stat(i, 100 + 5 * i) for i in range(3)] + [stat(3 + i, 130 + i, 80, r) for i, r in enumerate((10, 9, 8))]
    assert deload.stalled(stats) is True
    stats[-1] = stat(5, 135, 80, 9)  # 10, 9, 9: not falling, and the 1RM grows
    assert deload.stalled(stats) is False


def test_active_and_due_pure():
    st = DeloadState(user_id=1, started_on=TODAY, until=TODAY + timedelta(days=6))
    assert deload.active(st, TODAY) and deload.active(st, TODAY + timedelta(days=6))
    assert not deload.active(st, TODAY - timedelta(days=1)) and not deload.active(st, TODAY + timedelta(days=7))
    assert not deload.active(None, TODAY) and not deload.active(DeloadState(user_id=1), TODAY)
    assert deload.due(None, TODAY, NOW)
    assert not deload.due(st, TODAY, NOW)  # never while it runs
    snoozed = DeloadState(user_id=1, ask_after=NOW + timedelta(hours=1))
    assert not deload.due(snoozed, TODAY, NOW) and deload.due(snoozed, TODAY, NOW + timedelta(hours=1))
    assert deload.due(DeloadState(user_id=1, ask_after=(NOW - timedelta(days=1)).replace(tzinfo=None)), TODAY, NOW)


def test_offer_keyboard_buttons():
    kb = deload.offer_keyboard()
    assert [b.callback_data for b in kb.inline_keyboard[0]] == ["deload:yes", "deload:later", "deload:no"]


# ---- helpers: users, workouts, wellbeing ----


async def log(db, uid: int, day: date, name: str, sets: list[tuple[float, int]], *, drop: int = 0) -> None:
    async with db() as s:
        ex = await get_or_create_exercise(s, name)
        at = datetime.combine(day, time(9), tzinfo=UTC)
        w = Workout(user_id=uid, performed_on=day, started_at=at, source="miniapp")
        w.sets = [
            WorkoutSet(exercise_id=ex.id, set_index=i, reps=r, weight_kg=Decimal(str(wt)), drop_index=drop)
            for i, (wt, r) in enumerate(sets)
        ]
        s.add(w)
        await s.commit()


async def stalled_lifts(db, uid: int, names=(BENCH, SQUAT), days=ODD_DAYS, today=TODAY) -> None:
    """The same 80×8 on every day: no growth."""
    for name in names:
        for d in days:
            await log(db, uid, today - timedelta(days=d), name, [(80, 8)])


async def low_entries(db, uid: int, n: int, today=TODAY, **kw) -> None:
    async with db() as s:
        for k in range(n):
            at = datetime.combine(today - timedelta(days=k), time(12), tzinfo=TZ)
            s.add(WellbeingEntry(user_id=uid, noted_at=at, raw_text="плохо", **(kw or {"energy": 2})))
        await s.commit()


async def verdict(db, settings, uid: int, today=TODAY, programs_dir="default"):
    async with db() as s:
        user = await s.get(User, uid)
        return await deload.evaluate(s, user, today, TZ, settings.programs_dir if programs_dir == "default" else programs_dir)


async def state_of(db, uid: int) -> DeloadState | None:
    async with db() as s:
        return await s.get(DeloadState, uid)


# ---- evaluate ----


async def test_two_stalled_main_lifts_suggest_a_deload(db, settings):
    uid = await _user_on_program(db, start=TODAY - timedelta(days=10))
    await stalled_lifts(db, uid)
    v = await verdict(db, settings, uid)
    assert v.suggest and set(v.stalls) == {BENCH, SQUAT} and set(v.main_lifts) == {BENCH, SQUAT}
    assert v.weeks is None and v.low_entries == 0 and not v.program_deload
    msg = deload.offer_text(v)
    assert "Похоже, прогресс встал:" in msg and BENCH in msg and SQUAT in msg
    assert msg.endswith("Сделать разгрузочную неделю? Веса −15 %, подходов на треть меньше.")
    assert "Сделать разгрузочную неделю?" in msg


async def test_one_stalled_lift_is_not_enough(db, settings):
    uid = await _user_on_program(db, start=TODAY - timedelta(days=10))
    for d in ODD_DAYS:
        await log(db, uid, TODAY - timedelta(days=d), BENCH, [(80, 8)])
    for d, w in zip(reversed(ODD_DAYS), (60, 62.5, 65, 70, 72.5, 75), strict=True):  # the squat grows
        await log(db, uid, TODAY - timedelta(days=d), SQUAT, [(w, 8)])
    v = await verdict(db, settings, uid)
    assert v.stalls == [BENCH] and set(v.main_lifts) == {BENCH, SQUAT}
    assert not v.suggest and v.reasons() == []


async def test_a_lift_outside_the_program_is_not_a_main_lift(db, settings):
    uid = await _user_on_program(db, start=TODAY - timedelta(days=10))
    for d in ODD_DAYS:
        await log(db, uid, TODAY - timedelta(days=d), BENCH, [(80, 8)])
        await log(db, uid, TODAY - timedelta(days=d), "подтягивания с весом", [(10, 8)])  # not in the program
    v = await verdict(db, settings, uid)
    assert v.stalls == [BENCH] and v.main_lifts == [BENCH] and not v.suggest
    # control: the same stall on a second program lift suggests a deload
    for d in ODD_DAYS:
        await log(db, uid, TODAY - timedelta(days=d), SQUAT, [(80, 8)])
    assert (await verdict(db, settings, uid)).suggest


async def test_main_lift_needs_three_sessions_in_four_weeks(db, settings):
    uid = await _user_on_program(db, start=TODAY - timedelta(days=10))
    await stalled_lifts(db, uid, days=(1, 3))
    v = await verdict(db, settings, uid)
    assert v.main_lifts == [] and not v.suggest


async def test_drops_and_empty_sets_are_not_sessions(db, settings):
    uid = await _user_on_program(db, start=TODAY - timedelta(days=10))
    for d in ODD_DAYS:
        for name in (BENCH, SQUAT):
            await log(db, uid, TODAY - timedelta(days=d), name, [(80, 8)], drop=1)
    v = await verdict(db, settings, uid)
    assert v.main_lifts == [] and not v.suggest


async def test_sessions_before_the_last_deload_are_ignored(db, settings):
    uid = await _user_on_program(db, start=TODAY - timedelta(days=40))
    await stalled_lifts(db, uid, days=(16, 18, 20, 22, 24, 26))
    assert (await verdict(db, settings, uid)).suggest  # control: the stall counts without a deload
    async with db() as s:
        s.add(DeloadState(user_id=uid, started_on=TODAY - timedelta(days=15), until=TODAY - timedelta(days=9)))
        await s.commit()
    v = await verdict(db, settings, uid)
    assert v.main_lifts == [] and v.stalls == [] and not v.suggest


async def test_a_deload_that_is_running_is_not_an_ended_one(db, settings):
    # sessions during a running deload are not cut off (only `until < today` is "the last deload")
    uid = await _user_on_program(db, start=TODAY - timedelta(days=10))
    await stalled_lifts(db, uid)
    async with db() as s:
        s.add(DeloadState(user_id=uid, started_on=TODAY - timedelta(days=2), until=TODAY + timedelta(days=4)))
        await s.commit()
    assert (await verdict(db, settings, uid)).stalls


async def regular_training(db, uid: int, n: int = 6) -> None:
    for d in range(1, n + 1):
        await log(db, uid, TODAY - timedelta(days=d), "подтягивания с весом", [(10, 8)])


@pytest.mark.parametrize(("days", "weeks"), [(41, None), (42, 6), (50, 7)])
async def test_weeks_without_a_deload(db, settings, days, weeks):
    uid = await _user_on_program(db, start=TODAY - timedelta(days=days))
    await regular_training(db, uid)
    v = await verdict(db, settings, uid)
    assert v.weeks == weeks and v.suggest is (weeks is not None)
    if weeks:
        assert f"Уже {weeks} недель без разгрузки." in deload.offer_text(v)


async def test_weeks_rule_needs_regular_training(db, settings):
    uid = await _user_on_program(db, start=TODAY - timedelta(days=50))
    await regular_training(db, uid, n=5)  # five training days in four weeks
    assert (await verdict(db, settings, uid)).weeks is None
    await log(db, uid, TODAY - timedelta(days=6), "подтягивания с весом", [(10, 8)])
    assert (await verdict(db, settings, uid)).weeks == 7


async def test_weeks_rule_counts_from_the_last_deload(db, settings):
    uid = await _user_on_program(db, start=TODAY - timedelta(days=60))
    await regular_training(db, uid)
    assert (await verdict(db, settings, uid)).weeks == 8  # control
    async with db() as s:
        s.add(DeloadState(user_id=uid, started_on=TODAY - timedelta(days=16), until=TODAY - timedelta(days=10)))
        await s.commit()
    v = await verdict(db, settings, uid)
    assert v.weeks is None and not v.suggest


async def test_a_passed_program_deload_week_resets_the_weeks_count(db, settings):
    uid = await _user_on_program(db, start=TODAY - timedelta(days=60))
    await regular_training(db, uid)
    assert (await verdict(db, settings, uid)).weeks == 8
    await make_light_week(db, 3)  # week 3 ended on start + 20 = 40 days ago
    v = await verdict(db, settings, uid)
    assert v.weeks is None and not v.program_deload


async def test_low_wellbeing_entries_suggest_a_deload(db, settings):
    uid = await _user_on_program(db, start=TODAY - timedelta(days=10))
    async with db() as s:
        for k, kw in enumerate(({"energy": 2}, {"sleep_quality": 1}, {"sleep_hours": Decimal("5.9")})):
            s.add(WellbeingEntry(user_id=uid, noted_at=datetime.combine(TODAY - timedelta(days=k), time(8), tzinfo=TZ),
                                 raw_text="плохо", **kw))
        await s.commit()
    v = await verdict(db, settings, uid)
    assert v.low_entries == 3 and v.suggest
    assert "За неделю 3 раза мало сна или сил." in deload.offer_text(v)


@pytest.mark.parametrize(
    "ok_entry",
    [{"energy": 3}, {"sleep_quality": 3}, {"sleep_hours": Decimal("6.0")}, {"mood": 1}],
)
async def test_two_low_entries_or_fine_ones_do_not_count(db, settings, ok_entry):
    uid = await _user_on_program(db, start=TODAY - timedelta(days=10))
    await low_entries(db, uid, 2)
    async with db() as s:  # a third entry that is not low
        s.add(WellbeingEntry(user_id=uid, noted_at=datetime.combine(TODAY, time(8), tzinfo=TZ), raw_text="норм",
                             **ok_entry))
        await s.commit()
    v = await verdict(db, settings, uid)
    assert v.low_entries == 0 and not v.suggest


async def test_old_low_entries_do_not_count(db, settings):
    uid = await _user_on_program(db, start=TODAY - timedelta(days=10))
    await low_entries(db, uid, 3, today=TODAY - timedelta(days=7))  # days 7, 8, 9 ago: outside the last 7 days
    assert (await verdict(db, settings, uid)).low_entries == 0
    await low_entries(db, uid, 3, today=TODAY - timedelta(days=3))  # control: inside
    assert (await verdict(db, settings, uid)).low_entries == 3


async def make_light_week(db, number: int) -> None:
    """The program's own deload week: every item of the week is of light intensity."""
    async with db() as s:
        items = await s.scalars(
            select(ProgramItem)
            .join(ProgramDay, ProgramItem.day_id == ProgramDay.id)
            .join(ProgramWeek, ProgramDay.week_id == ProgramWeek.id)
            .where(ProgramWeek.number == number)
        )
        for item in items:
            item.intensity = "light"
        await s.commit()


@pytest.mark.parametrize(
    ("start", "near"),
    [(-14, True), (-7, True), (0, True), (1, False), (-21, False)],
    ids=["running", "next week", "in 14 days", "in 15 days", "already passed"],
)
async def test_the_programs_own_deload_week_blocks_the_offer(db, settings, start, near):
    uid = await _user_on_program(db, start=TODAY + timedelta(days=start))
    await low_entries(db, uid, 3)  # a reason that would otherwise suggest a deload
    assert (await verdict(db, settings, uid)).suggest  # control: nothing light in the program yet
    await make_light_week(db, 3)
    v = await verdict(db, settings, uid)
    assert v.program_deload is near
    assert v.suggest is (not near) and v.low_entries == 3
    assert (
        "В программе скоро своя разгрузочная неделя."
        in deload.status_text(None, v, TODAY)
    ) is near


async def test_program_deload_weeks_from_items_and_json(db, settings, tmp_path):
    uid = await _user_on_program(db)
    async with db() as s:
        assert await s.get(User, uid) is not None
        up_program = await s.scalar(select(ProgramWeek.program_id).limit(1))
        program = await load_program(s, up_program)
        assert deload.program_deload_weeks(program) == set()
        assert deload.program_deload_weeks(program, settings.programs_dir) == set()  # the real program has none
        (tmp_path / f"{program.slug}.json").write_text(
            json.dumps({"weeks": [{"number": 5, "deload": True}, {"number": 6, "deload": False}, {"number": 7}, "x"]}),
            encoding="utf-8",
        )
        assert deload.program_deload_weeks(program, tmp_path) == {5}
        (tmp_path / f"{program.slug}.json").write_text("not json", encoding="utf-8")
        assert deload.program_deload_weeks(program, tmp_path) == set()
        assert deload.program_deload_weeks(program, tmp_path / "missing") == set()
    await make_light_week(db, 4)
    async with db() as s:
        program = await load_program(s, up_program)
        assert deload.program_deload_weeks(program) == {4}


async def test_no_program_means_no_main_lifts(db, settings):
    async with db() as s:
        user = await get_or_create_user(s, 42, "Amir")
        await s.commit()
        uid = user.id
    await stalled_lifts(db, uid)
    v = await verdict(db, settings, uid)
    assert v.main_lifts == [] and not v.suggest


# ---- cadence: maybe_offer, postpone, start, cancel ----


async def offer(db, settings, uid: int, now: datetime) -> str | None:
    async with db() as s:
        out = await deload.maybe_offer(s, uid, now, TZ, settings.programs_dir)
        await s.commit()
        return out


async def stalled_user(db) -> int:
    uid = await _user_on_program(db, start=TODAY - timedelta(days=10))
    await stalled_lifts(db, uid)
    return uid


async def test_an_ignored_offer_comes_back_after_three_days(db, settings):
    uid = await stalled_user(db)
    first = await offer(db, settings, uid, NOW)
    assert first is not None and "Похоже, прогресс встал:" in first
    st = await state_of(db, uid)
    assert aware(st.offered_at) == NOW and aware(st.ask_after) == NOW + deload.LATER == NOW + timedelta(days=3)
    assert st.started_on is None  # an offer does not start anything

    assert await offer(db, settings, uid, NOW) is None  # the next workout the same day
    assert await offer(db, settings, uid, NOW + timedelta(days=2, hours=23)) is None
    again = await offer(db, settings, uid, NOW + timedelta(days=3))
    assert again == first
    st = await state_of(db, uid)
    assert aware(st.ask_after) == NOW + timedelta(days=6)


async def test_no_offer_without_a_reason(db, settings):
    uid = await _user_on_program(db, start=TODAY - timedelta(days=10))
    assert await offer(db, settings, uid, NOW) is None
    assert await state_of(db, uid) is None  # nothing was marked as sent
    assert await offer(db, settings, 9999, NOW) is None  # an unknown user


async def test_later_asks_again_in_three_days(db, settings):
    uid = await stalled_user(db)
    async with db() as s:
        await deload.postpone(s, uid, NOW, refuse=False)
        await s.commit()
    assert aware((await state_of(db, uid)).ask_after) == NOW + timedelta(days=3)
    assert await offer(db, settings, uid, NOW + timedelta(days=3) - timedelta(minutes=1)) is None
    assert await offer(db, settings, uid, NOW + timedelta(days=3)) is not None


async def test_no_asks_again_in_two_weeks(db, settings):
    uid = await stalled_user(db)
    async with db() as s:
        await deload.postpone(s, uid, NOW, refuse=True)
        await s.commit()
    assert aware((await state_of(db, uid)).ask_after) == NOW + timedelta(days=14)
    assert await offer(db, settings, uid, NOW + timedelta(days=3)) is None
    assert await offer(db, settings, uid, NOW + timedelta(days=13, hours=23)) is None
    assert await offer(db, settings, uid, NOW + timedelta(days=14)) is not None


async def test_start_makes_it_active_and_blocks_offers(db, settings):
    uid = await stalled_user(db)
    async with db() as s:
        until = await deload.start(s, uid, TODAY, NOW)
        await s.commit()
    assert until == TODAY + timedelta(days=6)
    st = await state_of(db, uid)
    assert (st.started_on, st.until) == (TODAY, until)
    async with db() as s:
        assert await deload.active_until(s, uid, TODAY) == until
        assert await deload.active_until(s, uid, until) == until
        assert await deload.active_until(s, uid, until + timedelta(days=1)) is None
        assert await deload.active_until(s, uid, TODAY - timedelta(days=1)) is None
    for days in (0, 3, 6):  # the stall is still there, but never while it runs
        assert await offer(db, settings, uid, NOW + timedelta(days=days)) is None
    # after it: two weeks of quiet counted from its last day
    assert aware(st.ask_after) == datetime(2026, 10, 14, tzinfo=UTC) + deload.AFTER_DELOAD
    assert not deload.due(st, until + timedelta(days=1), NOW + timedelta(days=7))
    assert deload.due(st, until + timedelta(days=15), datetime(2026, 10, 28, tzinfo=UTC))


async def test_cancel_the_same_day_forgets_it(db):
    async with db() as s:
        user = await get_or_create_user(s, 42, "A")
        await deload.start(s, user.id, TODAY, NOW)
        await s.commit()
        uid = user.id
    async with db() as s:
        assert await deload.cancel(s, uid, TODAY, NOW) is True
        await s.commit()
    st = await state_of(db, uid)
    assert st.started_on is None and st.until is None and aware(st.ask_after) == NOW + timedelta(days=14)
    async with db() as s:
        assert await deload.active_until(s, uid, TODAY) is None
        assert await deload.cancel(s, uid, TODAY, NOW) is False  # nothing is running


async def test_cancel_on_a_later_day_ends_it_yesterday(db):
    async with db() as s:
        user = await get_or_create_user(s, 42, "A")
        await deload.start(s, user.id, TODAY - timedelta(days=3), NOW - timedelta(days=3))
        await s.commit()
        uid = user.id
    async with db() as s:
        assert await deload.cancel(s, uid, TODAY, NOW) is True
        await s.commit()
    st = await state_of(db, uid)
    assert (st.started_on, st.until) == (TODAY - timedelta(days=3), TODAY - timedelta(days=1))
    assert not deload.active(st, TODAY) and deload.active(st, TODAY - timedelta(days=1))
    assert aware(st.ask_after) == NOW + timedelta(days=14)


async def test_cancel_is_a_no_op_without_a_state(db):
    async with db() as s:
        user = await get_or_create_user(s, 42, "A")
        assert await deload.cancel(s, user.id, TODAY, NOW) is False


# ---- scheduled (a start day after today) ----

NEXT_MON = date(2026, 10, 12)


async def test_start_in_the_future_is_scheduled_not_active(db, settings):
    uid = await stalled_user(db)
    async with db() as s:
        until = await deload.start(s, uid, TODAY, NOW, start_on=NEXT_MON)
        await s.commit()
    assert until == NEXT_MON + timedelta(days=6)
    st = await state_of(db, uid)
    assert (st.started_on, st.until) == (NEXT_MON, until)
    assert not deload.active(st, TODAY) and deload.scheduled(st, TODAY) and deload.pending(st, TODAY)
    assert deload.active(st, NEXT_MON) and not deload.scheduled(st, NEXT_MON) and deload.pending(st, NEXT_MON)
    assert deload.active(st, until) and not deload.pending(st, until + timedelta(days=1))
    async with db() as s:
        assert await deload.active_until(s, uid, TODAY) is None  # today's plan stays normal
        assert await deload.active_until(s, uid, NEXT_MON) == until
    assert aware(st.ask_after) == datetime(2026, 10, 18, tzinfo=UTC) + deload.AFTER_DELOAD


async def test_no_offers_while_a_deload_is_scheduled(db, settings):
    uid = await stalled_user(db)
    async with db() as s:
        await deload.start(s, uid, TODAY, NOW, start_on=NEXT_MON)
        await s.commit()
    st = await state_of(db, uid)
    assert not deload.due(st, TODAY, NOW) and not deload.due(st, TODAY, NOW + timedelta(days=30))
    for days in (0, 1, 3):
        assert await offer(db, settings, uid, NOW + timedelta(days=days)) is None


async def test_cancel_a_scheduled_deload_clears_it(db):
    async with db() as s:
        user = await get_or_create_user(s, 42, "A")
        await deload.start(s, user.id, TODAY, NOW, start_on=NEXT_MON)
        await s.commit()
        uid = user.id
    async with db() as s:
        assert await deload.cancel(s, uid, TODAY, NOW) is True
        await s.commit()
    st = await state_of(db, uid)
    assert st.started_on is None and st.until is None and aware(st.ask_after) == NOW + timedelta(days=14)
    assert not deload.pending(st, TODAY)
    async with db() as s:
        assert await deload.cancel(s, uid, TODAY, NOW) is False


async def test_start_on_in_the_past_clamps_to_today(db):
    async with db() as s:
        user = await get_or_create_user(s, 42, "A")
        until = await deload.start(s, user.id, TODAY, NOW, start_on=TODAY - timedelta(days=3))
        await s.commit()
        uid = user.id
    assert until == TODAY + timedelta(days=6)
    st = await state_of(db, uid)
    assert st.started_on == TODAY and deload.active(st, TODAY)


async def test_a_scheduled_deload_replaces_a_running_one(db):
    async with db() as s:
        user = await get_or_create_user(s, 42, "A")
        await deload.start(s, user.id, TODAY, NOW)
        await deload.start(s, user.id, TODAY, NOW, start_on=NEXT_MON)
        await s.commit()
        uid = user.id
    st = await state_of(db, uid)
    assert st.started_on == NEXT_MON and not deload.active(st, TODAY)


def test_scheduled_and_pending_pure():
    st = DeloadState(user_id=1, started_on=NEXT_MON, until=NEXT_MON + timedelta(days=6))
    assert deload.scheduled(st, TODAY) and not deload.scheduled(st, NEXT_MON)
    assert not deload.scheduled(None, TODAY) and not deload.scheduled(DeloadState(user_id=1), TODAY)
    assert not deload.pending(None, TODAY) and not deload.pending(DeloadState(user_id=1), TODAY)


def test_span_text():
    assert deload.span(NEXT_MON, date(2026, 10, 18)) == "с пн 12.10 по вс 18.10"


def test_status_text_for_a_scheduled_deload():
    st = DeloadState(user_id=1, started_on=NEXT_MON, until=date(2026, 10, 18))
    assert deload.status_text(st, deload.Verdict(), TODAY) == (
        "Разгрузочная неделя запланирована: с пн 12.10 по вс 18.10."
    )


# ---- status text ----


def test_status_text_variants():
    running = DeloadState(user_id=1, started_on=TODAY, until=date(2026, 10, 14))
    assert deload.status_text(running, deload.Verdict(), TODAY) == (
        "Идёт разгрузочная неделя до ср 14.10: веса −15 %, подходов меньше."
    )
    none = deload.status_text(None, deload.Verdict(), TODAY)
    assert none == "Разгрузки сейчас нет.\nДля оценки мало тренировок: нужно 3 сессии упражнения за 4 недели."
    growing = deload.Verdict(main_lifts=[BENCH, SQUAT])
    assert deload.status_text(None, growing, TODAY).endswith("Основные упражнения растут (2): разгрузка пока не нужна.")
    last = DeloadState(user_id=1, started_on=date(2026, 9, 1), until=date(2026, 9, 7))
    stalled_v = deload.Verdict(suggest=True, stalls=[BENCH, SQUAT], main_lifts=[BENCH, SQUAT])
    out = deload.status_text(last, stalled_v, TODAY)
    assert out.splitlines()[:2] == ["Разгрузки сейчас нет.", "Последняя: 01.09–07.09."]
    assert "Похоже, прогресс встал" in out


# ---- the day plan ----

UNTIL = date(2026, 10, 11)  # a Sunday
NOTE = deload.summary(UNTIL)


def light_sets(n: int) -> int:
    return max(min(n, 2), n - 1)


def test_deload_draft_lightens_every_exercise():
    i = inputs(ARMS, deload_until=UNTIL)
    draft = plan.rule_draft(i, PLAN_NOW)
    assert draft.readiness == "normal" and draft.adjusted and draft.deload
    assert draft.summary == NOTE and draft.summary.startswith("Разгрузочная неделя до")
    assert [e.weightFactor for e in draft.exercises] == [0.85] * len(ARMS)
    assert [e.sets for e in draft.exercises] == [deload.deload_sets(it.sets) for it in ARMS] == [4, 2, 4, 2, 2, 2]
    assert not any(e.skip or e.replaceWith for e in draft.exercises)
    assert [e.name for e in draft.exercises] == [it.name for it in ARMS]
    # reps are kept, a dropset keeps its drops (no reps in the plan)
    assert (draft.exercises[0].repsMin, draft.exercises[0].repsMax) == (8, 12)
    assert (draft.exercises[3].repsMin, draft.exercises[3].repsMax) == (None, None)


def test_deload_draft_on_the_legs_day():
    draft = plan.rule_draft(inputs(LEGS, deload_until=UNTIL), PLAN_NOW)
    assert [e.sets for e in draft.exercises] == [3, 3, 3, 3] and {e.weightFactor for e in draft.exercises} == {0.85}


def test_no_deload_leaves_the_draft_alone():
    draft = plan.rule_draft(inputs(ARMS), PLAN_NOW)
    assert not draft.deload and not draft.adjusted and draft.summary is None


def test_rest_still_wins_over_a_deload():
    draft = plan.rule_draft(inputs(ARMS, deload_until=UNTIL, sleep_hours=3), PLAN_NOW)
    assert draft.readiness == "rest" and all(e.skip for e in draft.exercises) and not draft.deload
    assert "спал 3 ч" in draft.summary.lower() and "Разгрузочная" not in draft.summary
    assert {e.weightFactor for e in draft.exercises} == {1.0}
    assert [e.sets for e in draft.exercises] == [it.sets for it in ARMS]


def test_light_day_takes_the_lighter_of_both():
    items = [plan.DayItem(n, f"упр {n}", n, 8, 12) for n in (1, 2, 3, 4, 5, 6)]
    light = plan.rule_draft(inputs(items, sleep_hours=5), PLAN_NOW)
    both = plan.rule_draft(inputs(items, sleep_hours=5, deload_until=UNTIL), PLAN_NOW)
    assert both.readiness == "light" and both.deload and both.adjusted
    assert {e.weightFactor for e in both.exercises} == {0.85}  # min(0.9, 0.85)
    assert {e.weightFactor for e in light.exercises} == {0.9}
    assert [e.sets for e in both.exercises] == [min(light_sets(n), deload.deload_sets(n)) for n in range(1, 7)]
    assert [e.sets for e in both.exercises] == [1, 2, 2, 3, 4, 4]
    assert NOTE in both.summary and "спал 5 ч" in both.summary.lower()


def test_mild_pain_on_a_deload_week_keeps_both_notes():
    draft = plan.rule_draft(inputs(ARMS, deload_until=UNTIL, pains=[plan.Pain("плечо", 1)]), PLAN_NOW)
    assert draft.readiness == "normal" and draft.deload and draft.protected
    assert draft.summary.startswith(NOTE) and draft.summary.endswith("эти упражнения легче.")
    assert {e.weightFactor for e in draft.exercises} == {0.85}


def test_pain_exercises_stay_lighter_than_the_deload():
    i = inputs(ARMS, deload_until=UNTIL, pains=[plan.Pain("левое плечо", None)])  # a light day
    draft = plan.rule_draft(i, PLAN_NOW)
    by = {e.name: e for e in draft.exercises}
    assert by["жим гантелей сидя"].weightFactor == 0.7  # the pain rule is lighter than 0.85
    assert by["сгибания с гантелями на бицепс с супинацией"].weightFactor == 0.85
    assert draft.readiness == "light" and draft.deload and draft.protected
    assert NOTE in draft.summary and draft.summary.startswith("Болит левое плечо")


def test_refinement_cannot_make_a_deload_heavier():
    i = inputs(ARMS, deload_until=UNTIL)
    draft = plan.rule_draft(i, PLAN_NOW)
    data = answer(draft, items={0: {"weightFactor": 1.0, "sets": 6}, 1: {"weightFactor": 1.3, "sets": 3},
                                2: {"weightFactor": 0.7}}, summary="Итог.")
    out = plan.apply_refinement(draft, data, i)
    assert [e.weightFactor for e in out.exercises[:3]] == [0.85, 0.85, 0.7]  # lighter is allowed
    assert [e.sets for e in out.exercises[:2]] == [4, 2]  # sets never above the draft either
    assert out.deload and out.adjusted and out.readiness == "normal"
    assert out.summary == f"{NOTE} Итог."  # the deload text is kept in front of the model's


def test_refinement_that_names_the_deload_keeps_its_own_text():
    i = inputs(ARMS, deload_until=UNTIL)
    draft = plan.rule_draft(i, PLAN_NOW)
    out = plan.apply_refinement(draft, answer(draft, summary="Разгрузка: берём веса полегче."), i)
    assert out.summary == "Разгрузка: берём веса полегче."


def test_refinement_without_a_deload_is_unchanged():
    i = inputs(ARMS, facts=[("не делаю становую", "training")])
    draft = plan.rule_draft(i, PLAN_NOW)
    out = plan.apply_refinement(draft, answer(draft, items={0: {"weightFactor": 0.8}}, summary="Итог."), i)
    assert not out.deload and out.summary == "Итог." and out.exercises[0].weightFactor == 0.8


def test_refinement_in_a_rest_day_ignores_the_deload():
    i = inputs(ARMS, deload_until=UNTIL, sleep_hours=3)
    draft = plan.rule_draft(i, PLAN_NOW)
    out = plan.apply_refinement(draft, answer(draft, summary="Отдыхай."), i)
    assert all(e.skip for e in out.exercises) and out.summary == "Отдыхай."


async def test_collect_inputs_reads_the_running_deload(db, settings):
    uid = await _user_on_program(db)
    async with db() as s:
        user = await s.get(User, uid)
        assert (await plan.collect_inputs(s, user, settings, MSK, PLAN_NOW)).deload_until is None
        s.add(DeloadState(user_id=uid, started_on=MON, until=MON + timedelta(days=6)))
        await s.commit()
        assert (await plan.collect_inputs(s, user, settings, MSK, PLAN_NOW)).deload_until == MON + timedelta(days=6)
        later = await plan.collect_inputs(s, user, settings, MSK, PLAN_NOW + timedelta(days=2))  # Wed, still on
        assert later.deload_until == MON + timedelta(days=6)
        sunday_after = await plan.collect_inputs(s, user, settings, MSK, PLAN_NOW + timedelta(days=7))  # Mon after
        assert sunday_after.deload_until is None
        st = await s.get(DeloadState, uid)
        st.started_on, st.until = MON - timedelta(days=10), MON - timedelta(days=4)  # over
        await s.commit()
        assert (await plan.collect_inputs(s, user, settings, MSK, PLAN_NOW)).deload_until is None


async def test_get_or_build_follows_the_deload_state(db, settings):
    uid = await _user_on_program(db)
    plain = await _build(db, settings, None)
    assert not plain.out.adjusted and plain.out.exercises == []
    async with db() as s:
        stored = await s.scalar(select(DayPlan.inputs_hash))

    async with db() as s:
        s.add(DeloadState(user_id=uid, started_on=MON, until=MON + timedelta(days=6)))
        await s.commit()
    built = await _build(db, settings, None)
    out = built.out
    assert out.adjusted and out.readiness == "normal"
    assert out.summary == deload.summary(MON + timedelta(days=6)) and out.summary.startswith("Разгрузочная неделя до вс 11.10")
    assert [e.weightFactor for e in out.exercises] == [0.85] * len(ARMS)
    assert [e.sets for e in out.exercises] == [deload.deload_sets(it.sets) for it in ARMS]
    async with db() as s:
        assert await s.scalar(select(func.count()).select_from(DayPlan)) == 1  # the stored plan was replaced
        assert await s.scalar(select(DayPlan.inputs_hash)) != stored  # the rebuild hash differs
    text_ = plan.plan_text(built)
    assert "Разгрузочная неделя до вс 11.10" in text_ and "вес −15 %" in text_

    # cancelled: the next build is the plain program day again
    async with db() as s:
        st = await s.get(DeloadState, uid)
        st.started_on = st.until = None
        await s.commit()
    back = await _build(db, settings, None)
    assert not back.out.adjusted and back.out.exercises == []


# ---- /deload and its buttons ----


@pytest.fixture
def published(monkeypatch):
    calls = []
    monkeypatch.setattr(live, "publish", lambda user_id, *topics: calls.append((user_id, topics)))
    return calls


@pytest.fixture(autouse=True)
def pinned(monkeypatch):
    monkeypatch.setattr(hd, "utcnow", lambda: NOW)


def msg(tg: int = 42):
    return SimpleNamespace(from_user=SimpleNamespace(id=tg, full_name="Amir"), answer=AsyncMock())


def cb(action: str, tg: int = 42):
    return SimpleNamespace(
        data=f"deload:{action}",
        from_user=SimpleNamespace(id=tg, full_name="Amir"),
        message=SimpleNamespace(edit_text=AsyncMock()),
        answer=AsyncMock(),
    )


def buttons(kb) -> list[str]:
    return [b.callback_data for row in kb.inline_keyboard for b in row]


def shown(c) -> str:
    return c.message.edit_text.await_args.args[0]


async def uid_of(db, tg: int = 42) -> int:
    async with db() as s:
        user = await get_or_create_user(s, tg, "Amir")
        await s.commit()
        return user.id


async def test_deload_command_without_a_deload(db, settings):
    m = msg()
    await hd.show_status(m, settings, db)
    assert m.answer.await_args.args[0] == (
        "Разгрузки сейчас нет.\nДля оценки мало тренировок: нужно 3 сессии упражнения за 4 недели."
    )
    assert buttons(m.answer.await_args.kwargs["reply_markup"]) == ["deload:ask"]


async def test_deload_command_shows_what_the_detection_sees(db, settings):
    await stalled_user(db)
    m = msg()
    await hd.show_status(m, settings, db)
    text_ = m.answer.await_args.args[0]
    assert text_.startswith("Разгрузки сейчас нет.\nПохоже, прогресс встал:") and BENCH in text_ and SQUAT in text_


async def test_deload_command_while_running(db, settings):
    uid = await uid_of(db)
    async with db() as s:
        s.add(DeloadState(user_id=uid, started_on=TODAY - timedelta(days=1), until=TODAY + timedelta(days=5)))
        await s.commit()
    m = msg()
    await hd.show_status(m, settings, db)
    assert m.answer.await_args.args[0] == "Идёт разгрузочная неделя до вт 13.10: веса −15 %, подходов меньше."
    assert buttons(m.answer.await_args.kwargs["reply_markup"]) == ["deload:cancel"]


async def test_deload_command_while_scheduled_offers_cancel(db, settings, published):
    uid = await uid_of(db)
    async with db() as s:
        s.add(DeloadState(user_id=uid, started_on=TODAY + timedelta(days=4), until=TODAY + timedelta(days=10)))
        await s.commit()
    m = msg()
    await hd.show_status(m, settings, db)
    assert m.answer.await_args.args[0] == "Разгрузочная неделя запланирована: с пн 12.10 по вс 18.10."
    assert buttons(m.answer.await_args.kwargs["reply_markup"]) == ["deload:cancel"]
    await hd.on_button(cb("cancel"), settings, db)
    st = await state_of(db, uid)
    assert st.started_on is None and st.until is None


async def test_ask_shows_the_confirmation_and_changes_nothing(db, settings, published):
    uid = await uid_of(db)
    c = cb("ask")
    await hd.on_button(c, settings, db)
    assert shown(c) == "Начать разгрузочную неделю до ср 14.10? Веса −15 %, подходов на треть меньше."
    assert buttons(c.message.edit_text.await_args.kwargs["reply_markup"]) == ["deload:yes", "deload:keep"]
    c.answer.assert_awaited_once()
    assert await state_of(db, uid) is None and published == []


async def test_keep_changes_nothing(db, settings, published):
    uid = await uid_of(db)
    c = cb("keep")
    await hd.on_button(c, settings, db)
    assert shown(c) == "Хорошо, без разгрузки." and await state_of(db, uid) is None and published == []


async def test_yes_starts_the_deload(db, settings, published):
    uid = await uid_of(db)
    c = cb("yes")
    await hd.on_button(c, settings, db)
    st = await state_of(db, uid)
    assert (st.started_on, st.until) == (TODAY, TODAY + timedelta(days=6))
    assert shown(c) == (
        "Разгрузочная неделя до ср 14.10 ✅ Веса −15 %, подходов на треть меньше. План на сегодня: /plan"
    )
    assert published == [(uid, ("plan",))]  # today's plan changes
    # a second tap does not restart it
    again = cb("yes")
    await hd.on_button(again, settings, db)
    assert shown(again) == "Разгрузочная неделя уже идёт до ср 14.10."
    assert (await state_of(db, uid)).until == TODAY + timedelta(days=6) and len(published) == 1


@pytest.mark.parametrize(("action", "days", "reply"), [
    ("later", 3, "Хорошо, спрошу через 3 дня."),
    ("no", 14, "Хорошо, не буду предлагать 2 недели. Начать самому: /deload"),
])
async def test_later_and_no_snooze_the_offer(db, settings, published, action, days, reply):
    uid = await uid_of(db)
    c = cb(action)
    await hd.on_button(c, settings, db)
    st = await state_of(db, uid)
    assert aware(st.ask_after) == NOW + timedelta(days=days) and st.started_on is None
    assert shown(c) == reply and published == []


async def test_cancel_clears_a_deload_begun_today(db, settings, published):
    uid = await uid_of(db)
    await hd.on_button(cb("yes"), settings, db)
    published.clear()
    c = cb("cancel")
    await hd.on_button(c, settings, db)
    st = await state_of(db, uid)
    assert st.started_on is None and st.until is None
    assert shown(c) == "Разгрузка отменена, план снова как в программе."
    assert published == [(uid, ("plan",))]
    again = cb("cancel")
    await hd.on_button(again, settings, db)
    assert shown(again) == "Разгрузка сейчас не идёт." and len(published) == 1


async def test_cancel_later_ends_it_yesterday(db, settings):
    uid = await uid_of(db)
    async with db() as s:
        s.add(DeloadState(user_id=uid, started_on=TODAY - timedelta(days=3), until=TODAY + timedelta(days=3)))
        await s.commit()
    await hd.on_button(cb("cancel"), settings, db)
    st = await state_of(db, uid)
    assert st.until == TODAY - timedelta(days=1) and st.started_on == TODAY - timedelta(days=3)


async def test_unknown_button_does_nothing(db, settings, published):
    uid = await uid_of(db)
    c = cb("xyz")
    await hd.on_button(c, settings, db)
    c.answer.assert_awaited_once()
    c.message.edit_text.assert_not_awaited()
    assert await state_of(db, uid) is None and published == []


async def test_plan_command_is_followed_by_the_offer_once(db, settings):
    await stalled_user(db)
    m = msg()
    await hd.offer_after_plan(m, settings, db)
    m.answer.assert_awaited_once()
    text_, kw = m.answer.await_args.args[0], m.answer.await_args.kwargs
    assert "Похоже, прогресс встал:" in text_ and "Сделать разгрузочную неделю?" in text_
    assert buttons(kw["reply_markup"]) == ["deload:yes", "deload:later", "deload:no"]
    again = msg()
    await hd.offer_after_plan(again, settings, db)  # snoozed for three days
    again.answer.assert_not_awaited()


async def test_offer_after_plan_never_raises(db, settings, monkeypatch):
    async def boom(*a, **kw):
        raise RuntimeError("db is gone")

    monkeypatch.setattr(workout_events.deload, "maybe_offer", boom)
    await hd.offer_after_plan(msg(), settings, db)


# ---- workout_events ----


class Outbox:
    def __init__(self, fail: bool = False) -> None:
        self.sent: list[tuple[str, object]] = []
        self.fail = fail

    async def __call__(self, text, kb):
        if self.fail:
            raise RuntimeError("telegram is down")
        self.sent.append((text, kb))


async def test_offer_deload_sends_the_text_and_buttons_once(db, settings):
    uid = await stalled_user(db)
    out = Outbox()
    assert await workout_events.offer_deload(db, uid, out, TZ, NOW, settings.programs_dir) is True
    [(text_, kb)] = out.sent
    assert "Похоже, прогресс встал:" in text_ and buttons(kb) == ["deload:yes", "deload:later", "deload:no"]
    assert await workout_events.offer_deload(db, uid, out, TZ, NOW + timedelta(days=2), settings.programs_dir) is False
    assert len(out.sent) == 1  # within three days
    assert await workout_events.offer_deload(db, uid, out, TZ, NOW + timedelta(days=3), settings.programs_dir) is True
    assert len(out.sent) == 2


async def test_offer_deload_sends_nothing_without_a_reason(db, settings):
    uid = await _user_on_program(db, start=TODAY - timedelta(days=10))
    out = Outbox()
    assert await workout_events.offer_deload(db, uid, out, TZ, NOW, settings.programs_dir) is False
    assert out.sent == []


async def test_offer_deload_survives_a_failing_send(db, settings):
    uid = await stalled_user(db)
    assert await workout_events.offer_deload(db, uid, Outbox(fail=True), TZ, NOW, settings.programs_dir) is False


async def test_after_save_offers_the_deload_unless_asked_not_to(db, settings):
    uid = await stalled_user(db)
    quiet = Outbox()
    await workout_events.after_save(db, uid, [], quiet, TZ, check_deload=False, now=NOW, programs_dir=settings.programs_dir)
    await workout_events.after_save(db, uid, [], None, TZ, now=NOW, programs_dir=settings.programs_dir)  # no sender
    assert quiet.sent == []
    out = Outbox()
    assert await workout_events.after_save(db, uid, [], out, TZ, now=NOW, programs_dir=settings.programs_dir) == []
    [(text_, kb)] = out.sent
    assert "Сделать разгрузочную неделю?" in text_ and kb is not None


# ---- migration 0013 ----


def _schema(conn) -> dict:
    insp = inspect(conn)
    out: dict = {"tables": set(insp.get_table_names())}
    if "deload_states" in out["tables"]:
        out["columns"] = {c["name"]: c["nullable"] for c in insp.get_columns("deload_states")}
        out["pk"] = insp.get_pk_constraint("deload_states")["constrained_columns"]
        out["fks"] = [
            (f["constrained_columns"], f["referred_table"], f["options"].get("ondelete"))
            for f in insp.get_foreign_keys("deload_states")
        ]
    return out


async def test_migration_0013_round_trip(tmp_path):
    engine, _ = make_engine(f"sqlite+aiosqlite:///{tmp_path}/m.db")

    async def run_sync(fn):
        async with engine.begin() as conn:
            return await conn.run_sync(fn)

    async def migrate_to(target: str):
        def fn(conn):
            cfg = migrate._config()
            cfg.attributes["connection"] = conn
            (command.upgrade if target == "head" else command.downgrade)(cfg, target)

        await run_sync(fn)

    try:
        await migrate_to("head")
        up = await run_sync(_schema)
        assert "deload_states" in up["tables"]
        assert up["columns"] == {
            "user_id": False, "started_on": True, "until": True, "ask_after": True, "offered_at": True,
            "updated_at": False,
        }
        assert up["pk"] == ["user_id"]
        assert up["fks"] == [(["user_id"], "users", "CASCADE")]

        async with engine.begin() as conn:
            await conn.execute(text("INSERT INTO users (telegram_id, rest_seconds, created_at) VALUES (1, 90, '2026-10-01 12:00:00')"))
            await conn.execute(text(
                "INSERT INTO deload_states (user_id, started_on, until, updated_at) "
                "VALUES (1, '2026-10-01', '2026-10-07', '2026-10-01 12:00:00')"
            ))
            with pytest.raises(Exception, match="UNIQUE|PRIMARY"):  # one row per user
                await conn.execute(text("INSERT INTO deload_states (user_id, updated_at) VALUES (1, '2026-10-01 12:00:00')"))

        await migrate_to("0012")
        down = await run_sync(_schema)
        assert "deload_states" not in down["tables"]
        assert "products" in down["tables"]  # the previous revision is intact
        async with engine.connect() as conn:
            assert (await conn.execute(text("SELECT telegram_id FROM users"))).scalar_one() == 1

        await migrate_to("head")
        assert await run_sync(_schema) == up
        async with engine.connect() as conn:
            assert (await conn.execute(text("SELECT count(*) FROM deload_states"))).scalar_one() == 0
    finally:
        await engine.dispose()


async def test_yes_on_an_old_offer_keeps_a_scheduled_deload(db, settings, published):
    uid = await uid_of(db)
    first, last = TODAY + timedelta(days=4), TODAY + timedelta(days=10)
    async with db() as s:
        s.add(DeloadState(user_id=uid, started_on=first, until=last))
        await s.commit()
    c = cb("yes")
    await hd.on_button(c, settings, db)
    st = await state_of(db, uid)
    assert (st.started_on, st.until) == (first, last)
    assert shown(c).startswith("Разгрузочная неделя уже запланирована с ") and published == []
