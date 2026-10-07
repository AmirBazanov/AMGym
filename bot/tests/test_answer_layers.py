"""Anti-hallucination layers of the diary answer: factual questions answered in code (answer_intent,
answer_direct), the post-check of the model's answer (answer_check) and the layered flow (answer)."""

import logging
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from test_answer_done import OWNER, TODAY, _seed
from test_log_text import FakeLLM, buttons, food, send

from gymbot.db.models import FoodEntry, User
from gymbot.handlers import log_text
from gymbot.services import answer, answer_check, answer_direct, answer_intent, nutrition
from gymbot.services.answer_intent import QUESTION_WORDS, Intent, classify, head_matches, mentions

NOW = datetime(2026, 10, 7, 16, tzinfo=UTC)
TZ = ZoneInfo("Europe/Moscow")
USER = 42
OWNER_NAMES = [n for n, _ in OWNER]
CATALOG = [*OWNER_NAMES, "жим лёжа", "присед со штангой"]
FRENCH = "французский жим в блоке из-за головы"
DUMBBELL_PRESS = "жим гантелей сидя"


# ---- 1. classify ----


@pytest.mark.parametrize(
    ("text", "intent", "day", "record", "listing"),
    [
        ("Сколько сегодня по тоннажу?", Intent.WORKOUT, "today", False, False),
        ("что я сегодня делал", Intent.WORKOUT, "today", False, False),
        ("сколько подходов", Intent.WORKOUT, "last", False, False),
        ("как потренил", Intent.WORKOUT, "last", False, False),
        ("что было на прошлой тренировке", Intent.WORKOUT, "last", False, False),
        ("сколько я съел", Intent.FOOD, "today", False, False),
        ("сколько белка осталось", Intent.FOOD, "today", False, False),
        ("сколько калорий сегодня", Intent.FOOD, "today", False, False),
        ("что я вчера ел?", Intent.FOOD, "yesterday", False, True),
        ("сколько я жал в жиме лёжа в прошлый раз", Intent.EXERCISE, "last", False, False),
        ("мой рекорд в приседе", Intent.EXERCISE, "last", True, False),
        ("какой у меня 1пм в жиме", Intent.EXERCISE, "last", True, False),
    ],
)
def test_classify_factual_questions(text, intent, day, record, listing):
    q = classify(text)
    assert q is not None, text
    assert (q.intent, q.day, q.record, q.listing) == (intent, day, record, listing)


@pytest.mark.parametrize(
    "text",
    [
        "как думаешь, что подтянуть на следующей тренировке?",
        "сколько калорий в шашлыке?",
        "что у меня сегодня?",
        "что сегодня по плану",
        "сколько подходов делать в жиме?",
        "с каким весом жать?",
        "какой у меня сейчас рабочий вес в жиме?",
        "сколько мне нужно белка в день",
        "привет",
        "тоннаж за неделю?",
    ],
)
def test_classify_leaves_advice_and_plan_to_the_model(text):
    assert classify(text) is None


def test_classify_workout_today_is_a_workout_question_not_an_exercise_one():
    # "тоннаж" must not be taken for an exercise record even with "сегодня" and "жим" around.
    q = classify("сколько сегодня по тоннажу?")
    assert q is not None and q.intent is Intent.WORKOUT and not q.record


# ---- 2. mentions / head_matches ----

NAMES = [*OWNER_NAMES, "жим лёжа", "присед со штангой"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("в жиме лёжа", ["жим лёжа"]),
        ("в приседе", ["присед со штангой"]),
        ("Сегодня ты сделал жим лёжа 3×10 по 60 кг", ["жим лёжа"]),
        ("жим гантелей сидя и разводки лёжа", [DUMBBELL_PRESS]),
    ],
)
def test_mentions_finds_inflected_names(text, expected):
    assert mentions(text, NAMES) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("во французском жиме", [FRENCH]),
        ("в жиме", [FRENCH, DUMBBELL_PRESS]),
        ("в отведениях", ["отведения на дельты", "отведения пек дек на заднюю дельту"]),
    ],
)
def test_head_matches_loose_head_word(text, expected):
    assert head_matches(text, OWNER_NAMES, QUESTION_WORDS) == expected


# ---- 3. deterministic replies ----


async def _user(s, targets: bool = True) -> User:
    user = User(telegram_id=USER, name="Amir", rest_seconds=90)
    if targets:
        user.kcal_target, user.protein_target_g, user.fat_target_g, user.carbs_target_g = 2500, 150, 80, 300
    s.add(user)
    await s.flush()
    return user


async def _eat(s, user_id: int) -> None:
    for hour, desc, kcal, p, f, c in ((6, "овсянка", 350, 12, 7, 60), (10, "курица с рисом", 650, 48, 15, 80)):
        s.add(FoodEntry(
            user_id=user_id, eaten_at=datetime(2026, 10, 7, hour, tzinfo=UTC), description=desc,
            kcal=kcal, protein_g=p, fat_g=f, carbs_g=c,
        ))
    await s.flush()


async def ask(db, text: str, exercises=None, day: date = TODAY, eat: bool = False, targets: bool = True):
    """The deterministic reply to `text` for user 42 with the given workout / food, at the fixed clock."""
    async with db() as s:
        if exercises:
            uid = await _seed(s, day, exercises)
            user = await s.get(User, uid)
            if targets:
                user.kcal_target, user.protein_target_g, user.fat_target_g, user.carbs_target_g = 2500, 150, 80, 300
        else:
            user = await _user(s, targets)
        if eat:
            await _eat(s, user.id)
        await s.commit()
        q = classify(text)
        assert q is not None, text
        return await answer_direct.reply(s, user, text, q, TZ, NOW)


async def test_direct_workout_today(db):
    text = await ask(db, "Сколько сегодня по тоннажу?", OWNER)
    lines = text.split("\n")
    assert lines[0] == "Сегодня: 6 упражнений, 24 подхода, тоннаж 6530 кг."
    assert len(lines) == 7
    assert "- французский жим в блоке из-за головы: 60×10 ×3, 60×8, 50×10, 40×10 (3180 кг)" in lines


async def test_direct_workout_yesterday_is_empty_and_shows_the_last_day(db):
    text = await ask(db, "что я вчера делал", OWNER)
    assert text.startswith(
        "Вчера (06.10) в истории тренировки нет.\n"
        "Последняя тренировка — сегодня: 6 упражнений, 24 подхода, тоннаж 6530 кг."
    )


async def test_direct_workout_today_empty_shows_an_older_day(db):
    text = await ask(db, "сколько сегодня тоннаж?", OWNER[:1], day=TODAY - timedelta(days=2))
    assert text.startswith(
        "Сегодня в истории тренировки нет.\n"
        "Последняя тренировка — 05.10: 1 упражнение, 6 подходов, тоннаж 880 кг."
    )


async def test_direct_workout_without_any_history(db):
    assert await ask(db, "как потренил?") == "Тренировок в истории пока нет."


async def test_direct_exercise_never_done_is_not_made_up(db):
    text = await ask(db, "сколько я жал в жиме лёжа в прошлый раз", OWNER)
    assert text == "Жим лёжа: в истории подходов нет.\nПодходы можно записать, просто написав их в чат."


async def test_direct_exercise_last_time_and_record(db):
    text = await ask(db, "во французском жиме сколько я делал в прошлый раз", OWNER)
    assert text == (
        "Французский жим в блоке из-за головы: последний раз 07.10: 60×10 ×3, 60×8, 50×10, 40×10 (3180 кг). "
        "Рекорд: 1ПМ по Эпли 80 кг (60×10, 07.10)."
    )


async def test_direct_one_rm_for_a_bare_head_word_lists_every_matching_exercise(db):
    lines = (await ask(db, "какой у меня 1пм в жиме", OWNER)).split("\n")
    assert len(lines) == 2
    assert lines[0].startswith("Французский жим в блоке из-за головы: рекорд — 1ПМ по Эпли 80 кг")
    assert lines[1].startswith("Жим гантелей сидя: рекорд — 1ПМ по Эпли 17,5 кг (12,5×12, 07.10); ")


async def test_direct_unknown_exercise_goes_to_the_model(db):
    assert await ask(db, "сколько я делал в прошлый раз в жиме ногами", OWNER) is None


async def test_direct_records_without_an_exercise(db):
    text = await ask(db, "какой мой рекорд?", OWNER)
    assert text.startswith("Рекорды из истории (1ПМ по Эпли):")
    assert len([ln for ln in text.split("\n") if ln.startswith("- ")]) == 6


async def test_direct_food_protein_left(db):
    text = await ask(db, "сколько белка осталось", eat=True)
    assert text.split("\n")[0] == "Белка осталось 90 г: съедено 60 из 150 г."


async def test_direct_food_totals_and_norm(db):
    text = await ask(db, "сколько я съел", eat=True)
    assert "Сегодня съедено: 1000 ккал, Б 60 г, Ж 22 г, У 140 г (2 записи)." in text
    assert "До нормы осталось: 1500 ккал, Б 90 г, Ж 58 г, У 160 г." in text


async def test_direct_food_listing_uses_local_time(db):
    text = await ask(db, "что я сегодня ел", eat=True)
    assert "- 09:00 овсянка, 350 ккал" in text  # 06:00 UTC is 09:00 in Moscow
    assert "- 13:00 курица с рисом, 650 ккал" in text


async def test_direct_food_without_targets(db):
    text = await ask(db, "сколько я съел", eat=True, targets=False)
    assert "Норма КБЖУ не задана." in text  # a sentence of its own in the reply
    assert "До нормы" not in text


async def test_direct_food_without_entries(db):
    text = await ask(db, "сколько я съел")
    assert text.startswith("Сегодня еды в дневнике нет; норма ")


async def test_food_block_without_targets_or_entries(db):
    async with db() as s:
        user = await _user(s, targets=False)
        block = answer_direct.food_block(await nutrition.day_summary(s, user, TODAY, TZ))
    assert block == "Еды сегодня (07.10) в дневнике нет; норма КБЖУ не задана."


async def test_build_context_has_the_counted_blocks(db, settings):
    async with db() as s:
        uid = await _seed(s, TODAY, OWNER)
        await _eat(s, uid)
        await s.commit()
        user = await s.get(User, uid)
        text = await answer.build_context(s, user, settings, None, TZ, NOW)
    assert "Еда сегодня (07.10)" in text
    assert "Последний раз и рекорды по упражнениям, вся история:" in text
    assert "Сделано сегодня, из истории: 6 упр., 24 подх., тоннаж 6530 кг." in text


# ---- 4. the checker ----

DONE = (
    "Сделано сегодня, из истории: 6 упр., 24 подх., тоннаж 6530 кг.\n"
    "- сгибания с гантелями на бицепс с супинацией: 20×8 ×3, 17,5×8 ×2, 15×8 (880 кг)\n"
    "- сгибания с гантелями на бицепс с пронацией: 12,5×8, 10×10 ×2 (300 кг)\n"
    "- французский жим в блоке из-за головы: 60×10 ×3, 60×8, 50×10, 40×10 (3180 кг)\n"
    "- жим гантелей сидя: 12,5×12 ×3 (450 кг)\n"
    "- отведения на дельты: 7,5×15 ×2, 7,5×10 (300 кг)\n"
    "- отведения пек дек на заднюю дельту: 35×12, 50×10 ×2 (1420 кг)"
)
FOOD = (
    "Еда сегодня (07.10), из дневника: 1450 ккал, Б 92 г, Ж 50 г, У 150 г; норма 2500 ккал, Б 150 г; "
    "до нормы осталось: 1050 ккал, Б 58 г."
)
PLAN = "План на сегодня: жим лёжа 4×8–10"
SUMMARY = f"{DONE}\n{FOOD}\n{PLAN}"


def evidence() -> answer_check.Evidence:
    return answer_check.Evidence(SUMMARY, set(OWNER_NAMES), list(CATALOG))


async def test_done_block_text_used_as_evidence_is_the_real_one(db):
    async with db() as s:
        uid = await _seed(s, TODAY, OWNER)
        assert await answer.done_block(s, uid, TODAY) == DONE


@pytest.mark.parametrize(
    ("text", "flags"),
    [
        ("Сегодня ты сделал жим лёжа 3×10 по 60 кг, тоннаж 1800 кг.", ["жим лёжа", "1800 кг"]),
        ("Сегодня: жим лёжа 3×10 по 60 кг, тоннаж 1800 кг.", ["жим лёжа", "1800 кг"]),
        ("Сегодня ты съел 1450 ккал, белка осталось 70 г.", ["70 г"]),
    ],
)
def test_violations_flags_made_up_claims(text, flags):
    assert answer_check.violations(text, evidence()) == flags


def test_violations_flags_a_made_up_list():
    flags = answer_check.violations("Сегодня ты сделал:\n- жим лёжа: 3×10 по 60 кг\n- присед 100×5", evidence())
    assert "жим лёжа" in flags
    assert "присед со штангой" in flags
    assert "100×5" in flags


@pytest.mark.parametrize(
    "text",
    [
        "Сегодня по плану жим 4×8–10, ставь 82,5",
        "Сегодня по плану жим лёжа 4×8–10, ставь 82,5 кг.",
        "Ты сделал 24 подхода, тоннаж 6530 кг.",
        "Ты сделал 24 подхода, тоннаж 6 530 кг.",
        "Тоннаж около 6,5 т за 24 подхода.",
        "Сегодня ты съел 1450 ккал, белка осталось 58 г.",
        "Во французском жиме в прошлый раз 60×10 на три подхода — в следующий раз попробуй 62,5 кг.",
        "Жим лёжа ты ещё не делал: начни с 40 кг.",
        "В прошлый раз сгибания 20×8, добавь +2,5 кг или 70 % от 1ПМ.",
        "Рекорд в жиме гантелей сидя — 1ПМ около 18 кг.",
        "Для 8–12 повторов рабочий вес около 70–75 % 1ПМ: начни с 50 кг.",
    ],
)
def test_violations_lets_supported_and_advice_pass(text):
    assert answer_check.violations(text, evidence()) == []


@pytest.mark.parametrize(
    ("token", "values", "scale", "expected"),
    [
        ("6,5", [6530], 1000, True),
        ("25", [24.7], 1, True),
        ("17,5", [17.5], 1, True),
        ("1800", [6530, 880], 1, False),
        ("60", [62.5], 1, False),
    ],
)
def test_supported(token, values, scale, expected):
    assert answer_check.supported(token, values, scale) is expected


# ---- 5. checked_answer flow ----

BAD = "Сегодня ты сделал жим лёжа 3×10 по 60 кг, тоннаж 1800 кг."
GOOD = "Ты сделал 24 подхода, тоннаж 6530 кг."


@pytest.fixture
def llm(settings):
    return FakeLLM(settings)


def context() -> answer.Context:
    return answer.Context(
        text=SUMMARY, done=DONE, food=FOOD,
        history={n: answer_direct.ExerciseHistory(n) for n in OWNER_NAMES},
        catalog=[*OWNER_NAMES, "жим лёжа"],
    )


def test_one_word_names_do_not_match_words_they_prefix():
    assert answer_intent.mentions("разве ты", ["разведения"]) == []
    assert answer_intent.mentions("в подтягиваниях", ["подтягивания"]) == ["подтягивания"]


async def test_exercise_named_in_the_question_is_no_proof_it_was_done(llm):
    # The question names the bench press; a past claim about it must still be flagged.
    bad = "В прошлый раз жим лёжа 3×8 по 80 кг."
    llm.answers = [bad, bad]
    reply = await answer.checked_answer(llm.client, context(), "сколько я жал в жиме лёжа на прошлой неделе?")
    assert reply.layer == answer.FALLBACK and "жим лёжа" in reply.flags


async def test_clean_answer_is_kept_at_a_low_temperature(llm):
    llm.answers = [GOOD]
    reply = await answer.checked_answer(llm.client, context(), "сколько я сделал?")
    assert (reply.text, reply.layer, reply.flags) == (GOOD, answer.LLM, [])
    assert len(llm.bodies) == 1
    assert llm.bodies[0]["temperature"] == answer.ANSWER_TEMPERATURE == 0.2


async def test_bad_answer_is_regenerated_with_a_correction(llm):
    llm.answers = [BAD, GOOD]
    reply = await answer.checked_answer(llm.client, context(), "сколько я сделал?")
    assert (reply.text, reply.layer, reply.flags) == (GOOD, answer.RETRIED, ["жим лёжа", "1800 кг"])
    assert len(llm.bodies) == 2
    last = [(m["role"], m["content"]) for m in llm.bodies[1]["messages"][-2:]]
    assert last == [("assistant", BAD), ("user", answer_check.correction(["жим лёжа", "1800 кг"]))]
    assert llm.bodies[1]["temperature"] == 0.2


async def test_two_bad_answers_give_the_honest_fallback(llm):
    llm.answers = [BAD, BAD]
    ctx = context()
    reply = await answer.checked_answer(llm.client, ctx, "сколько я сделал?")
    assert reply.layer == answer.FALLBACK
    assert reply.text == answer.FALLBACK_HEAD + "\n" + ctx.done
    assert "1800" not in reply.text


async def test_food_question_fallback_shows_the_food_block(llm):
    llm.answers = ["Сегодня ты съел 3000 ккал.", "Сегодня ты съел 3000 ккал."]
    ctx = context()
    reply = await answer.checked_answer(llm.client, ctx, "сколько я съел белка?")
    assert reply.layer == answer.FALLBACK
    assert reply.text.endswith(ctx.food)


async def test_failed_regeneration_gives_the_fallback(llm):
    llm.answers = [BAD, "", "", "", "", "", ""]  # no route answers the retry
    ctx = context()
    reply = await answer.checked_answer(llm.client, ctx, "сколько я сделал?")
    assert reply.layer == answer.FALLBACK
    assert reply.text == answer.FALLBACK_HEAD + "\n" + ctx.done


async def test_warning_names_the_flags_but_not_the_question(llm, caplog):
    llm.answers = [BAD, GOOD]
    question = "сколько я там всего намахал?"
    with caplog.at_level(logging.WARNING, logger="gymbot.services.answer"):
        await answer.checked_answer(llm.client, context(), question)
    warnings = " ".join(r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)
    assert "жим лёжа" in warnings and "1800 кг" in warnings
    assert question not in caplog.text and "намахал" not in caplog.text


# ---- 6. handler routing ----


@pytest.fixture
def diary():
    log_text.QA.clear()
    for store in (log_text.PENDING, log_text.CONTEXT, log_text.FACTS):
        store.clear()
    yield
    log_text.QA.clear()
    for store in (log_text.PENDING, log_text.CONTEXT, log_text.FACTS):
        store.clear()


async def _seed_today(db, settings, workout: bool = False, eat: bool = False) -> None:
    """User 42 with the owner's workout and/or food today, by the clock the handler really uses."""
    now = datetime.now(UTC)
    today = now.astimezone(ZoneInfo(settings.timezone)).date()
    async with db() as s:
        if workout:
            uid = await _seed(s, today, OWNER)
            user = await s.get(User, uid)
        else:
            user = await _user(s, targets=False)
        user.kcal_target, user.protein_target_g, user.fat_target_g, user.carbs_target_g = 2500, 150, 80, 300
        if eat:
            for desc, kcal, p, f, c in (("овсянка", 350, 12, 7, 60), ("курица с рисом", 650, 48, 15, 80)):
                s.add(FoodEntry(user_id=user.id, eaten_at=now, description=desc, kcal=kcal, protein_g=p,
                                fat_g=f, carbs_g=c))
        await s.commit()


T0 = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)


async def test_short_food_question_is_answered_from_the_database(llm, settings, db, diary):
    await _seed_today(db, settings, eat=True)
    llm.answers = [{"kind": "question", "clarification": "-"}]
    msg = await send("сколько белка осталось", llm, settings, db)
    text = msg.answer.await_args.args[0]
    assert text.startswith("Белка осталось 90 г: съедено 60 из 150 г.")
    assert len(llm.bodies) == 1  # only the parser
    assert log_text.QA[USER][-1][1] == text


async def test_workout_question_the_parser_called_unknown_is_still_answered(llm, settings, db, diary):
    await _seed_today(db, settings, workout=True)
    llm.answers = [{"kind": "unknown", "clarification": "Уточни."}]
    msg = await send("что я сегодня делал", llm, settings, db)
    text = msg.answer.await_args.args[0]
    assert text.startswith("Сегодня: 6 упражнений, 24 подхода, тоннаж 6530 кг.")
    assert len(llm.bodies) == 1


async def test_factual_looking_record_stays_a_preview(llm, settings, db, diary):
    llm.answers = [food(1)]
    msg = await send("сколько я съел самсу", llm, settings, db)
    assert buttons(msg)[0] == ["save", "drop"]
    assert len(llm.bodies) == 1
    assert log_text.QA == {}


async def test_factual_question_with_a_pending_preview_keeps_the_parsers_answer(llm, settings, db, diary):
    llm.answers = [food(1), {"kind": "question", "clarification": "Около 400 ккал."}]
    await send("самса", llm, settings, db)
    msg = await send("сколько я сегодня съел?", llm, settings, db, T0 + timedelta(minutes=1))
    assert len(llm.bodies) == 2
    assert msg.answer.await_args.args[0] == "Около 400 ккал."
    assert log_text.QA == {}


async def test_advice_question_goes_to_the_model_with_the_counted_blocks(llm, settings, db, diary):
    await _seed_today(db, settings, workout=True, eat=True)
    llm.answers = [{"kind": "question", "clarification": "-"}, "Подтяни плечи и сгибания."]
    msg = await send("как думаешь, что подтянуть на следующей тренировке?", llm, settings, db)
    assert len(llm.bodies) == 2
    assert msg.answer.await_args.args[0] == "Подтяни плечи и сгибания."
    system = llm.bodies[-1]["messages"][0]["content"]
    assert "Еда сегодня" in system and "Последний раз и рекорды" in system


# ---- after the advisor review: the user's own words are no evidence of a done exercise ----


def test_exercise_named_in_the_question_is_still_flagged_as_done():
    ev = answer_check.Evidence(
        text="Сделано сегодня, из истории: 6 упр., 24 подх., тоннаж 6530 кг.\nсколько я сделал в жиме лёжа сегодня?",
        history=set(OWNER_NAMES),
        names=CATALOG,
    )
    flags = answer_check.violations("Сегодня ты сделал жим лёжа 3×10 по 60 кг.", ev)
    assert any("жим лёжа" in f for f in flags)


def test_tonnage_with_a_thin_space_is_supported():
    ev = answer_check.Evidence(text="тоннаж 6530 кг", history=set(OWNER_NAMES), names=CATALOG)
    assert answer_check.violations("Сегодня тоннаж 6 530 кг.", ev) == []
    assert answer_check.violations("Сегодня тоннаж 1800 кг.", ev) != []


def test_one_word_name_does_not_match_a_word_that_only_starts_alike():
    names = ["разведения"]
    assert not mentions("разве ты не видишь, что я устал", names)
