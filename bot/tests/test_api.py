from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest
from conftest import init_data, make_settings
from sqlalchemy import select

from gymbot.db.models import Exercise, Reminder, Workout, WorkoutSet


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
