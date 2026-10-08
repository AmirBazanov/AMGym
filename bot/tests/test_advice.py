"""AI advice: the summary for the model, generation and the /advice handler."""

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select

from gymbot.db.models import (
    FoodEntry,
    Program,
    User,
    UserFact,
    UserProgram,
    WellbeingEntry,
    Workout,
    WorkoutSet,
)
from gymbot.handlers import advice as advice_handler
from gymbot.llm.openrouter import LLMError
from gymbot.llm.prompts import ADVICE_DISCLAIMER, ADVICE_SYSTEM_PROMPT
from gymbot.services import advice
from gymbot.services.programs import get_or_create_exercise, monday_of

MSK = ZoneInfo("Europe/Moscow")
TODAY = date(2026, 10, 7)  # Wednesday
NOW = datetime(2026, 10, 7, 12, 0, tzinfo=MSK).astimezone(UTC)
TG_ID = 987654321
NAME = "Амир Секретный"


def msk(d: date, h: int = 13) -> datetime:
    return datetime(d.year, d.month, d.day, h, 0, tzinfo=MSK).astimezone(UTC)


async def _user(s, **fields) -> User:
    user = User(telegram_id=TG_ID, name=NAME, rest_seconds=90, **fields)
    s.add(user)
    await s.flush()
    return user


def _food(user: User, at: datetime, kcal: int, protein: int) -> FoodEntry:
    return FoodEntry(user_id=user.id, eaten_at=at, description="еда", grams=Decimal(100), kcal=Decimal(kcal),
                     protein_g=Decimal(protein), fat_g=Decimal(60), carbs_g=Decimal(200), estimated=True)


async def _workout(s, user: User, day: date, sets: list[tuple[str, float | None, int, int]]) -> None:
    w = Workout(user_id=user.id, performed_on=day, started_at=msk(day, 18), source="chat")
    for i, (name, weight, reps, drop) in enumerate(sets):
        ex = await get_or_create_exercise(s, name)
        w.sets.append(WorkoutSet(exercise_id=ex.id, set_index=i, reps=reps, drop_index=drop,
                                 weight_kg=Decimal(str(weight)) if weight is not None else None))
    s.add(w)
    await s.flush()


@pytest.fixture
async def full_user(db):
    """Profile, norm, 5 days of food + today, 3 workouts in 14 days (+1 older), an active program."""
    async with db() as s:
        user = await _user(s, weight_kg=Decimal("82.5"), height_cm=180, birth_year=1998, goal="mass",
                           about="болит левое плечо", kcal_target=2500, protein_target_g=160,
                           fat_target_g=80, carbs_target_g=300)
        for d in range(2, 7):  # Oct 2..6: 5 completed days, 2000 kcal / 120 g protein each
            s.add(_food(user, msk(date(2026, 10, d), 9), 1200, 70))
            s.add(_food(user, msk(date(2026, 10, d), 19), 800, 50))
        s.add(_food(user, msk(date(2026, 9, 20)), 9999, 999))  # outside the window
        s.add(_food(user, msk(TODAY, 9), 800, 50))  # today so far
        await _workout(s, user, date(2026, 9, 1), [("жим лёжа", 100, 1, 0)])  # older than 14 days
        await _workout(s, user, date(2026, 10, 1), [("жим лёжа", 70, 5, 0)])
        await _workout(s, user, date(2026, 10, 5), [
            ("жим лёжа", 60, 10, 0), ("жим лёжа", 60, 10, 0), ("жим лёжа", 65, 8, 0), ("жим лёжа", 40, 10, 1),
        ])
        await _workout(s, user, date(2026, 10, 6), [("приседания", None, 15, 0), ("приседания", None, 15, 0)])
        program = await s.scalar(select(Program).order_by(Program.id).limit(1))
        s.add(UserProgram(user_id=user.id, program_id=program.id, started_on=monday_of(TODAY)))
        await s.commit()
        return user.id


async def _context(db, user_id: int) -> str:
    async with db() as s:
        user = await s.get(User, user_id)
        return await advice.build_context(s, user, None, MSK, NOW)


async def test_context_has_profile_and_norm(db, full_user):
    ctx = await _context(db, full_user)
    assert "28 лет" in ctx and "вес 82.5 кг" in ctx and "рост 180 см" in ctx and "ИМТ 25.5" in ctx
    assert "Цель: набор массы" in ctx
    assert "О себе: болит левое плечо" in ctx
    assert "Норма в день: 2500 ккал, Б 160 г, Ж 80 г, У 300 г." in ctx


async def test_context_nutrition_averages_completed_days(db, full_user):
    ctx = await _context(db, full_user)
    assert "записи в 5 из 7 дней" in ctx
    assert "2000 ккал, Б 120 г" in ctx  # today's 800 kcal and the September entry are not averaged
    assert "недобор 500 ккал" in ctx and "недобор белка 40 г" in ctx
    assert "Сегодня пока: 800 ккал, Б 50 г; до нормы осталось 1700 ккал, 110 г белка." in ctx


async def test_context_training(db, full_user):
    ctx = await _context(db, full_user)
    # 3 workouts in 14 days; volume 600+600+520+400 (drop) + 350 = 2470 kg; the September one is out.
    assert "Тренировки за 14 дней: 3, последняя 06.10, объём 2470 кг" in ctx
    assert "- жим лёжа: прошлый раз 05.10: 3 подх., лучший 65×8; 1ПМ по Эпли 82.3 кг" in ctx
    assert "- приседания: прошлый раз 06.10: 2 подх. по 15/15 без веса" in ctx
    # most recent exercise first
    assert ctx.index("- приседания") < ctx.index("- жим лёжа")


async def test_context_program(db, full_user):
    ctx = await _context(db, full_user)
    assert "Программа «" in ctx and "неделя 1 из" in ctx


async def test_context_has_no_identifiers_and_fits(db, full_user):
    ctx = await _context(db, full_user)
    assert str(TG_ID) not in ctx and NAME not in ctx and "Амир" not in ctx
    assert len(ctx) <= advice.CONTEXT_MAX


async def test_context_without_data_asks_to_log(db):
    async with db() as s:
        user = await _user(s)
        await s.commit()
        ctx = await advice.build_context(s, user, None, MSK, NOW)
        # build_context only reads: no program is created for the user
        assert await s.scalar(select(func.count()).select_from(UserProgram)) == 0
    assert "Профиль не заполнен" in ctx
    assert "Норма КБЖУ не задана." in ctx
    assert "Питание за 7 дней: записей нет, запиши" in ctx
    assert "Тренировок за 14 дней нет: запиши" in ctx
    assert "Программа" not in ctx and "Сегодня пока" not in ctx


async def test_context_few_food_days_asks_for_more(db):
    async with db() as s:
        user = await _user(s, kcal_target=2500)
        s.add(_food(user, msk(date(2026, 10, 6)), 2600, 100))
        await s.commit()
        ctx = await advice.build_context(s, user, None, MSK, NOW)
    assert "записи в 1 из 7 дней" in ctx and "перебор 100 ккал" in ctx
    assert "Дней с записями мало: запиши" in ctx


async def test_context_capped_with_many_exercises(db):
    async with db() as s:
        user = await _user(s, about="очень длинный текст о себе " * 20)
        sets = [(f"очень длинное название упражнения номер {i}", 50 + i, 10, 0) for i in range(30)]
        await _workout(s, user, date(2026, 10, 6), sets)
        await s.commit()
        ctx = await advice.build_context(s, user, None, MSK, NOW)
    assert len(ctx) <= advice.CONTEXT_MAX
    assert "и ещё упражнений:" in ctx


async def test_context_food_at_norm(db):
    async with db() as s:
        user = await _user(s, kcal_target=2000, protein_target_g=120)
        s.add(_food(user, msk(date(2026, 10, 6)), 2000, 120))
        await s.commit()
        ctx = await advice.build_context(s, user, None, MSK, NOW)
    assert "к норме: ккал в норме, белок в норме." in ctx


def _wb(user: User, day: date, h: int = 9, sleep: str | None = None, energy: int | None = None,
        pains: list[tuple[str, int | None]] | None = None, note: str | None = None) -> WellbeingEntry:
    return WellbeingEntry(
        user_id=user.id, noted_at=msk(day, h), sleep_hours=Decimal(sleep) if sleep else None, energy=energy,
        pains=json.dumps([{"place": p, "severity": sv} for p, sv in pains], ensure_ascii=False) if pains else None,
        note=note, raw_text="текст",
    )


async def test_context_wellbeing_block(db):
    async with db() as s:
        user = await _user(s)
        s.add_all([
            _wb(user, date(2026, 9, 20), sleep="3", energy=1, pains=[("спина", 5)], note="старое"),  # > 14 days
            _wb(user, date(2026, 9, 25), sleep="8", energy=4),
            _wb(user, date(2026, 10, 1), sleep="6", energy=2, pains=[("Колено", 2)]),
            _wb(user, date(2026, 10, 3), pains=[("левое плечо", None), ("колено", 3)], note="после жима"),
            _wb(user, date(2026, 10, 5), sleep="6.5", pains=[("левое плечо", 3), ("Колено", None)]),
            _wb(user, date(2026, 10, 6), sleep="7", energy=3, pains=[("левое плечо", 4)]),
            _wb(user, TODAY, 8, note="сил мало, ноги ватные"),
        ])
        await s.commit()
        ctx = await advice.build_context(s, user, None, MSK, NOW)
    assert ("Самочувствие за 14 дней: записей 6; сон в среднем 6.9 ч (записей о сне 4), "
            "ночей меньше 7 ч: 2; энергия в среднем 3/5.") in ctx
    # The 5 most recent pains (the 01.10 one is 6th) grouped by place, case-insensitively.
    assert "Боли: левое плечо 06.10 (4/5), 05.10 (3/5), 03.10; Колено 05.10, 03.10 (3/5)." in ctx
    assert "Последняя заметка 07.10: сил мало, ноги ватные" in ctx
    assert "спина" not in ctx and "старое" not in ctx and "01.10" not in ctx


async def test_context_without_wellbeing(db):
    async with db() as s:
        user = await _user(s)
        await s.commit()
        ctx = await advice.build_context(s, user, None, MSK, NOW)
    assert "Самочувствие за 14 дней: записей нет" in ctx and "Боли" not in ctx


async def test_context_full_user_with_wellbeing_keeps_program_and_fits(db, full_user):
    async with db() as s:
        user = await s.get(User, full_user)
        for d in range(1, 8):
            s.add(_wb(user, date(2026, 10, d), sleep="6", energy=2, pains=[(f"место {d}", 3)],
                      note="очень длинная заметка о самочувствии " * 10))
        await s.commit()
    ctx = await _context(db, full_user)
    assert len(ctx) <= advice.CONTEXT_MAX
    assert "Самочувствие за 14 дней: записей 7" in ctx and "Программа «" in ctx


async def test_context_has_active_facts_after_profile(db, full_user):
    async with db() as s:
        s.add_all([
            UserFact(user_id=full_user, text="не ест творог", category="food", active=True),
            UserFact(user_id=full_user, text="старое", category="other", active=False),
            UserFact(user_id=full_user, text="тренируется по утрам", category="training", active=True),
        ])
        await s.commit()
    ctx = await _context(db, full_user)
    line = next(ln for ln in ctx.splitlines() if ln.startswith("Факты о пользователе:"))
    assert "не ест творог" in line and "тренируется по утрам" in line and "старое" not in line
    assert ctx.index("Профиль:") < ctx.index("Факты о пользователе") < ctx.index("Норма в день")


async def test_context_with_50_long_facts_fits_and_keeps_program(db, full_user):
    async with db() as s:
        for i in range(50):
            s.add(UserFact(user_id=full_user, text=f"{i} " + "x" * 197, category="other", active=True))
        await s.commit()
    ctx = await _context(db, full_user)
    assert len(ctx) <= advice.CONTEXT_MAX and "Программа «" in ctx and "Факты о пользователе" in ctx


def test_advice_prompt_wellbeing_rules():
    p = ADVICE_SYSTEM_PROMPT.lower()
    assert "факт" in p and "самочувстви" in p and "боли" in p and "замен" in p and "недосып" in p and "диагноз" in p


def test_epley():
    assert advice.epley(100, 1) == 100
    assert advice.epley(60, 10) == pytest.approx(80)


class FakeLLM:
    def __init__(self, answer: str = "Питание\n- добери 40 г белка", error: Exception | None = None):
        self.answer, self.error, self.calls = answer, error, []

    async def complete_text(self, messages, **_kw):
        self.calls.append(messages)
        if self.error:
            raise self.error
        return self.answer


async def test_generate_sends_prompt_and_context(db, full_user):
    llm = FakeLLM()
    async with db() as s:
        user = await s.get(User, full_user)
        ctx = await advice.build_context(s, user, None, MSK, NOW)
        text = await advice.generate(s, user, None, llm, MSK, NOW)
    [messages] = llm.calls
    assert messages[0] == {"role": "system", "content": ADVICE_SYSTEM_PROMPT}
    assert messages[1]["role"] == "user" and messages[1]["content"].endswith(ctx)
    assert text == f"Питание\n- добери 40 г белка\n\n{ADVICE_DISCLAIMER}"


async def test_generate_keeps_models_disclaimer_once(db, full_user):
    llm = FakeLLM(f"Питание\n- x\n{ADVICE_DISCLAIMER}.")
    async with db() as s:
        text = await advice.generate(s, await s.get(User, full_user), None, llm, MSK, NOW)
    assert text.count(ADVICE_DISCLAIMER) == 1


async def test_generate_caps_telegram_length(db, full_user):
    async with db() as s:
        text = await advice.generate(s, await s.get(User, full_user), None, FakeLLM("а" * 5000), MSK, NOW)
    assert len(text) <= advice.TELEGRAM_MAX and text.endswith(ADVICE_DISCLAIMER)


async def test_generate_propagates_llm_error(db, full_user):
    async with db() as s:
        with pytest.raises(LLMError):
            await advice.generate(s, await s.get(User, full_user), None, FakeLLM(error=LLMError("x")), MSK, NOW)


# ---- handler ----


@pytest.mark.parametrize("text", ["совет", "Советы на неделю", "  что посоветуешь?", "Рекомендации по сну",
                                  "рекомендация", "посоветуй что-нибудь", "советуй", "советов дай", "СОВЕТ!"])
def test_trigger_matches(text):
    assert advice_handler.ADVICE_TEXT.match(text)


@pytest.mark.parametrize("text", ["съел совет", "жим 3х10 на 60", "что съесть", "советская колбаса 100 г",
                                  "Советский пломбир", "советник", "рекомендованная доза"])
def test_trigger_ignores(text):
    assert not advice_handler.ADVICE_TEXT.match(text)


def _message():
    return SimpleNamespace(
        from_user=SimpleNamespace(id=TG_ID, full_name="Amir"),
        chat=SimpleNamespace(id=TG_ID),
        bot=SimpleNamespace(id=1, send_chat_action=AsyncMock()),  # ChatActionSender logs bot.id
        answer=AsyncMock(),
    )


async def test_handler_answers_with_advice(db, settings, monkeypatch):
    seen = {}

    async def fake_generate(session, user, settings_, llm, tz, now_utc):
        seen.update(user=user.telegram_id, tz=tz, llm=llm)
        return "СОВЕТ"

    monkeypatch.setattr(advice, "generate", fake_generate)
    msg = _message()
    llm = object()
    await advice_handler.give_advice(msg, settings, db, llm)
    msg.answer.assert_awaited_once_with("СОВЕТ")
    msg.bot.send_chat_action.assert_awaited()  # "typing" while the model works
    assert seen == {"user": TG_ID, "tz": ZoneInfo(settings.timezone), "llm": llm}


async def test_handler_llm_error(db, settings, monkeypatch):
    async def failing(*args):
        raise LLMError("down")

    monkeypatch.setattr(advice, "generate", failing)
    msg = _message()
    await advice_handler.give_advice(msg, settings, db, object())
    msg.answer.assert_awaited_once_with("Нейросеть сейчас недоступна, попробуй ещё раз чуть позже.")
