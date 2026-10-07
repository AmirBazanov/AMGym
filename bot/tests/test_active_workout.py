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


def now_iso(delta: timedelta = timedelta()) -> str:
    """startedAt for a workout that really is in progress now (the API and the chat use the real clock)."""
    return (datetime.now(UTC) + delta).strftime("%Y-%m-%dT%H:%M:%SZ")


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
    # 5 h 59 min later is past local midnight in MSK (see test_context_line_stale_after_local_midnight): UTC here.
    assert aw.context_line(snapshot(UPDATED), UPDATED + timedelta(hours=5, minutes=59), ZoneInfo("UTC")) != ""
    broken = ActiveWorkout(user_id=1, client_id="w1", payload="{not json", updated_at=UPDATED)
    assert aw.context_line(broken, UPDATED, MSK) == ""


def test_context_line_is_capped():
    exercises = [
        {"name": f"упражнение номер {i}", "sets": [{"weight": 100.5, "reps": 12, "done": True}] * 10}
        for i in range(20)
    ]
    line = aw.context_line(snapshot(UPDATED, exercises=exercises), UPDATED, MSK)
    assert len(line) <= aw.LINE_MAX and line.endswith("…")


def test_context_line_survives_local_midnight():
    # Started 18:40 MSK, updated 19:10; at 00:10 MSK the next day it is 5 h 30 min old: still in progress
    # (a late workout must stay visible and restorable after midnight). Stale only after STALE_AFTER.
    midnight = UPDATED + timedelta(hours=5)
    assert aw.context_line(snapshot(UPDATED), midnight, MSK) != ""
    assert aw.context_line(snapshot(UPDATED), UPDATED + timedelta(hours=6, minutes=1), MSK) == ""


async def test_answer_context_has_the_workout_in_progress(client, auth, settings, db):
    await client.get("/api/state", headers=auth)
    await client.put("/api/workouts/active", json=active(startedAt=now_iso()), headers=auth)
    tz = ZoneInfo(settings.timezone)
    now = datetime.now(UTC)
    async with db() as s:
        user = await s.scalar(select(User).where(User.telegram_id == 42))
        ctx = await answer_service.build_context(s, user, settings, None, tz, now)
        assert "жим лёжа 82.5×8, 82.5×8 (2 из 4 подходов); тяга вертикального блока — ещё не начато." in ctx
        stale = await answer_service.build_context(s, user, settings, None, tz, now + timedelta(hours=7))
        assert "Сейчас идёт тренировка" not in stale


# ---- fresh(): what counts as a workout in progress ----


def test_fresh_returns_the_workout_while_in_progress():
    data = aw.fresh(snapshot(UPDATED), UPDATED + timedelta(minutes=5), MSK)
    assert data is not None and data.id == "w1" and data.exercises[0].name == "жим лёжа"


def test_fresh_none_when_missing_stale_or_unreadable():
    assert aw.fresh(None, UPDATED, MSK) is None
    assert aw.fresh(snapshot(UPDATED), UPDATED + timedelta(hours=6, minutes=1), MSK) is None
    assert aw.fresh(snapshot(UPDATED), UPDATED + timedelta(hours=5, minutes=59), MSK) is not None  # 01:09 MSK next day
    assert aw.fresh(ActiveWorkout(user_id=1, client_id="w1", payload="{not json", updated_at=UPDATED), UPDATED, MSK) is None
    assert aw.fresh(ActiveWorkout(user_id=1, client_id="w1", payload="{}", updated_at=UPDATED), UPDATED, MSK) is None


def test_fresh_none_when_started_too_long_ago_even_if_just_updated():
    now = datetime(2026, 10, 7, 20, 0, tzinfo=UTC)  # 23:00 MSK, same local day as the start below
    started = "2026-10-07T05:30:00Z"  # 08:30 MSK: 14.5 h earlier, same local day
    assert aw.fresh(snapshot(now, startedAt=started), now, MSK) is None
    assert aw.fresh(snapshot(now, startedAt="2026-10-07T09:00:00Z"), now, MSK) is not None  # 11 h earlier


def test_fresh_keeps_a_workout_that_crosses_local_midnight():
    # Started 23:50 MSK on the 7th, updated at 00:20 on the 8th: a late workout still in progress.
    now = datetime(2026, 10, 7, 21, 20, tzinfo=UTC)
    assert aw.fresh(snapshot(now, startedAt="2026-10-07T20:50:00Z"), now, MSK) is not None


def test_fresh_reads_naive_sqlite_times():
    assert aw.fresh(snapshot(UPDATED.replace(tzinfo=None)), UPDATED, MSK) is not None
    assert aw.fresh(snapshot(UPDATED.replace(tzinfo=None)), UPDATED + timedelta(hours=7), MSK) is None


# ---- save(): an unchanged resend keeps updated_at ----


async def test_save_unchanged_resend_keeps_updated_at_and_changed_body_bumps_it(client, auth, db):
    await client.get("/api/state", headers=auth)
    uid = (await user_obj(db)).id
    data = WorkoutIn.model_validate(active())
    first = datetime(2026, 10, 7, 16, 0, tzinfo=UTC)
    async with db() as s:
        assert await aw.save(s, uid, data, first) is True
        await s.commit()
    async with db() as s:  # the app reopened and sent the same body later
        assert await aw.save(s, uid, WorkoutIn.model_validate(active()), first + timedelta(hours=2)) is True
        await s.commit()
    (row,) = await stored(db)
    assert row.updated_at.replace(tzinfo=None) == first.replace(tzinfo=None)

    changed = active()
    changed["exercises"][0]["sets"][2] = {"weight": 82.5, "reps": 7, "done": True}
    later = first + timedelta(hours=3)
    async with db() as s:
        await aw.save(s, uid, WorkoutIn.model_validate(changed), later)
        await s.commit()
    (row,) = await stored(db)
    assert aw._aware(row.updated_at) == later

    async with db() as s:  # another workout id with the very same body is a change too
        await aw.save(s, uid, WorkoutIn.model_validate({**changed, "id": "w2"}), later + timedelta(hours=1))
        await s.commit()
    (row,) = await stored(db)
    assert row.client_id == "w2" and aw._aware(row.updated_at) == later + timedelta(hours=1)


async def test_idle_workout_still_goes_stale_after_identical_puts(client, auth, db):
    """Resending the same body must not keep an idle workout alive."""
    body = active(startedAt=now_iso())
    assert (await client.put("/api/workouts/active", json=body, headers=auth)).status_code == 204
    async with db() as s:
        row = await s.get(ActiveWorkout, (await user_obj(db)).id)
        row.updated_at = datetime.now(UTC) - timedelta(hours=7)
        await s.commit()
    assert (await client.put("/api/workouts/active", json=body, headers=auth)).status_code == 204
    assert (await client.get("/api/state", headers=auth)).json()["activeWorkout"] is None


# ---- overlap_note / overlap_for ----


def workout_in(**over) -> WorkoutIn:
    return WorkoutIn.model_validate(active(**over))


def test_overlap_note_exact_text_and_merged_runs():
    note = aw.overlap_note(workout_in(), ["Жим лёжа"])
    assert note == (
        "В мини-аппе уже отмечено: жим лёжа 82.5×8 ×2 — если это те же подходы, не сохраняй, "
        "они попадут в историю по «Завершить»."
    )


def test_overlap_note_different_sets_comma_separated_and_bodyweight():
    exercises = [
        {"name": "Жим лёжа", "sets": [
            {"weight": 80, "reps": 8, "done": True}, {"weight": 80, "reps": 8, "done": True},
            {"weight": 85, "reps": 6, "done": True}, {"weight": 80, "reps": None, "done": False},
        ]},
        {"name": "Подтягивания", "sets": [{"weight": None, "reps": 10, "done": True}]},
    ]
    note = aw.overlap_note(workout_in(exercises=exercises), ["жим лёжа", "подтягивания"])
    assert "жим лёжа 80×8 ×2, 85×6; подтягивания 10 повт. —" in note  # first letter lowercased, "; " between


def test_overlap_note_matches_by_containment_only_for_long_names():
    exercises = [{"name": "жим лёжа", "sets": [{"weight": 80, "reps": 8, "done": True}]}]
    assert aw.overlap_note(workout_in(exercises=exercises), ["жим лёжа в тренажёре"]) != ""
    short = [{"name": "жим", "sets": [{"weight": 80, "reps": 8, "done": True}]}]
    assert aw.overlap_note(workout_in(exercises=short), ["жим лёжа"]) == ""  # "жим" is shorter than 5


def test_overlap_note_empty_cases():
    assert aw.overlap_note(None, ["жим лёжа"]) == ""
    assert aw.overlap_note(workout_in(), ["присед"]) == ""  # no common exercise
    assert aw.overlap_note(workout_in(), ["тяга вертикального блока"]) == ""  # in the workout, nothing done
    assert aw.overlap_note(workout_in(), []) == ""


async def test_overlap_for_uses_fresh_snapshot_only(client, auth, db):
    await client.put("/api/workouts/active", json=active(startedAt=now_iso()), headers=auth)
    uid = (await user_obj(db)).id
    now = datetime.now(UTC)
    async with db() as s:
        assert "жим лёжа 82.5×8 ×2" in await aw.overlap_for(s, uid, ["жим лёжа"], now, MSK)
        assert await aw.overlap_for(s, uid, ["жим лёжа"], now + timedelta(hours=7), MSK) == ""  # stale
        assert await aw.overlap_for(s, uid + 100, ["жим лёжа"], now, MSK) == ""  # another user


# ---- GET /api/state and PUT /api/settings carry activeWorkout ----


async def test_state_has_no_active_workout_by_default(client, auth):
    assert (await client.get("/api/state", headers=auth)).json()["activeWorkout"] is None


async def test_state_returns_the_stored_snapshot_with_updated_at(client, auth):
    body = active(startedAt=now_iso())
    assert (await client.put("/api/workouts/active", json=body, headers=auth)).status_code == 204
    out = (await client.get("/api/state", headers=auth)).json()["activeWorkout"]
    assert out is not None
    assert out["id"] == "w1" and out["exercises"] == WorkoutIn.model_validate(body).model_dump(mode="json")["exercises"]
    updated = datetime.fromisoformat(out["updatedAt"])
    assert abs(datetime.now(UTC) - updated) < timedelta(minutes=1)


async def test_state_active_workout_gone_after_delete(client, auth):
    await client.put("/api/workouts/active", json=active(startedAt=now_iso()), headers=auth)
    await client.delete("/api/workouts/active", headers=auth)
    assert (await client.get("/api/state", headers=auth)).json()["activeWorkout"] is None


async def test_state_active_workout_is_per_user(tmp_path, make_client, auth):
    async with make_client(make_settings(tmp_path, allowed_user_ids=[42, 43])) as client:
        await client.put("/api/workouts/active", json=active(startedAt=now_iso()), headers=auth)
        assert (await client.get("/api/state", headers=OTHER)).json()["activeWorkout"] is None
        assert (await client.get("/api/state", headers=auth)).json()["activeWorkout"]["id"] == "w1"


async def test_state_active_workout_null_when_not_updated_for_over_six_hours(client, auth, db):
    await client.put("/api/workouts/active", json=active(startedAt=now_iso()), headers=auth)
    async with db() as s:
        row = await s.get(ActiveWorkout, (await user_obj(db)).id)
        row.updated_at = datetime.now(UTC) - timedelta(hours=7)
        await s.commit()
    assert (await client.get("/api/state", headers=auth)).json()["activeWorkout"] is None


async def test_state_active_workout_null_when_started_over_twelve_hours_ago(client, auth):
    await client.put("/api/workouts/active", json=active(startedAt=now_iso(-timedelta(hours=13))), headers=auth)
    assert (await client.get("/api/state", headers=auth)).json()["activeWorkout"] is None


async def test_state_active_workout_null_when_that_workout_is_finished(client, auth, db):
    st = (await client.get("/api/state", headers=auth)).json()
    body = active(programId=st["programId"], startedAt=now_iso())
    assert (await client.post("/api/workouts", json=body, headers=auth)).status_code == 200
    async with db() as s:  # a snapshot of the finished workout that is still stored
        uid = await s.scalar(select(User.id).where(User.telegram_id == 42))
        s.add(ActiveWorkout(
            user_id=uid, client_id="w1", payload=WorkoutIn.model_validate(body).model_dump_json(),
            updated_at=datetime.now(UTC),
        ))
        await s.commit()
    assert len(await stored(db)) == 1
    assert (await client.get("/api/state", headers=auth)).json()["activeWorkout"] is None


async def test_put_settings_response_carries_active_workout(client, auth):
    await client.put("/api/workouts/active", json=active(startedAt=now_iso()), headers=auth)
    r = await client.put("/api/settings", json={"restSeconds": 120}, headers=auth)
    assert r.status_code == 200, r.text
    assert r.json()["restSeconds"] == 120
    assert r.json()["activeWorkout"]["id"] == "w1" and "updatedAt" in r.json()["activeWorkout"]
    await client.delete("/api/workouts/active", headers=auth)
    assert (await client.put("/api/settings", json={"restSeconds": 90}, headers=auth)).json()["activeWorkout"] is None


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
