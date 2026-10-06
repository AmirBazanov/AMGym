"""GET /api/plan/today: rule-based readiness, optional model polish, cache, regenerate.

The model is faked with httpx.MockTransport; the clock is frozen through gymbot.services.plan.utcnow.
The first call for a new user is always the plan endpoint: it creates the user and starts the default
program on the Monday of the "now" week, so MON is week 1 weekday 1 of arms_specialization_8w.
"""

import json
from datetime import UTC, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import select

from gymbot.api.app import create_app
from gymbot.db.models import User, WellbeingEntry
from gymbot.llm.openrouter import OpenRouterClient

MSK = ZoneInfo("Europe/Moscow")
MON = datetime(2026, 10, 5, 10, 0, tzinfo=MSK).astimezone(UTC)  # training day
TUE = datetime(2026, 10, 6, 10, 0, tzinfo=MSK).astimezone(UTC)  # rest day
MON_MORNING = datetime(2026, 10, 5, 8, 0, tzinfo=MSK).astimezone(UTC)

# Week 1 Monday of the default program: (name, program sets, reps min, reps max); None reps = dropset.
DAY = [
    ("сгибания с гантелями на бицепс с супинацией", 6, 8, 12),
    ("сгибания с гантелями на бицепс с пронацией", 3, 8, 12),
    ("французский жим в блоке из-за головы", 6, 8, 12),
    ("жим гантелей сидя", 3, None, None),
    ("отведения на дельты", 3, 12, 15),
    ("отведения пек дек на заднюю дельту", 3, None, None),
]
NAMES = [d[0] for d in DAY]
ITEM_KEYS = {"name", "sets", "repsMin", "repsMax", "weightFactor", "skip", "replaceWith", "reason"}
PLAN_KEYS = {"date", "week", "weekday", "adjusted", "readiness", "summary", "exercises"}
GARBAGE = ['{"foo": 1}', "not json"]


class FakeModel:
    """OpenRouterClient over MockTransport; always answers with `content` and counts requests."""

    def __init__(self, settings, content: str = "not json"):
        self.content = content
        self.calls = 0
        http = httpx.AsyncClient(transport=httpx.MockTransport(self._handle))
        self.client = OpenRouterClient(settings.model_copy(update={"openrouter_api_key": "k"}), http)

    def answer(self, data: dict) -> None:
        self.content = json.dumps(data, ensure_ascii=False)

    async def _handle(self, req: httpx.Request) -> httpx.Response:
        self.calls += 1
        return httpx.Response(200, json={"choices": [{"message": {"content": self.content}}]})


def model_item(index: int, **over) -> dict:
    name, sets, rmin, rmax = DAY[index]
    item = {
        "name": name.capitalize(),  # the model may change the case; the API must keep program names
        "sets": sets,
        "repsMin": rmin,
        "repsMax": rmax,
        "weightFactor": 1.0,
        "skip": False,
        "replaceWith": None,
        "reason": None,
    }
    item.update(over)
    return item


def model_answer(items: list[dict] | None = None, **over) -> dict:
    data = {
        "summary": "Короткий итог.",
        "readiness": "normal",
        "exercises": items if items is not None else [model_item(i) for i in range(len(DAY))],
    }
    data.update(over)
    return data


@pytest.fixture
def fake(settings) -> FakeModel:
    return FakeModel(settings)


@pytest.fixture
def clock(monkeypatch):
    def set_now(value: datetime) -> None:
        monkeypatch.setattr("gymbot.services.plan.utcnow", lambda: value)

    set_now(MON)
    return set_now


@pytest.fixture
async def api(settings, db, fake, clock):
    app = create_app(settings, db, llm=fake.client)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def add_wellbeing(db, *, sleep=None, pains=None, noted_at=MON_MORNING) -> None:
    async with db() as s:
        uid = (await s.execute(select(User.id).where(User.telegram_id == 42))).scalar_one()
        s.add(
            WellbeingEntry(
                user_id=uid,
                noted_at=noted_at,
                sleep_hours=None if sleep is None else Decimal(str(sleep)),
                pains=None if pains is None else json.dumps(pains, ensure_ascii=False),
                raw_text="test",
            )
        )
        await s.commit()


async def start_with_wellbeing(api, db, fake, auth, **wellbeing) -> None:
    """Create the user through the plan endpoint (nothing to adjust yet), then note wellbeing."""
    first = await api.get("/api/plan/today", headers=auth)
    assert first.status_code == 200
    await add_wellbeing(db, **wellbeing)
    fake.calls = 0


def assert_rule_draft(data: dict) -> None:
    assert [e["name"] for e in data["exercises"]] == NAMES
    for e, (_, sets, rmin, rmax) in zip(data["exercises"], DAY, strict=True):
        assert set(e) == ITEM_KEYS
        assert e["sets"] == max(2, sets - 1)
        assert e["weightFactor"] == 0.9
        assert e["skip"] is False
        assert e["replaceWith"] is None
        assert e["repsMin"] == rmin
        assert e["repsMax"] == rmax
    assert [e["sets"] for e in data["exercises"]] == [5, 2, 5, 2, 2, 2]
    assert isinstance(data["summary"], str) and data["summary"].strip()


# --- Basics ---


async def test_401_without_init_data(api):
    assert (await api.get("/api/plan/today")).status_code == 401
    assert (await api.post("/api/plan/today/regenerate")).status_code == 401


async def test_nothing_to_adjust_skips_the_model(api, fake, auth):
    r = await api.get("/api/plan/today", headers=auth)
    assert r.status_code == 200
    assert r.json() == {"date": "2026-10-05", "week": 1, "weekday": 1, "adjusted": False, "readiness": "normal",
                        "summary": None, "exercises": []}
    assert set(r.json()) == PLAN_KEYS
    assert fake.calls == 0


async def test_regenerate_without_signals_skips_the_model(api, fake, auth):
    r = await api.post("/api/plan/today/regenerate", headers=auth)
    assert r.status_code == 200
    assert r.json() == {"date": "2026-10-05", "week": 1, "weekday": 1, "adjusted": False, "readiness": "normal",
                        "summary": None, "exercises": []}
    assert fake.calls == 0


async def test_rest_day_is_404(api, clock, auth):
    # MON creates the user and the program; TUE is a rest day of the arms program.
    assert (await api.get("/api/plan/today", headers=auth)).status_code == 200
    clock(TUE)
    assert (await api.get("/api/plan/today", headers=auth)).status_code == 404


# --- Rule draft (the model is garbage or fails validation) ---


@pytest.mark.parametrize("garbage", GARBAGE)
async def test_short_sleep_gives_light_rule_draft(api, db, fake, auth, garbage):
    fake.content = garbage
    await start_with_wellbeing(api, db, fake, auth, sleep=5)

    r = await api.get("/api/plan/today", headers=auth)
    assert r.status_code == 200
    data = r.json()
    assert set(data) == PLAN_KEYS
    assert (data["date"], data["week"], data["weekday"]) == ("2026-10-05", 1, 1)
    assert data["readiness"] == "light"
    assert data["adjusted"] is True
    assert len(data["exercises"]) == 6
    assert_rule_draft(data)
    assert fake.calls >= 1


async def test_second_get_is_cached_and_regenerate_calls_the_model_again(api, db, fake, auth):
    await start_with_wellbeing(api, db, fake, auth, sleep=5)

    first = await api.get("/api/plan/today", headers=auth)
    assert first.status_code == 200
    calls_after_first = fake.calls
    assert calls_after_first >= 1

    second = await api.get("/api/plan/today", headers=auth)
    assert second.json() == first.json()
    assert fake.calls == calls_after_first

    again = await api.post("/api/plan/today/regenerate", headers=auth)
    assert again.status_code == 200
    assert set(again.json()) == PLAN_KEYS
    assert fake.calls > calls_after_first


# --- Model answers ---


async def test_valid_model_answer_is_applied_with_program_names(api, db, fake, auth):
    items = [model_item(i, weightFactor=0.9, sets=max(2, DAY[i][1] - 1)) for i in range(6)]
    items[0].update(weightFactor=0.8, sets=4)
    fake.answer(model_answer(items, readiness="normal"))
    await start_with_wellbeing(api, db, fake, auth, sleep=5)

    r = await api.get("/api/plan/today", headers=auth)
    assert r.status_code == 200
    data = r.json()
    assert data["summary"] == "Короткий итог."
    assert data["readiness"] == "light"  # the model cannot change readiness
    assert data["adjusted"] is True
    assert [e["name"] for e in data["exercises"]] == NAMES  # not "Сгибания ..."
    first = data["exercises"][0]
    assert first["weightFactor"] == 0.8
    assert first["sets"] == 4
    assert set(first) == ITEM_KEYS
    assert data["exercises"][1]["weightFactor"] == 0.9


async def test_model_numbers_are_clamped(api, db, fake, auth):
    items = [model_item(i) for i in range(6)]
    items[0].update(weightFactor=3.0, sets=40)
    fake.answer(model_answer(items))
    await start_with_wellbeing(api, db, fake, auth, sleep=5)

    data = (await api.get("/api/plan/today", headers=auth)).json()
    # A light day: the model may not go above the rule draft (5 sets ×0.9, 2 sets ×0.9).
    assert data["exercises"][0]["weightFactor"] == 0.9
    assert data["exercises"][0]["sets"] == 5
    assert data["exercises"][1]["weightFactor"] == 0.9
    assert data["exercises"][1]["sets"] == 2


async def test_model_count_mismatch_falls_back_to_rule_draft(api, db, fake, auth):
    fake.answer(model_answer([model_item(i) for i in range(5)]))
    await start_with_wellbeing(api, db, fake, auth, sleep=5)

    data = (await api.get("/api/plan/today", headers=auth)).json()
    assert data["readiness"] == "light"
    assert_rule_draft(data)


# --- Readiness from wellbeing ---


async def test_very_short_sleep_means_rest(api, db, fake, auth):
    await start_with_wellbeing(api, db, fake, auth, sleep=3)

    data = (await api.get("/api/plan/today", headers=auth)).json()
    assert data["readiness"] == "rest"
    assert data["adjusted"] is True
    assert [e["name"] for e in data["exercises"]] == NAMES
    assert all(e["skip"] is True for e in data["exercises"])


async def test_severe_pain_in_a_used_group_means_rest(api, db, fake, auth):
    await start_with_wellbeing(api, db, fake, auth, sleep=8, pains=[{"place": "левое плечо", "severity": 5}])

    data = (await api.get("/api/plan/today", headers=auth)).json()
    assert data["readiness"] == "rest"
    assert data["adjusted"] is True


async def test_pain_in_an_unused_group_changes_nothing(api, db, fake, auth):
    await start_with_wellbeing(api, db, fake, auth, sleep=8, pains=[{"place": "колено", "severity": None}])

    data = (await api.get("/api/plan/today", headers=auth)).json()
    assert data["adjusted"] is False
    assert data["readiness"] == "normal"


async def test_yesterdays_wellbeing_counts(api, db, fake, auth):
    yesterday = datetime(2026, 10, 4, 22, 0, tzinfo=MSK).astimezone(UTC)
    await start_with_wellbeing(api, db, fake, auth, sleep=3, noted_at=yesterday)

    data = (await api.get("/api/plan/today", headers=auth)).json()
    assert data["readiness"] == "rest"
