"""Saved products inside the text flow (log_text.process_text): answered without the LLM, or passed to the parser."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from gymbot.db.models import UserFact
from gymbot.handlers import log_text
from gymbot.handlers import products as hp
from gymbot.llm.openrouter import OpenRouterClient
from gymbot.services import products as pr
from gymbot.services.products import ProductInfo
from gymbot.services.users import get_or_create_user

T0 = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
USER = 42
OTHER = 43

BAR = ProductInfo(
    "Протеиновый батончик", "Bombbar", 360.0, 33.0, 12.0, 30.0, net_weight_g=60.0, serving_g=30.0, source="label"
)
MARS = ProductInfo("Mars", "Mars", 450.0, 4.0, 16.8, 70.0, net_weight_g=51.0, barcode="5000159407236", source="off")
PROTEIN = ProductInfo("Протеин сывороточный", "Optimum", 380.0, 75.0, 6.0, 8.0, net_weight_g=900.0, serving_g=30.0)
SYSTEM_MARK = "Мои продукты"

COTTAGE_AND_PROTEIN = {
    "kind": "food",
    "foods": [
        {"description": "творог, 200 г", "grams": 200, "kcal": 180, "protein_g": 32, "fat_g": 1, "carbs_g": 6},
        {"description": "протеин, 1 скуп", "grams": 30, "kcal": 114, "protein_g": 22.5, "fat_g": 1.8, "carbs_g": 2.4},
    ],
}


class Llm:
    """OpenRouterClient over MockTransport: replays queued answers, records request bodies."""

    def __init__(self, settings, answers=()):
        self.answers = list(answers)
        self.bodies: list[dict] = []
        http = httpx.AsyncClient(transport=httpx.MockTransport(self._handle))
        self.client = OpenRouterClient(settings.model_copy(update={"openrouter_api_key": "k"}), http)

    def _handle(self, req: httpx.Request) -> httpx.Response:
        self.bodies.append(json.loads(req.content))
        content = json.dumps(self.answers.pop(0), ensure_ascii=False)
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    def system(self, n: int = -1) -> str:
        return self.bodies[n]["messages"][0]["content"]


def message(text: str, at: datetime = T0, user_id: int = USER):
    return SimpleNamespace(
        text=text,
        date=at,
        from_user=SimpleNamespace(id=user_id, full_name="Amir"),
        chat=SimpleNamespace(id=user_id),
        bot=SimpleNamespace(send_chat_action=AsyncMock()),
        answer=AsyncMock(),
    )


def answer_text(msg) -> str:
    return msg.answer.await_args.args[0]


def answer_buttons(msg) -> list[tuple[str, str]]:
    kb = msg.answer.await_args.kwargs["reply_markup"]
    return [(b.text, b.callback_data) for row in kb.inline_keyboard for b in row]


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    async def parser_answer(message, text, result, *args):
        return result, None

    monkeypatch.setattr(log_text, "_diary_answer", parser_answer)
    stores = (log_text.PENDING, log_text.CONTEXT, log_text.FACTS, hp.CARDS, hp.PREVIEWS, hp.CHOICES, hp.LATEST)
    for store in stores:
        store.clear()
    yield
    for store in stores:
        store.clear()


@pytest.fixture
def llm(settings):
    return Llm(settings)


async def save_products(db, *products: ProductInfo, user_id: int = USER) -> None:
    async with db() as session:
        user = await get_or_create_user(session, user_id, "Amir")
        for p in products:
            await pr.remember(session, user.id, p)
        await session.commit()


async def send(text, llm, settings, db, at=T0, **kw):
    msg = message(text, at)
    await log_text.process_text(msg, text, settings, db, llm.client, **kw)
    return msg


# ---- answered from memory, no LLM ----


async def test_same_bar_is_answered_from_memory_without_the_llm(llm, settings, db):
    await save_products(db, BAR)

    msg = await send("тот же батончик", llm, settings, db)

    assert llm.bodies == []  # no LLM request
    text = answer_text(msg)
    assert text.startswith("Записать еду? Всего 216 ккал") and "Bombbar Протеиновый батончик, 60 г" in text
    assert [d.split(":")[0] for _, d in answer_buttons(msg)] == ["psave", "pdrop"]
    assert log_text.PENDING == {} and log_text.CONTEXT == {}  # the parser's dialog is untouched
    (preview,) = hp.PREVIEWS.values()
    assert preview.raw_text == "тот же батончик"
    msg.bot.send_chat_action.assert_not_awaited()


async def test_product_name_without_an_amount_asks_how_much(llm, settings, db):
    await save_products(db, BAR, MARS)

    msg = await send("mars", llm, settings, db)

    assert llm.bodies == []
    assert answer_text(msg).startswith("Mars (Open Food Facts)") and "Сколько съел?" in answer_text(msg)
    assert any(t == "Вся упаковка (51 г)" for t, _ in answer_buttons(msg))


async def test_product_with_grams_is_answered_from_memory(llm, settings, db):
    await save_products(db, MARS)
    msg = await send("mars 50 г", llm, settings, db)
    assert llm.bodies == []
    assert "Mars, 50 г" in answer_text(msg)


async def test_voice_prefix_and_raw_text_are_kept(llm, settings, db):
    await save_products(db, MARS)
    msg = await send("mars 50 г", llm, settings, db, raw_text="[voice] mars 50 г", prefix="Распознал: «mars 50 г»\n\n")
    assert answer_text(msg).startswith("Распознал: «mars 50 г»\n\nЗаписать еду?")
    (preview,) = hp.PREVIEWS.values()
    assert preview.raw_text == "[voice] mars 50 г"


async def test_amount_after_the_card_is_not_sent_to_the_llm(llm, settings, db):
    await save_products(db, MARS)
    await send("mars", llm, settings, db)

    msg = await send("60 г", llm, settings, db, T0 + timedelta(minutes=1))

    assert llm.bodies == []
    assert "Mars, 60 г" in answer_text(msg)


async def test_amount_goes_to_the_parser_when_its_question_is_newer_than_the_card(llm, settings, db):
    """A card from 12:00, then the parser asks «Сколько грамм творога?» at 12:01: "200" answers the parser."""
    await save_products(db, MARS)
    await send("mars", llm, settings, db)
    llm.answers = [{"kind": "food", "clarification": "Сколько грамм творога?"}]
    asked = await send("творог", llm, settings, db, T0 + timedelta(minutes=1))
    assert len(llm.bodies) == 1 and "Сколько грамм творога?" in answer_text(asked)

    llm.answers = [{"kind": "food", "foods": [COTTAGE_AND_PROTEIN["foods"][0]]}]
    msg = await send("200", llm, settings, db, T0 + timedelta(minutes=2))

    assert len(llm.bodies) == 2  # the parser took it
    assert "творог" in answer_text(msg) and hp.PREVIEWS == {}


async def test_a_message_that_is_not_about_a_saved_product_goes_to_the_parser(llm, settings, db):
    await save_products(db, BAR, MARS)
    llm.answers = [{"kind": "food", "foods": [COTTAGE_AND_PROTEIN["foods"][0]]}]

    msg = await send("творог 200 г", llm, settings, db)

    assert len(llm.bodies) == 1
    assert "творог" in answer_text(msg) and hp.CARDS == {}


async def test_a_product_next_to_other_food_goes_to_the_parser(llm, settings, db):
    await save_products(db, MARS)
    llm.answers = [COTTAGE_AND_PROTEIN]
    await send("mars и кофе", llm, settings, db)
    assert len(llm.bodies) == 1 and hp.CARDS == {}


async def test_workout_text_is_not_taken_for_a_product(llm, settings, db):
    await save_products(db, BAR, MARS)
    llm.answers = [{"kind": "unknown"}]
    await send("жим 80 на 8", llm, settings, db)
    assert len(llm.bodies) == 1 and hp.CARDS == {}


async def test_other_users_products_are_not_used(llm, settings, db):
    await save_products(db, MARS, user_id=OTHER)
    llm.answers = [{"kind": "unknown"}]
    await send("mars 50 г", llm, settings, db)
    assert len(llm.bodies) == 1 and hp.CARDS == {} and SYSTEM_MARK not in llm.system()


# ---- the parser gets the products the message names ----


async def test_mentioned_product_inside_a_longer_message_is_in_the_parser_prompt(llm, settings, db):
    await save_products(db, BAR, PROTEIN)
    llm.answers = [COTTAGE_AND_PROTEIN]

    msg = await send("творог 200 г и протеин 1 скуп", llm, settings, db)

    assert len(llm.bodies) == 1
    system = llm.system()
    assert SYSTEM_MARK in system
    assert "Optimum Протеин сывороточный на 100 г 380 ккал Б75 Ж6 У8" in system and "порция 30 г" in system
    assert "Bombbar" not in system  # only the products the text names
    assert "Записать еду?" in answer_text(msg)  # the usual preview of the parser's answer
    assert hp.CARDS == {}


async def test_without_saved_products_the_prompt_has_no_products_line(llm, settings, db):
    llm.answers = [COTTAGE_AND_PROTEIN]
    await send("творог 200 г и протеин 1 скуп", llm, settings, db)
    assert SYSTEM_MARK not in llm.system()


async def test_saved_products_the_text_does_not_name_stay_out_of_the_prompt(llm, settings, db):
    await save_products(db, BAR, MARS, PROTEIN)
    llm.answers = [{"kind": "food", "foods": [COTTAGE_AND_PROTEIN["foods"][0]]}]
    await send("творог 200 г", llm, settings, db)
    assert SYSTEM_MARK not in llm.system()


async def test_products_line_goes_with_the_users_facts(llm, settings, db):
    await save_products(db, PROTEIN)
    async with db() as session:
        user = await get_or_create_user(session, USER, "Amir")
        session.add(UserFact(user_id=user.id, text="порция творога 200 г", created_at=T0))
        await session.commit()
    llm.answers = [COTTAGE_AND_PROTEIN]

    await send("творог и протеин 1 скуп", llm, settings, db)

    system = llm.system()
    assert SYSTEM_MARK in system and "порция творога 200 г" in system
    assert system.index(SYSTEM_MARK) < system.index("порция творога 200 г")
    assert system.count("Факты о пользователе") == 1  # one line, not two


async def test_same_answers_the_parsers_question_not_a_saved_product(settings, db):
    """The model asked «С каким весом?» about a workout; «тот же» answers it, no product card."""
    async with db() as session:
        user = await get_or_create_user(session, USER, "Amir")
        await pr.remember(session, user.id, MARS)
        await session.commit()
    question = {"kind": "unknown", "clarification": "С каким весом?"}
    workout = {"kind": "workout", "exercises": [{"exercise": "жим лёжа", "sets": [{"reps": 8, "weight_kg": 80}]}]}
    llm = Llm(settings, [question, workout])
    first = message("жим 8 раз")
    await log_text.process_text(first, "жим 8 раз", settings, db, llm.client)
    second = message("тот же", T0 + timedelta(minutes=1))
    await log_text.process_text(second, "тот же", settings, db, llm.client)
    assert len(llm.bodies) == 2  # the parser got «тот же»
    assert hp.CARDS == {} and hp.CHOICES == {}
    assert answer_text(second).startswith("Записать?")


def test_vision_prompt_copies_only_a_visible_table():
    from gymbot.llm.prompts import VISION_SYSTEM

    assert "таблица пищевой ценности" in VISION_SYSTEM
    assert "Таблицы не видно" in VISION_SYSTEM and "label не возвращай" in VISION_SYSTEM
