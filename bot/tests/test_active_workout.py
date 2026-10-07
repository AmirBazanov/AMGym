"""The workout in progress in the Mini App: PUT/DELETE /api/workouts/active and the diary answer line."""

import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from alembic import command
from conftest import init_data, make_settings
from sqlalchemy import func, inspect, select

from gymbot.db import migrate
from gymbot.db.models import ActiveWorkout, Exercise, User, Workout, WorkoutSet
from gymbot.db.session import make_engine
from gymbot.services import active_workout as aw
from gymbot.services import answer as answer_service
from gymbot.services.workouts import WorkoutIn

MSK = ZoneInfo("Europe/Moscow")
OTHER = {"X-Telegram-Init-Data": init_data(43, "Other")}


def active(wid="w1", **over):
    """Mid-workout snapshot: 2 of 4 bench sets done, the pulldown untouched (sets without reps yet)."""
    w = {
        "id": wid,
        "programId": "",
        "week": 1,
        "weekday": 3,
        "startedAt": "2026-10-07T15:40:00Z",  # 18:40 MSK
        "finishedAt": None,
        "exercises": [
            {
                "name": "жим лёжа",
                "target": "4х6-8",
                "dropset": False,
                "sets": [
                    {"weight": 82.5, "reps": 8, "done": True},
                    {"weight": 82.5, "reps": 8, "done": True},
                    {"weight": 82.5, "reps": None, "done": False},
                    {"weight": None, "reps": None, "done": False},
                ],
            },
            {
                "name": "тяга вертикального блока",
                "target": "3х10-12",
                "dropset": False,
                "sets": [{"weight": 50, "reps": None, "done": False} for _ in range(3)],
            },
        ],
    }
    w.update(over)
    return w


async def rows(db, model) -> int:
    async with db() as s:
        return await s.scalar(select(func.count()).select_from(model))


async def stored(db) -> list[ActiveWorkout]:
    async with db() as s:
        return list(await s.scalars(select(ActiveWorkout)))


async def user_obj(db, telegram_id: int = 42) -> User:
    async with db() as s:
        return await s.scalar(select(User).where(User.telegram_id == telegram_id))


# ---- API ----


async def test_put_stores_snapshot_without_history(client, auth, db):
    await client.get("/api/state", headers=auth)  # program + catalog exercises exist
    exercises_before = await rows(db, Exercise)
    r = await client.put("/api/workouts/active", json=active(exercises=[
        *active()["exercises"],
        {"name": "Совсем новое упражнение", "sets": [{"weight": 10, "reps": 10, "done": True}]},
    ]), headers=auth)
    assert r.status_code == 204, r.text
    (row,) = await stored(db)
    assert row.client_id == "w1"
    payload = json.loads(row.payload)
    assert [s["done"] for s in payload["exercises"][0]["sets"]] == [True, True, False, False]
    # Nothing in the history: no workouts, sets or new exercises; /api/state shows no history.
    assert await rows(db, Workout) == 0 and await rows(db, WorkoutSet) == 0
    assert await rows(db, Exercise) == exercises_before
    assert (await client.get("/api/state", headers=auth)).json()["history"] == []


async def test_put_upserts_one_row_per_user(client, auth, db):
    assert (await client.put("/api/workouts/active", json=active(), headers=auth)).status_code == 204
    first = (await stored(db))[0].updated_at
    nxt = active("w2")
    nxt["exercises"][0]["sets"][2] = {"weight": 82.5, "reps": 7, "done": True}
    assert (await client.put("/api/workouts/active", json=nxt, headers=auth)).status_code == 204
    (row,) = await stored(db)
    assert row.client_id == "w2" and row.updated_at >= first
    assert json.loads(row.payload)["exercises"][0]["sets"][2]["reps"] == 7


async def test_put_rejects_done_set_without_reps(client, auth, db):
    bad = active()
    bad["exercises"][0]["sets"][0]["reps"] = None
    r = await client.put("/api/workouts/active", json=bad, headers=auth)
    assert r.status_code == 422 and "set without reps" in r.text
    assert await stored(db) == []


async def test_put_size_limit(client, auth, db):
    big = active(exercises=[
        {"name": f"упражнение {i}", "target": "x" * 150, "sets": [{"weight": 10, "reps": None, "done": False}] * 20}
        for i in range(100)
    ])
    assert len(json.dumps(big, ensure_ascii=False, separators=(",", ":")).encode()) > aw.MAX_BYTES
    r = await client.put("/api/workouts/active", json=big, headers=auth)
    assert r.status_code == 422 and "64 KB" in r.text
    assert await stored(db) == []


async def test_put_with_nothing_done_yet(client, auth, db):
    fresh = active()
    for ex in fresh["exercises"]:
        ex["sets"] = [{"weight": None, "reps": None, "done": False} for _ in ex["sets"]]
    r = await client.put("/api/workouts/active", json=fresh, headers=auth)
    assert r.status_code == 204 and r.content == b""
    (row,) = await stored(db)
    line = aw.context_line(row, datetime.now(UTC), MSK)
    assert line.startswith("В мини-аппе открыта тренировка, ни один подход ещё не отмечен")
    assert "×" not in line


async def test_put_for_finished_workout_removes_its_snapshot(client, auth, db):
    st = (await client.get("/api/state", headers=auth)).json()
    done = active(programId=st["programId"])
    assert (await client.post("/api/workouts", json=done, headers=auth)).status_code == 200
    async with db() as s:  # a leftover snapshot of that workout, however it got there
        uid = await s.scalar(select(User.id).where(User.telegram_id == 42))
        s.add(ActiveWorkout(user_id=uid, client_id="w1", payload="{}", updated_at=datetime.now(UTC)))
        await s.commit()
    r = await client.put("/api/workouts/active", json=done, headers=auth)
    assert r.status_code == 204 and r.content == b""
    assert await stored(db) == []
    assert await rows(db, Workout) == 1


async def test_delete_is_idempotent(client, auth, db):
    assert (await client.delete("/api/workouts/active", headers=auth)).status_code == 204
    await client.put("/api/workouts/active", json=active(), headers=auth)
    assert (await client.delete("/api/workouts/active", headers=auth)).status_code == 204
    assert await stored(db) == []
    assert (await client.delete("/api/workouts/active", headers=auth)).status_code == 204


async def test_delete_with_client_id_keeps_a_newer_workout(client, auth, db):
    await client.put("/api/workouts/active", json=active("new"), headers=auth)
    assert (await client.delete("/api/workouts/active?clientId=old", headers=auth)).status_code == 204
    assert [r.client_id for r in await stored(db)] == ["new"]
    assert (await client.delete("/api/workouts/active?clientId=new", headers=auth)).status_code == 204
    assert await stored(db) == []


async def test_finish_clears_active_and_late_put_is_ignored(client, auth, db):
    st = (await client.get("/api/state", headers=auth)).json()
    await client.put("/api/workouts/active", json=active(programId=st["programId"]), headers=auth)
    done = active(programId=st["programId"], finishedAt="2026-10-07T16:40:00Z")
    r = await client.post("/api/workouts", json=done, headers=auth)
    assert r.status_code == 200, r.text
    assert await stored(db) == []
    assert await rows(db, WorkoutSet) == 2  # only the done sets, from the POST
    # A PUT that was in flight when the workout was finished does not bring it back.
    assert (await client.put("/api/workouts/active", json=done, headers=auth)).status_code == 204
    assert await stored(db) == []


async def test_finish_retry_also_clears(client, auth, db):
    st = (await client.get("/api/state", headers=auth)).json()
    done = active(programId=st["programId"])
    assert (await client.post("/api/workouts", json=done, headers=auth)).status_code == 200
    async with db() as s:  # e.g. a snapshot stored by an older app version between the two POSTs
        uid = (await s.scalar(select(User.id).where(User.telegram_id == 42)))
        s.add(ActiveWorkout(user_id=uid, client_id="w1", payload="{}", updated_at=datetime.now(UTC)))
        await s.commit()
    assert (await client.post("/api/workouts", json=done, headers=auth)).status_code == 200
    assert await stored(db) == []


async def test_finishing_another_workout_keeps_the_active_one(client, auth, db):
    st = (await client.get("/api/state", headers=auth)).json()
    await client.put("/api/workouts/active", json=active("today"), headers=auth)
    # The offline queue sends an older workout: today's snapshot stays.
    assert (await client.post("/api/workouts", json=active("old", programId=st["programId"]), headers=auth)).status_code == 200
    assert [r.client_id for r in await stored(db)] == ["today"]


async def test_auth_and_other_users(tmp_path, make_client, auth, db):
    async with make_client(make_settings(tmp_path, allowed_user_ids=[42, 43])) as client:
        assert (await client.put("/api/workouts/active", json=active())).status_code == 401
        assert (await client.delete("/api/workouts/active")).status_code == 401
        await client.put("/api/workouts/active", json=active(), headers=auth)
        # Another user's DELETE does not touch the owner's snapshot, and their own PUT is a separate row.
        assert (await client.delete("/api/workouts/active", headers=OTHER)).status_code == 204
        assert (await client.delete("/api/workouts/active?clientId=w1", headers=OTHER)).status_code == 204
        assert [r.client_id for r in await stored(db)] == ["w1"]
        assert (await client.put("/api/workouts/active", json=active("theirs"), headers=OTHER)).status_code == 204
        assert sorted(r.client_id for r in await stored(db)) == ["theirs", "w1"]
        assert (await client.delete("/api/workouts/active", headers=OTHER)).status_code == 204
        assert [r.client_id for r in await stored(db)] == ["w1"]
    owner, other = await user_obj(db, 42), await user_obj(db, 43)
    now = datetime.now(UTC)
    async with db() as s:  # the other user's diary answer does not see the owner's workout
        assert await aw.context_for(s, other.id, now, MSK) == ""
        assert "жим лёжа 82.5×8" in await aw.context_for(s, owner.id, now, MSK)


async def test_delete_workout_by_id_still_works(client, auth, db):
    st = (await client.get("/api/state", headers=auth)).json()
    out = (await client.post("/api/workouts", json=active(programId=st["programId"]), headers=auth)).json()
    assert (await client.delete("/api/workouts/999999", headers=auth)).status_code == 404
    assert (await client.delete(f"/api/workouts/{out['id']}", headers=auth)).status_code == 204


# ---- context line ----


def snapshot(updated_at: datetime, **over) -> ActiveWorkout:
    return ActiveWorkout(
        user_id=1, client_id="w1", payload=WorkoutIn.model_validate(active(**over)).model_dump_json(),
        updated_at=updated_at,
    )


UPDATED = datetime(2026, 10, 7, 16, 10, tzinfo=UTC)  # 19:10 MSK


def test_context_line_format():
    line = aw.context_line(snapshot(UPDATED), UPDATED + timedelta(minutes=5), MSK)
    assert line == (
        "Сейчас идёт тренировка в мини-аппе (начата 18:40, обновлена 19:10): жим лёжа 82.5×8, 82.5×8 "
        "(2 из 4 подходов); тяга вертикального блока — ещё не начато."
    )


def test_context_line_naive_sqlite_time_and_bodyweight_sets():
    exercises = [{"name": "подтягивания", "sets": [{"weight": None, "reps": 10, "done": True}]}]
    line = aw.context_line(snapshot(UPDATED.replace(tzinfo=None), exercises=exercises), UPDATED, MSK)
    assert line.endswith("обновлена 19:10): подтягивания 10 повт. (1 из 1 подхода).")


def test_context_line_nothing_ticked_yet():
    exercises = [{"name": "жим лёжа", "sets": [{"weight": 80, "reps": None, "done": False}] * 4}]
    line = aw.context_line(snapshot(UPDATED, exercises=exercises), UPDATED, MSK)
    assert line == "В мини-аппе открыта тренировка, ни один подход ещё не отмечен (обновлена 19:10): жим лёжа — ещё не начато."
    assert "начата" not in line


def test_context_line_stale_or_missing():
    assert aw.context_line(None, UPDATED, MSK) == ""
    assert aw.context_line(snapshot(UPDATED), UPDATED + timedelta(hours=6, minutes=1), MSK) == ""
    assert aw.context_line(snapshot(UPDATED), UPDATED + timedelta(hours=5, minutes=59), MSK) != ""
    broken = ActiveWorkout(user_id=1, client_id="w1", payload="{not json", updated_at=UPDATED)
    assert aw.context_line(broken, UPDATED, MSK) == ""


def test_context_line_is_capped():
    exercises = [
        {"name": f"упражнение номер {i}", "sets": [{"weight": 100.5, "reps": 12, "done": True}] * 10}
        for i in range(20)
    ]
    line = aw.context_line(snapshot(UPDATED, exercises=exercises), UPDATED, MSK)
    assert len(line) <= aw.LINE_MAX and line.endswith("…")


def test_context_line_other_day_shows_date():
    line = aw.context_line(snapshot(UPDATED), UPDATED + timedelta(hours=5), MSK)  # 00:10 MSK next day
    assert "(начата 07.10 18:40, обновлена 07.10 19:10)" in line


async def test_answer_context_has_the_workout_in_progress(client, auth, settings, db):
    await client.get("/api/state", headers=auth)
    await client.put("/api/workouts/active", json=active(), headers=auth)
    tz = ZoneInfo(settings.timezone)
    now = datetime.now(UTC)
    async with db() as s:
        user = await s.scalar(select(User).where(User.telegram_id == 42))
        ctx = await answer_service.build_context(s, user, settings, None, tz, now)
        assert "жим лёжа 82.5×8, 82.5×8 (2 из 4 подходов); тяга вертикального блока — ещё не начато." in ctx
        stale = await answer_service.build_context(s, user, settings, None, tz, now + timedelta(hours=7))
        assert "Сейчас идёт тренировка" not in stale


# ---- migration 0010 ----


async def test_migration_0010_round_trip(tmp_path):
    engine, _ = make_engine(f"sqlite+aiosqlite:///{tmp_path}/m.db")

    async def migrate_to(target: str) -> set[str]:
        def fn(conn):
            cfg = migrate._config()
            cfg.attributes["connection"] = conn
            (command.upgrade if target == "head" else command.downgrade)(cfg, target)
            insp = inspect(conn)
            tables = set(insp.get_table_names())
            if "active_workouts" in tables:
                assert {c["name"] for c in insp.get_columns("active_workouts")} == {
                    "user_id", "client_id", "payload", "updated_at"
                }
                assert insp.get_pk_constraint("active_workouts")["constrained_columns"] == ["user_id"]
            return tables

        async with engine.begin() as conn:
            return await conn.run_sync(fn)

    try:
        assert "active_workouts" in await migrate_to("head")
        assert "active_workouts" not in await migrate_to("0009")
        assert "active_workouts" in await migrate_to("head")
    finally:
        await engine.dispose()
