"""Program edits from the chat, no LLM: the routing gate, action validation, resolution, ops, preview, apply."""

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import func, select

from gymbot.db.models import DeloadState, Exercise, Program, WeightOverride, Workout
from gymbot.services import chat_edit as ce
from gymbot.services import chat_settings as cs
from gymbot.services import day_adjustments as dayadj
from gymbot.services import deload, saved_edits
from gymbot.services import program_editor as pe
from gymbot.services.chat_edit import (
    AddA,
    AdjustDayA,
    ClarifyA,
    ClearDayA,
    DayRef,
    DeloadA,
    MoveDayA,
    PrescribeA,
    RemoveA,
    ReorderA,
    ReplaceA,
    SwapDaysA,
    WeightA,
)
from gymbot.services.day_adjustments import DayAdjust
from gymbot.services.programs import load_program, normalize
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
    # stage 2: swaps, moves and a deload week
    "перенеси пятницу на субботу",
    "поменяй местами руки и базу",
    "следующая неделя — делоад",
    "давай на этой неделе делоад",
    "со следующей недели разгрузка",
    "сделай разгрузочную неделю с понедельника",
    # stage 3: a lighter day
    "сегодня облегчённо, −20 %",
    "сегодня полегче",
    "на завтра на подход меньше",
    "в пятницу без ног",
    "на 10 % легче",
    "без ног",
    "верни как было сегодня",
    "отмени поправку на пятницу",
    "сегодня на 2 подхода меньше",
    # the day replaced wholesale, "тренировку" as the structure word, the "давай …им" forms
    "замени сегодняшнюю тренировку на присед 3×8, жим лёжа 3×8 на 65 кг, тягу блока 3×10 на 60 кг, румынскую тягу 2×10 на 75 кг",
    "замени тренировку на сегодня на присед 3×8, жим лёжа 3×8 на 65 кг",
    "поменяй сегодняшнюю тренировку на базу",
    "давай добавим ноги в тренировку",
    "добавь присед 3×8 в тренировку",
    "давай заменим французский жим на разгибания в день рук",
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
    "удали сегодняшнюю тренировку",  # a saved record: a delete verb
    "убери сегодняшнюю запись",
    "запиши сегодняшнюю тренировку",
    "сделал сегодняшнюю тренировку",
    "удали запись за понедельник",
    # a request to the bot, not a command
    "а ты мне зачем сам продумай и запиши",
    "так сам запиши",
    "кнопки нет, сам подтверди",
    "сейчас первая неделя среда",
    "поставь сегодня жим 85",  # stays with settings
    "добавь напоминание на каждый день",
    "перенеси старт программы на понедельник",
    "норма белка 170 в день",
    "на брусьях 3×12",
    "на жим 4 подхода по 8 с 80",
    "поставь таймер отдыха 2 минуты",
    "удали самсу за пятницу",
    # how the user feels is wellbeing even next to "делоад" or "облегчённо"
    "болит плечо, сегодня облегчённо потренируюсь",
    "на этой неделе делоад, спал плохо",
    "сделай на этой неделе делоад, устал",
    "разгрузочная неделя, болит колено",
    "делоад нужен, сил нет на этой неделе",
    "завтра разгрузочный день на кефире",  # a diet's fasting day, not a deload week
    "на этой неделе разгрузочные дни по еде",
    "неделю на разгрузке по питанию",
    "неделя разгрузки по углеводам",
    "следующая неделя разгрузочная, диета",
    # how the user feels is never an edit, whatever the branch
    "перенеси тренировку на завтра, болит спина",
    "перенеси тренировку на завтра, спал плохо",
    "поменяй местами руки и базу, плечо ноет",
    "сделай сегодня ноги вместо рук, потянул спину",
    "на этой неделе делоад, чувствую себя плохо",
    "следующая неделя — делоад, простуда",
    # settings own weights for today and program switches
    "поставь сегодня жим 85 кг",
    "поменяй программу на другую",
    # food
    "замени рис на гречку в понедельник",
    "удали ужин в понедельник",
    # a lighter day: a question, how the user feels, a log of what was done, food
    "как облегчить завтрашнюю тренировку",
    "сегодня полегче, спина болит",
    "сегодня полегче, устал",
    "сегодня было легче",
    "жим 80 на 8 без ног",
    "кофе без сахара",
    "полегче с углеводами",
    "сегодня полегче?",
    "жим шёл легче",
    "жим 80 легче",
    "сделал на подход меньше",
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


# ---- compile_ops: swap, move, deload ----

WED, FRI = 3, 5


def swap(a, b, **kw):
    return SwapDaysA(type="swap_days", a=a, b=b, **kw)


def move(src, dst, **kw):
    return MoveDayA(type="move_day", src=src, dst=dst, **kw)


def test_compile_swap_today_with_a_labelled_day(snap):
    plan = ce.compile_ops(snap, [swap(DayRef(when="today"), DayRef(focus="база"))])
    assert plan.ops == [{"op": "move_day", "week": 1, "weekday": FRI, "weeks": [1], "toWeekday": WED}]
    assert plan.lines == [
        "пт 09.10: «База» вместо «Руки и плечи» (только неделя 1)",
        "ср 07.10: «Руки и плечи» вместо «База» (только неделя 1)",
    ]
    assert plan.ready() and plan.forks and plan.live_topics() == ["program", "plan", "state"]


def test_compile_swap_scope_all_weeks(snap):
    plan = ce.compile_ops(snap, [swap(DayRef(weekday=FRI), DayRef(weekday=WED), scope="all_weeks")])
    assert plan.ops[0]["weeks"] == list(range(1, 9))
    assert "все недели" in plan.lines[0]


def test_compile_move_today_to_a_free_tomorrow(snap):
    plan = ce.compile_ops(snap, [move(DayRef(when="today"), DayRef(when="tomorrow"))])
    assert plan.ops == [{"op": "move_day", "week": 1, "weekday": FRI, "weeks": [1], "toWeekday": 6}]
    assert plan.lines == ["«Руки и плечи»: пт 09.10 → сб 10.10 (только неделя 1)"]
    assert plan.notes == []


def test_compile_move_onto_an_occupied_day_becomes_a_swap_with_a_note(snap):
    plan = ce.compile_ops(snap, [move(DayRef(weekday=FRI), DayRef(weekday=WED))])
    assert plan.ops == [{"op": "move_day", "week": 1, "weekday": FRI, "weeks": [1], "toWeekday": WED}]
    assert len(plan.lines) == 2
    assert any("уже занято" in n and "поменяю дни местами" in n for n in plan.move_notes)


def test_compile_swap_with_a_rest_day_is_a_move_of_the_other_day(snap):
    plan = ce.compile_ops(snap, [swap(DayRef(weekday=6), DayRef(focus="база"))])
    assert plan.ops == [{"op": "move_day", "week": 1, "weekday": WED, "weeks": [1], "toWeekday": 6}]
    assert plan.lines == ["«База»: ср 07.10 → сб 10.10 (только неделя 1)"]
    assert plan.notes == []


def test_compile_move_of_a_rest_day_is_a_note(snap):
    plan = ce.compile_ops(snap, [move(DayRef(weekday=6), DayRef(weekday=FRI))])
    assert plan.ops == [] and "тренировки нет" in plan.notes[0] and not plan.ready()


def test_compile_swap_a_day_with_itself_is_a_note(snap):
    plan = ce.compile_ops(snap, [swap(DayRef(weekday=FRI), DayRef(when="today"))])
    assert plan.ops == [] and plan.notes == ["Это один и тот же день, менять нечего."]


def test_compile_swap_two_rest_days_is_a_note(snap):
    plan = ce.compile_ops(snap, [swap(DayRef(weekday=6), DayRef(weekday=7))])
    assert plan.ops == [] and "тренировок нет" in plan.notes[0] and not plan.ready()


def test_compile_swap_ambiguous_first_day_asks_and_the_pick_resolves(snap):
    actions = [swap(DayRef(focus="руки"), DayRef(weekday=WED))]
    plan = ce.compile_ops(snap, actions)
    assert plan.ops == [] and not plan.ready()
    assert plan.clarify.picks == [ce.Pick(0, "day", (1, 1)), ce.Pick(0, "day", (1, 5))]
    done = ce.compile_ops(snap, actions, {(0, "day"): (1, 1)})
    assert done.clarify is None
    assert done.ops == [{"op": "move_day", "week": 1, "weekday": 1, "weeks": [1], "toWeekday": WED}]


def test_compile_swap_ambiguous_second_day_asks_with_day2(snap):
    actions = [swap(DayRef(weekday=WED), DayRef(focus="руки"))]
    plan = ce.compile_ops(snap, actions)
    assert plan.clarify is not None
    assert [p.what for p in plan.clarify.picks] == ["day2", "day2"]
    done = ce.compile_ops(snap, actions, {(0, "day2"): (1, 1)})
    assert done.clarify is None
    assert done.ops == [{"op": "move_day", "week": 1, "weekday": WED, "weeks": [1], "toWeekday": 1}]


async def _check_workout(db, day_id: int, on: date) -> None:
    async with db() as s:
        user = await get_or_create_user(s, 42)
        s.add(Workout(user_id=user.id, performed_on=on, program_day_id=day_id, source="miniapp"))
        await s.commit()


async def test_compile_swap_warns_about_a_checked_day(db, snap):
    wed = snap.day(1, WED)
    await _check_workout(db, wed.id, date(2026, 10, 7))
    async with db() as s:
        user = await get_or_create_user(s, 42)
        fresh = await ce.load_snapshot(s, user, TODAY)
    assert fresh.done == {wed.id}
    plan = ce.compile_ops(fresh, [swap(DayRef(when="today"), DayRef(focus="база"))])
    assert plan.ops
    assert any("«База» уже отмечен ✓" in n and "переедет вместе с днём на пт 09.10" in n for n in plan.move_notes)
    clean = ce.compile_ops(snap, [swap(DayRef(when="today"), DayRef(focus="база"))])
    assert not any("✓" in n for n in clean.notes)


def test_compile_move_into_the_past_warns(snap):
    plan = ce.compile_ops(snap, [move(DayRef(weekday=FRI), DayRef(weekday=2))])
    assert plan.ops and any("вт 06.10" in n and "уже прошёл" in n for n in plan.move_notes)


def test_compile_deload_next_week_starts_next_monday(snap):
    plan = ce.compile_ops(snap, [DeloadA(type="deload", week="next_week")])
    assert plan.deload == ce.DeloadChange(date(2026, 10, 12), date(2026, 10, 18))
    (line,) = plan.lines
    assert line.startswith(ce.DELOAD_LINE) and "с пн 12.10 по вс 18.10 (7 дней)" in line
    assert plan.ops == [] and plan.ready() and plan.live_topics() == ["plan"]


def test_compile_deload_this_week_starts_today(snap):
    plan = ce.compile_ops(snap, [DeloadA(type="deload", week="this_week")])
    assert plan.deload == ce.DeloadChange(TODAY, date(2026, 10, 15))
    assert "с пт 09.10 по чт 15.10" in plan.lines[0]


def test_compile_deload_with_a_named_start(snap):
    plan = ce.compile_ops(snap, [DeloadA(type="deload", start=date(2026, 10, 19))])
    assert plan.deload.first == date(2026, 10, 19)


def test_compile_deload_start_in_the_past_is_a_note(snap):
    plan = ce.compile_ops(snap, [DeloadA(type="deload", start=date(2026, 10, 5))])
    assert plan.deload is None and not plan.ready()
    assert "уже прошёл" in plan.notes[0]


def test_compile_deload_too_far_ahead_is_a_note(snap):
    plan = ce.compile_ops(snap, [DeloadA(type="deload", start=date(2026, 12, 7))])
    assert plan.deload is None and str(ce.DELOAD_AHEAD) in plan.notes[0]


async def _snapshot_with_deload(db, first: date):
    async with db() as s:
        user = await get_or_create_user(s, 42)
        await deload.start(s, user.id, TODAY, datetime(2026, 10, 9, 9, tzinfo=UTC), start_on=first)
        snapshot = await ce.load_snapshot(s, user, TODAY)
        await s.commit()
    return snapshot


async def test_compile_deload_already_running_is_a_note(db):
    running = await _snapshot_with_deload(db, TODAY)
    assert running.deload == (TODAY, date(2026, 10, 15))
    plan = ce.compile_ops(running, [DeloadA(type="deload", week="next_week")])
    assert plan.deload is None and not plan.ready()
    assert "уже идёт" in plan.notes[0]


async def test_compile_deload_already_scheduled_the_same_day_is_a_note(db):
    scheduled = await _snapshot_with_deload(db, date(2026, 10, 12))
    plan = ce.compile_ops(scheduled, [DeloadA(type="deload", week="next_week")])
    assert plan.deload is None and "уже запланирована" in plan.notes[0]


async def test_compile_deload_replaces_a_scheduled_one_on_another_day(db):
    scheduled = await _snapshot_with_deload(db, date(2026, 10, 12))
    plan = ce.compile_ops(scheduled, [DeloadA(type="deload", week="this_week")])
    assert plan.deload.first == TODAY and "вместо запланированной с пн 12.10 по вс 18.10" in plan.lines[0]


def test_compile_deload_last_one_said_wins(snap):
    plan = ce.compile_ops(snap, [DeloadA(type="deload", week="this_week"), DeloadA(type="deload", week="next_week")])
    assert plan.deload.first == date(2026, 10, 12) and len(plan.lines) == 1


def test_drop_ops_keeps_the_deload_line(snap):
    plan = ce.compile_ops(snap, [
        swap(DayRef(weekday=FRI), DayRef(weekday=WED)), DeloadA(type="deload", week="next_week"),
    ])
    assert len(plan.lines) == 3
    plan.drop_ops("Не получилось")
    assert plan.ops == [] and plan.notes[0] == "Не получилось"
    assert len(plan.lines) == 1 and plan.lines[0].startswith(ce.DELOAD_LINE)
    assert plan.deload is not None and plan.ready()
    assert plan.move_notes == []  # no warnings about a move that will not happen


def test_compile_swap_today_with_the_legs_day_by_composition(snap):
    """«сделай сегодня ноги вместо рук»: the template has no «Ноги» label, the squat day (Wednesday) is it."""
    plan = ce.compile_ops(snap, [swap(DayRef(when="today"), DayRef(focus="ноги"))])
    assert plan.clarify is None
    assert plan.ops == [{"op": "move_day", "week": 1, "weekday": FRI, "weeks": [1], "toWeekday": WED}]


def test_parse_actions_swap_move_and_deload():
    valid, notes = ce.parse_actions({"actions": [
        {"type": "swap_days", "a": {"weekday": 5}, "b": {"focus": "база"}, "scope": "all_weeks"},
        {"type": "move_day", "src": {"when": "today"}, "dst": {"when": "tomorrow"}},
        {"type": "deload", "week": "next_week", "start": None},
        {"type": "deload", "week": "someday"},
        {"type": "swap_days", "a": None, "b": None},
    ]})
    assert notes == []
    assert [type(a) for a in valid] == [SwapDaysA, MoveDayA, DeloadA, DeloadA, SwapDaysA]
    assert valid[0].a.weekday == 5 and valid[0].scope == "all_weeks"
    assert valid[2].week == "next_week" and valid[3].week is None
    assert valid[4].a == DayRef() and valid[4].b == DayRef()


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


async def test_apply_swap_forks_and_swaps_in_the_copy(db):
    s, session, user, up, plan = await staged(db, [swap(DayRef(when="today"), DayRef(focus="база"))])
    template_slug, template_version = plan.slug, plan.version
    try:
        assert await ce.apply(session, user, up, plan, TODAY, "сегодня базу вместо рук") == []
        await session.commit()
    finally:
        await s.__aexit__(None, None, None)
    owned, versions = await count_programs(db)
    assert owned == 1 and versions[template_slug] == template_version
    async with db() as session:
        user = await get_or_create_user(session, 42)
        copy = await ce.load_snapshot(session, user, TODAY)
        assert not copy.template
        assert copy.day(1, FRI).focus == "База" and copy.day(1, WED).focus == "Руки и плечи"
        assert copy.day(2, FRI).focus == "Руки и плечи" and copy.day(2, WED).focus == "База"  # this week only


async def test_preview_of_a_swap_writes_nothing(db):
    s, session, user, up, plan = await staged(db, [swap(DayRef(when="today"), DayRef(focus="база"))])
    try:
        await ce.preview(session, user, up, plan)
        assert plan.copy_name and plan.copy_name.endswith(pe.COPY_MARK)
    finally:
        await s.__aexit__(None, None, None)
    assert (await count_programs(db))[0] == 0


async def test_apply_deload_writes_the_state(db):
    s, session, user, up, plan = await staged(db, [DeloadA(type="deload", week="next_week")])
    try:
        assert await ce.apply(session, user, up, plan, TODAY, "следующая неделя — делоад") == []
        await session.commit()
    finally:
        await s.__aexit__(None, None, None)
    async with db() as session:
        st = await session.scalar(select(DeloadState))
    assert (st.started_on, st.until) == (date(2026, 10, 12), date(2026, 10, 18))
    assert (await count_programs(db))[0] == 0  # a deload alone never forks the program


async def test_apply_deload_after_midnight_starts_today_with_a_note(db):
    s, session, user, up, plan = await staged(db, [DeloadA(type="deload", week="this_week")])
    try:
        notes = await ce.apply(session, user, up, plan, date(2026, 10, 10), "x")
        await session.commit()
    finally:
        await s.__aexit__(None, None, None)
    assert notes == ["Разгрузка начнётся сегодня, до пт 16.10."]
    async with db() as session:
        st = await session.scalar(select(DeloadState))
    assert (st.started_on, st.until) == (date(2026, 10, 10), date(2026, 10, 16))


async def test_apply_swap_and_deload_in_one_plan(db):
    s, session, user, up, plan = await staged(db, [
        swap(DayRef(weekday=FRI), DayRef(weekday=WED)), DeloadA(type="deload", week="next_week"),
    ])
    assert plan.ops and plan.deload and plan.live_topics() == ["program", "plan", "state"]
    try:
        await ce.apply(session, user, up, plan, TODAY, "x", datetime(2026, 10, 9, 9, tzinfo=UTC))
        await session.commit()
    finally:
        await s.__aexit__(None, None, None)
    assert (await count_programs(db))[0] == 1
    async with db() as session:
        st = await session.scalar(select(DeloadState))
        user = await get_or_create_user(session, 42)
        copy = await ce.load_snapshot(session, user, TODAY)
    assert st.started_on == date(2026, 10, 12)
    assert copy.day(1, FRI).focus == "База"
    assert copy.deload == (date(2026, 10, 12), date(2026, 10, 18))


def test_edit_prompt_lists_every_action_type_the_parser_accepts():
    """The edit call has no JSON schema (too large for Claude): the prompt alone names the actions, so it and
    chat_edit.Action must list the same types."""
    import re
    import typing

    from gymbot.llm.prompts_edit import EDIT_SYSTEM_PROMPT

    actions = EDIT_SYSTEM_PROMPT.split("Действия", 1)[1].split("DAY =", 1)[0]
    in_prompt = set(re.findall(r'^- \{"type": "(\w+)"', actions, re.MULTILINE))
    union = typing.get_args(typing.get_args(ce.Action)[0])
    in_parser = {typing.get_args(m.model_fields["type"].annotation)[0] for m in union}
    assert in_prompt == in_parser
    assert "replace_day" in in_prompt and "new_program" in in_prompt and len(in_prompt) == 15


# ---- review fixes: moves run last, one per command, chat checks, day weights, a changed deload ----


async def _apply(db, actions, today=TODAY):
    s, session, user, up, plan = await staged(db, actions)
    try:
        notes = await ce.apply(session, user, up, plan, today, "x")
        await session.commit()
    finally:
        await s.__aexit__(None, None, None)
    async with db() as session:
        user = await get_or_create_user(session, 42)
        copy = await ce.load_snapshot(session, user, TODAY)
    return plan, notes, copy


def _names(day):
    return [i.name for i in day.items]


async def test_swap_then_add_lands_in_the_day_the_preview_names(db):
    """The add is resolved against Friday's «Руки и плечи» before the swap: it must end up there (now on
    Wednesday), not in «База» that came to Friday."""
    plan, _, copy = await _apply(db, [
        swap(DayRef(when="today"), DayRef(focus="база")), AddA(type="add", day=DayRef(weekday=FRI), name="молотки"),
    ])
    assert [op["op"] for op in plan.ops] == ["add", "move_day"]
    assert any(ln.startswith("пт «Руки и плечи»: добавить молотки") for ln in plan.lines)
    arms, base = copy.day(1, WED), copy.day(1, FRI)
    assert arms.focus == "Руки и плечи" and "молотки" in _names(arms)
    assert base.focus == "База" and "молотки" not in _names(base)


async def test_swap_then_remove_removes_from_the_day_the_preview_names(db):
    plan, _, copy = await _apply(db, [
        swap(DayRef(weekday=FRI), DayRef(weekday=WED)), RemoveA(type="remove", day=DayRef(weekday=FRI), exercise=FRENCH),
    ])
    assert [op["op"] for op in plan.ops] == ["remove", "move_day"]
    assert copy.day(1, WED).focus == "Руки и плечи" and FRENCH not in _names(copy.day(1, WED))
    assert len(copy.day(1, FRI).items) == 4  # «База» untouched


def test_second_move_in_one_command_is_a_note(snap):
    plan = ce.compile_ops(snap, [
        swap(DayRef(weekday=FRI), DayRef(weekday=WED)), move(DayRef(weekday=1), DayRef(weekday=2)),
    ])
    assert [op["op"] for op in plan.ops] == ["move_day"] and plan.ops[0]["weekday"] == FRI
    assert any("одну пару дней" in n for n in plan.notes)


async def test_swap_warns_about_a_chat_workout_on_the_date(db, snap):
    async with db() as s:
        user = await get_or_create_user(s, 42)
        s.add(Workout(user_id=user.id, performed_on=date(2026, 10, 7), source="chat"))
        await s.commit()
    async with db() as s:
        fresh = await ce.load_snapshot(s, await get_or_create_user(s, 42), TODAY)
    assert fresh.done_dates == {date(2026, 10, 7)} and fresh.done == set()
    plan = ce.compile_ops(fresh, [swap(DayRef(when="today"), DayRef(focus="база"))])
    assert any(n.startswith("Ср 07.10 уже отмечен сделанным по записи из чата") for n in plan.move_notes)
    assert not any("из чата" in n for n in ce.compile_ops(snap, [swap(DayRef(when="today"), DayRef(focus="база"))]).move_notes)


async def _override(db, name: str, day: date, kg: float) -> None:
    async with db() as s:
        user = await get_or_create_user(s, 42)
        ex = await s.scalar(select(Exercise).where(Exercise.name == name))
        s.add(WeightOverride(user_id=user.id, exercise_id=ex.id, day=day, weight_kg=kg))
        await s.commit()


async def _overrides(db) -> dict[tuple[str, date], float]:
    async with db() as s:
        rows = (await s.execute(
            select(Exercise.name, WeightOverride.day, WeightOverride.weight_kg)
            .join(Exercise, Exercise.id == WeightOverride.exercise_id)
        )).all()
    return {(n, d): float(kg) for n, d, kg in rows}


async def test_day_weights_follow_a_moved_day(db):
    await _override(db, FRENCH, date(2026, 10, 9), 30)
    await _apply(db, [move(DayRef(when="today"), DayRef(when="tomorrow"))])
    assert await _overrides(db) == {(FRENCH, date(2026, 10, 10)): 30.0}


async def test_day_weights_swap_both_ways_and_a_weight_on_the_target_stays(db):
    fri, wed = date(2026, 10, 9), date(2026, 10, 7)
    await _override(db, FRENCH, fri, 30)  # Friday's arms day -> Wednesday
    await _override(db, "жим лёжа", wed, 80)  # Wednesday's base day -> Friday, but Friday has its own
    await _override(db, "жим лёжа", fri, 85)
    await _apply(db, [swap(DayRef(weekday=FRI), DayRef(weekday=WED))])
    assert await _overrides(db) == {(FRENCH, wed): 30.0, ("жим лёжа", fri): 85.0}


async def test_deload_changed_after_the_preview_is_not_overwritten(db):
    s, _session, _user, _up, plan = await staged(db, [DeloadA(type="deload", week="next_week")])
    await s.__aexit__(None, None, None)
    async with db() as other:  # /deload started one in between
        u = await get_or_create_user(other, 42)
        await deload.start(other, u.id, TODAY, datetime(2026, 10, 9, 9, tzinfo=UTC))
        await other.commit()
    async with db() as session:
        user = await get_or_create_user(session, 42)
        up = await active_program(session, user, TODAY)
        notes = await ce.apply(session, user, up, plan, TODAY, "x")
        await session.commit()
        st = await session.scalar(select(DeloadState))
    assert len(notes) == 1 and "изменилась" in notes[0]
    assert (st.started_on, st.until) == (TODAY, date(2026, 10, 15))
    assert plan.deload is None and not any(ln.startswith(ce.DELOAD_LINE) for ln in plan.lines)


# ---- stage 3: a lighter day (gymbot.services.day_adjustments) ----

MON, SAT = 1, 6
MON_DATE = date(2026, 10, 12)  # week 2, an arms day without legs
WED_DATE = date(2026, 10, 14)  # the base day: squat and romanian deadlift
AT = datetime(2026, 10, 9, 9, tzinfo=UTC)


@pytest.mark.parametrize(
    "text",
    ["верни всё как было сегодня", "верни как было, без поправки", "отмени облегчение на завтра", "убери поправку",
     "сними поправку на пятницу"],
)
def test_gate_undo_of_an_adjustment(text):
    assert ce.is_edit_command(text)


@pytest.mark.parametrize(
    "text",
    ["верни как было", "верни как было в настройках", "верни как было напоминание", "верни как было вчера"],
)
def test_gate_bare_undo_needs_a_day_and_is_not_settings_or_records(text):
    assert not ce.is_edit_command(text)


@pytest.mark.parametrize(
    "text",
    [
        # how the user feels: a bare "легче" is never a command
        "легче стало после массажа", "дышится легче", "пульс сегодня легче", "легче", "полегче",
        # workouts already done
        "сегодня облегчённо потренил", "в пятницу без ног потренировался", "сегодня тренировался без бицепса",
        # a measurement, not a weight percent; "без" a non-exercise
        "сегодня 20% жира", "сегодня без зала", "завтра без тренировки", "завтра без сахара", "в пятницу без кофе",
        # a question without "?"
        "сегодня можно полегче", "стоит ли сегодня полегче",
    ],
)
def test_gate_lighter_day_negatives_from_review(text):
    assert not ce.is_edit_command(text)


@pytest.mark.parametrize("text", ["давай полегче", "без жима в смите", "сделай тренировку полегче", "веса на 10 % меньше"])
def test_gate_lighter_day_with_an_imperative_or_an_exercise(text):
    assert ce.is_edit_command(text)


def test_gate_undo_next_to_food_or_a_question_is_not_a_command():
    assert not ce.is_edit_command("верни как было сегодня?")
    assert not ce.is_edit_command("верни как было, съел 2 самсы")


@pytest.mark.parametrize(
    "text",
    ["сегодня легче, чем вчера было, зря?", "что легче: жим или тяга"],
)
def test_gate_lighter_is_not_taken_from_food_questions_or_chatter(text):
    assert not ce.is_edit_command(text)


def test_parse_actions_adjust_day_and_clear_day():
    valid, notes = ce.parse_actions({"actions": [
        {"type": "adjust_day", "day": {"when": "today"}, "weight_factor": 0.8, "sets_delta": -1, "skip": ["ноги"],
         "note": "спина"},
        {"type": "adjust_day", "day": None, "weight_factor": None, "sets_delta": None, "skip": None, "note": None},
        {"type": "clear_day", "day": {"weekday": 5}},
        {"type": "clear_day"},
    ]})
    assert notes == [] and [type(a) for a in valid] == [AdjustDayA, AdjustDayA, ClearDayA, ClearDayA]
    assert (valid[0].day.when, valid[0].weight_factor, valid[0].sets_delta, valid[0].skip, valid[0].note) == (
        "today", 0.8, -1, ["ноги"], "спина")
    assert valid[1].day == DayRef() and valid[1].weight_factor is None and valid[1].skip is None
    assert valid[2].day.weekday == 5 and valid[3].day == DayRef()


def test_parse_actions_adjust_day_neutral_values_become_none():
    (a,), notes = ce.parse_actions({"actions": [
        {"type": "adjust_day", "weight_factor": 1.0, "sets_delta": 0, "skip": "  ", "note": None}]})
    assert notes == [] and a.weight_factor is None and a.sets_delta is None and a.skip is None


def test_parse_actions_adjust_day_skip_as_a_string_and_a_rounded_factor():
    (a,), _ = ce.parse_actions({"actions": [{"type": "adjust_day", "weight_factor": 0.8049, "skip": " без ног "}]})
    assert a.weight_factor == 0.8 and a.skip == ["без ног"]


@pytest.mark.parametrize("factor", [0.3, 0.99])
def test_parse_actions_adjust_day_factor_bounds_are_valid(factor):
    valid, notes = ce.parse_actions({"actions": [{"type": "adjust_day", "weight_factor": factor}]})
    assert notes == [] and valid[0].weight_factor == factor


@pytest.mark.parametrize("factor", [1.2, 0.29, 0.0, -0.5])
def test_parse_actions_adjust_day_only_lighter_weights(factor):
    valid, notes = ce.parse_actions({"actions": [{"type": "adjust_day", "weight_factor": factor}]})
    assert valid == [] and len(notes) == 1 and notes[0].startswith("Не применю — поправка дня")


@pytest.mark.parametrize("delta", [1, 2, -6, -10])
def test_parse_actions_adjust_day_only_fewer_sets(delta):
    valid, notes = ce.parse_actions({"actions": [{"type": "adjust_day", "sets_delta": delta}]})
    assert valid == [] and len(notes) == 1 and notes[0].startswith("Не применю — поправка дня")


@pytest.mark.parametrize("delta", [-1, -5])
def test_parse_actions_adjust_day_sets_bounds_are_valid(delta):
    valid, notes = ce.parse_actions({"actions": [{"type": "adjust_day", "sets_delta": delta}]})
    assert notes == [] and valid[0].sets_delta == delta


def test_parse_actions_invalid_adjust_keeps_the_valid_ones():
    valid, notes = ce.parse_actions({"actions": [
        {"type": "adjust_day", "weight_factor": 2.0},
        {"type": "adjust_day", "sets_delta": 3},
        {"type": "clear_day", "day": {"weekday": 5}},
    ]})
    assert [type(a) for a in valid] == [ClearDayA] and len(notes) == 1  # one note for the kind


def adjust(**kw):
    return AdjustDayA(type="adjust_day", **kw)


def clear_day(**kw):
    return ClearDayA(type="clear_day", **kw)


def with_adjustments(snap, mapping):
    from dataclasses import replace

    return replace(snap, adjustments=mapping)


def test_compile_adjust_today_by_percent(snap):
    plan = ce.compile_ops(snap, [adjust(day=DayRef(when="today"), weight_factor=0.8)])
    (change,) = plan.adjusts
    assert (change.day, change.adj, change.before) == (TODAY, DayAdjust(0.8), None)
    assert plan.lines == ["Сегодня (пт 09.10): веса −20 %"] == [change.line]
    assert plan.ready() and plan.ops == [] and plan.weights == [] and plan.live_topics() == ["plan"]
    assert plan.notes == [] and plan.clarify is None


def test_compile_adjust_without_a_day_is_today(snap):
    (change,) = ce.compile_ops(snap, [adjust(weight_factor=0.9, sets_delta=-1)]).adjusts
    assert change.day == TODAY and change.adj == DayAdjust(0.9, -1)


def test_compile_adjust_sets_only(snap):
    plan = ce.compile_ops(snap, [adjust(day=DayRef(when="today"), sets_delta=-2)])
    assert plan.adjusts[0].adj == DayAdjust(None, -2) and plan.lines == ["Сегодня (пт 09.10): на 2 подхода меньше"]


def test_compile_adjust_weekday_is_the_next_such_day(snap):
    (monday,) = ce.compile_ops(snap, [adjust(day=DayRef(weekday=MON), weight_factor=0.8)]).adjusts
    assert monday.day == MON_DATE and monday.line.startswith("В понедельник (пн 12.10)")
    (friday,) = ce.compile_ops(snap, [adjust(day=day(), weight_factor=0.8)]).adjusts
    assert friday.day == TODAY and friday.line.startswith("Сегодня (пт 09.10)")  # today counts
    (wednesday,) = ce.compile_ops(snap, [adjust(day=DayRef(weekday=WED), weight_factor=0.8)]).adjusts
    assert wednesday.day == WED_DATE


def test_compile_adjust_by_focus_takes_the_first_such_day_of_the_coming_week(snap):
    (base,) = ce.compile_ops(snap, [adjust(day=DayRef(focus="база"), weight_factor=0.8)]).adjusts
    assert base.day == WED_DATE
    (arms,) = ce.compile_ops(snap, [adjust(day=DayRef(focus="руки"), weight_factor=0.8)]).adjusts
    assert arms.day == TODAY  # Friday is an arms day, today included


def test_compile_adjust_unknown_focus_is_a_note(snap):
    plan = ce.compile_ops(snap, [adjust(day=DayRef(focus="кардио"), weight_factor=0.8)])
    assert plan.adjusts == [] and not plan.ready() and plan.notes == ["Не нашёл на ближайшей неделе день «кардио»."]


def test_compile_adjust_tomorrow_rest_day_is_a_note(snap):
    plan = ce.compile_ops(snap, [adjust(day=DayRef(when="tomorrow"), weight_factor=0.8)])  # Saturday
    assert plan.adjusts == [] and not plan.ready() and plan.lines == []
    assert plan.notes == ["Завтра (сб 10.10) по программе тренировки нет — облегчать нечего."]


def test_compile_adjust_rest_weekday_is_a_note(snap):
    plan = ce.compile_ops(snap, [adjust(day=DayRef(weekday=2), sets_delta=-1)])  # Tuesday
    assert plan.adjusts == [] and "по программе тренировки нет" in plan.notes[0] and "вт 13.10" in plan.notes[0]


def test_compile_adjust_empty_action_is_the_plans_light_day(snap):
    plan = ce.compile_ops(snap, [adjust(day=DayRef(when="today"))])
    (change,) = plan.adjusts
    assert change.adj == DayAdjust(plan_light_factor(), -1)
    assert "(по умолчанию" in change.line and "веса −10 %" in change.line and "на подход меньше" in change.line


def plan_light_factor() -> float:
    from gymbot.services.plan import LIGHT_FACTOR

    assert LIGHT_FACTOR == 0.9
    return LIGHT_FACTOR


def test_compile_adjust_with_explicit_values_has_no_default_mark(snap):
    (change,) = ce.compile_ops(snap, [adjust(weight_factor=0.8)]).adjusts
    assert "(по умолчанию" not in change.line


def test_compile_adjust_keeps_the_note(snap):
    (change,) = ce.compile_ops(snap, [adjust(weight_factor=0.8, note="спина")]).adjusts
    assert change.adj.note == "спина"


def test_compile_adjust_skip_by_exercise_name(snap):
    plan = ce.compile_ops(snap, [adjust(day=day(), skip=["французский жим"])])
    (change,) = plan.adjusts
    assert change.adj == DayAdjust(skip_exercises=[FRENCH]) and "пропуск: " + FRENCH in change.line
    assert "(по умолчанию" not in change.line and plan.notes == []


def test_compile_adjust_skip_by_muscle_group(snap):
    plan = ce.compile_ops(snap, [adjust(day=DayRef(weekday=WED), skip=["ноги"])])
    (change,) = plan.adjusts
    assert change.day == WED_DATE and change.adj == DayAdjust(skip_groups=["legs"])
    assert change.line == "В среду (ср 14.10): без ног"


def test_compile_adjust_skip_group_and_exercise_together(snap):
    (change,) = ce.compile_ops(snap, [adjust(day=day(), skip=["бицепс", "французский жим"], weight_factor=0.9)]).adjusts
    assert change.adj == DayAdjust(0.9, None, [FRENCH], ["biceps"])
    assert change.line.endswith("веса −10 %, без бицепса, пропуск: " + FRENCH)


def test_compile_adjust_skip_group_the_day_does_not_have_is_a_note(snap):
    plan = ce.compile_ops(snap, [adjust(day=DayRef(weekday=MON), skip=["ноги"])])
    assert plan.adjusts == [] and not plan.ready()
    assert plan.notes == ["В понедельник нет упражнений на ноги."]


def test_compile_adjust_skip_unknown_exercise_is_a_note_with_the_days_list(snap):
    plan = ce.compile_ops(snap, [adjust(day=day(), skip=["становая тяга"])])
    assert plan.adjusts == [] and len(plan.notes) == 1
    assert "нет «становая тяга»" in plan.notes[0] and FRENCH in plan.notes[0]


def test_compile_adjust_one_unknown_skip_keeps_the_rest(snap):
    plan = ce.compile_ops(snap, [adjust(day=day(), weight_factor=0.8, skip=["становая тяга"])])
    assert plan.adjusts[0].adj == DayAdjust(0.8) and len(plan.notes) == 1


def test_compile_adjust_skipping_everything_warns(snap):
    names = [i.name for i in snap.day(1, 5).items]
    plan = ce.compile_ops(snap, [adjust(day=day(), skip=names)])
    assert plan.adjusts and any("не останется ни одного упражнения" in n for n in plan.notes)


def test_compile_adjust_merges_with_the_stored_one(snap):
    stored = DayAdjust(0.9, -1, ["сгибания на бицепс с ez грифом хватом снизу"], [], "старая")
    s = with_adjustments(snap, {TODAY: stored})
    plan = ce.compile_ops(s, [adjust(weight_factor=0.8, skip=["французский жим"])])
    (change,) = plan.adjusts
    assert change.before == stored
    assert change.adj == DayAdjust(0.8, -1, sorted([FRENCH, "сгибания на бицепс с ez грифом хватом снизу"]), [], "старая")
    assert "(было: веса −10 %, на подход меньше, пропуск: сгибания на бицепс с ez грифом хватом снизу)" in change.line
    assert change.line.startswith("Сегодня (пт 09.10): веса −20 %, на подход меньше, пропуск: ")


def test_compile_adjust_same_as_stored_is_a_note(snap):
    s = with_adjustments(snap, {TODAY: DayAdjust(0.8)})
    plan = ce.compile_ops(s, [adjust(weight_factor=0.8)])
    assert plan.adjusts == [] and not plan.ready() and plan.notes == ["На пт 09.10 уже: веса −20 %."]


def test_compile_adjust_empty_action_over_a_stored_one_is_a_note(snap):
    s = with_adjustments(snap, {TODAY: DayAdjust(0.7, -2)})
    plan = ce.compile_ops(s, [adjust()])
    assert plan.adjusts == [] and plan.notes == ["На пт 09.10 уже: веса −30 %, на 2 подхода меньше."]


def test_compile_adjust_empty_action_over_a_skip_only_row_sets_the_default(snap):
    s = with_adjustments(snap, {TODAY: DayAdjust(skip_groups=["biceps"])})
    (change,) = ce.compile_ops(s, [adjust()]).adjusts
    assert change.adj == DayAdjust(0.9, -1, [], ["biceps"]) and "(по умолчанию" in change.line


def test_compile_adjust_other_days_row_is_not_merged(snap):
    s = with_adjustments(snap, {MON_DATE: DayAdjust(0.5)})
    (change,) = ce.compile_ops(s, [adjust(weight_factor=0.8)]).adjusts
    assert change.before is None and change.adj == DayAdjust(0.8) and "было" not in change.line


def test_compile_adjust_twice_for_one_day_in_one_command_is_one_change(snap):
    plan = ce.compile_ops(snap, [adjust(weight_factor=0.8), adjust(sets_delta=-1, skip=["французский жим"])])
    (change,) = plan.adjusts
    assert change.adj == DayAdjust(0.8, -1, [FRENCH]) and plan.lines == [change.line] and change.before is None


def test_compile_adjust_two_days_in_one_command(snap):
    plan = ce.compile_ops(snap, [adjust(weight_factor=0.8), adjust(day=DayRef(weekday=MON), sets_delta=-1)])
    assert [c.day for c in plan.adjusts] == [TODAY, MON_DATE] and len(plan.lines) == 2 and plan.live_topics() == ["plan"]


def test_compile_clear_day_removes_the_stored_adjustment(snap):
    stored = DayAdjust(0.8, -1)
    plan = ce.compile_ops(with_adjustments(snap, {TODAY: stored}), [clear_day(day=DayRef(when="today"))])
    (change,) = plan.adjusts
    assert (change.day, change.adj, change.before) == (TODAY, None, stored)
    assert plan.lines == ["Сегодня (пт 09.10): убрать поправку (веса −20 %, на подход меньше), план как по программе"]
    assert plan.ready() and plan.live_topics() == ["plan"]


def test_compile_clear_day_by_weekday(snap):
    s = with_adjustments(snap, {MON_DATE: DayAdjust(0.8)})
    (change,) = ce.compile_ops(s, [clear_day(day=DayRef(weekday=MON))]).adjusts
    assert change.day == MON_DATE and change.adj is None


def test_compile_clear_day_without_a_row_is_a_note(snap):
    plan = ce.compile_ops(snap, [clear_day()])
    assert plan.adjusts == [] and not plan.ready() and plan.notes == ["На сегодня (пт 09.10) поправок нет, план и так по программе."]


def test_compile_clear_after_adjust_in_one_command_cancels_it(snap):
    plan = ce.compile_ops(snap, [adjust(weight_factor=0.8), clear_day()])
    assert plan.adjusts == [] and plan.lines == [] and plan.notes == [] and not plan.ready()


def test_compile_adjust_after_clear_in_one_command_replaces_the_row(snap):
    s = with_adjustments(snap, {TODAY: DayAdjust(0.7)})
    plan = ce.compile_ops(s, [clear_day(), adjust(sets_delta=-1)])
    (change,) = plan.adjusts
    assert change.before == DayAdjust(0.7) and change.adj is not None


def test_adjust_next_to_program_ops_keeps_every_topic(snap):
    plan = ce.compile_ops(snap, [RemoveA(type="remove", day=day(), exercise="французский жим"), adjust(weight_factor=0.8)])
    assert plan.ops and plan.adjusts and plan.live_topics() == ["program", "plan", "state"]
    assert len(plan.lines) == 2


def test_adjust_next_to_a_weight_adds_plan_to_state(snap):
    a = WeightA(type="weight", said="сгибания с гантелями на бицепс с супинацией", weight_kg=30)
    plan = ce.compile_ops(snap, [a, adjust(weight_factor=0.8)])
    assert plan.live_topics() == ["state", "plan"]


def test_drop_ops_keeps_the_adjust_lines(snap):
    plan = ce.compile_ops(snap, [swap(DayRef(weekday=FRI), DayRef(weekday=WED)), adjust(weight_factor=0.8)])
    assert len(plan.lines) == 3
    plan.drop_ops("Не получилось")
    assert plan.ops == [] and plan.notes[0] == "Не получилось"
    assert plan.lines == [plan.adjusts[0].line] and plan.ready()


def test_prompt_context_lists_the_adjustments(snap):
    s = with_adjustments(snap, {TODAY: DayAdjust(0.8, -1), WED_DATE: DayAdjust(skip_groups=["legs"])})
    lines = ce.prompt_context(s).split("\n")
    (line,) = [ln for ln in lines if ln.startswith("Поправки дня: ")]
    assert line == "Поправки дня: сегодня (пт 09.10): веса −20 %, на подход меньше; в среду (ср 14.10): без ног."
    assert lines.index(line) < len(lines) - 1 and lines[-1].startswith("Каталог упражнений")


def test_prompt_context_without_adjustments_has_no_such_line(snap):
    assert "Поправки дня" not in ce.prompt_context(snap)


async def test_snapshot_loads_the_adjustments_of_the_coming_days(db):
    from gymbot.services.advice import NEXT_DAY_SEARCH

    async with db() as s:
        user = await get_or_create_user(s, 42)
        for d in (TODAY - timedelta(days=1), TODAY, TODAY + timedelta(days=NEXT_DAY_SEARCH),
                  TODAY + timedelta(days=NEXT_DAY_SEARCH + 1)):
            await dayadj.upsert(s, user.id, d, DayAdjust(0.8), "x", now=AT)
        await s.commit()
        snapshot = await ce.load_snapshot(s, user, TODAY)
    assert set(snapshot.adjustments) == {TODAY, TODAY + timedelta(days=NEXT_DAY_SEARCH)}
    assert snapshot.adjustments[TODAY] == DayAdjust(0.8)


async def test_a_stored_adjustment_is_merged_by_the_next_command(db):
    async with db() as s:
        user = await get_or_create_user(s, 42)
        await dayadj.upsert(s, user.id, TODAY, DayAdjust(0.9, -1), "x", now=AT)
        await s.commit()
    s, _session, _user, _up, plan = await staged(db, [adjust(weight_factor=0.8)])
    await s.__aexit__(None, None, None)
    assert plan.adjusts[0].before == DayAdjust(0.9, -1) and plan.adjusts[0].adj == DayAdjust(0.8, -1)
    assert "(было: веса −10 %, на подход меньше)" in plan.lines[0]


async def _adjustment_rows(db) -> list:
    from gymbot.db.models import DayAdjustment

    async with db() as session:
        return list((await session.scalars(select(DayAdjustment).order_by(DayAdjustment.day))).all())


async def test_apply_stores_the_adjustment(db):
    s, session, user, up, plan = await staged(db, [adjust(weight_factor=0.8, sets_delta=-1, skip=["французский жим"])])
    try:
        assert await ce.apply(session, user, up, plan, TODAY, "сегодня облегчённо, −20 %", AT) == []
        await session.commit()
    finally:
        await s.__aexit__(None, None, None)
    (row,) = await _adjustment_rows(db)
    assert (row.day, float(row.weight_factor), row.sets_delta) == (TODAY, 0.8, -1)
    assert row.skip_json == {"exercises": [FRENCH], "groups": []}
    assert (row.raw_text, row.source, row.created_at.replace(tzinfo=UTC)) == ("сегодня облегчённо, −20 %", "chat", AT)
    assert (await count_programs(db))[0] == 0  # an adjustment alone never forks the program
    assert plan.lines and plan.adjusts


async def test_apply_replaces_the_stored_row_with_the_merge(db):
    async with db() as s:
        user = await get_or_create_user(s, 42)
        await dayadj.upsert(s, user.id, TODAY, DayAdjust(0.9, -1, [FRENCH]), "старое", now=AT)
        await s.commit()
    s, session, user, up, plan = await staged(db, [adjust(weight_factor=0.8)])
    try:
        assert await ce.apply(session, user, up, plan, TODAY, "ещё легче", AT) == []
        await session.commit()
    finally:
        await s.__aexit__(None, None, None)
    (row,) = await _adjustment_rows(db)
    assert (float(row.weight_factor), row.sets_delta, row.raw_text) == (0.8, -1, "ещё легче")
    assert row.skip_json["exercises"] == [FRENCH]


async def test_apply_clears_the_adjustment(db):
    async with db() as s:
        user = await get_or_create_user(s, 42)
        await dayadj.upsert(s, user.id, TODAY, DayAdjust(0.8), "x", now=AT)
        await dayadj.upsert(s, user.id, MON_DATE, DayAdjust(0.8), "x", now=AT)
        await s.commit()
    s, session, user, up, plan = await staged(db, [clear_day(day=DayRef(when="today"))])
    try:
        assert await ce.apply(session, user, up, plan, TODAY, "верни как было сегодня", AT) == []
        await session.commit()
    finally:
        await s.__aexit__(None, None, None)
    assert [r.day for r in await _adjustment_rows(db)] == [MON_DATE]


async def test_apply_adjustment_changed_after_the_preview_is_not_overwritten(db):
    s, _session, _user, _up, plan = await staged(db, [adjust(weight_factor=0.8)])
    await s.__aexit__(None, None, None)
    async with db() as other:  # another command set one in between
        user = await get_or_create_user(other, 42)
        await dayadj.upsert(other, user.id, TODAY, DayAdjust(0.6), "другое", now=AT)
        await other.commit()
    async with db() as session:
        user = await get_or_create_user(session, 42)
        up = await active_program(session, user, TODAY)
        notes = await ce.apply(session, user, up, plan, TODAY, "x", AT)
        await session.commit()
    assert len(notes) == 1 and "изменилась после предпросмотра" in notes[0] and "пт 09.10" in notes[0]
    assert plan.adjusts == [] and plan.lines == []
    (row,) = await _adjustment_rows(db)
    assert float(row.weight_factor) == 0.6 and row.raw_text == "другое"


async def test_apply_clear_of_a_row_that_changed_is_skipped(db):
    async with db() as s:
        user = await get_or_create_user(s, 42)
        await dayadj.upsert(s, user.id, TODAY, DayAdjust(0.8), "x", now=AT)
        await s.commit()
    s, _session, _user, _up, plan = await staged(db, [clear_day()])
    await s.__aexit__(None, None, None)
    async with db() as other:
        user = await get_or_create_user(other, 42)
        await dayadj.upsert(other, user.id, TODAY, DayAdjust(0.7), "x", now=AT)
        await other.commit()
    async with db() as session:
        user = await get_or_create_user(session, 42)
        up = await active_program(session, user, TODAY)
        notes = await ce.apply(session, user, up, plan, TODAY, "x", AT)
        await session.commit()
    assert len(notes) == 1 and "изменилась" in notes[0]
    assert float((await _adjustment_rows(db))[0].weight_factor) == 0.7


async def test_apply_after_the_day_has_passed_skips_with_a_note(db):
    s, session, user, up, plan = await staged(db, [adjust(weight_factor=0.8)])
    try:
        notes = await ce.apply(session, user, up, plan, date(2026, 10, 10), "x", AT)
        await session.commit()
    finally:
        await s.__aexit__(None, None, None)
    assert len(notes) == 1 and "тот день уже прошёл" in notes[0] and "пт 09.10" in notes[0]
    assert plan.adjusts == [] and plan.lines == []
    assert await _adjustment_rows(db) == []


async def test_apply_keeps_a_future_day_when_today_moved_on(db):
    s, session, user, up, plan = await staged(db, [adjust(day=DayRef(weekday=MON), weight_factor=0.8)])
    try:
        assert await ce.apply(session, user, up, plan, date(2026, 10, 10), "x", AT) == []
        await session.commit()
    finally:
        await s.__aexit__(None, None, None)
    assert [r.day for r in await _adjustment_rows(db)] == [MON_DATE]


async def test_apply_adjustment_with_a_swap_in_one_plan(db):
    s, session, user, up, plan = await staged(db, [
        swap(DayRef(weekday=FRI), DayRef(weekday=WED)), adjust(day=DayRef(weekday=MON), weight_factor=0.8),
    ])
    assert plan.ops and plan.adjusts and plan.live_topics() == ["program", "plan", "state"]
    try:
        await ce.apply(session, user, up, plan, TODAY, "x", AT)
        await session.commit()
    finally:
        await s.__aexit__(None, None, None)
    assert (await count_programs(db))[0] == 1
    assert [r.day for r in await _adjustment_rows(db)] == [MON_DATE]


# ---- replace_day: the day's exercises become the list ----

REPLACE_DAY_JSON = {
    "type": "replace_day",
    "day": {"weekday": None, "focus": None, "when": "today"},
    "items": [
        {"name": "присед со штангой", "sets": 3, "repsMin": 8, "repsMax": None, "dropReps": None, "weight_kg": None},
        {"name": "жим лёжа", "sets": 3, "repsMin": 8, "repsMax": None, "dropReps": None, "weight_kg": 65},
        {"name": "тяга вертикального блока", "sets": 3, "repsMin": 10, "repsMax": None, "dropReps": None, "weight_kg": 60},
    ],
    "scope": None,
}


def test_parse_actions_replace_day():
    actions, notes = ce.parse_actions({"actions": [REPLACE_DAY_JSON]})
    assert notes == [] and len(actions) == 1 and isinstance(actions[0], ce.ReplaceDayA)
    assert [i.name for i in actions[0].items][1] == "жим лёжа" and actions[0].items[1].weight_kg == 65
    bad = {**REPLACE_DAY_JSON, "items": [{**REPLACE_DAY_JSON["items"][0], "sets": 0}]}
    actions, notes = ce.parse_actions({"actions": [bad]})
    assert actions == [] and notes and notes[0].startswith("Не применю — замена дня")


def test_replace_day_adds_first_then_removes_and_sets_the_weights(snap):
    (a,), _ = ce.parse_actions({"actions": [REPLACE_DAY_JSON]})
    old = [i.name for i in snap.day(1, 5).items]
    plan = ce.compile_ops(snap, [a])
    assert plan.ready() and plan.clarify is None and plan.notes == []
    kinds = [op["op"] for op in plan.ops]
    assert kinds == ["add"] * 3 + ["remove"] * len(old)  # the day is never empty while the ops run
    adds = [op for op in plan.ops if op["op"] == "add"]
    assert [op["name"] for op in adds] == ["присед со штангой", "жим лёжа", "тяга вертикального блока"]
    assert [op["position"] for op in adds] == [len(old) + 1, len(old) + 2, len(old) + 3]
    assert adds[0]["sets"] == 3 and adds[0]["repsMin"] == 8 and adds[0]["repsMax"] == 8
    assert all(op["week"] == 1 and op["weekday"] == 5 and op["weeks"] == [1] for op in plan.ops)
    removed = {op["itemId"] for op in plan.ops if op["op"] == "remove"}
    assert removed == {i.id for i in snap.day(1, 5).items}
    # the weights: a known exercise by id, today's date; the new one is looked up by name when applied
    assert [(w.name, w.day, w.kg) for w in plan.weights] == [("жим лёжа", TODAY, 65.0), ("тяга вертикального блока", TODAY, 60.0)]
    assert all(w.exercise_id is not None for w in plan.weights)
    assert any(ln.startswith("пт «Руки и плечи»: добавить присед со штангой 3×8 в конец") for ln in plan.lines)
    assert any(ln.startswith("пт «Руки и плечи»: убрать французский жим лёжа") for ln in plan.lines)
    assert "Вес на пт 09.10: жим лёжа 65 кг" in plan.lines


def test_replace_day_keeps_an_exercise_already_there_and_a_new_one_gets_its_weight(snap):
    a = ce.ReplaceDayA(type="replace_day", day=DayRef(when="today"), items=[
        ce.DayItemA(name="французский жим лёжа", sets=4, repsMin=10, repsMax=12),  # stays, re-prescribed
        ce.DayItemA(name="жим сидя в смите"),  # stays as is
        ce.DayItemA(name="прыжки на тумбу", sets=3, repsMin=8, weight_kg=60),  # new, with a weight
    ])
    assert "прыжки на тумбу" not in snap.known
    plan = ce.compile_ops(snap, [a])
    kinds = [op["op"] for op in plan.ops]
    assert kinds.count("add") == 1 and kinds.count("prescribe") == 1
    assert kinds.index("add") < kinds.index("remove")
    kept = {"французский жим лёжа", "жим сидя в смите"}
    removed = {op["itemId"] for op in plan.ops if op["op"] == "remove"}
    assert removed == {i.id for i in snap.day(1, 5).items if i.name not in kept}
    assert [(w.name, w.exercise_id) for w in plan.weights] == [("прыжки на тумбу", None)]  # by name when applied


def test_replace_day_same_list_changes_nothing(snap):
    items = [ce.DayItemA(name=i.name) for i in snap.day(1, 5).items]
    plan = ce.compile_ops(snap, [ce.ReplaceDayA(type="replace_day", day=DayRef(when="today"), items=items)])
    assert not plan.ready() and plan.notes == ["В дне пт «Руки и плечи» уже так."]


def test_replace_day_on_a_rest_day_makes_the_day(snap):
    """Incident 27.10: «замени сегодняшнюю тренировку на …» on a rest day answered «тренировки нет»."""
    a = ce.ReplaceDayA(type="replace_day", day=DayRef(when="tomorrow"), items=[
        ce.DayItemA(name="сгибания с гантелями на бицепс с супинацией", sets=4, repsMin=8),
        ce.DayItemA(name="жим гантелей сидя", sets=3, repsMin=12),
    ])
    plan = ce.compile_ops(snap, [a])
    assert plan.ready() and plan.notes == []
    assert [op["op"] for op in plan.ops] == ["add_day", "add", "add"]
    assert {(op["week"], op["weekday"]) for op in plan.ops} == {(1, 6)}  # tomorrow: Saturday of week 1
    assert plan.ops[0]["weeks"] == [1]
    assert plan.lines[0] == "Сб 10.10: тренировка вместо отдыха (только неделя 1)"
    assert plan.lines[1].startswith("сб: добавить сгибания с гантелями на бицепс с супинацией 4×8 (")


async def test_replace_day_applies_and_sets_the_new_exercise_weight(db):
    from gymbot.services import overrides

    (a,), _ = ce.parse_actions({"actions": [{**REPLACE_DAY_JSON, "items": [
        {**REPLACE_DAY_JSON["items"][0], "name": "прыжки на тумбу", "weight_kg": 60},  # a new exercise
        REPLACE_DAY_JSON["items"][1],
    ]}]})
    s, session, user, up, plan = await staged(db, [a])
    assert [w.exercise_id for w in plan.weights] == [None, plan.weights[1].exercise_id]
    try:
        await ce.preview(session, user, up, plan)  # the dry run: adds before removes, the day is never empty
        assert plan.ready()
    finally:
        await s.__aexit__(None, None, None)
    async with db() as session:  # "✅ Применить": a fresh session, as in the handler
        user = await get_or_create_user(session, 42)
        up = await active_program(session, user, TODAY)
        notes = await ce.apply(session, user, up, plan, TODAY, "замени сегодняшнюю тренировку на прыжки и жим")
        await session.commit()
    assert notes == []
    async with db() as session:
        user = await get_or_create_user(session, 42)
        copy = await ce.load_snapshot(session, user, TODAY)
        assert not copy.template
        assert [i.name for i in copy.day(1, 5).items] == ["прыжки на тумбу", "жим лёжа"]
        assert FRENCH in [i.name for i in copy.day(2, 5).items]  # this week only
        today = {w.exercise: w.weightKg for w in await overrides.for_day(session, user.id, TODAY)}
        assert today == {"прыжки на тумбу": 60.0, "жим лёжа": 65.0}


# ---- a workout on a rest day, a new schedule (incidents of 27–29.10) ----


@pytest.mark.parametrize("text", [
    "Я теперь хору вторник четверг суббота",
    "теперь хожу вт, чт, сб",
    "тренируюсь по понедельникам, средам и пятницам",
    "перенеси тренировки на вторник, четверг и субботу",
])
def test_a_new_schedule_routes_to_the_edit_model(text):
    assert ce.is_edit_command(text)


@pytest.mark.parametrize("text", [
    "какая тренировка во вторник и четверг",
    "во вторник и четверг болели ноги",
    "в понедельник и среду ел курицу",
    "удали записи за понедельник и вторник",
])
def test_weekdays_that_are_no_schedule_stay_away(text):
    assert not ce.is_edit_command(text)


def test_parse_actions_set_weekdays_sorted_and_checked():
    (a,), _ = ce.parse_actions({"actions": [{"type": "set_weekdays", "weekdays": [6, 2, 4], "scope": None}]})
    assert a.weekdays == [2, 4, 6]
    assert ce.parse_actions({"actions": [{"type": "set_weekdays", "weekdays": [2, 2, 9]}]}) == ([], [])


def test_set_weekdays_compiles_to_one_op_from_this_week(snap):
    plan = ce.compile_ops(snap, [ce.SetWeekdaysA(type="set_weekdays", weekdays=[2, 4, 6])])
    assert plan.ready()
    (op,) = plan.ops
    assert op == {"op": "set_weekdays", "week": 1, "weekday": 1, "weeks": list(range(1, 9)),
                  "fromWeekdays": [1, 3, 5], "toWeekdays": [2, 4, 6]}
    assert plan.lines[0] == "Расписание: пн/ср/пт → вт/чт/сб (все недели)"
    assert plan.lines[-1] == "сб 10.10: «Руки и плечи»"


def test_set_weekdays_with_a_wrong_number_of_days_explains(snap):
    plan = ce.compile_ops(snap, [ce.SetWeekdaysA(type="set_weekdays", weekdays=[2, 4])])
    assert not plan.ready()
    assert plan.notes[0].startswith("В программе 3 трен. в неделю (пн/ср/пт), а дней названо 2.")


async def test_set_weekdays_applies_and_the_day_weights_follow(db):
    from gymbot.services import overrides

    async with db() as session:
        user = await get_or_create_user(session, 42)
        snap = await ce.load_snapshot(session, user, TODAY)
        curl = item(snap, 5, "сгибания на бицепс с ez грифом хватом снизу")
        await overrides.upsert(session, user.id, curl.exercise_id, TODAY, 30)  # today's (Friday's) weight
        await session.commit()
    s, session, user, up, plan = await staged(db, [ce.SetWeekdaysA(type="set_weekdays", weekdays=[2, 4, 6])])
    try:
        await ce.preview(session, user, up, plan)
    finally:
        await s.__aexit__(None, None, None)
    async with db() as session:
        user = await get_or_create_user(session, 42)
        up = await active_program(session, user, TODAY)
        await ce.apply(session, user, up, plan, TODAY, "я теперь хожу вт, чт, сб")
        await session.commit()
    async with db() as session:
        user = await get_or_create_user(session, 42)
        after = await ce.load_snapshot(session, user, TODAY)
        for w in range(1, 9):
            assert sorted(after.weeks[w]) == [2, 4, 6]
        assert after.day(1, 6).focus == "Руки и плечи" and after.day(1, 4).focus == "База"
        moved = {w.exercise: w.weightKg for w in await overrides.for_day(session, user.id, TODAY + timedelta(days=1))}
        assert moved == {"сгибания на бицепс с ez грифом хватом снизу": 30.0}


async def test_replace_day_on_a_rest_day_applies(db):
    a = ce.ReplaceDayA(type="replace_day", day=DayRef(when="tomorrow"), items=[
        ce.DayItemA(name="сгибания с гантелями на бицепс с супинацией", sets=4, repsMin=8),
        ce.DayItemA(name="французский жим в блоке из-за головы", sets=4, repsMin=10),
    ])
    s, session, user, up, plan = await staged(db, [a])
    try:
        await ce.preview(session, user, up, plan)
        assert plan.ready() and plan.notes == []
    finally:
        await s.__aexit__(None, None, None)
    async with db() as session:
        user = await get_or_create_user(session, 42)
        up = await active_program(session, user, TODAY)
        await ce.apply(session, user, up, plan, TODAY, "замени завтрашнюю тренировку на сгибания и французский")
        await session.commit()
    async with db() as session:
        user = await get_or_create_user(session, 42)
        after = await ce.load_snapshot(session, user, TODAY)
        assert [i.name for i in after.day(1, 6).items] == [
            "сгибания с гантелями на бицепс с супинацией", "французский жим в блоке из-за головы",
        ]
        assert after.day(2, 6) is None  # this week only


def test_two_adds_on_one_rest_day_make_it_once(snap):
    plan = ce.compile_ops(snap, [
        AddA(type="add", day=DayRef(weekday=6), name="жим гантелей сидя"),
        AddA(type="add", day=DayRef(weekday=6), name="отведения на дельты"),
    ])
    assert [op["op"] for op in plan.ops] == ["add_day", "add", "add"]


def test_prompt_context_shows_rest_days(snap):
    text = ce.prompt_context(snap)
    assert " сб: отдых" in text and " вт: отдых" in text


async def test_set_weekdays_after_an_earlier_move_keeps_every_week_on_the_new_days(db):
    """Week 1 had Friday moved to Saturday: its own days go to вт/чт/сб, never a half-moved вт/ср/сб."""
    await _apply(db, [MoveDayA(type="move_day", src=DayRef(weekday=5), dst=DayRef(weekday=6))])
    s, session, user, up, plan = await staged(db, [ce.SetWeekdaysA(type="set_weekdays", weekdays=[2, 4, 6])])
    try:
        assert [op["fromWeekdays"] for op in plan.ops] == [[1, 3, 6], [1, 3, 5]]
        assert plan.lines[:2] == [
            "Расписание: пн/ср/сб → вт/чт/сб (только неделя 1)", "Расписание: пн/ср/пт → вт/чт/сб (недели 2–8)",
        ]
        await ce.apply(session, user, up, plan, TODAY, "я теперь хожу вт, чт, сб")
        await session.commit()
    finally:
        await s.__aexit__(None, None, None)
    async with db() as session:
        user = await get_or_create_user(session, 42)
        after = await ce.load_snapshot(session, user, TODAY)
    for w in range(1, 9):
        assert sorted(after.weeks[w]) == [2, 4, 6]
        assert after.day(w, 6).focus == "Руки и плечи"


# ---- a new training day until the end of the program (incident 30.10: «добавь день ног») ----


def leg_day(weekday, scope="from_this_week"):
    return ce.ReplaceDayA(type="replace_day", day=DayRef(weekday=weekday, focus="ноги"), scope=scope, items=[
        ce.DayItemA(name="присед со штангой", sets=3, repsMin=8, weight_kg=80),
        ce.DayItemA(name="румынская тяга", sets=3, repsMin=10),
    ])


def test_new_leg_day_on_saturdays_is_named_and_goes_to_the_end(snap):
    plan = ce.compile_ops(snap, [leg_day(6)])
    assert plan.ready() and plan.notes == []
    assert [op["op"] for op in plan.ops] == ["add_day", "add", "add"]
    assert plan.ops[0]["focus"] == "Ноги" and plan.ops[0]["week"] == 1
    assert plan.ops[0]["weeks"] == snap.all_weeks()
    assert plan.lines[0] == "Сб 10.10: тренировка «Ноги» вместо отдыха (все недели)"


def test_new_day_on_a_weekday_already_past_starts_next_week(snap):
    """Friday: «по вторникам до конца программы» starts on next Tuesday, not «уже прошло»."""
    plan = ce.compile_ops(snap, [leg_day(2)])
    assert plan.ready() and plan.notes == []
    assert {(op["week"], op["weekday"]) for op in plan.ops} == {(2, 2)}
    assert plan.ops[0]["weeks"] == [w for w in snap.all_weeks() if w >= 2]
    assert plan.lines[0].startswith("Вт 13.10: тренировка «Ноги» вместо отдыха (недели 2")


def test_new_day_on_a_past_weekday_this_week_only_is_a_note(snap):
    plan = ce.compile_ops(snap, [leg_day(2, scope="this_week")])
    assert not plan.ready() and plan.notes == ["Вт 06.10 уже прошло — тренировку туда не добавляю."]


async def test_new_leg_day_applies_to_every_week_with_the_weight_on_the_first(db):
    from gymbot.services import overrides

    s, session, user, up, plan = await staged(db, [leg_day(6)])
    try:
        await ce.preview(session, user, up, plan)
        assert plan.ready()
    finally:
        await s.__aexit__(None, None, None)
    async with db() as session:
        user = await get_or_create_user(session, 42)
        up = await active_program(session, user, TODAY)
        assert await ce.apply(session, user, up, plan, TODAY, "добавь тренировку ног по субботам") == []
        await session.commit()
    async with db() as session:
        user = await get_or_create_user(session, 42)
        after = await ce.load_snapshot(session, user, TODAY)
        for week in after.all_weeks():
            assert after.day(week, 6).focus == "Ноги"
            assert _names(after.day(week, 6)) == ["присед со штангой", "румынская тяга"]
        saturday = {w.exercise: w.weightKg for w in await overrides.for_day(session, user.id, TODAY + timedelta(days=1))}
        assert saturday == {"присед со штангой": 80.0}


def test_new_day_on_a_past_weekday_in_all_weeks_never_lands_on_this_week(snap):
    plan = ce.compile_ops(snap, [leg_day(2, scope="all_weeks")])
    assert plan.ready()
    assert plan.ops[0]["week"] == 2 and 1 not in plan.ops[0]["weeks"]


@pytest.mark.parametrize("text", [
    "измени в сегодняшней тренировке жим на 80",
    "измени сегодняшнюю тренировку, жим 80 а не 70",
    "измени последнюю тренировку: жим 3 подхода по 8",
    "измени тренировку в понедельник: жим 80",
    "измени жим в тренировке на 85 кг",
])
def test_fixing_a_saved_workout_is_not_a_program_edit(text):
    assert not ce.is_edit_command(text)


# ---- a new program from scratch (incident 30.10: «замени программу на 3 дня … на 8 недель с 02.11») ----


def _rx(name, sets, lo, hi=None):
    return {"name": name, "sets": sets, "repsMin": lo, "repsMax": hi, "dropReps": None, "weight_kg": None}


NEW_PROGRAM_JSON = {"type": "new_program", "name": None, "weeks": 8, "start": "2026-10-12", "days": [
    {"weekday": 1, "focus": None, "items": [_rx("жим лёжа", 4, 6, 8), _rx("тяга вертикального блока", 4, 8, 10)]},
    {"weekday": 3, "focus": "ноги", "items": [_rx("присед со штангой", 4, 8), _rx("румынская тяга", 3, 10)]},
    {"weekday": 5, "focus": None, "items": [_rx("сгибания на бицепс с ez грифом хватом снизу", 3, 10, 12)]},
]}


def new_program(**kw):
    (a,), notes = ce.parse_actions({"actions": [{**NEW_PROGRAM_JSON, **kw}]})
    assert notes == []
    return a


def test_new_program_previews_every_day_and_what_happens_to_the_current_one(snap):
    plan = ce.compile_ops(snap, [new_program()])
    assert plan.ready() and plan.ops == [] and plan.notes == []
    assert plan.lines == [
        "Новая программа «Программа пн/ср/пт, 8 нед.»: 8 нед. с пн 12.10, пн/ср/пт",
        "пн: жим лёжа 4×6–8, тяга вертикального блока 4×8–10",
        "ср «Ноги»: присед со штангой 4×8, румынская тяга 3×10",
        "пт: сгибания на бицепс с ez грифом хватом снизу 3×10–12",
        f"«{snap.name}» заканчивается: до пн 12.10 тренировок по программе нет, сделанные тренировки останутся в истории",
    ]
    assert plan.live_topics() == ["program", "plan", "state"]


def test_new_program_without_a_start_begins_next_monday_and_fills_defaults(snap):
    a = new_program(start=None, days=[{"weekday": 2, "focus": None, "items": [{"name": "жим лёжа"}]}])
    plan = ce.compile_ops(snap, [a])
    assert plan.new_program.start == date(2026, 10, 12)
    assert plan.lines[1] == "вт: жим лёжа 3×8–12 (по умолчанию)"


def test_new_program_alone_other_actions_are_dropped_with_a_note(snap):
    plan = ce.compile_ops(snap, [AddA(type="add", day=DayRef(weekday=5), name="молотки"), new_program()])
    assert plan.ready() and plan.ops == []
    assert plan.notes == ["Новую программу делаю отдельно от других правок: их повтори после неё."]


def test_new_program_in_the_past_or_with_a_weekday_twice_is_refused(snap):
    plan = ce.compile_ops(snap, [new_program(start="2026-09-28")])
    assert not plan.ready() and plan.notes == ["Пн 28.09 уже прошло — назови, с какого понедельника начать."]
    twice = {**NEW_PROGRAM_JSON, "days": [NEW_PROGRAM_JSON["days"][0], NEW_PROGRAM_JSON["days"][0]]}
    actions, notes = ce.parse_actions({"actions": [twice]})
    assert actions == [] and notes[0].startswith("Не применю — новая программа:")


async def test_new_program_applies_and_starts_on_its_monday(db):
    from gymbot.db.models import UserProgram

    async with db() as session:
        before = dict((await session.execute(select(Workout.id, Workout.program_day_id))).all())
    _, notes, copy = await _apply(db, [new_program()])
    assert notes == []
    async with db() as session:
        user = await get_or_create_user(session, 42)
        up = await active_program(session, user, TODAY)
        assert up.started_on == date(2026, 10, 12) and up.program.owner_user_id == user.id
        assert up.program.slug == "own.u1" and up.program.based_on_id is None
        program = await load_program(session, up.program_id)
        assert [w.number for w in program.weeks] == list(range(1, 9))
        assert all([d.weekday for d in w.days] == [1, 3, 5] for w in program.weeks)
        legs = program.weeks[7].days[1]
        assert legs.focus == "Ноги" and [(i.order, i.exercise.name, i.sets, i.reps_min, i.reps_max) for i in legs.items] == [
            (1, "присед со штангой", 4, 8, 8), (2, "румынская тяга", 3, 10, 10),
        ]
        assert dict((await session.execute(select(Workout.id, Workout.program_day_id))).all()) == before
        assert await session.scalar(select(func.count()).select_from(UserProgram)) >= 2  # the old choice stays
    assert copy.position(TODAY).not_started


TYPED_PROGRAM = (
    "замени программу на 3 дня (пн, ср, пт) на 8 недель с 02.11: пн — жим лёжа 4×6–8, тяга вертикального блока 4×8–10; "
    "ср — присед со штангой 4×8, румынская тяга 3×10; пт — жим гантелей сидя 3×8–10"
)


@pytest.mark.parametrize("text", [TYPED_PROGRAM, "смени программу на 3 дня: пн — жим лёжа 4×8; ср — присед 4×8"])
def test_a_program_spelled_out_reaches_the_edit_model(text):
    assert ce.is_edit_command(text)


@pytest.mark.parametrize("text", ["начни программу с понедельника", "смени программу на другую", "программа на пн и ср"])
def test_picking_or_starting_a_program_is_not_a_new_one(text):
    assert not ce._branch_new_program(normalize(text))


def test_a_new_program_is_never_taken_for_a_guess(snap):
    plan = ce.compile_ops(snap, [new_program()])
    assert not ce.only_guesses(plan, [new_program()], TYPED_PROGRAM)


@pytest.mark.parametrize("start, monday, note", [
    ("2026-10-14", date(2026, 10, 19), "Неделя программы начинается с понедельника: с пн 19.10."),  # a Wednesday
    ("2026-10-12", date(2026, 10, 12), None),
    (None, date(2026, 10, 12), None),  # Friday: next Monday
])
def test_new_program_starts_on_the_monday_on_or_after_the_date(snap, start, monday, note):
    plan = ce.compile_ops(snap, [new_program(start=start)])
    assert plan.new_program.start == monday
    assert plan.notes == ([note] if note else [])


def test_new_program_without_a_start_on_a_monday_begins_today(snap):
    from dataclasses import replace

    plan = ce.compile_ops(replace(snap, today=date(2026, 10, 12)), [new_program(start=None)])
    assert plan.new_program.start == date(2026, 10, 12)


def test_new_program_marks_defaults_and_new_exercises(snap):
    a = new_program(days=[{"weekday": 2, "focus": None, "items": [{"name": "жим лёжа"}, _rx("прыжки на тумбу", 3, 10)]}])
    plan = ce.compile_ops(snap, [a])
    assert plan.lines[1] == f"вт: жим лёжа 3×8–12 (по умолчанию), прыжки на тумбу 3×10 ({ce.NEW_EXERCISE})"


async def test_two_new_programs_get_their_own_slugs_and_names(db):
    await _apply(db, [new_program()])
    async with db() as session:
        user = await get_or_create_user(session, 42)
        up = await active_program(session, user, TODAY)
        first = (up.program.slug, up.program.name)
    await _apply(db, [new_program()])
    async with db() as session:
        user = await get_or_create_user(session, 42)
        up = await active_program(session, user, TODAY)
        assert first == ("own.u1", "Программа пн/ср/пт, 8 нед. · моя")
        assert (up.program.slug, up.program.name) == ("own.u1-2", "Программа пн/ср/пт, 8 нед. · моя 2")
        assert up.program.source is None


async def test_a_new_program_can_be_edited_from_the_chat_afterwards(db):
    await _apply(db, [new_program(start="2026-10-05")])  # this week's Monday: today is in week 1
    plan, notes, copy = await _apply(db, [AddA(type="add", day=DayRef(weekday=5), name="молотки", sets=3, repsMin=10)])
    assert notes == [] and not plan.forks
    assert _names(copy.day(1, 5))[-1] == "молотки" and copy.slug == "own.u1"


async def test_create_program_refuses_one_exercise_twice_in_a_day(db):
    async with db() as session:
        user = await get_or_create_user(session, 42)
        item = pe.NewItem("жим лёжа", 3, 8, 8, None)
        with pytest.raises(pe.EditError, match="Понедельник: одно упражнение дважды"):
            await pe.create_program(session, user, "x", 1, [pe.NewDay(1, None, [item, item])])
