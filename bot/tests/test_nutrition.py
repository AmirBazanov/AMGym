from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from gymbot.db.models import FoodEntry, User
from gymbot.services.nutrition import day_summary, local_day_bounds, week_summary

MSK = ZoneInfo("Europe/Moscow")
KEYS = ("kcal", "protein", "fat", "carbs")


async def _user(db, telegram_id: int = 42, **targets) -> int:
    async with db() as s:
        user = await s.scalar(select(User).where(User.telegram_id == telegram_id))
        if user is None:
            user = User(telegram_id=telegram_id, rest_seconds=90)
            s.add(user)
        for k, v in targets.items():
            setattr(user, k, v)
        await s.commit()
        return user.id


async def _food(db, user_id: int, eaten_at: datetime, kcal="100", protein="10", fat="5", carbs="20",
                description="еда", grams="100", estimated=True) -> int:
    async with db() as s:
        e = FoodEntry(
            user_id=user_id, eaten_at=eaten_at, description=description,
            grams=Decimal(grams) if grams is not None else None,
            kcal=Decimal(kcal), protein_g=Decimal(protein), fat_g=Decimal(fat), carbs_g=Decimal(carbs),
            estimated=estimated,
        )
        s.add(e)
        await s.commit()
        return e.id


def utc(*a) -> datetime:
    return datetime(*a, tzinfo=UTC)


# ---- local_day_bounds ----


def test_local_day_bounds_msk():
    start, end = local_day_bounds(date(2026, 10, 6), MSK)
    assert start == utc(2026, 10, 5, 21, 0) and end == utc(2026, 10, 6, 21, 0)
    assert start.tzinfo is not None and end.tzinfo is not None


def test_local_day_bounds_dst_day_is_25_hours():
    # Europe/Berlin leaves DST on 2026-10-25: that local day lasts 25 hours.
    start, end = local_day_bounds(date(2026, 10, 25), ZoneInfo("Europe/Berlin"))
    assert end - start == timedelta(hours=25)


# ---- services ----


async def test_day_summary_boundary_by_timezone(db):
    uid = await _user(db)
    # 00:30 MSK on Oct 6 is 21:30 UTC on Oct 5: it belongs to Oct 6.
    await _food(db, uid, utc(2026, 10, 5, 21, 30), description="после полуночи")
    # 23:59 MSK on Oct 5.
    await _food(db, uid, utc(2026, 10, 5, 20, 59), description="до полуночи")
    async with db() as s:
        user = await s.get(User, uid)
        d6 = await day_summary(s, user, date(2026, 10, 6), MSK)
        d5 = await day_summary(s, user, date(2026, 10, 5), MSK)
    assert [e.description for e in d6.entries] == ["после полуночи"]
    assert d6.entries[0].time == "00:30"
    assert [e.description for e in d5.entries] == ["до полуночи"]
    assert d5.entries[0].time == "23:59"


async def test_week_summary_has_seven_days_including_empty(db):
    uid = await _user(db)
    await _food(db, uid, utc(2026, 10, 5, 21, 30), kcal="300")  # Oct 6 MSK
    await _food(db, uid, utc(2026, 10, 6, 9, 0), kcal="200")  # Oct 6 MSK
    await _food(db, uid, utc(2026, 9, 29, 21, 30), kcal="50")  # Sep 30 MSK, first day of the week
    await _food(db, uid, utc(2026, 9, 29, 20, 30), kcal="999")  # Sep 29 MSK, outside the week
    await _food(db, uid, utc(2026, 10, 6, 21, 0), kcal="999")  # Oct 7 00:00 MSK, outside the week
    async with db() as s:
        user = await s.get(User, uid)
        wk = await week_summary(s, user, date(2026, 10, 6), MSK)
    assert [d.date for d in wk.days] == [date(2026, 9, 30) + timedelta(days=i) for i in range(7)]
    by = {d.date: d for d in wk.days}
    assert by[date(2026, 10, 6)].kcal == 500.0 and by[date(2026, 10, 6)].entries == 2
    assert by[date(2026, 9, 30)].kcal == 50.0 and by[date(2026, 9, 30)].entries == 1
    assert by[date(2026, 10, 3)].kcal == 0.0 and by[date(2026, 10, 3)].entries == 0


# ---- API: day ----


async def test_day_api_totals_targets_remaining(client, auth, db):
    await client.get("/api/state", headers=auth)
    uid = await _user(db, kcal_target=2600, protein_target_g=160, fat_target_g=80, carbs_target_g=300)
    await _food(db, uid, utc(2026, 10, 6, 9, 12), description="гречка", grams="200",
                kcal="220", protein="8", fat="2", carbs="42")
    await _food(db, uid, utc(2026, 10, 6, 6, 0), description="яйца", grams=None,
                kcal="155.5", protein="12.6", fat="10.6", carbs="1.1", estimated=False)
    r = await client.get("/api/nutrition/day", params={"date": "2026-10-06"}, headers=auth)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["date"] == "2026-10-06"
    assert out["targets"] == {"kcal": 2600, "protein": 160, "fat": 80, "carbs": 300}
    assert out["totals"] == {"kcal": 375.5, "protein": 20.6, "fat": 12.6, "carbs": 43.1}
    assert out["remaining"] == {"kcal": 2224.5, "protein": 139.4, "fat": 67.4, "carbs": 256.9}
    for k in KEYS:
        assert isinstance(out["totals"][k], float) and isinstance(out["remaining"][k], float)
    # Sorted by eatenAt.
    eggs, buckwheat = out["entries"]
    assert eggs["description"] == "яйца" and eggs["grams"] is None and eggs["estimated"] is False
    assert buckwheat == {
        "id": buckwheat["id"], "eatenAt": "2026-10-06T09:12:00Z", "time": "12:12",
        "description": "гречка", "grams": 200.0, "kcal": 220.0,
        "protein": 8.0, "fat": 2.0, "carbs": 42.0, "estimated": True,
    }
    for k in ("grams", *KEYS):
        assert isinstance(buckwheat[k], float)


async def test_day_api_remaining_null_without_targets(client, auth, db):
    await client.get("/api/state", headers=auth)
    uid = await _user(db)
    await _food(db, uid, utc(2026, 10, 6, 9, 0))
    out = (await client.get("/api/nutrition/day", params={"date": "2026-10-06"}, headers=auth)).json()
    assert out["targets"] == {k: None for k in KEYS}
    assert out["remaining"] == {k: None for k in KEYS}
    assert out["totals"] == {"kcal": 100.0, "protein": 10.0, "fat": 5.0, "carbs": 20.0}


async def test_day_api_remaining_negative_when_over(client, auth, db):
    await client.get("/api/state", headers=auth)
    uid = await _user(db, kcal_target=100, protein_target_g=None)
    await _food(db, uid, utc(2026, 10, 6, 9, 0), kcal="220")
    out = (await client.get("/api/nutrition/day", params={"date": "2026-10-06"}, headers=auth)).json()
    assert out["remaining"]["kcal"] == -120.0
    assert out["remaining"]["protein"] is None


async def test_day_api_empty_day(client, auth):
    out = (await client.get("/api/nutrition/day", params={"date": "2026-10-06"}, headers=auth)).json()
    assert out["entries"] == []
    assert out["totals"] == {k: 0.0 for k in KEYS}


async def test_day_api_defaults_to_today_in_timezone(client, auth, db):
    await client.get("/api/state", headers=auth)
    uid = await _user(db)
    await _food(db, uid, datetime.now(UTC))
    out = (await client.get("/api/nutrition/day", headers=auth)).json()
    assert out["date"] == datetime.now(MSK).date().isoformat()
    assert len(out["entries"]) == 1


async def test_day_api_only_own_entries(client, auth, db):
    await client.get("/api/state", headers=auth)
    other = await _user(db, telegram_id=99)
    await _food(db, other, utc(2026, 10, 6, 9, 0))
    out = (await client.get("/api/nutrition/day", params={"date": "2026-10-06"}, headers=auth)).json()
    assert out["entries"] == []


async def test_day_api_bad_date_422(client, auth):
    r = await client.get("/api/nutrition/day", params={"date": "06.10.2026"}, headers=auth)
    assert r.status_code == 422


async def test_nutrition_requires_auth(client):
    assert (await client.get("/api/nutrition/day")).status_code == 401
    assert (await client.get("/api/nutrition/week")).status_code == 401
    assert (await client.delete("/api/food/1")).status_code == 401


# ---- API: week ----


async def test_week_api(client, auth, db):
    await client.get("/api/state", headers=auth)
    uid = await _user(db, kcal_target=2600)
    await _food(db, uid, utc(2026, 10, 5, 21, 30), kcal="300", protein="20", fat="10", carbs="30")
    await _food(db, uid, utc(2026, 10, 6, 9, 0), kcal="200", protein="10", fat="5", carbs="20")
    r = await client.get("/api/nutrition/week", params={"end": "2026-10-06"}, headers=auth)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["targets"] == {"kcal": 2600, "protein": None, "fat": None, "carbs": None}
    assert [d["date"] for d in out["days"]] == [
        "2026-09-30", "2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04", "2026-10-05", "2026-10-06",
    ]
    assert out["days"][-1] == {
        "date": "2026-10-06", "kcal": 500.0, "protein": 30.0, "fat": 15.0, "carbs": 50.0, "entries": 2,
    }
    assert out["days"][0] == {"date": "2026-09-30", "kcal": 0.0, "protein": 0.0, "fat": 0.0, "carbs": 0.0,
                              "entries": 0}
    for k in KEYS:
        assert isinstance(out["days"][0][k], float)


async def test_week_api_defaults_to_today(client, auth):
    out = (await client.get("/api/nutrition/week", headers=auth)).json()
    assert len(out["days"]) == 7
    assert out["days"][-1]["date"] == datetime.now(MSK).date().isoformat()


# ---- API: delete ----


async def test_delete_food(client, auth, db):
    await client.get("/api/state", headers=auth)
    uid = await _user(db)
    fid = await _food(db, uid, utc(2026, 10, 6, 9, 0))
    assert (await client.delete(f"/api/food/{fid}", headers=auth)).status_code == 204
    async with db() as s:
        assert await s.get(FoodEntry, fid) is None
    assert (await client.delete(f"/api/food/{fid}", headers=auth)).status_code == 404


async def test_delete_other_users_food_404(client, auth, db):
    await client.get("/api/state", headers=auth)
    other = await _user(db, telegram_id=99)
    fid = await _food(db, other, utc(2026, 10, 6, 9, 0))
    assert (await client.delete(f"/api/food/{fid}", headers=auth)).status_code == 404
    async with db() as s:
        assert await s.get(FoodEntry, fid) is not None


# ---- API: targets via settings ----


async def test_state_has_empty_targets(client, auth):
    st = (await client.get("/api/state", headers=auth)).json()
    assert st["targets"] == {k: None for k in KEYS}


async def test_put_settings_targets(client, auth):
    r = await client.put(
        "/api/settings", json={"targets": {"kcal": 2600, "protein": 160, "fat": 80, "carbs": 300}}, headers=auth
    )
    assert r.status_code == 200, r.text
    assert r.json()["targets"] == {"kcal": 2600, "protein": 160, "fat": 80, "carbs": 300}
    # Partial update: only the given keys change; null resets a target.
    r = await client.put("/api/settings", json={"targets": {"fat": None, "carbs": 250}}, headers=auth)
    assert r.json()["targets"] == {"kcal": 2600, "protein": 160, "fat": None, "carbs": 250}
    # Settings without targets leave them alone.
    r = await client.put("/api/settings", json={"restSeconds": 120}, headers=auth)
    assert r.json()["targets"] == {"kcal": 2600, "protein": 160, "fat": None, "carbs": 250}
    st = (await client.get("/api/state", headers=auth)).json()
    assert st["targets"] == {"kcal": 2600, "protein": 160, "fat": None, "carbs": 250}
    day = (await client.get("/api/nutrition/day", headers=auth)).json()
    assert day["targets"] == st["targets"]


@pytest.mark.parametrize(
    "targets",
    [{"kcal": -1}, {"kcal": 10001}, {"protein": 1001}, {"fat": -5}, {"carbs": 1001}],
)
async def test_put_settings_targets_out_of_range(client, auth, targets):
    assert (await client.put("/api/settings", json={"targets": targets}, headers=auth)).status_code == 422


async def test_put_settings_targets_bounds_inclusive(client, auth):
    body = {"targets": {"kcal": 10000, "protein": 0, "fat": 1000, "carbs": 0}}
    r = await client.put("/api/settings", json=body, headers=auth)
    assert r.status_code == 200 and r.json()["targets"] == body["targets"]


# ---- API: date range (edge dates must not overflow into a 500) ----


@pytest.mark.parametrize("value", ["9999-12-31", "0001-01-01", "1999-12-31"])
async def test_day_api_date_out_of_range_422(client, auth, value):
    r = await client.get("/api/nutrition/day", params={"date": value}, headers=auth)
    assert r.status_code == 422, r.text


@pytest.mark.parametrize("value", ["0001-01-01", "9999-12-31", "1999-12-31"])
async def test_week_api_end_out_of_range_422(client, auth, value):
    r = await client.get("/api/nutrition/week", params={"end": value}, headers=auth)
    assert r.status_code == 422, r.text


async def test_nutrition_date_range_edges_ok(client, auth):
    far = (datetime.now(MSK).date() + timedelta(days=365)).isoformat()
    for path, key in (("/api/nutrition/day", "date"), ("/api/nutrition/week", "end")):
        assert (await client.get(path, params={key: "2000-01-01"}, headers=auth)).status_code == 200
        assert (await client.get(path, params={key: far}, headers=auth)).status_code == 200
