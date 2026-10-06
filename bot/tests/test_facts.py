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
