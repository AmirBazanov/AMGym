"""Seed the temp screenshot DB through the local API (dev user) plus direct rows for food and wellbeing."""

import asyncio
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import httpx

from gymbot.config import get_settings
from gymbot.db.models import FoodEntry, User, WellbeingEntry
from gymbot.db.session import make_engine
from sqlalchemy import select

API = "http://127.0.0.1:8000/api"
TZ = ZoneInfo("Europe/Moscow")
SLUG = "arms_specialization_8w"

SUP = "сгибания с гантелями на бицепс с супинацией"
PRO = "сгибания с гантелями на бицепс с пронацией"
FRB = "французский жим в блоке из-за головы"
DBP = "жим гантелей сидя"
LAT = "отведения на дельты"
PEC = "отведения пек дек на заднюю дельту"
EZU = "сгибания на бицепс с ez грифом хватом снизу"
EZO = "сгибания на бицепс с ez грифом хватом сверху"
FRL = "французский жим лёжа"
SMI = "жим сидя в смите"


def sets(*groups):
    """groups: (weight, reps, times)."""
    out = []
    for w, r, n in groups:
        out += [{"weight": w, "reps": r, "done": True}] * n
    return out


WORKOUTS = [
    # (local date, hour, week, weekday, [(name, sets)])
    (date(2026, 9, 18), 19, 1, 5, [
        (EZU, sets((22.5, 10, 3))), (EZO, sets((17.5, 10, 4))), (FRL, sets((30, 10, 4))),
        (SMI, sets((40, 10, 3))), (LAT, sets((7.5, 12, 3))), (PEC, sets((35, 12, 3))),
    ]),
    (date(2026, 9, 21), 19, 1, 1, [
        (SUP, sets((17.5, 10, 3), (15, 10, 3))), (PRO, sets((10, 12, 3))), (FRB, sets((50, 10, 4))),
        (DBP, sets((12.5, 10, 3))), (LAT, sets((7.5, 12, 3))), (PEC, sets((35, 12, 1), (45, 10, 2))),
    ]),
    (date(2026, 9, 25), 19, 2, 5, [
        (EZU, sets((25, 10, 4))), (EZO, sets((20, 10, 4))), (FRL, sets((32.5, 10, 4))),
        (SMI, sets((45, 10, 4))), (LAT, sets((7.5, 13, 4))), (PEC, sets((40, 12, 4))),
    ]),
    (date(2026, 9, 28), 20, 2, 1, [
        (SUP, sets((17.5, 10, 4))), (PRO, sets((12.5, 8, 2), (10, 10, 2))), (FRB, sets((55, 10, 4))),
        (DBP, sets((12.5, 10, 4))), (LAT, sets((7.5, 14, 4))), (PEC, sets((45, 12, 4))),
    ]),
    (date(2026, 9, 30), 19, 2, 3, [
        ("жим лёжа", sets((70, 10, 5))), ("тяга вертикального блока", sets((60, 10, 5))),
        ("присед со штангой", sets((70, 10, 5))), ("румынская тяга", sets((70, 10, 5))),
    ]),
    (date(2026, 10, 2), 19, 3, 5, [
        (EZU, sets((25, 12, 5))), (EZO, sets((20, 12, 3), (20, 10, 3))), (FRL, sets((35, 10, 6))),
        (SMI, sets((45, 12, 4))), (LAT, sets((7.5, 15, 3))), (PEC, sets((45, 12, 4))),
    ]),
    (date(2026, 10, 5), 20, 3, 3, [
        ("жим лёжа 30°", sets((60, 10, 4))), ("тяга горизонтального блока", sets((60, 12, 4))),
        ("румынская тяга", sets((75, 10, 4))),
    ]),
    # The owner's 07.10 arms workout.
    (date(2026, 10, 7), 19, 3, 1, [
        (SUP, sets((20, 8, 3), (17.5, 8, 2), (15, 8, 1))),
        (PRO, sets((12.5, 8, 1), (10, 10, 2))),
        (FRB, sets((60, 10, 3), (60, 8, 1), (50, 10, 1), (40, 10, 1))),
        (DBP, sets((12.5, 12, 3))),
        (LAT, sets((7.5, 15, 2), (7.5, 10, 1))),
        (PEC, sets((35, 12, 1), (50, 10, 2))),
    ]),
]

WEIGHTS = [86.0, 86.2, 85.9, 85.8, 85.9, 85.6, 85.5, 85.6, 85.3, 85.2, 85.1, 85.0]  # 27.09 .. 08.10

FOOD_TODAY = [  # (local hour:minute, description, grams, kcal, p, f, c, raw)
    ((9, 10), "протеин, 1 скуп", 30, 120, 24, 1.5, 3, "протеин 1 скуп"),
    ((9, 10), "курт", 30, 110, 9, 6, 3, "курт 3 штуки"),
    ((13, 40), "плов", 350, 630, 21, 28, 74, "плов, самса"),
    ((13, 40), "самса с говядиной", 150, 470, 15, 27, 42, "плов, самса"),
    ((16, 30), "протеин, 1 скуп", 30, 120, 24, 1.5, 3, "протеин после работы"),
]
FOOD_EARLIER = {
    date(2026, 10, 7): [((9, 0), "овсянка на молоке", 300, 330, 12, 9, 50), ((14, 0), "манты, 5 шт", 450, 900, 45, 50, 68),
                        ((20, 30), "творожный сыр и хлеб", 200, 420, 25, 15, 45), ((21, 0), "протеин, 1 скуп", 30, 120, 24, 1.5, 3)],
    date(2026, 10, 6): [((10, 0), "яичница из 3 яиц", 180, 280, 19, 21, 2), ((14, 30), "лагман", 500, 650, 30, 22, 80),
                        ((19, 0), "курица с рисом", 400, 620, 45, 12, 80)],
}


def at(day: date, hm: tuple[int, int]) -> datetime:
    return datetime(day.year, day.month, day.day, hm[0], hm[1], tzinfo=TZ).astimezone(UTC)


async def main() -> None:
    c = httpx.Client(base_url=API, trust_env=False, timeout=20)
    c.get("/state").raise_for_status()  # creates the dev user and their program
    r = c.put("/settings", json={
        "programId": SLUG, "startDate": "2026-09-17", "restSeconds": 120,
        "targets": {"kcal": 2800, "protein": 170, "fat": 85, "carbs": 330},
        "profile": {"heightCm": 181, "birthYear": 1997, "goal": "cut",
                    "about": "Специализация на руки, 8 недель. Левое плечо иногда ноет на жимах над головой."},
    })
    r.raise_for_status()
    for day, hour, week, weekday, exs in WORKOUTS:
        start = datetime(day.year, day.month, day.day, hour, 0, tzinfo=TZ)
        body = {
            "id": str(uuid.uuid4()), "programId": SLUG, "week": week, "weekday": weekday,
            "startedAt": start.isoformat(), "finishedAt": (start + timedelta(minutes=75)).isoformat(),
            "exercises": [{"name": n, "target": "", "dropset": False, "sets": s} for n, s in exs],
        }
        c.post("/workouts", json=body).raise_for_status()
    first = date(2026, 9, 27)
    for i, kg in enumerate(WEIGHTS):
        c.post("/body-weight", json={"weightKg": kg, "date": (first + timedelta(days=i)).isoformat()}).raise_for_status()
    for rm in [
        {"time": "08:30", "kind": "checkin"},
        {"time": "09:00", "kind": "text", "text": "Креатин 5 г"},
        {"time": "21:00", "kind": "nutrition"},
        {"time": "10:00", "kind": "advice", "weekday": 6},
    ]:
        c.post("/reminders", json=rm).raise_for_status()
    for text, cat in [
        ("манты у нас ~90 г за штуку", "food"),
        ("курт — сушёный сыр, ~10 г за штуку", "food"),
        ("лепёшка целиком ~250 г", "food"),
        ("левое плечо иногда ноет на жимах над головой", "health"),
        ("тренируюсь вечером после работы", "schedule"),
    ]:
        c.post("/facts", json={"text": text, "category": cat}).raise_for_status()

    settings = get_settings()
    engine, sm = make_engine(settings.database_url)
    async with sm() as s:
        user = await s.scalar(select(User))
        today = date(2026, 10, 8)
        for hm, d, g, k, p, f, cb, raw in FOOD_TODAY:
            s.add(FoodEntry(user_id=user.id, eaten_at=at(today, hm), description=d, grams=Decimal(g),
                            kcal=Decimal(k), protein_g=Decimal(str(p)), fat_g=Decimal(str(f)),
                            carbs_g=Decimal(cb), estimated=True, raw_text=raw))
        for day, rows in FOOD_EARLIER.items():
            for hm, d, g, k, p, f, cb in rows:
                s.add(FoodEntry(user_id=user.id, eaten_at=at(day, hm), description=d, grams=Decimal(g),
                                kcal=Decimal(k), protein_g=Decimal(str(p)), fat_g=Decimal(str(f)),
                                carbs_g=Decimal(cb), estimated=True, raw_text=d))
        s.add(WellbeingEntry(user_id=user.id, noted_at=at(today, (8, 40)), sleep_hours=Decimal("7.5"),
                             energy=4, mood=4, raw_text="спал 7,5 часов, сил нормально"))
        await s.commit()
    await engine.dispose()
    print("seeded")


asyncio.run(main())
