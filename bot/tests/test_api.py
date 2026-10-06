from datetime import date

from conftest import init_data, make_settings
from sqlalchemy import select

from gymbot.db.models import Exercise, Workout, WorkoutSet


def workout(wid="abc", **over):
    w = {
        "id": wid,
        "programId": "",
        "week": 1,
        "weekday": 1,
        "startedAt": "2026-10-05T15:00:00Z",
        "finishedAt": "2026-10-05T16:00:00Z",
        "exercises": [
            {
                "name": "Совсем новое упражнение",
                "target": "",
                "dropset": False,
                "sets": [
                    {"weight": 14, "reps": 10, "done": True},
                    {"weight": 14, "reps": 9, "done": False},  # not done
                    {"weight": 14, "reps": None, "done": True},  # no reps
                    {"weight": 16, "reps": 8, "done": True},
                ],
            },
        ],
    }
    w.update(over)
    return w


async def test_401_without_init_data(client):
    assert (await client.get("/api/state")).status_code == 401


async def test_401_with_bad_init_data(client):
    r = await client.get("/api/state", headers={"X-Telegram-Init-Data": "hash=bad"})
    assert r.status_code == 401


async def test_health_open(client):
    assert (await client.get("/api/health")).status_code == 200


async def test_dev_user_bypass(tmp_path, make_client):
    async with make_client(make_settings(tmp_path, dev_user_id=5)) as c:
        r = await c.get("/api/state")
    assert r.status_code == 200


async def test_403_user_not_allowed(tmp_path, make_client):
    async with make_client(make_settings(tmp_path, allowed_user_ids=[1])) as c:
        assert (await c.get("/api/state", headers={"X-Telegram-Init-Data": init_data(42)})).status_code == 403
        assert (await c.get("/api/state", headers={"X-Telegram-Init-Data": init_data(1)})).status_code == 200


async def test_state_creates_user_and_default_program(client, auth, db):
    r = await client.get("/api/state", headers=auth)
    assert r.status_code == 200
    st = r.json()
    assert st["programId"] and st["history"] == [] and st["restSeconds"] == 90
    assert date.fromisoformat(st["startDate"]).weekday() == 0  # Monday


async def test_post_workout(client, auth, db):
    st = (await client.get("/api/state", headers=auth)).json()
    w = workout(programId=st["programId"])
    r = await client.post("/api/workouts", json=w, headers=auth)
    assert r.status_code == 200, r.text
    out = r.json()
    (ex,) = out["exercises"]
    assert [(s["weight"], s["reps"]) for s in ex["sets"]] == [(14, 10), (16, 8)]
    assert (out["week"], out["weekday"], out["programId"]) == (1, 1, st["programId"])
    async with db() as s:
        saved = (await s.scalars(select(Workout))).one()
        assert saved.program_day_id is not None and saved.client_id == "abc"
        assert (await s.scalar(select(Exercise).where(Exercise.name == "совсем новое упражнение"))) is not None


async def test_post_workout_idempotent(client, auth, db):
    st = (await client.get("/api/state", headers=auth)).json()
    w = workout(programId=st["programId"])
    a = (await client.post("/api/workouts", json=w, headers=auth)).json()
    b = (await client.post("/api/workouts", json=w, headers=auth)).json()
    assert a["id"] == b["id"]
    async with db() as s:
        assert len((await s.scalars(select(Workout))).all()) == 1
        assert len((await s.scalars(select(WorkoutSet))).all()) == 2
    assert len((await client.get("/api/state", headers=auth)).json()["history"]) == 1


async def test_post_workout_other_program_has_no_day(client, auth, db):
    r = await client.post("/api/workouts", json=workout(programId="nope"), headers=auth)
    assert r.status_code == 200
    async with db() as s:
        assert (await s.scalars(select(Workout))).one().program_day_id is None


async def test_put_settings(client, auth):
    st = (await client.get("/api/state", headers=auth)).json()
    r = await client.put("/api/settings", json={"startDate": "2026-09-17", "restSeconds": 120}, headers=auth)
    assert r.status_code == 200
    out = r.json()
    assert out["startDate"] == "2026-09-14"  # Thursday snapped to its Monday
    assert out["restSeconds"] == 120 and out["programId"] == st["programId"]
    again = (await client.get("/api/state", headers=auth)).json()
    assert again["startDate"] == "2026-09-14" and again["restSeconds"] == 120


async def test_put_settings_unknown_program(client, auth):
    r = await client.put("/api/settings", json={"programId": "does-not-exist"}, headers=auth)
    assert r.status_code == 404


async def test_put_settings_rest_out_of_range(client, auth):
    assert (await client.put("/api/settings", json={"restSeconds": 5}, headers=auth)).status_code == 422


async def test_delete_workout(client, auth):
    st = (await client.get("/api/state", headers=auth)).json()
    wid = (await client.post("/api/workouts", json=workout(programId=st["programId"]), headers=auth)).json()["id"]
    assert (await client.delete(f"/api/workouts/{wid}", headers=auth)).status_code == 204
    assert (await client.get("/api/state", headers=auth)).json()["history"] == []
    assert (await client.delete(f"/api/workouts/{wid}", headers=auth)).status_code == 404


async def test_delete_other_users_workout_404(client, auth):
    st = (await client.get("/api/state", headers=auth)).json()
    wid = (await client.post("/api/workouts", json=workout(programId=st["programId"]), headers=auth)).json()["id"]
    other = {"X-Telegram-Init-Data": init_data(99)}
    assert (await client.delete(f"/api/workouts/{wid}", headers=other)).status_code == 404
    assert len((await client.get("/api/state", headers=auth)).json()["history"]) == 1
