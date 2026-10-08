"""The program day with weights from the database (gymbot.services.next_weights.day_weights) and its
direct diary answer (answer_intent.plan_question, answer_direct.plan_reply): the owner's 07.10 -> 09.10."""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from test_answer_done import OWNER, _seed

from gymbot.db.models import DeloadState, Program, User, UserProgram
from gymbot.services import answer, answer_direct, next_weights
from gymbot.services.answer_intent import plan_question, resolve_day

TZ = ZoneInfo("Europe/Moscow")
NOW = datetime(2026, 10, 8, 10, tzinfo=UTC)  # Thursday 13:00 in Moscow, the day after the arms day
TODAY = date(2026, 10, 8)
FRIDAY = date(2026, 10, 9)

OWNER_FRIDAY = """Завтра, пт 09.10 — тренировка по программе (неделя 1):
• Сгибания на бицепс с ez грифом хватом снизу 3×8–12 — 27,5 кг (по «сгибания с гантелями на бицепс с супинацией» 20×8 на руку: 1ПМ ≈ 25,3 × 2 × 0,85 ≈ 43,1 кг, 64 % для 12 повторов)
• Сгибания на бицепс с ez грифом хватом сверху 6×8–12 — 17,5 кг (по «сгибания с гантелями на бицепс с пронацией» 12,5×8 на руку: 1ПМ ≈ 15,8 × 2 × 0,85 ≈ 26,9 кг, 64 % для 12 повторов)
• Французский жим лёжа 6×8–12 — подбери по ощущениям: вес из «французский жим в блоке из-за головы» (блок) сюда не переносится; начни легко, рабочий вес — с 2 повторами в запасе
• Жим сидя в смите 3×8–12 — 20 кг (по «жим гантелей сидя» 12,5×12 на руку: 1ПМ ≈ 17,5 × 2 × 0,9 ≈ 31,5 кг, 64 % для 12 повторов)
• Отведения на дельты 3× дропсет 12-6-6 — 7,5 кг (как в прошлый раз 7,5 кг)
• Отведения пек дек на заднюю дельту 3×12–15 — 50 кг (как в прошлый раз 50 кг)
Бицепс, трицепс, плечи восстановятся к пт 09.10 17:00 (48 ч после тренировки) — тренировка после 17:00 в самый раз."""


async def _owner(s, started_on: date = date(2026, 10, 5)) -> User:
    uid = await _seed(s, date(2026, 10, 7), OWNER)  # 07.10 14:00 UTC = 17:00 in Moscow
    program = await s.scalar(select(Program).order_by(Program.id).limit(1))
    s.add(UserProgram(user_id=uid, program_id=program.id, started_on=started_on))
    await s.commit()
    return await s.get(User, uid)


class NoModel:
    """The direct answer never calls the model."""

    def __getattr__(self, name):
        raise AssertionError(f"the model was called: {name}")


@pytest.mark.parametrize(
    "question",
    ["какая завтра тренировка и какие веса", "Какая тренировка в пятницу?", "какие веса 9 числа", "что у меня завтра?"],
)
async def test_owner_friday_is_answered_in_code_the_same_every_time(db, settings, question):
    async with db() as s:
        await _owner(s)
    texts = {
        (await answer.respond(db, 42, "Amir", question, [], settings, NoModel(), NOW)).text for _ in range(3)
    }
    assert texts == {OWNER_FRIDAY}


async def test_owner_friday_weights_are_sane(db, settings):
    async with db() as s:
        user = await _owner(s)
        dw = await next_weights.day_weights(s, user, settings, TZ, NOW, FRIDAY)
    got = {r.name: (r.suggestion.weight, r.suggestion.source) for r in dw.rows}
    assert got == {
        "сгибания на бицепс с ez грифом хватом снизу": (27.5, "related"),
        "сгибания на бицепс с ez грифом хватом сверху": (17.5, "related"),
        "французский жим лёжа": (None, "none"),
        "жим сидя в смите": (20, "related"),
        "отведения на дельты": (7.5, "history"),
        "отведения пек дек на заднюю дельту": (50, "history"),
    }
    # Never the per-hand number as a bar weight (the incident: Smith 12,5 kg from 12,5 kg dumbbells).
    assert got["жим сидя в смите"][0] > 2 * 12.5 * 0.7


async def test_no_markdown_no_unrelated_lifts_no_cancelled_day():
    text = OWNER_FRIDAY
    assert "|" not in text and "**" not in text and "жим лёжа 90" not in text
    assert "не нужна" not in text and "вдвое" not in text


async def test_exercise_question_answers_its_next_program_day(db, settings):
    async with db() as s:
        await _owner(s)
    reply = await answer.respond(db, 42, "Amir", "с каким весом работать в смите?", [], settings, NoModel(), NOW)
    assert reply.layer == answer.DIRECT
    lines = reply.text.split("\n")
    assert lines[0] == "Завтра, пт 09.10 — тренировка по программе (неделя 1):"
    assert lines[1].startswith("• Жим сидя в смите 3×8–12 — 20 кг") and len(lines) == 3  # + the recovery note


async def test_rest_day_points_to_the_next_training_day(db, settings):
    async with db() as s:
        user = await _owner(s)
        text = await answer_direct.plan_reply(s, user, settings, "какая тренировка в четверг", plan_question(
            "какая тренировка в четверг"), TZ, NOW)
    assert text.startswith("Сегодня, чт 08.10 по программе отдых. Ближайшая тренировка:\nЗавтра, пт 09.10")


async def test_deload_on_a_future_day_lightens_weights_and_sets(db, settings):
    async with db() as s:
        user = await _owner(s)
        s.add(DeloadState(user_id=user.id, started_on=TODAY, until=TODAY + timedelta(days=6)))
        await s.commit()
        dw = await next_weights.day_weights(s, user, settings, TZ, NOW, FRIDAY)
    ez = dw.rows[0]
    assert dw.summary.startswith("Разгрузочная неделя")
    assert (ez.sets, ez.suggestion.base_weight, ez.suggestion.weight) == (2, 27.5, 22.5)
    assert "по плану дня −15 % от 27,5" in answer_direct.row_line(ez)


async def test_model_context_gets_the_weights_block_for_the_asked_day(db, settings):
    async with db() as s:
        user = await _owner(s)
        block = await answer.weights_block(s, user, settings, TZ, NOW, "а что по весам на пятницу, расскажи")
    head, *rows = block.split("\n")
    assert head == "Веса на завтра, пт 09.10 (посчитано дневником, гантели — на руку):"
    assert rows[0].startswith("• Сгибания на бицепс с ez грифом хватом снизу 3×8–12 — 27,5 кг")


@pytest.mark.parametrize(
    ("text", "day"),
    [
        ("какая завтра тренировка", date(2026, 10, 9)),
        ("что сегодня по плану", date(2026, 10, 8)),
        ("какие веса в понедельник", date(2026, 10, 12)),
        ("какая тренировка в чт", date(2026, 10, 8)),
        ("что у меня 3 числа", date(2026, 11, 3)),
        ("какие упражнения 9.10", date(2026, 10, 9)),
        ("какие веса послезавтра", date(2026, 10, 10)),
    ],
)
def test_plan_question_days(text, day):
    q = plan_question(text)
    assert q is not None and resolve_day(q.when, TODAY) == day


@pytest.mark.parametrize(
    "text",
    [
        "как облегчить завтрашнюю тренировку", "сколько подходов делать завтра", "можно ли завтра тренироваться",
        "какой вес был 7.10", "что я ел 5 числа", "сколько белка завтра", "завтра пойду в зал", "привет",
        "почему завтра руки",
    ],
)
def test_plan_question_leaves_advice_and_the_past_to_others(text):
    assert plan_question(text) is None


def test_past_date_with_month_is_not_the_plan():
    q = plan_question("какие веса 7.10")
    assert q is not None and resolve_day(q.when, TODAY) is None


@pytest.mark.parametrize(
    "text",
    [
        "какой у меня вес сегодня", "какой вес тела сегодня", "какой вес сегодня", "сколько я вешу сегодня",
        "как прошла тренировка сегодня", "что на завтрак", "что съесть на завтрак завтра",
    ],
)
def test_plan_question_review_false_positives(text):
    assert plan_question(text) is None


@pytest.mark.parametrize("text", ["какие веса 12.5", "какие веса 7.5 кг", "что у меня 12.5"])
def test_weights_are_not_dates(text):
    q = plan_question(text)
    assert q is None or q.when is None


def test_working_weight_questions_still_count():
    for text in ("какой вес ставить завтра", "какой рабочий вес завтра", "с каким весом завтра", "какие веса завтра"):
        q = plan_question(text)
        assert q is not None and q.weights and q.when == ("offset", 1), text
    assert plan_question("какая завтрашняя тренировка").when == ("offset", 1)


async def test_next_training_is_today_until_trained(db, settings):
    friday_morning = datetime(2026, 10, 9, 5, tzinfo=UTC)  # 08:00 in Moscow, Friday is a program day
    async with db() as s:
        await _owner(s)
    reply = await answer.respond(db, 42, "Amir", "какая следующая тренировка", [], settings, NoModel(), friday_morning)
    assert reply.text.startswith("Сегодня, пт 09.10 — тренировка по программе")
