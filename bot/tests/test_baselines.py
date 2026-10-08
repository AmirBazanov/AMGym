"""Working weights from facts: matching, the model's answer, processing, profile rule, API, advice, scheduling."""

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import httpx
import pytest
from alembic import command
from conftest import make_settings
from sqlalchemy import func, inspect, select, text
from test_mcp import TOKEN as MCP_TOKEN
from test_mcp import call, data, make_owner, mcp_client

from gymbot.api.app import create_app
from gymbot.db import migrate
from gymbot.db.models import Exercise, ExerciseBaseline, User, UserFact
from gymbot.db.session import make_engine
from gymbot.handlers import facts as facts_handler
from gymbot.handlers import log_text
from gymbot.llm.openrouter import OpenRouterClient
from gymbot.services import advice, baselines, profile
from gymbot.services.baselines import BaselineOut, match_exercise, parse_answer, same_stem, shares_word

TG = 42
YEAR = 2026
T1 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
T2 = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
MSK = ZoneInfo("Europe/Moscow")
NOW = datetime(2026, 10, 7, 12, 0, tzinfo=MSK).astimezone(UTC)

CATALOG = [
    "жим гантелей сидя", "жим лёжа", "жим лёжа 30°", "жим сидя в смите",
    "отведения гантелей на переднюю дельту", "отведения на дельты", "отведения пек дек на заднюю дельту",
    "присед в гаке лицом к спинке", "присед со штангой", "румынская тяга",
    "сгибания на бицепс с ez грифом хватом сверху", "сгибания на бицепс с ez грифом хватом снизу",
    "сгибания с гантелями на бицепс с пронацией", "сгибания с гантелями на бицепс с супинацией",
    "тяга вертикального блока", "тяга горизонтального блока",
    "французский жим в блоке из-за головы", "французский жим лёжа",
]  # fmt: skip


# ---- helpers ----


class FakeLLM:
    """MockTransport-backed OpenRouterClient: replays queued answers, then `fallback`; records request bodies."""

    def __init__(self, settings):
        self.answers: list = []
        self.fallback: object = None
        self.status = 200
        self.bodies: list[dict] = []
        self.hook = None  # async callable run while the "model" thinks
        http = httpx.AsyncClient(transport=httpx.MockTransport(self._handle))
        self.client = OpenRouterClient(settings.model_copy(update={"openrouter_api_key": "k"}), http)

    async def _handle(self, req: httpx.Request) -> httpx.Response:
        self.bodies.append(json.loads(req.content))
        if self.hook is not None:
            await self.hook()
        if self.status != 200:
            return httpx.Response(self.status, json={"error": "boom"})
        answer = self.answers.pop(0) if self.answers else self.fallback
        content = answer if isinstance(answer, str) else json.dumps(answer, ensure_ascii=False)
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def lift(said: str, kg, reps=None, exercise=None) -> dict:
    return {"said": said, "exercise": exercise, "weight_kg": kg, "reps": reps}


def reply(*lifts: dict, **body) -> dict:
    return {"lifts": list(lifts), "profile": body}


@pytest.fixture
def fake(settings):
    return FakeLLM(settings)


@pytest.fixture(autouse=True)
def pauses(monkeypatch) -> list[float]:
    """The backfill never sleeps in tests: every pause it asks for is recorded here instead."""
    asked: list[float] = []

    async def record(seconds: float) -> None:
        asked.append(seconds)

    monkeypatch.setattr(baselines, "_pause", record)
    return asked


@pytest.fixture(autouse=True)
async def clean_tasks():
    for store in (log_text.PENDING, log_text.CONTEXT, log_text.FACTS):
        store.clear()
    yield
    await baselines.cancel_all()


async def get_user(s, telegram_id: int = TG) -> User:
    user = await s.scalar(select(User).where(User.telegram_id == telegram_id))
    if user is None:
        user = User(telegram_id=telegram_id, name="Amir", rest_seconds=90)
        s.add(user)
        await s.flush()
    return user


async def seed_fact(db, body: str, *, lifts=(), active=True, at=T1, telegram_id=TG, processed=None) -> int:
    """A fact with ready baselines (name, kg, reps); processed defaults to "has lifts"."""
    async with db() as s:
        user = await get_user(s, telegram_id)
        fact = UserFact(user_id=user.id, text=body, category="training", active=active, created_at=at)
        for name, kg, reps in lifts:
            ex_id = await s.scalar(select(Exercise.id).where(Exercise.name == name))
            fact.baselines.append(
                ExerciseBaseline(user_id=user.id, exercise_id=ex_id, weight_kg=Decimal(str(kg)), reps=reps, created_at=at)
            )
        if processed if processed is not None else lifts:
            fact.baselines_at = at
        s.add(fact)
        await s.commit()
        return fact.id


async def count(db, model) -> int:
    async with db() as s:
        return await s.scalar(select(func.count()).select_from(model)) or 0


async def get_fact(db, fact_id: int) -> UserFact | None:
    async with db() as s:
        return await s.get(UserFact, fact_id)


async def state_baselines(client, auth) -> list[dict]:
    r = await client.get("/api/state", headers=auth)
    assert r.status_code == 200
    return r.json()["baselines"]


# ---- 1. match_exercise / parse_answer (pure) ----


async def test_hardcoded_catalog_equals_the_program_catalog(db):
    async with db() as s:
        assert await baselines.catalog(s, None) == CATALOG


@pytest.mark.parametrize(
    ("said", "expected"),
    [
        ("жим лёжа", "жим лёжа"),
        ("Жим Лёжа", "жим лёжа"),
        ("жим лежа", "жим лёжа"),
        ("жим лежа.", "жим лёжа"),
        ("«жим лежа»", "жим лёжа"),
        ("жим в горизонте", "жим лёжа"),
        ("жим штанги лежа", "жим лёжа"),
        ("жим лёжа 30°", "жим лёжа 30°"),  # exact catalog name beats the synonym table
        ("присед", "присед со штангой"),
        ("приседания", "присед со штангой"),
        ("приседы", "присед со штангой"),
        ("присед со штангой на спине", "присед со штангой"),
        ("присед в гаке лицом к спинке", "присед в гаке лицом к спинке"),
        ("тяга блока", "тяга вертикального блока"),
        ("тяга верхнего блока", "тяга вертикального блока"),
        ("тяга нижнего блока", "тяга горизонтального блока"),
        ("горизонтальная тяга", "тяга горизонтального блока"),
        ("румынка", "румынская тяга"),
        ("РДЛ", "румынская тяга"),
        ("становая", None),  # a synonym of an exercise that is not in the programs
        ("становая тяга", None),
        ("выпады", None),
        ("", None),
        ("   ", None),
        (None, None),
        (5, None),
    ],
)
def test_match_exercise(said, expected):
    assert match_exercise(said, CATALOG) == expected


def test_match_exercise_returns_the_catalog_spelling_and_needs_the_catalog():
    assert match_exercise("жим лежа", ["жим лёжа"]) == "жим лёжа"
    assert match_exercise("жим лежа", ["присед со штангой"]) is None  # synonym target not in this catalog
    assert match_exercise("жим лежа", []) is None


def one(item: dict, year: int = YEAR):
    got = parse_answer({"lifts": [item]}, CATALOG, year).lifts
    return got[0] if got else None


def test_said_wins_over_the_models_pick():
    squat = one(lift("присед", 140, 1, exercise="присед в гаке лицом к спинке"))
    assert squat.exercise == "присед со штангой"
    assert one(lift("приседания", 100, 5, exercise="присед в гаке лицом к спинке")).exercise == "присед со штангой"
    assert one(lift("тяга блока", 50, 10, exercise="тяга горизонтального блока")).exercise == "тяга вертикального блока"
    assert one(lift("тяга нижнего блока", 50, 10)).exercise == "тяга горизонтального блока"
    assert one(lift("жим в горизонте", 90, 8, exercise="жим лёжа 30°")).exercise == "жим лёжа"
    assert one(lift("жим лежа", 90, 8)).exercise == "жим лёжа"  # the exact catalog string, with ё
    assert one(lift("румынка", 100)).exercise == "румынская тяга"


def test_models_pick_is_used_when_said_is_not_recognised_and_shares_a_word():
    assert one(lift("сгибания с ez", 30, 10, exercise="сгибания на бицепс с ez грифом хватом сверху")).exercise == (
        "сгибания на бицепс с ez грифом хватом сверху"
    )
    assert one(lift("приседания в гакке", 100, 8, exercise="присед в гаке лицом к спинке")).exercise == (
        "присед в гаке лицом к спинке"  # присед ~ приседания
    )
    assert one(lift("жим лёжа на наклонной", 70, 8, exercise="жим лёжа 30°")) is None  # only generic words shared


def test_models_pick_without_a_shared_word_is_dropped():
    assert one(lift("грудные", 40, 10, exercise="жим лёжа 30°")) is None
    assert one(lift("грудные", 40, 10, exercise="жим лежа")) is None  # normalised, still no shared word
    assert one(lift("жим ногами", 200, 10, exercise="присед со штангой")) is None
    assert one(lift("жим лежа на наклонной", 60, 8, exercise="французский жим лёжа")) is None


def test_unknown_exercises_are_dropped():
    assert one(lift("становая", 120, 5)) is None  # not in the catalog, no pick: dropped, never created
    assert one(lift("что-то своё", 40, 10, exercise="несуществующее упражнение")) is None
    assert one(lift(None, 40, 10, exercise=None)) is None
    assert one(lift(None, 40, 10, exercise="жим лёжа")) is None  # nothing said: nothing to compare the pick with


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        ("присед", "приседания", True),
        ("дельты", "дельту", True),
        ("передняя", "перекрестный", False),
        ("жим", "жим", False),  # too short to count
        ("", "приседания", False),
    ],
)
def test_same_stem(a, b, expected):
    assert same_stem(a, b) is expected


@pytest.mark.parametrize(
    ("said", "pick", "expected"),
    [
        ("приседания в гакке", "присед в гаке лицом к спинке", True),
        ("сгибания с ez", "сгибания на бицепс с ez грифом хватом снизу", True),
        ("жим ногами", "присед со штангой", False),
        ("жим лежа на наклонной", "французский жим лёжа", False),  # "лежа", "жим" are generic or short
        ("грудные", "жим лёжа 30°", False),
        ("тяга штанги", "тяга вертикального блока", False),  # "тяг", "штанг", "блок" are generic
    ],
)
def test_shares_word(said, pick, expected):
    assert shares_word(said, pick) is expected


@pytest.mark.parametrize(
    ("kg", "expected"),
    [("80", 80.0), ("80,5", 80.5), ("80.5 кг", 80.5), (80, 80.0), (82.504, 82.5), (1, 1.0), (500, 500.0)],
)
def test_weight_formats_accepted(kg, expected):
    assert one(lift("жим лёжа", kg, 5)).weight_kg == expected


@pytest.mark.parametrize("kg", [0, 0.5, 900, 500.1, -10, None, "много", True, [90], {"kg": 90}])
def test_weight_out_of_range_or_junk_is_dropped(kg):
    assert one(lift("жим лёжа", kg, 5)) is None


@pytest.mark.parametrize(
    ("reps", "expected"),
    [(1, 1), (8, 8), ("8", 8), (100, 100), (None, None), (0, None), (101, None), (-3, None), ("мало", None)],
)
def test_reps(reps, expected):
    assert one(lift("жим лёжа", 90, reps)).reps == expected


def test_duplicate_exercise_keeps_the_first():
    data = {"lifts": [lift("жим лёжа", 90, 8), lift("жим в горизонте", 100, 5), lift("присед", 140, 1)]}
    got = parse_answer(data, CATALOG, YEAR).lifts
    assert [(x.exercise, x.weight_kg, x.reps) for x in got] == [("жим лёжа", 90.0, 8), ("присед со штангой", 140.0, 1)]


def test_invalid_first_duplicate_does_not_block_the_valid_one():
    data = {"lifts": [lift("жим лёжа", 900), lift("жим лёжа", 90, 8)]}
    assert [x.weight_kg for x in parse_answer(data, CATALOG, YEAR).lifts] == [90.0]


def test_profile_height_weight_age():
    got = parse_answer(reply(height_cm=185, weight_kg=85), CATALOG, YEAR).profile
    assert got == {"heightCm": 185, "weightKg": 85.0}
    assert isinstance(got["heightCm"], int)
    assert parse_answer(reply(age=30), CATALOG, 2026).profile == {"birthYear": 1996}
    assert parse_answer(reply(age=30), CATALOG, 2030).profile == {"birthYear": 2000}
    assert parse_answer(reply(birth_year=1990), CATALOG, YEAR).profile == {"birthYear": 1990}
    assert parse_answer(reply(birth_year=1990, age=30), CATALOG, YEAR).profile == {"birthYear": 1990}
    assert parse_answer(reply(height_cm="185 см", weight_kg="85,5"), CATALOG, YEAR).profile == {
        "heightCm": 185, "weightKg": 85.5,
    }  # fmt: skip
    assert parse_answer(reply(height_cm=185.6, weight_kg=85.04), CATALOG, YEAR).profile == {
        "heightCm": 186, "weightKg": 85.0,
    }  # fmt: skip


@pytest.mark.parametrize(
    "body",
    [
        {"height_cm": 50}, {"height_cm": 119}, {"height_cm": 251}, {"height_cm": 1850}, {"height_cm": "abc"},
        {"weight_kg": 20}, {"weight_kg": 29.9}, {"weight_kg": 301}, {"weight_kg": None},
        {"age": 5}, {"age": 9}, {"age": 150}, {"age": 101}, {"age": "n/a"},
        {"birth_year": 1900}, {"birth_year": 2020}, {"birth_year": 1929},
        {"height_cm": True, "weight_kg": False},
    ],
)  # fmt: skip
def test_profile_junk_and_out_of_range_dropped(body):
    assert parse_answer(reply(**body), CATALOG, YEAR).profile == {}


@pytest.mark.parametrize(
    "data",
    [
        None, [], "text", 5, {}, {"lifts": None}, {"lifts": "жим"}, {"lifts": {"said": "жим лёжа"}},
        {"lifts": [None, 5, "x", [], {}]}, {"lifts": [{"said": ["жим"], "weight_kg": 90}]},
        {"lifts": [{"said": "жим лёжа"}]}, {"profile": None}, {"profile": []}, {"profile": "рост 185"},
        {"lifts": [], "profile": {}},
    ],
)  # fmt: skip
def test_malformed_answers_never_raise(data):
    got = parse_answer(data, CATALOG, YEAR)
    assert got.lifts == [] and got.profile == {}


# ---- 2. process_fact ----


FACT = "рост 185 см, вес 85 кг, жим лёжа 90 кг на 8 повторений"


async def test_process_fact_stores_lift_and_profile(db, fake):
    fid = await seed_fact(db, FACT, processed=False)
    fake.fallback = reply(lift("жим лёжа", 90, 8, "жим лёжа"), height_cm=185, weight_kg=85)
    assert await baselines.process_fact(db, fake.client, fid) is True

    async with db() as s:
        fact = await s.get(UserFact, fid)
        rows = (await s.execute(select(ExerciseBaseline, Exercise.name).join(Exercise))).all()
        user = await s.get(User, fact.user_id)
    assert fact.baselines_at is not None
    [(row, name)] = rows
    assert (name, float(row.weight_kg), row.reps, row.fact_id, row.user_id) == ("жим лёжа", 90.0, 8, fid, user.id)
    assert user.height_cm == 185 and float(user.weight_kg) == 85.0

    assert len(fake.bodies) == 1
    system = fake.bodies[0]["messages"][0]["content"]
    assert all(name in system for name in CATALOG)
    assert fake.bodies[0]["messages"][-1]["content"] == f"Факт: {FACT}"


async def test_process_fact_without_digits_skips_the_model(db, fake):
    fid = await seed_fact(db, "не ем творог", processed=False)
    assert await baselines.process_fact(db, fake.client, fid) is True
    assert fake.bodies == []
    assert (await get_fact(db, fid)).baselines_at is not None
    assert await count(db, ExerciseBaseline) == 0


async def test_process_fact_is_not_repeated_for_a_processed_fact(db, fake):
    fid = await seed_fact(db, "не ем творог", processed=False)
    assert await baselines.process_fact(db, fake.client, fid) is True
    assert await baselines.process_fact(db, fake.client, fid) is False
    assert await baselines.process_fact(db, fake.client, 9999) is False  # no such fact
    assert fake.bodies == []


async def test_process_fact_skips_an_inactive_fact(db, fake):
    fid = await seed_fact(db, "жим лёжа 90 на 8", active=False, processed=False)
    assert await baselines.process_fact(db, fake.client, fid) is False
    assert fake.bodies == [] and (await get_fact(db, fid)).baselines_at is None


@pytest.mark.parametrize("failure", ["http500", "not json"])
async def test_process_fact_model_failure_keeps_fact_and_retry_marker(db, fake, failure):
    fid = await seed_fact(db, "жим лёжа 90 на 8", processed=False)
    if failure == "http500":
        fake.status = 500
    else:
        fake.fallback = "Извини, не могу ответить."
    assert await baselines.process_fact(db, fake.client, fid) is False
    assert fake.bodies  # the model was asked
    fact = await get_fact(db, fid)
    assert fact is not None and fact.baselines_at is None  # backfill will try again
    assert await count(db, ExerciseBaseline) == 0


async def test_process_fact_with_a_wrongly_shaped_answer_is_marked_without_weights(db, fake):
    fid = await seed_fact(db, "жим лёжа 90 на 8", processed=False)
    fake.fallback = {"foo": 1}
    assert await baselines.process_fact(db, fake.client, fid) is True
    assert (await get_fact(db, fid)).baselines_at is not None and await count(db, ExerciseBaseline) == 0


async def test_process_fact_drops_unknown_exercise_and_creates_none(db, fake):
    fid = await seed_fact(db, "становая 160 на 3", processed=False)
    fake.fallback = reply(lift("становая", 160, 3))
    before = await count(db, Exercise)
    assert await baselines.process_fact(db, fake.client, fid) is True
    assert await count(db, ExerciseBaseline) == 0 and await count(db, Exercise) == before


async def test_process_fact_replaces_old_baselines_of_the_fact(db, fake):
    fid = await seed_fact(db, "жим лёжа 100 на 5", lifts=[("жим лёжа", 90, 8)], processed=False)
    async with db() as s:  # a fact with stale weights and no marker (as after an edit)
        (await s.get(UserFact, fid)).baselines_at = None
        await s.commit()
    fake.fallback = reply(lift("жим лёжа", 100, 5))
    assert await baselines.process_fact(db, fake.client, fid) is True
    async with db() as s:
        rows = (await s.scalars(select(ExerciseBaseline))).all()
    assert [(float(r.weight_kg), r.reps) for r in rows] == [(100.0, 5)]


async def test_process_fact_writes_nothing_when_the_fact_is_deleted_meanwhile(db, fake):
    fid = await seed_fact(db, FACT, processed=False)
    fake.fallback = reply(lift("жим лёжа", 90, 8), height_cm=185)

    async def delete_fact():
        async with db() as s:
            await s.delete(await s.get(UserFact, fid))
            await s.commit()

    fake.hook = delete_fact
    assert await baselines.process_fact(db, fake.client, fid) is False
    assert await count(db, ExerciseBaseline) == 0 and await get_fact(db, fid) is None
    async with db() as s:
        assert (await get_user(s)).height_cm is None


async def test_process_fact_writes_nothing_when_the_text_changed_meanwhile(db, fake):
    fid = await seed_fact(db, FACT, processed=False)
    fake.fallback = reply(lift("жим лёжа", 90, 8), height_cm=185)

    async def edit_text():
        async with db() as s:
            (await s.get(UserFact, fid)).text = "жим лёжа 100 на 5"
            await s.commit()

    fake.hook = edit_text
    assert await baselines.process_fact(db, fake.client, fid) is False
    assert await count(db, ExerciseBaseline) == 0
    fact = await get_fact(db, fid)
    assert fact.baselines_at is None and fact.text == "жим лёжа 100 на 5"
    async with db() as s:
        assert (await get_user(s)).height_cm is None


async def test_process_fact_keeps_a_profile_value_set_in_the_mini_app(db, fake):
    fid = await seed_fact(db, "вес 85 кг, рост 185", processed=False)
    async with db() as s:
        user = await get_user(s)
        user.weight_kg = Decimal("80.0")
        await s.commit()
    fake.fallback = reply(weight_kg=85, height_cm=185)
    assert await baselines.process_fact(db, fake.client, fid) is True
    async with db() as s:
        user = await get_user(s)
    assert float(user.weight_kg) == 80.0 and user.height_cm == 185


async def test_process_fact_keeps_a_height_that_is_already_set(db, fake):
    fid = await seed_fact(db, "рост 190", processed=False)
    async with db() as s:
        (await get_user(s)).height_cm = 180
        await s.commit()
    fake.fallback = reply(height_cm=190)
    assert await baselines.process_fact(db, fake.client, fid) is True
    async with db() as s:
        assert (await get_user(s)).height_cm == 180


async def test_process_fact_counts_the_age_from_the_year_of_the_fact(db, fake):
    fid = await seed_fact(db, "мне 30 лет", at=datetime(2020, 5, 1, tzinfo=UTC), processed=False)
    fake.fallback = reply(age=30)
    assert await baselines.process_fact(db, fake.client, fid) is True
    async with db() as s:
        assert (await get_user(s)).birth_year == 1990  # 2020 - 30, not the current year - 30


async def test_process_fact_writes_nothing_when_the_fact_is_deactivated_meanwhile(db, fake):
    fid = await seed_fact(db, FACT, processed=False)
    fake.fallback = reply(lift("жим лёжа", 90, 8), height_cm=185, weight_kg=85)

    async def deactivate():
        async with db() as s:
            (await s.get(UserFact, fid)).active = False
            await s.commit()

    fake.hook = deactivate
    assert await baselines.process_fact(db, fake.client, fid) is False
    assert await count(db, ExerciseBaseline) == 0
    fact = await get_fact(db, fid)
    assert fact.baselines_at is None and fact.active is False
    async with db() as s:
        user = await get_user(s)
    assert user.height_cm is None and user.weight_kg is None


async def test_two_concurrent_extractions_of_one_fact_write_one_set_of_rows(db, fake):
    fid = await seed_fact(db, "жим лёжа 90 на 8, присед 140", processed=False)
    fake.fallback = reply(lift("жим лёжа", 90, 8), lift("присед", 140, 1))
    results = await asyncio.gather(
        baselines.process_fact(db, fake.client, fid), baselines.process_fact(db, fake.client, fid)
    )
    assert sorted(results) == [False, True]
    assert await count(db, ExerciseBaseline) == 2  # one per lift, no duplicates
    assert (await get_fact(db, fid)).baselines_at is not None


async def test_process_fact_from_mcp_gives_weights_but_no_profile(db, fake):
    fid = await seed_fact(db, FACT, processed=False)
    async with db() as s:
        (await s.get(UserFact, fid)).source_text = "[mcp] add_fact"
        await s.commit()
    fake.fallback = reply(lift("жим лёжа", 90, 8), height_cm=185, weight_kg=85)
    assert await baselines.process_fact(db, fake.client, fid) is True
    assert await count(db, ExerciseBaseline) == 1
    async with db() as s:
        user = await get_user(s)
    assert user.height_cm is None and user.weight_kg is None


# ---- 3. backfill ----


async def test_backfill_processes_active_facts_once(db, fake):
    await seed_fact(db, "жим лёжа 90 на 8", at=T1, processed=False)
    await seed_fact(db, "присед 140 на раз", at=T2, processed=False)
    off = await seed_fact(db, "румынка 100", at=T2, active=False, processed=False)
    fake.answers = [reply(lift("жим лёжа", 90, 8)), reply(lift("присед", 140, 1))]

    assert await baselines.backfill(db, fake.client) == 2
    assert len(fake.bodies) == 2
    assert [b["messages"][-1]["content"] for b in fake.bodies] == [  # oldest first
        "Факт: жим лёжа 90 на 8", "Факт: присед 140 на раз",
    ]  # fmt: skip
    async with db() as s:
        rows = (await s.execute(select(Exercise.name, ExerciseBaseline.weight_kg, ExerciseBaseline.reps)
                                .join(ExerciseBaseline).order_by(ExerciseBaseline.id))).all()
    assert [(n, float(w), r) for n, w, r in rows] == [("жим лёжа", 90.0, 8), ("присед со штангой", 140.0, 1)]
    assert (await get_fact(db, off)).baselines_at is None  # inactive facts wait

    assert await baselines.backfill(db, fake.client) == 0
    assert len(fake.bodies) == 2  # no new model calls


async def test_backfill_retries_after_a_failure(db, fake):
    await seed_fact(db, "жим лёжа 90 на 8", processed=False)
    fake.status = 500
    assert await baselines.backfill(db, fake.client) == 0
    calls = len(fake.bodies)
    assert calls > 0
    fake.status, fake.fallback = 200, reply(lift("жим лёжа", 90, 8))
    assert await baselines.backfill(db, fake.client) == 1
    assert await count(db, ExerciseBaseline) == 1 and len(fake.bodies) == calls + 1


async def test_backfill_stops_at_the_first_model_failure(db, fake, pauses):
    await seed_fact(db, "жим лёжа 90 на 8", at=T1, processed=False)
    await seed_fact(db, "присед 140 на раз", at=T1.replace(hour=13), processed=False)
    await seed_fact(db, "румынка 100", at=T2, processed=False)
    fake.status = 500
    assert await baselines.backfill(db, fake.client) == 0
    assert fake.bodies  # the model was asked...
    assert {b["messages"][-1]["content"] for b in fake.bodies} == {"Факт: жим лёжа 90 на 8"}  # ...for the first fact only
    assert pauses == []
    assert all(f.baselines_at is None for f in await _all_facts(db))


async def test_backfill_pauses_between_model_calls_only(db, fake, pauses):
    await seed_fact(db, "не ем творог", at=T1, processed=False)  # no digit: free, no pause, no call
    await seed_fact(db, "жим лёжа 90 на 8", at=T1.replace(hour=13), processed=False)
    await seed_fact(db, "присед 140 на раз", at=T1.replace(hour=14), processed=False)
    await seed_fact(db, "румынка 100", at=T2, processed=False)
    fake.fallback = reply()
    assert await baselines.backfill(db, fake.client) == 4
    assert len(fake.bodies) == 3
    assert pauses == [baselines.BACKFILL_PAUSE, baselines.BACKFILL_PAUSE] == [3.0, 3.0]


async def test_backfill_pause_is_configurable(db, fake, pauses):
    await seed_fact(db, "жим лёжа 90 на 8", at=T1, processed=False)
    await seed_fact(db, "присед 140 на раз", at=T2, processed=False)
    fake.fallback = reply()
    assert await baselines.backfill(db, fake.client, pause=0.5) == 2
    assert pauses == [0.5]


async def test_backfill_makes_at_most_cap_model_calls_per_run(db, fake):
    await seed_fact(db, "жим лёжа 90 на 8", at=T1, processed=False)
    await seed_fact(db, "присед 140 на раз", at=T1.replace(hour=13), processed=False)
    third = await seed_fact(db, "румынка 100", at=T2, processed=False)
    fake.fallback = reply()
    assert await baselines.backfill(db, fake.client, cap=2) == 2
    assert len(fake.bodies) == 2
    assert (await get_fact(db, third)).baselines_at is None
    assert await baselines.backfill(db, fake.client, cap=2) == 1  # the next start picks it up
    assert len(fake.bodies) == 3 and (await get_fact(db, third)).baselines_at is not None


async def test_backfill_facts_without_a_digit_do_not_count_towards_the_cap(db, fake):
    for i in range(3):
        await seed_fact(db, f"не ем творог {'я' * i}", at=T1.replace(hour=10 + i), processed=False)
    last = await seed_fact(db, "жим лёжа 90 на 8", at=T2, processed=False)
    fake.fallback = reply()
    assert await baselines.backfill(db, fake.client, cap=1) == 4
    assert len(fake.bodies) == 1 and (await get_fact(db, last)).baselines_at is not None


# ---- 4. profile rule: a fact only fills empty fields ----


async def test_fill_from_fact_fills_empty_fields(db):
    async with db() as s:
        user = await get_user(s)
        values = {"heightCm": 185, "weightKg": 85.0, "birthYear": 1996}
        assert await profile.fill_from_fact(s, user.id, values) == ["weightKg", "heightCm", "birthYear"]
        await s.commit()
    async with db() as s:
        user = await get_user(s)
    assert user.weight_kg == Decimal("85.0") and user.height_cm == 185 and user.birth_year == 1996


async def test_fill_from_fact_never_overwrites_a_set_field(db):
    async with db() as s:
        user = await get_user(s)
        user.weight_kg, user.height_cm = Decimal("80.0"), 180
        await s.flush()
        got = await profile.fill_from_fact(s, user.id, {"weightKg": 85.0, "heightCm": 185, "birthYear": 1996})
        assert got == ["birthYear"]
        await s.commit()
    async with db() as s:
        user = await get_user(s)
    assert user.weight_kg == Decimal("80.0") and user.height_cm == 180 and user.birth_year == 1996


async def test_fill_from_fact_same_value_ignored_keys_and_none(db):
    async with db() as s:
        user = await get_user(s)
        user.weight_kg = Decimal("85.0")
        await s.flush()
        values = {"weightKg": 85.0, "goal": "mass", "about": "x", "heightCm": None}
        assert await profile.fill_from_fact(s, user.id, values) == []
        assert await profile.fill_from_fact(s, user.id, {}) == []
        await s.commit()
    async with db() as s:
        user = await get_user(s)
    assert user.goal is None and user.about is None and user.height_cm is None


async def test_fill_from_fact_does_not_commit(db):
    async with db() as s:
        user = await get_user(s)
        await s.commit()
        assert await profile.fill_from_fact(s, user.id, {"heightCm": 185}) == ["heightCm"]
        await s.rollback()
    async with db() as s:
        assert (await get_user(s)).height_cm is None


async def test_fill_from_fact_rounds_the_weight(db):
    async with db() as s:
        user = await get_user(s)
        assert await profile.fill_from_fact(s, user.id, {"weightKg": 85.04}) == ["weightKg"]
        await s.commit()
    async with db() as s:
        assert (await get_user(s)).weight_kg == Decimal("85.0")


def test_set_profile_rounds_the_weight_and_changes_other_fields():
    user = User(telegram_id=1, rest_seconds=90)
    profile.set_profile(user, {"weightKg": 80.04, "goal": "mass", "about": "болит плечо", "heightCm": 181})
    assert user.weight_kg == Decimal("80.0") and user.goal == "mass" and user.about == "болит плечо"
    assert user.height_cm == 181
    profile.set_profile(user, {"goal": "cut", "birthYear": 1990})
    assert user.goal == "cut" and user.birth_year == 1990 and user.weight_kg == Decimal("80.0")  # others stay
    profile.set_profile(user, {"birthYear": None})  # null resets
    assert user.birth_year is None
    assert not hasattr(User, "profile_updated_at")


# ---- 5. cascade and visibility ----


async def test_orm_delete_of_a_fact_removes_its_baselines(db):
    fid = await seed_fact(db, "жим лёжа 90 на 8, присед 140", lifts=[("жим лёжа", 90, 8), ("присед со штангой", 140, 1)])
    other = await seed_fact(db, "румынка 100", lifts=[("румынская тяга", 100, None)], at=T2)
    assert await count(db, ExerciseBaseline) == 3
    async with db() as s:
        await s.delete(await s.get(UserFact, fid))
        await s.commit()
    async with db() as s:
        left = (await s.scalars(select(ExerciseBaseline))).all()
    assert [b.fact_id for b in left] == [other]


async def test_api_delete_fact_removes_baselines(client, db, auth):
    fid = await seed_fact(db, "жим лёжа 90 на 8", lifts=[("жим лёжа", 90, 8)])
    keep = await seed_fact(db, "румынка 100", lifts=[("румынская тяга", 100, None)], at=T2)
    assert (await client.delete(f"/api/facts/{fid}", headers=auth)).status_code == 204
    async with db() as s:
        left = (await s.scalars(select(ExerciseBaseline))).all()
    assert [b.fact_id for b in left] == [keep]
    assert [b["exercise"] for b in await state_baselines(client, auth)] == ["румынская тяга"]


def callback(data: str, user_id: int = TG):
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=user_id, full_name="Amir"),
        message=SimpleNamespace(edit_text=AsyncMock(), edit_reply_markup=AsyncMock()),
        answer=AsyncMock(),
    )


async def test_facts_command_delete_button_removes_baselines(db):
    fid = await seed_fact(db, "жим лёжа 90 на 8", lifts=[("жим лёжа", 90, 8)])
    keep = await seed_fact(db, "румынка 100", lifts=[("румынская тяга", 100, None)], at=T2)
    cb = callback(f"fact_del:{fid}")
    await facts_handler.delete_fact(cb, db)
    cb.answer.assert_awaited_once_with("Забыл.")
    assert await get_fact(db, fid) is None
    async with db() as s:
        left = (await s.scalars(select(ExerciseBaseline))).all()
    assert [b.fact_id for b in left] == [keep]


async def test_deactivating_hides_and_reactivating_restores_baselines(client, db, auth):
    fid = await seed_fact(db, "жим лёжа 90 на 8", lifts=[("жим лёжа", 90, 8)])
    assert [b["weightKg"] for b in await state_baselines(client, auth)] == [90.0]

    r = await client.patch(f"/api/facts/{fid}", json={"active": False}, headers=auth)
    assert r.status_code == 200 and r.json()["active"] is False
    assert await state_baselines(client, auth) == []
    assert await count(db, ExerciseBaseline) == 1  # hidden, not deleted

    r = await client.patch(f"/api/facts/{fid}", json={"active": True}, headers=auth)
    assert r.status_code == 200
    assert await state_baselines(client, auth) == [{"exercise": "жим лёжа", "weightKg": 90.0, "reps": 8, "factId": fid}]


async def test_patching_the_text_resets_baselines(client, db, auth):
    fid = await seed_fact(db, "жим лёжа 90 на 8", lifts=[("жим лёжа", 90, 8)])
    assert (await get_fact(db, fid)).baselines_at is not None
    r = await client.patch(f"/api/facts/{fid}", json={"text": "жим лёжа 100 на 5"}, headers=auth)
    assert r.status_code == 200
    assert await count(db, ExerciseBaseline) == 0
    assert (await get_fact(db, fid)).baselines_at is None
    assert await state_baselines(client, auth) == []


async def test_patching_without_a_text_change_keeps_baselines(client, db, auth):
    fid = await seed_fact(db, "жим лёжа 90 на 8", lifts=[("жим лёжа", 90, 8)])
    for body in ({"category": "health"}, {"text": "  жим лёжа 90 на 8  "}):
        assert (await client.patch(f"/api/facts/{fid}", json=body, headers=auth)).status_code == 200
    assert await count(db, ExerciseBaseline) == 1 and (await get_fact(db, fid)).baselines_at is not None


async def test_patching_the_text_with_a_model_processes_the_new_text(db, fake, settings, auth):
    fid = await seed_fact(db, "жим лёжа 90 на 8", lifts=[("жим лёжа", 90, 8)])
    fake.fallback = reply(lift("жим лёжа", 100, 5))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(settings, db, llm=fake.client)), base_url="http://t"
    ) as c:
        assert (await c.patch(f"/api/facts/{fid}", json={"text": "жим лёжа 100 на 5"}, headers=auth)).status_code == 200
        await baselines.drain()
        assert await state_baselines(c, auth) == [{"exercise": "жим лёжа", "weightKg": 100.0, "reps": 5, "factId": fid}]
    assert (await get_fact(db, fid)).baselines_at is not None


# ---- 6. API shape ----


async def test_state_baselines_empty_for_a_new_user(client, auth):
    r = await client.get("/api/state", headers=auth)
    assert r.status_code == 200 and r.json()["baselines"] == []


async def test_state_baselines_shape_and_exact_program_names(client, db, auth):
    fid = await seed_fact(
        db, "жим лёжа 90 на 8, присед 140, румынка ~100",
        lifts=[("жим лёжа", 90, 8), ("присед со штангой", 140, 1), ("румынская тяга", 100, None)],
    )  # fmt: skip
    got = await state_baselines(client, auth)
    assert got == [
        {"exercise": "жим лёжа", "weightKg": 90.0, "reps": 8, "factId": fid},
        {"exercise": "присед со штангой", "weightKg": 140.0, "reps": 1, "factId": fid},
        {"exercise": "румынская тяга", "weightKg": 100.0, "reps": None, "factId": fid},
    ]
    assert all(set(b) == {"exercise", "weightKg", "reps", "factId"} for b in got)
    assert all(b["exercise"] in CATALOG and isinstance(b["weightKg"], float) for b in got)


async def test_state_baselines_newest_fact_wins_per_exercise(client, db, auth):
    await seed_fact(db, "жим лёжа 80 на 10", lifts=[("жим лёжа", 80, 10)], at=T1)
    newer = await seed_fact(db, "жим лёжа 90 на 8", lifts=[("жим лёжа", 90, 8)], at=T2)
    got = await state_baselines(client, auth)
    assert got == [{"exercise": "жим лёжа", "weightKg": 90.0, "reps": 8, "factId": newer}]


async def test_state_baselines_older_fact_still_gives_other_exercises(client, db, auth):
    old = await seed_fact(db, "жим лёжа 80, присед 120", lifts=[("жим лёжа", 80, None), ("присед со штангой", 120, 5)], at=T1)
    new = await seed_fact(db, "жим лёжа 90 на 8", lifts=[("жим лёжа", 90, 8)], at=T2)
    got = {b["exercise"]: (b["weightKg"], b["factId"]) for b in await state_baselines(client, auth)}
    assert got == {"жим лёжа": (90.0, new), "присед со штангой": (120.0, old)}


async def test_state_baselines_do_not_leak_between_users(client, db, auth):
    await seed_fact(db, "не ем творог")  # the owner (first user) is the Mini App user
    await seed_fact(db, "жим лёжа 200", lifts=[("жим лёжа", 200, 1)], telegram_id=777)
    assert await state_baselines(client, auth) == []


# ---- 7. advice context ----


async def test_advice_context_has_the_working_weights_line(db):
    await seed_fact(db, "румынка ~100", lifts=[("румынская тяга", 100, None)], at=T1)
    await seed_fact(
        db, "жим лёжа 90 на 8, присед 140 на раз",
        lifts=[("жим лёжа", 90, 8), ("присед со штангой", 140, 1)], at=T2,
    )  # fmt: skip
    async with db() as s:
        user = await get_user(s)
        ctx = await advice.build_context(s, user, None, MSK, NOW)
    assert "Рабочие веса с твоих слов: жим лёжа 90×8, присед со штангой 140×1 (максимум), румынская тяга ~100." in ctx
    assert len(ctx) <= advice.CONTEXT_MAX


async def test_advice_context_without_baselines_has_no_line(db):
    await seed_fact(db, "не ем творог")
    await seed_fact(db, "жим лёжа 90", lifts=[("жим лёжа", 90, 8)], active=False)  # inactive: not counted
    async with db() as s:
        user = await get_user(s)
        ctx = await advice.build_context(s, user, None, MSK, NOW)
    assert "Рабочие веса" not in ctx


def out(name: str, kg: float = 90.0, reps: int | None = 8, fact_id: int = 1) -> BaselineOut:
    return BaselineOut(exercise=name, weightKg=kg, reps=reps, factId=fact_id)


def test_context_line_format():
    items = [out("жим лёжа"), out("присед со штангой", 140, 1), out("румынская тяга", 100, None), out("x", 82.5, 10)]
    assert baselines.context_line(items) == (
        "Рабочие веса с твоих слов: жим лёжа 90×8, присед со штангой 140×1 (максимум), румынская тяга ~100, x 82.5×10."
    )


def test_context_line_empty_and_capped():
    assert baselines.context_line([]) == ""
    assert baselines.context_line([out("жим лёжа")], max_chars=10) == ""  # not even the first one fits
    many = [out("упражнение номер " + "я" * 20 + str(i), 100.0 + i, 10) for i in range(20)]
    line = baselines.context_line(many)
    assert 0 < len(line) <= baselines.CONTEXT_CHARS and line.endswith(".")
    assert line.count("×") <= baselines.IN_CONTEXT
    assert "упражнение номер " + "я" * 20 + "0 100×10" in line  # newest-first order is kept: the first item leads
    tight = baselines.context_line(many, max_chars=100)
    assert len(tight) <= 100 and tight.startswith("Рабочие веса с твоих слов: ")


def test_context_line_takes_at_most_in_context_items():
    short = [out(f"у{i}", 10.0, 10) for i in range(20)]
    line = baselines.context_line(short)
    assert line.count("×") == baselines.IN_CONTEXT


# ---- 8. scheduling ----


def message(body: str):
    return SimpleNamespace(
        text=body,
        date=T1,
        from_user=SimpleNamespace(id=TG, full_name="Amir"),
        chat=SimpleNamespace(id=TG),
        bot=SimpleNamespace(send_chat_action=AsyncMock()),
        answer=AsyncMock(),
    )


async def offer_fact(body: str, settings, db, llm) -> str:
    msg = message(body)
    await log_text.log_free_text(msg, settings, db, llm)
    kb = msg.answer.await_args.kwargs["reply_markup"]
    return kb.inline_keyboard[0][0].callback_data.split(":", 1)[1]


async def test_remember_with_llm_schedules_baselines(db, fake, settings):
    token = await offer_fact("запомни: жим лёжа 90 на 8", settings, db, fake.client)
    assert fake.bodies == []  # the offer itself never calls the model
    fake.fallback = reply(lift("жим лёжа", 90, 8))
    await log_text.remember(callback(f"remember:{token}"), db, llm=fake.client)
    await baselines.drain()
    async with db() as s:
        [(row, name)] = (await s.execute(select(ExerciseBaseline, Exercise.name).join(Exercise))).all()
    assert (name, float(row.weight_kg), row.reps) == ("жим лёжа", 90.0, 8)
    assert len(fake.bodies) == 1


async def test_remember_without_llm_schedules_nothing(db, fake, settings, monkeypatch):
    spawned = []
    monkeypatch.setattr(baselines, "_spawn", lambda coro, what: (coro.close(), spawned.append(what)))
    token = await offer_fact("запомни: жим лёжа 90 на 8", settings, db, fake.client)
    cb = callback(f"remember:{token}")
    await log_text.remember(cb, db)  # the old call style
    await baselines.drain()
    assert spawned == [] and fake.bodies == []
    [fact] = (await _all_facts(db))
    assert fact.text == "жим лёжа 90 на 8" and fact.baselines_at is None
    assert await count(db, ExerciseBaseline) == 0
    assert cb.message.edit_text.await_args.args[0].startswith("Запомнил")


async def _all_facts(db) -> list[UserFact]:
    async with db() as s:
        return list((await s.scalars(select(UserFact))).all())


async def test_remember_duplicate_does_not_break_scheduling(db, fake, settings):
    await seed_fact(db, "жим лёжа 90 на 8", lifts=[("жим лёжа", 90, 8)])
    token = await offer_fact("запомни: жим лёжа 90 на 8", settings, db, fake.client)
    cb = callback(f"remember:{token}")
    await log_text.remember(cb, db, llm=fake.client)
    await baselines.drain()
    assert fake.bodies == []  # the existing fact is already processed
    assert len(await _all_facts(db)) == 1 and await count(db, ExerciseBaseline) == 1


async def test_api_create_fact_with_llm_schedules_baselines(db, fake, settings, auth):
    fake.fallback = reply(lift("жим лёжа", 90, 8), height_cm=185)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(settings, db, llm=fake.client)), base_url="http://t"
    ) as c:
        r = await c.post("/api/facts", json={"text": "жим лёжа 90 на 8, рост 185"}, headers=auth)
        assert r.status_code == 201
        fid = r.json()["id"]
        await baselines.drain()
        assert await state_baselines(c, auth) == [{"exercise": "жим лёжа", "weightKg": 90.0, "reps": 8, "factId": fid}]
        profile_out = (await c.get("/api/state", headers=auth)).json()["profile"]
    assert profile_out["heightCm"] == 185 and (await get_fact(db, fid)).baselines_at is not None


async def test_api_create_fact_without_llm_schedules_nothing(client, db, auth, monkeypatch):
    spawned = []
    monkeypatch.setattr(baselines, "_spawn", lambda coro, what: (coro.close(), spawned.append(what)))
    r = await client.post("/api/facts", json={"text": "жим лёжа 90 на 8"}, headers=auth)
    assert r.status_code == 201
    await baselines.drain()
    assert spawned == []
    assert (await get_fact(db, r.json()["id"])).baselines_at is None
    assert await state_baselines(client, auth) == []


async def test_api_duplicate_fact_post_does_not_reprocess(db, fake, settings, auth):
    fake.fallback = reply(lift("жим лёжа", 90, 8))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(settings, db, llm=fake.client)), base_url="http://t"
    ) as c:
        assert (await c.post("/api/facts", json={"text": "жим лёжа 90 на 8"}, headers=auth)).status_code == 201
        await baselines.drain()
        assert (await c.post("/api/facts", json={"text": "жим лёжа 90 на 8"}, headers=auth)).status_code == 200
        await baselines.drain()
    assert len(fake.bodies) == 1 and await count(db, ExerciseBaseline) == 1


def test_schedule_needs_a_client_with_routes(db, settings):
    assert baselines.schedule(db, None, 1) is None
    assert baselines.start_backfill(db, None) is None
    keyless = OpenRouterClient(settings)  # no keys in the test settings: no routes
    assert keyless.routes == [] and baselines.schedule(db, keyless, 1) is None
    assert not baselines.enabled(keyless) and not baselines.enabled(None)


async def test_background_failure_is_logged_not_raised(db, fake):
    fake.status = 500
    fid = await seed_fact(db, "жим лёжа 90 на 8", processed=False)
    task = baselines.schedule(db, fake.client, fid)
    assert task is not None
    await baselines.drain()
    assert task.done() and task.exception() is None
    assert (await get_fact(db, fid)).baselines_at is None


async def test_start_backfill_runs_in_background(db, fake):
    await seed_fact(db, "жим лёжа 90 на 8", processed=False)
    fake.fallback = reply(lift("жим лёжа", 90, 8))
    assert baselines.start_backfill(db, fake.client) is not None
    await baselines.drain()
    assert await count(db, ExerciseBaseline) == 1


# ---- 10. facts written over MCP: weights yes, profile no, notes nothing ----


@asynccontextmanager
async def mcp_running(tmp_path, db, llm):
    """The test_mcp harness with a model: `running` there builds the app without one."""
    app = create_app(make_settings(tmp_path, mcp_token=MCP_TOKEN, public_url="https://t"), db, llm=llm)
    async with app.router.lifespan_context(app):
        yield app


async def test_mcp_add_fact_gives_weights_but_never_the_profile(tmp_path, db, fake):
    await make_owner(db)
    fake.fallback = reply(lift("жим лёжа", 90, 8), height_cm=185, weight_kg=85)
    async with mcp_running(tmp_path, db, fake.client) as app, mcp_client(app) as client:
        added = data(await call(client, "add_fact", text="жим лёжа 90 на 8, рост 185, вес 85"))
        await baselines.drain()
    fid = added["fact"]["id"]
    async with db() as s:
        [(row, name)] = (await s.execute(select(ExerciseBaseline, Exercise.name).join(Exercise))).all()
        user = await get_user(s)
    assert (name, float(row.weight_kg), row.reps, row.fact_id) == ("жим лёжа", 90.0, 8, fid)
    assert (await get_fact(db, fid)).baselines_at is not None
    assert user.height_cm is None and user.weight_kg is None
    assert len(fake.bodies) == 1


async def test_backfill_of_an_mcp_fact_gives_weights_but_leaves_the_profile(db, fake):
    fid = await seed_fact(db, "жим лёжа 90 на 8, рост 185, вес 85", processed=False)
    async with db() as s:
        (await s.get(UserFact, fid)).source_text = "[mcp] add_fact"
        await s.commit()
    fake.fallback = reply(lift("жим лёжа", 90, 8), height_cm=185, weight_kg=85)
    assert await baselines.backfill(db, fake.client) == 1
    assert await count(db, ExerciseBaseline) == 1
    async with db() as s:
        user = await get_user(s)
    assert user.height_cm is None and user.weight_kg is None and user.birth_year is None


async def test_backfill_marks_an_mcp_note_without_calling_the_model(db, fake):
    fid = await seed_fact(db, "[mcp] жим лёжа 90 на 8, рост 185", processed=False)
    async with db() as s:
        (await s.get(UserFact, fid)).source_text = "[mcp] log_note"
        await s.commit()
    assert await baselines.backfill(db, fake.client) == 1
    assert len(fake.bodies) == 0
    assert (await get_fact(db, fid)).baselines_at is not None
    assert await count(db, ExerciseBaseline) == 0


async def test_mcp_log_note_with_digits_never_asks_the_model(tmp_path, db, fake):
    await make_owner(db)
    fake.fallback = reply(lift("жим лёжа", 90, 8))
    async with mcp_running(tmp_path, db, fake.client) as app, mcp_client(app) as client:
        note = data(await call(client, "log_note", text="жим лёжа 90 на 8"))
        await baselines.drain()
    assert fake.bodies == [] and await count(db, ExerciseBaseline) == 0
    assert (await get_fact(db, note["fact"]["id"])).source_text == "[mcp] log_note"
    assert await baselines.backfill(db, fake.client) == 1  # picked up by a restart: marked, still no model
    assert fake.bodies == []


# ---- 11. migration 0008 ----


def _tables_and_columns(conn):
    insp = inspect(conn)
    return {
        "tables": set(insp.get_table_names()),
        "user_facts": {c["name"] for c in insp.get_columns("user_facts")},
        "users": {c["name"] for c in insp.get_columns("users")},
    }


async def test_migration_0008_round_trip(tmp_path):
    engine, _ = make_engine(f"sqlite+aiosqlite:///{tmp_path}/m.db")

    def run(fn):
        async def go():
            async with engine.begin() as conn:
                return await conn.run_sync(fn)

        return go()

    def migrate_to(target: str):
        def fn(conn):
            cfg = migrate._config()
            cfg.attributes["connection"] = conn
            (command.upgrade if target == "head" else command.downgrade)(cfg, target)

        return run(fn)

    try:
        await migrate_to("head")
        up = await run(_tables_and_columns)
        assert "exercise_baselines" in up["tables"]
        assert "baselines_at" in up["user_facts"] and "profile_updated_at" not in up["users"]

        async with engine.begin() as conn:
            await conn.execute(text("INSERT INTO users (telegram_id, rest_seconds, created_at) VALUES (1, 90, '2026-10-01 12:00:00')"))
            await conn.execute(
                text("INSERT INTO user_facts (user_id, text, category, active, created_at) "
                     "VALUES (1, 'не ем творог', 'food', 1, '2026-10-01 12:00:00')")
            )  # fmt: skip

        await migrate_to("0007")
        down = await run(_tables_and_columns)
        assert "exercise_baselines" not in down["tables"]
        assert "baselines_at" not in down["user_facts"] and "profile_updated_at" not in down["users"]
        async with engine.connect() as conn:
            assert (await conn.execute(text("SELECT text FROM user_facts"))).scalar_one() == "не ем творог"

        await migrate_to("head")
        again = await run(_tables_and_columns)
        assert again == up
        async with engine.connect() as conn:
            row = (await conn.execute(text("SELECT text, baselines_at FROM user_facts"))).one()
        assert tuple(row) == ("не ем творог", None)  # the old fact is picked up by backfill
    finally:
        await engine.dispose()
