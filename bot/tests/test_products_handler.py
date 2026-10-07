"""Product cards in the chat (handlers/products.py): amount buttons, text amounts, preview, save, /products."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot
from aiogram.types import CallbackQuery, Chat, Message, User
from sqlalchemy import select

from gymbot.db.models import FoodEntry, Product
from gymbot.db.models import User as UserRow
from gymbot.handlers import products as hp
from gymbot.services import products as pr
from gymbot.services.products import Amount, ProductInfo
from gymbot.services.users import get_or_create_user

T0 = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
USER = 42
OTHER = 43

MARS = ProductInfo(
    "Mars", "Mars", 450.0, 4.0, 16.8, 70.0, net_weight_g=51.0, barcode="5000159407236", source="off"
)
BAR = ProductInfo(
    "Протеиновый батончик", "Bombbar", 360.0, 33.0, 12.0, 30.0, net_weight_g=60.0, serving_g=30.0, source="label"
)
PROT_A = ProductInfo("Протеин сывороточный", "Optimum", 380.0, 75.0, 6.0, 8.0, net_weight_g=900.0, serving_g=30.0)
PROT_B = ProductInfo("Протеин изолят", "Myprotein", 370.0, 85.0, 2.0, 5.0, net_weight_g=1000.0, serving_g=25.0)


def message(text: str = "", at: datetime = T0, user_id: int = USER):
    return SimpleNamespace(
        text=text,
        date=at,
        from_user=SimpleNamespace(id=user_id, full_name="Amir"),
        chat=SimpleNamespace(id=user_id),
        answer=AsyncMock(),
    )


def callback(data: str, user_id: int = USER):
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=user_id, full_name="Amir"),
        message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock()),
        answer=AsyncMock(),
    )


def buttons(markup) -> list[tuple[str, str]]:
    return [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row]


def sent(msg) -> tuple[str, list[tuple[str, str]]]:
    """(text, buttons) of the last answer."""
    args, kwargs = msg.answer.await_args
    markup = kwargs.get("reply_markup")
    return args[0], buttons(markup) if markup else []


def edited(cb) -> tuple[str, list[tuple[str, str]]]:
    args, kwargs = cb.message.edit_text.await_args
    markup = kwargs.get("reply_markup")
    return args[0], buttons(markup) if markup else []


def tokens_of(items: list[tuple[str, str]], prefix: str) -> list[str]:
    return [data.split(":")[1] for _, data in items if data.startswith(prefix + ":")]


def action(items: list[tuple[str, str]], what: str) -> str:
    (data,) = [d for _, d in items if d.endswith(f":{what}")]
    return data


@pytest.fixture(autouse=True)
def clean_state():
    stores = (hp.CARDS, hp.PREVIEWS, hp.CHOICES, hp.LATEST)
    for store in stores:
        store.clear()
    yield
    for store in stores:
        store.clear()


@pytest.fixture
def published(monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(hp.live, "publish", lambda *a: calls.append(a))
    return calls


# ---- offer ----


async def test_offer_without_amount_asks_how_much():
    msg = message()
    await hp.offer(msg, MARS, raw_text="[photo]")
    text, kb = sent(msg)
    assert text.startswith("Mars (Open Food Facts)")
    assert "На 100 г: 450 ккал, Б4 Ж16.8 У70" in text
    assert "Упаковка 51 г" in text
    assert "Сколько съел?" in text
    assert [t for t, _ in kb] == ["Вся упаковка (51 г)", "½ (25.5 г)", "Ввести граммы", "✖ Отмена"]
    assert all(d.startswith("pa:") for _, d in kb)
    assert len(hp.CARDS) == 1 and len(hp.PREVIEWS) == 0
    assert hp.LATEST[USER] in hp.CARDS


async def test_offer_serving_button_only_with_a_serving():
    msg = message()
    await hp.offer(msg, BAR, raw_text="[photo]")
    texts = [t for t, _ in sent(msg)[1]]
    assert texts == ["Вся упаковка (60 г)", "½ (30 г)", "Порция (30 г)", "Ввести граммы", "✖ Отмена"]
    assert "Упаковка 60 г, порция 30 г" in sent(msg)[0]

    msg = message()
    await hp.offer(msg, replace(MARS, net_weight_g=None, serving_g=None), raw_text="[photo]")
    assert [t for t, _ in sent(msg)[1]] == ["Ввести граммы", "✖ Отмена"]


async def test_offer_with_a_note_shows_it_on_the_card():
    msg = message()
    await hp.offer(msg, MARS, raw_text="[photo]", note="На этикетке 500 ккал")
    assert "На этикетке 500 ккал" in sent(msg)[0]


async def test_offer_with_a_prefix():
    msg = message()
    await hp.offer(msg, MARS, raw_text="[photo]", prefix="Распознал: «марс»\n\n")
    assert sent(msg)[0].startswith("Распознал: «марс»\n\nMars")


async def test_offer_with_amount_goes_straight_to_the_preview():
    msg = message()
    await hp.offer(msg, MARS, raw_text="[photo] 50 г", amount=Amount(grams=50))
    text, kb = sent(msg)
    assert text.startswith("Записать еду? Всего 225 ккал")
    assert "Mars, 50 г: 225 ккал, Б2 Ж8 У35" in text
    assert [d.split(":")[0] for _, d in kb] == ["psave", "pdrop"]
    (ptoken,) = hp.PREVIEWS
    assert tokens_of(kb, "psave") == [ptoken]
    preview = hp.PREVIEWS[ptoken]
    assert preview.food.kcal == 225 and preview.food.grams == 50 and preview.raw_text == "[photo] 50 г"


async def test_offer_with_a_package_amount_uses_the_net_weight():
    msg = message()
    await hp.offer(msg, MARS, raw_text="x", amount=Amount(packages=1))
    assert "Mars, 51 г" in sent(msg)[0]


async def test_offer_with_an_amount_the_product_cannot_turn_into_grams_asks():
    msg = message()
    await hp.offer(msg, replace(MARS, serving_g=None), raw_text="x", amount=Amount(servings=1))
    assert "Сколько съел?" in sent(msg)[0] and hp.PREVIEWS == {}


# ---- amount buttons ----


async def card_token(product=MARS, **kw) -> tuple[str, object]:
    msg = message()
    await hp.offer(msg, product, raw_text="[photo]", **kw)
    return hp.LATEST[USER], msg


async def test_all_button_edits_the_card_into_a_preview():
    token, _ = await card_token()
    cb = callback(f"pa:{token}:all")
    await hp.choose_amount(cb)
    text, kb = edited(cb)
    assert text.startswith("Записать еду? Всего 230 ккал")  # 450 * 0.51
    assert "Mars, 51 г" in text
    assert [d.split(":")[0] for _, d in kb] == ["psave", "pdrop"]
    cb.answer.assert_awaited_once_with()


async def test_half_and_serving_buttons():
    token, _ = await card_token(BAR)
    half = callback(f"pa:{token}:half")
    await hp.choose_amount(half)
    assert "Bombbar Протеиновый батончик, 30 г" in edited(half)[0]
    srv = callback(f"pa:{token}:srv")
    await hp.choose_amount(srv)
    assert "30 г" in edited(srv)[0]
    # both previews were made for the same card: only the last one lives
    assert len(hp.PREVIEWS) == 1


async def test_serving_button_without_a_serving_is_stale():
    token, _ = await card_token(MARS)
    cb = callback(f"pa:{token}:srv")
    await hp.choose_amount(cb)
    cb.answer.assert_awaited_once_with(hp.STALE, show_alert=True)
    cb.message.edit_text.assert_not_awaited()


async def test_grams_button_asks_for_grams_and_keeps_the_card_open(db):
    token, _ = await card_token()
    cb = callback(f"pa:{token}:g")
    await hp.choose_amount(cb)
    cb.message.answer.assert_awaited_once_with(hp.ASK_GRAMS)
    assert token in hp.CARDS and hp.LATEST[USER] == token
    taken, msg = await say(db, "40 г", datetime.now(UTC) + timedelta(minutes=1))  # the answer the button asked for
    assert taken is True and "Mars, 40 г" in sent(msg)[0]


async def test_cancel_button_closes_the_card():
    token, _ = await card_token()
    cb = callback(f"pa:{token}:x")
    await hp.choose_amount(cb)
    cb.message.edit_text.assert_awaited_once_with("Отменено.")
    assert hp.CARDS == {} and USER not in hp.LATEST


async def test_another_users_button_is_stale():
    token, _ = await card_token()
    cb = callback(f"pa:{token}:all", user_id=OTHER)
    await hp.choose_amount(cb)
    cb.answer.assert_awaited_once_with(hp.STALE, show_alert=True)
    cb.message.edit_text.assert_not_awaited()
    assert token in hp.CARDS


@pytest.mark.parametrize("data", ["pa:nope:all", "pa:", "pa"])
async def test_unknown_card_is_stale(data):
    cb = callback(data)
    await hp.choose_amount(cb)
    cb.answer.assert_awaited_once_with(hp.STALE, show_alert=True)


# ---- on_text ----


async def saved(db, *products: ProductInfo, user_id: int = USER) -> None:
    """Products into the user's memory, the first one eaten first (so the last is the newest)."""
    async with db() as session:
        user = await get_or_create_user(session, user_id, "Amir")
        for p in products:
            await pr.remember(session, user.id, p)
        await session.commit()


async def say(db, text: str, at: datetime = T0 + timedelta(minutes=1), **kw):
    msg = message(text, at)
    taken = await hp.on_text(msg, text, text, db, **kw)
    return taken, msg


async def test_grams_after_a_card_make_a_preview(db):
    await card_token()
    taken, msg = await say(db, "60 г")
    assert taken is True
    text, kb = sent(msg)
    assert text.startswith("Записать еду? Всего 270 ккал") and "Mars, 60 г" in text
    assert [d.split(":")[0] for _, d in kb] == ["psave", "pdrop"]
    (preview,) = hp.PREVIEWS.values()
    assert preview.food.grams == 60 and preview.raw_text == "[photo]\n60 г"


async def test_a_bare_number_after_a_card_is_grams(db):
    await card_token()
    taken, msg = await say(db, "60")
    assert taken is True
    assert "Mars, 60 г" in sent(msg)[0]


@pytest.mark.parametrize(("text", "grams"), [("2 порции", 60), ("1 скуп", 30), ("пол порции", 15), ("половина", 30)])
async def test_other_amount_forms_after_a_card(db, text, grams):
    await card_token(BAR)
    taken, _ = await say(db, text)
    assert taken is True
    (preview,) = hp.PREVIEWS.values()
    assert preview.food.grams == grams


async def test_amount_the_product_cannot_turn_into_grams_asks_for_grams(db):
    await card_token(MARS)  # no serving
    taken, msg = await say(db, "2 порции")
    assert taken is True
    msg.answer.assert_awaited_once_with(hp.ASK_GRAMS)
    assert hp.PREVIEWS == {}


async def test_text_revising_an_open_preview_replaces_the_old_preview(db):
    await card_token()
    await say(db, "60 г")
    (old,) = hp.PREVIEWS
    _, second = await say(db, "80 г", T0 + timedelta(minutes=2))
    assert old not in hp.PREVIEWS
    (new,) = hp.PREVIEWS
    assert new != old
    assert hp.PREVIEWS[new].food.grams == 80
    assert hp.PREVIEWS[new].raw_text == "[photo]\n60 г\n80 г"
    assert tokens_of(sent(second)[1], "psave") == [new]
    # the first preview's button no longer saves anything
    cb = callback(f"psave:{old}")
    await hp.save(cb, db)
    cb.answer.assert_awaited_once_with(hp.STALE, show_alert=True)


async def test_the_card_is_not_taken_when_the_parser_dialog_is_newer(db):
    """"200" may answer the parser's «Сколько грамм творога?»: the open card does not grab it."""
    await card_token()
    taken, msg = await say(db, "200 г", dialog_at=T0 + timedelta(seconds=30))
    assert taken is False
    msg.answer.assert_not_awaited()
    assert hp.PREVIEWS == {}


async def test_the_card_is_taken_when_it_is_newer_than_the_dialog(db):
    await card_token()
    taken, _ = await say(db, "200 г", dialog_at=T0 - timedelta(minutes=5))
    assert taken is True


async def test_unrelated_text_with_an_open_card_is_not_taken(db):
    await card_token()
    for text in ("привет", "жим 80 на 8", "съел плов 300 г"):
        taken, msg = await say(db, text)
        assert taken is False
        msg.answer.assert_not_awaited()
    assert hp.PREVIEWS == {}


async def test_amount_with_foreign_words_is_not_taken(db):
    await card_token()
    taken, _ = await say(db, "творог 200 г")
    assert taken is False


async def test_the_cards_own_name_next_to_the_amount_is_taken(db):
    await card_token()
    taken, msg = await say(db, "mars 40 г")
    assert taken is True and "Mars, 40 г" in sent(msg)[0]


async def test_an_old_card_expires(db):
    await card_token()
    taken, _ = await say(db, "60 г", T0 + hp.TTL + timedelta(minutes=1))
    assert taken is False


async def test_another_users_card_is_not_used(db):
    await card_token()
    msg = message("60 г", T0 + timedelta(minutes=1), user_id=OTHER)
    assert await hp.on_text(msg, "60 г", "60 г", db) is False


async def test_text_without_cards_and_products_is_not_taken(db):
    taken, msg = await say(db, "съел тот же батончик")
    assert taken is False
    msg.answer.assert_not_awaited()


async def test_saved_product_message_makes_a_card(db):
    await saved(db, MARS)
    taken, msg = await say(db, "mars")
    assert taken is True
    text, kb = sent(msg)
    assert text.startswith("Mars (") and "Сколько съел?" in text
    assert [t for t, _ in kb][-2:] == ["Ввести граммы", "✖ Отмена"]
    (card,) = hp.CARDS.values()
    assert card.product.product_id is not None and card.raw_text == "mars"


async def test_saved_product_message_with_an_amount_makes_a_preview(db):
    await saved(db, BAR)
    taken, msg = await say(db, "съел тот же батончик")
    assert taken is True
    text, kb = sent(msg)
    assert text.startswith("Записать еду? Всего 216 ккал") and "Bombbar Протеиновый батончик, 60 г" in text
    assert [d.split(":")[0] for _, d in kb] == ["psave", "pdrop"]
    (preview,) = hp.PREVIEWS.values()
    assert preview.raw_text == "съел тот же батончик"


async def test_saved_product_message_with_grams(db):
    await saved(db, BAR, MARS)
    taken, msg = await say(db, "mars 50 г")
    assert taken is True
    assert "Mars, 50 г" in sent(msg)[0]


async def test_saved_product_message_keeps_the_prefix(db):
    await saved(db, MARS)
    msg = message("mars 50 г", T0 + timedelta(minutes=1))
    assert await hp.on_text(msg, "mars 50 г", "[voice] mars 50 г", db, prefix="Распознал: «mars 50 г»\n\n")
    assert sent(msg)[0].startswith("Распознал: «mars 50 г»\n\n")
    (preview,) = hp.PREVIEWS.values()
    assert preview.raw_text == "[voice] mars 50 г"


async def test_two_candidates_ask_which(db):
    await saved(db, PROT_A, PROT_B)
    taken, msg = await say(db, "протеин 1 скуп")
    assert taken is True
    text, kb = sent(msg)
    assert text == hp.WHICH
    assert [t for t, _ in kb] == ["Myprotein Протеин изолят", "Optimum Протеин сывороточный"]  # newest first
    assert all(d.startswith("pc:") for _, d in kb)
    assert hp.CARDS == {} and len(hp.CHOICES) == 1


async def test_picking_a_candidate_with_a_known_amount_makes_a_preview(db):
    await saved(db, PROT_A, PROT_B)
    _, msg = await say(db, "протеин 1 скуп")
    _, kb = sent(msg)
    cb = callback(kb[1][1])  # Optimum, serving 30 g
    await hp.pick(cb)
    text, buttons_ = edited(cb)
    assert text.startswith("Записать еду? Всего 114 ккал") and "Optimum Протеин сывороточный, 30 г" in text
    assert [d.split(":")[0] for _, d in buttons_] == ["psave", "pdrop"]
    assert hp.CHOICES == {}
    cb.answer.assert_awaited_once_with()


async def test_picking_a_candidate_without_an_amount_shows_the_card(db):
    await saved(db, PROT_A, PROT_B)
    _, msg = await say(db, "протеин")
    _, kb = sent(msg)
    cb = callback(kb[0][1])
    await hp.pick(cb)
    text, buttons_ = edited(cb)
    assert "Сколько съел?" in text and text.startswith("Myprotein Протеин изолят")
    assert any(d.startswith("pa:") for _, d in buttons_)
    # and the amount can then be typed
    # (a button press stamps the card with the real clock, so the follow-up is sent "now")
    taken, typed = await say(db, "25 г", datetime.now(UTC) + timedelta(minutes=1))
    assert taken is True and "Myprotein Протеин изолят, 25 г" in sent(typed)[0]


async def test_same_protein_takes_the_newest_without_asking(db):
    await saved(db, PROT_A, PROT_B)
    taken, msg = await say(db, "тот же протеин")
    assert taken is True
    assert sent(msg)[0].startswith("Myprotein Протеин изолят")
    assert hp.CHOICES == {}


async def test_pick_of_another_user_or_a_bad_index_is_stale(db):
    await saved(db, PROT_A, PROT_B)
    await say(db, "протеин")
    (token,) = hp.CHOICES
    for data, user in ((f"pc:{token}:0", OTHER), (f"pc:{token}:9", USER), (f"pc:{token}:x", USER), ("pc:zzz:0", USER)):
        cb = callback(data, user_id=user)
        await hp.pick(cb)
        cb.answer.assert_awaited_once_with(hp.STALE, show_alert=True)
    assert token in hp.CHOICES  # still pickable by the owner


async def test_choices_are_bounded_to_four_candidates(db):
    many = [replace(PROT_A, name=f"Протеин вкус{i}", brand=f"Б{i}", barcode=f"46070{i}") for i in range(6)]
    await saved(db, *many)
    _, msg = await say(db, "протеин")
    assert len(sent(msg)[1]) == hp.CANDIDATES_MAX


async def test_products_of_another_user_are_not_matched(db):
    await saved(db, MARS, user_id=OTHER)
    taken, _ = await say(db, "mars 50 г")
    assert taken is False


async def test_parser_context_names_only_mentioned_products(db):
    await saved(db, MARS, BAR)
    line = await hp.parser_context(db, USER, "творог 200 г и mars 1 шт")
    assert line.startswith("Мои продукты") and "Mars" in line and "Bombbar" not in line
    assert await hp.parser_context(db, USER, "творог 200 г") == ""
    assert await hp.parser_context(db, OTHER, "mars") == ""


# ---- psave / pdrop ----


async def preview_for(product=MARS, grams=60.0, raw_text="[photo]", at=T0, alias=None) -> tuple[str, str]:
    msg = message("", at)
    await hp.offer(msg, product, raw_text=raw_text, amount=Amount(grams=grams), alias=alias)
    (ptoken,) = hp.PREVIEWS
    return ptoken, hp.LATEST[USER]


async def food_rows(db) -> list[FoodEntry]:
    async with db() as session:
        return list(await session.scalars(select(FoodEntry).order_by(FoodEntry.id)))


async def product_rows(db) -> list[Product]:
    async with db() as session:
        return list(await session.scalars(select(Product).order_by(Product.id)))


async def test_save_writes_an_exact_entry_and_remembers_the_product(db, published):
    sent_at = T0 - timedelta(minutes=7)
    ptoken, _ = await preview_for(MARS, 60, "[photo] 60 г", at=sent_at)
    cb = callback(f"psave:{ptoken}")

    await hp.save(cb, db)

    (entry,) = await food_rows(db)
    assert entry.estimated is False
    assert (entry.kcal, entry.protein_g, entry.fat_g, entry.carbs_g, entry.grams) == (
        Decimal("270.0"), Decimal("2.4"), Decimal("10.1"), Decimal("42.0"), Decimal("60.0"),
    )
    assert entry.description == "Mars, 60 г"
    assert entry.raw_text == "[photo] 60 г"
    assert entry.eaten_at.replace(tzinfo=None) == sent_at.replace(tzinfo=None)  # the message time, not now
    (product,) = await product_rows(db)
    assert (product.name, product.barcode, float(product.kcal_100g), product.source) == (
        "Mars", "5000159407236", 450, "off",
    )
    async with db() as session:
        user = await session.scalar(select(UserRow).where(UserRow.telegram_id == USER))
    assert entry.user_id == product.user_id == user.id
    assert published == [(user.id, "nutrition")]
    text, kb = edited(cb)
    assert text.startswith("Mars, 60 г: 270 ккал, Б2 Ж10 У42") and "/products" in text and "Mars 50 г" in text
    assert kb == []
    cb.answer.assert_awaited_once_with()
    assert hp.PREVIEWS == {} and hp.CARDS == {} and USER not in hp.LATEST


async def test_double_tap_saves_once(db, published):
    ptoken, _ = await preview_for()
    first, second = callback(f"psave:{ptoken}"), callback(f"psave:{ptoken}")

    await hp.save(first, db)
    await hp.save(second, db)

    assert len(await food_rows(db)) == 1 and len(await product_rows(db)) == 1
    second.answer.assert_awaited_once_with(hp.STALE, show_alert=True)
    second.message.edit_text.assert_not_awaited()
    assert len(published) == 1


async def test_save_of_another_users_preview_is_stale_and_keeps_it(db, published):
    ptoken, _ = await preview_for()
    cb = callback(f"psave:{ptoken}", user_id=OTHER)
    await hp.save(cb, db)
    cb.answer.assert_awaited_once_with(hp.STALE, show_alert=True)
    assert await food_rows(db) == [] and published == []
    assert ptoken in hp.PREVIEWS  # the owner can still save
    owner = callback(f"psave:{ptoken}")
    await hp.save(owner, db)
    assert len(await food_rows(db)) == 1


async def test_save_with_unknown_token_is_stale(db, published):
    for data in ("psave:nope", "psave:", "psave"):
        cb = callback(data)
        await hp.save(cb, db)
        cb.answer.assert_awaited_once_with(hp.STALE, show_alert=True)
    assert await food_rows(db) == []


async def test_save_keeps_the_label_kcal_as_printed(db, published):
    odd = replace(BAR, kcal=round(298 * 0.82, 1), protein_g=20.0, fat_g=10.0, carbs_g=32.0)  # not 4/9/4
    ptoken, _ = await preview_for(odd, 100)
    await hp.save(callback(f"psave:{ptoken}"), db)
    (entry,) = await food_rows(db)
    assert entry.kcal == Decimal("244.4") and entry.protein_g == Decimal("20.0")


async def test_save_remembers_the_caption_alias(db, published):
    ptoken, _ = await preview_for(BAR, 60, alias="кирпич")
    await hp.save(callback(f"psave:{ptoken}"), db)
    (product,) = await product_rows(db)
    assert product.aliases == "кирпич"
    # ... and the alias works as a name next time
    taken, msg = await say(db, "съел кирпич")
    assert taken is True and "Bombbar Протеиновый батончик" in sent(msg)[0]


async def test_saving_the_same_product_again_updates_it_and_adds_an_entry(db, published):
    for _ in range(2):
        ptoken, _ = await preview_for()
        await hp.save(callback(f"psave:{ptoken}"), db)
    assert len(await food_rows(db)) == 2 and len(await product_rows(db)) == 1


async def test_save_failure_lets_the_user_press_again(db, published, monkeypatch):
    ptoken, _ = await preview_for()

    async def boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(hp.pr, "remember", boom)
    with pytest.raises(RuntimeError):
        await hp.save(callback(f"psave:{ptoken}"), db)
    assert ptoken in hp.PREVIEWS and await food_rows(db) == [] and published == []
    monkeypatch.undo()
    cb = callback(f"psave:{ptoken}")
    await hp.save(cb, db)
    assert len(await food_rows(db)) == 1


async def test_drop_cancels_and_the_save_button_goes_stale(db, published):
    ptoken, _ = await preview_for()
    cb = callback(f"pdrop:{ptoken}")
    await hp.drop(cb)
    cb.message.edit_text.assert_awaited_once_with("Отменено.")
    assert hp.PREVIEWS == {} and hp.CARDS == {} and USER not in hp.LATEST
    late = callback(f"psave:{ptoken}")
    await hp.save(late, db)
    late.answer.assert_awaited_once_with(hp.STALE, show_alert=True)
    assert await food_rows(db) == []


async def test_drop_of_another_users_preview_changes_nothing(db):
    ptoken, _ = await preview_for()
    cb = callback(f"pdrop:{ptoken}", user_id=OTHER)
    await hp.drop(cb)
    assert ptoken in hp.PREVIEWS and len(hp.CARDS) == 1


# ---- /products ----


async def test_products_empty(db):
    msg = message("/products")
    await hp.list_products(msg, db)
    msg.answer.assert_awaited_once_with(hp.NO_PRODUCTS, reply_markup=None)


async def test_products_list_with_delete_buttons(db):
    await saved(db, BAR, MARS)
    msg = message("/products")
    await hp.list_products(msg, db)
    text, kb = sent(msg)
    assert text.startswith("Мои продукты (на 100 г):")
    assert "1. Mars: 450 ккал, Б4 Ж16.8 У70 (уп. 51 г)" in text
    assert "2. Bombbar Протеиновый батончик: 360 ккал, Б33 Ж12 У30 (уп. 60 г, порция 30 г)" in text
    assert [t for t, _ in kb] == ["🗑 1. Mars", "🗑 2. Bombbar Протеиновый батончик"]  # newest first
    async with db() as session:
        ids = [p.product_id for p in await pr.user_products(session, USER)]
    assert [d for _, d in kb] == [f"pdel:{i}" for i in ids]


async def test_products_list_is_per_user(db):
    await saved(db, MARS, user_id=OTHER)
    msg = message("/products")
    await hp.list_products(msg, db)
    assert sent(msg)[0] == hp.NO_PRODUCTS


async def test_pdel_deletes_the_owners_product_and_redraws_the_list(db):
    await saved(db, BAR, MARS)
    async with db() as session:
        mars_id = (await pr.user_products(session, USER))[0].product_id
    cb = callback(f"pdel:{mars_id}")
    await hp.delete_product(cb, db)
    text, kb = edited(cb)
    assert "Mars" not in text and "Bombbar" in text and len(kb) == 1
    cb.answer.assert_awaited_once_with("Удалено")
    assert [p.name for p in await product_rows(db)] == ["Протеиновый батончик"]


async def test_pdel_of_the_last_product_shows_the_empty_text(db):
    await saved(db, MARS)
    (row,) = await product_rows(db)
    cb = callback(f"pdel:{row.id}")
    await hp.delete_product(cb, db)
    assert edited(cb)[0] == hp.NO_PRODUCTS


async def test_pdel_does_not_delete_another_users_product(db):
    await saved(db, MARS, user_id=OTHER)
    (row,) = await product_rows(db)
    cb = callback(f"pdel:{row.id}", user_id=USER)
    await hp.delete_product(cb, db)
    cb.answer.assert_awaited_once_with("Этого продукта уже нет.")
    assert len(await product_rows(db)) == 1


@pytest.mark.parametrize("data", ["pdel:abc", "pdel:", "pdel:9999"])
async def test_pdel_with_a_bad_id_is_a_no_op(db, data):
    await saved(db, MARS)
    cb = callback(data)
    await hp.delete_product(cb, db)
    cb.answer.assert_awaited_once_with("Этого продукта уже нет.")
    assert len(await product_rows(db)) == 1


# ---- routing: every product button reaches exactly its own handler ----


def telegram_callback(data: str) -> CallbackQuery:
    return CallbackQuery(id="1", from_user=User(id=USER, is_bot=False, first_name="Amir"), chat_instance="c", data=data)


@pytest.mark.parametrize(
    ("data", "handler"),
    [
        ("pc:abc:0", hp.pick),
        ("pa:abc:all", hp.choose_amount),
        ("pdrop:abc", hp.drop),
        ("psave:abc", hp.save),
        ("pdel:12", hp.delete_product),
    ],
)
async def test_each_product_button_matches_one_handler(data, handler):
    matched = []
    for h in hp.router.callback_query.handlers:
        ok, _ = await h.check(telegram_callback(data))
        if ok:
            matched.append(h.callback)
    assert matched == [handler]


async def test_products_command_is_routed():
    (h,) = [h for h in hp.router.message.handlers if h.callback is hp.list_products]
    msg = Message(message_id=1, date=T0, chat=Chat(id=USER, type="private"), text="/products")
    ok, _ = await h.check(msg, bot=Bot("123:abc"))
    assert ok
