"""PATCH /api/programs/{slug}: the program editor's day edits and the copy made on the first edit (phase 2).

Program under test: arms_specialization_8w (8 weeks, days weekday 1, 3, 5). Monday has 6 exercises in every week;
its 4th is «жим гантелей сидя» in weeks 1-3 and «отведения гантелей на переднюю дельту» in weeks 4-8. Wednesday's
first exercise is «жим лёжа 30°» in weeks 3-5 and «жим лёжа» in weeks 1, 2, 6, 7, 8. Dates are never hard-coded:
they come from /api/state `startDate` (the suite also runs with a shifted clock)."""

from datetime import date, timedelta

import pytest
import pytest_asyncio
from conftest import init_data, make_settings
from sqlalchemy import delete, func, select

from gymbot.db.models import Exercise, Program, ProgramDay, ProgramItem, ProgramWeek
from gymbot.services import live
from gymbot.services import next_weights as nw
from gymbot.services.users import get_or_create_user

TEMPLATE = "arms_specialization_8w"
OTHER = 777
ALL_WEEKS = list(range(1, 9))

DELTS = "отведения на дельты"
FRONT_RAISE = "отведения гантелей на переднюю дельту"
DB_PRESS = "жим гантелей сидя"
REAR_DELT = "отведения пек дек на заднюю дельту"
BENCH = "жим лёжа"
MONDAY_WEEK1 = [
    "сгибания с гантелями на бицепс с супинацией",
    "сгибания с гантелями на бицепс с пронацией",
    "французский жим в блоке из-за головы",
    DB_PRESS,
    DELTS,
    REAR_DELT,
]


@pytest.fixture
def settings(tmp_path):
    """Both users are allowed: with an empty allowlist only the first user in `users` is let in."""
    return make_settings(tmp_path, allowed_user_ids=[42, OTHER])


@pytest.fixture
def other_auth():
    return {"X-Telegram-Init-Data": init_data(OTHER)}


# ---- helpers ----


async def get_program(client, auth, slug=TEMPLATE) -> dict:
    r = await client.get(f"/api/programs/{slug}", headers=auth)
    assert r.status_code == 200, r.text
    return r.json()


def day_of(program: dict, week: int, weekday: int) -> dict:
    (w,) = [w for w in program["weeks"] if w["number"] == week]
    (d,) = [d for d in w["days"] if d["weekday"] == weekday]
    return d


def names(program: dict, week: int, weekday: int) -> list[str]:
    return [e["name"] for e in day_of(program, week, weekday)["exercises"]]


def item_of(program: dict, week: int, weekday: int, name: str) -> dict:
    (e,) = [e for e in day_of(program, week, weekday)["exercises"] if e["name"] == name]
    return e


def item_id(program: dict, week: int, weekday: int, name: str) -> int:
    return item_of(program, week, weekday, name)["id"]


def signature(program: dict) -> dict:
    """(week, weekday) -> exercises without ids: what the user sees."""
    return {
        (w["number"], d["weekday"]): [
            (e["name"], e["intensity"], e["order"], tuple(sorted(e["prescription"].items(), key=str)))
            for e in d["exercises"]
        ]
        for w in program["weeks"]
        for d in w["days"]
    }


def changed_days(before: dict, after: dict) -> set[tuple[int, int]]:
    a, b = signature(before), signature(after)
    assert a.keys() == b.keys()
    return {k for k in a if a[k] != b[k]}


async def patch(client, auth, slug, version, ops, **extra):
    return await client.patch(
        f"/api/programs/{slug}", json={"version": version, "ops": ops, **extra}, headers=auth
    )


async def ok(client, auth, slug, version, ops, **extra) -> dict:
    r = await patch(client, auth, slug, version, ops, **extra)
    assert r.status_code == 200, r.text
    return r.json()


def rx(sets=3, repsMin=10, **kw) -> dict:
    return {"sets": sets, "repsMin": repsMin, **kw}


async def make_copy(client, auth) -> dict:
    """The first PATCH on the template (a trivial prescription) makes user 42's copy; returns it."""
    template = await get_program(client, auth)
    first = template["weeks"][0]["days"][0]["exercises"][0]["id"]
    body = await ok(
        client, auth, TEMPLATE, 1,
        [{"op": "prescribe", "week": 1, "weekday": 1, "itemId": first, **rx(3, 10)}],
    )
    assert body["switchedFrom"] == TEMPLATE
    return body["program"]


@pytest_asyncio.fixture
async def copy(client, auth) -> dict:
    return await make_copy(client, auth)


async def program_count(client, auth) -> int:
    return len((await client.get("/api/programs", headers=auth)).json())


async def state(client, auth) -> dict:
    return (await client.get("/api/state", headers=auth)).json()


async def exercise_count(db) -> int:
    async with db() as s:
        return await s.scalar(select(func.count(Exercise.id)))


async def day_count(db) -> int:
    async with db() as s:
        return await s.scalar(select(func.count(ProgramDay.id)))


def skipped(result: dict) -> dict[int, str]:
    return {x["week"]: x["reason"] for x in result["skipped"]}


# ---- 1: replace ----


async def test_replace_in_one_week_creates_exercise_and_leaves_other_weeks(client, auth, db):
    template = await get_program(client, auth)
    before = await exercise_count(db)
    target = item_of(template, 1, 1, DELTS)

    body = await ok(
        client, auth, TEMPLATE, 1,
        [{"op": "replace", "week": 1, "weekday": 1, "itemId": target["id"], "name": "Молотки с канатом"}],
    )

    program = body["program"]
    assert body["results"] == [{"op": 0, "weeks": [1], "skipped": []}]
    replaced = day_of(program, 1, 1)["exercises"][4]
    assert replaced["name"] == "молотки с канатом"
    assert (replaced["order"], replaced["prescription"]) == (target["order"], target["prescription"])
    assert names(program, 1, 1) == [*MONDAY_WEEK1[:4], "молотки с канатом", REAR_DELT]
    assert changed_days(template, program) == {(1, 1)}
    assert await exercise_count(db) == before + 1
    async with db() as s:
        assert await s.scalar(select(func.count(Exercise.id)).where(Exercise.name == "молотки с канатом")) == 1


async def test_replace_with_differently_spelled_existing_name_uses_the_canonical_exercise(client, auth, db):
    template = await get_program(client, auth)
    before = await exercise_count(db)
    body = await ok(
        client, auth, TEMPLATE, 1,
        [{"op": "replace", "week": 1, "weekday": 1, "itemId": item_id(template, 1, 1, DELTS), "name": "  ЖИМ  ЛЕЖА "}],
    )
    assert names(body["program"], 1, 1)[4] == BENCH
    assert await exercise_count(db) == before


async def test_replace_by_alias_uses_the_aliased_exercise(client, auth, db):
    async with db() as s:
        ex = await s.scalar(select(Exercise).where(Exercise.name == "румынская тяга"))
        ex.aliases = ["рдл"]
        await s.commit()
    template = await get_program(client, auth)
    before = await exercise_count(db)
    body = await ok(
        client, auth, TEMPLATE, 1,
        [{"op": "replace", "week": 1, "weekday": 1, "itemId": item_id(template, 1, 1, DELTS), "name": "РДЛ"}],
    )
    assert names(body["program"], 1, 1)[4] == "румынская тяга"
    assert await exercise_count(db) == before


# ---- 2, 3: replace over several weeks ----


async def test_replace_over_all_weeks_skips_weeks_without_that_exercise(client, auth):
    template = await get_program(client, auth)
    body = await ok(
        client, auth, TEMPLATE, 1,
        [{
            "op": "replace", "week": 1, "weekday": 3, "itemId": item_id(template, 1, 3, BENCH),
            "name": "жим в хаммере", "weeks": ALL_WEEKS,
        }],
    )
    (result,) = body["results"]
    assert result["weeks"] == [1, 2, 6, 7, 8]
    assert skipped(result) == {3: "нет этого упражнения", 4: "нет этого упражнения", 5: "нет этого упражнения"}
    program = body["program"]
    for week in (1, 2, 6, 7, 8):
        assert names(program, week, 3)[0] == "жим в хаммере"
    for week in (3, 4, 5):
        assert names(program, week, 3)[0] == "жим лёжа 30°"


async def test_replace_with_exercise_already_in_the_source_day_is_422(client, auth):
    template = await get_program(client, auth)
    r = await patch(
        client, auth, TEMPLATE, 1,
        [{"op": "replace", "week": 4, "weekday": 1, "itemId": item_id(template, 4, 1, DELTS), "name": FRONT_RAISE}],
    )
    assert r.status_code == 422
    assert isinstance(r.json()["detail"], str)
    assert await program_count(client, auth) == 1  # nothing was written, not even the copy


async def test_replace_skips_weeks_that_already_have_the_exercise(client, auth):
    template = await get_program(client, auth)
    body = await ok(
        client, auth, TEMPLATE, 1,
        [{
            "op": "replace", "week": 1, "weekday": 1, "itemId": item_id(template, 1, 1, DELTS),
            "name": FRONT_RAISE, "weeks": ALL_WEEKS,
        }],
    )
    (result,) = body["results"]
    assert result["weeks"] == [1, 2, 3]
    assert skipped(result) == {w: "упражнение уже есть в дне" for w in (4, 5, 6, 7, 8)}
    program = body["program"]
    for week in (1, 2, 3):
        assert names(program, week, 1)[4] == FRONT_RAISE
    for week in (4, 5, 6, 7, 8):
        assert names(program, week, 1) == names(template, week, 1)


# ---- 4: prescribe ----


async def test_prescribe_range_and_intensity(client, auth):
    template = await get_program(client, auth)
    target = item_id(template, 1, 1, DELTS)
    body = await ok(
        client, auth, TEMPLATE, 1,
        [{"op": "prescribe", "week": 1, "weekday": 1, "itemId": target, "sets": 5, "repsMin": 8, "repsMax": 12,
          "intensity": "heavy"}],
    )
    assert body["results"] == [{"op": 0, "weeks": [1], "skipped": []}]
    changed = item_of(body["program"], 1, 1, DELTS)
    assert changed["intensity"] == "heavy"
    assert changed["prescription"] == {
        "sets": 5, "reps_min": 8, "reps_max": 12, "drop_reps": None, "raw": "5х8-12",
    }
    assert changed_days(template, body["program"]) == {(1, 1)}


async def test_prescribe_without_reps_max_is_exactly_reps_min(client, auth):
    template = await get_program(client, auth)
    body = await ok(
        client, auth, TEMPLATE, 1,
        [{"op": "prescribe", "week": 1, "weekday": 1, "itemId": item_id(template, 1, 1, DELTS), **rx(4, 10)}],
    )
    prescription = item_of(body["program"], 1, 1, DELTS)["prescription"]
    assert prescription == {"sets": 4, "reps_min": 10, "reps_max": 10, "drop_reps": None, "raw": "4х10"}


async def test_prescribe_dropset(client, auth):
    template = await get_program(client, auth)
    body = await ok(
        client, auth, TEMPLATE, 1,
        [{"op": "prescribe", "week": 1, "weekday": 1, "itemId": item_id(template, 1, 1, DELTS),
          "sets": 3, "dropReps": [12, 6, 6]}],
    )
    prescription = item_of(body["program"], 1, 1, DELTS)["prescription"]
    assert prescription == {
        "sets": 3, "reps_min": None, "reps_max": None, "drop_reps": [12, 6, 6], "raw": "дропсет 3х 12-6-6",
    }


async def test_prescribe_dropset_replaces_a_plain_range_and_back(client, auth):
    copy = await make_copy(client, auth)
    target = item_id(copy, 1, 1, DELTS)
    mid = await ok(
        client, auth, copy["id"], 1,
        [{"op": "prescribe", "week": 1, "weekday": 1, "itemId": target, "sets": 2, "dropReps": [10, 5]}],
    )
    assert item_of(mid["program"], 1, 1, DELTS)["prescription"]["drop_reps"] == [10, 5]
    back = await ok(
        client, auth, copy["id"], 2,
        [{"op": "prescribe", "week": 1, "weekday": 1, "itemId": target, **rx(4, 9, repsMax=11)}],
    )
    assert item_of(back["program"], 1, 1, DELTS)["prescription"] == {
        "sets": 4, "reps_min": 9, "reps_max": 11, "drop_reps": None, "raw": "4х9-11",
    }


async def test_prescribe_without_intensity_keeps_it(client, auth):
    copy = await make_copy(client, auth)
    target = item_id(copy, 1, 1, DELTS)
    await ok(
        client, auth, copy["id"], 1,
        [{"op": "prescribe", "week": 1, "weekday": 1, "itemId": target, **rx(3, 8), "intensity": "light"}],
    )
    body = await ok(
        client, auth, copy["id"], 2, [{"op": "prescribe", "week": 1, "weekday": 1, "itemId": target, **rx(4, 8)}]
    )
    changed = item_of(body["program"], 1, 1, DELTS)
    assert (changed["intensity"], changed["prescription"]["sets"]) == ("light", 4)


@pytest.mark.parametrize(
    "bad",
    [
        {"sets": 0, "repsMin": 10},
        {"sets": 21, "repsMin": 10},
        {"sets": -1, "repsMin": 10},
        {"sets": 3, "repsMin": 0},
        {"sets": 3, "repsMin": 101},
        {"sets": 3, "repsMin": 12, "repsMax": 8},
        {"sets": 3, "repsMin": 8, "repsMax": 101},
        {"sets": 3, "dropReps": [12]},
        {"sets": 3, "dropReps": [12, 10, 8, 6, 5, 4]},
        {"sets": 3, "dropReps": [12, 0]},
        {"sets": 3, "dropReps": [12, 101]},
        {"sets": 3},
        {"sets": 3, "repsMin": 10, "intensity": "extreme"},
    ],
    ids=lambda b: str(b),
)
async def test_prescribe_invalid_is_422(client, auth, bad):
    template = await get_program(client, auth)
    op = {"op": "prescribe", "week": 1, "weekday": 1, "itemId": item_id(template, 1, 1, DELTS), **bad}
    r = await patch(client, auth, TEMPLATE, 1, [op])
    assert r.status_code == 422
    assert isinstance(r.json()["detail"], str)
    assert await program_count(client, auth) == 1


async def test_prescribe_over_all_weeks_is_absolute(client, auth):
    template = await get_program(client, auth)
    body = await ok(
        client, auth, TEMPLATE, 1,
        [{"op": "prescribe", "week": 1, "weekday": 1, "itemId": item_id(template, 1, 1, DELTS),
          "sets": 5, "repsMin": 15, "weeks": ALL_WEEKS}],
    )
    (result,) = body["results"]
    assert result["weeks"] == ALL_WEEKS and result["skipped"] == []
    for week in ALL_WEEKS:
        assert item_of(body["program"], week, 1, DELTS)["prescription"] == {
            "sets": 5, "reps_min": 15, "reps_max": 15, "drop_reps": None, "raw": "5х15",
        }
    assert changed_days(template, body["program"]) == {(w, 1) for w in ALL_WEEKS}


async def test_prescribe_over_weeks_skips_where_the_exercise_is_missing(client, auth):
    template = await get_program(client, auth)
    body = await ok(
        client, auth, TEMPLATE, 1,
        [{"op": "prescribe", "week": 1, "weekday": 3, "itemId": item_id(template, 1, 3, BENCH),
          "sets": 6, "repsMin": 3, "weeks": ALL_WEEKS}],
    )
    (result,) = body["results"]
    assert result["weeks"] == [1, 2, 6, 7, 8]
    assert skipped(result) == {w: "нет этого упражнения" for w in (3, 4, 5)}
    for week in (3, 4, 5):
        assert item_of(body["program"], week, 3, "жим лёжа 30°")["prescription"]["sets"] != 6


# ---- 5: add ----


async def test_add_reorder_and_prescribe_by_temp_id_in_one_request(client, auth):
    template = await get_program(client, auth)
    ids = [item_id(template, 1, 1, n) for n in MONDAY_WEEK1]
    # final order: the new one in the middle, the old ones reversed around it
    wanted = [ids[5], ids[4], ids[3], "t1", ids[2], ids[1], ids[0]]
    body = await ok(
        client, auth, TEMPLATE, 1,
        [
            {"op": "add", "week": 1, "weekday": 1, "tempId": "t1", "name": "Молотки с канатом", "position": 1,
             **rx(3, 10)},
            {"op": "reorder", "week": 1, "weekday": 1, "itemIds": wanted},
            {"op": "prescribe", "week": 1, "weekday": 1, "itemId": "t1", "sets": 5, "repsMin": 5, "repsMax": 8,
             "intensity": "heavy"},
        ],
    )
    assert [r["op"] for r in body["results"]] == [0, 1, 2]
    assert all(r["weeks"] == [1] and r["skipped"] == [] for r in body["results"])
    day = day_of(body["program"], 1, 1)
    assert [e["name"] for e in day["exercises"]] == [
        REAR_DELT, DELTS, DB_PRESS, "молотки с канатом", *MONDAY_WEEK1[2::-1],
    ]
    assert [e["order"] for e in day["exercises"]] == list(range(1, 8))
    new = item_of(body["program"], 1, 1, "молотки с канатом")
    assert new["intensity"] == "heavy"
    assert new["prescription"] == {"sets": 5, "reps_min": 5, "reps_max": 8, "drop_reps": None, "raw": "5х5-8"}


async def test_unknown_temp_id_is_422(client, auth):
    template = await get_program(client, auth)
    r = await patch(
        client, auth, TEMPLATE, 1,
        [{"op": "prescribe", "week": 1, "weekday": 1, "itemId": "ghost", **rx()}],
    )
    assert r.status_code == 422 and isinstance(r.json()["detail"], str)
    # a tempId of a later add is not known yet either
    r = await patch(
        client, auth, TEMPLATE, 1,
        [
            {"op": "remove", "week": 1, "weekday": 1, "itemId": "t1"},
            {"op": "add", "week": 1, "weekday": 1, "tempId": "t1", "name": "Молотки", "position": 1, **rx()},
        ],
    )
    assert r.status_code == 422
    assert await get_program(client, auth) == template


async def test_add_duplicate_temp_id_is_422(client, auth):
    add = {"op": "add", "week": 1, "weekday": 1, "position": 1, **rx()}
    r = await patch(
        client, auth, TEMPLATE, 1,
        [{**add, "tempId": "t", "name": "Молотки"}, {**add, "tempId": "t", "name": "Скручивания"}],
    )
    assert r.status_code == 422 and isinstance(r.json()["detail"], str)
    assert await program_count(client, auth) == 1


async def test_add_exercise_already_in_the_day_is_422(client, auth):
    r = await patch(
        client, auth, TEMPLATE, 1,
        [{"op": "add", "week": 1, "weekday": 1, "tempId": "t", "name": "Жим гантелей СИДЯ", "position": 1, **rx()}],
    )
    assert r.status_code == 422 and isinstance(r.json()["detail"], str)
    assert await program_count(client, auth) == 1


async def test_add_at_position_one_goes_first(client, auth):
    body = await ok(
        client, auth, TEMPLATE, 1,
        [{"op": "add", "week": 1, "weekday": 1, "tempId": "t", "name": "Молотки", "position": 1, **rx(3, 12)}],
    )
    day = day_of(body["program"], 1, 1)
    assert [e["name"] for e in day["exercises"]] == ["молотки", *MONDAY_WEEK1]
    assert [e["order"] for e in day["exercises"]] == list(range(1, 8))
    assert day["exercises"][0]["intensity"] is None
    assert day["exercises"][0]["prescription"]["raw"] == "3х12"


async def test_add_in_the_middle_and_past_the_end(client, auth):
    middle = await ok(
        client, auth, TEMPLATE, 1,
        [{"op": "add", "week": 1, "weekday": 1, "tempId": "t", "name": "Молотки", "position": 3, **rx()}],
    )
    assert names(middle["program"], 1, 1) == [*MONDAY_WEEK1[:2], "молотки", *MONDAY_WEEK1[2:]]
    copy = middle["program"]
    end = await ok(
        client, auth, copy["id"], 1,
        [{"op": "add", "week": 1, "weekday": 1, "tempId": "t", "name": "Скручивания", "position": 99, **rx()}],
    )
    day = day_of(end["program"], 1, 1)
    assert day["exercises"][-1]["name"] == "скручивания"
    assert [e["order"] for e in day["exercises"]] == list(range(1, 9))


async def test_add_position_below_one_is_422(client, auth):
    r = await patch(
        client, auth, TEMPLATE, 1,
        [{"op": "add", "week": 1, "weekday": 1, "tempId": "t", "name": "Молотки", "position": 0, **rx()}],
    )
    assert r.status_code == 422 and isinstance(r.json()["detail"], str)


async def test_add_over_weeks_skips_weeks_that_already_have_it(client, auth):
    template = await get_program(client, auth)
    body = await ok(
        client, auth, TEMPLATE, 1,
        [{"op": "add", "week": 1, "weekday": 1, "tempId": "t", "name": FRONT_RAISE, "position": 99,
          "weeks": ALL_WEEKS, **rx(3, 12)}],
    )
    (result,) = body["results"]
    assert result["weeks"] == [1, 2, 3]
    assert skipped(result) == {w: "упражнение уже есть в дне" for w in (4, 5, 6, 7, 8)}
    for week in (1, 2, 3):
        assert names(body["program"], week, 1)[-1] == FRONT_RAISE
    assert changed_days(template, body["program"]) == {(1, 1), (2, 1), (3, 1)}


async def test_add_beyond_twenty_exercises(client, auth):
    copy = await make_copy(client, auth)
    fill = [
        {"op": "add", "week": 2, "weekday": 1, "tempId": f"f{n}", "name": f"упражнение {n}", "position": 99, **rx()}
        for n in range(14)
    ]
    body = await ok(client, auth, copy["id"], 1, fill)
    assert len(names(body["program"], 2, 1)) == 20
    version = body["program"]["version"]

    over = {"op": "add", "week": 2, "weekday": 1, "tempId": "x", "name": "лишнее", "position": 99, **rx()}
    r = await patch(client, auth, copy["id"], version, [over])
    assert r.status_code == 422 and isinstance(r.json()["detail"], str)

    # as another week of a wider edit the full day is skipped, not an error
    wide = {**over, "week": 1, "weeks": [1, 2]}
    body = await ok(client, auth, copy["id"], version, [wide])
    (result,) = body["results"]
    assert result["weeks"] == [1]
    assert skipped(result) == {2: "в дне уже 20 упражнений"}
    assert len(names(body["program"], 2, 1)) == 20


# ---- 6: remove ----


async def test_remove_renumbers_the_day(client, auth):
    template = await get_program(client, auth)
    body = await ok(
        client, auth, TEMPLATE, 1,
        [{"op": "remove", "week": 1, "weekday": 1, "itemId": item_id(template, 1, 1, MONDAY_WEEK1[1])}],
    )
    assert body["results"] == [{"op": 0, "weeks": [1], "skipped": []}]
    day = day_of(body["program"], 1, 1)
    assert [e["name"] for e in day["exercises"]] == [MONDAY_WEEK1[0], *MONDAY_WEEK1[2:]]
    assert [e["order"] for e in day["exercises"]] == [1, 2, 3, 4, 5]
    assert changed_days(template, body["program"]) == {(1, 1)}


async def test_remove_the_last_exercise_of_a_day_is_422_and_rolls_everything_back(client, auth):
    copy = await make_copy(client, auth)
    before = await get_program(client, auth, copy["id"])
    wednesday = before["weeks"][0]["days"][1]
    assert wednesday["weekday"] == 3 and len(wednesday["exercises"]) == 4
    ops = [{"op": "remove", "week": 1, "weekday": 3, "itemId": e["id"]} for e in wednesday["exercises"]]

    r = await patch(client, auth, copy["id"], before["version"], ops)
    assert r.status_code == 422 and isinstance(r.json()["detail"], str)

    after = await get_program(client, auth, copy["id"])
    assert after == before  # the first three removals were rolled back, the version is not bumped
    assert after["version"] == before["version"]
    # three removals are fine and still leave a day
    body = await ok(client, auth, copy["id"], before["version"], ops[:3])
    assert len(names(body["program"], 1, 3)) == 1


async def test_failed_first_edit_on_the_template_makes_no_copy(client, auth):
    template = await get_program(client, auth)
    wednesday = template["weeks"][0]["days"][1]
    ops = [{"op": "remove", "week": 1, "weekday": 3, "itemId": e["id"]} for e in wednesday["exercises"]]
    r = await patch(client, auth, TEMPLATE, 1, ops)
    assert r.status_code == 422
    assert await program_count(client, auth) == 1
    assert (await state(client, auth))["programId"] == TEMPLATE
    assert await get_program(client, auth) == template


async def test_remove_over_weeks_skips_weeks_without_the_exercise(client, auth):
    template = await get_program(client, auth)
    body = await ok(
        client, auth, TEMPLATE, 1,
        [{"op": "remove", "week": 1, "weekday": 3, "itemId": item_id(template, 1, 3, BENCH), "weeks": ALL_WEEKS}],
    )
    (result,) = body["results"]
    assert result["weeks"] == [1, 2, 6, 7, 8]
    assert skipped(result) == {w: "нет этого упражнения" for w in (3, 4, 5)}
    for week in (1, 2, 6, 7, 8):
        assert len(names(body["program"], week, 3)) == 3
    for week in (3, 4, 5):
        assert len(names(body["program"], week, 3)) == 4


async def test_remove_skips_a_week_where_it_is_the_last_exercise(client, auth):
    copy = await make_copy(client, auth)
    week2 = day_of(copy, 2, 1)["exercises"]
    keep = week2[-1]
    assert keep["name"] == REAR_DELT
    others = [{"op": "remove", "week": 2, "weekday": 1, "itemId": e["id"]} for e in week2[:-1]]
    wide = {"op": "remove", "week": 1, "weekday": 1, "itemId": item_id(copy, 1, 1, REAR_DELT), "weeks": [1, 2]}
    body = await ok(client, auth, copy["id"], 1, [*others, wide])
    result = body["results"][-1]
    assert result["weeks"] == [1]
    assert skipped(result) == {2: "последнее упражнение дня"}
    assert names(body["program"], 2, 1) == [REAR_DELT]


async def test_edit_skips_a_week_that_has_no_such_day(client, auth, db):
    copy = await make_copy(client, auth)
    async with db() as s:
        gone = await s.scalar(
            select(ProgramDay.id)
            .join(ProgramWeek, ProgramWeek.id == ProgramDay.week_id)
            .join(Program, Program.id == ProgramWeek.program_id)
            .where(Program.slug == copy["id"], ProgramWeek.number == 2, ProgramDay.weekday == 3)
        )
        await s.execute(delete(ProgramItem).where(ProgramItem.day_id == gone))
        await s.execute(delete(ProgramDay).where(ProgramDay.id == gone))
        await s.commit()
    body = await ok(
        client, auth, copy["id"], 1,
        [{"op": "prescribe", "week": 1, "weekday": 3, "itemId": item_id(copy, 1, 3, BENCH),
          "sets": 2, "repsMin": 5, "weeks": [1, 2]}],
    )
    (result,) = body["results"]
    assert result["weeks"] == [1]
    assert skipped(result) == {2: "нет этого дня"}



# ---- 7: reorder ----


async def test_reorder_one_week(client, auth):
    template = await get_program(client, auth)
    ids = [item_id(template, 1, 1, n) for n in MONDAY_WEEK1]
    body = await ok(
        client, auth, TEMPLATE, 1,
        [{"op": "reorder", "week": 1, "weekday": 1, "itemIds": ids[::-1]}],
    )
    assert body["results"] == [{"op": 0, "weeks": [1], "skipped": []}]
    day = day_of(body["program"], 1, 1)
    assert [e["name"] for e in day["exercises"]] == MONDAY_WEEK1[::-1]
    assert [e["order"] for e in day["exercises"]] == [1, 2, 3, 4, 5, 6]
    assert changed_days(template, body["program"]) == {(1, 1)}


async def test_reorder_must_be_a_full_permutation(client, auth):
    template = await get_program(client, auth)
    ids = [item_id(template, 1, 1, n) for n in MONDAY_WEEK1]
    for bad in (ids[:-1], [*ids, ids[0]], [*ids[:-1], ids[0]], [], [*ids, 10**9]):
        r = await patch(client, auth, TEMPLATE, 1, [{"op": "reorder", "week": 1, "weekday": 1, "itemIds": bad}])
        assert r.status_code == 422, bad
        assert isinstance(r.json()["detail"], str)
    assert await program_count(client, auth) == 1
    assert await get_program(client, auth) == template


async def test_reorder_over_weeks_skips_weeks_with_another_set_of_exercises(client, auth):
    template = await get_program(client, auth)
    ids = [item_id(template, 1, 1, n) for n in MONDAY_WEEK1]
    body = await ok(
        client, auth, TEMPLATE, 1,
        [{"op": "reorder", "week": 1, "weekday": 1, "itemIds": ids[::-1], "weeks": ALL_WEEKS}],
    )
    (result,) = body["results"]
    assert result["weeks"] == [1, 2, 3]
    assert skipped(result) == {w: "другой набор упражнений" for w in (4, 5, 6, 7, 8)}
    for week in (1, 2, 3):
        assert names(body["program"], week, 1) == MONDAY_WEEK1[::-1]
    for week in (4, 5, 6, 7, 8):
        assert names(body["program"], week, 1) == names(template, week, 1)


# ---- 8: errors ----


def _ops_weeks_without_week(ids):
    return [{"op": "prescribe", "week": 1, "weekday": 1, "itemId": ids["mon"], **rx(), "weeks": [2, 3]}]


def _ops_week_99(ids):
    return [{"op": "prescribe", "week": 99, "weekday": 1, "itemId": ids["mon"], **rx()}]


def _ops_week_0(ids):
    return [{"op": "prescribe", "week": 0, "weekday": 1, "itemId": ids["mon"], **rx()}]


def _ops_weeks_contains_unknown_week(ids):
    return [{"op": "prescribe", "week": 1, "weekday": 1, "itemId": ids["mon"], **rx(), "weeks": [1, 99]}]


def _ops_weekday_without_training(ids):
    return [{"op": "prescribe", "week": 1, "weekday": 2, "itemId": ids["mon"], **rx()}]


def _ops_weekday_out_of_range(ids):
    return [{"op": "prescribe", "week": 1, "weekday": 8, "itemId": ids["mon"], **rx()}]


def _ops_unknown_kind(ids):
    return [{"op": "move_day", "week": 1, "weekday": 1, "toWeekday": 2}]


def _ops_nonsense_kind(ids):
    return [{"op": "explode", "week": 1, "weekday": 1, "itemId": ids["mon"]}]


def _ops_empty(ids):
    return []


def _ops_fifty_one(ids):
    return [{"op": "prescribe", "week": 1, "weekday": 1, "itemId": ids["mon"], **rx()}] * 51


def _ops_item_from_another_day(ids):
    return [{"op": "prescribe", "week": 1, "weekday": 1, "itemId": ids["wed"], **rx()}]


def _ops_item_from_another_week(ids):
    return [{"op": "remove", "week": 1, "weekday": 1, "itemId": ids["mon_week2"]}]


def _ops_unknown_item(ids):
    return [{"op": "remove", "week": 1, "weekday": 1, "itemId": 10**9}]


def _ops_replace_blank_name(ids):
    return [{"op": "replace", "week": 1, "weekday": 1, "itemId": ids["mon"], "name": "   "}]


def _ops_missing_field(ids):
    return [{"op": "replace", "week": 1, "weekday": 1, "itemId": ids["mon"]}]


@pytest.mark.parametrize(
    "build",
    [
        _ops_weeks_without_week,
        _ops_week_99,
        _ops_week_0,
        _ops_weeks_contains_unknown_week,
        _ops_weekday_without_training,
        _ops_weekday_out_of_range,
        _ops_unknown_kind,
        _ops_nonsense_kind,
        _ops_empty,
        _ops_fifty_one,
        _ops_item_from_another_day,
        _ops_item_from_another_week,
        _ops_unknown_item,
        _ops_replace_blank_name,
        _ops_missing_field,
    ],
    ids=lambda f: f.__name__.removeprefix("_ops_"),
)
async def test_invalid_requests_are_422_and_change_nothing(client, auth, build):
    template = await get_program(client, auth)
    ids = {
        "mon": item_id(template, 1, 1, DELTS),
        "wed": item_id(template, 1, 3, BENCH),
        "mon_week2": item_id(template, 2, 1, DELTS),
    }
    r = await patch(client, auth, TEMPLATE, 1, build(ids))
    assert r.status_code == 422, r.text
    assert isinstance(r.json()["detail"], str)
    assert await program_count(client, auth) == 1
    assert await get_program(client, auth) == template
    assert (await state(client, auth))["programId"] == TEMPLATE


async def test_fifty_ops_are_allowed(client, auth):
    template = await get_program(client, auth)
    op = {"op": "prescribe", "week": 1, "weekday": 1, "itemId": item_id(template, 1, 1, DELTS), **rx()}
    body = await ok(client, auth, TEMPLATE, 1, [op] * 50)
    assert [r["op"] for r in body["results"]] == list(range(50))


async def test_missing_version_or_ops_is_422(client, auth):
    r = await client.patch(f"/api/programs/{TEMPLATE}", json={"ops": []}, headers=auth)
    assert r.status_code == 422
    r = await client.patch(f"/api/programs/{TEMPLATE}", json={"version": 1}, headers=auth)
    assert r.status_code == 422


async def test_patch_requires_telegram_auth(client):
    r = await client.patch(f"/api/programs/{TEMPLATE}", json={"version": 1, "ops": []})
    assert r.status_code in (401, 403)


# ---- 9: the copy and versions ----


async def test_first_patch_on_the_template_makes_the_users_copy(client, auth, db):
    template = await get_program(client, auth)
    before_state = await state(client, auth)
    assert before_state["programId"] == TEMPLATE
    body = await ok(
        client, auth, TEMPLATE, 1,
        [{"op": "prescribe", "week": 1, "weekday": 1, "itemId": item_id(template, 1, 1, DELTS), **rx(7, 7)}],
    )
    program = body["program"]
    async with db() as s:
        uid = (await get_or_create_user(s, 42)).id
    assert program["id"] == f"{TEMPLATE}.u{uid}"
    assert program["name"].endswith(" · моя") and program["name"].startswith(template["name"])
    assert (program["version"], program["editable"], program["basedOn"]) == (1, True, TEMPLATE)
    assert program["source"] == template["source"]
    assert body["switchedFrom"] == TEMPLATE
    assert body["results"] == [{"op": 0, "weeks": [1], "skipped": []}]

    assert (await state(client, auth))["programId"] == program["id"]
    assert (await state(client, auth))["programVersion"] == 1
    listed = {p["id"]: p for p in (await client.get("/api/programs", headers=auth)).json()}
    assert set(listed) == {TEMPLATE, program["id"]}
    assert listed[TEMPLATE]["editable"] is False and listed[TEMPLATE]["version"] == 1

    # the template is untouched, the copy differs from it only by the edit
    assert await get_program(client, auth) == template
    assert changed_days(template, program) == {(1, 1)}
    assert item_of(program, 1, 1, DELTS)["prescription"]["raw"] == "7х7"
    assert await get_program(client, auth, program["id"]) == program


async def test_edits_of_the_copy_bump_the_version_and_a_stale_one_is_409(client, auth, copy):
    slug = copy["id"]
    assert copy["version"] == 1
    target = item_id(copy, 1, 1, DELTS)
    op = {"op": "prescribe", "week": 1, "weekday": 1, "itemId": target, **rx(4, 12)}

    body = await ok(client, auth, slug, 1, [op])
    assert body["switchedFrom"] is None
    assert body["program"]["id"] == slug and body["program"]["version"] == 2
    assert (await state(client, auth))["programVersion"] == 2

    r = await patch(client, auth, slug, 1, [{**op, "sets": 9}])
    assert r.status_code == 409
    conflict = r.json()
    assert conflict["detail"] == "version"
    assert conflict["program"]["id"] == slug and conflict["program"]["version"] == 2
    assert conflict["program"] == body["program"]  # the current program, the edit was not applied
    assert item_of(conflict["program"], 1, 1, DELTS)["prescription"]["sets"] == 4

    again = await ok(client, auth, slug, 2, [{**op, "sets": 9}])
    assert again["program"]["version"] == 3


async def test_patch_ahead_of_the_version_is_409_too(client, auth, copy):
    r = await patch(
        client, auth, copy["id"], 5,
        [{"op": "prescribe", "week": 1, "weekday": 1, "itemId": item_id(copy, 1, 1, DELTS), **rx()}],
    )
    assert r.status_code == 409 and r.json()["detail"] == "version"
    assert r.json()["program"]["version"] == 1


async def test_stale_template_after_the_copy_exists_gets_the_copy_not_a_second_one(client, auth, copy):
    template = await get_program(client, auth)
    r = await patch(
        client, auth, TEMPLATE, 1,
        [{"op": "prescribe", "week": 1, "weekday": 1, "itemId": item_id(template, 1, 1, DELTS), **rx()}],
    )
    assert r.status_code == 409
    assert r.json()["detail"] == "version"
    assert r.json()["program"]["id"] == copy["id"]
    assert r.json()["program"] == copy
    assert await program_count(client, auth) == 2
    assert await get_program(client, auth, copy["id"]) == copy


async def test_patch_of_a_program_that_is_not_active_is_409_not_active(client, auth, copy):
    r = await client.put("/api/settings", json={"programId": TEMPLATE}, headers=auth)
    assert r.status_code == 200 and r.json()["programId"] == TEMPLATE
    r = await patch(
        client, auth, copy["id"], copy["version"],
        [{"op": "prescribe", "week": 1, "weekday": 1, "itemId": item_id(copy, 1, 1, DELTS), **rx()}],
    )
    assert r.status_code == 409
    assert r.json() == {"detail": "not_active"}
    assert await get_program(client, auth, copy["id"]) == copy


async def test_unknown_slug_is_404(client, auth):
    r = await patch(
        client, auth, "nope", 1, [{"op": "remove", "week": 1, "weekday": 1, "itemId": 1}]
    )
    assert r.status_code == 404


async def test_another_users_copy_is_404(client, auth, other_auth):
    theirs = await make_copy(client, other_auth)
    assert theirs["id"].startswith(f"{TEMPLATE}.u")
    r = await patch(
        client, auth, theirs["id"], 1,
        [{"op": "prescribe", "week": 1, "weekday": 1, "itemId": item_id(theirs, 1, 1, DELTS), **rx()}],
    )
    assert r.status_code == 404
    assert (await client.get(f"/api/programs/{theirs['id']}", headers=auth)).status_code == 404
    # the owner's copy is untouched and still at version 1
    assert (await get_program(client, other_auth, theirs["id"]))["version"] == 1
    assert (await state(client, auth))["programId"] == TEMPLATE
    assert await program_count(client, auth) == 1


async def test_two_users_get_separate_copies(client, auth, other_auth, db):
    mine = await make_copy(client, auth)
    theirs = await make_copy(client, other_auth)
    assert mine["id"] != theirs["id"]
    async with db() as s:
        assert mine["id"] == f"{TEMPLATE}.u{(await get_or_create_user(s, 42)).id}"
        assert theirs["id"] == f"{TEMPLATE}.u{(await get_or_create_user(s, OTHER)).id}"
    assert [p["id"] for p in (await client.get("/api/programs", headers=auth)).json() if p["editable"]] == [mine["id"]]


# ---- dry run and live events ----


@pytest.fixture
def published(monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(live, "publish", lambda *args, **kw: calls.append((args, kw)))
    return calls


async def test_dry_run_on_the_template_writes_nothing(client, auth, db, published):
    template = await get_program(client, auth)
    before_state = await state(client, auth)
    exercises_before = await exercise_count(db)
    ops = [
        {"op": "replace", "week": 1, "weekday": 1, "itemId": item_id(template, 1, 1, DELTS),
         "name": "Молотки с канатом", "weeks": ALL_WEEKS},
        {"op": "add", "week": 1, "weekday": 1, "tempId": "t", "name": FRONT_RAISE, "position": 1,
         "weeks": ALL_WEEKS, **rx()},
    ]

    dry = await ok(client, auth, TEMPLATE, 1, ops, dryRun=True)
    assert dry["program"] == template
    assert dry["switchedFrom"] == TEMPLATE
    assert [r["op"] for r in dry["results"]] == [0, 1]
    assert dry["results"][0]["weeks"] == ALL_WEEKS
    assert dry["results"][1]["weeks"] == [1, 2, 3]

    assert await program_count(client, auth) == 1
    assert await state(client, auth) == before_state
    assert await get_program(client, auth) == template
    assert await exercise_count(db) == exercises_before
    assert published == []

    real = await ok(client, auth, TEMPLATE, 1, ops)  # the same request for real says the same
    assert real["results"] == dry["results"]
    assert real["switchedFrom"] == TEMPLATE
    assert real["program"]["id"] != TEMPLATE
    assert len(published) == 1


async def test_dry_run_on_the_copy_keeps_the_version(client, auth, copy, published):
    slug = copy["id"]
    op = {"op": "remove", "week": 1, "weekday": 1, "itemId": item_id(copy, 1, 1, DELTS), "weeks": ALL_WEEKS}
    dry = await ok(client, auth, slug, 1, [op], dryRun=True)
    assert dry["program"] == copy and dry["program"]["version"] == 1
    assert dry["switchedFrom"] is None
    assert dry["results"][0]["weeks"] == ALL_WEEKS
    assert await get_program(client, auth, slug) == copy
    assert (await state(client, auth))["programVersion"] == 1
    assert published == []


async def test_dry_run_still_validates(client, auth, copy):
    r = await patch(
        client, auth, copy["id"], 1,
        [{"op": "prescribe", "week": 1, "weekday": 1, "itemId": item_id(copy, 1, 1, DELTS), **rx(0)}],
        dryRun=True,
    )
    assert r.status_code == 422
    r = await patch(
        client, auth, copy["id"], 7,
        [{"op": "prescribe", "week": 1, "weekday": 1, "itemId": item_id(copy, 1, 1, DELTS), **rx()}],
        dryRun=True,
    )
    assert r.status_code == 409 and r.json()["detail"] == "version"


async def test_real_patch_publishes_live_topics_only_on_success(client, auth, db, published):
    template = await get_program(client, auth)
    op = {"op": "prescribe", "week": 1, "weekday": 1, "itemId": item_id(template, 1, 1, DELTS), **rx()}

    assert (await patch(client, auth, TEMPLATE, 1, [{**op, "sets": 0}])).status_code == 422
    assert (await patch(client, auth, TEMPLATE, 4, [op])).status_code == 409
    assert (await patch(client, auth, "nope", 1, [op])).status_code == 404
    assert published == []

    body = await ok(client, auth, TEMPLATE, 1, [op])
    async with db() as s:
        uid = (await get_or_create_user(s, 42)).id
    assert published == [((uid, "program", "plan", "state"), {})]

    await ok(client, auth, body["program"]["id"], 1, [{**op, "itemId": item_id(body["program"], 1, 1, DELTS)}])
    assert len(published) == 2 and published[1] == published[0]


# ---- 10: history safety ----


def exercise_row(name: str, n: int = 2) -> dict:
    return {
        "name": name,
        "target": "",
        "dropset": False,
        "sets": [{"weight": 20, "reps": 10, "done": True} for _ in range(n)],
    }


async def post_week1_monday(client, auth, template: dict) -> dict:
    start = (await state(client, auth))["startDate"]
    day = day_of(template, 1, 1)
    body = {
        "id": "w-history",
        "programId": TEMPLATE,
        "week": 1,
        "weekday": 1,
        "programDayId": day["id"],
        "startedAt": f"{start}T12:00:00Z",
        "finishedAt": f"{start}T13:00:00Z",
        "exercises": [exercise_row(e["name"]) for e in day["exercises"]],
    }
    r = await client.post("/api/workouts", json=body, headers=auth)
    assert r.status_code == 200, r.text
    return r.json()


def history_view(st: dict) -> list[dict]:
    """The history as the user sees it; only the ids that legitimately change with the copy are dropped."""
    return [{k: v for k, v in h.items() if k not in ("programId", "programDayId")} for h in st["history"]]


async def test_history_targets_survive_a_fork_and_prescribing_every_item_everywhere(client, auth):
    template = await get_program(client, auth)
    await post_week1_monday(client, auth, template)
    before = await state(client, auth)
    (entry,) = before["history"]
    assert [x["target"] for x in entry["exercises"]] == [e["prescription"]["raw"] for e in day_of(template, 1, 1)["exercises"]]

    ops = [
        {"op": "prescribe", "week": 1, "weekday": 1, "itemId": e["id"], "sets": 1, "repsMin": 5, "weeks": ALL_WEEKS}
        for e in day_of(template, 1, 1)["exercises"]
    ]
    body = await ok(client, auth, TEMPLATE, 1, ops)

    assert all(
        e["prescription"]["raw"] == "1х5" for e in day_of(body["program"], 1, 1)["exercises"]
    )  # the program really changed
    after = await state(client, auth)
    assert after["programId"] == body["program"]["id"]
    assert history_view(after) == history_view(before)
    (kept,) = after["history"]
    assert [x["target"] for x in kept["exercises"]] == [x["target"] for x in entry["exercises"]]
    assert all(x["target"] != "1х5" for x in kept["exercises"])


async def test_history_targets_survive_replace_remove_reorder(client, auth):
    template = await get_program(client, auth)
    await post_week1_monday(client, auth, template)
    before = await state(client, auth)
    ids = [item_id(template, 1, 1, n) for n in MONDAY_WEEK1]
    ops = [
        {"op": "replace", "week": 1, "weekday": 1, "itemId": ids[4], "name": "Молотки с канатом"},
        {"op": "remove", "week": 1, "weekday": 1, "itemId": ids[0]},
        {"op": "reorder", "week": 1, "weekday": 1, "itemIds": [ids[5], "t", ids[4], ids[3], ids[2], ids[1]]},
    ]
    ops.insert(2, {"op": "add", "week": 1, "weekday": 1, "tempId": "t", "name": "Скручивания", "position": 1, **rx()})
    await ok(client, auth, TEMPLATE, 1, ops)
    assert history_view(await state(client, auth)) == history_view(before)


async def test_replace_keeps_the_old_exercise_row_and_its_name(client, auth, db):
    async with db() as s:
        old = await s.scalar(select(Exercise).where(Exercise.name == DELTS))
        old_id = old.id
    template = await get_program(client, auth)
    await ok(
        client, auth, TEMPLATE, 1,
        [{"op": "replace", "week": 1, "weekday": 1, "itemId": item_id(template, 1, 1, DELTS), "name": "Молотки",
          "weeks": ALL_WEEKS}],
    )
    async with db() as s:
        row = await s.get(Exercise, old_id)
        assert row is not None and row.name == DELTS
        assert await s.scalar(select(func.count(Exercise.id)).where(Exercise.name == "молотки")) == 1


async def test_program_days_are_never_deleted(client, auth, db):
    counts = [await day_count(db)]
    template = await get_program(client, auth)
    ids = [item_id(template, 1, 3, n) for n in names(template, 1, 3)]
    body = await ok(  # the fork adds 24 days, the edits none
        client, auth, TEMPLATE, 1,
        [{"op": "prescribe", "week": 1, "weekday": 3, "itemId": ids[0], **rx(), "weeks": ALL_WEEKS}],
    )
    counts.append(await day_count(db))
    slug = body["program"]["id"]
    wed_ids = [e["id"] for e in day_of(body["program"], 1, 3)["exercises"]]
    version = body["program"]["version"]
    steps = [
        [{"op": "remove", "week": 1, "weekday": 3, "itemId": wed_ids[1], "weeks": ALL_WEEKS}],
        [{"op": "add", "week": 1, "weekday": 3, "tempId": "t", "name": "Молотки", "position": 1, **rx(),
          "weeks": ALL_WEEKS}],
        [{"op": "replace", "week": 1, "weekday": 3, "itemId": wed_ids[2], "name": "Скручивания",
          "weeks": ALL_WEEKS}],
        [{"op": "remove", "week": 1, "weekday": 3, "itemId": wed_ids[3], "weeks": ALL_WEEKS},
         {"op": "remove", "week": 1, "weekday": 3, "itemId": wed_ids[0], "weeks": ALL_WEEKS}],
    ]
    for ops in steps:
        r = await patch(client, auth, slug, version, ops)
        if r.status_code == 200:
            version = r.json()["program"]["version"]
        counts.append(await day_count(db))
    assert counts[1] == counts[0] + 24
    assert counts == sorted(counts) and len(set(counts[1:])) == 1


# ---- 11: the plan sees the edit ----


async def test_plan_day_of_the_edited_week_names_the_new_exercise(client, auth, db, settings):
    template = await get_program(client, auth)
    start = (await state(client, auth))["startDate"]  # a Monday: week 1, weekday 1
    monday = date.fromisoformat(start)
    assert monday.isoweekday() == 1
    await ok(
        client, auth, TEMPLATE, 1,
        [{"op": "replace", "week": 1, "weekday": 1, "itemId": item_id(template, 1, 1, DELTS),
          "name": "Молотки с канатом"}],
    )
    async with db() as s:
        user = await get_or_create_user(s, 42)
        ref = await nw.program_ref(s, user, settings)
        assert ref is not None
        week, weekday, items = nw.program_day(ref, monday)
        assert (week, weekday) == (1, 1)
        assert [i.name for i in items] == [*MONDAY_WEEK1[:4], "молотки с канатом", REAR_DELT]
        week2, _, items2 = nw.program_day(ref, monday + timedelta(days=7))
        assert week2 == 2
        assert [i.name for i in items2] == MONDAY_WEEK1
