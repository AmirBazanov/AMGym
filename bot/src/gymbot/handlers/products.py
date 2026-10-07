"""Packaged products in the chat: a product card -> «Сколько съел?» -> an exact food preview -> «Сохранить».

Where a card comes from:
  - a food photo with a barcode found in Open Food Facts, or with a readable label (handlers/photo.py, `offer`);
  - a short message about a saved product ("тот же батончик", "протеин 1 скуп"): log_text.process_text calls
    `on_text` before the parser. Several saved products fit -> buttons to pick one.
The amount comes from a button (вся упаковка / ½ / порция / «Ввести граммы») or the next message ("60 г",
"60", "2 порции"): `on_text` takes such a message for the newest open card, unless the parser's dialog is newer.
The preview has its own buttons (psave/pdrop): the food is saved with estimated=False, the product goes to the
user's products (gymbot.services.products.remember), and the Mini App gets a "nutrition" refresh.
/products lists saved products with 🗑 buttons. State lives in memory (by token) for TTL, like log_text.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from gymbot.db.models import FoodEntry
from gymbot.db.session import Sessionmaker
from gymbot.llm.schemas import ParsedFood
from gymbot.services import live
from gymbot.services import products as pr
from gymbot.services.users import get_or_create_user

log = logging.getLogger(__name__)
router = Router(name="products")

TTL = timedelta(minutes=15)
MAX_PENDING = 200
CANDIDATES_MAX = 4
STALE = "Эта запись уже сохранена или устарела."
ASK_GRAMS = "Напиши, сколько съел, например «60 г», «2 порции» или «половина»."
WHICH = "Какой продукт?"
SAVED = "Еда сохранена ✅ Продукт в /products: в следующий раз хватит «{name} 50 г»."
NO_PRODUCTS = "Сохранённых продуктов пока нет. Пришли фото штрихкода или этикетки упаковки."
SOURCE = {"off": "Open Food Facts", "label": "с этикетки", "manual": "вручную"}


@dataclass
class Card:
    """A product the user is logging: waits for the amount, then holds the current preview."""

    user_id: int  # Telegram id
    product: pr.ProductInfo
    raw_text: str
    sent_at: datetime  # the user's message time: the entry belongs to it
    at: datetime  # last activity (TTL; newer than the parser's dialog = "60 г" answers this card)
    note: str | None = None
    alias: str | None = None  # a caption like "мой протеин", remembered with the product
    preview: str | None = None  # token in PREVIEWS


@dataclass
class Preview:
    card: str  # token in CARDS
    user_id: int
    food: ParsedFood
    raw_text: str


@dataclass
class Choice:
    """Several saved products fit a message: buttons to pick one."""

    user_id: int
    products: list[pr.ProductInfo]
    amount: pr.Amount
    raw_text: str
    sent_at: datetime
    prefix: str = ""


CARDS: dict[str, Card] = {}
PREVIEWS: dict[str, Preview] = {}
CHOICES: dict[str, Choice] = {}
LATEST: dict[int, str] = {}  # Telegram id -> token of the newest card


def _token(store: dict) -> str:
    if len(store) >= MAX_PENDING:
        store.pop(next(iter(store)))
    return secrets.token_hex(6)


def _fmt(v: float | None) -> str:
    return f"{v:.1f}".rstrip("0").rstrip(".") if v is not None else "?"


def card_text(card: Card) -> str:
    p = card.product
    lines = [f"{p.title} ({SOURCE.get(p.source, p.source)})"]
    lines.append(
        f"На 100 г: {_fmt(p.kcal)} ккал, Б{_fmt(p.protein_g)} Ж{_fmt(p.fat_g)} У{_fmt(p.carbs_g)}"
    )
    sizes = [f"упаковка {p.net_weight_g:g} г" if p.net_weight_g else "", f"порция {p.serving_g:g} г" if p.serving_g else ""]
    if any(sizes):
        lines.append(", ".join(s for s in sizes if s).capitalize())
    if card.note:
        lines.append(f"\n{card.note}")
    lines.append("\nСколько съел? Нажми кнопку или напиши, например «60 г».")
    return "\n".join(lines)


def amount_keyboard(token: str, p: pr.ProductInfo) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if p.net_weight_g:
        rows.append(
            [
                InlineKeyboardButton(text=f"Вся упаковка ({p.net_weight_g:g} г)", callback_data=f"pa:{token}:all"),
                InlineKeyboardButton(text=f"½ ({p.net_weight_g / 2:g} г)", callback_data=f"pa:{token}:half"),
            ]
        )
    if p.serving_g:
        rows.append([InlineKeyboardButton(text=f"Порция ({p.serving_g:g} г)", callback_data=f"pa:{token}:srv")])
    rows.append(
        [
            InlineKeyboardButton(text="Ввести граммы", callback_data=f"pa:{token}:g"),
            InlineKeyboardButton(text="✖ Отмена", callback_data=f"pa:{token}:x"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def preview_text(food: ParsedFood, note: str | None) -> str:
    text = (
        f"Записать еду? Всего {food.kcal:.0f} ккал\n"
        f"• {food.description}: {food.kcal:.0f} ккал, Б{food.protein_g:.0f} Ж{food.fat_g:.0f} У{food.carbs_g:.0f}"
    )
    return text + (f"\n\n{note}" if note else "")


def preview_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Сохранить", callback_data=f"psave:{token}"),
                InlineKeyboardButton(text="✖ Отмена", callback_data=f"pdrop:{token}"),
            ]
        ]
    )


def _new_card(card: Card) -> str:
    token = _token(CARDS)
    CARDS[token] = card
    LATEST[card.user_id] = token
    return token


def _make_preview(card_token: str, grams: float) -> tuple[str, str]:
    """(text, preview token) for `grams` of the card's product; the card's previous preview stops working."""
    card = CARDS[card_token]
    if card.preview:
        PREVIEWS.pop(card.preview, None)
    food = pr.food_for(card.product, grams)
    token = _token(PREVIEWS)
    PREVIEWS[token] = Preview(card_token, card.user_id, food, card.raw_text)
    card.preview = token
    return preview_text(food, card.note), token


async def offer(
    message: Message,
    product: pr.ProductInfo,
    *,
    raw_text: str,
    amount: pr.Amount | None = None,
    note: str | None = None,
    alias: str | None = None,
    prefix: str = "",
) -> None:
    """A card for the product: the exact preview right away when `amount` gives grams, else «Сколько съел?»."""
    user_id = message.from_user.id  # type: ignore[union-attr]
    card = Card(user_id, product, raw_text, message.date, message.date, note=note, alias=alias)
    token = _new_card(card)
    grams = pr.grams_for(product, amount) if amount is not None else None
    if grams:
        text, ptoken = _make_preview(token, grams)
        await message.answer(prefix + text, reply_markup=preview_keyboard(ptoken))
        return
    await message.answer(prefix + card_text(card), reply_markup=amount_keyboard(token, product))


def _open_card(user_id: int, now: datetime) -> tuple[str, Card] | None:
    token = LATEST.get(user_id)
    card = CARDS.get(token) if token else None
    if card is None or now - card.at > TTL:
        return None
    return token, card  # type: ignore[return-value]


async def on_text(
    message: Message,
    text: str,
    raw_text: str,
    sessionmaker: Sessionmaker,
    *,
    dialog_at: datetime | None = None,
    prefix: str = "",
) -> bool:
    """Handle a text message about a packaged product; False = not ours, the parser takes it.

    1. An amount alone ("60 г", "60", "2 порции") for the newest open card, if that card is newer than the
       parser's dialog (`dialog_at`): otherwise "200" may answer the parser's «Сколько грамм творога?».
    2. A message about a saved product (services.products.match).
    """
    user_id = message.from_user.id  # type: ignore[union-attr]
    now = message.date
    if (opened := _open_card(user_id, now)) is not None and (dialog_at is None or opened[1].at >= dialog_at):
        token, card = opened
        amount, rest = pr.parse_amount(text)
        own = pr.keys(card.product)
        if not amount.empty and all(any(pr.same_word(w, k) for k in own) for w in rest):
            grams = pr.grams_for(card.product, amount)
            if not grams:
                await message.answer(prefix + ASK_GRAMS)
                return True
            card.at = now
            if raw_text not in card.raw_text.split("\n"):
                card.raw_text = f"{card.raw_text}\n{raw_text}"
            preview, ptoken = _make_preview(token, grams)
            await message.answer(prefix + preview, reply_markup=preview_keyboard(ptoken))
            return True
    async with sessionmaker() as session:
        saved = await pr.user_products(session, user_id)
    found = pr.match(text, saved)
    if found is None:
        return False
    if len(found.products) == 1:
        await offer(message, found.products[0], raw_text=raw_text, amount=found.amount, prefix=prefix)
        return True
    token = _token(CHOICES)
    options = found.products[:CANDIDATES_MAX]
    CHOICES[token] = Choice(user_id, options, found.amount, raw_text, now, prefix)
    rows = [[InlineKeyboardButton(text=p.title[:60], callback_data=f"pc:{token}:{i}")] for i, p in enumerate(options)]
    await message.answer(prefix + WHICH, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    return True


async def parser_context(sessionmaker: Sessionmaker, user_id: int, text: str) -> str:
    """'Мои продукты: ...' for the parser when the message names saved products (else '')."""
    async with sessionmaker() as session:
        saved = await pr.user_products(session, user_id)
    return pr.prompt_line(text, saved)


# ---- Buttons ----


def _parts(cb: CallbackQuery) -> list[str]:
    return (cb.data or "").split(":")


@router.callback_query(F.data.startswith("pc:"))
async def pick(cb: CallbackQuery) -> None:
    _, token, index = (_parts(cb) + ["", ""])[:3]
    choice = CHOICES.get(token)
    if choice is None or choice.user_id != cb.from_user.id or not index.isdigit() or int(index) >= len(choice.products):
        await cb.answer(STALE, show_alert=True)
        return
    CHOICES.pop(token, None)
    product = choice.products[int(index)]
    card = Card(choice.user_id, product, choice.raw_text, choice.sent_at, datetime.now(UTC))
    ctoken = _new_card(card)
    grams = pr.grams_for(product, choice.amount)
    if cb.message:
        if grams:
            text, ptoken = _make_preview(ctoken, grams)
            await cb.message.edit_text(text, reply_markup=preview_keyboard(ptoken))  # type: ignore[union-attr]
        else:
            await cb.message.edit_text(card_text(card), reply_markup=amount_keyboard(ctoken, product))  # type: ignore[union-attr]
    await cb.answer()


@router.callback_query(F.data.startswith("pa:"))
async def choose_amount(cb: CallbackQuery) -> None:
    _, token, what = (_parts(cb) + ["", ""])[:3]
    card = CARDS.get(token)
    if card is None or card.user_id != cb.from_user.id:
        await cb.answer(STALE, show_alert=True)
        return
    p = card.product
    if what == "x":
        _close(token)
        if cb.message:
            await cb.message.edit_text("Отменено.")  # type: ignore[union-attr]
        await cb.answer()
        return
    if what == "g":
        card.at = datetime.now(UTC)
        LATEST[card.user_id] = token
        await cb.answer()
        if cb.message:
            await cb.message.answer(ASK_GRAMS)  # type: ignore[union-attr]
        return
    grams = {
        "all": p.net_weight_g,
        "half": p.net_weight_g / 2 if p.net_weight_g else None,
        "srv": p.serving_g,
    }.get(what)
    if not grams:
        await cb.answer(STALE, show_alert=True)
        return
    card.at = datetime.now(UTC)
    text, ptoken = _make_preview(token, grams)
    if cb.message:
        await cb.message.edit_text(text, reply_markup=preview_keyboard(ptoken))  # type: ignore[union-attr]
    await cb.answer()


def _close(card_token: str) -> None:
    card = CARDS.pop(card_token, None)
    if card is None:
        return
    if card.preview:
        PREVIEWS.pop(card.preview, None)
    if LATEST.get(card.user_id) == card_token:
        del LATEST[card.user_id]


@router.callback_query(F.data.startswith("pdrop:"))
async def drop(cb: CallbackQuery) -> None:
    token = _parts(cb)[1] if len(_parts(cb)) > 1 else ""
    preview = PREVIEWS.get(token)
    if preview is not None and preview.user_id == cb.from_user.id:
        PREVIEWS.pop(token, None)
        _close(preview.card)
    if cb.message:
        await cb.message.edit_text("Отменено.")  # type: ignore[union-attr]
    await cb.answer()


@router.callback_query(F.data.startswith("psave:"))
async def save(cb: CallbackQuery, sessionmaker: Sessionmaker) -> None:
    token = _parts(cb)[1] if len(_parts(cb)) > 1 else ""
    preview = PREVIEWS.pop(token, None)  # pop: a double tap must not save twice
    if preview is None or preview.user_id != cb.from_user.id:
        if preview is not None:
            PREVIEWS[token] = preview
        await cb.answer(STALE, show_alert=True)
        return
    card = CARDS.get(preview.card)
    if card is None:
        await cb.answer(STALE, show_alert=True)
        return
    f = preview.food
    try:
        async with sessionmaker() as session:
            user = await get_or_create_user(session, cb.from_user.id, cb.from_user.full_name)
            session.add(
                FoodEntry(
                    user_id=user.id,
                    description=f.description,
                    grams=Decimal(str(f.grams)) if f.grams else None,
                    kcal=Decimal(str(f.kcal)),
                    protein_g=Decimal(str(f.protein_g)),
                    fat_g=Decimal(str(f.fat_g)),
                    carbs_g=Decimal(str(f.carbs_g)),
                    estimated=False,  # label numbers, not a model's guess
                    raw_text=preview.raw_text,
                    eaten_at=card.sent_at,
                )
            )
            await pr.remember(session, user.id, card.product, card.alias)
            await session.commit()
    except Exception:
        PREVIEWS[token] = preview  # let the user press again
        raise
    _close(preview.card)
    live.publish(user.id, "nutrition")
    if cb.message:
        shown = f"{f.description}: {f.kcal:.0f} ккал, Б{f.protein_g:.0f} Ж{f.fat_g:.0f} У{f.carbs_g:.0f}"
        await cb.message.edit_text(f"{shown}\n\n" + SAVED.format(name=card.product.name))  # type: ignore[union-attr]
    await cb.answer()


# ---- /products ----


def _list(saved: list[pr.ProductInfo]) -> tuple[str, InlineKeyboardMarkup | None]:
    if not saved:
        return NO_PRODUCTS, None
    lines = ["Мои продукты (на 100 г):"]
    rows = []
    for i, p in enumerate(saved, 1):
        sizes = ", ".join(
            s for s in (f"уп. {p.net_weight_g:g} г" if p.net_weight_g else "", f"порция {p.serving_g:g} г" if p.serving_g else "") if s
        )
        lines.append(
            f"{i}. {p.title}: {_fmt(p.kcal)} ккал, Б{_fmt(p.protein_g)} Ж{_fmt(p.fat_g)} У{_fmt(p.carbs_g)}"
            + (f" ({sizes})" if sizes else "")
        )
        rows.append([InlineKeyboardButton(text=f"🗑 {i}. {p.title[:40]}", callback_data=f"pdel:{p.product_id}")])
    lines.append("\nЗаписать: «<название> 50 г», «1 порция», «тот же». Удалить — кнопкой.")
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("products"))
async def list_products(message: Message, sessionmaker: Sessionmaker) -> None:
    async with sessionmaker() as session:
        saved = await pr.user_products(session, message.from_user.id)  # type: ignore[union-attr]
    text, kb = _list(saved)
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("pdel:"))
async def delete_product(cb: CallbackQuery, sessionmaker: Sessionmaker) -> None:
    raw = _parts(cb)[1] if len(_parts(cb)) > 1 else ""
    deleted = False
    async with sessionmaker() as session:
        if raw.isdigit():
            deleted = await pr.delete(session, cb.from_user.id, int(raw))
            await session.commit()
        saved = await pr.user_products(session, cb.from_user.id)
    text, kb = _list(saved)
    if cb.message:
        await cb.message.edit_text(text, reply_markup=kb)  # type: ignore[union-attr]
    await cb.answer("Удалено" if deleted else "Этого продукта уже нет.")

