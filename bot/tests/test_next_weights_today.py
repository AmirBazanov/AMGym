"""Today's program day with weights (next_weights.day_weights for today): the day plan's corrections applied
without writing a DayPlan row, the stored plan and its hash, replacements, rest, the owner's override, per-hand
dumbbells; the plain-text sanitizer in the model's answers; advice.recovery_times."""

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select
from test_advice import FakeLLM as AdviceLLM
from test_answer_layers import BAD, context
from test_log_text import FakeLLM
from test_next_weights_day import _owner

from gymbot.db.models import DayPlan, Exercise, User, WeightOverride, WellbeingEntry, Workout, WorkoutSet
from gymbot.llm.prompts import ADVICE_DISCLAIMER
from gymbot.services import advice, answer, answer_direct, next_weights, plan
from gymbot.services.programs import get_or_create_exercise

TZ = ZoneInfo("Europe/Moscow")
FRIDAY_NOW = datetime(2026, 10, 9, 6, tzinfo=UTC)  # Friday 09:00 in Moscow: a training day of the owner's program
FRIDAY = date(2026, 10, 9)
MONDAY_NOW = datetime(2026, 10, 12, 6, tzinfo=UTC)  # Monday of week 2: the dumbbell day, with history from 07.10
MONDAY = date(2026, 10, 12)

EZ_UNDER = "сгибания на бицепс с ez грифом хватом снизу"  # 27,5 kg from the owner's dumbbell curls
SMITH = "жим сидя в смите"
DUMBBELL_PRESS = "жим гантелей сидя"
DUMBBELL_CURL = "сгибания с гантелями на бицепс с супинацией"
# Friday sets by exercise on a normal day, and on a light day (sleep 5 h): a set less, but never below 2.
NORMAL_SETS = [3, 6, 6, 3, 3, 3]
LIGHT_SETS = [2, 5, 5, 2, 2, 2]


async def _sleep(s, user: User, hours: int, now: datetime = FRIDAY_NOW) -> None:
    s.add(WellbeingEntry(user_id=user.id, noted_at=now - timedelta(hours=1), raw_text="текст",
                         sleep_hours=Decimal(hours)))
    await s.commit()


async def _plan_rows(s) -> int:
    return await s.scalar(select(func.count()).select_from(DayPlan))


async def _today(s, user: User, settings, now: datetime = FRIDAY_NOW) -> next_weights.DayWeights:
    return await next_weights.day_weights(s, user, settings, TZ, now, now.astimezone(TZ).date())


async def _stored(s) -> DayPlan:
    return await s.scalar(select(DayPlan))


async def _edit_stored(s, edit) -> list[dict]:
    """Applies `edit` to the stored plan's exercises, keeping the hash (so the row still counts as current)."""
    row = await _stored(s)
    exercises = json.loads(row.exercises_json)
    edit(exercises)
    row.exercises_json = json.dumps(exercises, ensure_ascii=False)
    await s.commit()
    return exercises


# ---- 1. a light day without a stored plan: the rule draft, nothing written ----


async def test_light_day_applies_the_rule_draft_without_writing_a_plan(db, settings):
    async with db() as s:
        user = await _owner(s)
        await _sleep(s, user, 5)
        dw = await _today(s, user, settings)
        assert await _plan_rows(s) == 0
    assert not dw.rest and dw.summary.lower().startswith("спал 5 ч")
    assert [r.sets for r in dw.rows] == LIGHT_SETS
    ez = dw.rows[0].suggestion
    assert (ez.base_weight, ez.factor, ez.weight) == (27.5, 0.9, 25)
    assert ez.weight == next_weights.scale_weight(ez.base_weight, 0.9, 2.5)
    for r in dw.rows:
        s_ = r.suggestion
        if s_.weight is not None:
            assert s_.factor == 0.9
            assert s_.weight == next_weights.scale_weight(s_.base_weight, 0.9, next_weights.equipment_step(r.name))
    assert "по плану дня −10 % от 27,5" in answer_direct.row_line(dw.rows[0])


async def test_normal_day_changes_nothing_and_writes_nothing(db, settings):
    async with db() as s:
        user = await _owner(s)
        dw = await _today(s, user, settings)
        assert await _plan_rows(s) == 0
    assert [r.sets for r in dw.rows] == NORMAL_SETS and dw.summary is None and not dw.rest
    assert all(r.suggestion.factor == 1 and r.suggestion.weight == r.suggestion.base_weight for r in dw.rows)


# ---- 2. the stored plan and its hash ----


async def test_stored_plan_with_the_same_hash_is_used(db, settings):
    async with db() as s:
        user = await _owner(s)
        await _sleep(s, user, 5)
        built = await plan.get_or_build(s, user, settings, None, TZ, FRIDAY_NOW)  # rules only
        assert await _plan_rows(s) == 1
        dw = await _today(s, user, settings)
        assert await _plan_rows(s) == 1
    assert built.out.readiness == "light"
    assert [(r.name, r.sets) for r in dw.rows] == [(e.name, e.sets) for e in built.out.exercises]
    # The factor lands on every row that has a number (a row without one, «французский жим лёжа», has none).
    assert [r.suggestion.factor for r in dw.rows if r.suggestion.weight is not None] == [0.9] * 5
    assert dw.rows[2].suggestion.weight is None
    assert dw.summary == built.out.summary


async def test_stored_plan_beats_the_rule_draft_while_the_hash_matches(db, settings):
    async with db() as s:
        user = await _owner(s)
        await _sleep(s, user, 5)
        await plan.get_or_build(s, user, settings, None, TZ, FRIDAY_NOW)

        def edit(exercises):  # what the model may have decided: a lighter first exercise
            exercises[0].update(sets=1, weightFactor=0.8, reason="модель так решила")

        await _edit_stored(s, edit)
        dw = await _today(s, user, settings)
    first = dw.rows[0]
    assert (first.sets, first.suggestion.factor, first.note) == (1, 0.8, "модель так решила")
    assert first.suggestion.weight == next_weights.scale_weight(27.5, 0.8, 2.5) == 22.5
    assert [r.sets for r in dw.rows[1:]] == LIGHT_SETS[1:]


async def test_stored_plan_with_a_stale_hash_is_ignored(db, settings):
    async with db() as s:
        user = await _owner(s)
        await _sleep(s, user, 5)
        await plan.get_or_build(s, user, settings, None, TZ, FRIDAY_NOW)

        def edit(exercises):
            exercises[0].update(sets=1, weightFactor=0.8)

        await _edit_stored(s, edit)
        (await _stored(s)).inputs_hash = "0" * 64
        await s.commit()
        dw = await _today(s, user, settings)
    first = dw.rows[0]
    assert (first.sets, first.suggestion.factor, first.suggestion.weight) == (2, 0.9, 25)  # the draft's


async def test_plan_stale_after_new_wellbeing_falls_back_to_the_new_draft(db, settings):
    """The inputs changed (a worse night) after the plan was stored: its hash no longer matches."""
    async with db() as s:
        user = await _owner(s)
        await _sleep(s, user, 5)
        await plan.get_or_build(s, user, settings, None, TZ, FRIDAY_NOW)
        s.add(WellbeingEntry(user_id=user.id, noted_at=FRIDAY_NOW - timedelta(minutes=5), raw_text="ещё",
                             sleep_hours=Decimal(3)))
        await s.commit()
        dw = await _today(s, user, settings)
    assert dw.rest  # the draft for 3 h of sleep, not the stored light plan


# ---- 3. a replacement in the stored plan ----


async def test_replacement_in_the_stored_plan(db, settings):
    async with db() as s:
        user = await _owner(s)
        await _sleep(s, user, 5)
        await plan.get_or_build(s, user, settings, None, TZ, FRIDAY_NOW)

        def edit(exercises):
            smith = next(e for e in exercises if e["name"] == SMITH)
            smith["replaceWith"] = "  Жим гантелей сидя "  # the name is trimmed and lowercased

        await _edit_stored(s, edit)
        dw = await _today(s, user, settings)
    row = next(r for r in dw.rows if r.program_name == SMITH)
    assert (row.name, row.program_name) == (DUMBBELL_PRESS, SMITH)
    assert row.suggestion.per_hand and row.suggestion.source == "history"  # its own history: 12,5 × 12 on 07.10
    line = answer_direct.row_line(row)
    assert line.startswith(f"• Жим гантелей сидя (вместо «{SMITH}») 2×8–12 — ")
    assert "кг на руку" in line
    assert [r.program_name for r in dw.rows][3] == SMITH  # the row keeps its place in the day


# ---- 4. rest ----


async def test_rest_readiness_says_rest_and_keeps_the_program_as_written(db, settings):
    async with db() as s:
        user = await _owner(s)
        await _sleep(s, user, 3)
        dw = await _today(s, user, settings)
        assert await _plan_rows(s) == 0
    assert dw.rest
    assert [r.sets for r in dw.rows] == NORMAL_SETS
    assert all(r.suggestion is not None and r.suggestion.factor == 1 for r in dw.rows)
    text = answer_direct.plan_day_text(dw, FRIDAY)
    assert text.split("\n")[1].startswith("План дня советует отдохнуть: Спал 3 ч")
    assert "Если всё же идёшь:" in text
    assert "• Сгибания на бицепс с ez грифом хватом снизу 3×8–12 — 27,5 кг" in text  # the weights are still there


async def test_rest_from_a_stored_plan_with_everything_skipped(db, settings):
    async with db() as s:
        user = await _owner(s)
        await _sleep(s, user, 3)
        built = await plan.get_or_build(s, user, settings, None, TZ, FRIDAY_NOW)
        dw = await _today(s, user, settings)
    assert built.out.readiness == "rest" and all(e.skip for e in built.out.exercises)
    assert dw.rest and all(r.suggestion is not None for r in dw.rows)
    assert "План дня советует отдохнуть" in answer_direct.plan_day_text(dw, FRIDAY)


# ---- 5. the owner's override is used as is ----


async def test_override_for_today_is_not_scaled_on_a_light_day(db, settings):
    async with db() as s:
        user = await _owner(s)
        await _sleep(s, user, 5)
        ex = await s.scalar(select(Exercise).where(Exercise.name == EZ_UNDER))
        s.add(WeightOverride(user_id=user.id, exercise_id=ex.id, day=FRIDAY, weight_kg=Decimal(30)))
        await s.commit()
        dw = await _today(s, user, settings)
    ez, french = dw.rows[0].suggestion, dw.rows[2].suggestion
    assert (ez.source, ez.override, ez.weight, ez.base_weight, ez.factor) == ("override", True, 30, 30, 1.0)
    assert dw.rows[0].sets == 2  # the sets still follow the light day
    assert "ты поставил на сегодня 30 кг" in answer_direct.row_line(dw.rows[0])
    assert "по плану дня" not in answer_direct.row_line(dw.rows[0])
    assert french.factor == 0.9 or french.weight is None  # the others are scaled as before


async def test_override_of_another_day_is_ignored(db, settings):
    async with db() as s:
        user = await _owner(s)
        ex = await s.scalar(select(Exercise).where(Exercise.name == EZ_UNDER))
        s.add(WeightOverride(user_id=user.id, exercise_id=ex.id, day=FRIDAY - timedelta(days=1),
                             weight_kg=Decimal(30)))
        await s.commit()
        dw = await _today(s, user, settings)
    assert (dw.rows[0].suggestion.source, dw.rows[0].suggestion.weight) == ("related", 27.5)


# ---- 6. dumbbells are per hand ----


async def test_dumbbell_exercise_is_per_hand_with_a_footer(db, settings):
    async with db() as s:
        user = await _owner(s)
        dw = await next_weights.day_weights(s, user, settings, TZ, MONDAY_NOW, MONDAY)
    curl = dw.rows[0]
    assert curl.name == DUMBBELL_CURL
    assert curl.suggestion.per_hand and curl.suggestion.weight is not None
    line = answer_direct.row_line(curl)
    assert f"{answer_direct.kg(curl.suggestion.weight)} кг на руку" in line
    text = answer_direct.plan_day_text(dw, MONDAY)
    assert text.splitlines()[0] == "Сегодня, пн 12.10 — тренировка по программе (неделя 2):"
    assert "Гантели — вес одной гантели (на руку)." in text.splitlines()


async def test_no_footer_without_a_dumbbell_weight(db, settings):
    async with db() as s:
        user = await _owner(s)
        dw = await _today(s, user, settings)  # Friday: EZ bar, Smith, cables
    assert not any(r.suggestion.per_hand for r in dw.rows)
    assert "Гантели — вес одной гантели" not in answer_direct.plan_day_text(dw, FRIDAY)


# ---- 7. the sanitizer hooks ----

TABLE_ANSWER = (
    "**Итого:** сегодня 24 подхода.\n\n"
    "| Упражнение | Подходы |\n|---|---|\n| жим гантелей сидя | 3 |\n\n"
    "* держи темп"
)


async def test_checked_answer_sends_plain_text(settings):
    llm = FakeLLM(settings)
    llm.answers = [TABLE_ANSWER]
    reply = await answer.checked_answer(llm.client, context(), "сколько я сделал?")
    assert reply.layer == answer.LLM
    assert "|" not in reply.text and "**" not in reply.text and "---" not in reply.text
    assert reply.text.startswith("Итого: сегодня 24 подхода.")
    assert "• жим гантелей сидя — 3" in reply.text and "• держи темп" in reply.text


async def test_retried_answer_is_plain_too(settings):
    llm = FakeLLM(settings)
    llm.answers = [BAD, "**Ты сделал** 24 подхода."]
    reply = await answer.checked_answer(llm.client, context(), "сколько я сделал?")
    assert (reply.text, reply.layer) == ("Ты сделал 24 подхода.", answer.RETRIED)


ADVICE_ANSWER = "### Питание\n- **добери** 40 г белка\n\n## Сон\n* ложись раньше\n\n" + ADVICE_DISCLAIMER


async def test_advice_generate_strips_markdown_and_keeps_the_disclaimer(db, settings):
    async with db() as s:
        user = await _owner(s)
        text = await advice.generate(s, user, settings, AdviceLLM(ADVICE_ANSWER), TZ, FRIDAY_NOW)
    assert "###" not in text and "##" not in text and "**" not in text
    assert text.startswith("Питание\n- добери 40 г белка\n\nСон\n• ложись раньше")
    assert text.count(ADVICE_DISCLAIMER) == 1 and text.endswith(ADVICE_DISCLAIMER)


async def test_advice_disclaimer_is_added_after_stripping(db, settings):
    async with db() as s:
        user = await _owner(s)
        text = await advice.generate(s, user, settings, AdviceLLM("**Питание**\n| a | b |\n|---|---|\n| x | 1 |"),
                                     TZ, FRIDAY_NOW)
    assert text == f"Питание\n• x — 1\n\n{ADVICE_DISCLAIMER}"


# ---- 8. advice.recovery_times ----


async def _biceps(db, hours_ago: float, sets: int, drops: int = 0):
    """A user with one workout `hours_ago` before FRIDAY_NOW: `sets` main biceps sets (+ drops)."""
    started = FRIDAY_NOW - timedelta(hours=hours_ago)
    async with db() as s:
        user = User(telegram_id=7, name="T", rest_seconds=90)
        s.add(user)
        await s.flush()
        ex = await get_or_create_exercise(s, DUMBBELL_CURL)
        w = Workout(user_id=user.id, performed_on=started.astimezone(TZ).date(), started_at=started, source="chat")
        for i in range(sets):
            w.sets.append(WorkoutSet(exercise_id=ex.id, set_index=i, reps=8, weight_kg=Decimal(20)))
            for d in range(1, drops + 1):
                w.sets.append(WorkoutSet(exercise_id=ex.id, set_index=i, drop_index=d, reps=6, weight_kg=Decimal(15)))
        s.add(w)
        await s.commit()
        return started, await advice.recovery_times(s, user.id, FRIDAY, FRIDAY_NOW)


async def test_recovery_three_sets_ten_hours_ago(db):
    started, got = await _biceps(db, 10, 3)
    assert got == {"бицепс": started + timedelta(hours=48)}


async def test_recovery_two_sets_need_no_rest(db):
    _, got = await _biceps(db, 10, 2)
    assert got == {}


async def test_recovery_drops_are_not_main_sets(db):
    _, got = await _biceps(db, 10, 2, drops=2)
    assert got == {}


@pytest.mark.parametrize("hours", [50, 48])
async def test_recovery_after_48_hours_is_over(db, hours):
    _, got = await _biceps(db, hours, 6)
    assert got == {}


async def test_recovery_just_inside_48_hours(db):
    started, got = await _biceps(db, 47.5, 3)
    assert got == {"бицепс": started + timedelta(hours=48)}


async def test_recovery_sums_sets_of_one_group_and_keeps_the_latest_session(db):
    _, got = await _biceps(db, 30, 2)  # 2 sets alone are not enough
    assert got == {}
    async with db() as s:  # a second session 5 h ago adds 1 set: 3 in 48 h, the end counts from the latest
        user = await s.scalar(select(User).where(User.telegram_id == 7))
        ex = await get_or_create_exercise(s, DUMBBELL_CURL)
        later = FRIDAY_NOW - timedelta(hours=5)
        w = Workout(user_id=user.id, performed_on=later.astimezone(TZ).date(), started_at=later, source="chat")
        w.sets.append(WorkoutSet(exercise_id=ex.id, set_index=0, reps=8, weight_kg=Decimal(20)))
        s.add(w)
        await s.commit()
        got = await advice.recovery_times(s, user.id, FRIDAY, FRIDAY_NOW)
    assert got == {"бицепс": later + timedelta(hours=48)}

