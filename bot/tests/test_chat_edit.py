"""Program edits from the chat, no LLM: the routing gate, action validation, resolution, ops, preview, apply."""

from datetime import date

import pytest
from sqlalchemy import func, select

from gymbot.db.models import Exercise, Program, WeightOverride
from gymbot.services import chat_edit as ce
from gymbot.services import chat_settings as cs
from gymbot.services import program_editor as pe
from gymbot.services import saved_edits
from gymbot.services.chat_edit import (
    AddA,
    ClarifyA,
    DayRef,
    PrescribeA,
    RemoveA,
    ReorderA,
    ReplaceA,
    WeightA,
)
from gymbot.services.programs import load_program
from gymbot.services.users import active_program, get_or_create_user

TODAY = date(2026, 10, 9)  # Friday: program week 1
FRENCH = "французский жим лёжа"

EDIT_YES = [
    "убери французский жим из дня рук",
    "добавь подтягивания в день спины 3×8",
    "вместо жима в Смите поставь жим гантелей",
    "на разгибания 4 подхода по 10–12",
    "поставь на сгибания 30 кг",
    "поменяй местами руки и спину на этой неделе",
    "сделай сегодня ноги вместо рук",
    "перенеси тренировку на завтра",
    "замени в пятницу французский жим на разгибания",
    "убери французский жим из пятничной тренировки",
]

EDIT_NO = [
    "вместо жима сделал гантели 30 на 8",
    "жим 85 на 8",
    "сделал подтягивания 3×8",
    "что поменять в дне рук?",
    "убери самсу",
    "съел 2 самсы",
    "сделал жим 80×8",
    "что сегодня?",
    "удали последнюю запись",
    "запиши самочувствие",
    "жим 80×8",
    "подтягивания 3×8",
    "жим лёжа 4х8 по 80",
    "удали вчерашнюю тренировку",
    "удали запись за понедельник",
    "поставь сегодня жим 85",  # stays with settings
    "добавь напоминание на каждый день",
    "перенеси старт программы на понедельник",
    "норма белка 170 в день",
    "на брусьях 3×12",
    "на жим 4 подхода по 8 с 80",
    "поставь таймер отдыха 2 минуты",
    "удали самсу за пятницу",
    # stage 2 (deload, a lighter day) is not routed yet: these are mostly wellbeing
    "следующая неделя — делоад",
    "сегодня облегчённо, −20 %",
    "болит плечо, сегодня облегчённо потренируюсь",
    "на этой неделе делоад, спал плохо",
    # settings own weights for today and program switches
    "поставь сегодня жим 85 кг",
    "поменяй программу на другую",
    # food
    "замени рис на гречку в понедельник",
    "удали ужин в понедельник",
]


@pytest.mark.parametrize("text", EDIT_YES)
def test_gate_accepts_edit_commands(text):
    assert ce.is_edit_command(text)


@pytest.mark.parametrize("text", EDIT_NO)
def test_gate_rejects_records_questions_and_settings(text):
    assert not ce.is_edit_command(text)


@pytest.mark.parametrize(
    "text",
    ["удали последний подход на этой неделе", "удали 2 последних подхода, сегодня день ног",
     "убери последний подход в среду"],
)
def test_saved_edits_still_sees_deletes_that_pass_the_gate(text):
    """The edit model is asked first; when it declines, saved_edits must still recognize the delete."""
    assert ce.is_edit_command(text)
    intent = saved_edits.detect(text, TODAY)
    assert intent is not None and intent.action == "delete"


def test_settings_own_todays_weight_with_kg():
    assert cs.is_settings_request("поставь сегодня жим 85 кг") and not ce.is_edit_command("поставь сегодня жим 85 кг")


# ---- parse_actions ----


def test_parse_actions_drops_invalid_with_a_note_and_keeps_valid():
    valid, notes = ce.parse_actions({"actions": [
        {"type": "add", "day": {"weekday": 5}, "name": "подтягивания", "sets": 50, "repsMin": 8},
        {"type": "weight", "said": "жим", "weight_kg": 0},
        {"type": "remove", "day": {"weekday": 5}, "exercise": FRENCH},
    ]})
    assert [type(a) for a in valid] == [RemoveA]
    assert len(notes) == 2 and all(n.startswith("Не применю — ") for n in notes)


def test_parse_actions_same_invalid_kind_gives_one_note():
    _, notes = ce.parse_actions({"actions": [
        {"type": "weight", "said": "жим", "weight_kg": 0},
        {"type": "weight", "said": "тяга", "weight_kg": 9999},
    ]})
    assert len(notes) == 1


@pytest.mark.parametrize("data", [None, "x", {"actions": "x"}, {}, {"actions": None}, []])
def test_parse_actions_garbage(data):
    assert ce.parse_actions(data) == ([], [])


def test_parse_actions_unknown_type_is_dropped_silently():
    assert ce.parse_actions({"actions": [{"type": "teleport", "day": {}}]}) == ([], [])


def test_parse_actions_bad_scope_and_intensity_become_none():
    valid, _ = ce.parse_actions({"actions": [
        {"type": "prescribe", "day": None, "exercise": FRENCH, "sets": 4, "scope": "forever", "intensity": "crazy"}
    ]})
    assert valid[0].scope is None and valid[0].intensity is None and valid[0].sets == 4


def test_parse_actions_prescribe_without_change_is_dropped_silently():
    valid, notes = ce.parse_actions({"actions": [{"type": "prescribe", "exercise": FRENCH}]})
    assert valid == [] and notes == []


# ---- the program snapshot ----


@pytest.fixture
async def snap(db):
    async with db() as s:
        user = await get_or_create_user(s, 42)
        snapshot = await ce.load_snapshot(s, user, TODAY)
        await s.commit()
    return snapshot


def day(week=1, weekday=5, **kw):
    return DayRef(weekday=weekday, **kw)


def item(snap, weekday, name, week=1):
    return next(i for i in snap.day(week, weekday).items if i.name == name)


def test_snapshot_is_the_template_in_week_one(snap):
    assert snap.template and snap.week == 1 and len(snap.weeks) == 8
    assert snap.day(1, 5).focus == "Руки и плечи" and snap.day(1, 3).focus == "База"


# ---- resolve_day ----


def labels(days):
    return [(d.week, d.weekday) for d in days]


def test_resolve_day_by_weekday(snap):
    assert labels(ce.resolve_day(snap, DayRef(weekday=5))) == [(1, 5)]
    assert ce.resolve_day(snap, DayRef(weekday=2)) == []


def test_resolve_day_today_and_tomorrow(snap):
    assert labels(ce.resolve_day(snap, DayRef(when="today"))) == [(1, 5)]
    assert ce.resolve_day(snap, DayRef(when="tomorrow")) == []  # Saturday: rest


def test_resolve_day_by_focus(snap):
    assert labels(ce.resolve_day(snap, DayRef(focus="база"))) == [(1, 3)]
    assert labels(ce.resolve_day(snap, DayRef(focus="руки"))) == [(1, 1), (1, 5)]


def test_resolve_day_focus_narrowed_by_exercise(snap):
    assert labels(ce.resolve_day(snap, DayRef(focus="руки"), "французский жим")) == [(1, 5)]


def test_resolve_day_by_composition(snap):
    assert labels(ce.resolve_day(snap, DayRef(focus="спина"))) == [(1, 3)]


# ---- resolve_item ----


def test_resolve_item_exact_name(snap):
    hits, exact = ce.resolve_item(snap.day(1, 5), "жим сидя в смите")
    assert [h.name for h in hits] == ["жим сидя в смите"] and exact


def test_resolve_item_short_name(snap):
    hits, exact = ce.resolve_item(snap.day(1, 5), "французский")
    assert [h.name for h in hits] == [FRENCH] and exact


def test_resolve_item_ambiguous_words(snap):
    hits, exact = ce.resolve_item(snap.day(1, 5), "сгибания")
    assert len(hits) == 2 and not exact


def test_resolve_item_not_in_day(snap):
    assert ce.resolve_item(snap.day(1, 5), "становая тяга") == ([], False)


# ---- scopes ----


def test_scope_weeks():
    weeks = list(range(1, 9))
    assert ce.scope_weeks(None, 3, weeks) == [3]
    assert ce.scope_weeks("this_week", 3, weeks) == [3]
    assert ce.scope_weeks("from_this_week", 3, weeks) == [3, 4, 5, 6, 7, 8]
    assert ce.scope_weeks("all_weeks", 3, weeks) == weeks


def test_format_weeks():
    assert ce.format_weeks([1, 2, 3, 5]) == "1–3, 5"
    assert ce.format_weeks([1, 2]) == "1, 2"
    assert ce.format_weeks([5, 3, 4, 4]) == "3–5"


# ---- compile_ops ----


def test_compile_remove(snap):
    plan = ce.compile_ops(snap, [RemoveA(type="remove", day=day(), exercise="французский жим")])
    fid = item(snap, 5, FRENCH).id
    assert plan.ops == [{"op": "remove", "week": 1, "weekday": 5, "weeks": [1], "itemId": fid}]
    assert plan.lines == [f"пт «Руки и плечи»: убрать {FRENCH} (только неделя 1)"]
    assert plan.ready() and plan.forks and plan.live_topics() == ["program", "plan", "state"]


def test_compile_replace_defaults_to_all_weeks(snap):
    a = ReplaceA(type="replace", day=day(), exercise="жим в смите", new_name="жим гантелей сидя")
    plan = ce.compile_ops(snap, [a])
    assert plan.ops == [{
        "op": "replace", "week": 1, "weekday": 5, "weeks": list(range(1, 9)),
        "itemId": item(snap, 5, "жим сидя в смите").id, "name": "жим гантелей сидя",
    }]
    assert "все недели" in plan.lines[0] and "новое упражнение" not in plan.lines[0]


def test_compile_replace_unknown_name_is_flagged_new(snap):
    a = ReplaceA(type="replace", day=day(), exercise="жим в смите", new_name="жим колен", scope="this_week")
    plan = ce.compile_ops(snap, [a])
    assert plan.ops[0]["weeks"] == [1] and plan.ops[0]["name"] == "жим колен"
    assert ce.NEW_EXERCISE in plan.lines[0]


def test_compile_replace_with_itself_is_a_note(snap):
    plan = ce.compile_ops(snap, [ReplaceA(type="replace", day=day(), exercise=FRENCH, new_name=FRENCH)])
    assert plan.ops == [] and "уже стоит" in plan.notes[0]


def test_compile_prescribe_sets_only_keeps_reps(snap):
    it = item(snap, 5, FRENCH)
    plan = ce.compile_ops(snap, [PrescribeA(type="prescribe", day=day(), exercise=FRENCH, sets=it.sets + 1)])
    assert plan.ops == [{
        "op": "prescribe", "week": 1, "weekday": 5, "weeks": [1],
        "itemId": it.id, "sets": it.sets + 1, "repsMin": it.reps_min, "repsMax": it.reps_max,
    }]


def test_compile_prescribe_reps_only_keeps_sets(snap):
    it = item(snap, 5, FRENCH)
    plan = ce.compile_ops(snap, [PrescribeA(type="prescribe", day=day(), exercise=FRENCH, repsMin=10, repsMax=12)])
    op = plan.ops[0]
    assert (op["sets"], op["repsMin"], op["repsMax"]) == (it.sets, 10, 12)
    assert "10–12" in plan.lines[0]


def test_compile_prescribe_drop_sets(snap):
    a = PrescribeA(type="prescribe", day=day(), exercise=FRENCH, sets=3, dropReps=[12, 6, 6], scope="all_weeks")
    plan = ce.compile_ops(snap, [a])
    assert plan.ops[0]["dropReps"] == [12, 6, 6] and "repsMin" not in plan.ops[0]
    assert plan.ops[0]["weeks"] == list(range(1, 9))
    assert "3× дропсет 12-6-6" in plan.lines[0]


def test_compile_prescribe_intensity_only(snap):
    plan = ce.compile_ops(snap, [PrescribeA(type="prescribe", day=day(), exercise=FRENCH, intensity="light")])
    assert plan.ops[0]["intensity"] == "light"


def test_compile_prescribe_same_as_now_is_a_note(snap):
    it = item(snap, 5, FRENCH)
    plan = ce.compile_ops(snap, [PrescribeA(type="prescribe", day=day(), exercise=FRENCH, sets=it.sets)])
    assert plan.ops == [] and "уже" in plan.notes[0]


def test_compile_add_defaults(snap):
    plan = ce.compile_ops(snap, [AddA(type="add", day=day(), name="подтягивания")])
    n = len(snap.day(1, 5).items)
    assert plan.ops == [{
        "op": "add", "week": 1, "weekday": 5, "weeks": [1], "tempId": "c1", "name": "подтягивания",
        "position": n + 1, "sets": 3, "repsMin": 8, "repsMax": 12,
    }]
    assert "3×8–12 (по умолчанию)" in plan.lines[0]
    assert "(по умолчанию)" in plan.lines[0]


def test_compile_add_explicit_prescription_has_no_default_mark(snap):
    plan = ce.compile_ops(snap, [AddA(type="add", day=day(), name="подтягивания", sets=4, repsMin=6)])
    assert plan.ops[0]["sets"] == 4 and (plan.ops[0]["repsMin"], plan.ops[0]["repsMax"]) == (6, 6)
    assert "по умолчанию" not in plan.lines[0]


def test_compile_add_only_reps_marks_sets_default(snap):
    plan = ce.compile_ops(snap, [AddA(type="add", day=day(), name="подтягивания", repsMin=8)])
    assert "(подходы по умолчанию)" in plan.lines[0]


def test_compile_add_drop_sets(snap):
    plan = ce.compile_ops(snap, [AddA(type="add", day=day(), name="подтягивания", sets=3, dropReps=[12, 6, 6])])
    assert plan.ops[0]["dropReps"] == [12, 6, 6] and "repsMin" not in plan.ops[0]
    assert "3× дропсет 12-6-6" in plan.lines[0]


def test_compile_add_after_sets_position(snap):
    french = item(snap, 5, FRENCH)
    plan = ce.compile_ops(snap, [AddA(type="add", day=day(), name="подтягивания", sets=3, repsMin=8, after="французский")])
    assert plan.ops[0]["position"] == french.order + 1
    assert f"после «{FRENCH}»" in plan.lines[0]


def test_compile_add_existing_exercise_is_a_note(snap):
    plan = ce.compile_ops(snap, [AddA(type="add", day=day(), name=FRENCH)])
    assert plan.ops == [] and "уже есть" in plan.notes[0]


def test_compile_reorder(snap):
    ids = [i.id for i in snap.day(1, 5).items]
    last = snap.day(1, 5).items[-1]
    plan = ce.compile_ops(snap, [ReorderA(type="reorder", day=day(), order=[last.name])])
    assert plan.ops == [{
        "op": "reorder", "week": 1, "weekday": 5, "weeks": [1], "itemIds": [ids[-1], *ids[:-1]],
    }]


def test_compile_reorder_same_order_is_a_note(snap):
    first = snap.day(1, 5).items[0]
    plan = ce.compile_ops(snap, [ReorderA(type="reorder", day=day(), order=[first.name])])
    assert plan.ops == [] and "порядок уже такой" in plan.notes[0]


def test_compile_two_candidate_days_ask_which_one(snap):
    a = AddA(type="add", day=DayRef(focus="руки"), name="подтягивания")
    plan = ce.compile_ops(snap, [a])
    assert plan.ops == [] and not plan.ready()
    c = plan.clarify
    assert c.question.startswith("Какой день") and len(c.options) == 2
    assert c.options[0].startswith("пн") and c.options[1].startswith("пт")
    assert c.picks == [ce.Pick(0, "day", (1, 1)), ce.Pick(0, "day", (1, 5))]


def test_compile_pick_resolves_the_day(snap):
    a = AddA(type="add", day=DayRef(focus="руки"), name="подтягивания")
    plan = ce.compile_ops(snap, [a], {(0, "day"): (1, 5)})
    assert plan.clarify is None and plan.ops[0]["weekday"] == 5


def test_compile_ambiguous_exercise_asks_which_one(snap):
    plan = ce.compile_ops(snap, [RemoveA(type="remove", day=day(), exercise="сгибания")])
    assert plan.clarify is not None and len(plan.clarify.options) == 2
    pick = plan.clarify.picks[1]
    done = ce.compile_ops(snap, [RemoveA(type="remove", day=day(), exercise="сгибания")], {(0, pick.what): pick.value})
    assert done.clarify is None and done.ops[0]["itemId"] == pick.value


def test_compile_exercise_not_in_day_is_a_note(snap):
    plan = ce.compile_ops(snap, [RemoveA(type="remove", day=day(), exercise="становая тяга")])
    assert plan.ops == [] and plan.clarify is None
    assert "нет «становая тяга»" in plan.notes[0] and not plan.ready()


def test_compile_day_without_workout_is_a_note(snap):
    plan = ce.compile_ops(snap, [RemoveA(type="remove", day=DayRef(when="tomorrow"), exercise=FRENCH)])
    assert plan.ops == [] and plan.notes == ["Завтра по программе тренировки нет."]


def test_compile_models_own_clarify(snap):
    plan = ce.compile_ops(snap, [ClarifyA(type="clarify", question="Какие?", options=["а", "б"])])
    assert plan.clarify.question == "Какие?" and plan.clarify.options == ["а", "б"]
    assert plan.clarify.picks == [None, None]


def test_compile_clarify_without_options_is_a_note(snap):
    plan = ce.compile_ops(snap, [ClarifyA(type="clarify", question="Что именно?")])
    assert plan.clarify is None and plan.notes == ["Что именно?"]


def test_compile_weight_defaults_to_the_nearest_program_day(snap):
    a = WeightA(type="weight", said="сгибания с гантелями на бицепс с супинацией", weight_kg=30)
    plan = ce.compile_ops(snap, [a])
    assert plan.ops == [] and plan.ready()
    (w,) = plan.weights
    assert (w.name, w.day, w.kg) == ("сгибания с гантелями на бицепс с супинацией", date(2026, 10, 12), 30)
    assert plan.live_topics() == ["state"] and plan.lines == ["Вес на пн 12.10: " + w.name + " 30 кг"]


def test_compile_weight_explicit_date(snap):
    a = WeightA(type="weight", said="жим", exercise="жим лёжа", weight_kg=87.5, date=date(2026, 10, 10))
    plan = ce.compile_ops(snap, [a])
    assert [(w.name, w.day, w.kg) for w in plan.weights] == [("жим лёжа", date(2026, 10, 10), 87.5)]


def test_compile_weight_in_the_past_is_a_note(snap):
    a = WeightA(type="weight", said="жим", exercise="жим лёжа", weight_kg=80, date=date(2026, 10, 8))
    plan = ce.compile_ops(snap, [a])
    assert plan.weights == [] and "не ставлю" in plan.notes[0]


def test_compile_weight_unknown_exercise_is_a_note(snap):
    plan = ce.compile_ops(snap, [WeightA(type="weight", said="подъём на носки", weight_kg=50)])
    assert plan.weights == [] and "Нет в программе" in plan.notes[0]


def test_compile_weight_last_one_said_wins(snap):
    a = [
        WeightA(type="weight", said="жим", exercise="жим лёжа", weight_kg=80, date=date(2026, 10, 14)),
        WeightA(type="weight", said="жим", exercise="жим лёжа", weight_kg=85, date=date(2026, 10, 14)),
    ]
    plan = ce.compile_ops(snap, a)
    assert [w.kg for w in plan.weights] == [85] and len(plan.lines) == 1


# ---- preview and apply on the database ----


async def staged(db, actions, picks=None):
    """The plan for `actions` plus the objects preview/apply take, in one open session."""
    s = db()
    session = await s.__aenter__()
    user = await get_or_create_user(session, 42)
    snapshot = await ce.load_snapshot(session, user, TODAY)
    plan = ce.compile_ops(snapshot, actions, picks)
    up = await active_program(session, user, TODAY)
    return s, session, user, up, plan


async def count_programs(db):
    async with db() as s:
        owned = await s.scalar(select(func.count()).select_from(Program).where(Program.owner_user_id.is_not(None)))
        versions = dict((await s.execute(select(Program.slug, Program.version))).all())
    return owned, versions


async def count_exercises(db) -> int:
    async with db() as s:
        return await s.scalar(select(func.count()).select_from(Exercise)) or 0


async def test_preview_writes_nothing(db):
    before = await count_programs(db)
    exercises = await count_exercises(db)
    s, session, user, up, plan = await staged(db, [
        RemoveA(type="remove", day=day(), exercise=FRENCH),
        AddA(type="add", day=day(), name="подтягивания с резинкой"),
        ReplaceA(type="replace", day=day(), exercise="жим сидя в смите", new_name="жим в хаммере сидя"),
    ])
    assert len(plan.ops) == 3
    try:
        await ce.preview(session, user, up, plan)
        assert plan.copy_name and plan.copy_name.endswith(pe.COPY_MARK)
    finally:
        await s.__aexit__(None, None, None)
    owned, versions = await count_programs(db)
    assert owned == 0 and (owned, versions) == (0, before[1])
    assert await count_exercises(db) == exercises  # the dry run's new exercises are rolled back too


async def test_apply_on_the_template_forks_and_edits_the_copy(db):
    s, session, user, up, plan = await staged(db, [RemoveA(type="remove", day=day(), exercise=FRENCH)])
    template_slug, template_version = plan.slug, plan.version
    try:
        assert await ce.apply(session, user, up, plan, TODAY, "убери французский жим") == []
        await session.commit()
    finally:
        await s.__aexit__(None, None, None)
    owned, versions = await count_programs(db)
    assert owned == 1 and versions[template_slug] == template_version
    async with db() as session:
        user = await get_or_create_user(session, 42)
        copy = await ce.load_snapshot(session, user, TODAY)
        assert not copy.template and copy.slug != template_slug
        assert FRENCH not in [i.name for i in copy.day(1, 5).items]
        assert FRENCH in [i.name for i in copy.day(2, 5).items]  # this week only
        tpl = await session.scalar(select(Program).where(Program.slug == template_slug))
        assert FRENCH in [i.exercise.name for i in (await load_program(session, tpl.id)).weeks[0].days[2].items]


async def test_replace_on_all_weeks_reports_skipped_weeks(db):
    a = ReplaceA(type="replace", day=DayRef(weekday=3), exercise="жим лёжа", new_name="жим гантелей лёжа")
    s, session, user, up, plan = await staged(db, [a])
    try:
        await ce.preview(session, user, up, plan)
    finally:
        await s.__aexit__(None, None, None)
    assert plan.ops[0]["weeks"] == list(range(1, 9))
    skip = [ln for ln in plan.lines if "пропущу" in ln]
    assert skip == ["жим лёжа → жим гантелей лёжа: недели 3–5 пропущу — там другое упражнение"]


async def test_stale_version_is_a_conflict(db):
    s, session, user, up, plan = await staged(db, [RemoveA(type="remove", day=day(), exercise=FRENCH)])
    plan.version += 1
    try:
        with pytest.raises(pe.Conflict):
            await ce.apply(session, user, up, plan, TODAY, "x")
    finally:
        await s.__aexit__(None, None, None)
    assert (await count_programs(db))[0] == 0


async def test_program_changed_between_preview_and_apply_is_a_conflict(db):
    async with db() as session:  # the user and the program choice exist before two sessions overlap
        await active_program(session, await get_or_create_user(session, 42), TODAY)
        await session.commit()
    first = [RemoveA(type="remove", day=day(), exercise=FRENCH)]
    s1, ses1, user1, up1, plan1 = await staged(db, first)
    second = [RemoveA(type="remove", day=day(), exercise="жим сидя в смите")]
    s2, ses2, user2, up2, plan2 = await staged(db, second)
    try:
        await ce.apply(ses1, user1, up1, plan1, TODAY, "first")
        await ses1.commit()
    finally:
        await s1.__aexit__(None, None, None)
    try:
        with pytest.raises(pe.Conflict):  # plan2 still points at the template, the copy is active now
            await ce.apply(ses2, user2, up2, plan2, TODAY, "second")
    finally:
        await s2.__aexit__(None, None, None)
    owned, _ = await count_programs(db)
    assert owned == 1


async def test_apply_writes_the_weight_for_the_day(db):
    a = WeightA(type="weight", said="сгибания с гантелями на бицепс с супинацией", weight_kg=30)
    s, session, user, up, plan = await staged(db, [a])
    try:
        assert await ce.apply(session, user, up, plan, TODAY, "поставь на сгибания 30 кг") == []
        await session.commit()
    finally:
        await s.__aexit__(None, None, None)
    async with db() as session:
        (row,) = (await session.scalars(select(WeightOverride))).all()
    assert (row.day, float(row.weight_kg)) == (date(2026, 10, 12), 30.0)
    assert (await count_programs(db))[0] == 0  # weights alone never fork the program


async def test_apply_skips_a_day_that_has_passed(db):
    a = WeightA(type="weight", said="жим", exercise="жим лёжа", weight_kg=80, date=date(2026, 10, 14))
    s, session, user, up, plan = await staged(db, [a])
    assert any(ln.startswith("Вес на ") for ln in plan.lines)
    try:
        notes = await ce.apply(session, user, up, plan, date(2026, 10, 20), "x")
        await session.commit()
    finally:
        await s.__aexit__(None, None, None)
    assert len(notes) == 1 and "не выставлен" in notes[0]
    assert not any(ln.startswith("Вес на ") for ln in plan.lines)  # «Готово» does not list it
    async with db() as session:
        assert (await session.scalars(select(WeightOverride))).all() == []
