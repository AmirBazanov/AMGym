"""Review fixes of the diary answer layers: routing around records, history names over synonyms, food
advice in the check, drop sets, "last time" mid-workout, bodyweight sets, rounding."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from test_answer_done import OWNER, _seed
from test_log_text import T0, FakeLLM, send

from gymbot.db.models import User, Workout, WorkoutSet
from gymbot.handlers import log_text
from gymbot.llm.schemas import ParseResult
from gymbot.services import answer, answer_check, answer_direct
from gymbot.services.answer_intent import classify, synonym_targets
from gymbot.services.programs import get_or_create_exercise

TODAY = date(2026, 10, 7)
NOW = datetime(2026, 10, 7, 16, tzinfo=UTC)
MSK = ZoneInfo("Europe/Moscow")


@pytest.fixture
def llm(settings):
    return FakeLLM(settings)


@pytest.fixture
def diary():
    log_text.QA.clear()
    yield
    log_text.QA.clear()


async def ask(db, question: str, user_id: int) -> str | None:
    async with db() as s:
        user = await s.get(User, user_id)
        q = classify(question)
        assert q is not None, question
        return await answer_direct.reply(s, user, question, q, MSK, NOW)


# ---- 1. a record in the message keeps the parser's clarification ----


@pytest.mark.parametrize(
    ("text", "parsed"),
    [
        ("съел курт сколько белка осталось", {"kind": "food", "foods": [], "unknown_terms": ["курт"]}),
        ("поел курицу сколько белка осталось", {"kind": "unknown", "clarification": "Сколько грамм курицы?"}),
        ("сделал жим как в прошлый раз 3 подхода по 8", {"kind": "unknown", "clarification": "С каким весом?"}),
        ("сделал жим как в прошлый раз 3 подхода по 8", {"kind": "question", "clarification": "С каким весом?"}),
        # voice: no punctuation at all
        ("ну вот поел гречку с курицей сколько калорий осталось", {"kind": "unknown", "clarification": "Сколько?"}),
        ("съел 200 г творога сколько белка осталось", {"kind": "question", "clarification": "-"}),
    ],
)
def test_a_record_in_the_message_is_not_a_factual_question(text, parsed):
    assert log_text._factual_question(text, ParseResult.model_validate(parsed), None) is False


@pytest.mark.parametrize(
    ("text", "parsed"),
    [
        ("сколько белка осталось", {"kind": "question", "clarification": "-"}),
        ("что я сегодня делал", {"kind": "unknown", "clarification": "Уточни."}),
        ("сколько я съел", {"kind": "question", "clarification": "-"}),
    ],
)
def test_plain_factual_questions_still_go_to_the_database(text, parsed):
    assert log_text._factual_question(text, ParseResult.model_validate(parsed), None) is True


async def test_clarification_about_food_stays_and_the_answer_continues_the_record(llm, settings, db, diary):
    llm.answers = [
        {"kind": "unknown", "clarification": "Сколько грамм курицы?"},
        {"kind": "food", "foods": [{"description": "курица, 200 г", "grams": 200, "kcal": 330, "protein_g": 62,
                                    "fat_g": 7, "carbs_g": 0}]},
    ]
    msg = await send("поел курицу сколько белка осталось", llm, settings, db)
    assert msg.answer.await_args.args[0] == "Сколько грамм курицы?"
    assert len(llm.bodies) == 1 and log_text.QA == {}
    second = await send("200 г", llm, settings, db, T0 + timedelta(minutes=1))
    assert llm.had_history()  # the parser saw its own question: "200 г" is not orphaned
    assert "Сохранить" in str(second.answer.await_args.kwargs.get("reply_markup"))


async def test_clarification_about_sets_stays(llm, settings, db, diary):
    llm.answers = [{"kind": "unknown", "clarification": "С каким весом?"}]
    msg = await send("сделал жим как в прошлый раз 3 подхода по 8", llm, settings, db)
    assert msg.answer.await_args.args[0] == "С каким весом?"
    assert len(llm.bodies) == 1


# ---- 2. history names win over synonyms ----


@pytest.mark.parametrize(
    ("history", "question", "expected"),
    [
        (["румынская тяга с гантелями"], "какой рекорд в румынке", ["Румынская тяга с гантелями: рекорд"]),
        (["приседания со штангой"], "сколько я приседал в прошлый раз", ["Приседания со штангой: последний раз"]),
        (["жим штанги лёжа", "жим лёжа на наклонной скамье"], "рекорд в жиме лёжа",
         ["Жим штанги лёжа: рекорд", "Жим лёжа на наклонной скамье: рекорд"]),
    ],
)
async def test_history_names_win_over_synonyms(db, history, question, expected):
    async with db() as s:
        uid = await _seed(s, TODAY - timedelta(days=2), [(n, [(40, 10), (40, 8)]) for n in history])
        await s.commit()
    text = await ask(db, question, uid)
    lines = text.split("\n")
    assert [line[: len(e)] for line, e in zip(lines, expected, strict=False)] == expected
    assert "подходов нет" not in text


async def test_hack_squat_is_not_the_barbell_squat(db):
    assert synonym_targets("гакк-приседания") == []
    async with db() as s:
        uid = await _seed(s, TODAY - timedelta(days=2), [("гакк-приседания", [(100, 10)])])
        await s.commit()
    text = await ask(db, "мой рекорд в приседе", uid)
    assert text.startswith("Присед со штангой: в истории подходов нет.")
    assert "гакк" not in text.lower()


def test_check_accepts_a_synonym_of_a_history_name():
    ev = answer_check.Evidence("румынская тяга с гантелями: 40×10, 40×8", {"румынская тяга с гантелями"},
                               ["румынская тяга с гантелями", "румынская тяга", "жим лёжа"])
    assert answer_check.violations("В прошлый раз в румынке было 40×10.", ev) == []
    assert answer_check.violations("В прошлый раз жим лёжа был 40×10.", ev) == ["жим лёжа"]


# ---- 3. food advice is not a claim about the past ----

FOOD = (
    "Еда сегодня (07.10), из дневника: 1357 ккал, Б 87 г; норма 2500 ккал, Б 150 г; "
    "до нормы осталось: 1143 ккал, Б 63 г. Белка до нормы 62,6 г. 1ПМ по Эпли 101 кг."
)


@pytest.mark.parametrize(
    "text",
    [
        "Осталось 63 г белка, это примерно 250 г творога",
        "До нормы осталось 1143 ккал: на ужин 150 г риса и 200 г курицы",
        "Сегодня ты съел 1357 ккал, белка не хватает 63 г, так что на ужин 200 г курицы",
        "Белка осталось около 60 г",
        "Рекорд 1ПМ 101 кг, рабочий 75% ≈ 76 кг",
        "Осталось 1143 ккал: съешь творог 200 г или перекуси орехами 30 г",
    ],
)
def test_food_advice_passes(text):
    assert answer_check.violations(text, answer_check.Evidence(FOOD, set())) == []


def test_unhedged_wrong_food_number_is_still_flagged():
    assert answer_check.violations("Белка осталось 60 г", answer_check.Evidence(FOOD, set())) == ["60 г"]


# ---- 4. the fallback topic ----


def test_fallback_is_not_food_for_words_with_ed_inside():
    ctx = answer.Context(text="", done="DONE", food="FOOD", history={})
    assert answer.fallback(ctx, "что подтянуть на следующей тренировке в среду?", ["1800 кг"]).endswith("DONE")
    assert answer.fallback(ctx, "какая еда сегодня?", []).endswith("FOOD")


# ---- 5. drop sets and 7. bodyweight sets ----


async def _seed_rows(s, day: date, rows) -> int:
    """rows: (exercise, weight or None, reps, drop_index) in order."""
    user = User(telegram_id=42, name="Amir", rest_seconds=90)
    s.add(user)
    await s.flush()
    w = Workout(user_id=user.id, performed_on=day, started_at=datetime.combine(day, datetime.min.time(), UTC),
                source="miniapp")
    for i, (name, weight, reps, drop) in enumerate(rows):
        ex = await get_or_create_exercise(s, name)
        w.sets.append(WorkoutSet(exercise_id=ex.id, set_index=i, reps=reps, drop_index=drop,
                                 weight_kg=None if weight is None else Decimal(str(weight))))
    s.add(w)
    await s.commit()
    return user.id


async def test_drops_fold_into_their_set_and_count_in_the_tonnage_only(db):
    async with db() as s:
        uid = await _seed_rows(s, TODAY, [
            ("разгибания на трицепс", 30, 10, 0), ("разгибания на трицепс", 30, 10, 0),
            ("разгибания на трицепс", 30, 10, 0), ("разгибания на трицепс", 20, 8, 1),
            ("разгибания на трицепс", None, 6, 2),
        ])
        done = await answer.done_block(s, uid, TODAY)
    text = await ask(db, "сколько сегодня тоннаж?", uid)
    assert text.split("\n") == [
        "Сегодня: 1 упражнение, 3 подхода, тоннаж 1060 кг.",
        "- разгибания на трицепс: 30×10 ×2, 30×10 → 20×8 → 6 повт. (1060 кг)",
    ]
    assert done.startswith("Сделано сегодня, из истории: 1 упр., 3 подх., тоннаж 1060 кг.")


async def test_bodyweight_exercise_has_no_zero_tonnage_and_a_reps_record(db):
    async with db() as s:
        uid = await _seed_rows(s, TODAY - timedelta(days=1),
                               [("подтягивания", None, 10, 0), ("подтягивания", None, 8, 0)])
    text = await ask(db, "мой рекорд в подтягиваниях", uid)
    assert text == "Подтягивания: рекорд — больше всего 10 повторов в подходе (06.10); последний раз 06.10: 10 повт., 8 повт."
    assert "0 кг" not in text and ".." not in text
    last = await ask(db, "сколько я подтягивался в прошлый раз в подтягиваниях", uid)
    assert ".." not in last and "(0 кг)" not in last


# ---- 6. "last time" mid-workout ----


async def test_last_time_is_the_day_before_today_mid_workout(db):
    async with db() as s:
        uid = await _seed(s, TODAY - timedelta(days=3), [OWNER[2]])
        w = Workout(user_id=uid, performed_on=TODAY, started_at=NOW, source="miniapp")
        ex = await get_or_create_exercise(s, OWNER[2][0])
        w.sets.append(WorkoutSet(exercise_id=ex.id, set_index=0, reps=10, weight_kg=Decimal(65)))
        s.add(w)
        await s.commit()
    last = await ask(db, "сколько я делал во французском жиме в прошлый раз", uid)
    assert "последний раз 04.10: 60×10 ×3, 60×8, 50×10, 40×10 (3180 кг)" in last
    today = await ask(db, "сколько я сегодня делал во французском жиме", uid)
    assert "последний раз 07.10: 65×10 (650 кг)" in today
    yesterday = await ask(db, "сколько я вчера делал во французском жиме", uid)
    assert yesterday.startswith("Французский жим в блоке из-за головы: вчера подходов нет; последний раз 07.10")
    previous = await ask(db, "что было на прошлой тренировке", uid)
    assert previous.startswith("Прошлая тренировка — 04.10: 1 упражнение, 6 подходов, тоннаж 3180 кг.")


# ---- 7. rounding ----


@pytest.mark.parametrize(("x", "digits", "expected"), [(12.5, 1, "13"), (10.5, 0, "11"), (0.25, 1, "0,3"),
                                                       (62.6, 1, "63"), (7.0, 1, "7")])
def test_num_rounds_half_up(x, digits, expected):
    assert answer_direct.num(x, digits) == expected
