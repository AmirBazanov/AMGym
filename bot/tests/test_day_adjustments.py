"""Manual one-day plan adjustments (stage 3 of the chat program control): the migration, the service, the
day plan rules, the stored plan and the weights of another day. Every time is passed in or pinned."""

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from alembic import command
from sqlalchemy import func, inspect, select, text
from sqlalchemy.exc import IntegrityError
from test_next_weights_day import TZ
from test_plan import ARMS, LEGS, MON, MSK, NOW, FakeLLM, _build, _user_on_program, _wellbeing, answer, inputs

from gymbot.db import migrate
from gymbot.db.models import DayAdjustment, DayPlan, User
from gymbot.db.session import make_engine
from gymbot.services import day_adjustments as dayadj
from gymbot.services import deload, next_weights, plan
from gymbot.services.day_adjustments import DayAdjust
from gymbot.services.users import get_or_create_user

DAY = date(2026, 10, 9)
AT = datetime(2026, 10, 9, 9, tzinfo=UTC)
ARMS_SETS = [6, 3, 6, 3, 3, 3]  # test_plan.ARMS


# ---- migration 0015 ----


def _schema(conn) -> dict:
    insp = inspect(conn)
    out: dict = {"tables": set(insp.get_table_names())}
    if "day_adjustments" in out["tables"]:
        out["columns"] = {c["name"]: c["nullable"] for c in insp.get_columns("day_adjustments")}
        out["pk"] = insp.get_pk_constraint("day_adjustments")["constrained_columns"]
        out["fks"] = [
            (f["constrained_columns"], f["referred_table"], f["options"].get("ondelete"))
            for f in insp.get_foreign_keys("day_adjustments")
        ]
        out["unique"] = [u["column_names"] for u in insp.get_unique_constraints("day_adjustments")]
    return out


async def test_migration_0015_round_trip(tmp_path):
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
        assert "day_adjustments" in up["tables"]
        assert up["columns"] == {
            "id": False, "user_id": False, "day": False, "weight_factor": True, "sets_delta": True,
            "skip_json": True, "note": True, "source": False, "raw_text": True, "created_at": False,
        }
        assert up["pk"] == ["id"]
        assert up["fks"] == [(["user_id"], "users", "CASCADE")]
        assert up["unique"] == [["user_id", "day"]]

        row = "INSERT INTO day_adjustments (user_id, day, source, created_at) VALUES (1, :day, 'chat', '2026-10-01 12:00:00')"
        async with engine.begin() as conn:
            await conn.execute(text("PRAGMA foreign_keys=ON"))
            await conn.execute(text("INSERT INTO users (telegram_id, rest_seconds, created_at) VALUES (1, 90, '2026-10-01 12:00:00')"))
            await conn.execute(text(row), {"day": "2026-10-09"})
            await conn.execute(text(row), {"day": "2026-10-10"})  # another day of the same user is fine
        with pytest.raises(IntegrityError, match="UNIQUE"):  # one row per user and day
            async with engine.begin() as conn:
                await conn.execute(text(row), {"day": "2026-10-09"})
        async with engine.begin() as conn:  # deleting the user takes the rows along
            await conn.execute(text("PRAGMA foreign_keys=ON"))
            await conn.execute(text("INSERT INTO users (telegram_id, rest_seconds, created_at) VALUES (2, 90, '2026-10-01 12:00:00')"))
            await conn.execute(text(
                "INSERT INTO day_adjustments (user_id, day, source, created_at) "
                "SELECT id, '2026-10-09', 'chat', '2026-10-01 12:00:00' FROM users WHERE telegram_id = 2"
            ))
            await conn.execute(text("DELETE FROM users WHERE telegram_id = 2"))
            assert (await conn.execute(text("SELECT count(*) FROM day_adjustments"))).scalar_one() == 2

        await migrate_to("0014")
        down = await run_sync(_schema)
        assert up["tables"] - down["tables"] == {"day_adjustments"}  # only this table goes
        assert down["tables"] - up["tables"] == set()
        async with engine.connect() as conn:
            assert (await conn.execute(text("SELECT telegram_id FROM users"))).scalar_one() == 1

        await migrate_to("head")
        assert await run_sync(_schema) == up
        async with engine.connect() as conn:
            assert (await conn.execute(text("SELECT count(*) FROM day_adjustments"))).scalar_one() == 0
    finally:
        await engine.dispose()


# ---- DayAdjust ----


def test_empty_and_not_empty():
    assert DayAdjust().empty()
    assert DayAdjust(note="только заметка").empty()  # a note alone changes nothing
    assert not DayAdjust(weight_factor=0.8).empty()
    assert not DayAdjust(sets_delta=-1).empty()
    assert not DayAdjust(skip_exercises=["жим"]).empty()
    assert not DayAdjust(skip_groups=["legs"]).empty()


def test_merged_new_values_replace_and_skips_add_up():
    old = DayAdjust(0.9, -1, ["б", "а"], ["legs"], "старая")
    new = DayAdjust(0.8, None, ["в", "а"], ["biceps"], None)
    assert old.merged(new) == DayAdjust(0.8, -1, ["а", "б", "в"], ["biceps", "legs"], "старая")
    assert old.merged(DayAdjust(None, -2, note="новая")) == DayAdjust(0.9, -2, ["а", "б"], ["legs"], "новая")
    assert DayAdjust().merged(old) == DayAdjust(0.9, -1, ["а", "б"], ["legs"], "старая")


@pytest.mark.parametrize(
    ("adj", "words"),
    [
        (DayAdjust(0.8), "веса −20 %"),
        (DayAdjust(0.85), "веса −15 %"),
        (DayAdjust(0.9), "веса −10 %"),
        (DayAdjust(0.7), "веса −30 %"),
        (DayAdjust(sets_delta=-1), "на подход меньше"),
        (DayAdjust(sets_delta=-2), "на 2 подхода меньше"),
        (DayAdjust(sets_delta=-4), "на 4 подхода меньше"),
        (DayAdjust(sets_delta=-5), "на 5 подходов меньше"),
        (DayAdjust(skip_groups=["legs"]), "без ног"),
        (DayAdjust(skip_groups=["legs", "biceps"]), "без ног и бицепса"),
        (DayAdjust(skip_groups=["legs", "biceps", "abs"]), "без ног, бицепса, пресса"),
        (DayAdjust(skip_groups=["wings"]), "без wings"),  # an unknown key is said as it is
        (DayAdjust(skip_exercises=["жим лёжа", "тяга"]), "пропуск: жим лёжа, тяга"),
        (DayAdjust(0.8, -1, ["присед"], ["legs"]), "веса −20 %, на подход меньше, без ног, пропуск: присед"),
    ],
)
def test_describe(adj, words):
    assert dayadj.describe(adj) == words
    assert dayadj.summary(adj) == f"Твоя поправка на день: {words}."
    assert dayadj.summary(adj).startswith(dayadj.SUMMARY_MARK)


# ---- the service on the database ----


async def _uid(db, tg: int = 42) -> int:
    async with db() as s:
        user = await get_or_create_user(s, tg, "Amir")
        await s.commit()
        return user.id


async def test_upsert_get_round_trip(db):
    uid = await _uid(db)
    adj = DayAdjust(0.8, -1, ["жим лёжа", "-"], ["legs", "biceps"], "спина")
    async with db() as s:
        assert await dayadj.get(s, uid, DAY) is None
        await dayadj.upsert(s, uid, DAY, adj, "сегодня полегче", now=AT)
        await s.commit()
    async with db() as s:
        got = await dayadj.get(s, uid, DAY)
        row = await s.scalar(select(DayAdjustment))
    assert got == DayAdjust(0.8, -1, ["-", "жим лёжа"], ["biceps", "legs"], "спина")  # sorted, a plain float
    assert isinstance(got.weight_factor, float)
    assert row.weight_factor == Decimal("0.80") and row.raw_text == "сегодня полегче" and row.source == "chat"
    assert row.skip_json == {"exercises": ["жим лёжа", "-"], "groups": ["legs", "biceps"]}
    assert row.created_at.replace(tzinfo=UTC) == AT


async def test_upsert_replaces_the_whole_row_and_keeps_one_row(db):
    uid = await _uid(db)
    async with db() as s:
        await dayadj.upsert(s, uid, DAY, DayAdjust(0.8, -2, ["а"], ["legs"], "заметка"), "первое", now=AT)
        await dayadj.upsert(s, uid, DAY, DayAdjust(sets_delta=-1), "второе", source="miniapp", now=AT)
        await s.commit()
    async with db() as s:
        assert await s.scalar(select(func.count()).select_from(DayAdjustment)) == 1
        got = await dayadj.get(s, uid, DAY)
        row = await s.scalar(select(DayAdjustment))
    assert got == DayAdjust(None, -1, [], [], None)  # not merged: the caller merges first
    assert row.skip_json is None and row.weight_factor is None and row.note is None
    assert (row.raw_text, row.source) == ("второе", "miniapp")


async def test_upsert_rounds_the_factor_and_cuts_the_note(db):
    uid = await _uid(db)
    async with db() as s:
        await dayadj.upsert(s, uid, DAY, DayAdjust(0.8049, note="я" * 500), None, now=AT)
        await s.commit()
    async with db() as s:
        got = await dayadj.get(s, uid, DAY)
    assert got.weight_factor == 0.8 and len(got.note) == dayadj.NOTE_MAX


@pytest.mark.parametrize("empty", [DayAdjust(), DayAdjust(note="только заметка")])
async def test_upsert_of_an_empty_adjustment_raises(db, empty):
    uid = await _uid(db)
    async with db() as s:
        with pytest.raises(ValueError, match="empty"):
            await dayadj.upsert(s, uid, DAY, empty, "x", now=AT)
        assert await s.scalar(select(func.count()).select_from(DayAdjustment)) == 0


async def test_between_is_inclusive_and_per_user(db):
    uid, other = await _uid(db), await _uid(db, 43)
    days = [DAY - timedelta(days=1), DAY, DAY + timedelta(days=3), DAY + timedelta(days=4)]
    async with db() as s:
        for i, d in enumerate(days):
            await dayadj.upsert(s, uid, d, DayAdjust(sets_delta=-(i + 1)), "x", now=AT)
        await dayadj.upsert(s, other, DAY, DayAdjust(0.5), "x", now=AT)
        await s.commit()
    async with db() as s:
        got = await dayadj.between(s, uid, DAY, DAY + timedelta(days=3))
        assert await dayadj.between(s, uid, DAY + timedelta(days=5), DAY + timedelta(days=9)) == {}
        assert (await dayadj.between(s, other, DAY, DAY))[DAY].weight_factor == 0.5
    assert got == {DAY: DayAdjust(sets_delta=-2), DAY + timedelta(days=3): DayAdjust(sets_delta=-3)}


async def test_clear_reports_whether_there_was_one(db):
    uid, other = await _uid(db), await _uid(db, 43)
    async with db() as s:
        assert await dayadj.clear(s, uid, DAY) is False
        await dayadj.upsert(s, uid, DAY, DayAdjust(0.8), "x", now=AT)
        await dayadj.upsert(s, other, DAY, DayAdjust(0.8), "x", now=AT)
        assert await dayadj.clear(s, uid, DAY) is True
        assert await dayadj.clear(s, uid, DAY) is False
        await s.commit()
    async with db() as s:
        assert await dayadj.get(s, uid, DAY) is None
        assert await dayadj.get(s, other, DAY) is not None  # another user's row stays


# ---- the rules of the day plan ----


def adj_inputs(adj, items=ARMS, **kw):
    return inputs(items, adjustment=adj, **kw)


def test_plan_version_was_bumped():
    assert plan.PLAN_VERSION == 4


def test_adjust_item_pure():
    adj = DayAdjust(0.8, -1)
    assert plan.adjust_item(adj, "жим лёжа", 4, 4, 1.0) == (3, 0.8, False)
    assert plan.adjust_item(adj, "жим лёжа", 4, 4, 0.7) == (3, 0.7, False)  # the lighter factor stays
    assert plan.adjust_item(adj, "жим лёжа", 4, 2, 0.9) == (2, 0.8, False)  # fewer sets already: not cut twice
    assert plan.adjust_item(adj, "жим лёжа", 1, 1, 1.0) == (1, 0.8, False)  # never below one set
    assert plan.adjust_item(DayAdjust(sets_delta=-5), "жим лёжа", 3, 3, 1.0) == (1, 1.0, False)
    assert plan.adjust_item(DayAdjust(skip_exercises=["Жим Лёжа"]), "жим лёжа", 4, 4, 1.0)[2] is True
    assert plan.adjust_item(DayAdjust(skip_groups=["legs"]), "присед со штангой", 4, 4, 1.0)[2] is True
    assert plan.adjust_item(DayAdjust(skip_groups=["legs"]), "жим лёжа", 4, 4, 1.0)[2] is False


def test_adjustment_lightens_every_exercise_and_takes_a_set_off():
    adj = DayAdjust(0.8, -1)
    draft = plan.rule_draft(adj_inputs(adj), NOW)
    assert draft.readiness == "normal" and draft.adjusted and draft.manual and not draft.deload
    assert {e.weightFactor for e in draft.exercises} == {0.8}
    assert [e.sets for e in draft.exercises] == [n - 1 for n in ARMS_SETS] == [5, 2, 5, 2, 2, 2]
    assert not any(e.skip or e.replaceWith for e in draft.exercises)
    assert [e.name for e in draft.exercises] == [i.name for i in ARMS]
    assert (draft.exercises[0].repsMin, draft.exercises[0].repsMax) == (8, 12)
    assert (draft.exercises[3].repsMin, draft.exercises[3].repsMax) == (None, None)  # a dropset keeps its drops
    assert draft.summary == dayadj.summary(adj) == "Твоя поправка на день: веса −20 %, на подход меньше."


def test_sets_never_go_below_one():
    items = [plan.DayItem(1, "жим лёжа", 1, 5, 5), plan.DayItem(2, "жим гантелей сидя", 2, 8, 12),
             plan.DayItem(3, "присед со штангой", 6, 8, 12)]
    draft = plan.rule_draft(adj_inputs(DayAdjust(sets_delta=-5), items), NOW)
    assert [e.sets for e in draft.exercises] == [1, 1, 1]
    assert {e.weightFactor for e in draft.exercises} == {1.0}


def test_sets_only_adjustment_keeps_the_weights():
    draft = plan.rule_draft(adj_inputs(DayAdjust(sets_delta=-2)), NOW)
    assert [e.sets for e in draft.exercises] == [4, 1, 4, 1, 1, 1] and {e.weightFactor for e in draft.exercises} == {1.0}


def test_weight_only_adjustment_keeps_the_sets():
    draft = plan.rule_draft(adj_inputs(DayAdjust(0.7)), NOW)
    assert [e.sets for e in draft.exercises] == ARMS_SETS and {e.weightFactor for e in draft.exercises} == {0.7}


def test_skip_by_name_marks_one_exercise():
    adj = DayAdjust(skip_exercises=["Отведения на Дельты"])  # the program's own spelling is matched by normalize()
    draft = plan.rule_draft(adj_inputs(adj), NOW)
    skipped = [e for e in draft.exercises if e.skip]
    assert [e.name for e in skipped] == ["отведения на дельты"] and skipped[0].reason == dayadj.SKIP_REASON
    assert draft.adjusted and draft.manual and draft.readiness == "normal"
    assert draft.summary == "Твоя поправка на день: пропуск: Отведения на Дельты."  # the reason is not repeated
    assert all(e.weightFactor == 1.0 and not e.reason for e in draft.exercises if not e.skip)


def test_skip_by_group_marks_the_whole_group():
    draft = plan.rule_draft(adj_inputs(DayAdjust(skip_groups=["biceps"])), NOW)
    assert {e.name for e in draft.exercises if e.skip} == {i.name for i in ARMS if plan.muscle_group(i.name) == "biceps"}
    assert {e.name for e in draft.exercises if e.skip} == {ARMS[0].name, ARMS[1].name}
    assert all(e.reason == dayadj.SKIP_REASON for e in draft.exercises if e.skip)
    assert draft.summary == "Твоя поправка на день: без бицепса."


def test_skip_group_legs_on_the_base_day():
    draft = plan.rule_draft(adj_inputs(DayAdjust(skip_groups=["legs"]), LEGS), NOW)
    assert [(e.name, e.skip) for e in draft.exercises] == [
        ("жим лёжа", False), ("тяга вертикального блока", False), ("присед со штангой", True), ("румынская тяга", True),
    ]


def test_skipped_exercise_keeps_its_program_sets_and_the_rest_are_lightened():
    adj = DayAdjust(0.8, -1, skip_exercises=[ARMS[0].name])
    draft = plan.rule_draft(adj_inputs(adj), NOW)
    assert draft.exercises[0].skip and {e.weightFactor for e in draft.exercises[1:]} == {0.8}
    assert [e.sets for e in draft.exercises[1:]] == [2, 5, 2, 2, 2]


def test_light_day_takes_the_lighter_and_does_not_cut_sets_twice():
    items = [plan.DayItem(n, f"упр {n}", n, 8, 12) for n in (1, 2, 3, 4, 5, 6)]
    light = plan.rule_draft(inputs(items, sleep_hours=5), NOW)
    both = plan.rule_draft(adj_inputs(DayAdjust(0.8, -1), items, sleep_hours=5), NOW)
    assert both.readiness == "light" and both.manual and both.adjusted
    assert {e.weightFactor for e in both.exercises} == {0.8}  # min(0.9, 0.8)
    assert {e.weightFactor for e in light.exercises} == {0.9}
    assert [e.sets for e in light.exercises] == [1, 2, 2, 3, 4, 5]
    assert [e.sets for e in both.exercises] == [1, 1, 2, 3, 4, 5]  # min of both rules, not both subtracted
    assert "спал 5 ч" in both.summary.lower() and dayadj.summary(DayAdjust(0.8, -1)) in both.summary


def test_light_day_keeps_its_factor_when_the_adjustment_is_milder():
    both = plan.rule_draft(adj_inputs(DayAdjust(0.95), sleep_hours=5), NOW)
    assert {e.weightFactor for e in both.exercises} == {0.9}


def test_pain_exercises_stay_lighter_than_the_adjustment():
    draft = plan.rule_draft(adj_inputs(DayAdjust(0.8), pains=[plan.Pain("левое плечо", None)]), NOW)
    by = {e.name: e for e in draft.exercises}
    assert by["жим гантелей сидя"].weightFactor == 0.7
    assert by[ARMS[0].name].weightFactor == 0.8


def test_adjustment_next_to_a_deload_takes_the_lighter():
    until = MON + timedelta(days=6)
    both = plan.rule_draft(adj_inputs(DayAdjust(0.8, -1), deload_until=until), NOW)
    assert both.deload and both.manual and {e.weightFactor for e in both.exercises} == {0.8}
    assert [e.sets for e in both.exercises] == [min(max(1, n - 1), deload.deload_sets(n)) for n in ARMS_SETS]
    milder = plan.rule_draft(adj_inputs(DayAdjust(0.95), deload_until=until), NOW)
    assert {e.weightFactor for e in milder.exercises} == {0.85}
    assert both.summary.startswith("Разгрузочная неделя") and dayadj.summary(DayAdjust(0.8, -1)) in both.summary


@pytest.mark.parametrize("kw", [{"sleep_hours": 3}, {"energy": 1}, {"pains": [plan.Pain("левое плечо", 5)]}])
def test_rest_wins_over_the_adjustment(kw):
    draft = plan.rule_draft(adj_inputs(DayAdjust(0.8, -1, skip_groups=["biceps"]), **kw), NOW)
    assert draft.readiness == "rest" and not draft.manual and all(e.skip for e in draft.exercises)
    assert {e.weightFactor for e in draft.exercises} == {1.0}
    assert [e.sets for e in draft.exercises] == ARMS_SETS
    assert all(e.reason != dayadj.SKIP_REASON for e in draft.exercises)
    assert dayadj.SUMMARY_MARK not in draft.summary


def test_empty_or_missing_adjustment_changes_nothing():
    for adj in (None, DayAdjust(), DayAdjust(note="заметка")):
        draft = plan.rule_draft(adj_inputs(adj), NOW)
        assert not draft.manual and not draft.adjusted and draft.summary is None


def test_hash_changes_with_an_adjustment():
    plain, with_adj = inputs(), adj_inputs(DayAdjust(0.8))
    h_plain = plan.inputs_hash(plain, plan.rule_draft(plain, NOW))
    h_adj = plan.inputs_hash(with_adj, plan.rule_draft(with_adj, NOW))
    assert h_plain != h_adj
    other = adj_inputs(DayAdjust(0.7))
    assert h_adj != plan.inputs_hash(other, plan.rule_draft(other, NOW))
    again = adj_inputs(DayAdjust(0.8))
    assert h_adj == plan.inputs_hash(again, plan.rule_draft(again, NOW))
    # even an adjustment that does not change the draft (a rest day) changes the inputs part of the hash
    rest = inputs(sleep_hours=3)
    rest_adj = adj_inputs(DayAdjust(0.8), sleep_hours=3)
    assert plan.inputs_hash(rest, plan.rule_draft(rest, NOW)) != plan.inputs_hash(rest_adj, plan.rule_draft(rest_adj, NOW))


# ---- the model's refinement ----


def test_refinement_cannot_make_an_adjusted_day_heavier():
    i = adj_inputs(DayAdjust(0.8, -1))
    draft = plan.rule_draft(i, NOW)
    data = answer(draft, items={0: {"weightFactor": 1.0, "sets": 6}, 1: {"weightFactor": 1.3, "sets": 3},
                                2: {"weightFactor": 0.7}}, summary=draft.summary)
    out = plan.apply_refinement(draft, data, i)
    assert [e.weightFactor for e in out.exercises[:3]] == [0.8, 0.8, 0.7]  # lighter is allowed
    assert [e.sets for e in out.exercises[:2]] == [5, 2]  # the draft's sets are the cap
    assert out.manual and out.adjusted and out.readiness == "normal"


def test_refinement_that_forgets_the_note_gets_it_in_front():
    adj = DayAdjust(0.8, -1)
    i = adj_inputs(adj)
    draft = plan.rule_draft(i, NOW)
    out = plan.apply_refinement(draft, answer(draft, summary="Сегодня работаем в спокойном темпе."), i)
    assert out.summary == f"{dayadj.summary(adj)} Сегодня работаем в спокойном темпе."


def test_refinement_that_keeps_the_note_is_not_doubled():
    i = adj_inputs(DayAdjust(0.8, -1))
    draft = plan.rule_draft(i, NOW)
    text_ = "Твоя поправка учтена: веса −20 %."
    assert plan.apply_refinement(draft, answer(draft, summary=text_), i).summary == text_


def test_refinement_without_a_manual_adjustment_gets_no_note():
    i = inputs(sleep_hours=5)
    draft = plan.rule_draft(i, NOW)
    out = plan.apply_refinement(draft, answer(draft, summary="Итог."), i)
    assert not out.manual and out.summary == "Итог."


def test_refinement_keeps_the_skips_of_the_adjustment():
    i = adj_inputs(DayAdjust(skip_exercises=[ARMS[4].name]))
    draft = plan.rule_draft(i, NOW)
    out = plan.apply_refinement(draft, answer(draft, items={4: {"skip": False}}), i)
    assert out.exercises[4].name == ARMS[4].name and out.manual


# ---- the stored plan and the weights of another day ----


async def test_collect_inputs_reads_the_adjustment(db, settings):
    uid = await _user_on_program(db)
    async with db() as s:
        user = await s.get(User, uid)
        assert (await plan.collect_inputs(s, user, settings, MSK, NOW)).adjustment is None
        await dayadj.upsert(s, uid, MON + timedelta(days=2), DayAdjust(0.8), "x", now=NOW)  # another day
        await dayadj.upsert(s, uid, MON, DayAdjust(0.8, -1, ["а"]), "x", now=NOW)
        await s.commit()
        got = (await plan.collect_inputs(s, user, settings, MSK, NOW)).adjustment
    assert got == DayAdjust(0.8, -1, ["а"], [], None)


async def test_get_or_build_rebuilds_with_a_stored_adjustment(db, settings):
    uid = await _user_on_program(db)
    plain = await _build(db, settings, None)
    assert not plain.out.adjusted and plain.out.exercises == []
    async with db() as s:
        stored = await s.scalar(select(DayPlan.inputs_hash))

    async with db() as s:
        await dayadj.upsert(s, uid, MON, DayAdjust(0.8, -1, ["отведения на дельты"]), "x", now=NOW)
        await s.commit()
    built = await _build(db, settings, None, now=NOW + timedelta(minutes=5))
    out = built.out
    assert out.adjusted and out.readiness == "normal"
    assert out.summary.startswith(dayadj.SUMMARY_MARK) and "веса −20 %" in out.summary
    assert {e.weightFactor for e in out.exercises if not e.skip} == {0.8}
    by = {e.name: e for e in out.exercises}
    assert by["отведения на дельты"].skip and by["отведения на дельты"].reason == dayadj.SKIP_REASON
    assert [e.sets for e in out.exercises if not e.skip] == [5, 2, 5, 2, 2]
    async with db() as s:
        assert await s.scalar(select(func.count()).select_from(DayPlan)) == 1  # replaced, not added
        assert await s.scalar(select(DayPlan.inputs_hash)) != stored
    assert dayadj.SUMMARY_MARK in plan.plan_text(built)

    async with db() as s:  # the command is taken back: the plain program day again
        assert await dayadj.clear(s, uid, MON)
        await s.commit()
    back = await _build(db, settings, None, now=NOW + timedelta(minutes=10))
    assert not back.out.adjusted and back.out.exercises == []


async def test_rebuild_after_wellbeing_does_not_drop_the_adjustment(db, settings):
    uid = await _user_on_program(db)
    async with db() as s:
        await dayadj.upsert(s, uid, MON, DayAdjust(0.7), "x", now=NOW)
        await s.commit()
    await _wellbeing(db, uid, NOW - timedelta(hours=2), sleep_hours=Decimal(5))
    built = await _build(db, settings, None)  # a light day (rules only: no model)
    assert built.out.readiness == "light"
    assert {e.weightFactor for e in built.out.exercises} == {0.7}
    assert dayadj.summary(DayAdjust(0.7)) in built.out.summary


async def test_model_answer_cannot_undo_the_adjustment_in_a_stored_plan(db, settings):
    uid = await _user_on_program(db)
    adj = DayAdjust(0.8)
    async with db() as s:
        await dayadj.upsert(s, uid, MON, adj, "x", now=NOW)
        await s.commit()
    await _wellbeing(db, uid, NOW - timedelta(hours=2), sleep_hours=Decimal(5))
    draft = plan.rule_draft(inputs(sleep_hours=5, adjustment=adj), NOW)
    llm = FakeLLM(settings, [json.dumps(answer(draft, summary="Всё хорошо.", items={0: {"weightFactor": 1.0}}),
                                        ensure_ascii=False)])
    built = await _build(db, settings, llm.client)
    assert llm.requests == 1
    assert built.out.exercises[0].weightFactor == 0.8
    assert built.out.summary == f"{dayadj.summary(adj)} Всё хорошо."


MON2 = date(2026, 10, 12)  # NOW is Monday 05.10: a program day a week ahead (week 2)


async def _with_history(db, settings):
    """A user on the arms program who lifted every exercise of MON2 on 01.10 (40 kg x 12): weights exist."""
    from gymbot.db.models import Workout, WorkoutSet
    from gymbot.services.programs import get_or_create_exercise

    uid = await _user_on_program(db)
    async with db() as s:
        user = await s.get(User, uid)
        names = [r.program_name for r in (await next_weights.day_weights(s, user, settings, TZ, NOW, MON2)).rows]
        w = Workout(user_id=uid, performed_on=date(2026, 10, 1), started_at=NOW - timedelta(days=4), source="chat")
        i = 0
        for name in names:
            ex = await get_or_create_exercise(s, name)
            for reps in (12, 12, 12):
                w.sets.append(WorkoutSet(exercise_id=ex.id, set_index=i, reps=reps, weight_kg=Decimal(40)))
                i += 1
        s.add(w)
        await s.commit()
    return uid, names


async def _weights(db, uid, settings, day=MON2):
    async with db() as s:
        user = await s.get(User, uid)
        return await next_weights.day_weights(s, user, settings, TZ, NOW, day)


async def _store(db, uid, adj, day=MON2):
    async with db() as s:
        await dayadj.upsert(s, uid, day, adj, "x", now=NOW)
        await s.commit()


async def test_day_weights_for_another_day_apply_sets_skip_and_summary(db, settings):
    uid, names = await _with_history(db, settings)
    base = await _weights(db, uid, settings)
    assert base.summary is None and len(base.rows) == 6
    assert all(r.suggestion is not None and r.suggestion.weight for r in base.rows)
    skipped = names[4]
    await _store(db, uid, DayAdjust(0.8, -1, [skipped]))
    adjusted = await _weights(db, uid, settings)
    assert adjusted.summary == f"Твоя поправка на день: веса −20 %, на подход меньше, пропуск: {skipped}."
    for b, a in zip(base.rows, adjusted.rows, strict=True):
        if a.program_name == skipped:
            assert a.suggestion is None and a.note == dayadj.SKIP_REASON and a.sets == b.sets
            continue
        assert a.sets == max(1, b.sets - 1) and a.note is None
        assert a.suggestion.factor == 0.8 and a.suggestion.base_weight == b.suggestion.weight
        assert a.suggestion.weight < b.suggestion.weight


async def test_day_weights_skip_by_group(db, settings):
    uid, names = await _with_history(db, settings)
    await _store(db, uid, DayAdjust(skip_groups=["biceps"]))
    rows = (await _weights(db, uid, settings)).rows
    gone = {r.program_name for r in rows if r.suggestion is None}
    assert gone == {n for n in names if plan.muscle_group(n) == "biceps"} and len(gone) == 2
    assert all(r.note == dayadj.SKIP_REASON for r in rows if r.suggestion is None)
    assert all(r.suggestion.factor == 1.0 for r in rows if r.suggestion is not None)


async def test_day_weights_sets_only_keeps_the_weights(db, settings):
    uid, _ = await _with_history(db, settings)
    base = await _weights(db, uid, settings)
    await _store(db, uid, DayAdjust(sets_delta=-5))
    got = await _weights(db, uid, settings)
    assert [r.sets for r in got.rows] == [1] * 6
    assert [r.suggestion.weight for r in got.rows] == [r.suggestion.weight for r in base.rows]
    assert got.summary == "Твоя поправка на день: на 5 подходов меньше."


async def test_day_weights_leave_other_days_alone(db, settings):
    uid, _ = await _with_history(db, settings)
    await _store(db, uid, DayAdjust(0.5, -1))
    wed = await _weights(db, uid, settings, date(2026, 10, 14))
    assert wed.summary is None and all(r.note is None for r in wed.rows)


async def test_day_weights_with_a_deload_the_lighter_wins(db, settings):
    from gymbot.db.models import DeloadState
    uid, _ = await _with_history(db, settings)
    base = await _weights(db, uid, settings)
    async with db() as s:
        s.add(DeloadState(user_id=uid, started_on=MON2, until=MON2 + timedelta(days=6)))
        await s.commit()
    only_deload = await _weights(db, uid, settings)
    assert [r.sets for r in only_deload.rows] == [deload.deload_sets(r.sets) for r in base.rows]
    await _store(db, uid, DayAdjust(0.95, -1))  # milder weights: the deload's factor stays
    milder = await _weights(db, uid, settings)
    assert [r.suggestion.factor for r in milder.rows] == [deload.WEIGHT_FACTOR] * 6
    assert [r.sets for r in milder.rows] == [min(deload.deload_sets(r.sets), max(1, r.sets - 1)) for r in base.rows]
    assert milder.summary.startswith("Разгрузочная неделя") and dayadj.SUMMARY_MARK in milder.summary
    await _store(db, uid, DayAdjust(0.6, -5))  # stronger: the adjustment wins
    stronger = await _weights(db, uid, settings)
    assert [r.suggestion.factor for r in stronger.rows] == [0.6] * 6
    assert [r.sets for r in stronger.rows] == [1] * 6
