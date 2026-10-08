"""Programs as the server's data: GET /api/programs[/{slug}], GET /api/exercises, visibility of users' own
copies, the workout snapshot of targets and programDayId (phase 1 of the program editor)."""

import json
from datetime import date
from types import SimpleNamespace

import pytest
from conftest import init_data, make_settings
from sqlalchemy import select, update

from gymbot.db.models import (
    Exercise,
    Program,
    ProgramDay,
    ProgramItem,
    ProgramWeek,
    Workout,
)
from gymbot.llm.schemas import ParseResult
from gymbot.services import baselines, chat_settings, live
from gymbot.services import workouts as ws
from gymbot.services.programs import (
    WEEKDAY_TITLES,
    backfill_program_meta,
    load_program,
    raw_prescription,
)
from gymbot.services.users import active_program, get_or_create_user, set_program

TEMPLATE = "arms_specialization_8w"
OTHER = 777


@pytest.fixture
def settings(tmp_path):
    """Both users are allowed: with an empty allowlist only the first user in `users` is let in."""
    return make_settings(tmp_path, allowed_user_ids=[42, OTHER])


@pytest.fixture
def other_auth():
    return {"X-Telegram-Init-Data": init_data(OTHER)}


def template_json(settings) -> dict:
    return json.loads((settings.programs_dir / f"{TEMPLATE}.json").read_text(encoding="utf-8"))


def json_positions(data: dict) -> dict[tuple[int, int, int], dict]:
    """(week, weekday, order) -> exercise of the program JSON."""
    return {
        (w["number"], d["weekday"], e["order"]): e
        for w in data["weeks"]
        for d in w["days"]
        for e in d["exercises"]
    }


async def make_copy(
    session, template_slug: str, owner_user_id: int, slug: str, name: str, program_id: int | None = None
) -> Program:
    """Test-only deep copy of a template into a user's own program (phase 2 has the real fork)."""
    template = await load_program(session, (await session.scalar(select(Program).where(Program.slug == template_slug))).id)
    extra = {} if program_id is None else {"id": program_id}
    copy = Program(
        slug=slug,
        name=name,
        source=template.source,
        owner_user_id=owner_user_id,
        based_on_id=template.id,
        version=1,
        **extra,
    )
    for w in template.weeks:
        week = ProgramWeek(number=w.number)
        for d in w.days:
            day = ProgramDay(weekday=d.weekday, focus=d.focus, base_day_id=d.id)
            for i in d.items:
                day.items.append(
                    ProgramItem(
                        exercise_id=i.exercise_id,
                        order=i.order,
                        intensity=i.intensity,
                        sets=i.sets,
                        reps_min=i.reps_min,
                        reps_max=i.reps_max,
                        drop_reps=list(i.drop_reps) if i.drop_reps else None,
                    )
                )
            week.days.append(day)
        copy.weeks.append(week)
    session.add(copy)
    await session.flush()
    return copy


async def add_user(db, telegram_id: int):
    async with db() as s:
        user = await get_or_create_user(s, telegram_id)
        await s.commit()
        return user.id


async def give_unique_exercise(session, program: Program, name: str) -> None:
    """The first item of `program` now trains an exercise that exists nowhere else."""
    ex = Exercise(name=name, aliases=[])
    session.add(ex)
    await session.flush()
    item = (
        await session.scalars(
            select(ProgramItem)
            .join(ProgramDay, ProgramDay.id == ProgramItem.day_id)
            .join(ProgramWeek, ProgramWeek.id == ProgramDay.week_id)
            .where(ProgramWeek.program_id == program.id)
            .order_by(ProgramItem.id)
            .limit(1)
        )
    ).first()
    item.exercise_id = ex.id
    await session.flush()


def workout(wid="w1", programId=TEMPLATE, week=1, weekday=1, exercises=None, **over):
    w = {
        "id": wid,
        "programId": programId,
        "week": week,
        "weekday": weekday,
        "startedAt": "2026-10-05T15:00:00Z",
        "finishedAt": "2026-10-05T16:00:00Z",
        "exercises": exercises
        or [{"name": "какое-то упражнение", "target": "", "dropset": False, "sets": [{"weight": 10, "reps": 10}]}],
    }
    w.update(over)
    return w


def ex_payload(name: str, n: int = 1, done: bool = True) -> dict:
    return {
        "name": name,
        "target": "",
        "dropset": False,
        "sets": [{"weight": 20, "reps": 10, "done": done} for _ in range(n)],
    }


async def template_day_ids(client, auth) -> dict[tuple[int, int], int]:
    """(week, weekday) -> ProgramDay.id of the template, from the API."""
    program = (await client.get(f"/api/programs/{TEMPLATE}", headers=auth)).json()
    return {(w["number"], d["weekday"]): d["id"] for w in program["weeks"] for d in w["days"]}


# ---- 1, 2: reading programs ----


async def test_list_programs_summary(client, auth, settings):
    data = template_json(settings)
    names = {e["name"] for w in data["weeks"] for d in w["days"] for e in d["exercises"]}
    r = await client.get("/api/programs", headers=auth)
    assert r.status_code == 200
    (entry,) = r.json()
    assert set(entry) == {"id", "name", "source", "weeks", "daysPerWeek", "exercises", "editable", "basedOn", "version"}
    assert entry == {
        "id": TEMPLATE,
        "name": data["name"],
        "source": data["source"],
        "weeks": 8,
        "daysPerWeek": 3,
        "exercises": len(names),
        "editable": False,
        "basedOn": None,
        "version": 1,
    }


async def test_get_program_matches_json_exactly(client, auth, settings):
    data = template_json(settings)
    r = await client.get(f"/api/programs/{TEMPLATE}", headers=auth)
    assert r.status_code == 200
    out = r.json()
    assert set(out) == {"id", "name", "source", "version", "editable", "basedOn", "weeks"}
    assert (out["id"], out["name"], out["source"]) == (TEMPLATE, data["name"], data["source"])
    assert (out["version"], out["editable"], out["basedOn"]) == (1, False, None)
    assert [w["number"] for w in out["weeks"]] == list(range(1, 9))

    expected = json_positions(data)
    focus = {(w["number"], d["weekday"]): d["focus"] for w in data["weeks"] for d in w["days"]}
    seen = set()
    for week in out["weeks"]:
        weekdays = [d["weekday"] for d in week["days"]]
        assert weekdays == sorted(weekdays) == [1, 3, 5]
        for day in week["days"]:
            assert isinstance(day["id"], int)
            assert day["title"] == WEEKDAY_TITLES[day["weekday"]]
            assert day["focus"] == focus[(week["number"], day["weekday"])]
            orders = [e["order"] for e in day["exercises"]]
            assert orders == sorted(orders)
            for e in day["exercises"]:
                assert isinstance(e["id"], int)
                assert set(e) == {"id", "name", "intensity", "order", "prescription"}
                assert set(e["prescription"]) == {"sets", "reps_min", "reps_max", "drop_reps", "raw"}
                src = expected[(week["number"], day["weekday"], e["order"])]
                assert (e["name"], e["intensity"], e["prescription"]) == (
                    src["name"],
                    src["intensity"],
                    src["prescription"],
                )
                seen.add((week["number"], day["weekday"], e["order"]))
    assert seen == set(expected) and len(seen) == 128


async def test_day_titles_are_russian_weekdays(client, auth):
    out = (await client.get(f"/api/programs/{TEMPLATE}", headers=auth)).json()
    assert [d["title"] for d in out["weeks"][0]["days"]] == ["понедельник", "среда", "пятница"]


async def test_get_unknown_program_404(client, auth):
    assert (await client.get("/api/programs/nope", headers=auth)).status_code == 404


# ---- 3: raw_prescription ----


async def test_raw_prescription_equals_legacy_formula_on_template(db, settings):
    def legacy(i) -> str:
        if i.drop_reps:
            return f"дропсет {i.sets}х {'-'.join(map(str, i.drop_reps))}"
        return f"{i.sets}х{i.reps_min}-{i.reps_max}"

    async with db() as s:
        program = await load_program(s, (await s.scalar(select(Program).where(Program.slug == TEMPLATE))).id)
        items = [i for w in program.weeks for d in w.days for i in d.items]
        assert len(items) == 128
        for i in items:
            assert raw_prescription(i) == legacy(i)


def test_raw_prescription_equal_reps_is_one_number():
    assert raw_prescription(SimpleNamespace(sets=4, reps_min=10, reps_max=10, drop_reps=None)) == "4х10"
    assert raw_prescription(ProgramItem(sets=4, reps_min=10, reps_max=10, drop_reps=None)) == "4х10"


def test_raw_prescription_dropset():
    item = SimpleNamespace(sets=3, reps_min=None, reps_max=None, drop_reps=[12, 6, 6])
    assert raw_prescription(item) == "дропсет 3х 12-6-6"
    assert raw_prescription(ProgramItem(sets=3, drop_reps=[12, 6, 6])) == "дропсет 3х 12-6-6"


def test_raw_prescription_range_and_missing_reps():
    assert raw_prescription(SimpleNamespace(sets=6, reps_min=8, reps_max=12, drop_reps=None)) == "6х8-12"
    assert raw_prescription(SimpleNamespace(sets=3, reps_min=None, reps_max=None, drop_reps=None)) == "3х"


# ---- 4: visibility ----


async def test_other_users_copy_is_invisible(db, client, auth):
    await client.get("/api/state", headers=auth)  # creates user 42
    other_id = await add_user(db, OTHER)
    async with db() as s:
        copy = await make_copy(s, TEMPLATE, other_id, "copy-777", "Копия 777")
        await give_unique_exercise(s, copy, "уникальное упражнение")
        await s.commit()

    r = await client.get("/api/programs", headers=auth)
    assert [p["id"] for p in r.json()] == [TEMPLATE]
    assert (await client.get("/api/programs/copy-777", headers=auth)).status_code == 404
    assert (await client.put("/api/settings", json={"programId": "copy-777"}, headers=auth)).status_code == 404

    async with db() as s:
        user42 = await get_or_create_user(s, 42)
        with pytest.raises(LookupError):
            await set_program(s, user42, "copy-777", date(2026, 10, 5))
        snap = await chat_settings.load_snapshot(s, user42, date(2026, 10, 8))
        assert [p.slug for p in snap.programs] == [TEMPLATE]
        assert "уникальное упражнение" not in await baselines.catalog(s, user42.id)
        assert "уникальное упражнение" not in snap.catalog
    # The program the 404 rejected was not switched to.
    assert (await client.get("/api/state", headers=auth)).json()["programId"] == TEMPLATE


async def test_own_copy_is_visible_and_editable(db, client, auth):
    await client.get("/api/state", headers=auth)
    async with db() as s:
        user42 = await get_or_create_user(s, 42)
        copy = await make_copy(s, TEMPLATE, user42.id, "mine", "Моя программа")
        await give_unique_exercise(s, copy, "уникальное упражнение")
        await s.commit()
        uid = user42.id

    listed = {p["id"]: p for p in (await client.get("/api/programs", headers=auth)).json()}
    assert set(listed) == {TEMPLATE, "mine"}
    assert (listed["mine"]["editable"], listed["mine"]["basedOn"]) == (True, TEMPLATE)
    assert (listed[TEMPLATE]["editable"], listed[TEMPLATE]["basedOn"]) == (False, None)
    r = await client.get("/api/programs/mine", headers=auth)
    assert r.status_code == 200
    assert (r.json()["editable"], r.json()["basedOn"], r.json()["name"]) == (True, TEMPLATE, "Моя программа")

    async with db() as s:
        assert "уникальное упражнение" in await baselines.catalog(s, uid)
        assert "уникальное упражнение" not in await baselines.catalog(s, None)
        user42 = await get_or_create_user(s, 42)
        snap = await chat_settings.load_snapshot(s, user42, date(2026, 10, 8))
        assert {p.slug for p in snap.programs} == {TEMPLATE, "mine"}


async def test_other_user_still_does_not_see_my_copy(db, client, auth, other_auth):
    await client.get("/api/state", headers=auth)
    async with db() as s:
        user42 = await get_or_create_user(s, 42)
        await make_copy(s, TEMPLATE, user42.id, "mine", "Моя программа")
        await s.commit()
    assert [p["id"] for p in (await client.get("/api/programs", headers=other_auth)).json()] == [TEMPLATE]
    assert (await client.get("/api/programs/mine", headers=other_auth)).status_code == 404


# ---- 5: default program ----


async def test_active_program_fallback_is_a_template_not_a_copy(db):
    other_id = await add_user(db, OTHER)
    async with db() as s:
        copy = await make_copy(s, TEMPLATE, other_id, "copy-777", "Копия 777", program_id=0)
        template = await s.scalar(select(Program).where(Program.slug == TEMPLATE))
        assert copy.id < template.id  # the copy would win a plain "first program" pick
        await s.commit()
    async with db() as s:
        newcomer = await get_or_create_user(s, 555)
        up = await active_program(s, newcomer, date(2026, 10, 8))
        assert up.program.slug == TEMPLATE
        assert up.program.owner_user_id is None


# ---- 6: programVersion ----


async def test_state_program_version(client, auth, db):
    st = (await client.get("/api/state", headers=auth)).json()
    assert st["programVersion"] == 1 and isinstance(st["programVersion"], int)
    async with db() as s:
        await s.execute(update(Program).where(Program.slug == TEMPLATE).values(version=3))
        await s.commit()
    assert (await client.get("/api/state", headers=auth)).json()["programVersion"] == 3


# ---- 7: exercise catalog ----


async def test_exercises_catalog_counts_only_own_sets(client, auth, other_auth, settings):
    data = template_json(settings)
    template_names = {e["name"] for w in data["weeks"] for d in w["days"] for e in d["exercises"]}
    first = data["weeks"][0]["days"][0]["exercises"][0]["name"]

    r = await client.get("/api/exercises", headers=auth)
    assert r.status_code == 200
    rows = r.json()
    assert all(set(x) == {"name", "sets"} for x in rows)
    assert {x["name"] for x in rows} == template_names
    assert all(x["sets"] == 0 for x in rows)

    posted = workout("w42", exercises=[ex_payload(first, 3), ex_payload(first, 1, done=False), ex_payload("мой новый жим", 2)])
    assert (await client.post("/api/workouts", json=posted, headers=auth)).status_code == 200
    other = workout(
        "w777", exercises=[ex_payload(first, 5), ex_payload("чужое упражнение", 4)]
    )
    assert (await client.post("/api/workouts", json=other, headers=other_auth)).status_code == 200

    rows = (await client.get("/api/exercises", headers=auth)).json()
    assert rows[0] == {"name": first, "sets": 3}
    assert rows[1] == {"name": "мой новый жим", "sets": 2}
    assert "чужое упражнение" not in {x["name"] for x in rows}
    rest = rows[2:]
    assert all(x["sets"] == 0 for x in rest)
    assert [x["name"] for x in rest] == sorted(x["name"] for x in rest)
    assert {x["name"] for x in rows} == template_names | {"мой новый жим"}

    theirs = (await client.get("/api/exercises", headers=other_auth)).json()
    assert theirs[0] == {"name": first, "sets": 5}
    assert theirs[1] == {"name": "чужое упражнение", "sets": 4}
    assert "мой новый жим" not in {x["name"] for x in theirs}


# ---- 8: snapshot of targets ----


async def test_workout_snapshot_survives_program_edits(client, auth, db, settings):
    data = template_json(settings)
    day_json = data["weeks"][0]["days"][0]
    exs = sorted(day_json["exercises"], key=lambda e: e["order"])
    assert len(exs) == 6
    day_ids = await template_day_ids(client, auth)

    body = workout("snap", week=1, weekday=1, exercises=[ex_payload(e["name"]) for e in exs])
    r = await client.post("/api/workouts", json=body, headers=auth)
    assert r.status_code == 200, r.text
    assert r.json()["programDayId"] == day_ids[(1, 1)]
    expected = [(e["name"], e["prescription"]["raw"], bool(e["prescription"]["drop_reps"])) for e in exs]
    assert [(x["name"], x["target"], x["dropset"]) for x in r.json()["exercises"]] == expected

    async with db() as s:
        w = (await s.scalars(select(Workout))).one()
        snapshot = json.loads(w.targets_json)
        ids = {e.name: e.id for e in (await s.scalars(select(Exercise))).all()}
        assert snapshot == [
            {"exerciseId": ids[name], "target": target, "dropset": dropset} for name, target, dropset in expected
        ]
        day = await s.get(ProgramDay, day_ids[(1, 1)])
        items = (await s.scalars(select(ProgramItem).where(ProgramItem.day_id == day.id).order_by(ProgramItem.order))).all()
        assert len(items) == 6
        items[0].sets = 9  # a plain item
        items[2].drop_reps = [10, 5]  # a plain item becomes a dropset
        items[3].drop_reps = [8, 4, 4, 4]  # a dropset changes
        await s.commit()

    history = (await client.get("/api/state", headers=auth)).json()["history"]
    (h,) = history
    assert [(x["name"], x["target"], x["dropset"]) for x in h["exercises"]] == expected
    assert h["programDayId"] == day_ids[(1, 1)]
    assert (h["week"], h["weekday"], h["programId"]) == (1, 1, TEMPLATE)


async def test_chat_workout_has_no_program_day(client, auth, db):
    await client.get("/api/state", headers=auth)
    async with db() as s:
        user = await get_or_create_user(s, 42)
        parsed = ParseResult.model_validate(
            {"kind": "workout", "exercises": [{"exercise": "жим лёжа", "sets": [{"reps": 8, "weight_kg": 60}]}]}
        )
        await ws.save_from_chat(s, user, parsed, "жим 60 на 8", date(2026, 10, 5))
        await s.commit()
    (h,) = (await client.get("/api/state", headers=auth)).json()["history"]
    assert h["source"] == "chat" and h["programDayId"] is None
    assert h["programId"] == TEMPLATE
    assert [x["target"] for x in h["exercises"]] == [""]


# ---- 9: programDayId in POST /api/workouts ----


async def test_program_day_id_wins_over_week_and_weekday(client, auth):
    day_ids = await template_day_ids(client, auth)
    body = workout("a", week=1, weekday=1, programDayId=day_ids[(2, 3)])
    r = await client.post("/api/workouts", json=body, headers=auth)
    assert r.status_code == 200, r.text
    assert (r.json()["week"], r.json()["weekday"], r.json()["programDayId"]) == (2, 3, day_ids[(2, 3)])
    (h,) = (await client.get("/api/state", headers=auth)).json()["history"]
    assert (h["week"], h["weekday"], h["programDayId"]) == (2, 3, day_ids[(2, 3)])


async def switch_to_own_copy(db, client, auth) -> int:
    """User 42 now runs their own copy "mine"; returns the user id."""
    await client.get("/api/state", headers=auth)
    async with db() as s:
        user = await get_or_create_user(s, 42)
        await make_copy(s, TEMPLATE, user.id, "mine", "Моя программа")
        await s.commit()
        uid = user.id
    r = await client.put("/api/settings", json={"programId": "mine"}, headers=auth)
    assert r.status_code == 200 and r.json()["programId"] == "mine"
    return uid


async def test_template_day_id_lands_on_the_copys_day(client, auth, db):
    day_ids = await template_day_ids(client, auth)
    await switch_to_own_copy(db, client, auth)
    body = workout("b", programId=TEMPLATE, week=1, weekday=1, programDayId=day_ids[(3, 5)])
    r = await client.post("/api/workouts", json=body, headers=auth)
    assert r.status_code == 200, r.text

    async with db() as s:
        w = (await s.scalars(select(Workout))).one()
        day = await s.get(ProgramDay, w.program_day_id)
        week = await s.get(ProgramWeek, day.week_id)
        program = await s.get(Program, week.program_id)
        assert program.slug == "mine"
        assert day.base_day_id == day_ids[(3, 5)]
        assert (week.number, day.weekday) == (3, 5)
    (h,) = (await client.get("/api/state", headers=auth)).json()["history"]
    assert (h["programId"], h["week"], h["weekday"]) == ("mine", 3, 5)
    assert h["programDayId"] == w.program_day_id


async def test_template_slug_without_day_id_resolves_in_active_copy(client, auth, db):
    await switch_to_own_copy(db, client, auth)
    r = await client.post("/api/workouts", json=workout("c", programId=TEMPLATE, week=1, weekday=5), headers=auth)
    assert r.status_code == 200, r.text
    async with db() as s:
        w = (await s.scalars(select(Workout))).one()
        day = await s.get(ProgramDay, w.program_day_id)
        week = await s.get(ProgramWeek, day.week_id)
        program = await s.get(Program, week.program_id)
        assert (program.slug, week.number, day.weekday) == ("mine", 1, 5)
    (h,) = (await client.get("/api/state", headers=auth)).json()["history"]
    assert (h["programId"], h["week"], h["weekday"]) == ("mine", 1, 5)


async def test_day_of_another_users_copy_is_not_used(client, auth, db):
    await client.get("/api/state", headers=auth)
    other_id = await add_user(db, OTHER)
    async with db() as s:
        copy = await make_copy(s, TEMPLATE, other_id, "copy-777", "Копия 777")
        foreign_day = (await load_program(s, copy.id)).weeks[0].days[0].id
        await s.commit()
    body = workout("d", programId="nope", week=5, weekday=2, programDayId=foreign_day)
    r = await client.post("/api/workouts", json=body, headers=auth)
    assert r.status_code == 200, r.text
    assert r.json()["programDayId"] is None

    async with db() as s:
        w = (await s.scalars(select(Workout))).one()
        assert w.program_day_id is None and w.targets_json is None
        performed = w.performed_on
    start = date.fromisoformat((await client.get("/api/state", headers=auth)).json()["startDate"])
    days = (performed - start).days
    (h,) = (await client.get("/api/state", headers=auth)).json()["history"]
    assert h["programDayId"] is None and h["programId"] == TEMPLATE
    assert (h["week"], h["weekday"]) == (min(max(days // 7 + 1, 1), 8), performed.isoweekday())


# ---- 10: backfill of day focus ----


async def test_backfill_program_meta_restores_template_focus_only(db, settings):
    data = template_json(settings)
    focus = {(w["number"], d["weekday"]): d["focus"] for w in data["weeks"] for d in w["days"]}
    other_id = await add_user(db, OTHER)
    async with db() as s:
        await make_copy(s, TEMPLATE, other_id, "copy-777", "Копия 777")
        await s.execute(update(ProgramDay).values(focus=None))  # template and copy alike
        await s.commit()

    async with db() as s:
        assert await backfill_program_meta(s, settings.programs_dir) == 24
    async with db() as s:
        template = await load_program(s, (await s.scalar(select(Program).where(Program.slug == TEMPLATE))).id)
        got = {(w.number, d.weekday): d.focus for w in template.weeks for d in w.days}
        assert got == focus
        copy = await load_program(s, (await s.scalar(select(Program).where(Program.slug == "copy-777"))).id)
        assert all(d.focus is None for w in copy.weeks for d in w.days)
    async with db() as s:
        assert await backfill_program_meta(s, settings.programs_dir) == 0


# ---- 11: live topics ----


def test_program_is_a_live_topic():
    assert "program" in live.TOPICS
