import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from conftest import init_data, make_settings
from sqlalchemy import select

from gymbot.db.models import Exercise, Reminder, User, WeightOverride, Workout, WorkoutSet


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
    # Another Telegram user is not the owner (first user) of this personal app at all.
    assert (await client.delete(f"/api/workouts/{wid}", headers=other)).status_code == 403
    assert len((await client.get("/api/state", headers=auth)).json()["history"]) == 1


async def test_post_workout_done_set_without_reps_is_rejected(client, auth):
    bad = workout(wid="noreps")
    bad["exercises"][0]["sets"].append({"weight": 14, "reps": None, "done": True})
    assert (await client.post("/api/workouts", json=bad, headers=auth)).status_code == 422


async def test_first_user_becomes_owner(client, auth):
    assert (await client.get("/api/state", headers=auth)).status_code == 200
    stranger = {"X-Telegram-Init-Data": init_data(12345)}
    assert (await client.get("/api/state", headers=stranger)).status_code == 403


async def test_dev_bypass_refused_through_tunnel(tmp_path, make_client):
    async with make_client(make_settings(tmp_path, dev_user_id=7)) as c:
        assert (await c.get("/api/state")).status_code == 200
        assert (await c.get("/api/state", headers={"cf-connecting-ip": "1.2.3.4"})).status_code == 401


# ---- reminders ----


async def test_reminder_create_and_list(client, auth):
    r = await client.post("/api/reminders", json={"time": "09:30", "kind": "text", "text": "Креатин"}, headers=auth)
    assert r.status_code == 201
    body = r.json()
    assert set(body) == {"id", "time", "kind", "text", "enabled", "weekday"}
    assert body["weekday"] is None
    assert body["time"] == "09:30" and body["kind"] == "text" and body["text"] == "Креатин"
    assert body["enabled"] is True
    listed = (await client.get("/api/reminders", headers=auth)).json()
    assert listed == [body]


async def test_reminder_list_sorted_by_time(client, auth):
    for t in ("21:00", "07:15", "09:30"):
        await client.post("/api/reminders", json={"time": t, "kind": "text", "text": t}, headers=auth)
    assert [r["time"] for r in (await client.get("/api/reminders", headers=auth)).json()] == [
        "07:15", "09:30", "21:00"
    ]


async def test_reminder_nutrition_ignores_text(client, auth):
    r = await client.post(
        "/api/reminders", json={"time": "20:00", "kind": "nutrition", "text": "игнор"}, headers=auth
    )
    assert r.status_code == 201
    assert r.json()["text"] is None and r.json()["kind"] == "nutrition"
    assert (await client.get("/api/reminders", headers=auth)).json()[0]["text"] is None


@pytest.mark.parametrize(
    "bad", ["24:00", "9:30", "09:60", "0930", "09:30:00", "", "\u0660\u0669:\u0663\u0660", "09:30\n", "09-30", "ab:cd"]
)
async def test_reminder_invalid_time_422(client, auth, bad):
    r = await client.post("/api/reminders", json={"time": bad, "kind": "nutrition"}, headers=auth)
    assert r.status_code == 422
    assert (await client.get("/api/reminders", headers=auth)).json() == []


@pytest.mark.parametrize("good", ["00:00", "23:59", "12:05"])
async def test_reminder_valid_time_edges(client, auth, good):
    r = await client.post("/api/reminders", json={"time": good, "kind": "nutrition"}, headers=auth)
    assert r.status_code == 201 and r.json()["time"] == good


@pytest.mark.parametrize("payload", [
    {"time": "09:30", "kind": "text"},
    {"time": "09:30", "kind": "text", "text": None},
    {"time": "09:30", "kind": "text", "text": ""},
    {"time": "09:30", "kind": "text", "text": "   "},
    {"time": "09:30", "kind": "text", "text": "x" * 201},
    {"time": "09:30", "kind": "weekly", "text": "x"},
    {"time": "09:30", "text": "x"},
])
async def test_reminder_invalid_body_422(client, auth, payload):
    assert (await client.post("/api/reminders", json=payload, headers=auth)).status_code == 422
    assert (await client.get("/api/reminders", headers=auth)).json() == []


async def test_reminder_text_length_200_ok(client, auth):
    r = await client.post("/api/reminders", json={"time": "09:30", "kind": "text", "text": "x" * 200}, headers=auth)
    assert r.status_code == 201 and r.json()["text"] == "x" * 200


async def test_reminder_limit_20_then_409(client, auth):
    for i in range(20):
        r = await client.post(
            "/api/reminders",
            json={"time": f"{i:02d}:00", "kind": "text", "text": f"n{i}", "enabled": i % 3 != 0},
            headers=auth,
        )
        assert r.status_code == 201
    r = await client.post("/api/reminders", json={"time": "23:30", "kind": "text", "text": "лишнее"}, headers=auth)
    assert r.status_code == 409
    assert len((await client.get("/api/reminders", headers=auth)).json()) == 20


async def test_reminder_limit_frees_after_delete(client, auth):
    ids = []
    for i in range(20):
        r = await client.post("/api/reminders", json={"time": f"{i:02d}:00", "kind": "nutrition"}, headers=auth)
        ids.append(r.json()["id"])
    assert (await client.delete(f"/api/reminders/{ids[0]}", headers=auth)).status_code == 204
    assert (await client.post("/api/reminders", json={"time": "23:30", "kind": "nutrition"}, headers=auth)).status_code == 201


async def test_reminder_other_user_isolated(tmp_path, make_client):
    # Two allowed users: otherwise the second one is refused with 403 (personal app).
    one = {"X-Telegram-Init-Data": init_data(42)}
    two = {"X-Telegram-Init-Data": init_data(99)}
    async with make_client(make_settings(tmp_path, allowed_user_ids=[42, 99])) as c:
        rid = (await c.post("/api/reminders", json={"time": "09:30", "kind": "text", "text": "моё"}, headers=one)).json()["id"]
        assert (await c.get("/api/reminders", headers=two)).json() == []
        assert (await c.patch(f"/api/reminders/{rid}", json={"enabled": False, "weekday": None}, headers=two)).status_code == 404
        assert (await c.delete(f"/api/reminders/{rid}", headers=two)).status_code == 404
        mine = (await c.get("/api/reminders", headers=one)).json()
        assert len(mine) == 1 and mine[0]["enabled"] is True and mine[0]["text"] == "моё"
        # The other user's reminders do not count towards the limit.
        for i in range(20):
            assert (await c.post("/api/reminders", json={"time": f"{i:02d}:10", "kind": "nutrition"}, headers=two)).status_code == 201
        assert (await c.post("/api/reminders", json={"time": "23:30", "kind": "nutrition"}, headers=one)).status_code == 201


async def test_reminder_unknown_id_404(client, auth):
    assert (await client.patch("/api/reminders/9999", json={"enabled": False, "weekday": None}, headers=auth)).status_code == 404
    assert (await client.delete("/api/reminders/9999", headers=auth)).status_code == 404


async def test_reminder_requires_auth(client):
    assert (await client.get("/api/reminders")).status_code == 401
    assert (await client.post("/api/reminders", json={"time": "09:30", "kind": "nutrition"})).status_code == 401


async def test_reminder_patch_changes_only_given_fields(client, auth):
    rid = (await client.post("/api/reminders", json={"time": "09:30", "kind": "text", "text": "Креатин"}, headers=auth)).json()["id"]
    r = await client.patch(f"/api/reminders/{rid}", json={"enabled": False, "weekday": None}, headers=auth)
    assert r.status_code == 200
    assert r.json() == {"id": rid, "time": "09:30", "kind": "text", "text": "Креатин", "enabled": False, "weekday": None}
    r = await client.patch(f"/api/reminders/{rid}", json={"time": "10:15"}, headers=auth)
    assert r.json() == {"id": rid, "time": "10:15", "kind": "text", "text": "Креатин", "enabled": False, "weekday": None}
    r = await client.patch(f"/api/reminders/{rid}", json={"text": "  Протеин "}, headers=auth)
    assert r.json() == {"id": rid, "time": "10:15", "kind": "text", "text": "Протеин", "enabled": False, "weekday": None}
    assert (await client.get("/api/reminders", headers=auth)).json() == [r.json()]


async def test_reminder_patch_empty_body_keeps_everything(client, auth):
    created = (await client.post("/api/reminders", json={"time": "09:30", "kind": "text", "text": "Креатин"}, headers=auth)).json()
    r = await client.patch(f"/api/reminders/{created['id']}", json={}, headers=auth)
    assert r.status_code == 200 and r.json() == created


async def test_reminder_patch_invalid_values_422(client, auth):
    rid = (await client.post("/api/reminders", json={"time": "09:30", "kind": "text", "text": "Креатин"}, headers=auth)).json()["id"]
    for body in ({"time": "24:00"}, {"time": "9:30"}, {"kind": "weekly"}, {"text": "x" * 201}, {"enabled": "maybe"}):
        assert (await client.patch(f"/api/reminders/{rid}", json=body, headers=auth)).status_code == 422
    assert (await client.get("/api/reminders", headers=auth)).json()[0]["time"] == "09:30"


async def test_reminder_patch_to_text_without_text_422(client, auth):
    rid = (await client.post("/api/reminders", json={"time": "20:00", "kind": "nutrition"}, headers=auth)).json()["id"]
    assert (await client.patch(f"/api/reminders/{rid}", json={"kind": "text"}, headers=auth)).status_code == 422
    assert (await client.patch(f"/api/reminders/{rid}", json={"kind": "text", "text": "  "}, headers=auth)).status_code == 422
    after = (await client.get("/api/reminders", headers=auth)).json()[0]
    assert after["kind"] == "nutrition" and after["text"] is None
    ok = await client.patch(f"/api/reminders/{rid}", json={"kind": "text", "text": "Вода"}, headers=auth)
    assert ok.status_code == 200 and ok.json()["kind"] == "text" and ok.json()["text"] == "Вода"


async def test_reminder_patch_text_null_on_text_kind_422(client, auth):
    rid = (await client.post("/api/reminders", json={"time": "09:30", "kind": "text", "text": "Креатин"}, headers=auth)).json()["id"]
    assert (await client.patch(f"/api/reminders/{rid}", json={"text": None}, headers=auth)).status_code == 422
    assert (await client.get("/api/reminders", headers=auth)).json()[0]["text"] == "Креатин"


async def test_reminder_patch_kind_nutrition_drops_text(client, auth):
    rid = (await client.post("/api/reminders", json={"time": "09:30", "kind": "text", "text": "Креатин"}, headers=auth)).json()["id"]
    r = await client.patch(f"/api/reminders/{rid}", json={"kind": "nutrition"}, headers=auth)
    assert r.status_code == 200 and r.json()["kind"] == "nutrition" and r.json()["text"] is None


async def test_reminder_delete(client, auth):
    rid = (await client.post("/api/reminders", json={"time": "09:30", "kind": "nutrition"}, headers=auth)).json()["id"]
    assert (await client.delete(f"/api/reminders/{rid}", headers=auth)).status_code == 204
    assert (await client.get("/api/reminders", headers=auth)).json() == []
    assert (await client.delete(f"/api/reminders/{rid}", headers=auth)).status_code == 404


async def test_reminder_created_at_passed_time_is_marked_today(client, auth, db, settings):
    # 00:00 has always passed today, so the reminder must not fire right away: last_sent_on = today.
    tz = ZoneInfo(settings.timezone)
    before = datetime.now(tz).date()
    rid = (await client.post("/api/reminders", json={"time": "00:00", "kind": "nutrition"}, headers=auth)).json()["id"]
    after = datetime.now(tz).date()
    async with db() as s:
        last = await s.scalar(select(Reminder.last_sent_on).where(Reminder.id == rid))
    assert last in (before, after)  # tolerates the test running across local midnight


async def test_reminder_patch_rearms_last_sent_on(client, auth, db, settings):
    # Re-timing to an already-passed time (00:00) and re-enabling mark today as done, so nothing fires at once.
    tz = ZoneInfo(settings.timezone)
    rid = (await client.post("/api/reminders", json={"time": "23:59", "kind": "nutrition"}, headers=auth)).json()["id"]
    async with db() as s:
        r = await s.get(Reminder, rid)
        r.last_sent_on = None
        await s.commit()
    before = datetime.now(tz).date()
    assert (await client.patch(f"/api/reminders/{rid}", json={"time": "00:00"}, headers=auth)).status_code == 200
    after = datetime.now(tz).date()
    async with db() as s:
        assert await s.scalar(select(Reminder.last_sent_on).where(Reminder.id == rid)) in (before, after)
        r = await s.get(Reminder, rid)
        r.last_sent_on = None
        r.enabled = False
        await s.commit()
    assert (await client.patch(f"/api/reminders/{rid}", json={"enabled": True}, headers=auth)).status_code == 200
    async with db() as s:
        assert await s.scalar(select(Reminder.last_sent_on).where(Reminder.id == rid)) is not None


# ---- profile ----

PROFILE_KEYS = {"weightKg", "heightCm", "birthYear", "goal", "about"}


async def _profile(client, auth) -> dict:
    return (await client.get("/api/state", headers=auth)).json()["profile"]


async def test_profile_empty_for_new_user(client, auth):
    assert await _profile(client, auth) == dict.fromkeys(PROFILE_KEYS)


async def test_put_profile_saves_rounds_and_trims(client, auth):
    body = {"weightKg": 82.46, "heightCm": 180, "birthYear": 1998, "goal": "mass", "about": "  болит плечо  "}
    r = await client.put("/api/settings", json={"profile": body}, headers=auth)
    assert r.status_code == 200
    expected = {"weightKg": 82.5, "heightCm": 180, "birthYear": 1998, "goal": "mass", "about": "болит плечо"}
    assert r.json()["profile"] == expected
    assert await _profile(client, auth) == expected


async def test_put_profile_is_partial(client, auth):
    full = {"weightKg": 80, "heightCm": 180, "birthYear": 1998, "goal": "cut", "about": "заметка"}
    await client.put("/api/settings", json={"profile": full}, headers=auth)
    await client.put("/api/settings", json={"profile": {"heightCm": 181}}, headers=auth)
    assert await _profile(client, auth) == {**full, "weightKg": 80.0, "heightCm": 181}
    await client.put("/api/settings", json={"profile": {"weightKg": None}}, headers=auth)
    assert await _profile(client, auth) == {**full, "weightKg": None, "heightCm": 181}
    r = await client.put("/api/settings", json={"restSeconds": 120}, headers=auth)
    assert r.status_code == 200 and r.json()["restSeconds"] == 120
    assert await _profile(client, auth) == {**full, "weightKg": None, "heightCm": 181}


async def test_put_profile_blank_about_stores_null(client, auth):
    await client.put("/api/settings", json={"profile": {"about": "что-то"}}, headers=auth)
    r = await client.put("/api/settings", json={"profile": {"about": "   "}}, headers=auth)
    assert r.status_code == 200 and r.json()["profile"]["about"] is None
    assert (await _profile(client, auth))["about"] is None


def _bad_profiles() -> list[dict]:
    year = datetime.now(UTC).year
    return [
        {"weightKg": 29.9}, {"weightKg": 300.1},
        {"heightCm": 119}, {"heightCm": 251},
        {"birthYear": 1929}, {"birthYear": year - 9},
        {"goal": "bulk"}, {"about": "x" * 501},
    ]


@pytest.mark.parametrize("bad", _bad_profiles(), ids=lambda b: f"{next(iter(b))}={str(next(iter(b.values())))[:6]}")
async def test_put_profile_out_of_range_422_and_nothing_changed(client, auth, bad):
    before = await _profile(client, auth)
    r = await client.put("/api/settings", json={"profile": bad}, headers=auth)
    assert r.status_code == 422
    assert await _profile(client, auth) == before


def _good_profiles() -> list[dict]:
    year = datetime.now(UTC).year
    return [
        {"weightKg": 30}, {"weightKg": 300},
        {"heightCm": 120}, {"heightCm": 250},
        {"birthYear": 1930}, {"birthYear": year - 10},
        {"about": "x" * 500},
        *({"goal": g} for g in ("mass", "cut", "strength", "health")),
    ]


@pytest.mark.parametrize("good", _good_profiles(), ids=lambda b: f"{next(iter(b))}={str(next(iter(b.values())))[:6]}")
async def test_put_profile_boundaries_ok(client, auth, good):
    r = await client.put("/api/settings", json={"profile": good}, headers=auth)
    assert r.status_code == 200
    key, value = next(iter(good.items()))
    assert r.json()["profile"][key] == value
    assert (await _profile(client, auth))[key] == value


# ---- reminders: weekday and kind=advice ----


async def _mk(client, auth, **body) -> dict:
    body = {"time": "09:30", "kind": "text", "text": "Креатин", **body}
    r = await client.post("/api/reminders", json=body, headers=auth)
    assert r.status_code == 201, r.text
    return r.json()


async def test_reminder_advice_ignores_text_and_keeps_weekday(client, auth):
    r = await client.post(
        "/api/reminders", json={"time": "10:00", "kind": "advice", "text": "игнор", "weekday": 6}, headers=auth
    )
    assert r.status_code == 201
    out = r.json()
    assert out["kind"] == "advice" and out["text"] is None and out["weekday"] == 6
    assert (await client.get("/api/reminders", headers=auth)).json() == [out]


async def test_reminder_weekday_defaults_to_every_day(client, auth):
    assert (await _mk(client, auth))["weekday"] is None


@pytest.mark.parametrize("bad", [-1, 7, "mon"])
async def test_reminder_weekday_invalid_422(client, auth, bad):
    r = await client.post(
        "/api/reminders", json={"time": "09:30", "kind": "text", "text": "x", "weekday": bad}, headers=auth
    )
    assert r.status_code == 422
    assert (await client.get("/api/reminders", headers=auth)).json() == []
    created = await _mk(client, auth, weekday=3)
    r = await client.patch(f"/api/reminders/{created['id']}", json={"weekday": bad}, headers=auth)
    assert r.status_code == 422
    assert (await client.get("/api/reminders", headers=auth)).json() == [created]


async def test_reminder_patch_weekday_set_reset_keep(client, auth):
    rid = (await _mk(client, auth))["id"]
    url = f"/api/reminders/{rid}"
    assert (await client.patch(url, json={"weekday": 2}, headers=auth)).json()["weekday"] == 2
    assert (await client.patch(url, json={}, headers=auth)).json()["weekday"] == 2
    assert (await client.patch(url, json={"enabled": False}, headers=auth)).json()["weekday"] == 2
    assert (await client.patch(url, json={"weekday": None}, headers=auth)).json()["weekday"] is None
    assert (await client.get("/api/reminders", headers=auth)).json()[0]["weekday"] is None


async def test_reminder_patch_text_to_advice_drops_text(client, auth):
    rid = (await _mk(client, auth))["id"]
    r = await client.patch(f"/api/reminders/{rid}", json={"kind": "advice"}, headers=auth)
    assert r.status_code == 200 and r.json()["kind"] == "advice" and r.json()["text"] is None


async def test_reminder_patch_advice_to_text_without_text_422(client, auth):
    rid = (await _mk(client, auth, kind="advice", text=None))["id"]
    assert (await client.patch(f"/api/reminders/{rid}", json={"kind": "text"}, headers=auth)).status_code == 422
    after = (await client.get("/api/reminders", headers=auth)).json()[0]
    assert after["kind"] == "advice" and after["text"] is None
    ok = await client.patch(f"/api/reminders/{rid}", json={"kind": "text", "text": "Вода"}, headers=auth)
    assert ok.status_code == 200 and ok.json()["text"] == "Вода"


async def test_reminder_patch_weekday_rearms_last_sent_on(client, auth, db, settings):
    # Changing the weekday must not fire today's already-passed time at once.
    tz = ZoneInfo(settings.timezone)
    rid = (await _mk(client, auth, time="00:00", kind="nutrition", text=None))["id"]
    async with db() as s:
        r = await s.get(Reminder, rid)
        r.last_sent_on = None
        await s.commit()
    before = datetime.now(tz).date()
    assert (await client.patch(f"/api/reminders/{rid}", json={"weekday": 3}, headers=auth)).status_code == 200
    after = datetime.now(tz).date()
    async with db() as s:
        assert await s.scalar(select(Reminder.last_sent_on).where(Reminder.id == rid)) in (before, after)


# ---- reminders: kind=checkin ----


async def test_reminder_checkin_ignores_text(client, auth):
    r = await client.post("/api/reminders", json={"time": "08:00", "kind": "checkin", "text": "игнор"}, headers=auth)
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["kind"] == "checkin" and out["text"] is None and out["time"] == "08:00"
    assert (await client.get("/api/reminders", headers=auth)).json() == [out]


async def test_reminder_checkin_without_text_ok(client, auth):
    out = await _mk(client, auth, kind="checkin", text=None)
    assert out["kind"] == "checkin" and out["text"] is None


async def test_reminder_patch_text_to_checkin_drops_text(client, auth):
    rid = (await _mk(client, auth))["id"]
    r = await client.patch(f"/api/reminders/{rid}", json={"kind": "checkin"}, headers=auth)
    assert r.status_code == 200 and r.json()["kind"] == "checkin" and r.json()["text"] is None
    assert (await client.get("/api/reminders", headers=auth)).json() == [r.json()]


# ---- wellbeing: GET /api/wellbeing, DELETE /api/wellbeing/{id} ----

WB_KEYS = {"id", "notedAt", "date", "sleepHours", "sleepQuality", "energy", "mood", "pains", "note"}


async def _wb_user(client, auth, db, telegram_id: int = 42) -> int:
    """Make sure the user exists (the first authorised request creates it) and return its id."""
    assert (await client.get("/api/state", headers=auth)).status_code == 200
    from gymbot.db.models import User

    async with db() as s:
        return await s.scalar(select(User.id).where(User.telegram_id == telegram_id))


async def _wb(db, user_id: int, noted_at: datetime, raw_text: str = "сон 7ч", **fields) -> int:
    from gymbot.db.models import WellbeingEntry

    async with db() as s:
        e = WellbeingEntry(user_id=user_id, noted_at=noted_at, raw_text=raw_text, **fields)
        s.add(e)
        await s.commit()
        return e.id


async def _wb_exists(db, entry_id: int) -> bool:
    from gymbot.db.models import WellbeingEntry

    async with db() as s:
        return await s.scalar(select(WellbeingEntry.id).where(WellbeingEntry.id == entry_id)) is not None


def _local_noon_utc(tz: ZoneInfo, days_ago: int) -> datetime:
    day = datetime.now(tz).date() - timedelta(days=days_ago)
    return datetime(day.year, day.month, day.day, 12, tzinfo=tz).astimezone(UTC)


async def test_wellbeing_requires_auth(client):
    assert (await client.get("/api/wellbeing")).status_code == 401
    assert (await client.delete("/api/wellbeing/1")).status_code == 401
    bad = {"X-Telegram-Init-Data": init_data(token="999:other")}
    assert (await client.get("/api/wellbeing", headers=bad)).status_code == 401


async def test_wellbeing_empty_for_new_user(client, auth):
    r = await client.get("/api/wellbeing", headers=auth)
    assert r.status_code == 200 and r.json() == []


async def test_wellbeing_entry_shape(client, auth, db):
    uid = await _wb_user(client, auth, db)
    noted = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=1)
    eid = await _wb(
        db, uid, noted, raw_text="спал 7.5, плечо болит, бодрый",
        sleep_hours=Decimal("7.5"), sleep_quality=4, energy=5, mood=3,
        pains=json.dumps([{"place": "плечо", "severity": 3}], ensure_ascii=False), note="бодрый",
    )
    data = (await client.get("/api/wellbeing", headers=auth)).json()
    assert len(data) == 1
    e = data[0]
    assert set(e) == WB_KEYS
    assert e["id"] == eid
    assert e["sleepHours"] == 7.5 and isinstance(e["sleepHours"], float)
    assert (e["sleepQuality"], e["energy"], e["mood"]) == (4, 5, 3)
    assert e["pains"] == [{"place": "плечо", "severity": 3}]
    assert e["note"] == "бодрый"
    parsed = datetime.fromisoformat(e["notedAt"])
    assert parsed.utcoffset() == timedelta(0)
    assert parsed == noted
    assert e["date"] == noted.astimezone(ZoneInfo("Europe/Moscow")).date().isoformat()


async def test_wellbeing_nullable_fields(client, auth, db):
    uid = await _wb_user(client, auth, db)
    await _wb(db, uid, datetime.now(UTC) - timedelta(minutes=5))
    e = (await client.get("/api/wellbeing", headers=auth)).json()[0]
    assert set(e) == WB_KEYS
    assert e["sleepHours"] is None and e["sleepQuality"] is None
    assert e["energy"] is None and e["mood"] is None and e["note"] is None
    assert e["pains"] == []


async def test_wellbeing_sorted_newest_first_ties_by_id_desc(client, auth, db):
    uid = await _wb_user(client, auth, db)
    now = datetime.now(UTC).replace(microsecond=0)
    old = await _wb(db, uid, now - timedelta(days=2))
    tie_a = await _wb(db, uid, now - timedelta(days=1))
    tie_b = await _wb(db, uid, now - timedelta(days=1))
    newest = await _wb(db, uid, now - timedelta(hours=1))
    # Inserted last, but older than the tied pair: the order follows notedAt, not id.
    mid = await _wb(db, uid, now - timedelta(hours=30))
    data = (await client.get("/api/wellbeing", headers=auth)).json()
    assert [e["id"] for e in data] == [newest, tie_b, tie_a, mid, old]


async def test_wellbeing_only_current_users_entries(tmp_path, make_client, db):
    one = {"X-Telegram-Init-Data": init_data(42)}
    two = {"X-Telegram-Init-Data": init_data(77)}
    async with make_client(make_settings(tmp_path, allowed_user_ids=[42, 77])) as c:
        u1 = await _wb_user(c, one, db, 42)
        u2 = await _wb_user(c, two, db, 77)
        now = datetime.now(UTC)
        mine = await _wb(db, u1, now - timedelta(hours=1), raw_text="моё")
        theirs = await _wb(db, u2, now - timedelta(hours=2), raw_text="чужое")
        assert [e["id"] for e in (await c.get("/api/wellbeing", headers=one)).json()] == [mine]
        assert [e["id"] for e in (await c.get("/api/wellbeing", headers=two)).json()] == [theirs]


async def test_wellbeing_window_excludes_old_entries(client, auth, db):
    uid = await _wb_user(client, auth, db)
    now = datetime.now(UTC)
    recent = await _wb(db, uid, now - timedelta(hours=1))
    old = await _wb(db, uid, now - timedelta(days=20))
    default = (await client.get("/api/wellbeing", headers=auth)).json()  # days defaults to 14
    assert [e["id"] for e in default] == [recent]
    short = (await client.get("/api/wellbeing?days=14", headers=auth)).json()
    assert [e["id"] for e in short] == [recent]
    longer = (await client.get("/api/wellbeing?days=30", headers=auth)).json()
    assert [e["id"] for e in longer] == [recent, old]


async def test_wellbeing_window_counts_local_days_including_today(client, auth, db, settings):
    tz = ZoneInfo(settings.timezone)
    uid = await _wb_user(client, auth, db)
    last_in = await _wb(db, uid, _local_noon_utc(tz, 13))  # 14th local day counting today
    first_out = await _wb(db, uid, _local_noon_utc(tz, 14))
    ids = [e["id"] for e in (await client.get("/api/wellbeing?days=14", headers=auth)).json()]
    assert ids == [last_in]
    ids = [e["id"] for e in (await client.get("/api/wellbeing?days=15", headers=auth)).json()]
    assert ids == [last_in, first_out]


async def test_wellbeing_days_1_is_only_today(client, auth, db, settings):
    tz = ZoneInfo(settings.timezone)
    uid = await _wb_user(client, auth, db)
    await _wb(db, uid, _local_noon_utc(tz, 1))
    now = datetime.now(UTC).replace(microsecond=0)
    today = await _wb(db, uid, now)
    data = (await client.get("/api/wellbeing?days=1", headers=auth)).json()
    assert [e["id"] for e in data] == [today]
    assert data[0]["date"] == now.astimezone(tz).date().isoformat()


@pytest.mark.parametrize(
    "tz_name,utc_hm,day_shift",
    [
        ("Europe/Moscow", (21, 30), 1),  # 21:30 UTC is already 00:30 of the next day in Moscow
        ("America/Los_Angeles", (3, 0), -1),  # 03:00 UTC is still the previous evening in Los Angeles
        ("UTC", (21, 30), 0),
    ],
)
async def test_wellbeing_date_is_local_day_of_noted_at(tmp_path, make_client, db, tz_name, utc_hm, day_shift):
    utc_day = datetime.now(UTC).date() - timedelta(days=4)  # well inside the default window in any zone
    noted = datetime(utc_day.year, utc_day.month, utc_day.day, *utc_hm, tzinfo=UTC)
    auth = {"X-Telegram-Init-Data": init_data()}
    async with make_client(make_settings(tmp_path, timezone=tz_name)) as c:
        uid = await _wb_user(c, auth, db)
        await _wb(db, uid, noted)
        e = (await c.get("/api/wellbeing", headers=auth)).json()[0]
    assert e["date"] == (utc_day + timedelta(days=day_shift)).isoformat()
    parsed = datetime.fromisoformat(e["notedAt"])
    assert parsed.utcoffset() == timedelta(0) and parsed == noted  # notedAt stays UTC


async def test_wellbeing_pains_parsed(client, auth, db):
    uid = await _wb_user(client, auth, db)
    pains = [{"place": "плечо", "severity": 3}, {"place": "колено", "severity": None}]
    await _wb(db, uid, datetime.now(UTC) - timedelta(hours=1), pains=json.dumps(pains, ensure_ascii=False))
    e = (await client.get("/api/wellbeing", headers=auth)).json()[0]
    assert e["pains"] == pains


@pytest.mark.parametrize("bad", [None, "", "not json{", '[{"place": "плечо"'])
async def test_wellbeing_missing_or_malformed_pains_is_empty_list(client, auth, db, bad):
    uid = await _wb_user(client, auth, db)
    await _wb(db, uid, datetime.now(UTC) - timedelta(hours=1), pains=bad)
    r = await client.get("/api/wellbeing", headers=auth)
    assert r.status_code == 200
    assert r.json()[0]["pains"] == []


@pytest.mark.parametrize("days", [0, -1, 367, 400])
async def test_wellbeing_days_out_of_range_422(client, auth, days):
    assert (await client.get(f"/api/wellbeing?days={days}", headers=auth)).status_code == 422


@pytest.mark.parametrize("days", [1, 366])
async def test_wellbeing_days_boundaries_ok(client, auth, days):
    assert (await client.get(f"/api/wellbeing?days={days}", headers=auth)).status_code == 200


async def test_wellbeing_days_not_a_number_422(client, auth):
    assert (await client.get("/api/wellbeing?days=abc", headers=auth)).status_code == 422


async def test_wellbeing_delete_own(client, auth, db):
    uid = await _wb_user(client, auth, db)
    now = datetime.now(UTC)
    keep = await _wb(db, uid, now - timedelta(hours=1))
    gone = await _wb(db, uid, now - timedelta(hours=2))
    assert (await client.delete(f"/api/wellbeing/{gone}", headers=auth)).status_code == 204
    assert [e["id"] for e in (await client.get("/api/wellbeing", headers=auth)).json()] == [keep]
    assert not await _wb_exists(db, gone) and await _wb_exists(db, keep)
    assert (await client.delete(f"/api/wellbeing/{gone}", headers=auth)).status_code == 404


async def test_wellbeing_delete_other_users_entry_404(tmp_path, make_client, db):
    one = {"X-Telegram-Init-Data": init_data(42)}
    two = {"X-Telegram-Init-Data": init_data(77)}
    async with make_client(make_settings(tmp_path, allowed_user_ids=[42, 77])) as c:
        u1 = await _wb_user(c, one, db, 42)
        await _wb_user(c, two, db, 77)
        eid = await _wb(db, u1, datetime.now(UTC) - timedelta(hours=1))
        assert (await c.delete(f"/api/wellbeing/{eid}", headers=two)).status_code == 404
        assert await _wb_exists(db, eid)
        assert [e["id"] for e in (await c.get("/api/wellbeing", headers=one)).json()] == [eid]
        assert (await c.delete(f"/api/wellbeing/{eid}", headers=one)).status_code == 204


async def test_wellbeing_delete_unknown_id_404(client, auth, db):
    uid = await _wb_user(client, auth, db)
    eid = await _wb(db, uid, datetime.now(UTC) - timedelta(hours=1))
    assert (await client.delete("/api/wellbeing/9999", headers=auth)).status_code == 404
    assert await _wb_exists(db, eid)


# ---- facts: GET/POST /api/facts, PATCH/DELETE /api/facts/{id} ----

FACT_KEYS = {"id", "text", "category", "createdAt", "active"}
FACT_LIMIT = 50


async def _fact(db, user_id: int, text: str = "не ем творог", category: str = "other", active: bool = True, created_at=None) -> int:
    from gymbot.db.models import UserFact

    async with db() as s:
        f = UserFact(
            user_id=user_id, text=text, category=category, active=active,
            created_at=created_at or datetime.now(UTC),
        )
        s.add(f)
        await s.commit()
        return f.id


async def _fill_facts(db, user_id: int, n: int = FACT_LIMIT, active: bool = True) -> list[int]:
    from gymbot.db.models import UserFact

    async with db() as s:
        rows = [UserFact(user_id=user_id, text=f"факт {i}", category="other", active=active, created_at=datetime.now(UTC)) for i in range(n)]
        s.add_all(rows)
        await s.commit()
        return [r.id for r in rows]


async def _fact_rows(db, user_id: int | None = None) -> list:
    from gymbot.db.models import UserFact

    async with db() as s:
        q = select(UserFact).order_by(UserFact.id)
        if user_id is not None:
            q = q.where(UserFact.user_id == user_id)
        return list((await s.scalars(q)).all())


async def _fact_exists(db, fact_id: int) -> bool:
    from gymbot.db.models import UserFact

    async with db() as s:
        return await s.scalar(select(UserFact.id).where(UserFact.id == fact_id)) is not None


async def test_facts_require_auth(client):
    assert (await client.get("/api/facts")).status_code == 401
    assert (await client.post("/api/facts", json={"text": "x"})).status_code == 401
    assert (await client.patch("/api/facts/1", json={"text": "x"})).status_code == 401
    assert (await client.delete("/api/facts/1")).status_code == 401
    bad = {"X-Telegram-Init-Data": init_data(token="999:other")}
    assert (await client.get("/api/facts", headers=bad)).status_code == 401


async def test_facts_empty_for_new_user(client, auth):
    r = await client.get("/api/facts", headers=auth)
    assert r.status_code == 200 and r.json() == []


async def test_fact_create_defaults_and_shape(client, auth):
    r = await client.post("/api/facts", json={"text": "не ем творог"}, headers=auth)
    assert r.status_code == 201
    f = r.json()
    assert set(f) == FACT_KEYS
    assert f["text"] == "не ем творог" and f["category"] == "other" and f["active"] is True
    assert isinstance(f["id"], int)
    created = datetime.fromisoformat(f["createdAt"])
    assert created.utcoffset() == timedelta(0)
    assert abs(datetime.now(UTC) - created) < timedelta(minutes=5)
    assert (await client.get("/api/facts", headers=auth)).json() == [f]


@pytest.mark.parametrize("category", ["food", "training", "health", "schedule", "other"])
async def test_fact_create_every_category(client, auth, category):
    r = await client.post("/api/facts", json={"text": "факт", "category": category}, headers=auth)
    assert r.status_code == 201 and r.json()["category"] == category


async def test_fact_create_unknown_category_422(client, auth):
    r = await client.post("/api/facts", json={"text": "факт", "category": "sport"}, headers=auth)
    assert r.status_code == 422
    assert (await client.get("/api/facts", headers=auth)).json() == []


@pytest.mark.parametrize("payload", [{}, {"text": None}, {"text": 5}, {"text": "x", "category": None}])
async def test_fact_create_invalid_body_422(client, auth, payload):
    assert (await client.post("/api/facts", json=payload, headers=auth)).status_code == 422
    assert (await client.get("/api/facts", headers=auth)).json() == []


async def test_fact_create_cleans_text(client, auth):
    r = await client.post("/api/facts", json={"text": "  не ем   творог "}, headers=auth)
    assert r.status_code == 201 and r.json()["text"] == "не ем творог"
    r = await client.post("/api/facts", json={"text": "\tнет\n\nлактозы  "}, headers=auth)
    assert r.status_code == 201 and r.json()["text"] == "нет лактозы"


@pytest.mark.parametrize("text", ["", "   ", "\n\t "])
async def test_fact_create_empty_text_422(client, auth, text):
    assert (await client.post("/api/facts", json={"text": text}, headers=auth)).status_code == 422
    assert (await client.get("/api/facts", headers=auth)).json() == []


async def test_fact_create_text_length_edges(client, auth):
    assert (await client.post("/api/facts", json={"text": "я" * 201}, headers=auth)).status_code == 422
    assert (await client.get("/api/facts", headers=auth)).json() == []
    assert (await client.post("/api/facts", json={"text": "я" * 200}, headers=auth)).status_code == 201
    # The limit applies after cleaning: padding does not count.
    assert (await client.post("/api/facts", json={"text": "  " + "ю" * 200 + "  "}, headers=auth)).status_code == 201
    # 201 chars even if some of them are collapsible whitespace that remains one space.
    assert (await client.post("/api/facts", json={"text": "а " + "б" * 199}, headers=auth)).status_code == 422
    # Collapsing whitespace brings this one down from 202 to exactly 200.
    assert (await client.post("/api/facts", json={"text": "а  " + "в" * 198}, headers=auth)).status_code == 201


async def test_fact_list_includes_inactive_sorted_newest_first_ties_by_id_desc(client, auth, db):
    uid = await _wb_user(client, auth, db)
    now = datetime.now(UTC).replace(microsecond=0)
    old = await _fact(db, uid, "старый", created_at=now - timedelta(days=2))
    tie_a = await _fact(db, uid, "а", created_at=now - timedelta(days=1))
    tie_b = await _fact(db, uid, "б", active=False, created_at=now - timedelta(days=1))
    newest = await _fact(db, uid, "новый", created_at=now - timedelta(hours=1))
    # Inserted last, but older than the tied pair: the order follows createdAt, not id.
    mid = await _fact(db, uid, "средний", active=False, created_at=now - timedelta(hours=30))
    data = (await client.get("/api/facts", headers=auth)).json()
    assert [f["id"] for f in data] == [newest, tie_b, tie_a, mid, old]
    assert all(set(f) == FACT_KEYS for f in data)
    assert {f["id"]: f["active"] for f in data} == {newest: True, tie_b: False, tie_a: True, mid: False, old: True}
    assert datetime.fromisoformat(data[0]["createdAt"]).utcoffset() == timedelta(0)


async def test_facts_only_current_users_facts(tmp_path, make_client, db):
    one = {"X-Telegram-Init-Data": init_data(42)}
    two = {"X-Telegram-Init-Data": init_data(77)}
    async with make_client(make_settings(tmp_path, allowed_user_ids=[42, 77])) as c:
        u1 = await _wb_user(c, one, db, 42)
        u2 = await _wb_user(c, two, db, 77)
        mine = await _fact(db, u1, "моё")
        theirs = await _fact(db, u2, "чужое", active=False)
        assert [f["id"] for f in (await c.get("/api/facts", headers=one)).json()] == [mine]
        assert [f["id"] for f in (await c.get("/api/facts", headers=two)).json()] == [theirs]


async def test_fact_create_duplicate_active_returns_existing_200(client, auth, db):
    first = (await client.post("/api/facts", json={"text": "Не ем творог", "category": "food"}, headers=auth)).json()
    r = await client.post("/api/facts", json={"text": "не ем творог", "category": "health"}, headers=auth)
    assert r.status_code == 200
    assert r.json() == first
    assert len(await _fact_rows(db)) == 1
    assert len((await client.get("/api/facts", headers=auth)).json()) == 1


@pytest.mark.parametrize(
    "dup",
    [
        "НЕ ЕМ ТВОРОГ",  # case
        "не ем творог.",  # trailing period
        "не ем творог!!",  # trailing exclamation
        "«не ем творог»",  # guillemets
        '"не ем творог"',  # straight quotes
        "  не   ем творог  ",  # whitespace
        "«Не ем творог».",  # quotes plus period
    ],
)
async def test_fact_create_duplicate_normalisation(client, auth, db, dup):
    first = (await client.post("/api/facts", json={"text": "не ем творог"}, headers=auth)).json()
    r = await client.post("/api/facts", json={"text": dup}, headers=auth)
    assert r.status_code == 200 and r.json()["id"] == first["id"]
    assert len(await _fact_rows(db)) == 1


async def test_fact_create_duplicate_yo_equals_ye(client, auth, db):
    first = (await client.post("/api/facts", json={"text": "Всё без глютена"}, headers=auth)).json()
    r = await client.post("/api/facts", json={"text": "все без глютена"}, headers=auth)
    assert r.status_code == 200 and r.json()["id"] == first["id"]
    assert len(await _fact_rows(db)) == 1


async def test_fact_create_not_a_duplicate_if_text_differs(client, auth, db):
    await client.post("/api/facts", json={"text": "не ем творог"}, headers=auth)
    r = await client.post("/api/facts", json={"text": "не ем сыр"}, headers=auth)
    assert r.status_code == 201
    assert len(await _fact_rows(db)) == 2


async def test_fact_create_duplicate_of_inactive_is_new(client, auth, db):
    uid = await _wb_user(client, auth, db)
    old = await _fact(db, uid, "не ем творог", active=False)
    r = await client.post("/api/facts", json={"text": "Не ем творог."}, headers=auth)
    assert r.status_code == 201 and r.json()["id"] != old and r.json()["active"] is True
    assert len(await _fact_rows(db)) == 2


async def test_fact_create_duplicate_of_other_users_fact_is_new(tmp_path, make_client, db):
    one = {"X-Telegram-Init-Data": init_data(42)}
    two = {"X-Telegram-Init-Data": init_data(77)}
    async with make_client(make_settings(tmp_path, allowed_user_ids=[42, 77])) as c:
        u1 = await _wb_user(c, one, db, 42)
        await _wb_user(c, two, db, 77)
        theirs = await _fact(db, u1, "не ем творог")
        r = await c.post("/api/facts", json={"text": "не ем творог"}, headers=two)
        assert r.status_code == 201 and r.json()["id"] != theirs
        assert len(await _fact_rows(db)) == 2


async def test_fact_limit_50_then_409(client, auth, db):
    uid = await _wb_user(client, auth, db)
    await _fill_facts(db, uid, FACT_LIMIT)
    r = await client.post("/api/facts", json={"text": "пятьдесят первый"}, headers=auth)
    assert r.status_code == 409
    assert len(await _fact_rows(db)) == FACT_LIMIT


async def test_fact_limit_49_still_allows_one(client, auth, db):
    uid = await _wb_user(client, auth, db)
    await _fill_facts(db, uid, FACT_LIMIT - 1)
    assert (await client.post("/api/facts", json={"text": "последний"}, headers=auth)).status_code == 201
    assert (await client.post("/api/facts", json={"text": "лишний"}, headers=auth)).status_code == 409


async def test_fact_limit_ignores_inactive(client, auth, db):
    uid = await _wb_user(client, auth, db)
    await _fill_facts(db, uid, FACT_LIMIT, active=False)
    r = await client.post("/api/facts", json={"text": "новый активный"}, headers=auth)
    assert r.status_code == 201


async def test_fact_limit_is_per_user(tmp_path, make_client, db):
    one = {"X-Telegram-Init-Data": init_data(42)}
    two = {"X-Telegram-Init-Data": init_data(77)}
    async with make_client(make_settings(tmp_path, allowed_user_ids=[42, 77])) as c:
        u1 = await _wb_user(c, one, db, 42)
        await _wb_user(c, two, db, 77)
        await _fill_facts(db, u1, FACT_LIMIT)
        assert (await c.post("/api/facts", json={"text": "ещё"}, headers=one)).status_code == 409
        assert (await c.post("/api/facts", json={"text": "ещё"}, headers=two)).status_code == 201


async def test_fact_limit_frees_after_delete_and_deactivate(client, auth, db):
    uid = await _wb_user(client, auth, db)
    ids = await _fill_facts(db, uid, FACT_LIMIT)
    assert (await client.post("/api/facts", json={"text": "новый"}, headers=auth)).status_code == 409
    assert (await client.delete(f"/api/facts/{ids[0]}", headers=auth)).status_code == 204
    assert (await client.post("/api/facts", json={"text": "новый"}, headers=auth)).status_code == 201
    assert (await client.post("/api/facts", json={"text": "ещё один"}, headers=auth)).status_code == 409
    assert (await client.patch(f"/api/facts/{ids[1]}", json={"active": False}, headers=auth)).status_code == 200
    assert (await client.post("/api/facts", json={"text": "ещё один"}, headers=auth)).status_code == 201


async def test_fact_duplicate_at_limit_returns_existing_not_409(client, auth, db):
    uid = await _wb_user(client, auth, db)
    ids = await _fill_facts(db, uid, FACT_LIMIT)
    r = await client.post("/api/facts", json={"text": "Факт 7"}, headers=auth)
    assert r.status_code == 200 and r.json()["id"] == ids[7]


async def test_fact_patch_changes_only_given_fields(client, auth):
    f = (await client.post("/api/facts", json={"text": "не ем творог", "category": "food"}, headers=auth)).json()
    r = await client.patch(f"/api/facts/{f['id']}", json={"category": "health"}, headers=auth)
    assert r.status_code == 200
    assert r.json() == {**f, "category": "health"}
    r = await client.patch(f"/api/facts/{f['id']}", json={"text": "  не ем   сыр "}, headers=auth)
    assert r.status_code == 200 and r.json() == {**f, "category": "health", "text": "не ем сыр"}
    r = await client.patch(f"/api/facts/{f['id']}", json={"active": False}, headers=auth)
    assert r.status_code == 200 and r.json() == {**f, "category": "health", "text": "не ем сыр", "active": False}
    # An empty body keeps everything.
    r = await client.patch(f"/api/facts/{f['id']}", json={}, headers=auth)
    assert r.status_code == 200 and r.json() == {**f, "category": "health", "text": "не ем сыр", "active": False}
    assert (await client.get("/api/facts", headers=auth)).json() == [r.json()]


async def test_fact_patch_invalid_values_422_and_nothing_changed(client, auth):
    f = (await client.post("/api/facts", json={"text": "не ем творог", "category": "food"}, headers=auth)).json()
    for body in (
        {"text": ""},
        {"text": "   "},
        {"text": "я" * 201},
        {"category": "sport"},
        {"active": "maybe"},
        {"text": "x", "category": "sport"},  # one bad field: no partial update
    ):
        assert (await client.patch(f"/api/facts/{f['id']}", json=body, headers=auth)).status_code == 422, body
    assert (await client.get("/api/facts", headers=auth)).json() == [f]


async def test_fact_patch_text_length_200_ok(client, auth):
    f = (await client.post("/api/facts", json={"text": "факт"}, headers=auth)).json()
    r = await client.patch(f"/api/facts/{f['id']}", json={"text": "  " + "я" * 200 + " "}, headers=auth)
    assert r.status_code == 200 and r.json()["text"] == "я" * 200


async def test_fact_patch_text_to_duplicate_of_other_active_422(client, auth):
    a = (await client.post("/api/facts", json={"text": "не ем творог"}, headers=auth)).json()
    b = (await client.post("/api/facts", json={"text": "не ем сыр"}, headers=auth)).json()
    r = await client.patch(f"/api/facts/{b['id']}", json={"text": "НЕ ЕМ ТВОРОГ."}, headers=auth)
    assert r.status_code == 422
    assert {f["id"]: f["text"] for f in (await client.get("/api/facts", headers=auth)).json()} == {
        a["id"]: "не ем творог",
        b["id"]: "не ем сыр",
    }


async def test_fact_patch_text_to_duplicate_of_inactive_ok(client, auth, db):
    uid = await _wb_user(client, auth, db)
    await _fact(db, uid, "не ем творог", active=False)
    b = (await client.post("/api/facts", json={"text": "не ем сыр"}, headers=auth)).json()
    r = await client.patch(f"/api/facts/{b['id']}", json={"text": "не ем творог"}, headers=auth)
    assert r.status_code == 200 and r.json()["text"] == "не ем творог"


async def test_fact_patch_text_to_own_text_with_other_case_ok(client, auth):
    # A fact is not a duplicate of itself.
    f = (await client.post("/api/facts", json={"text": "не ем творог"}, headers=auth)).json()
    r = await client.patch(f"/api/facts/{f['id']}", json={"text": "Не ем творог."}, headers=auth)
    assert r.status_code == 200 and r.json()["text"] == "Не ем творог."


async def test_fact_patch_text_to_duplicate_of_other_users_fact_ok(tmp_path, make_client, db):
    one = {"X-Telegram-Init-Data": init_data(42)}
    two = {"X-Telegram-Init-Data": init_data(77)}
    async with make_client(make_settings(tmp_path, allowed_user_ids=[42, 77])) as c:
        u1 = await _wb_user(c, one, db, 42)
        await _wb_user(c, two, db, 77)
        await _fact(db, u1, "не ем творог")
        mine = (await c.post("/api/facts", json={"text": "не ем сыр"}, headers=two)).json()
        r = await c.patch(f"/api/facts/{mine['id']}", json={"text": "не ем творог"}, headers=two)
        assert r.status_code == 200


async def test_fact_patch_reactivate_at_limit_409(client, auth, db):
    uid = await _wb_user(client, auth, db)
    await _fill_facts(db, uid, FACT_LIMIT)
    off = await _fact(db, uid, "выключенный", active=False)
    r = await client.patch(f"/api/facts/{off}", json={"active": True}, headers=auth)
    assert r.status_code == 409
    rows = {f.id: f for f in await _fact_rows(db, uid)}
    assert rows[off].active is False


async def test_fact_patch_reactivate_below_limit_ok(client, auth, db):
    uid = await _wb_user(client, auth, db)
    await _fill_facts(db, uid, FACT_LIMIT - 1)
    off = await _fact(db, uid, "выключенный", active=False)
    r = await client.patch(f"/api/facts/{off}", json={"active": True}, headers=auth)
    assert r.status_code == 200 and r.json()["active"] is True


async def test_fact_patch_edit_active_fact_at_limit_ok(client, auth, db):
    uid = await _wb_user(client, auth, db)
    ids = await _fill_facts(db, uid, FACT_LIMIT)
    r = await client.patch(f"/api/facts/{ids[3]}", json={"text": "изменённый", "category": "food"}, headers=auth)
    assert r.status_code == 200 and r.json()["text"] == "изменённый" and r.json()["category"] == "food"
    # active: true on an already active fact is not a re-activation.
    r = await client.patch(f"/api/facts/{ids[4]}", json={"active": True}, headers=auth)
    assert r.status_code == 200 and r.json()["active"] is True


async def test_fact_patch_edit_inactive_fact_at_limit_ok(client, auth, db):
    uid = await _wb_user(client, auth, db)
    await _fill_facts(db, uid, FACT_LIMIT)
    off = await _fact(db, uid, "выключенный", active=False)
    r = await client.patch(f"/api/facts/{off}", json={"text": "всё ещё выключенный", "category": "health"}, headers=auth)
    assert r.status_code == 200 and r.json()["active"] is False and r.json()["category"] == "health"


async def test_fact_patch_deactivate_at_limit_ok(client, auth, db):
    uid = await _wb_user(client, auth, db)
    ids = await _fill_facts(db, uid, FACT_LIMIT)
    r = await client.patch(f"/api/facts/{ids[0]}", json={"active": False}, headers=auth)
    assert r.status_code == 200 and r.json()["active"] is False


async def test_fact_patch_reactivate_duplicate_of_active_422(client, auth, db):
    uid = await _wb_user(client, auth, db)
    await _fact(db, uid, "не ем творог")
    off = await _fact(db, uid, "Не ем творог.", active=False)
    r = await client.patch(f"/api/facts/{off}", json={"active": True}, headers=auth)
    assert r.status_code == 422
    rows = {f.id: f for f in await _fact_rows(db, uid)}
    assert rows[off].active is False


async def test_fact_patch_other_user_404(tmp_path, make_client, db):
    one = {"X-Telegram-Init-Data": init_data(42)}
    two = {"X-Telegram-Init-Data": init_data(77)}
    async with make_client(make_settings(tmp_path, allowed_user_ids=[42, 77])) as c:
        mine = (await c.post("/api/facts", json={"text": "моё", "category": "food"}, headers=one)).json()
        assert (await c.get("/api/state", headers=two)).status_code == 200
        r = await c.patch(f"/api/facts/{mine['id']}", json={"text": "взлом", "active": False}, headers=two)
        assert r.status_code == 404
        assert (await c.get("/api/facts", headers=one)).json() == [mine]
        assert (await c.get("/api/facts", headers=two)).json() == []


async def test_fact_patch_unknown_id_404(client, auth):
    assert (await client.patch("/api/facts/9999", json={"text": "x"}, headers=auth)).status_code == 404


async def test_fact_delete_own(client, auth, db):
    uid = await _wb_user(client, auth, db)
    keep = await _fact(db, uid, "оставить")
    gone = await _fact(db, uid, "удалить", active=False)
    r = await client.delete(f"/api/facts/{gone}", headers=auth)
    assert r.status_code == 204 and r.content == b""
    assert not await _fact_exists(db, gone) and await _fact_exists(db, keep)
    assert [f["id"] for f in (await client.get("/api/facts", headers=auth)).json()] == [keep]
    assert (await client.delete(f"/api/facts/{gone}", headers=auth)).status_code == 404


async def test_fact_delete_other_user_404(tmp_path, make_client, db):
    one = {"X-Telegram-Init-Data": init_data(42)}
    two = {"X-Telegram-Init-Data": init_data(77)}
    async with make_client(make_settings(tmp_path, allowed_user_ids=[42, 77])) as c:
        u1 = await _wb_user(c, one, db, 42)
        await _wb_user(c, two, db, 77)
        fid = await _fact(db, u1, "моё")
        assert (await c.delete(f"/api/facts/{fid}", headers=two)).status_code == 404
        assert await _fact_exists(db, fid)
        assert [f["id"] for f in (await c.get("/api/facts", headers=one)).json()] == [fid]
        assert (await c.delete(f"/api/facts/{fid}", headers=one)).status_code == 204


async def test_fact_delete_unknown_id_404(client, auth, db):
    uid = await _wb_user(client, auth, db)
    fid = await _fact(db, uid)
    assert (await client.delete("/api/facts/9999", headers=auth)).status_code == 404
    assert await _fact_exists(db, fid)


async def test_state_weight_overrides_default_empty(client, auth, db):
    assert (await client.get("/api/state", headers=auth)).json()["weightOverrides"] == []


async def test_state_weight_overrides_only_today_local(client, auth, db, settings):
    await client.get("/api/state", headers=auth)  # creates the user (telegram id 42)
    today = datetime.now(ZoneInfo(settings.timezone)).date()
    async with db() as s:
        uid = await s.scalar(select(User.id).where(User.telegram_id == 42))
        ex = await s.scalar(select(Exercise.id).where(Exercise.name == "жим лёжа"))
        row = await s.scalar(select(Exercise.id).where(Exercise.name == "присед со штангой"))
        s.add(WeightOverride(user_id=uid, exercise_id=ex, day=today, weight_kg=Decimal(85)))
        s.add(WeightOverride(user_id=uid, exercise_id=row, day=today - timedelta(days=1), weight_kg=Decimal(100)))
        await s.commit()
    st = (await client.get("/api/state", headers=auth)).json()
    assert st["weightOverrides"] == [{"exercise": "жим лёжа", "weightKg": 85.0, "date": today.isoformat()}]


async def test_state_weight_overrides_are_per_user(client, auth, db, settings):
    await client.get("/api/state", headers=auth)
    today = datetime.now(ZoneInfo(settings.timezone)).date()
    async with db() as s:
        other = User(telegram_id=7)
        s.add(other)
        await s.flush()
        ex = await s.scalar(select(Exercise.id).where(Exercise.name == "жим лёжа"))
        s.add(WeightOverride(user_id=other.id, exercise_id=ex, day=today, weight_kg=Decimal(70)))
        await s.commit()
    assert (await client.get("/api/state", headers=auth)).json()["weightOverrides"] == []
