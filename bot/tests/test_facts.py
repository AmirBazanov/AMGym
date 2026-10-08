"""User facts: the service shared by the bot and the API, and the /facts command."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from gymbot.db.models import User, UserFact
from gymbot.handlers import facts as facts_handler
from gymbot.services import facts

TG = 42


@pytest.mark.parametrize(
    ("text", "category"),
    [
        ("не ем творог", "food"),
        ("самса у нас ~150 г", "food"),
        ("аллергия на орехи", "health"),
        ("болит левое колено, без прыжков", "health"),
        ("жим лёжа делаю только с гантелями", "training"),
        ("тренируюсь по утрам", "training"),
        ("работаю в ночную смену по вторникам", "schedule"),
        ("люблю синий цвет", "other"),
    ],
)
def test_guess_category(text, category):
    assert facts.guess_category(text) == category


def test_clean_and_normalize():
    assert facts.clean("  не ем   творог \n") == "не ем творог"
    assert facts.normalize("«Не ем творог!»") == facts.normalize("не ем творог") == facts.normalize("НЕ ЕМ ТВОРОГ.")
    assert facts.normalize("ёжик") == facts.normalize("ежик")


async def _user(db) -> int:
    async with db() as s:
        user = User(telegram_id=TG, rest_seconds=90)
        s.add(user)
        await s.commit()
        return user.id


async def test_add_fact_created_duplicate_limit(db):
    uid = await _user(db)
    async with db() as s:
        r = await facts.add_fact(s, uid, "  не ем  творог ", source_text="запомни: не ем творог")
        assert r.status == "created" and r.fact.text == "не ем творог" and r.fact.category == "food"
        dup = await facts.add_fact(s, uid, "Не ем творог.")
        assert dup.status == "duplicate" and dup.fact.id == r.fact.id
        r.fact.active = False
        again = await facts.add_fact(s, uid, "не ем творог")  # only active facts count as duplicates
        assert again.status == "created" and again.fact.id != r.fact.id
        for i in range(facts.MAX_ACTIVE - 1):
            s.add(UserFact(user_id=uid, text=f"факт {i}", category="other", active=True))
        await s.flush()
        full = await facts.add_fact(s, uid, "новый")
        assert full.status == "limit" and full.fact is None
        await s.commit()
    with pytest.raises(ValueError):
        async with db() as s:
            await facts.add_fact(s, uid, "   ")
    with pytest.raises(ValueError):
        async with db() as s:
            await facts.add_fact(s, uid, "x" * 201)


async def test_prompt_facts_newest_active_first(db):
    uid = await _user(db)
    async with db() as s:
        for text, active in (("первый", True), ("выключен", False), ("второй", True)):
            await facts.add_fact(s, uid, text)
            if not active:
                (await s.scalar(select(UserFact).where(UserFact.text == text))).active = False
        await s.commit()
        assert await facts.prompt_facts(s, TG) == ["второй", "первый"]
        assert await facts.prompt_facts(s, 999) == []


# ---- /facts ----


def _message():
    return SimpleNamespace(from_user=SimpleNamespace(id=TG, full_name="Amir"), answer=AsyncMock())


def _cb(data: str, user_id: int = TG):
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=user_id, full_name="Amir"),
        message=SimpleNamespace(edit_text=AsyncMock()),
        answer=AsyncMock(),
    )


async def test_facts_command_empty(db):
    msg = _message()
    await facts_handler.list_facts(msg, db)
    assert "запомни: …" in msg.answer.await_args.args[0]
    assert msg.answer.await_args.kwargs.get("reply_markup") is None


async def test_facts_command_lists_and_deletes(db):
    uid = await _user(db)
    async with db() as s:
        a = (await facts.add_fact(s, uid, "не ем творог")).fact
        b = (await facts.add_fact(s, uid, "тренируюсь по утрам")).fact
        off = (await facts.add_fact(s, uid, "выключенный")).fact
        off.active = False
        await s.commit()
        ids = (a.id, b.id)
    msg = _message()
    await facts_handler.list_facts(msg, db)
    text = msg.answer.await_args.args[0]
    assert "1. тренируюсь по утрам" in text and "2. не ем творог" in text and "выключенный" not in text
    kb = msg.answer.await_args.kwargs["reply_markup"]
    assert [b.callback_data for row in kb.inline_keyboard for b in row] == [f"fact_del:{ids[1]}", f"fact_del:{ids[0]}"]

    other = _cb(f"fact_del:{ids[0]}", user_id=777)
    await facts_handler.delete_fact(other, db)
    async with db() as s:
        assert await s.get(UserFact, ids[0]) is not None

    cb = _cb(f"fact_del:{ids[0]}")
    await facts_handler.delete_fact(cb, db)
    async with db() as s:
        assert await s.get(UserFact, ids[0]) is None
    edited = cb.message.edit_text.await_args
    assert "не ем творог" not in edited.args[0] and "тренируюсь по утрам" in edited.args[0]

    last = _cb(f"fact_del:{ids[1]}")
    await facts_handler.delete_fact(last, db)
    assert "запомни: …" in last.message.edit_text.await_args.args[0]


async def test_facts_command_fits_telegram(db):
    uid = await _user(db)
    async with db() as s:
        for i in range(facts.MAX_ACTIVE):
            await facts.add_fact(s, uid, f"{i} " + "очень длинный факт " * 10)
        await s.commit()
    msg = _message()
    await facts_handler.list_facts(msg, db)
    assert len(msg.answer.await_args.args[0]) <= 4096
    kb = msg.answer.await_args.kwargs["reply_markup"]
    assert all(len(b.text) <= 40 for row in kb.inline_keyboard for b in row)


def test_help_mentions_facts():
    from gymbot.handlers.common import HELP

    assert "/facts" in HELP and "запомни" in HELP


@pytest.mark.parametrize(
    "offer",
    [
        "1ПМ жим лёжа 100 кг",
        "1 ПМ в приседе 120 кг",
        "ПМ жим 90",
        "1RM жим 100 кг",
        "rm приседа 120",
        "e1RM 105 кг",
        "максимум по Эпли 110 кг",
        "жим ≈ 100 кг",
        "максимум по расчёту 100 кг",
        "расчётный вес 80 кг",
        "оценочный максимум 90 кг",
        "примерный максимум 95 кг",
    ],
)
def test_derived_offer_markers(offer):
    # A model marker about a lift is derived unless the user said the number himself.
    assert facts.derived_offer(offer, "какая завтра тренировка и какие веса") is True
    assert facts.derived_offer(offer, "что угодно 100 120 90 80 105 110 95") is False


@pytest.mark.parametrize(
    ("offer", "said"),
    [
        ("мой 1ПМ в жиме 100", "мой 1ПМ в жиме 100 кг"),
        ("мой 1ПМ в жиме 100", "максимум в жиме сотка, ровно 100"),
        ("пью ≈ 2 л воды", "пью литра два воды"),
        ("колено ~90°", "колено сгибается градусов на 90"),
        ("жим лёжа ~80 кг", "жму примерно восемьдесят"),
        ("присед ~125 кг", "присед сто двадцать пять"),
        ("гантели ~7.5 кг", "отведения с гантелями семь с половиной"),
    ],
)
def test_derived_offer_keeps_what_the_user_said(offer, said):
    assert facts.derived_offer(offer, said) is False


@pytest.mark.parametrize(
    ("offer", "said", "derived"),
    [
        ("самса ~150 г", "съел самсу грамм 150", False),
        ("жим на штанге ~50 кг", "какие веса завтра", True),
        ("творог 5 %", "ем творог 5%", False),
        ("молоко 2,5 %", "пью молоко 2.5", False),
        ("молоко 2.5 %", "пью молоко 2,5", False),
        ("молоко 3,2 %", "пью молоко 2,5", False),  # food: not a lift number
        ("Запомнить: на штанге ~50 кг", "жим 50 кг на штанге", False),
    ],
)
def test_derived_offer_with_tilde_or_percent(offer, said, derived):
    assert facts.derived_offer(offer, said) is derived


@pytest.mark.parametrize(
    ("offer", "said"),
    [
        ("не ем свинину", "не ем свинину"),
        ("тренируюсь по вечерам", "тренируюсь по вечерам"),
        ("манты ~90 г/шт", "манты штук 5 по 90 грамм"),
        ("пью кофе без сахара", ""),
    ],
)
def test_derived_offer_ordinary_facts(offer, said):
    assert facts.derived_offer(offer, said) is False


def test_derived_offer_food_portion_estimate_is_still_offered():
    # The parser's own job: "они у нас большие" -> a portion; the user confirms it with the button.
    assert not facts.derived_offer("самса ~150 г", "съел 2 самсы, они у нас большие")
    assert facts.derived_offer("сгибания на штанге ~50 кг", "какая завтра тренировка и какие веса")
    assert not facts.derived_offer("жим лёжа ~80 кг", "жму примерно 80")
