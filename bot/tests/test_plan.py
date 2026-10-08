"""Adaptive day plan: rules, pain mapping, model answer validation, cache, /plan and the recompute after wellbeing."""

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import func, select

from gymbot.db.models import DayPlan, Program, User, UserFact, WellbeingEntry
from gymbot.handlers import plan as plan_handler
from gymbot.llm.openrouter import OpenRouterClient
from gymbot.services import plan
from gymbot.services.plan import DayItem, Pain, PlanInputs, RecentSets
from gymbot.services.programs import load_program

MSK = ZoneInfo("Europe/Moscow")
MON = date(2026, 10, 5)
NOW = datetime(2026, 10, 5, 10, 0, tzinfo=MSK).astimezone(UTC)

ARMS = [
    DayItem(1, "сгибания с гантелями на бицепс с супинацией", 6, 8, 12),
    DayItem(2, "сгибания с гантелями на бицепс с пронацией", 3, 8, 12),
    DayItem(3, "французский жим в блоке из-за головы", 6, 8, 12),
    DayItem(4, "жим гантелей сидя", 3, None, None, [12, 6, 6]),
    DayItem(5, "отведения на дельты", 3, 12, 15),
    DayItem(6, "отведения пек дек на заднюю дельту", 3, None, None, [12, 6, 6]),
]
LEGS = [
    DayItem(1, "жим лёжа", 4, 8, 12),
    DayItem(2, "тяга вертикального блока", 4, 8, 12),
    DayItem(3, "присед со штангой", 4, 8, 12),
    DayItem(4, "румынская тяга", 4, 8, 12),
]


def inputs(items=ARMS, **kw) -> PlanInputs:
    return PlanInputs(today=MON, items=list(items), **kw)


# ---- pain places -> exercises ----

ALL_NAMES = [
    "жим гантелей сидя", "жим лёжа", "жим лёжа 30°", "жим сидя в смите", "отведения гантелей на переднюю дельту",
    "отведения на дельты", "отведения пек дек на заднюю дельту", "присед в гаке лицом к спинке", "присед со штангой",
    "румынская тяга", "сгибания на бицепс с ez грифом хватом сверху", "сгибания на бицепс с ez грифом хватом снизу",
    "сгибания с гантелями на бицепс с пронацией", "сгибания с гантелями на бицепс с супинацией",
    "тяга вертикального блока", "тяга горизонтального блока", "французский жим в блоке из-за головы",
    "французский жим лёжа",
]
LOADED = {
    "левое плечо": {"жим гантелей сидя", "жим лёжа", "жим лёжа 30°", "жим сидя в смите",
                    "отведения гантелей на переднюю дельту", "отведения на дельты", "отведения пек дек на заднюю дельту"},
    "колено": {"присед в гаке лицом к спинке", "присед со штангой", "румынская тяга"},
    "поясница": {"присед в гаке лицом к спинке", "присед со штангой", "румынская тяга"},
    "правый локоть": {"сгибания на бицепс с ez грифом хватом сверху", "сгибания на бицепс с ez грифом хватом снизу",
                      "сгибания с гантелями на бицепс с пронацией", "сгибания с гантелями на бицепс с супинацией",
                      "французский жим в блоке из-за головы", "французский жим лёжа"},
}
LOADED["предплечье"] = LOADED["запястье"] = LOADED["правый локоть"]  # not the shoulder: "предПЛЕЧье"
LOADED["ноги"] = LOADED["колено"]


@pytest.mark.parametrize("place", list(LOADED))
def test_pain_place_loads_exercises(place):
    assert {n for n in ALL_NAMES if plan.loads(place, n)} == LOADED[place]


def test_unknown_place_loads_nothing():
    assert not any(plan.loads("голова", n) for n in ALL_NAMES)
    assert not plan.loads("плечо", "жим ногами")


# ---- readiness rules ----


@pytest.mark.parametrize(
    ("kw", "items", "readiness"),
    [
        ({}, ARMS, "normal"),
        ({"sleep_hours": 3.9}, ARMS, "rest"),
        ({"sleep_hours": 4}, ARMS, "light"),
        ({"sleep_hours": 5.9}, ARMS, "light"),
        ({"sleep_hours": 6}, ARMS, "normal"),
        ({"energy": 1}, ARMS, "rest"),
        ({"energy": 2}, ARMS, "light"),
        ({"energy": 3}, ARMS, "normal"),
        ({"kcal_yesterday": 1740, "kcal_target": 2500}, ARMS, "light"),  # 69.6 %
        ({"kcal_yesterday": 1750, "kcal_target": 2500}, ARMS, "normal"),
        ({"kcal_yesterday": None, "kcal_target": 2500}, ARMS, "normal"),  # nothing logged: unknown, not starving
        ({"kcal_yesterday": 500, "kcal_target": None}, ARMS, "normal"),
        ({"pains": [Pain("левое плечо", 4)]}, ARMS, "rest"),
        ({"pains": [Pain("левое плечо", 3)]}, ARMS, "light"),
        ({"pains": [Pain("левое плечо", None)]}, ARMS, "light"),  # the parser rarely sets severity
        ({"pains": [Pain("левое плечо", 1)]}, ARMS, "normal"),
        ({"pains": [Pain("колено", 5)]}, ARMS, "normal"),  # nothing loads the knee on the arms day
        ({"pains": [Pain("колено", 5)]}, LEGS, "rest"),
    ],
)
def test_rule_readiness(kw, items, readiness):
    assert plan.rule_readiness(inputs(items, **kw))[0] == readiness


def test_normal_day_is_not_adjusted():
    draft = plan.rule_draft(inputs(), NOW)
    assert draft.readiness == "normal" and not draft.adjusted and draft.summary is None
    assert not plan.needs_model(inputs(), draft)


def test_light_draft():
    draft = plan.rule_draft(inputs(sleep_hours=5), NOW)
    assert draft.readiness == "light" and draft.adjusted
    assert [e.sets for e in draft.exercises] == [5, 2, 5, 2, 2, 2]
    assert all(e.weightFactor == 0.9 and not e.skip and e.replaceWith is None for e in draft.exercises)
    assert (draft.exercises[0].repsMin, draft.exercises[0].repsMax) == (8, 12)
    assert (draft.exercises[3].repsMin, draft.exercises[3].repsMax) == (None, None)  # dropset keeps its drops
    assert "спал 5 ч" in draft.summary.lower() and not draft.summary.startswith("Сегодня лучше отдохнуть")
    assert [e.name for e in draft.exercises] == [i.name for i in ARMS]


def test_light_never_raises_sets():
    draft = plan.rule_draft(inputs([DayItem(1, "жим лёжа", 1, 5, 5)], energy=2), NOW)
    assert draft.exercises[0].sets == 1


def test_rest_draft_skips_everything():
    draft = plan.rule_draft(inputs(sleep_hours=3), NOW)
    assert draft.readiness == "rest" and draft.adjusted and all(e.skip for e in draft.exercises)
    assert "спал 3 ч" in draft.summary.lower() and not draft.summary.startswith("Сегодня лучше отдохнуть")


def test_pain_lightens_only_loaded_exercises():
    draft = plan.rule_draft(inputs(sleep_hours=8, pains=[Pain("левое плечо", None)]), NOW)
    by = {e.name: e for e in draft.exercises}
    assert by["жим гантелей сидя"].weightFactor == 0.7 and "плечо" in by["жим гантелей сидя"].reason
    assert by["отведения на дельты"].weightFactor == 0.7
    assert by["сгибания с гантелями на бицепс с супинацией"].weightFactor == 0.9  # light day only
    assert plan.needs_model(inputs(pains=[Pain("левое плечо", None)]), draft)


def test_mild_pain_on_a_normal_day():
    draft = plan.rule_draft(inputs(pains=[Pain("плечо", 1)]), NOW)
    assert draft.readiness == "normal" and draft.adjusted
    by = {e.name: e for e in draft.exercises}
    assert by["отведения на дельты"].weightFactor == 0.85 and by["отведения на дельты"].sets == 3
    assert by["французский жим в блоке из-за головы"].weightFactor == 1


def test_sore_date_is_the_local_workout_day():
    # 22:30 UTC on Oct 3 is 01:30 Moscow on Oct 4: the workout's local day.
    at = datetime(2026, 10, 3, 22, 30, tzinfo=UTC)
    recent = [RecentSets(at, "сгибания с гантелями на бицепс с супинацией", 6, date(2026, 10, 4))]
    draft = plan.rule_draft(inputs(texts=["бицепс забит"], recent=recent), NOW)
    assert "после 04.10" in draft.exercises[1].reason


def test_sore_muscles_after_heavy_session():
    recent = [RecentSets(NOW - timedelta(hours=30), "сгибания с гантелями на бицепс с супинацией", 6)]
    sore = inputs(texts=["бицепс забит после пятницы"], recent=recent)
    draft = plan.rule_draft(sore, NOW)
    by = {e.name: e for e in draft.exercises}
    assert by["сгибания с гантелями на бицепс с пронацией"].weightFactor == 0.85
    assert "не восстановил" in by["сгибания с гантелями на бицепс с пронацией"].reason
    assert by["отведения на дельты"].weightFactor == 1
    # No soreness record, or the session was > 48 h ago, or it was light: no change.
    assert not plan.rule_draft(inputs(recent=recent), NOW).adjusted
    old = [RecentSets(NOW - timedelta(hours=50), "сгибания с гантелями на бицепс с супинацией", 6)]
    assert not plan.rule_draft(inputs(texts=["бицепс забит"], recent=old), NOW).adjusted
    few = [RecentSets(NOW - timedelta(hours=30), "сгибания с гантелями на бицепс с супинацией", 2)]
    assert not plan.rule_draft(inputs(texts=["бицепс забит"], recent=few), NOW).adjusted


def test_training_facts_trigger_the_model():
    i = inputs(facts=[("не делаю жим над головой", "training")])
    assert plan.needs_model(i, plan.rule_draft(i, NOW))
    j = inputs(facts=[("не ем творог", "food")])
    assert not plan.needs_model(j, plan.rule_draft(j, NOW))


# ---- the model's answer ----


def answer(draft, **changes) -> dict:
    items = [e.model_dump() for e in draft.exercises]
    for i, ch in changes.get("items", {}).items():
        items[i].update(ch)
    return {"summary": changes.get("summary", "Итог."), "readiness": "normal", "exercises": items}


def test_refinement_keeps_program_names_and_readiness():
    draft = plan.rule_draft(inputs(sleep_hours=5), NOW)
    data = answer(draft, items={0: {"name": "Сгибания С Гантелями На Бицепс С Супинацией", "weightFactor": 0.8,
                                    "sets": 4, "reason": "меньше объёма"}})
    out = plan.apply_refinement(draft, data, inputs(sleep_hours=5))
    assert out.readiness == "light" and out.summary == "Итог."
    assert out.exercises[0].name == ARMS[0].name and out.exercises[0].weightFactor == 0.8
    assert out.exercises[0].sets == 4 and out.exercises[0].reason == "меньше объёма"


def test_refinement_never_heavier_than_the_light_draft():
    draft = plan.rule_draft(inputs(sleep_hours=5), NOW)
    data = answer(draft, items={0: {"weightFactor": 3.0, "sets": 40, "repsMin": 0, "repsMax": 500},
                                1: {"weightFactor": 1.5, "sets": 10},
                                2: {"weightFactor": 0.8, "sets": 3},
                                3: {"repsMin": 10, "repsMax": 12}})
    out = plan.apply_refinement(draft, data, inputs(sleep_hours=5))
    e = out.exercises[0]
    # sets <= draft sets, factor <= draft factor on a light day; reps outside 1..50 fall back / are clamped
    assert (e.weightFactor, e.sets, e.repsMin, e.repsMax) == (0.9, 5, 8, 50)
    assert (out.exercises[1].weightFactor, out.exercises[1].sets) == (0.9, 2)
    assert (out.exercises[2].weightFactor, out.exercises[2].sets) == (0.8, 3)  # lighter is fine
    assert (out.exercises[3].repsMin, out.exercises[3].repsMax) == (None, None)  # dropset


def test_refinement_on_a_normal_day_never_above_the_program():
    i = inputs(facts=[("не делаю становую", "training")])
    draft = plan.rule_draft(i, NOW)
    assert draft.readiness == "normal" and plan.needs_model(i, draft)
    data = answer(draft, items={0: {"weightFactor": 1.3, "sets": 8}, 1: {"weightFactor": 0.8}})
    out = plan.apply_refinement(draft, data, i)
    assert (out.exercises[0].weightFactor, out.exercises[0].sets) == (1.0, 6)
    assert out.exercises[1].weightFactor == 0.8


def test_refinement_keeps_pain_protected_reps_and_sets():
    i = inputs(sleep_hours=5, pains=[Pain("левое плечо", None)])
    draft = plan.rule_draft(i, NOW)
    pos = 4  # отведения на дельты: draft 2 sets, 12–15, ×0.7
    assert (draft.exercises[pos].sets, draft.exercises[pos].weightFactor) == (2, 0.7)
    data = answer(draft, items={pos: {"sets": 10, "repsMin": 30, "repsMax": 50, "weightFactor": 1.4},
                                0: {"repsMin": 12, "repsMax": 20}})
    out = plan.apply_refinement(draft, data, i)
    e = out.exercises[pos]
    assert (e.sets, e.repsMin, e.repsMax, e.weightFactor) == (2, 12, 15, 0.7)
    assert (out.exercises[0].repsMin, out.exercises[0].repsMax) == (12, 20)  # unprotected reps may change


def test_replacement_that_loads_the_painful_place_is_dropped():
    i = inputs(sleep_hours=8, pains=[Pain("левое плечо", 2)])
    draft = plan.rule_draft(i, NOW)
    data = answer(draft, items={3: {"replaceWith": "армейский жим с гантелями"},
                                0: {"replaceWith": "молотковые сгибания с гантелями"}})
    out = plan.apply_refinement(draft, data, i)
    assert out.exercises[3].replaceWith is None
    assert out.exercises[0].replaceWith == "молотковые сгибания с гантелями"


@pytest.mark.parametrize(
    "data",
    [
        "not a dict",
        {"exercises": "no"},
        {"summary": "x"},
        {"exercises": [{"name": "жим лёжа"}] * 5},  # count mismatch
        {"exercises": [{"name": "другое упражнение"}] * 6},  # names do not match the day
    ],
)
def test_structural_mismatch_falls_back_to_draft(data):
    draft = plan.rule_draft(inputs(sleep_hours=5), NOW)
    assert plan.apply_refinement(draft, data, inputs(sleep_hours=5)) == draft


def test_replacement_must_stay_in_group_and_equipment():
    draft = plan.rule_draft(inputs(sleep_hours=5), NOW)
    data = answer(draft, items={
        0: {"replaceWith": "молотковые сгибания с гантелями"},  # biceps, dumbbells: ok
        1: {"replaceWith": "присед со штангой"},  # another group
        2: {"replaceWith": "французский жим лёжа со штангой"},  # triceps, but a barbell instead of the cable
        4: {"replaceWith": "что-нибудь полегче"},  # unknown group
    })
    out = plan.apply_refinement(draft, data, inputs(sleep_hours=5))
    assert [e.replaceWith for e in out.exercises[:5]] == ["молотковые сгибания с гантелями", None, None, None, None]


def test_model_cannot_unprotect_pain_or_rest():
    i = inputs(sleep_hours=8, pains=[Pain("левое плечо", None)])
    draft = plan.rule_draft(i, NOW)
    data = answer(draft, items={3: {"weightFactor": 1.0}, 4: {"weightFactor": 0.5}})
    out = plan.apply_refinement(draft, data, i)
    assert out.exercises[3].weightFactor == 0.7  # not lighter than the pain rule allows... nor heavier
    assert out.exercises[4].weightFactor == 0.5

    r = inputs(sleep_hours=3)
    rest = plan.rule_draft(r, NOW)
    out = plan.apply_refinement(rest, answer(rest, items={0: {"skip": False}}, summary="Отдыхай."), r)
    assert all(e.skip for e in out.exercises) and out.summary == "Отдыхай."


def test_summary_prefix_is_stripped():
    draft = plan.rule_draft(inputs(sleep_hours=3), NOW)
    out = plan.apply_refinement(draft, answer(draft, summary="Сегодня лучше отдохнуть: спал 3 ч."), inputs(sleep_hours=3))
    assert out.summary == "Спал 3 ч."


# ---- names come from the program in the DB (Exercise.name), the same names as its JSON ----


async def test_db_names_match_program_json(db, settings):
    """plan, next_weights and MCP name items by Exercise.name (no JSON reads): for the template every name
    must equal the name in data/programs, the names the Mini App and the history knew."""
    data = json.loads((settings.programs_dir / "arms_specialization_8w.json").read_text(encoding="utf-8"))
    async with db() as s:
        program_id = await s.scalar(select(Program.id).where(Program.slug == "arms_specialization_8w"))
        program = await load_program(s, program_id)
    db_names = {
        (w.number, d.weekday, i.order): i.exercise.name for w in program.weeks for d in w.days for i in d.items
    }
    json_names = {
        (w["number"], d["weekday"], e["order"]): e["name"]
        for w in data["weeks"] for d in w["days"] for e in d["exercises"]
    }
    assert len(json_names) == 128
    assert db_names == json_names


# ---- DB: collect, cache, regenerate ----


class FakeLLM:
    def __init__(self, settings, answers=None):
        self.answers = list(answers or [])
        self.requests = 0
        http = httpx.AsyncClient(transport=httpx.MockTransport(self._handle))
        self.client = OpenRouterClient(
            settings.model_copy(update={"openrouter_api_key": "k", "openrouter_fallback_models": [],
                                        "groq_api_key": "", "stt_api_key": ""}),
            http,
        )

    async def _handle(self, req: httpx.Request) -> httpx.Response:
        self.requests += 1
        content = self.answers.pop(0) if self.answers else "not json"
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


async def _user_on_program(db, *, start: date = MON) -> int:
    async with db() as s:
        program = await s.scalar(select(Program).where(Program.slug == "arms_specialization_8w"))
        user = User(telegram_id=42, rest_seconds=90)
        s.add(user)
        await s.flush()
        from gymbot.db.models import UserProgram

        s.add(UserProgram(user_id=user.id, program_id=program.id, started_on=start))
        await s.commit()
        return user.id


async def _wellbeing(db, uid: int, at: datetime, **kw) -> None:
    async with db() as s:
        pains = kw.pop("pains", None)
        s.add(WellbeingEntry(user_id=uid, noted_at=at, raw_text=kw.pop("raw_text", "текст"),
                             pains=json.dumps(pains, ensure_ascii=False) if pains else None, **kw))
        await s.commit()


async def _build(db, settings, llm, now=NOW, force=False):
    async with db() as s:
        user = await s.scalar(select(User).where(User.telegram_id == 42))
        return await plan.get_or_build(s, user, settings, llm, MSK, now, force=force)


async def test_collect_inputs(db, settings):
    uid = await _user_on_program(db)
    await _wellbeing(db, uid, NOW - timedelta(days=1), sleep_hours=Decimal(7), energy=4,
                     pains=[{"place": "колено", "severity": 2}])
    await _wellbeing(db, uid, NOW - timedelta(hours=2), sleep_hours=Decimal(5),
                     pains=[{"place": "Колено", "severity": 4}, {"place": "плечо", "severity": None}])
    await _wellbeing(db, uid, NOW - timedelta(days=3), sleep_hours=Decimal(2))  # too old
    async with db() as s:
        s.add(UserFact(user_id=uid, text="не делаю жим над головой", category="training", active=True))
        await s.commit()
        user = await s.get(User, uid)
        i = await plan.collect_inputs(s, user, settings, MSK, NOW)
    assert [it.name for it in i.items] == [a.name for a in ARMS] and i.items[3].drop_reps == [12, 6, 6]
    assert i.sleep_hours == 5 and i.energy == 4  # newest value of each
    assert {(p.place.lower(), p.severity) for p in i.pains} == {("колено", 4), ("плечо", None)}
    assert i.facts == [("не делаю жим над головой", "training")]


async def test_not_a_training_day(db, settings):
    await _user_on_program(db)
    tue = NOW + timedelta(days=1)
    assert await _build(db, settings, None, now=tue) is None
    async with db() as s:
        user = await s.scalar(select(User))
        assert await plan.collect_inputs(s, user, settings, MSK, tue) is None


async def test_no_program_no_plan(db, settings):
    async with db() as s:
        s.add(User(telegram_id=42, rest_seconds=90))
        await s.commit()
    assert await _build(db, settings, None) is None


async def test_cache_and_regenerate(db, settings):
    uid = await _user_on_program(db)
    await _wellbeing(db, uid, NOW - timedelta(hours=2), sleep_hours=Decimal(5))
    llm = FakeLLM(settings)  # answers garbage: the rule draft is used and cached
    built = await _build(db, settings, llm.client)
    assert built.out.readiness == "light" and built.out.adjusted and llm.requests >= 1
    first = llm.requests
    again = await _build(db, settings, llm.client, now=NOW + timedelta(minutes=5))
    assert llm.requests == first and again.out == built.out  # same inputs: cached, the model is not asked
    await _build(db, settings, llm.client, force=True)
    assert llm.requests > first
    await _wellbeing(db, uid, NOW + timedelta(minutes=10), energy=1)
    changed = await _build(db, settings, llm.client, now=NOW + timedelta(minutes=11))
    assert changed.out.readiness == "rest"
    async with db() as s:
        assert await s.scalar(select(func.count()).select_from(DayPlan)) == 1  # upsert, one row per day


async def test_todays_workout_does_not_rebuild_the_plan(db, settings):
    from gymbot.db.models import Workout, WorkoutSet
    from gymbot.services.programs import get_or_create_exercise

    uid = await _user_on_program(db)
    await _wellbeing(db, uid, NOW - timedelta(hours=2), sleep_hours=Decimal(5), raw_text="бицепс забит")
    llm = FakeLLM(settings)
    first = await _build(db, settings, llm.client)
    calls = llm.requests
    async with db() as s:  # the user trains: 6 biceps sets today
        ex = await get_or_create_exercise(s, "сгибания с гантелями на бицепс с супинацией")
        w = Workout(user_id=uid, performed_on=MON, started_at=NOW, source="chat")
        w.sets = [WorkoutSet(exercise_id=ex.id, set_index=i, reps=10, weight_kg=Decimal(14)) for i in range(6)]
        s.add(w)
        await s.commit()
    again = await _build(db, settings, llm.client, now=NOW + timedelta(hours=1))
    assert llm.requests == calls and again.out == first.out


async def test_valid_model_answer_is_used(db, settings):
    uid = await _user_on_program(db)
    await _wellbeing(db, uid, NOW - timedelta(hours=2), sleep_hours=Decimal(5))
    draft = plan.rule_draft(inputs(sleep_hours=5), NOW)
    llm = FakeLLM(settings, [json.dumps(answer(draft, summary="Сегодня полегче.",
                                               items={0: {"weightFactor": 0.8}}), ensure_ascii=False)])
    built = await _build(db, settings, llm.client)
    assert built.out.summary == "Сегодня полегче." and built.out.exercises[0].weightFactor == 0.8
    assert llm.requests == 1


async def test_normal_day_does_not_call_the_model(db, settings):
    await _user_on_program(db)
    llm = FakeLLM(settings)
    built = await _build(db, settings, llm.client)
    assert built.out.model_dump() == {"date": MON, "week": 1, "weekday": 1, "adjusted": False,
                                      "readiness": "normal", "summary": None, "exercises": []}
    assert llm.requests == 0


# ---- /plan ----


def _message():
    return SimpleNamespace(from_user=SimpleNamespace(id=42, full_name="Amir"), answer=AsyncMock())


async def test_plan_command_light_day(db, settings, monkeypatch):
    monkeypatch.setattr(plan, "utcnow", lambda: NOW)
    uid = await _user_on_program(db)
    await _wellbeing(db, uid, NOW - timedelta(hours=2), sleep_hours=Decimal(5),
                     pains=[{"place": "левое плечо", "severity": None}])
    llm = FakeLLM(settings)
    msg = _message()
    await plan_handler.show_plan(msg, settings.model_copy(update={"miniapp_url": "https://x.test/app"}), db,
                                 llm.client)
    text = msg.answer.await_args.args[0]
    assert text.startswith("Сегодня: лёгкая версия")
    assert "• Сгибания с гантелями на бицепс с супинацией 5×8–12, вес −10 %" in text
    assert "• Жим гантелей сидя 2× дропсет 12-6-6, вес −30 %" in text
    kb = msg.answer.await_args.kwargs["reply_markup"]
    assert kb.inline_keyboard[0][0].web_app.url == "https://x.test/app"


async def test_plan_command_rest_and_normal_and_no_training(db, settings, monkeypatch):
    monkeypatch.setattr(plan, "utcnow", lambda: NOW)
    uid = await _user_on_program(db)
    msg = _message()
    await plan_handler.show_plan(msg, settings, db, None)
    text = msg.answer.await_args.args[0]
    assert text.startswith("Сегодня по программе, без поправок")
    assert "• Сгибания с гантелями на бицепс с супинацией 6×8–12" in text
    assert msg.answer.await_args.kwargs.get("reply_markup") is None  # no MINIAPP_URL

    await _wellbeing(db, uid, NOW - timedelta(hours=1), sleep_hours=Decimal(3))
    msg = _message()
    await plan_handler.show_plan(msg, settings, db, None)
    assert msg.answer.await_args.args[0].startswith("Сегодня лучше отдохнуть: Спал 3 ч")

    monkeypatch.setattr(plan, "utcnow", lambda: NOW + timedelta(days=1))
    msg = _message()
    await plan_handler.show_plan(msg, settings, db, None)
    assert "тренировки нет" in msg.answer.await_args.args[0]


async def test_plan_command_starts_program_for_a_new_user(db, settings, monkeypatch):
    monkeypatch.setattr(plan, "utcnow", lambda: NOW)
    msg = _message()
    await plan_handler.show_plan(msg, settings, db, None)
    assert msg.answer.await_args.args[0].startswith("Сегодня по программе")


# ---- recompute after a wellbeing record ----


async def test_plan_follows_saved_wellbeing_on_a_training_day(db, settings, monkeypatch):
    from gymbot.handlers import log_text
    from gymbot.llm.schemas import ParseResult

    monkeypatch.setattr(plan, "utcnow", lambda: NOW)
    await _user_on_program(db)
    llm = FakeLLM(settings)
    token = "t1"
    log_text.PENDING[token] = log_text.Pending(
        42, ParseResult.model_validate({"kind": "wellbeing", "wellbeing": {"sleep_hours": 5}}), "спал 5 ч", NOW
    )
    cb = SimpleNamespace(
        data=f"save:{token}", from_user=SimpleNamespace(id=42, full_name="Amir"),
        message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock()), answer=AsyncMock(),
    )
    await log_text.save(cb, settings, db, llm.client)
    assert cb.message.edit_text.await_args.args[0].endswith("Самочувствие сохранено ✅")
    assert cb.message.answer.await_args.args[0].startswith("Сегодня: лёгкая версия")
    assert cb.answer.await_count == 1


async def test_no_plan_message_when_not_adjusted_or_not_a_training_day(db, settings, monkeypatch):
    from gymbot.handlers import log_text
    from gymbot.llm.schemas import ParseResult

    await _user_on_program(db)
    for now, sleep in ((NOW, 8), (NOW + timedelta(days=1), 3)):
        monkeypatch.setattr(plan, "utcnow", lambda now=now: now)
        log_text.PENDING["t"] = log_text.Pending(
            42, ParseResult.model_validate({"kind": "wellbeing", "wellbeing": {"sleep_hours": sleep}}), "сон", now
        )
        cb = SimpleNamespace(
            data="save:t", from_user=SimpleNamespace(id=42, full_name="Amir"),
            message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock()), answer=AsyncMock(),
        )
        await log_text.save(cb, settings, db, None)
        assert cb.message.answer.await_count == 0


async def test_plan_failure_does_not_break_the_save(db, settings, monkeypatch):
    from gymbot.handlers import log_text
    from gymbot.llm.schemas import ParseResult

    monkeypatch.setattr(plan, "utcnow", lambda: NOW)
    await _user_on_program(db)

    async def boom(*a, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(plan, "get_or_build", boom)
    log_text.PENDING["t"] = log_text.Pending(
        42, ParseResult.model_validate({"kind": "wellbeing", "wellbeing": {"sleep_hours": 5}}), "сон", NOW
    )
    cb = SimpleNamespace(
        data="save:t", from_user=SimpleNamespace(id=42, full_name="Amir"),
        message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock()), answer=AsyncMock(),
    )
    await log_text.save(cb, settings, db, None)
    assert cb.message.edit_text.await_args.args[0].endswith("Самочувствие сохранено ✅")
    async with db() as s:
        assert await s.scalar(select(func.count()).select_from(WellbeingEntry)) == 1


def test_help_and_commands_mention_plan():
    from gymbot.handlers.common import HELP

    assert "/plan" in HELP


async def test_concurrent_save_uses_the_other_plan(db, settings, monkeypatch):
    """IntegrityError on the unique (user, day): the plan saved by the other writer is returned."""
    uid = await _user_on_program(db)
    await _wellbeing(db, uid, NOW - timedelta(hours=2), sleep_hours=Decimal(5))
    async with db() as s:  # the other writer's plan for today
        s.add(DayPlan(user_id=uid, plan_date=MON, readiness="rest", summary="Чужой.", inputs_hash="x" * 64,
                      exercises_json=json.dumps([{"name": a.name, "sets": a.sets, "repsMin": None, "repsMax": None,
                                                  "weightFactor": 1.0, "skip": True, "replaceWith": None,
                                                  "reason": None} for a in ARMS], ensure_ascii=False)))
        await s.commit()
    real, calls = plan._stored, []

    async def racing(session, user_id, day):  # not seen before the insert: the race window
        calls.append(day)
        return None if len(calls) <= 2 else await real(session, user_id, day)

    monkeypatch.setattr(plan, "_stored", racing)
    built = await _build(db, settings, None)
    assert built.out.readiness == "rest" and built.out.summary == "Чужой." and len(calls) == 3


async def test_repeated_rebuild_after_wellbeing_does_not_call_the_model(db, settings, monkeypatch):
    monkeypatch.setattr(plan, "utcnow", lambda: NOW)
    uid = await _user_on_program(db)
    await _wellbeing(db, uid, NOW - timedelta(hours=2), sleep_hours=Decimal(5))
    llm = FakeLLM(settings)
    msg = SimpleNamespace(answer=AsyncMock())
    await plan_handler.send_after_wellbeing(msg, 42, settings, db, llm.client)
    calls = llm.requests
    assert calls >= 1 and msg.answer.await_count == 1
    await plan_handler.send_after_wellbeing(msg, 42, settings, db, llm.client)
    assert llm.requests == calls  # same inputs: the stored plan is reused


async def test_save_by_another_user_keeps_the_record(db, settings):
    from gymbot.handlers import log_text
    from gymbot.llm.schemas import ParseResult

    log_text.PENDING["t"] = log_text.Pending(
        42, ParseResult.model_validate({"kind": "wellbeing", "wellbeing": {"sleep_hours": 5}}), "сон", NOW
    )
    cb = SimpleNamespace(
        data="save:t", from_user=SimpleNamespace(id=777, full_name="Чужой"),
        message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock()), answer=AsyncMock(),
    )
    await log_text.save(cb, settings, db, None)
    assert cb.answer.await_args.kwargs.get("show_alert") is True
    assert "t" in log_text.PENDING
    log_text.PENDING.clear()
