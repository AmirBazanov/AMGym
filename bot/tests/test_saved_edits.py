"""Edit or delete saved records from the chat (handlers/saved_edits.py, services/saved_edits.py)."""

import json
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from test_log_text import T0, USER, FakeLLM, callback, food, message, token_of

from gymbot.db.models import FoodEntry, User, WellbeingEntry, Workout, WorkoutSet
from gymbot.handlers import log_text
from gymbot.handlers import saved_edits as hse
from gymbot.llm.schemas import ParsedExercise, ParsedSet, ParseResult
from gymbot.services import live
from gymbot.services import saved_edits as se
from gymbot.services.programs import get_or_create_exercise
from gymbot.services.users import get_or_create_user
from gymbot.services.workouts import delete_last_chat_sets


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for store in (log_text.PENDING, log_text.CONTEXT, log_text.FACTS, hse.OFFERS, hse.CHOICES):
        store.clear()
    monkeypatch.setattr(hse, "utcnow", lambda: T0 + timedelta(minutes=5))

    async def parser_answer(message, text, result, *args):
        return result

    monkeypatch.setattr(log_text, "_diary_answer", parser_answer)
    yield
    for store in (log_text.PENDING, log_text.CONTEXT, log_text.FACTS, hse.OFFERS, hse.CHOICES):
        store.clear()


@pytest.fixture
def llm(settings):
    return FakeLLM(settings)


@pytest.fixture
def published(monkeypatch):
    calls = []
    monkeypatch.setattr(live, "publish", lambda user_id, *topics: calls.append((user_id, topics)))
    return calls


async def send(text, llm, settings, db, at=T0):
    msg = message(text, at)
    await log_text.log_free_text(msg, settings, db, llm.client)
    return msg


def fix_token(msg) -> str:
    kb = msg.answer.await_args.kwargs["reply_markup"]
    return kb.inline_keyboard[0][0].callback_data.split(":", 1)[1]


async def test_food_edit_after_save(llm, settings, db, published):
    llm.answers = [food(3)]
    msg = await send("три самсы", llm, settings, db)
    await log_text.save(callback(f"save:{token_of(msg)}"), settings, db)

    llm.answers = [food(2, revises=True)]
    edit = await send("самса была 2, а не 3", llm, settings, db, T0 + timedelta(minutes=1))
    text = edit.answer.await_args.args[0]
    assert text.startswith("Исправить?") and "→ самса, 2 шт" in text
    assert llm.last_messages()[-3] == "самса, 3 шт 450 г"  # the saved record as the previous turn

    await hse.confirm(callback(f"fixok:{fix_token(edit)}"), settings, db)
    async with db() as s:
        rows = (await s.scalars(select(FoodEntry))).all()
    assert [(r.description, float(r.grams), r.raw_text) for r in rows] == [
        ("самса, 2 шт", 300.0, "три самсы\n[edit] самса была 2, а не 3")
    ]
    assert [t for _, t in published] == [("nutrition",), ("nutrition",)]


async def test_delete_food_and_workout_set(llm, settings, db, published):
    llm.answers = [food(3), {"kind": "workout", "exercises": [
        {"exercise": "Жим штанги лёжа", "sets": [{"reps": 8, "weight_kg": 80}, {"reps": 8, "weight_kg": 80}]}
    ]}]
    m1 = await send("три самсы", llm, settings, db)
    await log_text.save(callback(f"save:{token_of(m1)}"), settings, db)
    m2 = await send("жим 2 по 8 на 80", llm, settings, db)
    await log_text.save(callback(f"save:{token_of(m2)}"), settings, db)

    d = await send("убери самсу", llm, settings, db, T0 + timedelta(minutes=1))
    assert d.answer.await_args.args[0].startswith("Удалить?\n• самса, 3 шт — 1200 ккал")
    await hse.confirm(callback(f"fixok:{fix_token(d)}"), settings, db)

    e = await send("в жиме было 85, а не 80", llm, settings, db, T0 + timedelta(minutes=2))
    assert "85 кг × 8, 85 кг × 8" in e.answer.await_args.args[0]  # no model call: deterministic
    await hse.confirm(callback(f"fixok:{fix_token(e)}"), settings, db)

    u = await send("удали последний подход", llm, settings, db, T0 + timedelta(minutes=3))
    await hse.confirm(callback(f"fixok:{fix_token(u)}"), settings, db)
    async with db() as s:
        assert (await s.scalars(select(FoodEntry))).all() == []
        sets = (await s.scalars(select(WorkoutSet))).all()
    assert [(float(x.weight_kg), x.reps) for x in sets] == [(85.0, 8)]
    assert sets[0].raw_text.endswith("\n[edit] в жиме было 85, а не 80")


async def test_new_record_does_not_route(llm, settings, db):
    llm.answers = [food(1)]
    msg = await send("съел самсу", llm, settings, db)
    assert token_of(msg) in log_text.PENDING
    assert not hse.OFFERS and not hse.CHOICES
    assert USER  # same user throughout


# ---- helpers: rows inserted directly ----

TODAY = date(2026, 10, 6)  # the local day of T0 (15:00 in Moscow)
MSK = ZoneInfo("Europe/Moscow")
OTHER = 7  # a second Telegram user


async def user_id(db, tg: int = USER) -> int:
    async with db() as s:
        user = await get_or_create_user(s, tg, "U")
        await s.commit()
        return user.id


async def add_food(db, at, description="самса, 3 шт", kcal=1200, tg=USER, raw="три самсы", grams=450):
    uid = await user_id(db, tg)
    async with db() as s:
        e = FoodEntry(user_id=uid, eaten_at=at, description=description, grams=grams, kcal=kcal, protein_g=45,
                      fat_g=66, carbs_g=114, raw_text=raw)
        s.add(e)
        await s.commit()
        return e.id


async def add_workout(db, day, exercises, source="miniapp", raw=None, tg=USER):
    """exercises: [(name, [(reps, weight)])]; set_index runs over the whole workout."""
    uid = await user_id(db, tg)
    at = datetime.combine(day, datetime.min.time(), tzinfo=UTC).replace(hour=9)
    async with db() as s:
        w = Workout(user_id=uid, performed_on=day, started_at=at, source=source)
        s.add(w)
        await s.flush()
        i = 0
        for name, sets in exercises:
            ex = await get_or_create_exercise(s, name)
            for reps, weight in sets:
                s.add(WorkoutSet(workout_id=w.id, exercise_id=ex.id, set_index=i, reps=reps, weight_kg=weight,
                                 raw_text=raw, created_at=at + timedelta(minutes=i)))
                i += 1
        await s.commit()
        return w.id


async def add_wellbeing(db, at=T0, tg=USER, **kw):
    uid = await user_id(db, tg)
    async with db() as s:
        e = WellbeingEntry(user_id=uid, noted_at=at, raw_text=kw.pop("raw", "спал 5 часов"), **kw)
        s.add(e)
        await s.commit()
        return e.id


async def rows(db, model, order=None):
    async with db() as s:
        return list((await s.scalars(select(model).order_by(order if order is not None else model.id))).all())


async def saved(text, llm, settings, db, at=T0):
    """Send `text` (the LLM answers already queued) and tap Save."""
    msg = await send(text, llm, settings, db, at)
    await log_text.save(callback(f"save:{token_of(msg)}"), settings, db)
    return msg


def alert_of(cb) -> bool:
    return cb.answer.await_args.kwargs.get("show_alert") is True


def reply(msg) -> str:
    return msg.answer.await_args.args[0]


GYM = "Жим штанги лёжа"
SQUAT = "Присед со штангой"


def sets_answer(*exercises):
    return {"kind": "workout", "exercises": [
        {"exercise": n, "sets": [{"reps": r, "weight_kg": w} for r, w in sets]} for n, sets in exercises
    ]}


# ---- 1. routing: positives ----


@pytest.mark.parametrize(
    ("text", "attrs"),
    [
        ("удали последнюю еду", {"action": "delete", "latest": True, "kind": "food", "words": ()}),
        ("убери самсу", {"action": "delete", "words": ("самсу",), "weak": False}),
        ("удали обед", {"action": "delete", "meal": "обед", "kind": "food"}),
        ("вчерашний плов удали", {"action": "delete", "words": ("плов",), "day": TODAY - timedelta(days=1)}),
        ("самса была 2, а не 3", {"action": "edit", "pair": (2, 3), "words": ("самса",)}),
        ("самса была две а не три", {"action": "edit", "pair": (2, 3), "words": ("самса",)}),
        ("в плове было 250 г, а не 350", {"action": "edit", "pair": (250, 350), "words": ("плове",)}),
        ("удали последний подход", {"action": "delete", "kind": "workout", "sets_only": True, "latest": True}),
        ("в жиме было 85, а не 80", {"action": "edit", "pair": (85, 80), "words": ("жиме",)}),
        ("удали вчерашнюю тренировку", {
            "action": "delete", "kind": "workout", "whole_workout": True, "day": TODAY - timedelta(days=1)}),
        ("сон был 7 часов, а не 5", {"action": "edit", "kind": "wellbeing", "pair": (7, 5)}),
        ("бутерброд без сыра", {"action": "edit", "weak": True, "soft": True, "words": ("бутерброд",)}),
        ("в жиме не 80, а 85", {"action": "edit", "weak": True, "pair": (85, 80), "words": ("жиме",)}),
    ],
)
def test_detect_routes(text, attrs):
    intent = se.detect(text, TODAY)
    assert intent is not None, text
    for name, value in attrs.items():
        assert getattr(intent, name) == value, (text, name)


# ---- 2. routing: negatives ----


@pytest.mark.parametrize(
    "text",
    [
        "съел самсу",
        "самса 3 шт",
        "жим 3 по 8 на 80",
        "вес был 84, а не 85",
        "убери напоминание в 9",
        "измени норму на 2500 ккал",
        "съел бутерброд без сыра",
        "гречка без масла 200 г",
        "удали самсу?",
        "удали самсу " + "и ещё что-нибудь вот так " * 3,  # a story: more than MAX_WORDS words
    ],
)
def test_detect_ignores(text):
    assert se.detect(text, TODAY) is None, text


def test_detect_word_limit_is_exactly_twelve():
    twelve = "удали самсу " + " ".join(["вот"] * 10)
    assert len(twelve.split()) == se.MAX_WORDS
    assert se.detect(twelve, TODAY) is not None
    assert se.detect(twelve + " вот", TODAY) is None


# ---- 3. an open preview wins ----


async def test_open_preview_takes_the_correction(llm, settings, db):
    llm.answers = [food(3), food(2, revises=True)]
    first = await send("три самсы", llm, settings, db)  # not saved
    second = await send("самса была 2, а не 3", llm, settings, db, T0 + timedelta(minutes=1))
    assert len(llm.bodies) == 2
    assert llm.last_messages()[-3] == "три самсы"  # the open preview as the previous turn
    assert token_of(first) not in log_text.PENDING
    assert token_of(second) in log_text.PENDING
    assert not hse.OFFERS and not hse.CHOICES


# ---- 4. right after saving ----


async def test_edit_after_save_gives_the_model_the_saved_record(llm, settings, db):
    llm.answers = [food(3), food(2, revises=True)]
    await saved("три самсы", llm, settings, db)
    edit = await send("самса была 2 а не 3", llm, settings, db, T0 + timedelta(minutes=1))
    history = json.loads(llm.last_messages()[-2])
    assert history["foods"][0]["grams"] == 450
    assert history["foods"][0]["description"] == "самса, 3 шт"
    assert "→" in reply(edit)
    assert not log_text.PENDING  # the edit is not a new record to save


# ---- 5. delete by meal ----


async def test_delete_meal_takes_the_whole_message_of_that_meal(llm, settings, db, published):
    morning = {"kind": "food", "foods": [
        {"description": "овсянка", "grams": 200, "kcal": 300, "protein_g": 10, "fat_g": 6, "carbs_g": 50},
        {"description": "чай", "grams": 200, "kcal": 4, "protein_g": 0, "fat_g": 0, "carbs_g": 1},
    ]}
    lunch = {"kind": "food", "foods": [
        {"description": "плов", "grams": 350, "kcal": 600, "protein_g": 25, "fat_g": 25, "carbs_g": 70},
        {"description": "салат", "grams": 150, "kcal": 90, "protein_g": 2, "fat_g": 6, "carbs_g": 6},
    ]}
    llm.answers = [morning, lunch]
    await saved("овсянка и чай", llm, settings, db, datetime(2026, 10, 6, 5, 0, tzinfo=UTC))  # 08:00 local
    await saved("плов и салат", llm, settings, db, datetime(2026, 10, 6, 10, 30, tzinfo=UTC))  # 13:30 local

    d = await send("удали обед", llm, settings, db)
    text = reply(d)
    assert text.startswith("Удалить?") and "плов" in text and "салат" in text
    assert "овсянка" not in text and "чай" not in text
    await hse.confirm(callback(f"fixok:{fix_token(d)}"), settings, db)
    left = await rows(db, FoodEntry)
    assert [e.description for e in left] == ["овсянка", "чай"]
    assert len(llm.bodies) == 2  # a delete never asks the model


async def test_delete_latest_food_is_the_newest_message(llm, settings, db):
    await add_food(db, datetime(2026, 10, 6, 5, 0, tzinfo=UTC), "овсянка", raw="овсянка и чай")
    await add_food(db, datetime(2026, 10, 6, 5, 0, tzinfo=UTC), "чай", raw="овсянка и чай")
    await add_food(db, datetime(2026, 10, 6, 10, 30, tzinfo=UTC), "плов", raw="плов и салат")
    await add_food(db, datetime(2026, 10, 6, 10, 30, tzinfo=UTC), "салат", raw="плов и салат")
    d = await send("удали последнюю еду", llm, settings, db)
    text = reply(d)
    assert "плов" in text and "салат" in text and "овсянка" not in text
    await hse.confirm(callback(f"fixok:{fix_token(d)}"), settings, db)
    assert [e.description for e in await rows(db, FoodEntry)] == ["овсянка", "чай"]


# ---- 6. ambiguity ----


async def test_two_matches_ask_which_one(llm, settings, db, published):
    llm.answers = [food(1), food(2)]
    await saved("самса", llm, settings, db)
    await saved("две самсы", llm, settings, db, T0 + timedelta(minutes=1))

    d = await send("убери самсу", llm, settings, db, T0 + timedelta(minutes=2))
    assert reply(d) == "Что удалить?"
    kb = d.answer.await_args.kwargs["reply_markup"].inline_keyboard
    assert len(kb) == 3  # two candidates and Отмена
    assert [r[0].callback_data.split(":")[0] for r in kb] == ["fixpick", "fixpick", "fixno"]
    assert "самса, 2 шт" in kb[0][0].text  # newest first
    assert "самса, 1 шт" in kb[1][0].text
    token = kb[0][0].callback_data.split(":")[1]

    cb = callback(f"fixpick:{token}:1")
    await hse.pick(cb, settings, db)
    shown = cb.message.edit_text.await_args
    assert shown.args[0].startswith("Удалить?") and "самса, 1 шт" in shown.args[0]
    ok = shown.kwargs["reply_markup"].inline_keyboard[0][0].callback_data
    assert ok.startswith("fixok:")
    assert await rows(db, FoodEntry) and len(await rows(db, FoodEntry)) == 2  # nothing yet

    await hse.confirm(callback(ok), settings, db)
    left = await rows(db, FoodEntry)
    assert [e.description for e in left] == ["самса, 2 шт"]


async def test_at_most_five_candidates(llm, settings, db):
    for i in range(7):
        await add_food(db, T0 + timedelta(minutes=i - 20), f"самса, {i + 1} шт", raw=f"самса {i}")
    d = await send("убери самсу", llm, settings, db)
    kb = d.answer.await_args.kwargs["reply_markup"].inline_keyboard
    assert len(kb) == se.MAX_CHOICES + 1
    assert kb[-1][0].callback_data.startswith("fixno:")
    assert all(len(r[0].text) <= hse.LABEL_MAX for r in kb)
    assert "самса, 7 шт" in kb[0][0].text  # the newest first


async def test_cancel_a_choice_list(llm, settings, db):
    await add_food(db, T0 - timedelta(minutes=10), raw="а")
    await add_food(db, T0 - timedelta(minutes=5), raw="б")
    d = await send("убери самсу", llm, settings, db)
    kb = d.answer.await_args.kwargs["reply_markup"].inline_keyboard
    cb = callback(kb[-1][0].callback_data)
    await hse.cancel(cb)
    cb.message.edit_text.assert_awaited_once_with("Отменено.")
    assert len(await rows(db, FoodEntry)) == 2
    assert not hse.CHOICES


async def test_pick_with_bad_index_keeps_the_choice(llm, settings, db):
    await add_food(db, T0 - timedelta(minutes=10), raw="а")
    await add_food(db, T0 - timedelta(minutes=5), raw="б")
    d = await send("убери самсу", llm, settings, db)
    token = d.answer.await_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data.split(":")[1]
    cb = callback(f"fixpick:{token}:9")
    await hse.pick(cb, settings, db)
    assert token in hse.CHOICES
    cb.message.edit_text.assert_not_awaited()


# ---- 7, 8. workouts: whole workout, last set ----


async def test_delete_whole_workout(llm, settings, db, published):
    yesterday = TODAY - timedelta(days=1)
    wid = await add_workout(db, yesterday, [(GYM, [(8, 80), (8, 80)]), (SQUAT, [(10, 100)])])
    today_id = await add_workout(db, TODAY, [(GYM, [(5, 90)])])

    d = await send("удали вчерашнюю тренировку", llm, settings, db)
    text = reply(d)
    assert text.startswith("Удалить тренировку 05.10?")
    assert "подходов 2" in text and "подходов 1" in text
    assert "Всего: упражнений 2, подходов 3, тоннаж 2280 кг" in text  # 8*80*2 + 10*100
    await hse.confirm(callback(f"fixok:{fix_token(d)}"), settings, db)

    left = await rows(db, Workout)
    assert [w.id for w in left] == [today_id] and wid not in [w.id for w in left]
    assert [(s.reps, float(s.weight_kg)) for s in await rows(db, WorkoutSet)] == [(5, 90.0)]
    uid = await user_id(db)
    assert published == [(uid, ("workouts", "state"))]


async def test_deleting_the_only_set_removes_the_empty_workout(llm, settings, db, published):
    await add_workout(db, TODAY, [(GYM, [(8, 80)])])
    d = await send("удали последний подход", llm, settings, db)
    assert "Удалить?" in reply(d) and "80 кг × 8" in reply(d)
    await hse.confirm(callback(f"fixok:{fix_token(d)}"), settings, db)
    assert await rows(db, Workout) == [] and await rows(db, WorkoutSet) == []


async def test_delete_last_set_keeps_the_rest_of_the_workout(llm, settings, db):
    await add_workout(db, TODAY, [(GYM, [(8, 80), (6, 85)])])
    d = await send("удали последний подход", llm, settings, db)
    assert "85 кг × 6" in reply(d) and "80 кг" not in reply(d)
    await hse.confirm(callback(f"fixok:{fix_token(d)}"), settings, db)
    assert [(s.reps, float(s.weight_kg)) for s in await rows(db, WorkoutSet)] == [(8, 80.0)]
    assert len(await rows(db, Workout)) == 1


async def test_last_set_with_its_drops_goes_together(llm, settings, db):
    wid = await add_workout(db, TODAY, [(GYM, [(12, 60)])])
    async with db() as s:
        ex = (await s.scalars(select(WorkoutSet))).first().exercise_id
        s.add_all([WorkoutSet(workout_id=wid, exercise_id=ex, set_index=1, reps=6, weight_kg=50, drop_index=1),
                   WorkoutSet(workout_id=wid, exercise_id=ex, set_index=2, reps=6, weight_kg=40, drop_index=2)])
        await s.commit()
    d = await send("удали последний подход", llm, settings, db)
    assert reply(d).count("(дроп)") == 2
    await hse.confirm(callback(f"fixok:{fix_token(d)}"), settings, db)
    assert [(s.reps, float(s.weight_kg)) for s in await rows(db, WorkoutSet)] == []


# ---- 9. deterministic workout swap ----


async def test_weight_swap_touches_only_matching_sets_without_the_model(llm, settings, db, published):
    llm.answers = [sets_answer((GYM, [(8, 80), (8, 80), (6, 85)]), (SQUAT, [(10, 100)]))]
    await saved("жим 8 на 80 дважды и 6 на 85, присед 10 на 100", llm, settings, db)
    bodies = len(llm.bodies)

    e = await send("в жиме было 90, а не 80", llm, settings, db, T0 + timedelta(minutes=1))
    assert len(llm.bodies) == bodies  # deterministic
    assert "90 кг × 8, 90 кг × 8, 85 кг × 6" in reply(e)
    await hse.confirm(callback(f"fixok:{fix_token(e)}"), settings, db)

    sets = await rows(db, WorkoutSet, WorkoutSet.set_index)
    assert [(s.reps, float(s.weight_kg)) for s in sets] == [(8, 90.0), (8, 90.0), (6, 85.0), (10, 100.0)]
    # every set of the chat message got the trail, the untouched squat too
    assert {s.raw_text for s in sets} == {
        "жим 8 на 80 дважды и 6 на 85, присед 10 на 100\n[edit] в жиме было 90, а не 80"
    }
    async with db() as s:
        user = await s.get(User, await user_id(db))
        assert await delete_last_chat_sets(s, user) == 4  # /undo still removes the whole message
        await s.commit()
    assert await rows(db, WorkoutSet) == [] and await rows(db, Workout) == []


async def test_miniapp_sets_get_a_bare_edit_trail(llm, settings, db):
    await add_workout(db, TODAY, [(GYM, [(8, 80), (8, 80)])], source="miniapp", raw=None)
    e = await send("в жиме было 85, а не 80", llm, settings, db)
    await hse.confirm(callback(f"fixok:{fix_token(e)}"), settings, db)
    sets = await rows(db, WorkoutSet)
    assert [float(s.weight_kg) for s in sets] == [85.0, 85.0]
    assert {s.raw_text for s in sets} == {"[edit] в жиме было 85, а не 80"}
    assert llm.bodies == []


async def test_reps_swap_when_the_old_number_is_reps(llm, settings, db):
    await add_workout(db, TODAY, [(GYM, [(8, 80), (10, 80)])])
    e = await send("в жиме было 12, а не 10", llm, settings, db)
    assert "80 кг × 8, 80 кг × 12" in reply(e)
    assert llm.bodies == []


# ---- 10. workout edit through the model ----


async def test_workout_edit_adds_sets_right_after_the_exercise(llm, settings, db, published):
    await add_workout(db, TODAY, [(GYM, [(8, 80), (8, 80)]), (SQUAT, [(10, 100)])])
    llm.answers = [sets_answer((GYM, [(8, 80), (8, 80), (8, 80)]))]

    e = await send("исправь жим: 3 по 8 на 80", llm, settings, db)
    text = reply(e)
    assert text.startswith("Исправить?") and "→ " in text
    assert text.count("80 кг × 8") == 5  # two before, three after
    assert json.loads(llm.last_messages()[-2])["exercises"][0]["sets"][0]["weight_kg"] == 80  # saved record as history
    await hse.confirm(callback(f"fixok:{fix_token(e)}"), settings, db)

    sets = await rows(db, WorkoutSet, WorkoutSet.set_index)
    assert [s.set_index for s in sets] == [0, 1, 2, 3]  # the squat moved to the end
    assert [(s.reps, float(s.weight_kg)) for s in sets] == [
        (8, 80.0), (8, 80.0), (8, 80.0), (10, 100.0)
    ]
    assert [s.raw_text for s in sets] == ["[edit] исправь жим: 3 по 8 на 80"] * 3 + [None]  # the added set too
    assert (await rows(db, Workout))[0].id == sets[0].workout_id


async def test_workout_edit_to_fewer_sets_deletes_the_rest(llm, settings, db):
    await add_workout(db, TODAY, [(GYM, [(8, 80), (8, 80), (8, 80)])])
    llm.answers = [sets_answer((GYM, [(8, 80)]))]
    e = await send("исправь жим: только 8 на 80", llm, settings, db)
    await hse.confirm(callback(f"fixok:{fix_token(e)}"), settings, db)
    assert len(await rows(db, WorkoutSet)) == 1


async def test_three_sets_not_two_edits_the_whole_exercise(llm, settings, db):
    """«в жиме было 3 подхода, а не 2»: "подход" must not narrow the target to the last set."""
    await add_workout(db, TODAY, [(GYM, [(8, 80), (8, 80)])])
    llm.answers = [sets_answer((GYM, [(8, 80), (8, 80), (8, 80)]))]
    e = await send("в жиме было 3 подхода, а не 2", llm, settings, db)
    await hse.confirm(callback(f"fixok:{fix_token(e)}"), settings, db)
    assert len(await rows(db, WorkoutSet)) == 3


# ---- 11. wellbeing ----


async def test_sleep_swap_is_deterministic(llm, settings, db, published):
    await add_wellbeing(db, sleep_hours=5, energy=3)
    e = await send("сон был 7 часов, а не 5", llm, settings, db)
    assert "сон 5 ч" in reply(e) and "сон 7 ч" in reply(e) and "энергия 3/5" in reply(e)
    assert llm.bodies == []
    await hse.confirm(callback(f"fixok:{fix_token(e)}"), settings, db)
    [entry] = await rows(db, WellbeingEntry)
    assert float(entry.sleep_hours) == 7.0 and entry.energy == 3
    assert entry.raw_text == "спал 5 часов\n[edit] сон был 7 часов, а не 5"
    assert published == [(await user_id(db), ("wellbeing", "plan"))]


async def test_wellbeing_edit_through_the_model(llm, settings, db, published):
    await add_wellbeing(db, mood=2, energy=3)
    llm.answers = [{"kind": "wellbeing", "wellbeing": {"mood": 5, "energy": 3}}]
    e = await send("настроение было отличное, а не плохое", llm, settings, db)
    assert "настроение 2/5" in reply(e) and "→ энергия 3/5, настроение 5/5" in reply(e)
    await hse.confirm(callback(f"fixok:{fix_token(e)}"), settings, db)
    [entry] = await rows(db, WellbeingEntry)
    assert (entry.mood, entry.energy) == (5, 3)
    assert entry.raw_text.endswith("\n[edit] настроение было отличное, а не плохое")
    assert published[-1][1] == ("wellbeing", "plan")


async def test_wellbeing_fix_verb_through_the_model(llm, settings, db):
    await add_wellbeing(db, energy=2)
    llm.answers = [{"kind": "wellbeing", "wellbeing": {"energy": 4}}]
    e = await send("исправь самочувствие: энергия 4", llm, settings, db)
    await hse.confirm(callback(f"fixok:{fix_token(e)}"), settings, db)
    assert (await rows(db, WellbeingEntry))[0].energy == 4


async def test_delete_wellbeing(llm, settings, db, published):
    await add_wellbeing(db, sleep_hours=6)
    d = await send("удали сон", llm, settings, db)
    assert reply(d).startswith("Удалить самочувствие")
    await hse.confirm(callback(f"fixok:{fix_token(d)}"), settings, db)
    assert await rows(db, WellbeingEntry) == []


# ---- 12. stale offers ----


async def _delete_offer(llm, settings, db):
    await add_food(db, T0 - timedelta(minutes=30))
    d = await send("убери самсу", llm, settings, db)
    return fix_token(d)


async def test_second_confirm_is_an_alert(llm, settings, db):
    token = await _delete_offer(llm, settings, db)
    await hse.confirm(callback(f"fixok:{token}"), settings, db)
    assert await rows(db, FoodEntry) == []
    again = callback(f"fixok:{token}")
    await hse.confirm(again, settings, db)
    assert alert_of(again)
    again.message.edit_text.assert_not_awaited()


async def test_changed_row_makes_the_offer_stale(llm, settings, db, published):
    token = await _delete_offer(llm, settings, db)
    async with db() as s:
        (await s.scalars(select(FoodEntry))).first().kcal = 999
        await s.commit()
    cb = callback(f"fixok:{token}")
    await hse.confirm(cb, settings, db)
    assert alert_of(cb)
    cb.message.edit_text.assert_awaited_once_with(hse.STALE)
    [entry] = await rows(db, FoodEntry)
    assert float(entry.kcal) == 999 and published == []


async def test_deleted_row_makes_the_offer_stale(llm, settings, db):
    token = await _delete_offer(llm, settings, db)
    async with db() as s:
        await s.delete((await s.scalars(select(FoodEntry))).first())
        await s.commit()
    cb = callback(f"fixok:{token}")
    await hse.confirm(cb, settings, db)
    assert alert_of(cb)


async def test_edit_offer_goes_stale_when_the_sets_change(llm, settings, db):
    await add_workout(db, TODAY, [(GYM, [(8, 80)])])
    e = await send("в жиме было 85, а не 80", llm, settings, db)
    async with db() as s:
        (await s.scalars(select(WorkoutSet))).first().reps = 5
        await s.commit()
    cb = callback(f"fixok:{fix_token(e)}")
    await hse.confirm(cb, settings, db)
    assert alert_of(cb)
    [row] = await rows(db, WorkoutSet)
    assert (row.reps, float(row.weight_kg), row.raw_text) == (5, 80.0, None)


async def test_offer_expires_after_the_ttl(llm, settings, db, monkeypatch):
    token = await _delete_offer(llm, settings, db)
    monkeypatch.setattr(hse, "utcnow", lambda: T0 + timedelta(minutes=5) + hse.TTL + timedelta(seconds=1))
    cb = callback(f"fixok:{token}")
    await hse.confirm(cb, settings, db)
    assert alert_of(cb)
    assert len(await rows(db, FoodEntry)) == 1


async def test_offer_just_inside_the_ttl_works(llm, settings, db, monkeypatch):
    token = await _delete_offer(llm, settings, db)
    monkeypatch.setattr(hse, "utcnow", lambda: T0 + timedelta(minutes=5) + hse.TTL)
    await hse.confirm(callback(f"fixok:{token}"), settings, db)
    assert await rows(db, FoodEntry) == []


async def test_unknown_token_is_an_alert(settings, db):
    for handler, data in ((hse.confirm, "fixok:nope"), (hse.cancel, "fixno:nope")):
        cb = callback(data)
        await (handler(cb, settings, db) if handler is hse.confirm else handler(cb))
        assert alert_of(cb)


# ---- 13. ownership ----


async def test_foreign_tap_is_an_alert_and_keeps_the_token(llm, settings, db):
    token = await _delete_offer(llm, settings, db)
    stranger = callback(f"fixok:{token}", user_id=OTHER)
    await hse.confirm(stranger, settings, db)
    assert alert_of(stranger)
    assert len(await rows(db, FoodEntry)) == 1 and token in hse.OFFERS
    nope = callback(f"fixno:{token}", user_id=OTHER)
    await hse.cancel(nope)
    assert alert_of(nope) and token in hse.OFFERS
    await hse.confirm(callback(f"fixok:{token}"), settings, db)  # the owner still can
    assert await rows(db, FoodEntry) == []


async def test_foreign_pick_keeps_the_choice(llm, settings, db):
    await add_food(db, T0 - timedelta(minutes=10), raw="а")
    await add_food(db, T0 - timedelta(minutes=5), raw="б")
    d = await send("убери самсу", llm, settings, db)
    token = d.answer.await_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data.split(":")[1]
    cb = callback(f"fixpick:{token}:0", user_id=OTHER)
    await hse.pick(cb, settings, db)
    assert alert_of(cb) and token in hse.CHOICES


async def test_other_users_rows_are_never_candidates(llm, settings, db):
    await add_food(db, T0 - timedelta(minutes=10), tg=OTHER, description="самса, 5 шт", raw="чужая")
    await add_food(db, T0 - timedelta(minutes=5), raw="моя")
    d = await send("убери самсу", llm, settings, db)  # one candidate: no buttons to pick from
    assert reply(d).startswith("Удалить?") and "самса, 3 шт" in reply(d)
    await hse.confirm(callback(f"fixok:{fix_token(d)}"), settings, db)
    [left] = await rows(db, FoodEntry)
    assert left.description == "самса, 5 шт"


async def test_only_other_users_rows_means_not_found(llm, settings, db):
    await add_food(db, T0 - timedelta(minutes=10), tg=OTHER)
    await user_id(db)
    d = await send("убери самсу", llm, settings, db)
    assert reply(d).startswith("Не нашёл")


async def test_user_without_any_records_gets_not_found(llm, settings, db):
    d = await send("удали последнюю еду", llm, settings, db)
    assert reply(d).startswith("Не нашёл еду")


async def test_other_users_workout_cannot_be_applied(llm, settings, db):
    """apply reloads rows with the owner's id: a unit built for one user is Stale for another."""
    await add_workout(db, TODAY, [(GYM, [(8, 80)])])
    uid, other = await user_id(db), await user_id(db, OTHER)
    intent = se.detect("удали последний подход", TODAY)
    async with db() as s:
        unit = (await se.find(s, uid, intent, T0, MSK)).units[0]
        with pytest.raises(se.Stale):
            await se.apply(s, other, unit, "delete", None, "x", T0, MSK)


# ---- 14. the 14-day guard ----


async def test_named_day_older_than_two_weeks(llm, settings, db):
    await add_food(db, datetime(2026, 9, 20, 9, 0, tzinfo=UTC))
    d = await send("удали самсу за 20.09", llm, settings, db)
    assert reply(d) == hse.TOO_OLD
    assert len(await rows(db, FoodEntry)) == 1


async def test_named_day_exactly_at_the_limit_is_allowed(llm, settings, db):
    await add_food(db, datetime(2026, 9, 22, 9, 0, tzinfo=UTC))  # 14 days before 06.10
    d = await send("удали самсу за 22.09", llm, settings, db)
    assert reply(d).startswith("Удалить?")


async def test_old_rows_are_never_latest(llm, settings, db):
    await add_food(db, T0 - timedelta(days=20))
    d = await send("удали последнюю еду", llm, settings, db)
    assert reply(d).startswith("Не нашёл")
    assert len(await rows(db, FoodEntry)) == 1


async def test_latest_looks_back_two_weeks(llm, settings, db):
    await add_food(db, T0 - timedelta(days=10))
    d = await send("удали последнюю еду", llm, settings, db)
    assert reply(d).startswith("Удалить?")


async def test_old_workouts_and_wellbeing_are_out_of_reach(llm, settings, db):
    await add_workout(db, TODAY - timedelta(days=20), [(GYM, [(8, 80)])])
    await add_wellbeing(db, at=T0 - timedelta(days=20), sleep_hours=5)
    for text in ("удали последний подход", "удали сон", "в жиме было 85, а не 80"):
        d = await send(text, llm, settings, db)
        assert reply(d).startswith("Не нашёл"), text


async def test_apply_refuses_a_unit_that_aged_out(db):
    await add_food(db, T0)
    uid = await user_id(db)
    intent = se.detect("убери самсу", TODAY)
    async with db() as s:
        unit = (await se.find(s, uid, intent, T0, MSK)).units[0]
        with pytest.raises(se.Stale):
            await se.apply(s, uid, unit, "delete", None, "x", T0 + timedelta(days=20), MSK)
        await se.apply(s, uid, unit, "delete", None, "x", T0 + timedelta(days=13), MSK)  # still fine


# ---- 15. nothing found ----


async def test_strong_command_without_a_match_says_so(llm, settings, db):
    d = await send("убери плов", llm, settings, db)
    assert reply(d).startswith("Не нашёл «плов»")
    assert llm.bodies == [] and not hse.OFFERS


async def test_weak_commands_without_a_match_go_to_the_parser(llm, settings, db):
    llm.answers = [food(1, "бутерброд без сыра")]
    msg = await send("бутерброд без сыра", llm, settings, db)
    assert len(llm.bodies) == 1 and token_of(msg) in log_text.PENDING
    assert not hse.OFFERS

    llm.answers = [sets_answer((GYM, [(8, 85)]))]
    log_text.PENDING.clear()
    log_text.CONTEXT.clear()
    msg = await send("в жиме не 80, а 85", llm, settings, db, T0 + timedelta(hours=1))
    assert len(llm.bodies) == 2 and not hse.OFFERS


async def test_record_verb_never_becomes_an_edit(llm, settings, db):
    llm.answers = [food(1, "бутерброд без сыра")]
    await add_food(db, T0, "бутерброд", raw="бутерброд")
    msg = await send("съел бутерброд без сыра", llm, settings, db, T0 + timedelta(minutes=1))
    assert len(llm.bodies) == 1 and token_of(msg) in log_text.PENDING and not hse.OFFERS


async def test_not_found_names_the_requested_day(llm, settings, db):
    await user_id(db)
    d = await send("удали вчерашний плов", llm, settings, db)
    assert reply(d).startswith("Не нашёл «плов» за вчера")


async def test_not_found_names_the_requested_day_for_a_new_user(llm, settings, db):
    """No user row yet: the reply must still talk about the day that was asked for, not "за сегодня"."""
    d = await send("удали вчерашний плов", llm, settings, db)
    assert reply(d).startswith("Не нашёл «плов» за вчера")


async def test_edit_whole_workout_asks_for_the_exercise(llm, settings, db):
    await add_workout(db, TODAY, [(GYM, [(8, 80)])])
    d = await send("исправь тренировку", llm, settings, db)
    assert reply(d) == hse.NAME_EXERCISE


# ---- 16. cancel ----


async def test_cancel_a_hard_offer(llm, settings, db):
    token = await _delete_offer(llm, settings, db)
    cb = callback(f"fixno:{token}")
    await hse.cancel(cb)
    cb.message.edit_text.assert_awaited_once_with("Отменено.")
    assert len(await rows(db, FoodEntry)) == 1 and token not in hse.OFFERS


async def test_cancel_a_soft_offer_adds_the_hint(llm, settings, db):
    llm.answers = [food(1, "бутерброд с сыром"), food(1, "бутерброд без сыра")]
    await saved("бутерброд с сыром", llm, settings, db)
    soft = await send("бутерброд без сыра", llm, settings, db, T0 + timedelta(minutes=1))
    assert reply(soft).startswith("Исправить?")
    cb = callback(f"fixno:{fix_token(soft)}")
    await hse.cancel(cb)
    cb.message.edit_text.assert_awaited_once_with(f"Отменено. {hse.SOFT_HINT}")


# ---- 17. soft edits: only the meal just saved ----


async def test_soft_edit_of_food_saved_two_hours_ago_goes_to_the_parser(llm, settings, db):
    llm.answers = [food(1, "бутерброд"), food(1, "бутерброд без сыра")]
    await saved("бутерброд", llm, settings, db)
    msg = await send("бутерброд без сыра", llm, settings, db, T0 + timedelta(hours=2))
    assert len(llm.bodies) == 2 and token_of(msg) in log_text.PENDING and not hse.OFFERS


async def test_soft_edit_just_inside_the_hour_routes(llm, settings, db):
    llm.answers = [food(1, "бутерброд с сыром"), food(1, "бутерброд без сыра")]
    await saved("бутерброд с сыром", llm, settings, db)
    msg = await send("бутерброд без сыра", llm, settings, db, T0 + timedelta(minutes=se.SOFT_MINUTES - 1))
    assert reply(msg).startswith("Исправить?") and len(hse.OFFERS) == 1


async def test_soft_edit_ignores_workouts_and_old_days(llm, settings, db):
    await add_food(db, T0 - timedelta(days=1), "бутерброд, 1 шт", raw="бутерброд")
    llm.answers = [food(1, "бутерброд без сыра")]
    msg = await send("бутерброд без сыра", llm, settings, db)
    assert token_of(msg) in log_text.PENDING and not hse.OFFERS


# ---- 18. the model's answer is not a revision ----


async def test_wrong_kind_from_the_model_is_not_understood(llm, settings, db):
    await add_food(db, T0 - timedelta(minutes=30))
    llm.answers = [{"kind": "question", "clarification": "Не знаю"}]
    e = await send("самса была 2, а не 3", llm, settings, db)
    assert reply(e) == hse.NOT_UNDERSTOOD
    assert e.answer.await_args.kwargs.get("reply_markup") is None
    assert not hse.OFFERS


async def test_workout_answer_for_a_food_edit_is_not_understood(llm, settings, db):
    await add_food(db, T0 - timedelta(minutes=30))
    llm.answers = [sets_answer((GYM, [(8, 80)]))]
    e = await send("самса была 2, а не 3", llm, settings, db)
    assert reply(e) == hse.NOT_UNDERSTOOD and not hse.OFFERS


async def test_unchanged_answer_says_so(llm, settings, db):
    await add_food(db, T0 - timedelta(minutes=30))
    llm.answers = [food(3)]
    e = await send("исправь самсу", llm, settings, db)
    assert reply(e) == hse.SAME and not hse.OFFERS


async def test_model_outage_is_reported(llm, settings, db):
    await add_food(db, T0 - timedelta(minutes=30))
    llm.answers = ["not json"] * 10
    e = await send("самса была 2, а не 3", llm, settings, db)
    assert reply(e) == hse.UNAVAILABLE and not hse.OFFERS


async def test_model_may_return_extra_foods_for_a_single_entry_edit(llm, settings, db):
    await add_food(db, T0 - timedelta(minutes=30))
    two = {"kind": "food", "foods": [
        {"description": "чай", "grams": 200, "kcal": 4, "protein_g": 0, "fat_g": 0, "carbs_g": 1},
        food(2)["foods"][0],
    ]}
    llm.answers = [two]
    e = await send("самса была 2, а не 3", llm, settings, db)
    assert "→ самса, 2 шт" in reply(e) and "чай" not in reply(e)
    await hse.confirm(callback(f"fixok:{fix_token(e)}"), settings, db)
    assert [x.description for x in await rows(db, FoodEntry)] == ["самса, 2 шт"]


# ---- 19. voice ----


async def test_voice_edit_keeps_prefix_and_trail(llm, settings, db):
    llm.answers = [food(3), food(2, revises=True)]
    await saved("три самсы", llm, settings, db)
    msg = message("самса была 2, а не 3", T0 + timedelta(minutes=1))
    prefix = "Распознал: «самса была 2, а не 3»\n\n"
    await log_text.process_text(
        msg, "самса была 2, а не 3", settings, db, llm.client, raw_text="[voice] самса была 2, а не 3", prefix=prefix
    )
    assert reply(msg).startswith(prefix + "Исправить?")
    await hse.confirm(callback(f"fixok:{fix_token(msg)}"), settings, db)
    [entry] = await rows(db, FoodEntry)
    assert entry.raw_text == "три самсы\n[edit] [voice] самса была 2, а не 3"


async def test_voice_prefix_on_replies_without_an_offer(llm, settings, db):
    msg = message("убери плов", T0)
    await log_text.process_text(msg, "убери плов", settings, db, llm.client, prefix="Распознал: «убери плов»\n\n")
    assert reply(msg).startswith("Распознал: «убери плов»\n\nНе нашёл")


async def test_edited_row_stays_in_its_message_group(llm, settings, db):
    """An [edit] trail on one row must not split the meal it was saved with."""
    two = {"kind": "food", "foods": [
        {"description": "самса, 3 шт", "grams": 450, "kcal": 1200, "protein_g": 45, "fat_g": 66, "carbs_g": 114},
        {"description": "чай", "grams": 200, "kcal": 0, "protein_g": 0, "fat_g": 0, "carbs_g": 0},
    ]}
    llm.answers = [two]
    await saved("самса и чай", llm, settings, db)
    llm.answers = [food(2, revises=True)]
    edit = await send("самса была 2, а не 3", llm, settings, db, T0 + timedelta(minutes=1))
    await hse.confirm(callback(f"fixok:{fix_token(edit)}"), settings, db)

    d = await send("удали последнюю еду", llm, settings, db, T0 + timedelta(minutes=2))
    assert "самса, 2 шт" in reply(d) and "чай" in reply(d)
    await hse.confirm(callback(f"fixok:{fix_token(d)}"), settings, db)
    assert await rows(db, FoodEntry) == []


def test_original_strips_the_edit_trail():
    assert se.original("самса и чай\n[edit] x\n[edit] y") == "самса и чай"
    assert se.original("[edit] x") is None
    assert se.original(None) is None


def test_long_units_and_bolshe():
    today = T0.date()
    assert se.detect("в плове было 250 граммов, а не 350", today).pair == (250, 350)
    bolshe = se.detect("самса была больше, а не 120 г", today)  # no number pair, no wellbeing word: parser's
    assert bolshe is None or bolshe.kind != "wellbeing"


async def test_new_sleep_record_with_nothing_saved_goes_to_the_parser(llm, settings, db):
    llm.answers = [{"kind": "wellbeing", "wellbeing": {"sleep_hours": 5}}]
    msg = await send("спал 5 часов, а не 8", llm, settings, db)
    assert len(llm.bodies) == 1 and token_of(msg) in log_text.PENDING


# ---- review fixes ----

REVIEW_NEGATIVES = [
    # 1: infinitives, questions and plain phrases are not commands
    "как убрать живот", "хочу убрать живот к лету", "надо убрать сладкое", "удалить жир с боков как",
    "нужно исправить технику", "поправить осанку", "исправить осанку упражнения", "удали программу",
    "были у врача, а не в зале", "присед был тяжелый а не легкий", "убрать бы самсу", "можно удалить запись",
    "стереть историю", "изменить вес в жиме", "поменял жим на присед, а не на тягу",
    # 2: a new record with "а не" stays a new record
    "съел 2 самсы, а не 3 как обычно", "присел 100 на 5, а не 95", "выпил 2 кофе, а не 1",
    "сделал жим 85, а не 80", "пожал 90 на 3, а не 85", "доел плов, а не бросил",
    # 3: plan and today's weights
    "убери из плана жим", "давай сегодня жим 85, а не 80", "поставь жим 85, а не 80",
    "завтра будет присед 100, а не 95", "в плане жим 85, а не 80", "удали напоминание про креатин",
    # 4: another cup, not a fix
    "ещё кофе без сахара", "еще чай без сахара", "второй бутерброд без сыра", "выпил чай без сахара",
]


@pytest.mark.parametrize("text", REVIEW_NEGATIVES)
def test_review_negatives_do_not_route(text):
    assert se.detect(text, TODAY) is None


@pytest.mark.parametrize("text", ["как убрать живот", "надо убрать сладкое", "нужно исправить технику",
                                  "были у врача, а не в зале", "съел 2 самсы, а не 3 как обычно"])
async def test_review_negatives_reach_the_parser(text, llm, settings, db):
    await add_food(db, T0 - timedelta(hours=1), "самса, 1 шт", kcal=400, grams=150, raw="самса")
    llm.answers = [{"kind": "question", "clarification": "Ответ."}]
    await send(text, llm, settings, db)
    assert len(llm.bodies) == 1 and not hse.OFFERS and not hse.CHOICES


@pytest.mark.parametrize(
    "text",
    ["удали самсу", "удали пожалуйста самсу", "убери самсу", "сотри самсу", "исправь самсу", "поправь плов",
     "измени жим", "самса была 2, а не 3", "не 3, а 2", "2, а не 3", "спал 7, а не 5", "сон был 7, а не 5",
     "в жиме не 80, а 85", "не самса, а беляш", "настроение было отличное, а не плохое", "бутерброд без сыра",
     "обед без хлеба", "удали вчерашнюю тренировку", "удали последний подход"],
)
def test_review_positives_route(text):
    assert se.detect(text, TODAY) is not None


def test_pozhaluysta_is_not_a_record_verb():
    intent = se.detect("удали пожалуйста самсу", TODAY)
    assert intent is not None and intent.words == ("самсу",)


async def test_new_record_with_a_ne_never_edits_yesterday(llm, settings, db):
    await add_food(db, T0 - timedelta(days=1), "самса, 1 шт", kcal=400, grams=150, raw="самса")
    llm.answers = [food(2)]
    msg = await send("съел 2 самсы, а не 3 как обычно", llm, settings, db)
    assert token_of(msg) in log_text.PENDING and not hse.OFFERS


async def test_pair_must_hold_the_old_value_exactly(llm, settings, db):
    await add_food(db, T0 - timedelta(minutes=30), "самса, 1 шт", kcal=400, grams=150, raw="самса")
    msg = await send("самса была 2, а не 3", llm, settings, db)
    assert reply(msg).startswith("Не нашёл «самса» со значением 3") and llm.bodies == [] and not hse.OFFERS


async def test_workout_pair_must_match_the_weight(llm, settings, db):
    await add_workout(db, TODAY, [("Жим штанги лёжа", [(5, 95), (5, 95)])], source="chat", raw="жим 2 по 5 на 95")
    msg = await send("в жиме было 100, а не 90", llm, settings, db)
    assert reply(msg).startswith("Не нашёл") and not hse.OFFERS


async def test_soft_needs_the_ingredient_in_the_saved_record(llm, settings, db):
    await add_food(db, T0 - timedelta(minutes=20), "чай", kcal=2, grams=200, raw="чай")
    llm.answers = [food(1, "чай без сахара")]
    msg = await send("чай без сахара", llm, settings, db)
    assert token_of(msg) in log_text.PENDING and not hse.OFFERS  # another cup: the parser's preview


async def test_soft_edits_when_the_ingredient_is_there(llm, settings, db):
    await add_food(db, T0 - timedelta(minutes=20), "чай с сахаром", kcal=60, grams=200, raw="чай с сахаром")
    llm.answers = [food(1, "чай")]
    msg = await send("чай без сахара", llm, settings, db)
    assert reply(msg).startswith("Исправить?") and len(hse.OFFERS) == 1


async def test_soft_meal_respects_the_hour_and_never_yesterday(llm, settings, db):
    lunch = datetime(2026, 10, 5, 10, 0, tzinfo=UTC)  # yesterday 13:00 in Moscow
    await add_food(db, lunch, "хлеб", kcal=250, grams=100, raw="обед: суп и хлеб")
    await add_food(db, T0 - timedelta(hours=2), "хлеб", kcal=250, grams=100, raw="обед: суп и хлеб")
    llm.answers = [{"kind": "question", "clarification": "Ок."}]
    await send("обед без хлеба", llm, settings, db)
    assert len(llm.bodies) == 1 and not hse.OFFERS  # both lunches are older than SOFT_MINUTES


async def test_soft_meal_within_the_hour_routes(llm, settings, db):
    await add_food(db, T0 - timedelta(minutes=10), "хлеб", kcal=250, grams=100, raw="обед: суп и хлеб")
    llm.answers = [{"kind": "food", "foods": [
        {"description": "суп", "grams": 300, "kcal": 150, "protein_g": 6, "fat_g": 6, "carbs_g": 18}]}]
    msg = await send("обед без хлеба", llm, settings, db)
    assert reply(msg).startswith("Исправить?") and len(hse.OFFERS) == 1


async def test_whole_workout_delete_is_stale_if_a_set_was_added(llm, settings, db):
    wid = await add_workout(db, TODAY - timedelta(days=1), [("Присед", [(5, 100)])])
    d = await send("удали вчерашнюю тренировку", llm, settings, db)
    async with db() as s:
        w = await s.get(Workout, wid)
        ex = await get_or_create_exercise(s, "Присед")
        s.add(WorkoutSet(workout_id=w.id, exercise_id=ex.id, set_index=1, reps=5, weight_kg=100))
        await s.commit()
    cb = callback(f"fixok:{fix_token(d)}")
    await hse.confirm(cb, settings, db)
    assert alert_of(cb) and len(await rows(db, WorkoutSet)) == 2 and len(await rows(db, Workout)) == 1


def test_edited_rejects_an_exercise_without_sets():
    unit = se.Unit("workout", "exercise", (1,), T0, TODAY, ParseResult(kind="workout", exercises=[
        ParsedExercise(exercise="жим", sets=[ParsedSet(reps=8, weight_kg=80)])]))
    empty = ParseResult.model_construct(kind="workout", exercises=[ParsedExercise(exercise="жим", sets=[])])
    assert se.edited(unit, empty) is None


async def test_undo_after_an_edit_that_inserted_sets_in_the_middle(llm, settings, db):
    llm.answers = [
        {"kind": "workout", "exercises": [{"exercise": "Жим штанги лёжа", "sets": [{"reps": 8, "weight_kg": 80}]}]},
        {"kind": "workout", "exercises": [{"exercise": "Присед", "sets": [{"reps": 5, "weight_kg": 100}]}]},
        {"kind": "workout", "exercises": [{"exercise": "Жим штанги лёжа", "sets": [
            {"reps": 8, "weight_kg": 80}, {"reps": 8, "weight_kg": 80}]}]},
    ]
    await saved("жим 8 на 80", llm, settings, db)
    await saved("присед 5 на 100", llm, settings, db, T0 + timedelta(minutes=5))
    e = await send("исправь жим: 2 подхода по 8 на 80", llm, settings, db, T0 + timedelta(minutes=6))
    await hse.confirm(callback(f"fixok:{fix_token(e)}"), settings, db)
    sets = await rows(db, WorkoutSet, WorkoutSet.set_index)
    assert [s.reps for s in sets] == [8, 8, 5]  # the added bench set sits before the squat
    async with db() as s:
        user = await get_or_create_user(s, USER, "Amir")
        removed = await delete_last_chat_sets(s, user)
        await s.commit()
    assert removed == 1  # /undo removes the last message (the squat), not the set added by the edit
    assert [s.reps for s in await rows(db, WorkoutSet, WorkoutSet.set_index)] == [8, 8]


async def test_not_3_but_2_right_after_saving_edits_that_record(llm, settings, db, published):
    llm.answers = [food(3)]
    await saved("три самсы", llm, settings, db)
    llm.answers = [food(2, revises=True)]
    e = await send("не 3, а 2", llm, settings, db, T0 + timedelta(minutes=1))
    assert reply(e).startswith("Исправить?") and "→ самса, 2 шт" in reply(e)
    assert llm.last_messages()[-3] == "самса, 3 шт 450 г"  # the saved record as the previous turn
    await hse.confirm(callback(f"fixok:{fix_token(e)}"), settings, db)
    rows_ = await rows(db, FoodEntry)
    assert [(r.description, r.raw_text) for r in rows_] == [("самса, 2 шт", "три самсы\n[edit] не 3, а 2")]


async def test_not_3_but_2_long_after_saving_goes_to_the_parser(llm, settings, db):
    llm.answers = [food(3)]
    await saved("три самсы", llm, settings, db)
    llm.answers = [food(2)]
    msg = await send("не 3, а 2", llm, settings, db, T0 + hse.LAST_SAVED_WINDOW + timedelta(minutes=1))
    assert token_of(msg) in log_text.PENDING and not hse.OFFERS


async def test_not_80_but_85_right_after_saving_sets_touches_only_that_message(llm, settings, db):
    await add_workout(db, TODAY, [("Жим штанги лёжа", [(8, 80)])], source="chat", raw="жим 8 на 80")
    llm.answers = [{"kind": "workout", "exercises": [
        {"exercise": "Жим штанги лёжа", "sets": [{"reps": 6, "weight_kg": 80}]}]}]
    await saved("жим 6 на 80", llm, settings, db)
    e = await send("не 80, а 85", llm, settings, db, T0 + timedelta(minutes=1))
    assert "85 кг × 6" in reply(e) and llm.bodies and len(llm.bodies) == 1  # deterministic, no model call
    await hse.confirm(callback(f"fixok:{fix_token(e)}"), settings, db)
    sets = await rows(db, WorkoutSet, WorkoutSet.set_index)
    assert [(s.reps, float(s.weight_kg)) for s in sets] == [(8, 80.0), (6, 85.0)]


# ---- which number of "W на R" the pair is about ----


@pytest.mark.parametrize(
    "text, pair, field",
    [
        ("жим 85 на 5, а не 80", (85, 80), "weight_kg"),
        ("жим 85х5 а не 80", (85, 80), "weight_kg"),
        ("жим 85 на 5, а не на 6", (5, 6), "reps"),
        ("в жиме на 6, а не 5", (6, 5), "reps"),
        ("в жиме на 6 раз, а не на 5", (6, 5), "reps"),
        ("в жиме было 85, а не 80", (85, 80), None),
        ("в плове было 250 г, а не 350", (250, 350), None),
    ],
)
def test_pair_takes_the_replaced_number(text, pair, field):
    intent = se.detect(text, TODAY)
    assert intent.pair == pair and intent.pair_field == field


async def test_weight_on_reps_pair_changes_the_weight_only(llm, settings, db):
    await add_workout(db, TODAY, [("Жим штанги лёжа", [(5, 80), (5, 80)])], source="chat", raw="жим 2 по 5 на 80")
    e = await send("жим 85 на 5, а не 80", llm, settings, db)
    assert "85 кг × 5, 85 кг × 5" in reply(e) and llm.bodies == []
    await hse.confirm(callback(f"fixok:{fix_token(e)}"), settings, db)
    sets = await rows(db, WorkoutSet, WorkoutSet.set_index)
    assert [(s.reps, float(s.weight_kg)) for s in sets] == [(5, 85.0), (5, 85.0)]


async def test_reps_pair_changes_the_reps_only(llm, settings, db):
    await add_workout(db, TODAY, [("Жим штанги лёжа", [(5, 5), (5, 80)])], source="chat", raw="жим")
    e = await send("в жиме на 6, а не 5", llm, settings, db)
    assert "5 кг × 6, 80 кг × 6" in reply(e) and llm.bodies == []  # the 5 kg weight stays: the pair is reps


async def test_reps_pair_never_rewrites_a_matching_weight(llm, settings, db):
    await add_workout(db, TODAY, [("Жим штанги лёжа", [(8, 5)])], source="chat", raw="жим")
    e = await send("в жиме на 6, а не 5", llm, settings, db)
    assert reply(e).startswith("Не нашёл") and not hse.OFFERS  # reps were 8, not 5


@pytest.mark.parametrize(
    "text", ["исправь технику в приседе", "поправь технику жима", "исправь форму в тяге", "поправь осанку",
             "исправь технике в приседе"],
)
async def test_technique_goes_to_the_parser(text, llm, settings, db):
    await add_workout(db, TODAY, [("Присед", [(5, 100)])], source="chat", raw="присед")
    assert se.detect(text, TODAY) is None
    llm.answers = [{"kind": "question", "clarification": "Совет."}]
    await send(text, llm, settings, db)
    assert len(llm.bodies) == 1 and not hse.OFFERS
