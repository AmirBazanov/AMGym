"""GET /api/hub: workouts for the owner's dashboard on GitHub Pages (gymbot.services.hub).

The clock is pinned through gymbot.services.plan.utcnow; the program starts on MON, so the current week is
program week 1: Mon «Руки и плечи», Wed «База», Fri «Руки и плечи».
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import httpx
import pytest
from conftest import make_settings
from sqlalchemy import select

from gymbot.api.app import create_app
from gymbot.db.models import ProgramDay, WellbeingEntry, Workout, WorkoutSet
from gymbot.services import day_adjustments as dayadj
from gymbot.services import hub
from gymbot.services.programs import get_or_create_exercise
from gymbot.services.users import get_or_create_user, set_program

MSK = ZoneInfo("Europe/Moscow")
HUB_TOKEN = "hub-secret"
MON = date(2026, 10, 5)
NOW = datetime(2026, 10, 6, 10, 0, tzinfo=MSK).astimezone(UTC)  # Tuesday, a rest day
ORIGIN = "https://aventrallc.github.io"


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr("gymbot.services.plan.utcnow", lambda: NOW)


def client_for(tmp_path, db, **kw) -> httpx.AsyncClient:
    app = create_app(make_settings(tmp_path, **kw), db)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def get(tmp_path, db, token: str | None = HUB_TOKEN, headers: dict | None = None) -> httpx.Response:
    async with client_for(tmp_path, db, hub_token=HUB_TOKEN) as c:
        return await c.get("/api/hub", params={} if token is None else {"token": token}, headers=headers)


async def owner(db, program: bool = True) -> int:
    async with db() as s:
        user = await get_or_create_user(s, 42, "Amir")
        if program:
            await set_program(s, user, "arms_specialization_8w", MON)
        await s.commit()
        return user.id


async def add_workout(db, user_id: int, day: date, sets: list[tuple[str, float | None, int, int]], *,
                      minutes: int | None = None, focus: str | None = None) -> None:
    """`sets`: (exercise, weight, reps, drop_index)."""
    async with db() as s:
        program_day_id = None
        if focus is not None:
            program_day_id = await s.scalar(select(ProgramDay.id).where(ProgramDay.focus == focus).limit(1))
        started = datetime(day.year, day.month, day.day, 18, 0, tzinfo=MSK).astimezone(UTC)
        w = Workout(user_id=user_id, performed_on=day, started_at=started, program_day_id=program_day_id,
                    finished_at=started + timedelta(minutes=minutes) if minutes is not None else None)
        for i, (name, weight, reps, drop) in enumerate(sets):
            ex = await get_or_create_exercise(s, name)
            w.sets.append(WorkoutSet(exercise_id=ex.id, set_index=i, reps=reps, drop_index=drop,
                                     weight_kg=None if weight is None else Decimal(str(weight))))
        s.add(w)
        await s.commit()


async def test_no_route_without_hub_token(tmp_path, db):
    await owner(db)
    async with client_for(tmp_path, db) as c:
        r = await c.get("/api/hub", params={"token": ""})
    assert r.status_code == 404


async def test_wrong_or_missing_token_is_403(tmp_path, db):
    await owner(db)
    for token in ("nope", "", None, "хаб"):
        r = await get(tmp_path, db, token, headers={"Origin": ORIGIN})
        assert r.status_code == 403, token
        assert r.headers["access-control-allow-origin"] == ORIGIN  # the dashboard can read the error
        assert "workouts" not in r.text


async def test_format_and_headers(tmp_path, db):
    uid = await owner(db)
    async with db() as s:
        await dayadj.upsert(s, uid, MON + timedelta(days=2), dayadj.DayAdjust(skip_groups=["legs"]), "в среду без ног")
        await s.commit()
    await add_workout(db, uid, MON, [
        ("жим лёжа", 60, 5, 0), ("жим лёжа", 60, 5, 0), ("жим лёжа", 60, 5, 0), ("жим лёжа", 62.5, 4, 0),
        ("тяга вертикального блока", 50, 8, 0), ("тяга вертикального блока", 40, 6, 1),
    ], minutes=45, focus="База")
    await add_workout(db, uid, MON - timedelta(days=3), [("подтягивания", None, 8, 0), ("подтягивания", None, 8, 0)])
    await add_workout(db, uid, MON - timedelta(days=95), [("жим лёжа", 50, 5, 0)])  # outside 90 days

    r = await get(tmp_path, db, headers={"Origin": ORIGIN})
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == ORIGIN
    assert r.headers["cache-control"] == "no-store"
    assert "Origin" in r.headers["vary"]
    data = r.json()
    assert set(data) == {"workouts"} and set(data["workouts"]) == {"plan", "log"}

    plan = {p["day"]: p for p in data["workouts"]["plan"]}
    assert sorted(plan) == [1, 3, 5]
    for p in plan.values():
        assert set(p) == {"day", "title", "note"}  # no "time": program days have none
    assert plan[1]["title"] == "Руки и плечи"
    assert plan[1]["note"].startswith("Сгибания с гантелями на бицепс с супинацией, ")
    assert plan[1]["note"].endswith(", …")  # six exercises, five named
    # The Wednesday adjustment takes the legs off the day, as the Mini App shows it.
    assert plan[3]["title"] == "База"
    assert plan[3]["note"] == "Жим лёжа, тяга вертикального блока · без ног"

    log = sorted(data["workouts"]["log"], key=lambda x: x["date"])
    assert log == [
        {"date": "2026-10-02", "title": "Тренировка", "note": "Подтягивания ×8×2"},
        {"date": "2026-10-05", "title": "База",
         "note": "Жим лёжа 60×5×3 + 62.5×4, тяга вертикального блока 50×8 → 40×6 · тоннаж 1,8 т · 45 мин"},
    ]


async def test_cors_only_for_allowed_origins(tmp_path, db):
    await owner(db)
    local = await get(tmp_path, db, headers={"Origin": "http://localhost:8765"})
    assert local.headers["access-control-allow-origin"] == "http://localhost:8765"
    other = await get(tmp_path, db, headers={"Origin": "https://evil.example"})
    assert other.status_code == 200
    assert "access-control-allow-origin" not in other.headers


async def test_empty_program_and_log(tmp_path, db):
    r = await get(tmp_path, db)  # the owner has not reached the bot yet
    assert r.json() == {"workouts": {"plan": [], "log": []}}
    await owner(db, program=False)
    r = await get(tmp_path, db)
    assert r.status_code == 200
    assert r.json() == {"workouts": {"plan": [], "log": []}}


async def test_read_only(tmp_path, db):
    """The hub never starts a program for the owner (users.active_program would)."""
    await owner(db, program=False)
    await get(tmp_path, db)
    r = await get(tmp_path, db)
    assert r.json()["workouts"]["plan"] == []


async def test_today_plan_keeps_wellbeing_out(tmp_path, db, monkeypatch):
    """Today the day plan cuts the weights for poor sleep and a pain; the public note names neither."""
    monday = datetime(2026, 10, 5, 10, 0, tzinfo=MSK).astimezone(UTC)
    monkeypatch.setattr("gymbot.services.plan.utcnow", lambda: monday)
    uid = await owner(db)
    async with db() as s:
        s.add(WellbeingEntry(user_id=uid, noted_at=monday - timedelta(hours=2), sleep_hours=Decimal(4),
                             pains='[{"place": "колено", "severity": 4}]', raw_text="спал 4 часа, болит колено"))
        await s.commit()
    note = next(p["note"] for p in (await get(tmp_path, db)).json()["workouts"]["plan"] if p["day"] == 1)
    assert note.startswith("Сгибания с гантелями")
    for word in ("сон", "спал", "колен", "4 ч", "бол"):  # the day plan's summary here: «Спал 4 ч. Вес −10 %, …»
        assert word not in note.casefold()


async def test_long_day_keeps_the_adjustment(tmp_path, db):
    uid = await owner(db)
    adj = dayadj.DayAdjust(weight_factor=0.8, sets_delta=-1, skip_exercises=["несуществующее упражнение " * 3])
    async with db() as s:
        await dayadj.upsert(s, uid, MON + timedelta(days=4), adj, "в пятницу полегче")
        await s.commit()
    note = next(p["note"] for p in (await get(tmp_path, db)).json()["workouts"]["plan"] if p["day"] == 5)
    assert len(note) <= hub.NOTE_MAX
    assert note.endswith(dayadj.describe(adj))
    assert ", … · веса −20 %" in note


def test_set_runs_and_tonnage():
    assert hub._sets(["60×5"] * 5) == "60×5×5"
    assert hub._sets(["60×5", "60×5", "62.5×4", "60×5"]) == "60×5×2 + 62.5×4 + 60×5"
    assert hub._tonnage(Decimal(3200)) == "3,2 т"
    assert hub._tonnage(Decimal("850.5")) == "850,5 кг"
