"""Plausibility of parsed food estimates: density, references, the repair round and its fallbacks."""

import asyncio
import json
import logging
from datetime import UTC, datetime

import pytest
from test_log_text import FakeLLM, callback, message, token_of
from test_photo_handler import Vision, make_message
from test_products_text import save_products

from gymbot.handlers import log_text, photo
from gymbot.handlers import products as hp
from gymbot.handlers import saved_edits as hse
from gymbot.llm.openrouter import LLMError
from gymbot.llm.prompts import EXAMPLES
from gymbot.llm.schemas import ParsedFood, ParseResult
from gymbot.services import plausibility as pl
from gymbot.services.products import ProductInfo

T0 = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
INCIDENT_TEXT = "съел 2 самсы, а не 3 как обычно"
# The incident: ~240 g of samsa at 8.8 kcal/g (macros as absurd as the kcal, so ParsedFood kept them).
INCIDENT = {"description": "самса, 2 шт", "grams": 240, "kcal": 2116, "protein_g": 30, "fat_g": 186, "carbs_g": 85}


def f(description: str, grams: float | None, kcal: float, p: float = 0, fat: float = 0, c: float = 0) -> ParsedFood:
    """A food with macros that agree with kcal unless given (then kcal is what the macros say)."""
    if not (p or fat or c):
        c = kcal / 4
    return ParsedFood(description=description, grams=grams, kcal=kcal, protein_g=p, fat_g=fat, carbs_g=c)


def foods_result(*foods: ParsedFood | dict, **kw) -> ParseResult:
    return ParseResult.model_validate({"kind": "food", "foods": [x if isinstance(x, dict) else x.model_dump() for x in foods], **kw})


def kinds(food: ParsedFood) -> set[tuple[str, bool]]:
    return {(x.kind, x.high) for x in pl.flags(food)}


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    async def parser_answer(message, text, result, *args):
        return result

    monkeypatch.setattr(log_text, "_diary_answer", parser_answer)
    stores = (
        log_text.PENDING, log_text.CONTEXT, log_text.FACTS, log_text.LOOKUPS, log_text.QA,
        hp.CARDS, hp.PREVIEWS, hp.CHOICES, hp.LATEST, hse.OFFERS, hse.CHOICES,
    )
    for store in stores:
        store.clear()
    yield
    for store in stores:
        store.clear()


# ---- the reference table ----


def test_reference_macros_agree_with_their_kcal():
    """ParsedFood rewrites kcal that differs from 4P+9F+4C by >15%: a reference must survive it."""
    for ref in pl.REFERENCES:
        computed = 4 * ref.protein_g + 9 * ref.fat_g + 4 * ref.carbs_g
        assert abs(computed - ref.kcal) <= 0.06 * ref.kcal, ref.key
        fixed = pl.from_reference(ParsedFood(description=ref.key, grams=200, kcal=1, protein_g=0, fat_g=0, carbs_g=0), ref)
        assert fixed.kcal == pytest.approx(ref.kcal * 2, abs=1), ref.key


def test_glossary_weights_match_the_prompt():
    assert pl.reference_for("самса").piece_g == 120
    assert pl.reference_for("лепёшка").piece_g == 250
    assert pl.reference_for("плов, каса").piece_g == 300
    assert pl.reference_for("курт").per_piece == pytest.approx(65)
    assert pl.reference_for("самса").per_piece == pytest.approx(300)
    assert pl.reference_for("лепёшка").per_piece == pytest.approx(650)


@pytest.mark.parametrize(
    ("name", "key"),
    [
        ("самса с курицей, 3 шт", "самса"),
        ("Самсы", "самса"),
        ("лепёшка, 0.5 шт", "лепёшка"),
        ("плов, каса", "плов"),
        ("манты, 3 шт", "манты"),
        ("курты, 5 шт", "курт"),
        ("яйцо варёное, 2 шт", "яйцо"),
        ("чак-чак", "чак-чак"),
        ("хлеб чёрный", "хлеб"),
        ("куртоб", None),  # another dish, not курт
        ("яичница", None),  # fried in oil
        ("хлебцы", None),
        ("хлеб с маслом", None),  # fat stem: density only
        ("банан в шоколаде", None),
        ("яйцо и хлеб", None),  # two references
        ("куриная грудка", None),
    ],
)
def test_reference_matching(name, key):
    ref = pl.reference_for(name)
    assert (ref.key if ref else None) == key


# ---- density ----


def test_incident_is_flagged_by_density_macros_and_reference():
    assert kinds(ParsedFood(**INCIDENT)) == {("density", True), ("macros", True), ("reference", True)}


def test_generic_food_above_6_5_kcal_per_gram_is_flagged():
    assert ("density", True) in kinds(f("котлета", 100, 700, p=20, fat=70, c=5))
    assert kinds(f("котлета", 100, 640, p=20, fat=60, c=10)) == set()


@pytest.mark.parametrize(
    "food",
    [
        f("масло сливочное", 100, 748, p=0.5, fat=82.5, c=0.8),
        f("масло подсолнечное, 1 ст. л.", 17, 150, fat=17),
        f("грецкие орехи", 100, 654, p=15, fat=65, c=14),
        f("арахис жареный", 100, 610, p=26, fat=52, c=10),
        f("семечки подсолнуха", 100, 600, p=21, fat=53, c=11),
        f("шоколад горький", 100, 560, p=8, fat=36, c=48),
        f("сало", 100, 800, p=2, fat=89, c=0),
        f("майонез", 50, 340, fat=37, c=1.5),
        f("чипсы", 100, 540, p=6, fat=35, c=50),
        f("халва", 100, 520, p=12, fat=30, c=50),
        f("кешью", 50, 300, p=9, fat=24, c=13),
        f("фундук", 100, 650, p=15, fat=62, c=9),
    ],
)
def test_fats_nuts_seeds_sweets_may_be_dense(food):
    assert pl.flags(food) == []


def test_even_fats_have_a_ceiling():
    assert ("density", True) in kinds(f("масло сливочное", 100, 1100, fat=122))


@pytest.mark.parametrize(
    "food",
    [
        f("чай", 250, 2, c=0.5),
        f("вода", 500, 0),
        f("кофе чёрный", 200, 4, p=0.3, c=0.7),
        f("кола зеро", 330, 1, c=0.2),
        f("минералка", 500, 0),
        f("американо", 300, 5, p=0.3, c=1),
        f("бульон овощной", 300, 15, c=3.75),
    ],
)
def test_drinks_may_be_about_zero(food):
    assert pl.flags(food) == []


def test_food_with_almost_no_calories_is_flagged_low():
    assert ("density", False) in kinds(f("куриная грудка", 500, 4, p=1))


def test_macros_above_the_weight_are_flagged():
    sugar = f("сахар", 100, 480, c=120)  # 4.8 kcal/g is fine, 120 g of carbs in 100 g is not
    assert kinds(sugar) == {("macros", True)}


def test_tiny_items_are_not_flagged():
    assert pl.flags(f("соль", 2, 0)) == []
    assert pl.flags(f("соус", 5, 60, fat=6, c=1.5)) == []  # 12 kcal/g, but 27 kcal over the bound
    assert pl.flags(f("огурец", 100, 5, c=1.25)) == []  # under 0.1 kcal/g by 5 kcal only


# ---- references ----


def test_reference_scales_by_pieces_without_grams():
    three = f("самса, 3 шт", None, 3000, p=100, fat=200, c=300)
    flag = next(x for x in pl.flags(three) if x.kind == "reference")
    assert flag.high and flag.expected_kcal == pytest.approx(900)
    fixed = pl.from_reference(three, flag.ref)
    assert fixed.grams == 360 and fixed.kcal == 900
    assert pl.flags(f("самса, 3 шт", None, 1000, p=35, fat=55, c=90)) == []


def test_reference_scales_by_grams_first():
    # Users state their own piece sizes: 3 big manty of 90 g are 620 kcal (the prompt's example)
    assert pl.flags(f("манты, 3 шт", 270, 620, p=30, fat=30, c=57)) == []
    # 17 small pieces in 250 g: grams win over the count
    assert pl.flags(f("самса, 17 шт", 250, 650, p=25, fat=40, c=45)) == []
    plov = f("плов", 450, 2000, p=60, fat=120, c=170)
    flag = next(x for x in pl.flags(plov) if x.kind == "reference")
    assert flag.expected_kcal == pytest.approx(810)
    assert pl.from_reference(plov, flag.ref).kcal == 810


def test_reference_without_amount_is_one_portion():
    plov = f("плов", None, 2000, p=60, fat=120, c=170)
    flag = next(x for x in pl.flags(plov) if x.kind == "reference")
    assert flag.expected_kcal == pytest.approx(540)
    fixed = pl.from_reference(plov, flag.ref)
    assert (fixed.grams, fixed.kcal) == (300, 540)


async def test_assumed_portion_is_never_replaced_by_the_reference():
    # "плов, 3 касы" without grams: one каса is the bot's guess, so a failed repair only warns
    plov = f("плов, 3 касы", None, 1620, p=54, fat=63, c=207)
    assert ("reference", True) in kinds(plov)
    out = await pl.review(foods_result(plov), None)
    assert out.foods[0].kcal == 1620 and out.note == "Проверь калории — оценка выглядит завышенной (плов, 3 касы)."
    counted = await pl.review(foods_result(f("самса, 3 шт", None, 3000, p=100, fat=200, c=300)), None)
    assert counted.foods[0].kcal == 900 and "Скорректировал по справочнику" in counted.note


def test_reference_bounds_are_x2_above_and_2_5_below():
    assert pl.flags(f("лепёшка, 1 шт", 250, 1245, p=40, fat=45, c=170)) == []  # 1.9x
    assert ("reference", True) in kinds(f("лепёшка, 1 шт", 250, 1400, p=40, fat=60, c=175))  # 2.15x
    assert pl.flags(f("лепёшка, 1 шт", 250, 300, p=10, fat=2, c=60)) == []  # ÷2.2
    assert ("reference", False) in kinds(f("лепёшка, 1 шт", 250, 200, p=7, fat=1, c=41))  # ÷3.25


# ---- no false flags on normal answers ----

NORMAL = [
    f("куриная грудка", 200, 330, p=62, fat=7, c=0),
    f("рис варёный", 150, 195, p=4, fat=0.5, c=42),
    f("плов, каса", 300, 540, p=18, fat=21, c=69),
    f("плов", None, 600, p=20, fat=25, c=75),
    f("лепёшка, 0.5 шт", 125, 325, p=11, fat=2, c=65),
    f("лепёшка, 1 шт", 250, 650, p=22, fat=4, c=130),
    f("самса, 2 шт", 300, 800, p=30, fat=44, c=76),
    f("самса с курицей, 3 шт", 360, 1050, p=40, fat=58, c=94),
    f("самса с курицей, 4 шт", 480, 1460, p=40, fat=89, c=125),
    f("манты, 3 шт", 270, 620, p=30, fat=30, c=57),
    f("манты, 5 шт", None, 650, p=30, fat=33, c=60),
    f("чучвара, каса", 250, 550, p=25, fat=24, c=60),
    f("лагман, каса", 300, 400, p=18, fat=15, c=50),
    f("шурпа, каса", 300, 270, p=15, fat=18, c=12),
    f("шашлык, 2 шт", 200, 510, p=40, fat=38, c=2),
    f("курт, 5 шт", 50, 130, p=12.5, fat=7.5, c=1.5),
    f("курт маленький, 3 шт", 30, 78, p=7.5, fat=4.5, c=0.9),
    f("катык", 200, 114, p=6, fat=6.4, c=8),
    f("яйцо варёное, 2 шт", 100, 155, p=13, fat=11, c=1),
    f("банан, 1 шт", 120, 110, p=1.3, fat=0.4, c=26),
    f("хлеб, 2 куска", 60, 154, p=5, fat=2, c=29),
    f("гречка варёная", 200, 220, p=8, fat=2, c=43),
    f("творог 5%", 200, 250, p=34, fat=10, c=6),
    f("овсянка на молоке", 250, 252, p=9, fat=8, c=36),
    f("чай с сахаром", 250, 40, c=10),
    f("чай", 200, 2, c=0.5),
    f("кофе с молоком", 250, 59, p=3, fat=3, c=5),
    f("кола", 330, 140, c=35),
    f("грецкие орехи", 100, 654, p=15, fat=65, c=14),
    f("масло сливочное", 10, 75, fat=8.3),
    f("салат из овощей", 150, 64, p=2, fat=4, c=5),
]


@pytest.mark.parametrize("food", NORMAL, ids=lambda x: x.description)
def test_normal_answers_are_not_flagged(food):
    assert pl.flags(food) == []


def test_prompt_examples_are_not_flagged():
    foods = [ParsedFood(**x) for _, a in EXAMPLES for x in json.loads(a).get("foods", [])]
    assert len(foods) >= 8
    assert [x.description for x in foods if pl.flags(x)] == []


async def test_nothing_flagged_means_no_repair_call():
    async def reparse(correction):
        raise AssertionError("no repair for a plausible answer")

    result = foods_result(*NORMAL[:5])
    assert await pl.review(result, reparse) is result


# ---- review: repair, reference, warning ----


async def test_incident_without_repair_falls_back_to_the_reference():
    out = await pl.review(foods_result(INCIDENT), None)
    (food,) = out.foods
    assert (food.description, food.grams, food.kcal) == ("самса, 2 шт", 240, 600)
    assert (food.protein_g, food.fat_g, food.carbs_g) == (21.6, 31.2, 60.0)
    assert "Скорректировал по справочнику: самса, 2 шт 2116 → 600 ккал (обычно самса ≈ 300 ккал за шт (120 г))." in out.note


async def test_repair_that_fixes_the_estimate_is_taken():
    asked = []

    async def reparse(correction):
        asked.append(correction)
        good = {**INCIDENT, "description": "самса", "kcal": 620, "protein_g": 22, "fat_g": 32, "carbs_g": 61}
        return foods_result(good, revises=True, note="Пересчитал")

    out = await pl.review(foods_result(INCIDENT, note="своя"), reparse)
    assert asked == [
        (
            "Оценка «самса, 2 шт 240 г — 2116 ккал» неправдоподобна: обычно самса ≈ 300 ккал за шт (120 г). "
            "Пересчитай КБЖУ и верни ПОЛНУЮ запись."
        )
    ]
    (food,) = out.foods
    assert food.kcal == pytest.approx(620, abs=10) and food.description == "самса, 2 шт"
    assert out.revises is False and out.note == "своя"  # the correction is the bot's, not a revision


async def test_repair_that_is_still_implausible_falls_back_per_item():
    tea = f("чай", 200, 2, c=0.5)

    async def reparse(correction):
        return foods_result({**INCIDENT, "kcal": 1900, "fat_g": 165}, tea)

    out = await pl.review(foods_result(INCIDENT, tea), reparse)
    assert [x.kcal for x in out.foods] == [600, 2]
    assert "Скорректировал по справочнику" in out.note


@pytest.mark.parametrize("answer", ["error", "question", "fewer foods"])
async def test_unusable_repair_falls_back(answer):
    async def reparse(correction):
        if answer == "error":
            raise LLMError("all models failed")
        if answer == "question":
            return ParseResult(kind="question", clarification="?")
        return foods_result()

    out = await pl.review(foods_result(INCIDENT, f("чай", 200, 2, c=0.5)), reparse)
    assert [x.kcal for x in out.foods] == [600, 2]


async def test_without_a_reference_the_preview_gets_a_warning():
    cutlet = f("котлета", 100, 700, p=20, fat=70, c=5)
    meat = f("куриная грудка", 500, 4, p=1)

    async def reparse(correction):
        return foods_result(cutlet, meat)  # the same again

    out = await pl.review(foods_result(cutlet, meat, note="оценка"), reparse)
    assert [x.kcal for x in out.foods] == [cutlet.kcal, meat.kcal]  # never blocked, never invented
    assert out.note.split("\n") == [
        "оценка",
        "Проверь калории — оценка выглядит завышенной (котлета).",
        "Проверь калории — оценка выглядит заниженной (куриная грудка).",
    ]


async def test_exact_numbers_are_untouched():
    async def reparse(correction):
        raise AssertionError("exact numbers are never re-asked")

    result = foods_result(INCIDENT)
    assert await pl.review(result, reparse, exact=lambda food: True) is result


async def test_other_kinds_pass_through():
    result = ParseResult(kind="question", clarification="2 самсы — это около 600 ккал.")
    assert await pl.review(result, None) is result


def test_flags_are_logged_without_text(caplog):
    with caplog.at_level(logging.INFO, logger="gymbot.services.plausibility"):
        asyncio.run(pl.review(foods_result(INCIDENT), None))
    assert "food plausibility" in caplog.text and "самса, 2" not in caplog.text and "2116" not in caplog.text


# ---- the handlers ----


async def send(text, llm, settings, db):
    msg = message(text, T0)
    await log_text.process_text(msg, text, settings, db, llm.client)
    return msg


async def test_incident_in_chat_repair_then_reference(settings, db):
    llm = FakeLLM(settings)
    bad = {"kind": "food", "foods": [INCIDENT], "revises": False, "note": None}
    llm.answers = [bad, {**bad, "revises": True}]

    msg = await send(INCIDENT_TEXT, llm, settings, db)

    assert len(llm.bodies) == 2
    repair = llm.last_messages()
    assert repair[-1].startswith("Оценка «самса, 2 шт 240 г — 2116 ккал» неправдоподобна")
    assert repair[-3] == INCIDENT_TEXT and json.loads(repair[-2])["foods"][0]["kcal"] == 2116
    text = msg.answer.await_args.args[0]
    assert text.startswith("Записать еду? Всего 600 ккал") and "Скорректировал по справочнику" in text
    (pending,) = log_text.PENDING.values()
    assert pending.raw_text == INCIDENT_TEXT  # the correction is not the user's text
    assert pending.result.foods[0].kcal == 600 and pending.result.revises is False
    assert log_text.CONTEXT[42].texts == [INCIDENT_TEXT]


async def test_incident_in_chat_repaired_by_the_model(settings, db):
    llm = FakeLLM(settings)
    good = {**INCIDENT, "kcal": 640, "protein_g": 22, "fat_g": 34, "carbs_g": 62}
    llm.answers = [{"kind": "food", "foods": [INCIDENT]}, {"kind": "food", "revises": True, "foods": [good]}]

    msg = await send(INCIDENT_TEXT, llm, settings, db)

    text = msg.answer.await_args.args[0]
    assert "Всего 6" in text and "Скорректировал" not in text and "Проверь калории" not in text
    (pending,) = log_text.PENDING.values()
    assert 580 <= pending.result.foods[0].kcal <= 700


async def test_plausible_chat_answer_costs_one_request(settings, db):
    llm = FakeLLM(settings)
    llm.answers = [{"kind": "food", "foods": [NORMAL[2].model_dump(), NORMAL[4].model_dump()]}]
    msg = await send("плов каса и пол лепёшки", llm, settings, db)
    assert len(llm.bodies) == 1 and "Всего 865 ккал" in msg.answer.await_args.args[0]


@pytest.mark.parametrize("saved", [True, False])
async def test_saved_product_numbers_are_not_rechecked(settings, db, saved):
    # Label numbers a reference would call absurd (700 kcal per 100 g of bread): the user's product wins.
    toast = ProductInfo("Хлеб тостовый", "Harrys", 700.0, 10.0, 50.0, 50.0, net_weight_g=500.0, source="label")
    if saved:
        await save_products(db, toast)
    llm = FakeLLM(settings)
    exact = {"description": "хлеб тостовый Harrys", "grams": 100, "kcal": 700, "protein_g": 10, "fat_g": 50,
             "carbs_g": 50}
    llm.answers = [{"kind": "food", "foods": [exact]}, {"kind": "food", "foods": [exact]}]

    msg = await send("хлеб тостовый 100 г и кофе", llm, settings, db)

    assert ("Мои продукты" in llm.bodies[0]["messages"][0]["content"]) is saved
    assert len(llm.bodies) == (1 if saved else 2)  # an estimate is re-asked, the label's numbers are not
    text = msg.answer.await_args.args[0]
    assert ("Всего 700 ккал" in text) is saved and ("Скорректировал" in text) is not saved


async def test_photo_plate_is_repaired_over_the_text_parser(settings, db):
    v = Vision(settings, {"foods": [INCIDENT], "note": None})
    v.text_answers = [{"kind": "food", "foods": [{**INCIDENT, "kcal": 1800, "fat_g": 156}]}]
    msg = make_message("2 самсы")

    await photo.log_photo(msg, settings, db, v.client)

    assert len(v.photo_bodies) == 1 and len(v.text_bodies) == 1  # the image is not sent again
    sent = [m["content"] for m in v.text_bodies[0]["messages"]]
    assert sent[-3] == "Фото еды. Подпись: 2 самсы" and sent[-1].startswith("Оценка «самса, 2 шт")
    text = msg.answer.await_args.args[0]
    assert "Всего 600 ккал" in text and "Скорректировал по справочнику" in text
    (pending,) = log_text.PENDING.values()
    assert pending.raw_text == "[photo] 2 самсы"


async def test_saved_edit_estimate_is_checked_too(settings, db):
    llm = FakeLLM(settings)
    three = {"description": "самса, 3 шт", "grams": 360, "kcal": 900, "protein_g": 32, "fat_g": 47, "carbs_g": 90}
    llm.answers = [{"kind": "food", "foods": [three]}]
    first = await send("три самсы", llm, settings, db)
    await log_text.save(callback(f"save:{token_of(first)}"), settings, db)
    bad = {"kind": "food", "revises": True, "foods": [INCIDENT]}
    llm.answers = [bad, bad]

    edit = message("самса была 2 а не 3", T0.replace(minute=1))
    await log_text.process_text(edit, "самса была 2 а не 3", settings, db, llm.client)

    assert len(llm.bodies) == 3  # the record, the edit, one repair round
    text = edit.answer.await_args.args[0]
    assert text.startswith("Исправить?") and "600 ккал" in text and "Скорректировал по справочнику" in text
