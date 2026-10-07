"""Packaged products, the service side (gymbot.services.products): Open Food Facts by barcode, label checks,
OFF vs label, amounts, matching a message against saved products, remembering, migration 0012."""

from decimal import Decimal

import httpx
import pytest
from alembic import command
from sqlalchemy import inspect, select, text

from gymbot.db import migrate
from gymbot.db.models import Product
from gymbot.db.session import make_engine
from gymbot.llm.schemas import LabelPer100, ParsedLabel
from gymbot.services import products as pr
from gymbot.services.food_lookup import TIMEOUT, USER_AGENT
from gymbot.services.products import Amount, ProductInfo
from gymbot.services.users import get_or_create_user

CODE = "5000159407236"


def prod(name, brand=None, kcal=360.0, p=33.0, f=12.0, c=30.0, net=None, serving=None, **kw) -> ProductInfo:
    return ProductInfo(name=name, brand=brand, kcal=kcal, protein_g=p, fat_g=f, carbs_g=c, net_weight_g=net,
                       serving_g=serving, **kw)


MARS = prod("Mars", "Mars", 450, 4, 17, 70, net=51)
BAR = prod("Протеиновый батончик", "Bombbar", 360, 33, 12, 30, net=60)
WHEY = prod("Whey протеин", "Optimum Nutrition", 380, 75, 6, 8, serving=30)
CHOCO = prod("Протеин шоколад", "MyProtein", 400, 80, 5, 6, serving=25)
LOAF = prod("Батон нарезной", None, 260, 8, 3, 50, net=400)


def label(kcal=360, p=33, f=12, c=30, name="Протеиновый батончик", brand="Bombbar", net=60, serving=None) -> ParsedLabel:
    """As the vision client builds it: from the model's JSON (a LabelPer100 instance would be dropped, see below)."""
    return ParsedLabel.model_validate({
        "name": name, "brand": brand, "per100": {"kcal": kcal, "protein_g": p, "fat_g": f, "carbs_g": c},
        "net_weight_g": net, "serving_g": serving,
    })


# ---- Open Food Facts ----

OFF_FULL = {
    "status": 1,
    "product": {
        "product_name": "Protein bar",
        "product_name_ru": "Протеиновый батончик",
        "brands": "Bombbar, Другой бренд",
        "nutriments": {
            "energy-kcal_100g": 360, "proteins_100g": 33, "fat_100g": 12, "carbohydrates_100g": 30,
        },
        "product_quantity": "60",
        "serving_quantity": 30,
    },
}


def off_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def off_answer(payload, status=200):
    return off_client(lambda req: httpx.Response(status, json=payload))


async def test_off_request_has_the_code_the_fields_and_a_user_agent():
    seen: list[httpx.Request] = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json=OFF_FULL)

    async with off_client(handler) as http:
        await pr.off_product(CODE, http)

    (req,) = seen
    assert req.method == "GET"
    assert req.url.host == "world.openfoodfacts.org"
    assert req.url.path == f"/api/v2/product/{CODE}.json"
    assert req.url.params["fields"] == pr.OFF_FIELDS
    assert req.headers["user-agent"] == USER_AGENT
    assert req.extensions["timeout"]["read"] == TIMEOUT


async def test_off_found_product_has_names_numbers_and_weights():
    async with off_answer(OFF_FULL) as http:
        p = await pr.off_product(CODE, http)
    assert p is not None
    assert p.name == "Протеиновый батончик"  # the Russian name wins
    assert p.brand == "Bombbar"  # the first of the brands
    assert (p.kcal, p.protein_g, p.fat_g, p.carbs_g) == (360, 33, 12, 30)
    assert (p.net_weight_g, p.serving_g) == (60, 30)
    assert p.barcode == CODE and p.source == "off" and p.complete
    assert p.title == "Bombbar Протеиновый батончик"


async def test_off_unknown_product_is_none_for_status_0_and_for_404():
    async with off_answer({"status": 0, "status_verbose": "product not found", "code": CODE}) as http:
        assert await pr.off_product(CODE, http) is None
    async with off_answer({"status": 0}, status=404) as http:
        assert await pr.off_product(CODE, http) is None
    async with off_client(lambda req: httpx.Response(404, text="not json at all")) as http:
        assert await pr.off_product(CODE, http) is None


@pytest.mark.parametrize("payload", [[], "text", {"status": 1}, {"status": 1, "product": "x"}, {"product": {}}])
async def test_off_garbage_answers_are_none(payload):
    async with off_answer(payload) as http:
        assert await pr.off_product(CODE, http) is None


async def test_off_without_nutriments_is_incomplete():
    payload = {"status": 1, "product": {"product_name": "Конфеты", "brands": "Рот Фронт"}}
    async with off_answer(payload) as http:
        p = await pr.off_product(CODE, http)
    assert p is not None
    assert (p.kcal, p.protein_g, p.fat_g, p.carbs_g) == (None, None, None, None)
    assert not p.complete
    assert p.name == "Конфеты"


async def test_off_partial_nutriments_are_incomplete():
    payload = {"status": 1, "product": {"product_name": "X", "nutriments": {"energy-kcal_100g": 100, "fat_100g": 2}}}
    async with off_answer(payload) as http:
        p = await pr.off_product(CODE, http)
    assert p is not None and p.kcal == 100 and p.protein_g is None and not p.complete


async def test_off_kilojoules_only_are_converted():
    payload = {"status": 1, "product": {"product_name": "X", "nutriments": {
        "energy-kj_100g": 1506, "proteins_100g": 33, "fat_100g": 12, "carbohydrates_100g": 30}}}
    async with off_answer(payload) as http:
        p = await pr.off_product(CODE, http)
    assert p is not None and p.kcal == pytest.approx(360, abs=0.5)


async def test_off_kcal_wins_over_kilojoules():
    payload = {"status": 1, "product": {"product_name": "X", "nutriments": {
        "energy-kcal_100g": 100, "energy-kj_100g": 9999, "proteins_100g": 1, "fat_100g": 1, "carbohydrates_100g": 1}}}
    async with off_answer(payload) as http:
        p = await pr.off_product(CODE, http)
    assert p is not None and p.kcal == 100


async def test_off_numbers_may_be_strings_with_commas_and_junk_is_none():
    payload = {"status": 1, "product": {"product_name": "X", "nutriments": {
        "energy-kcal_100g": "449,5", "proteins_100g": "abc", "fat_100g": -3, "carbohydrates_100g": True}}}
    async with off_answer(payload) as http:
        p = await pr.off_product(CODE, http)
    assert p is not None
    assert p.kcal == 449.5
    assert (p.protein_g, p.fat_g, p.carbs_g) == (None, None, None)


async def test_off_without_a_name_is_named_by_its_code():
    async with off_answer({"status": 1, "product": {"nutriments": {}}}) as http:
        p = await pr.off_product(CODE, http)
    assert p is not None and p.name == f"Продукт {CODE}" and p.brand is None


async def test_off_name_is_capped_and_whitespace_collapsed():
    payload = {"status": 1, "product": {"product_name": "  Очень   " + "длинное " * 60, "brands": " A   B , C"}}
    async with off_answer(payload) as http:
        p = await pr.off_product(CODE, http)
    assert p is not None and len(p.name) <= 200 and "  " not in p.name
    assert p.brand == "A B"


@pytest.mark.parametrize(
    ("net", "serving", "kept"),
    [(51, 100, None), (51, 51, None), (60, 30, 30), (None, 100, 100), (60, None, None)],
    ids=["serving-bigger-than-pack", "serving-equals-pack", "normal", "no-pack-weight", "no-serving"],
)
async def test_off_serving_as_big_as_the_pack_is_dropped(net, serving, kept):
    product = {"product_name": "Bar", "nutriments": {}}
    if net is not None:
        product["product_quantity"] = net
    if serving is not None:
        product["serving_quantity"] = serving
    async with off_answer({"status": 1, "product": product}) as http:
        p = await pr.off_product(CODE, http)
    assert p is not None
    assert p.serving_g == kept
    assert p.net_weight_g == net


@pytest.mark.parametrize("exc", [httpx.ReadTimeout, httpx.ConnectTimeout, httpx.ConnectError])
async def test_off_timeouts_and_network_errors_raise(exc):
    def handler(req):
        raise exc("down", request=req)

    async with off_client(handler) as http:
        with pytest.raises(exc):
            await pr.off_product(CODE, http)


@pytest.mark.parametrize("status", [429, 500, 503])
async def test_off_server_errors_raise(status):
    async with off_answer({"error": "busy"}, status=status) as http:
        with pytest.raises(httpx.HTTPStatusError):
            await pr.off_product(CODE, http)


# ---- label validation ----


def per100(kcal, p, f, c) -> LabelPer100:
    return LabelPer100(kcal=kcal, protein_g=p, fat_g=f, carbs_g=c)


@pytest.mark.parametrize(
    "numbers",
    [
        (360, 33, 12, 30),  # exact: 4*33 + 9*12 + 4*30 = 360
        (330, 33, 12, 30),  # 8% under: fiber and polyols are not in the macros
        (400, 33, 12, 30),  # 10% over
        (300, 33, 12, 30),  # 17% under
        (0, 0, 0, 0),  # water
        (884, 0, 100, 0),  # oil
        (41, 0.6, 0, 9.5),  # a small number with rounding
    ],
)
def test_check_label_accepts_consistent_numbers(numbers):
    assert pr.check_label(per100(*numbers))


@pytest.mark.parametrize(
    ("numbers", "why"),
    [
        ((460, 33, 12, 30), "kcal 28% above the macros"),
        ((250, 33, 12, 30), "kcal 31% under the macros"),
        ((360, 33, 12, None), "carbs missing"),
        ((None, 33, 12, 30), "kcal missing"),
        ((360, None, None, None), "only kcal"),
        ((1000, 0, 111, 0), "kcal above 950"),
        ((500, 60, 40, 20), "macros sum above 105 g"),
        ((360, 33, 12, 30.0 + 80), "macros sum above 105 g with consistent-looking kcal"),
    ],
)
def test_check_label_rejects_inconsistent_or_incomplete_numbers(numbers, why):
    assert not pr.check_label(per100(*numbers)), why


def test_check_label_empty_label_is_not_ok():
    assert not pr.check_label(LabelPer100())


def test_check_label_tolerance_is_20_percent_of_the_larger_side():
    # macros say 360: 20% of 360 is 72, so 290 (off by 70) passes and 280 (off by 80) does not
    assert pr.check_label(per100(290, 33, 12, 30))
    assert not pr.check_label(per100(280, 33, 12, 30))


# ---- OFF vs label ----


def off_product_info(**kw) -> ProductInfo:
    base = {"name": "Протеиновый батончик", "brand": "Bombbar", "kcal": 360, "protein_g": 33, "fat_g": 12,
            "carbs_g": 30, "net_weight_g": 60, "barcode": CODE, "source": "off"}
    return ProductInfo(**{**base, **kw})


def test_combine_off_and_agreeing_label_gives_off_without_a_note():
    d = pr.combine(off_product_info(), label(kcal=365), CODE)
    assert d.product is not None and d.product.source == "off" and d.product.kcal == 360
    assert d.note is None and d.problem is None


def test_combine_off_without_a_label_is_just_off():
    d = pr.combine(off_product_info(), None, CODE)
    assert d.product is not None and d.product.source == "off" and d.note is None


def test_combine_18_percent_difference_adds_a_note_and_keeps_off():
    # OFF says 360 kcal, the label 425: 18% above OFF
    lab = label(kcal=425, p=30, f=15, c=40)  # macros: 120 + 135 + 160 = 415, so the label itself passes
    assert pr.check_label(lab.per100)
    d = pr.combine(off_product_info(), lab, CODE)
    assert d.product is not None and d.product.source == "off" and d.product.kcal == 360  # OFF numbers are used
    assert d.note is not None
    assert "425" in d.note and "360" in d.note and "Open Food Facts" in d.note


def test_combine_difference_just_under_the_threshold_has_no_note():
    # 14% of 360 = 50.4: 410 differs by 50. The label (410 vs macros 4*30+9*13+4*40 = 397) is consistent.
    lab = label(kcal=410, p=30, f=13, c=40)
    assert pr.check_label(lab.per100)
    d = pr.combine(off_product_info(), lab, CODE)
    assert d.note is None and d.product is not None and d.product.kcal == 360


def test_combine_difference_in_the_other_direction_also_adds_a_note():
    lab = label(kcal=295, p=25, f=12, c=20)  # macros 100 + 108 + 80 = 288: consistent; 18% under OFF's 360
    assert pr.check_label(lab.per100)
    d = pr.combine(off_product_info(), lab, CODE)
    assert d.note is not None and "295" in d.note


def test_combine_unclear_label_next_to_good_off_uses_off_without_a_note():
    bad = label(kcal=900, p=33, f=12, c=30)
    assert not pr.check_label(bad.per100)
    d = pr.combine(off_product_info(), bad, CODE)
    assert d.product is not None and d.product.source == "off"
    assert d.note is None and d.problem is None


def test_combine_off_fills_missing_weights_from_the_label():
    off = off_product_info(net_weight_g=None, serving_g=None)
    d = pr.combine(off, label(net=75, serving=25), CODE)
    assert d.product is not None
    assert (d.product.net_weight_g, d.product.serving_g) == (75, 25)


def test_combine_off_weights_win_over_the_label_ones():
    off = off_product_info(net_weight_g=60, serving_g=30)
    d = pr.combine(off, label(net=75, serving=25), CODE)
    assert d.product is not None
    assert (d.product.net_weight_g, d.product.serving_g) == (60, 30)


def test_combine_label_serving_as_big_as_the_pack_is_dropped():
    d = pr.combine(off_product_info(net_weight_g=None), label(net=60, serving=60), CODE)
    assert d.product is not None and d.product.serving_g is None


def test_combine_label_only_makes_a_label_product():
    d = pr.combine(None, label(), CODE)
    p = d.product
    assert p is not None and p.source == "label" and p.barcode == CODE
    assert (p.name, p.brand, p.kcal, p.net_weight_g) == ("Протеиновый батончик", "Bombbar", 360, 60)
    assert d.note is None


def test_combine_label_without_a_name_is_called_a_label_product():
    d = pr.combine(None, label(name=None, brand=None), None)
    assert d.product is not None and d.product.name == pr.LABEL_NAME and d.product.barcode is None


def test_combine_unclear_label_without_off_asks_for_another_photo():
    d = pr.combine(None, label(kcal=900), None)
    assert d.product is None and d.problem == pr.LABEL_UNCLEAR and d.note is None


def test_combine_incomplete_label_without_off_asks_for_another_photo():
    d = pr.combine(None, ParsedLabel(name="Что-то", per100=LabelPer100(kcal=300)), CODE)
    assert d.problem == pr.LABEL_UNCLEAR


def test_combine_off_without_numbers_and_no_label_asks_for_the_table():
    off = off_product_info(kcal=None, protein_g=None, fat_g=None, carbs_g=None)
    d = pr.combine(off, None, CODE)
    assert d.product is None and d.problem == pr.NO_NUMBERS.format(name="Bombbar Протеиновый батончик")


def test_combine_off_without_numbers_and_a_good_label_takes_the_numbers_from_the_label():
    off = off_product_info(name="Батончик OFF", brand="BrandOFF", kcal=None, protein_g=None, fat_g=None,
                           carbs_g=None, net_weight_g=70)
    d = pr.combine(off, label(name=None, brand=None, net=None), CODE)
    p = d.product
    assert p is not None and p.source == "label"
    assert (p.kcal, p.protein_g, p.fat_g, p.carbs_g) == (360, 33, 12, 30)
    assert (p.name, p.brand, p.net_weight_g) == ("Батончик OFF", "BrandOFF", 70)  # OFF knows the name and weight


def test_combine_off_without_numbers_and_a_named_label_prefers_the_label_name():
    off = off_product_info(name="Батончик OFF", kcal=None, protein_g=None, fat_g=None, carbs_g=None)
    d = pr.combine(off, label(), CODE)
    assert d.product is not None and d.product.name == "Протеиновый батончик" and d.product.brand == "Bombbar"


def test_combine_off_without_numbers_and_an_unclear_label_asks_again():
    off = off_product_info(kcal=None, protein_g=None, fat_g=None, carbs_g=None)
    assert pr.combine(off, label(kcal=900), CODE).problem == pr.LABEL_UNCLEAR


def test_combine_nothing_is_an_empty_decision_a_plate():
    d = pr.combine(None, None, None)
    assert d == pr.Decision()
    assert d.product is None and d.problem is None and d.note is None


# ---- amounts ----


@pytest.mark.parametrize(
    ("text_", "amount", "rest"),
    [
        ("50 г", Amount(grams=50), []),
        ("50г", Amount(grams=50), []),
        ("50 gr", Amount(grams=50), []),
        ("50g", Amount(grams=50), []),
        ("62,5 грамма", Amount(grams=62.5), []),
        ("200 мл", Amount(grams=200), []),
        ("60", Amount(bare=60), []),
        ("2", Amount(bare=2), []),
        ("2 порции", Amount(servings=2), []),
        ("1 скуп", Amount(servings=1), []),
        ("три скупа", Amount(servings=3), []),
        ("порция", Amount(servings=1), []),
        ("пол порции", Amount(servings=0.5), []),
        ("полторы порции", Amount(servings=1.5), []),
        ("половина", Amount(packages=0.5), []),
        ("половину", Amount(packages=0.5), []),
        ("½", Amount(packages=0.5), []),
        ("вся пачка", Amount(packages=1), []),
        ("всю упаковку", Amount(packages=1), []),
        ("целиком", Amount(packages=1), []),
        ("1.5 пачки", Amount(packages=1.5), []),
        ("2 батончика", Amount(packages=2), []),
        ("съел тот же батончик", Amount(packages=1), []),
        ("запиши mars 50 г", Amount(grams=50), ["mars"]),
        ("Mars 2", Amount(bare=2), ["mars"]),
        ("кофе", Amount(), ["кофе"]),
        ("", Amount(), []),
    ],
)
def test_parse_amount(text_, amount, rest):
    got, left = pr.parse_amount(text_)
    assert got == amount
    assert left == rest


def test_parse_amount_empty_property():
    assert pr.parse_amount("привет")[0].empty
    assert not pr.parse_amount("60")[0].empty


def test_parse_amount_first_unit_wins():
    assert pr.parse_amount("50 г и 100 г")[0].grams == 50


def test_grams_for_every_unit():
    p = prod("X", net=60, serving=30)
    assert pr.grams_for(p, Amount(grams=45)) == 45
    assert pr.grams_for(p, Amount(servings=2)) == 60
    assert pr.grams_for(p, Amount(packages=0.5)) == 30
    assert pr.grams_for(p, Amount(packages=1)) == 60
    assert pr.grams_for(p, Amount(bare=80)) == 80  # 15 g and more: grams
    assert pr.grams_for(p, Amount(bare=15)) == 15
    assert pr.grams_for(p, Amount(bare=2)) == 60  # below 15: servings first
    assert pr.grams_for(p, Amount()) is None


def test_grams_for_needs_the_weight_it_converts_with():
    no_serving = prod("X", net=60)
    no_pack = prod("X", serving=30)
    nothing = prod("X")
    assert pr.grams_for(no_serving, Amount(servings=1)) is None
    assert pr.grams_for(no_pack, Amount(packages=1)) is None
    assert pr.grams_for(no_pack, Amount(bare=2)) == 60  # no pack: a serving
    assert pr.grams_for(nothing, Amount(bare=2)) is None
    assert pr.grams_for(nothing, Amount(bare=60)) == 60
    assert pr.grams_for(nothing, Amount(grams=60)) == 60


def test_food_for_scales_the_label_and_does_not_reestimate():
    food = pr.food_for(BAR, 60)
    assert food.description == "Bombbar Протеиновый батончик, 60 г"
    assert (food.grams, food.kcal, food.protein_g, food.fat_g, food.carbs_g) == (60, 216, 19.8, 7.2, 18)


def test_food_for_keeps_a_kcal_that_differs_from_4_9_4():
    fiber = prod("Хлебцы", None, 300, 20, 10, 40)  # macros say 330; the label is what was printed
    assert pr.food_for(fiber, 50).kcal == 150


# ---- matching a message against saved products ----


def names(found: pr.Match | None) -> list[str] | None:
    return None if found is None else [p.name for p in found.products]


@pytest.mark.parametrize(
    ("message_", "amount"),
    [
        ("съел тот же батончик", Amount(packages=1)),
        ("съела такой же батончик", Amount(packages=1)),
        ("Съел тот же батончик!", Amount(packages=1)),
        ("батончик", Amount(packages=1)),
        ("батончика 30 г", Amount(grams=30, packages=1)),  # grams_for prefers the grams
        ("bombbar", Amount()),
        ("Bombbar 60 г", Amount(grams=60)),
        ("половину батончика", Amount(packages=0.5)),
    ],
)
def test_match_saved_bar(message_, amount):
    found = pr.match(message_, [BAR])
    assert names(found) == ["Протеиновый батончик"]
    assert found is not None and found.amount == amount


def test_match_same_alone_with_several_products_asks_which():
    found = pr.match("тот же", [MARS, BAR])
    assert names(found) == ["Mars", "Протеиновый батончик"]  # candidates, newest first


def test_match_same_alone_with_one_product_takes_it():
    assert names(pr.match("тот же", [MARS])) == ["Mars"]


@pytest.mark.parametrize("message_", ["то же самое", "тот же плов"])
def test_match_same_with_foreign_words_goes_to_the_parser(message_):
    assert pr.match(message_, [MARS, BAR]) is None


def test_match_mars_with_grams():
    found = pr.match("mars 50 г", [BAR, MARS])
    assert names(found) == ["Mars"]
    assert found is not None and found.amount == Amount(grams=50)


@pytest.mark.parametrize("message_", ["MARS 50 г", "mars 50g", "съел mars, 50 г", "50 грамм mars"])
def test_match_ignores_case_punctuation_and_attached_units(message_):
    found = pr.match(message_, [BAR, MARS])
    assert names(found) == ["Mars"]
    assert found is not None and found.amount.grams == 50


def test_match_whole_pack():
    found = pr.match("вся пачка mars", [MARS])
    assert names(found) == ["Mars"]
    assert found is not None and found.amount == Amount(packages=1)
    assert pr.grams_for(MARS, found.amount) == 51


def test_match_bare_two_is_two_packs():
    found = pr.match("mars 2", [MARS])
    assert found is not None and found.amount == Amount(bare=2)
    assert pr.grams_for(MARS, found.amount) == 102


def test_match_half():
    found = pr.match("половина mars", [MARS])
    assert found is not None and found.amount == Amount(packages=0.5)
    assert pr.grams_for(MARS, found.amount) == 25.5


def test_match_one_scoop_of_two_proteins_asks_which():
    found = pr.match("протеин 1 скуп", [WHEY, CHOCO])
    assert names(found) == ["Whey протеин", "Протеин шоколад"]  # newest first
    assert found is not None and found.amount == Amount(servings=1)


def test_match_same_protein_takes_the_newest():
    found = pr.match("тот же протеин", [CHOCO, WHEY])
    assert names(found) == ["Протеин шоколад"]
    found = pr.match("тот же протеин", [WHEY, CHOCO])
    assert names(found) == ["Whey протеин"]


def test_match_a_more_specific_name_picks_one_of_two_proteins():
    found = pr.match("whey 1 скуп", [CHOCO, WHEY])
    assert names(found) == ["Whey протеин"]
    found = pr.match("протеин шоколад 30 г", [WHEY, CHOCO])
    assert names(found) == ["Протеин шоколад"]


def test_match_brand_and_alias_name_the_product():
    mine = prod("Whey", "Optimum Nutrition", 380, 75, 6, 8, serving=30, aliases="мой протеин, on")
    assert names(pr.match("мой протеин 1 скуп", [mine])) == ["Whey"]
    assert names(pr.match("optimum 1 скуп", [mine])) == ["Whey"]
    assert pr.match("on 1 скуп", [mine]) is None  # a two-letter alias is too short to be a name


@pytest.mark.parametrize(
    "message_",
    [
        "кофе с молоком 200 мл",
        "mars и bombbar",
        "bombbar и mars",
        "mars 50 г и чай",
        "жим 80 на 8",
        "жим лёжа 3 по 10",
        "плов 300 г",
        "привет",
        "",
        "марс 50 г",  # Cyrillic spelling of a Latin name: not guessed
    ],
)
def test_match_unrelated_or_multi_food_messages_go_to_the_parser(message_):
    assert pr.match(message_, [MARS, BAR]) is None


def test_match_a_word_that_is_only_the_start_of_another_does_not_match():
    assert pr.match("батон 50 г", [BAR]) is None  # "батон" is not "батончик"
    assert pr.match("батончик 50 г", [LOAF]) is None
    assert names(pr.match("батон 50 г", [LOAF])) == ["Батон нарезной"]
    assert names(pr.match("батона 50 г", [LOAF])) == ["Батон нарезной"]


def test_match_a_loaf_and_a_bar_are_told_apart():
    found = pr.match("батон 50 г", [BAR, LOAF])
    assert names(found) == ["Батон нарезной"]
    found = pr.match("батончик 50 г", [BAR, LOAF])
    assert names(found) == ["Протеиновый батончик"]


def test_match_eat_a_bar_with_no_saved_products_goes_to_the_parser():
    assert pr.match("съел батончик", []) is None
    assert pr.match("тот же", []) is None
    assert pr.match("mars 50 г", []) is None


def test_match_bar_with_only_unrelated_products_goes_to_the_parser():
    assert pr.match("съел батончик", [WHEY]) is None
    assert pr.match("съел батончик", [LOAF]) is None


def test_match_numbers_in_a_name_do_not_name_the_product():
    cola = prod("Cola 0.5", "Coca", 42, 0, 0, 10.6)
    assert pr.match("0.5", [cola]) is None
    assert names(pr.match("cola 0.5", [cola])) == ["Cola 0.5"]


def test_mentioned_and_prompt_line_only_for_named_products():
    assert pr.mentioned("жим 80 на 8", [MARS, BAR]) == []
    assert pr.mentioned("съел mars и кофе", [MARS, BAR]) == [MARS]
    assert pr.prompt_line("жим 80 на 8", [MARS, BAR]) == ""
    line = pr.prompt_line("mars и кофе", [MARS, BAR])
    assert line == "Мои продукты (точные числа с упаковки): Mars на 100 г 450 ккал Б4 Ж17 У70, упаковка 51 г"
    assert "Bombbar" not in line


def test_prompt_line_names_several_products_in_one_line_and_stays_short():
    line = pr.prompt_line("mars и bombbar", [MARS, BAR])
    assert line.startswith("Мои продукты (точные числа с упаковки): Mars")
    assert "; Bombbar Протеиновый батончик на 100 г 360 ккал Б33 Ж12 У30, упаковка 60 г" in line

    many = [prod(f"Батончик номер{i}", "Бренд" * 5, 360, 33, 12, 30, net=60) for i in range(20)]
    long_line = pr.prompt_line("батончик", many)
    assert 0 < len(long_line) <= pr.PROMPT_LINE_MAX


def test_title_does_not_repeat_the_brand():
    assert prod("Mars", "Mars").title == "Mars"
    assert prod("Snickers Protein", "snickers").title == "Snickers Protein"
    assert prod("Протеиновый батончик", "Bombbar").title == "Bombbar Протеиновый батончик"
    assert prod("Хлеб", None).title == "Хлеб"


@pytest.mark.parametrize(
    ("a", "b", "same"),
    [
        ("батончик", "батончика", True),
        ("протеин", "протеином", True),
        ("батон", "батончик", False),  # "батон" is five letters but the tail is three
        ("кот", "котлета", False),
        ("mars", "mars", True),
        ("mars", "marsh", True),
        ("mar", "mars", False),  # too short a stem
    ],
)
def test_same_word(a, b, same):
    assert pr.same_word(a, b) is same
    assert pr.same_word(b, a) is same


# ---- the user's products in the database ----


async def make_user(db, telegram_id=42):
    async with db() as session:
        user = await get_or_create_user(session, telegram_id, "Amir")
        await session.commit()
        return user.id


async def test_remember_inserts_a_new_product(db):
    uid = await make_user(db)
    async with db() as session:
        row = await pr.remember(session, uid, off_product_info(serving_g=30))
        await session.commit()
        assert row.id is not None
    async with db() as session:
        (got,) = await session.scalars(select(Product))
    assert (got.user_id, got.name, got.brand, got.barcode, got.source) == (uid, "Протеиновый батончик", "Bombbar", CODE, "off")
    assert (got.kcal_100g, got.protein_100g, got.fat_100g, got.carbs_100g) == (
        Decimal("360.0"), Decimal("33.0"), Decimal("12.0"), Decimal("30.0"))
    assert (got.net_weight_g, got.serving_g) == (Decimal("60.0"), Decimal("30.0"))
    assert got.aliases is None and got.created_at is not None and got.updated_at is not None


async def test_remember_by_barcode_refreshes_numbers_instead_of_adding_a_row(db):
    uid = await make_user(db)
    async with db() as session:
        await pr.remember(session, uid, off_product_info())
        await pr.remember(session, uid, off_product_info(name="Батончик (новый рецепт)", kcal=380, protein_g=30))
        await session.commit()
        rows = list(await session.scalars(select(Product)))
    assert len(rows) == 1
    assert rows[0].name == "Батончик (новый рецепт)" and rows[0].kcal_100g == Decimal("380.0")


async def test_remember_by_brand_and_name_ignores_case_when_there_is_no_barcode(db):
    uid = await make_user(db)
    async with db() as session:
        await pr.remember(session, uid, prod("Whey", "Optimum", barcode=None, source="label"))
        await pr.remember(session, uid, prod("WHEY", "optimum", 400, 80, 5, 5, barcode=None, source="label"))
        await pr.remember(session, uid, prod("Whey", "Other brand", barcode=None, source="label"))
        await session.commit()
        rows = list(await session.scalars(select(Product).order_by(Product.id)))
    assert [(r.name, r.brand) for r in rows] == [("WHEY", "optimum"), ("Whey", "Other brand")]
    assert rows[0].kcal_100g == Decimal("400.0")


async def test_remember_keeps_label_products_without_a_name_apart(db):
    uid = await make_user(db)
    async with db() as session:
        await pr.remember(session, uid, prod(pr.LABEL_NAME, None, 100, 5, 2, 15, source="label"))
        await pr.remember(session, uid, prod(pr.LABEL_NAME, None, 200, 10, 5, 25, source="label"))
        await session.commit()
        rows = list(await session.scalars(select(Product)))
    assert len(rows) == 2  # two different unnamed packages are not one product


async def test_remember_a_saved_product_keeps_its_own_numbers_and_moves_to_the_top(db):
    uid = await make_user(db)
    async with db() as session:
        first = await pr.remember(session, uid, prod("Mars", "Mars", 450, 4, 17, 70, net=51, barcode="111", source="off"))
        await pr.remember(session, uid, prod("Snickers", "Mars", 480, 9, 24, 60, net=50, barcode="222", source="off"))
        await session.commit()
        before = first.updated_at
    async with db() as session:
        saved = await pr.user_products(session, 42)
        assert [p.name for p in saved] == ["Snickers", "Mars"]  # last eaten first
        mars = next(p for p in saved if p.name == "Mars")
        assert mars.product_id is not None
        # Eating it again from memory: a different "kcal" in the passed info must not rewrite the row.
        await pr.remember(session, uid, ProductInfo(**{**mars.__dict__, "kcal": 1.0}))
        await session.commit()
    async with db() as session:
        saved = await pr.user_products(session, 42)
        assert [p.name for p in saved] == ["Mars", "Snickers"]  # "тот же" now means Mars
        assert saved[0].kcal == 450
        row = await session.scalar(select(Product).where(Product.name == "Mars"))
        assert row is not None and row.updated_at.replace(tzinfo=None) > before.replace(tzinfo=None)


async def test_remember_alias_is_added_once_and_ignores_case(db):
    uid = await make_user(db)
    async with db() as session:
        await pr.remember(session, uid, off_product_info(), alias="мой батончик")
        await pr.remember(session, uid, off_product_info(), alias="Мой батончик")
        await pr.remember(session, uid, off_product_info(), alias="бомбар")
        await pr.remember(session, uid, off_product_info())
        await session.commit()
        (row,) = await session.scalars(select(Product))
    assert row.aliases == "мой батончик, бомбар"
    saved = pr.info(row)
    assert saved.aliases == "мой батончик, бомбар"
    assert names(pr.match("мой батончик", [saved])) == ["Протеиновый батончик"]


async def test_remember_does_not_touch_another_users_product(db):
    mine = await make_user(db, 42)
    other = await make_user(db, 43)
    async with db() as session:
        theirs = await pr.remember(session, other, off_product_info(name="Чужой"))
        await session.commit()
        info_of_theirs = pr.info(theirs)
    async with db() as session:
        # My save with the other user's product id (a stale or forged card) makes my own row.
        await pr.remember(session, mine, info_of_theirs)
        await session.commit()
        rows = list(await session.scalars(select(Product).order_by(Product.id)))
    assert [(r.user_id, r.name) for r in rows] == [(other, "Чужой"), (mine, "Чужой")]
    async with db() as session:
        assert len(await pr.user_products(session, 42)) == 1
        assert len(await pr.user_products(session, 43)) == 1


async def test_remember_the_same_barcode_for_two_users_is_fine(db):
    a = await make_user(db, 42)
    b = await make_user(db, 43)
    async with db() as session:
        await pr.remember(session, a, off_product_info())
        await pr.remember(session, b, off_product_info())
        await session.commit()
        assert len(list(await session.scalars(select(Product)))) == 2


async def test_user_products_is_limited_and_newest_first(db):
    uid = await make_user(db)
    async with db() as session:
        for i in range(5):
            await pr.remember(session, uid, prod(f"P{i}", None, barcode=f"90{i}", source="off"))
        await session.commit()
        got = await pr.user_products(session, 42, limit=3)
    assert [p.name for p in got] == ["P4", "P3", "P2"]


async def test_user_products_of_an_unknown_user_is_empty(db):
    async with db() as session:
        assert await pr.user_products(session, 999) == []


async def test_delete_removes_only_my_product(db):
    mine = await make_user(db, 42)
    other = await make_user(db, 43)
    async with db() as session:
        a = await pr.remember(session, mine, off_product_info(barcode="1"))
        b = await pr.remember(session, other, off_product_info(barcode="2"))
        await session.commit()
        a_id, b_id = a.id, b.id
    async with db() as session:
        assert await pr.delete(session, 42, b_id) is False  # not mine
        assert await pr.delete(session, 42, 12345) is False
        assert await pr.delete(session, 42, a_id) is True
        await session.commit()
    async with db() as session:
        left = list(await session.scalars(select(Product)))
    assert [r.id for r in left] == [b_id]


async def test_info_round_trips_a_row(db):
    uid = await make_user(db)
    async with db() as session:
        row = await pr.remember(session, uid, off_product_info(serving_g=30))
        await session.commit()
        p = pr.info(row)
    assert p.product_id == row.id
    assert (p.name, p.brand, p.kcal, p.protein_g, p.fat_g, p.carbs_g) == ("Протеиновый батончик", "Bombbar", 360, 33, 12, 30)
    assert (p.net_weight_g, p.serving_g, p.barcode, p.source) == (60, 30, CODE, "off")
    assert p.complete


# ---- migration 0012 ----


def _schema(conn) -> dict:
    insp = inspect(conn)
    out: dict = {"tables": set(insp.get_table_names())}
    if "products" in out["tables"]:
        out["columns"] = {c["name"] for c in insp.get_columns("products")}
        out["unique"] = [sorted(u["column_names"]) for u in insp.get_unique_constraints("products")]
        out["indexes"] = [(i["name"], i["column_names"]) for i in insp.get_indexes("products")]
        out["fks"] = [(f["constrained_columns"], f["referred_table"]) for f in insp.get_foreign_keys("products")]
    return out


async def test_migration_0012_round_trip(tmp_path):
    engine, _ = make_engine(f"sqlite+aiosqlite:///{tmp_path}/m.db")

    async def run_sync(fn):
        async with engine.begin() as conn:
            return await conn.run_sync(fn)

    async def migrate_to(target: str):
        def fn(conn):
            cfg = migrate._config()
            cfg.attributes["connection"] = conn
            (command.upgrade if target == "head" else command.downgrade)(cfg, target)

        await run_sync(fn)

    insert = (
        "INSERT INTO products (user_id, name, barcode, kcal_100g, protein_100g, fat_100g, carbs_100g, source, "
        "created_at, updated_at) VALUES (1, 'Mars', :barcode, 450, 4, 17, 70, 'off', "
        "'2026-10-08 12:00:00', '2026-10-08 12:00:00')"
    )
    try:
        await migrate_to("head")
        up = await run_sync(_schema)
        assert "products" in up["tables"] and "body_weights" in up["tables"]
        assert up["columns"] == {
            "id", "user_id", "name", "brand", "barcode", "kcal_100g", "protein_100g", "fat_100g", "carbs_100g",
            "net_weight_g", "serving_g", "source", "aliases", "created_at", "updated_at",
        }
        assert up["unique"] == [["barcode", "user_id"]]
        assert ("ix_products_user_id", ["user_id"]) in up["indexes"]
        assert up["fks"] == [(["user_id"], "users")]

        async with engine.begin() as conn:
            await conn.execute(text("INSERT INTO users (telegram_id, rest_seconds, created_at) VALUES (1, 90, '2026-10-01 12:00:00')"))
            await conn.execute(text(insert), {"barcode": CODE})
            with pytest.raises(Exception, match="UNIQUE"):  # one row per (user, barcode)
                await conn.execute(text(insert), {"barcode": CODE})
        async with engine.begin() as conn:
            await conn.execute(text(insert), {"barcode": None})  # label products have no barcode
            await conn.execute(text(insert), {"barcode": None})  # and several of them may exist

        await migrate_to("0011")
        down = await run_sync(_schema)
        assert "products" not in down["tables"]
        assert "body_weights" in down["tables"]  # the previous revision is intact
        async with engine.connect() as conn:
            assert (await conn.execute(text("SELECT telegram_id FROM users"))).scalar_one() == 1

        await migrate_to("head")
        assert await run_sync(_schema) == up
        async with engine.connect() as conn:
            assert (await conn.execute(text("SELECT count(*) FROM products"))).scalar_one() == 0
    finally:
        await engine.dispose()



# ---- source bugs found while writing these tests (strict xfail: they flip to errors once fixed) ----


@pytest.mark.parametrize("message_", ["съел батончик, половину", "батончик половина"])
def test_match_package_word_before_the_fraction_still_means_half(message_):
    found = pr.match(message_, [BAR])
    assert found is not None and found.amount.packages == 0.5


def test_parsed_label_keeps_a_per100_instance():
    lab = ParsedLabel(per100=LabelPer100(kcal=360, protein_g=33, fat_g=12, carbs_g=30))
    assert lab.per100.kcal == 360


@pytest.mark.parametrize(("factor", "ok"), [(1.25, False), (0.75, False), (1.15, True), (0.85, True), (1.0, True)])
def test_check_label_tolerance_is_symmetric(factor, ok):
    from gymbot.llm.schemas import LabelPer100
    from gymbot.services.products import check_label

    computed = 4 * 20 + 9 * 10 + 4 * 32  # 298
    assert check_label(LabelPer100(kcal=round(computed * factor, 1), protein_g=20, fat_g=10, carbs_g=32)) is ok


# ---- review fixes ----

PIZZA = prod("Пицца Маргарита", net=400)
BREAST = prod("Куриная грудка", net=500)
MILK = prod("Молоко", net=930)
@pytest.mark.parametrize("message_", ["тот же батончик", "съел тот же батончик", "такой же батончик"])
def test_same_bar_without_a_saved_bar_goes_to_the_parser(message_):
    assert pr.match(message_, [PIZZA, MILK, BREAST, LOAF]) is None


def test_same_bar_with_a_saved_bar_still_matches():
    bar = prod("Протеиновый батончик", net=60)
    found = pr.match("съел тот же батончик", [PIZZA, bar, MILK])
    assert names(found) == ["Протеиновый батончик"]
    assert found is not None and pr.grams_for(bar, found.amount) == 60


def test_bare_small_number_prefers_the_serving():
    tub = prod("Протеин", net=900, serving=30)
    assert pr.grams_for(tub, Amount(bare=2)) == 60


def test_bare_small_number_of_a_big_pack_without_serving_asks():
    assert pr.grams_for(prod("Протеин", net=900), Amount(bare=2)) is None
    assert pr.grams_for(prod("Mars", net=51), Amount(bare=2)) == 102  # a small pack: packages


def test_time_is_not_grams():
    bar = prod("Протеиновый батончик", net=60)
    found = pr.match("батончик в 15:00", [bar])
    assert found is not None and found.amount.grams is None and found.amount.bare is None
    assert pr.grams_for(bar, found.amount) == 60
    assert pr.parse_amount("в 9:30 50 г")[0] == Amount(grams=50)


@pytest.mark.parametrize("message_", ["батончик?", "mars?", "сколько в mars?"])
def test_questions_never_match(message_):
    assert pr.match(message_, [prod("Протеиновый батончик", net=60), MARS]) is None


@pytest.mark.parametrize(
    ("text", "name"),
    [("пиццы 200 г", "Пицца Маргарита"), ("пиццу 150 г", "Пицца Маргарита"), ("грудки 150 г", "Куриная грудка")],
)
def test_word_forms_match(text, name):
    assert names(pr.match(text, [PIZZA, BREAST])) == [name]


@pytest.mark.parametrize(("a", "b", "same"), [
    ("пицца", "пиццы", True), ("грудка", "грудки", True), ("батончик", "батончика", True),
    ("протеин", "протеином", True), ("батон", "батончик", False), ("банан", "банка", False),
])  # fmt: skip
def test_same_word_stems(a, b, same):
    assert pr.same_word(a, b) is same


def test_mentioned_uses_word_forms():
    assert [p.name for p in pr.mentioned("кусок пиццы и чай", [PIZZA, BREAST])] == ["Пицца Маргарита"]
