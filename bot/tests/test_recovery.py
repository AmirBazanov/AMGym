"""Recovery-aware advice and answers: the muscle load block counted in code (gymbot.services.advice), the
next program day, muscle group mapping (gymbot.services.plan.muscle_group) and the prompt rules."""

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from test_answer_done import OWNER, _seed

from gymbot.db.models import Program, User, UserProgram, Workout, WorkoutSet
from gymbot.llm.prompts import ADVICE_SYSTEM_PROMPT, ANSWER_SYSTEM_PROMPT
from gymbot.services import advice, answer, answer_check, answer_direct
from gymbot.services.plan import muscle_group
from gymbot.services.programs import get_or_create_exercise, load_program

TZ = ZoneInfo("Europe/Moscow")
DAY = date(2026, 10, 7)  # the owner's arms day (Wednesday); _seed starts it at 14:00 UTC
NEXT_MORNING = datetime(2026, 10, 8, 9, tzinfo=UTC)  # 19 h later, Thursday (a rest day of the program)
ROOT = Path(__file__).resolve().parents[2]

ARMS_LOAD = (
    "Нагрузка по группам мышц за 7 дней (основные подходы): "
    "бицепс — 9 подх., последний раз 07.10 (вчера); трицепс — 6 подх., последний раз 07.10 (вчера); "
    "плечи — 9 подх., последний раз 07.10 (вчера); грудь — 0; спина — 0; ноги — 0.\n"
    "Восстанавливаются (48 ч после тренировки): бицепс, трицепс, плечи — до пт 09.10 17:00. "
    "Отдохнули: грудь, спина, ноги."
)


async def _owner(s) -> User:
    uid = await _seed(s, DAY, OWNER)
    return await s.get(User, uid)


async def _start_program(s, user: User, started_on: date = date(2026, 10, 5)) -> Program:
    program = (await s.scalars(select(Program).order_by(Program.id))).first()
    s.add(UserProgram(user_id=user.id, program_id=program.id, started_on=started_on))
    await s.flush()
    return await load_program(s, program.id)


async def _workout(s, user_id: int, day: date, sets: list[tuple[str, int, int]], hour: int = 14) -> None:
    """`sets`: (exercise, reps, drop_index)."""
    w = Workout(user_id=user_id, performed_on=day, started_at=datetime(day.year, day.month, day.day, hour, tzinfo=UTC))
    for i, (name, reps, drop) in enumerate(sets):
        ex = await get_or_create_exercise(s, name)
        w.sets.append(WorkoutSet(exercise_id=ex.id, set_index=i, reps=reps, drop_index=drop, weight_kg=Decimal(20)))
    s.add(w)
    await s.flush()


# ---- the block ----


async def test_load_block_on_the_owners_arms_day(db):
    async with db() as s:
        user = await _owner(s)
        text = await advice.muscle_load(s, user.id, NEXT_MORNING.date(), NEXT_MORNING, TZ)
    assert text == ARMS_LOAD


async def test_groups_recover_after_48_hours(db):
    async with db() as s:
        user = await _owner(s)
        later = datetime(2026, 10, 9, 14, 30, tzinfo=UTC)  # 48.5 h after the start
        text = await advice.muscle_load(s, user.id, later.date(), later, TZ)
    assert "бицепс — 9 подх., последний раз 07.10 (позавчера)" in text
    assert "Восстанавливаются" not in text


async def test_groups_recovering_at_different_times(db):
    async with db() as s:
        user = await _owner(s)
        await _workout(s, user.id, date(2026, 10, 8), [("присед со штангой", 5, 0)] * 4, hour=6)
        text = await advice.muscle_load(s, user.id, NEXT_MORNING.date(), NEXT_MORNING, TZ)
    assert (
        "Восстанавливаются (48 ч после тренировки): бицепс, трицепс, плечи — до пт 09.10 17:00; "
        "ноги — до сб 10.10 09:00. Отдохнули: грудь, спина." in text
    )


async def test_drops_other_abs_and_old_workouts(db):
    async with db() as s:
        user = User(telegram_id=42, name="Amir", rest_seconds=90)
        s.add(user)
        await s.flush()
        await _workout(s, user.id, DAY, [
            ("жим лёжа", 10, 0), ("жим лёжа", 6, 1), ("жим лёжа", 4, 2),  # one main set, two drops
            ("скручивания", 20, 0), ("скручивания", 20, 0),
            ("берпи", 15, 0),
        ])
        await _workout(s, user.id, DAY - timedelta(days=7), [("присед со штангой", 5, 0)] * 5)  # out of the window
        text = await advice.muscle_load(s, user.id, NEXT_MORNING.date(), NEXT_MORNING, TZ)
    assert "грудь — 1 подх., последний раз 07.10 (вчера)" in text
    assert "пресс — 2 подх." in text and "прочее — 1 подх." in text
    assert "ноги — 0" in text
    assert "Восстанавливаются" not in text  # 1-2 main sets per group need no rest


async def test_no_workouts(db):
    async with db() as s:
        user = User(telegram_id=42, name="Amir", rest_seconds=90)
        s.add(user)
        await s.flush()
        assert await advice.muscle_load(s, user.id, DAY, NEXT_MORNING, TZ) == "Нагрузка по группам мышц за 7 дней: тренировок нет."


async def test_advice_summary_has_the_block_and_the_next_training_day(db, settings):
    async with db() as s:
        user = await _owner(s)
        await _start_program(s, user)
        text = await advice.build_context(s, user, settings, TZ, NEXT_MORNING)
    assert ARMS_LOAD in text
    assert "сегодня день отдыха. Следующая тренировка по программе: пт 09.10 (бицепс, трицепс, плечи): " in text
    assert len(text) <= advice.CONTEXT_MAX


async def test_arms_morning_before_the_programs_arms_evening(db, settings):
    """Friday morning, 43 h after Wednesday's arms: the block says until when, the program day stays."""
    async with db() as s:
        user = await _owner(s)
        await _start_program(s, user)
        friday = datetime(2026, 10, 9, 7, tzinfo=UTC)
        text = await advice.build_context(s, user, settings, TZ, friday)
    assert "бицепс, трицепс, плечи — до пт 09.10 17:00." in text
    assert "сегодня по плану: сгибания" in text
    assert "Следующая тренировка по программе: пн 12.10 (бицепс, трицепс, плечи)." in text  # today is the next session
    assert "прошлый раз" in text  # the exercise lines still fit on a training day
    assert len(text) <= advice.CONTEXT_MAX


async def test_summary_for_the_answer_leaves_out_what_the_answer_counts_itself(db, settings):
    async with db() as s:
        user = await _owner(s)
        await _start_program(s, user)
        monday = datetime(2026, 10, 12, 9, tzinfo=UTC)
        full = await advice.build_context(s, user, settings, TZ, monday)
        short = await advice.build_context(s, user, settings, TZ, monday, for_answer=True)
    assert "прошлый раз" in full and "прошлый раз" not in short
    assert "сегодня по плану:" in full and "сегодня по плану:" not in short
    assert "сегодня день тренировки (план на сегодня ниже). Следующая тренировка по программе: ср 14.10" in short


async def test_next_training_day_rolls_over_and_ends_with_the_program(db):
    async with db() as s:
        user = await _owner(s)
        program = await _start_program(s, user)
        found = advice.next_training_day(program, date(2026, 10, 5), date(2026, 10, 9))  # Friday
        assert found is not None and found[0] == date(2026, 10, 12)  # Monday of week 2
        last = date(2026, 10, 5) + timedelta(weeks=len(program.weeks)) - timedelta(days=3)  # the last Friday
        assert advice.next_training_day(program, date(2026, 10, 5), last) is None


async def test_answer_context_has_the_block_and_points_records_to_the_done_block(db, settings):
    async with db() as s:
        user = await _owner(s)
        ctx = await answer.gather(s, user, settings, None, TZ, NEXT_MORNING)
    assert ARMS_LOAD in ctx.text
    assert "- жим гантелей сидя: последний раз 07.10 (подходы в блоке «Сделано»); рекорд 1ПМ" in ctx.text
    assert ctx.text.count("12,5×12 ×3") == 1  # the sets are listed once, in «Сделано»


def test_records_block_without_the_done_day_keeps_the_sets():
    h = answer_direct.ExerciseHistory("жим лёжа", {DAY: [answer_direct.Set(Decimal(60), 10)]})
    assert "последний раз 07.10: 60×10" in answer_direct.records_block({"жим лёжа": h})
    assert "(подходы в блоке «Сделано»)" in answer_direct.records_block({"жим лёжа": h}, DAY)


async def test_answer_check_takes_the_block_numbers_as_evidence(db, settings):
    async with db() as s:
        user = await _owner(s)
        ctx = await answer.gather(s, user, settings, None, TZ, NEXT_MORNING)
    question = "что подтянуть на следующей тренировке?"
    ev = ctx.evidence(question, [])
    good = (
        "Вчера на бицепс было 9 подх., на трицепс 6 подх., на плечи 9 подх.: они ещё восстанавливаются. "
        "Грудь, спина и ноги за неделю — 0 подходов, их и подтягивай."
    )
    assert answer_check.violations(good, ev) == []
    assert answer_check.violations("Вчера на бицепс было 37 подх.", ev) == ["37 подх"]


# ---- mapping ----


def _program_exercises() -> list[str]:
    names: set[str] = set()
    for path in sorted((ROOT / "data" / "programs").glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        names |= {e["name"] for w in data["weeks"] for d in w["days"] for e in d["exercises"]}
    return sorted(names)


# Program exercises that are meant to count as «прочее» (none so far): anything else must map to a group.
EXPLICITLY_OTHER: set[str] = set()


def test_every_program_exercise_maps_to_a_group():
    names = _program_exercises()
    assert names
    unmapped = [n for n in names if muscle_group(n) is None]
    print("unmapped program exercises:", unmapped)
    assert set(unmapped) == EXPLICITLY_OTHER


@pytest.mark.parametrize(
    ("name", "group"),
    [
        ("сгибания с гантелями на бицепс с супинацией", "biceps"),
        ("сгибания на бицепс с ez грифом хватом снизу", "biceps"),
        ("французский жим лёжа", "triceps"),
        ("разгибания на трицепс в блоке", "triceps"),
        ("отведения на дельты", "shoulders"),
        ("жим гантелей сидя", "shoulders"),
        ("жим сидя в смите", "shoulders"),
        ("жим гантелей над головой", "shoulders"),
        ("жим лёжа 30°", "chest"),
        ("разводка гантелей лёжа", "chest"),
        ("тяга горизонтального блока", "back"),
        ("подтягивания", "back"),
        ("присед в гаке лицом к спинке", "legs"),
        ("выпады с гантелями", "legs"),
        ("жим ногами", "legs"),
        ("румынская тяга", "legs"),
        ("становая тяга", "legs"),
        ("сгибания ног лёжа", "legs"),
        ("скручивания", "abs"),
        ("подъём ног в висе", "abs"),
        ("берпи", None),
    ],
)
def test_muscle_group_mapping(name, group):
    assert muscle_group(name) == group


# ---- prompts ----


@pytest.mark.parametrize("prompt", [ADVICE_SYSTEM_PROMPT, ANSWER_SYSTEM_PROMPT])
def test_prompts_keep_recovering_groups_out_of_the_next_session(prompt):
    assert "«Восстанавливаются»" in prompt and "48 ч" in prompt and "не нагружай" in prompt.replace("не советуй нагружать", "не нагружай")
    assert "«Следующая тренировка по программе»" in prompt and "недогруженн" in prompt


def test_leg_press_is_legs_not_abs():
    from gymbot.services.plan import muscle_group

    assert muscle_group("лег пресс") == "legs"
    assert muscle_group("легпресс") == "legs"
    assert muscle_group("скручивания на пресс") == "abs"


def test_answer_prompt_is_a_coach_that_answers_not_a_retelling():
    p = ANSWER_SYSTEM_PROMPT
    # Gone: the plain-text rule, the 700-character cap and "never cancel the program day for recovery".
    assert "без Markdown" not in p and "700 символов" not in p and "не отменяй" not in p
    # Format: a direct answer first, bullets, a next step, bold only.
    assert "900 символов" in p and "прямой ответ" in p and "«• »" in p and "**жирный**" in p
    assert "Таблицы, заголовки с #, код" in p
    # Soreness or another workout: options, one recommendation, the program day is not insisted on.
    assert "поменять местами с другим днём программы" in p and "−10–20 %" in p and "Не настаивай" in p
    assert "к какому времени" in p and "я ошибся" in p
    # The data rules stay.
    for rule in ("«Веса на … (посчитано дневником)»", "в дневнике этого нет", "«Сделано …»", "«Еда сегодня»",
                 "Диагнозы не ставь", "никогда не пиши, что что-то записал", "«Что сегодня?»", "мини-апп"):
        assert rule in p, rule
