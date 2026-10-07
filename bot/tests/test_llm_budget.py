"""Fewer tokens and calls per message: the parser gets the exercise catalog only when the text may be a
workout (gymbot.llm.prompts.needs_catalog), and the diary answer reuses the summary for a minute
(gymbot.services.answer.ContextCache). The plan refinement is skipped by its inputs hash (tests/test_plan.py:
test_cache_and_regenerate, test_repeated_rebuild_after_wellbeing_does_not_call_the_model)."""

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest
from test_answer_done import OWNER, _seed

from gymbot.db.models import User
from gymbot.llm.prompts import CATALOG_LINE, EXAMPLES, SYSTEM_PROMPT, build_messages, needs_catalog
from gymbot.services import active_workout, advice, answer, live

CATALOG = ["жим лёжа", "румынская тяга", "сгибания с гантелями на бицепс с супинацией", "тяга вертикального блока"]
WORKOUT_JSON = '{"kind":"workout","exercises":[{"exercise":"жим лёжа","sets":[{"reps":10,"weight_kg":60}]}]}'


# ---- the parser's catalog ----


def test_catalog_line_is_in_the_prompt():
    assert CATALOG_LINE in SYSTEM_PROMPT


@pytest.mark.parametrize(
    "text",
    ["жим 3х10", "сделал 3 по 10", "жал 80 на 8", "присед 100 на 5", "румынка 70 на 10", "сгибания на бицепс",
     "отжимания 20 раз", "вертикальная тяга 55", "брусья три по десять", "бицуха три по двенадцать на пятнадцать"],
)
def test_workout_like_text_gets_the_catalog(text):
    assert needs_catalog(text, CATALOG)
    assert "тяга вертикального блока" in build_messages(text, CATALOG)[0]["content"]


@pytest.mark.parametrize(
    "text",
    ["плов, касушку и пол лепёшки", "запеканка", "привет", "пожалуйста, запомни", "что сегодня?",
     "что подтянуть на следующей тренировке?", "сил мало, спал плохо"],
)
def test_other_text_goes_without_the_catalog(text):
    assert not needs_catalog(text, CATALOG)
    system = build_messages(text, CATALOG)[0]["content"]
    assert "каталоге" not in system and "тяга вертикального блока" not in system
    assert "{catalog}" not in system


def test_a_correction_of_a_workout_keeps_the_catalog():
    assert needs_catalog("нет, 65", CATALOG, [("жим 60", WORKOUT_JSON)])
    # Any number keeps the catalog, food dialog or not: a few tokens are cheaper than a split history.
    assert needs_catalog("нет, четыре", CATALOG, [("три самсы", '{"kind":"food","foods":[]}')])
    for text in ["съел 2 самсы", "яблоко 200 г", "спал 6 часов, сил мало", "я на два дня уеду"]:
        assert needs_catalog(text, CATALOG)


def test_catalog_word_without_gym_words_gets_the_catalog():
    assert needs_catalog("супинация зашла", CATALOG)  # a word of a catalog name


def test_examples_have_no_empty_lists():
    assert all('"exercises":[]' not in a and '"foods":[]' not in a for _, a in EXAMPLES)


# ---- the answer's summary cache ----

TZ = ZoneInfo("Europe/Moscow")
NOW = datetime(2026, 10, 8, 9, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def counted(monkeypatch):
    """Counts summary builds (advice.build_context calls)."""
    calls = []
    real = advice.build_context

    async def build(*a, **kw):
        calls.append(1)
        return await real(*a, **kw)

    monkeypatch.setattr(advice, "build_context", build)
    return calls


async def test_summary_is_reused_within_the_ttl(db, settings, counted):
    clock = Clock()
    cache = answer.ContextCache(clock=clock)
    async with db() as s:
        user = await s.get(User, await _seed(s, date(2026, 10, 7), OWNER))
        first = await answer.gather(s, user, settings, None, TZ, NOW, cache)
        clock.t += answer.CONTEXT_TTL - 1
        second = await answer.gather(s, user, settings, None, TZ, NOW, cache)
        assert len(counted) == 1 and second.text == first.text and second.history.keys() == first.history.keys()
        clock.t += 2
        await answer.gather(s, user, settings, None, TZ, NOW, cache)
        assert len(counted) == 2  # expired


async def test_a_published_change_or_a_new_day_rebuilds(db, settings, counted):
    cache = answer.ContextCache(clock=Clock())
    async with db() as s:
        user = await s.get(User, await _seed(s, date(2026, 10, 7), OWNER))
        await answer.gather(s, user, settings, None, TZ, NOW, cache)
        live.publish(user.id, "nutrition")  # e.g. food logged from the chat
        await answer.gather(s, user, settings, None, TZ, NOW, cache)
        assert len(counted) == 2
        await answer.gather(s, user, settings, None, TZ, NOW.replace(day=9), cache)
        assert len(counted) == 3


async def test_workout_in_progress_is_always_fresh(db, settings, counted, monkeypatch):
    lines = iter(["Сейчас идёт тренировка в мини-аппе: жим лёжа 60×10.", "Сейчас идёт тренировка в мини-аппе: жим лёжа 60×10, 60×9."])

    async def context_for(*_a):
        return next(lines)

    monkeypatch.setattr(active_workout, "context_for", context_for)
    cache = answer.ContextCache(clock=Clock())
    async with db() as s:
        user = await s.get(User, await _seed(s, date(2026, 10, 7), OWNER))
        first = await answer.gather(s, user, settings, None, TZ, NOW, cache)
        second = await answer.gather(s, user, settings, None, TZ, NOW, cache)
    assert len(counted) == 1
    assert first.text.endswith("60×10.") and second.text.endswith("60×10, 60×9.")
    assert second.in_progress == ["жим лёжа"]


def test_one_cache_per_database(db):
    assert answer.cache_for(db) is answer.cache_for(db)


import pytest as _pytest


@_pytest.mark.parametrize(
    "text",
    [
        "икры 20 20 20",
        "трапеция 40 кг 12 раз",
        "хаммеры 14 кг 12 раз",
        "бабочка 50 12 12 10",
        "брусья 12 10 8",
        "турник 10 8 6",
        "Сделал хаммер двенадцать двенадцать десять",
    ],
)
def test_any_numbers_keep_the_catalog(text):
    from gymbot.llm.prompts import needs_catalog

    assert needs_catalog(text, ["молотки", "сгибания с гантелями на бицепс"], None)
