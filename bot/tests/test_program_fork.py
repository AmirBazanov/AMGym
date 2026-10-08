"""Program editor, copy on first edit: the first PATCH of the active template makes the user's own copy,
re-links the current cycle's workouts and keeps the history the Mini App shows (golden test). Also the
phase-1 review fixes around copies: sync_programs, the deload JSON, programDayId of shared template days.

Dates are always derived from today (the suite also runs with a shifted clock)."""

import json
import shutil
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from conftest import init_data, make_settings
from sqlalchemy import func, select, update

from gymbot.db.models import (
    ActiveWorkout,
    Exercise,
    Program,
    ProgramDay,
    ProgramWeek,
    User,
    UserProgram,
    Workout,
    WorkoutSet,
)
from gymbot.services import baselines, deload
from gymbot.services import next_weights as nw
from gymbot.services.programs import load_program, monday_of, sync_programs

TEMPLATE = "arms_specialization_8w"
OTHER = 777


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path, allowed_user_ids=[42, OTHER])


def today(settings) -> date:
    return datetime.now(ZoneInfo(settings.timezone)).date()


async def program(client, auth, slug: str) -> dict:
    r = await client.get(f"/api/programs/{slug}", headers=auth)
    assert r.status_code == 200, r.text
    return r.json()


def day_of(p: dict, week: int, weekday: int) -> dict:
    w = next(w for w in p["weeks"] if w["number"] == week)
    return next(d for d in w["days"] if d["weekday"] == weekday)


def body(wid: str, p: dict, week: int, weekday: int, on: date, *, with_day_id: bool = True) -> dict:
    day = day_of(p, week, weekday)
    out = {
        "id": wid,
        "programId": p["id"],
        "week": week,
        "weekday": weekday,
        "startedAt": f"{on.isoformat()}T09:00:00Z",
        "finishedAt": f"{on.isoformat()}T10:00:00Z",
        "exercises": [
            {"name": e["name"], "target": "x", "dropset": False, "sets": [{"weight": 20 + k, "reps": 10}]}
            for k, e in enumerate(day["exercises"][:3])
        ],
    }
    if with_day_id:
        out["programDayId"] = day["id"]
    return out


async def start_program(client, auth, started_on: date) -> dict:
    r = await client.put("/api/settings", json={"programId": TEMPLATE, "startDate": started_on.isoformat()},
                         headers=auth)
    assert r.status_code == 200, r.text
    return r.json()


async def first_edit(client, auth, p: dict, **over) -> dict:
    """A harmless first edit far ahead (week 8 Friday, same prescription): makes the copy."""
    item = day_of(p, 8, 5)["exercises"][0]
    rx = item["prescription"]
    op = {"op": "prescribe", "week": 8, "weekday": 5, "itemId": item["id"], "sets": rx["sets"],
          "repsMin": rx["reps_min"], "repsMax": rx["reps_max"], "dropReps": rx["drop_reps"]}
    r = await client.patch(f"/api/programs/{p['id']}", json={"version": p["version"], "ops": [op], **over},
                           headers=auth)
    assert r.status_code == 200, r.text
    return r.json()


async def seed(client, auth, db, settings) -> tuple[date, dict]:
    """A cycle started two weeks ago with workouts in it, one workout of the previous cycle, a chat workout
    and a workout in progress prepared from the template."""
    started = monday_of(today(settings)) - timedelta(days=14)
    await start_program(client, auth, started)
    p = await program(client, auth, TEMPLATE)
    posts = [
        body("old", p, 1, 1, started - timedelta(days=7)),  # previous cycle: stays on the template
        body("w1", p, 1, 1, started),
        body("w2", p, 1, 3, started + timedelta(days=2), with_day_id=False),  # slug + week + weekday
        body("w3", p, 1, 5, started + timedelta(days=4)),
        body("w4", p, 2, 1, started + timedelta(days=7)),
        body("w5", p, 2, 3, started + timedelta(days=9)),
    ]
    for b in posts:
        r = await client.post("/api/workouts", json=b, headers=auth)
        assert r.status_code == 200, r.text
    async with db() as s:  # a chat workout: no planned day, placed by date in the active program
        user = await s.scalar(select(User).where(User.telegram_id == 42))
        ex = await s.scalar(select(Exercise).order_by(Exercise.id).limit(1))
        on = started + timedelta(days=8)
        w = Workout(user_id=user.id, performed_on=on, source="chat",
                    started_at=datetime(on.year, on.month, on.day, 9, tzinfo=UTC),
                    finished_at=datetime(on.year, on.month, on.day, 10, tzinfo=UTC))
        w.sets = [WorkoutSet(exercise_id=ex.id, set_index=0, reps=8, weight_kg=40, raw_text="чат")]
        s.add(w)
        await s.commit()
    active = body("live", p, 3, 1, today(settings))
    active["exercises"][0]["sets"].append({"weight": None, "reps": None, "done": False})
    r = await client.put("/api/workouts/active", json=active, headers=auth)
    assert r.status_code == 204, r.text
    return started, p


async def state(client, auth) -> dict:
    r = await client.get("/api/state", headers=auth)
    assert r.status_code == 200, r.text
    return r.json()


def without_program_refs(history: list[dict]) -> list[dict]:
    return [{k: v for k, v in h.items() if k not in ("programId", "programDayId")} for h in history]


async def test_golden_history_is_the_same_after_the_copy(client, auth, db, settings):
    started, template = await seed(client, auth, db, settings)
    before = await state(client, auth)
    assert before["programId"] == TEMPLATE
    assert len(before["history"]) == 7

    out = await first_edit(client, auth, template)
    copy_slug = out["program"]["id"]
    assert out["switchedFrom"] == TEMPLATE

    after = await state(client, auth)
    assert after["programId"] == copy_slug
    assert after["startDate"] == before["startDate"] == started.isoformat()
    # Golden: everything but the program references is identical (targets come from the snapshots).
    assert without_program_refs(after["history"]) == without_program_refs(before["history"])

    by_client = {h["clientId"] or h["id"]: h for h in after["history"]}
    old_by_client = {h["clientId"] or h["id"]: h for h in before["history"]}
    async with db() as s:
        base_of = dict((await s.execute(select(ProgramDay.id, ProgramDay.base_day_id))).all())
    for key, h in by_client.items():
        was = old_by_client[key]
        if key == "old":  # the previous cycle stays on the template
            assert h["programId"] == TEMPLATE and h["programDayId"] == was["programDayId"]
        elif was["programDayId"] is None:  # the chat workout: placed by date, in the active program
            assert h["programDayId"] is None and h["programId"] == copy_slug
        else:  # re-linked to the copy's day made from the same template day
            assert h["programId"] == copy_slug
            assert h["programDayId"] != was["programDayId"]
            assert base_of[h["programDayId"]] == was["programDayId"]
            assert (h["week"], h["weekday"]) == (was["week"], was["weekday"])

    async with db() as s:  # the workout in progress now points at the copy and its day
        row = await s.scalar(select(ActiveWorkout))
        payload = json.loads(row.payload)
        assert payload["programId"] == copy_slug
        assert base_of[payload["programDayId"]] == day_of(template, 3, 1)["id"]


async def test_copy_row_user_program_and_untouched_template(client, auth, db, settings):
    started = monday_of(today(settings)) - timedelta(days=7)
    await start_program(client, auth, started)
    template_before = await program(client, auth, TEMPLATE)

    out = await first_edit(client, auth, template_before)

    assert await program(client, auth, TEMPLATE) == template_before  # the template never changes
    async with db() as s:
        user = await s.scalar(select(User).where(User.telegram_id == 42))
        copy = await s.scalar(select(Program).where(Program.owner_user_id == user.id))
        tpl = await s.scalar(select(Program).where(Program.slug == TEMPLATE))
        assert copy.slug == f"{TEMPLATE}.u{user.id}" == out["program"]["id"]
        assert copy.name == f"{tpl.name} · моя"
        assert (copy.version, copy.based_on_id, tpl.version) == (1, tpl.id, 1)
        ups = (await s.scalars(select(UserProgram).where(UserProgram.user_id == user.id)
                               .order_by(UserProgram.id))).all()
        assert [u.program_id for u in ups[-2:]] == [tpl.id, copy.id]
        assert ups[-1].started_on == ups[-2].started_on == started
        loaded = await load_program(s, copy.id)
        assert all(d.base_day_id is not None for w in loaded.weeks for d in w.days)
    p = out["program"]
    assert (p["editable"], p["basedOn"], p["version"]) == (True, TEMPLATE, 1)
    # Same structure, names, prescriptions and focus; only the ids differ.
    strip = lambda prog: [
        (w["number"], d["weekday"], d["focus"], [(e["name"], e["order"], e["intensity"], e["prescription"])
                                                 for e in d["exercises"]])
        for w in prog["weeks"] for d in w["days"]
    ]
    assert strip(p) == strip(template_before)


async def test_second_copy_gets_numbered_slug_and_name(client, auth, db, settings):
    await start_program(client, auth, monday_of(today(settings)))
    first = await first_edit(client, auth, await program(client, auth, TEMPLATE))
    await start_program(client, auth, monday_of(today(settings)))  # back to the template
    second = await first_edit(client, auth, await program(client, auth, TEMPLATE))
    assert second["program"]["id"] == first["program"]["id"] + "-2"
    assert second["program"]["name"].endswith(" · моя 2")


async def test_dry_run_on_the_template_makes_no_copy(client, auth, db, settings):
    _, template = await seed(client, auth, db, settings)
    before = await state(client, auth)
    out = await first_edit(client, auth, template, dryRun=True)
    assert out["switchedFrom"] == TEMPLATE
    assert out["program"]["id"] == TEMPLATE  # the stored program, unchanged
    assert out["results"] == [{"op": 0, "weeks": [8], "skipped": []}]
    assert await state(client, auth) == before
    async with db() as s:
        assert await s.scalar(select(func.count(Program.id))) == 1
        payload = json.loads((await s.scalar(select(ActiveWorkout))).payload)
        assert payload["programId"] == TEMPLATE


async def test_copy_freezes_targets_of_rows_without_a_snapshot(client, auth, db, settings):
    _, template = await seed(client, auth, db, settings)
    async with db() as s:
        await s.execute(update(Workout).values(targets_json=None))
        await s.commit()
    before = await state(client, auth)
    out = await first_edit(client, auth, template)
    copy = out["program"]
    # Flatten the copy's prescriptions on every week: the re-linked rows must not follow them.
    ops = [
        {"op": "prescribe", "week": w["number"], "weekday": d["weekday"], "itemId": e["id"], "sets": 1,
         "repsMin": 5}
        for w in copy["weeks"][:2] for d in w["days"] for e in d["exercises"]
    ]
    r = await client.patch(f"/api/programs/{copy['id']}", json={"version": 1, "ops": ops}, headers=auth)
    assert r.status_code == 200, r.text
    assert without_program_refs((await state(client, auth))["history"]) == without_program_refs(before["history"])


async def test_copy_works_for_next_weights_and_catalog(client, auth, db, settings):
    started = monday_of(today(settings))
    await start_program(client, auth, started)
    template = await program(client, auth, TEMPLATE)
    async with db() as s:
        user = await s.scalar(select(User).where(User.telegram_id == 42))
        ref = await nw.program_ref(s, user, settings)
        names_before = nw.program_day(ref, started)[2]
    out = await first_edit(client, auth, template)
    monday = day_of(out["program"], 1, 1)
    r = await client.patch(
        f"/api/programs/{out['program']['id']}",
        json={"version": 1, "ops": [{"op": "replace", "week": 1, "weekday": 1, "itemId": monday["exercises"][0]["id"],
                                     "name": "Молотки на скамье"}]},
        headers=auth,
    )
    assert r.status_code == 200, r.text
    async with db() as s:
        user = await s.scalar(select(User).where(User.telegram_id == 42))
        ref = await nw.program_ref(s, user, settings)
        assert ref.program.slug == out["program"]["id"]
        week, weekday, items = nw.program_day(ref, started)
        assert (week, weekday) == (1, 1)
        assert [i.name for i in items] == ["молотки на скамье", *[i.name for i in names_before[1:]]]
        other = await s.scalar(select(User).where(User.telegram_id == OTHER))
        assert "молотки на скамье" in await baselines.catalog(s, user.id)
        assert "молотки на скамье" not in await baselines.catalog(s, other.id if other else None)
        assert "молотки на скамье" not in await baselines.catalog(s, None)


async def test_template_day_id_with_two_copy_days_picks_the_closest(client, auth, db, settings):
    await start_program(client, auth, monday_of(today(settings)))
    template = await program(client, auth, TEMPLATE)
    out = await first_edit(client, auth, template)
    copy = out["program"]
    shared = day_of(template, 1, 1)["id"]
    async with db() as s:  # week 2 Monday of the copy now also claims week 1 Monday of the template
        await s.execute(update(ProgramDay).where(ProgramDay.id == day_of(copy, 2, 1)["id"]).values(base_day_id=shared))
        await s.commit()
    on = monday_of(today(settings))
    for wid, week in (("a", 2), ("b", 1)):
        b = body(wid, template, 1, 1, on)
        b["week"] = week
        r = await client.post("/api/workouts", json=b, headers=auth)
        assert r.status_code == 200, r.text
        assert r.json()["programDayId"] == day_of(copy, week, 1)["id"]
        assert r.json()["programId"] == copy["id"]


async def test_sync_programs_never_takes_a_copy_for_a_template(db, settings, tmp_path, caplog):
    async with db() as s:
        user = User(telegram_id=5, rest_seconds=90)
        s.add(user)
        await s.flush()
        s.add(Program(slug="clash.u1", name="моя", owner_user_id=user.id))
        await s.commit()
    folder = tmp_path / "programs"
    folder.mkdir()
    src = settings.programs_dir / f"{TEMPLATE}.json"
    shutil.copy(src, folder / f"{TEMPLATE}.json")
    shutil.copy(src, folder / "clash.u1.json")
    shutil.copy(src, folder / "fresh.json")
    async with db() as s:
        await sync_programs(s, folder)
        rows = dict((await s.execute(select(Program.slug, Program.owner_user_id))).all())
        weeks = await s.scalar(
            select(func.count(ProgramWeek.id)).join(Program).where(Program.slug == "clash.u1")
        )
    assert rows[TEMPLATE] is None and rows["fresh"] is None  # the template kept, a new file imported
    assert rows["clash.u1"] is not None and weeks == 0  # the copy untouched, the clashing file skipped
    assert "clash.u1.json" in caplog.text


async def test_deload_weeks_of_a_copy_come_from_the_template_json(client, auth, db, settings, tmp_path):
    await start_program(client, auth, monday_of(today(settings)))
    out = await first_edit(client, auth, await program(client, auth, TEMPLATE))
    folder = tmp_path / "deload"
    folder.mkdir()
    data = json.loads((settings.programs_dir / f"{TEMPLATE}.json").read_text(encoding="utf-8"))
    data["weeks"][4]["deload"] = True  # week 5
    (folder / f"{TEMPLATE}.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    async with db() as s:
        copy = await load_program(s, await s.scalar(select(Program.id).where(Program.slug == out["program"]["id"])))
        tpl = await load_program(s, await s.scalar(select(Program.id).where(Program.slug == TEMPLATE)))
        assert await deload.template_slug(s, copy) == TEMPLATE
        assert await deload.template_slug(s, tpl) == TEMPLATE
        assert deload.program_deload_weeks(copy, folder, await deload.template_slug(s, copy)) == {5}
        assert deload.program_deload_weeks(copy, folder) == set()  # a copy has no JSON of its own
        assert deload.program_deload_weeks(tpl, folder) == {5}


async def test_other_user_cannot_patch_my_copy(client, auth, db, settings):
    await start_program(client, auth, monday_of(today(settings)))
    out = await first_edit(client, auth, await program(client, auth, TEMPLATE))
    other = {"X-Telegram-Init-Data": init_data(OTHER)}
    r = await client.patch(f"/api/programs/{out['program']['id']}", json={"version": 1, "ops": [
        {"op": "remove", "week": 1, "weekday": 1, "itemId": day_of(out["program"], 1, 1)["exercises"][0]["id"]}
    ]}, headers=other)
    assert r.status_code == 404
