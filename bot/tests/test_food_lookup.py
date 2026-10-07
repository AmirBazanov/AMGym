"""Unknown dish lookup: sources (Open Food Facts, Wikipedia, Tavily), variants from the model, amounts."""

import json
import logging

import httpx
import pytest

from gymbot.llm.openrouter import LLMError
from gymbot.services import food_lookup
from gymbot.services.food_lookup import Amount, Macros, Option, Source

MODULE_LOGGER = "gymbot.services.food_lookup"
OFF_NUTRIMENTS = {
    "energy-kcal_100g": "380",
    "proteins_100g": "30",
    "fat_100g": "10.5",
    "carbohydrates_100g": "5",
}


@pytest.fixture(autouse=True)
def clean_cache():
    food_lookup._CACHE.clear()
    yield
    food_lookup._CACHE.clear()


class Net:
    """MockTransport router by host and `action`; each slot is a JSON value, an int status or an exception."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.classic: object = {"products": []}
        self.search: object = {"hits": []}
        self.wiki_search: object = ["x", [], [], []]
        self.wiki_query: object = {"query": {"pages": {}}}
        self.tavily: object = {"results": []}
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(self._handle))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host, params = request.url.host, request.url.params
        if host == "world.openfoodfacts.org":
            slot = self.classic
        elif host == "search.openfoodfacts.org":
            slot = self.search
        elif host == "ru.wikipedia.org":
            slot = self.wiki_search if params.get("action") == "opensearch" else self.wiki_query
        elif host == "api.tavily.com":
            slot = self.tavily
        else:
            raise AssertionError(f"unexpected host {host}")
        if isinstance(slot, Exception):
            raise slot
        if isinstance(slot, int):
            return httpx.Response(slot, request=request)
        return httpx.Response(200, json=slot, request=request)

    def hosts(self) -> list[str]:
        return [r.url.host for r in self.requests]

    def down(self, *slots: str) -> None:
        for slot in slots:
            setattr(self, slot, httpx.ConnectError("boom"))


@pytest.fixture
async def net():
    n = Net()
    yield n
    await n.http.aclose()


def product(code: str, name: str, nutriments: dict | None = None, quantity: str = "") -> dict:
    return {"code": code, "product_name": name, "quantity": quantity, "nutriments": nutriments}


# ---- Open Food Facts ----


async def test_off_classic_parses_products_and_strings(net):
    net.classic = {
        "products": [
            product("111", "Курт сушёный", OFF_NUTRIMENTS, "100 г"),
            product("222", "Курт солёный", {"energy-kcal_100g": 300}),
        ]
    }
    sources = await food_lookup.lookup("курт", net.http)
    assert [s.title for s in sources] == ["Open Food Facts: Курт сушёный", "Open Food Facts: Курт солёный"]
    assert sources[0].per100 == Macros(kcal=380, protein_g=30, fat_g=10.5, carbs_g=5)
    assert sources[0].snippet == "упаковка 100 г"
    assert sources[0].url == "https://world.openfoodfacts.org/product/111"
    assert sources[1].per100 is None  # three of four numbers are missing: no macros, not zeros
    assert sources[1].snippet == ""


async def test_off_search_service_hits(net):
    net.search = {"hits": [product("333", "Курт творожный", OFF_NUTRIMENTS)]}
    sources = await food_lookup.lookup("курт", net.http)
    assert [s.title for s in sources] == ["Open Food Facts: Курт творожный"]
    assert sources[0].per100 is not None and sources[0].per100.kcal == 380
    assert sources[0].url.endswith("/333")
    assert "search.openfoodfacts.org" in net.hosts()


@pytest.mark.parametrize("missing", ["energy-kcal_100g", "proteins_100g", "fat_100g", "carbohydrates_100g"])
async def test_off_per100_needs_all_four_numbers(net, missing):
    nutriments = {k: v for k, v in OFF_NUTRIMENTS.items() if k != missing}
    net.classic = {"products": [product("1", "Курт", nutriments)]}
    (source,) = await food_lookup.lookup("курт", net.http)
    assert source.per100 is None


async def test_off_zero_is_a_number_and_negative_is_not(net):
    zero = {**OFF_NUTRIMENTS, "carbohydrates_100g": 0}
    negative = {**OFF_NUTRIMENTS, "carbohydrates_100g": "-1"}
    net.classic = {"products": [product("1", "Курт А", zero), product("2", "Курт Б", negative)]}
    a, b = await food_lookup.lookup("курт", net.http)
    assert a.per100 is not None and a.per100.carbs_g == 0
    assert b.per100 is None


async def test_off_skips_products_without_the_term(net):
    net.classic = {
        "products": [
            product("1", "Жевательная мармелад", OFF_NUTRIMENTS),
            product("2", "", OFF_NUTRIMENTS),
            product("3", "Курт", OFF_NUTRIMENTS),
        ]
    }
    sources = await food_lookup.lookup("курт", net.http)
    assert [s.title for s in sources] == ["Open Food Facts: Курт"]


async def test_off_stem_matches_inflected_names(net):
    net.classic = {"products": [product("1", "Косушки солёные", OFF_NUTRIMENTS)]}
    sources = await food_lookup.lookup("косушка", net.http)
    assert [s.title for s in sources] == ["Open Food Facts: Косушки солёные"]


async def test_same_product_under_two_barcodes_appears_once(net):
    net.classic = {
        "products": [product("111", "Курт сушёный", OFF_NUTRIMENTS), product("112", "Курт сушёный", OFF_NUTRIMENTS)]
    }
    sources = await food_lookup.lookup("курт", net.http)
    assert len(sources) == 1 and sources[0].url.endswith("/111")


async def test_same_name_with_different_macros_is_kept(net):
    other = {**OFF_NUTRIMENTS, "energy-kcal_100g": "250"}
    net.classic = {"products": [product("1", "Курт", OFF_NUTRIMENTS), product("2", "Курт", other)]}
    assert len(await food_lookup.lookup("курт", net.http)) == 2


async def test_off_garbage_payloads_are_ignored(net):
    net.classic = {"products": "nope"}
    net.search = {"hits": [None, 5, {"product_name": ["x"]}]}
    assert await food_lookup.lookup("курт", net.http) == []


# ---- Wikipedia ----


async def test_wikipedia_uses_only_the_article_about_the_word(net):
    net.wiki_search = [
        "курт",
        ["Курт", "Куртуа, Тибо"],
        ["", ""],
        ["https://ru.wikipedia.org/wiki/Курт", "https://ru.wikipedia.org/wiki/Куртуа,_Тибо"],
    ]
    net.wiki_query = {"query": {"pages": {"1": {"title": "Курт", "extract": "Курт — сушёные  шарики\nиз сыра."}}}}
    sources = await food_lookup.lookup("курт", net.http)
    assert [s.title for s in sources] == ["Википедия: Курт"]
    assert sources[0].snippet == "Курт — сушёные шарики из сыра."  # whitespace collapsed
    assert sources[0].per100 is None
    assert sources[0].url == "https://ru.wikipedia.org/wiki/Курт"
    (query,) = [r for r in net.requests if r.url.params.get("prop") == "extracts"]
    assert query.url.params["titles"] == "Курт"


async def test_wikipedia_nothing_about_the_word_skips_the_second_request(net):
    net.wiki_search = ["курт", ["Куртуа, Тибо", "Курск"], ["", ""], ["u1", "u2"]]
    assert await food_lookup.lookup("курт", net.http) == []
    assert not [r for r in net.requests if r.url.params.get("prop") == "extracts"]


async def test_wikipedia_article_without_extract_is_dropped(net):
    net.wiki_search = ["курт", ["Курт"], [""], ["u1"]]
    net.wiki_query = {"query": {"pages": {"1": {"title": "Курт", "extract": ""}}}}
    assert await food_lookup.lookup("курт", net.http) == []


async def test_wikipedia_snippet_is_capped(net):
    net.wiki_search = ["курт", ["Курт"], [""], ["u1"]]
    net.wiki_query = {"query": {"pages": {"1": {"title": "Курт", "extract": "а" * 1000}}}}
    (source,) = await food_lookup.lookup("курт", net.http)
    assert len(source.snippet) == food_lookup.SNIPPET_MAX


# ---- requests ----


async def test_every_request_carries_the_user_agent(net):
    net.wiki_search = ["курт", ["Курт"], [""], ["u1"]]
    net.wiki_query = {"query": {"pages": {"1": {"title": "Курт", "extract": "Курт — сыр."}}}}
    await food_lookup.lookup("курт", net.http, tavily_key="k")
    assert {r.url.host for r in net.requests} == {
        "world.openfoodfacts.org", "search.openfoodfacts.org", "ru.wikipedia.org", "api.tavily.com",
    }  # fmt: skip
    assert len(net.requests) == 5
    assert all(r.headers["user-agent"] == "GymAPP/0.1 (personal diary)" for r in net.requests)


async def test_off_classic_request_shape(net):
    await food_lookup.lookup("курт", net.http)
    (req,) = [r for r in net.requests if r.url.host == "world.openfoodfacts.org"]
    assert req.url.path == "/cgi/search.pl"
    assert req.url.params["search_terms"] == "курт" and req.url.params["json"] == "1"


# ---- failures ----


async def test_network_errors_give_nothing_and_are_not_cached(net):
    net.down("classic", "search", "wiki_search")
    assert await food_lookup.lookup("курт", net.http) == []
    first = len(net.requests)
    assert first == 3 and not food_lookup._CACHE
    assert await food_lookup.lookup("курт", net.http) == []
    assert len(net.requests) == 2 * first  # asked again: a network blip is not "nothing found"


async def test_failed_sources_are_skipped_with_a_warning_without_traceback(net, caplog):
    net.classic = 503
    net.search = 503
    net.wiki_search = ["курт", ["Курт"], [""], ["u1"]]
    net.wiki_query = {"query": {"pages": {"1": {"title": "Курт", "extract": "Курт — сыр."}}}}
    caplog.set_level(logging.WARNING, logger=MODULE_LOGGER)
    sources = await food_lookup.lookup("курт", net.http)
    assert [s.title for s in sources] == ["Википедия: Курт"]
    warnings = [r for r in caplog.records if r.name == MODULE_LOGGER and r.levelno == logging.WARNING]
    assert len(warnings) == 2
    assert all(r.exc_info is None and "HTTP 503" in r.getMessage() for r in warnings)
    assert "openfoodfacts.org" not in caplog.text  # no URL with the query in the log


async def test_connect_error_warning_names_only_the_error_type(net, caplog):
    net.down("classic")
    caplog.set_level(logging.WARNING, logger=MODULE_LOGGER)
    await food_lookup.lookup("курт", net.http)
    (record,) = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert record.exc_info is None and "ConnectError" in record.getMessage()


async def test_bad_json_from_a_source_is_just_a_failed_source(net):
    net.classic = ["not", "a", "dict"]  # .get on a list fails: still only that source is lost
    net.search = {"hits": [product("1", "Курт", OFF_NUTRIMENTS)]}
    sources = await food_lookup.lookup("курт", net.http)
    assert [s.title for s in sources] == ["Open Food Facts: Курт"]


# ---- cache ----


async def test_successful_lookup_is_cached_by_normalized_term(net):
    net.search = {"hits": [product("1", "Курт", OFF_NUTRIMENTS)]}
    first = await food_lookup.lookup("Курт ", net.http)
    made = len(net.requests)
    assert made == 3 and len(first) == 1
    assert await food_lookup.lookup("курт", net.http) == first
    assert await food_lookup.lookup("  КУРТ", net.http) == first
    assert len(net.requests) == made


async def test_nothing_found_is_cached_too(net):
    assert await food_lookup.lookup("курт", net.http) == []
    made = len(net.requests)
    assert await food_lookup.lookup("курт", net.http) == []
    assert len(net.requests) == made


async def test_yo_and_case_share_a_cache_entry(net):
    await food_lookup.lookup("Жёлудь", net.http)
    made = len(net.requests)
    await food_lookup.lookup("желудь", net.http)
    assert len(net.requests) == made


async def test_blank_term_is_not_looked_up(net):
    assert await food_lookup.lookup("   ", net.http) == []
    assert net.requests == []


async def test_cache_is_bounded(net, monkeypatch):
    monkeypatch.setattr(food_lookup, "CACHE_MAX", 2)
    for term in ("курт", "кутаб", "бурсак"):
        await food_lookup.lookup(term, net.http)
    assert list(food_lookup._CACHE) == ["кутаб", "бурсак"]


# ---- Tavily ----


async def test_tavily_is_not_called_without_a_key(net):
    await food_lookup.lookup("курт", net.http)
    assert "api.tavily.com" not in net.hosts()


async def test_tavily_post_and_sources(net):
    net.tavily = {
        "answer": "Курт — сушёный  сыр.",
        "results": [
            {"title": "Курт", "content": "Курт делают из кислого молока.", "url": "https://x.test/kurt"},
            {"title": "Пусто", "content": "", "url": "https://x.test/empty"},
            "junk",
        ],
    }
    sources = await food_lookup.lookup("курт", net.http, tavily_key="k")
    (req,) = [r for r in net.requests if r.url.host == "api.tavily.com"]
    assert req.method == "POST" and req.url.path == "/search"
    body = json.loads(req.content)
    assert body["api_key"] == "k" and "курт" in body["query"]
    assert body["max_results"] == 5 and body["include_answer"] is True
    assert sources == [
        Source("Поиск: ответ", "Курт — сушёный сыр.", None, ""),
        Source("Курт", "Курт делают из кислого молока.", None, "https://x.test/kurt"),
    ]


async def test_tavily_failure_does_not_lose_other_sources(net):
    net.tavily = 401
    net.search = {"hits": [product("1", "Курт", OFF_NUTRIMENTS)]}
    sources = await food_lookup.lookup("курт", net.http, tavily_key="k")
    assert [s.title for s in sources] == ["Open Food Facts: Курт"]


# ---- suggest ----


def opt(name: str = "курт (сушёный сыр)", **kw) -> dict:
    return {
        "name": name, "portion_g": 25, "kcal": 65, "protein_g": 6, "fat_g": 4, "carbs_g": 1,
        "note": "на 100 г 260 ккал, по Open Food Facts", **kw,
    }  # fmt: skip


class FakeJSON:
    def __init__(self, answer=None, error: Exception | None = None):
        self.answer, self.error = answer, error
        self.calls: list[list[dict[str, str]]] = []

    async def complete_json(self, messages):
        self.calls.append(messages)
        if self.error:
            raise self.error
        return self.answer


SOURCE = Source("Open Food Facts: Курт", "упаковка 100 г", Macros(kcal=380, protein_g=30, fat_g=10, carbs_g=5), "u")


async def test_suggest_returns_at_most_three_options():
    llm = FakeJSON({"options": [opt(f"вариант {i}") for i in range(5)]})
    options = await food_lookup.suggest("курт", [SOURCE], llm)
    assert [o.name for o in options] == ["вариант 0", "вариант 1", "вариант 2"]
    assert all(isinstance(o, Option) for o in options)


async def test_suggest_drops_only_the_invalid_items():
    answer = {
        "options": [
            opt("плохой минус", kcal=-1),
            opt("хороший 1"),
            opt("плохая порция", portion_g=0),
            opt("n" * 61),
            opt("хороший 2"),
            "junk",
            {"name": "без чисел"},
        ]
    }
    options = await food_lookup.suggest("курт", [SOURCE], FakeJSON(answer))
    assert [o.name for o in options] == ["хороший 1", "хороший 2"]


async def test_suggest_invalid_items_do_not_use_up_the_limit():
    answer = {"options": [opt("a", kcal=-1), opt("b", portion_g=0), opt("c"), opt("d"), opt("e"), opt("f")]}
    options = await food_lookup.suggest("курт", [SOURCE], FakeJSON(answer))
    assert [o.name for o in options] == ["c", "d", "e"]


async def test_suggest_drops_duplicates_by_name():
    answer = {"options": [opt("Курт (сыр)"), opt("  курт   (СЫР) "), opt("кУрт (сыр)", portion_g=10), opt("другой")]}
    options = await food_lookup.suggest("курт", [SOURCE], FakeJSON(answer))
    assert [o.name for o in options] == ["Курт (сыр)", "другой"]


async def test_suggest_without_sources_marks_every_option():
    answer = {"options": [opt("a", note="по описанию"), opt("b", note=""), opt("c")]}
    options = await food_lookup.suggest("курт", [], FakeJSON(answer))
    assert [o.note for o in options] == [food_lookup.NO_SOURCE_NOTE] * 3


async def test_suggest_with_sources_keeps_the_models_note():
    options = await food_lookup.suggest("курт", [SOURCE], FakeJSON({"options": [opt()]}))
    assert options[0].note == "на 100 г 260 ккал, по Open Food Facts"


async def test_suggest_message_has_term_phrase_and_macros():
    llm = FakeJSON({"options": []})
    await food_lookup.suggest("курт", [SOURCE], llm, "съел 5 маленьких куртов")
    (messages,) = llm.calls
    assert messages[0]["role"] == "system" and messages[-1]["role"] == "user"
    user = messages[-1]["content"]
    assert "курт" in user and "съел 5 маленьких куртов" in user
    assert "на 100 г: 380 ккал" in user and "Б30 Ж10 У5" in user


async def test_suggest_message_without_sources_says_so():
    llm = FakeJSON({"options": []})
    await food_lookup.suggest("курт", [], llm)
    assert llm.calls[0][-1]["content"].endswith("Источники:\nнет")


async def test_suggest_llm_error_gives_no_options(caplog):
    caplog.set_level(logging.WARNING, logger=MODULE_LOGGER)
    assert await food_lookup.suggest("курт", [SOURCE], FakeJSON(error=LLMError("all routes failed"))) == []
    assert all(r.exc_info is None for r in caplog.records)


@pytest.mark.parametrize("answer", [["options"], "text", None, {}, {"options": "x"}, {"options": None}, {"other": []}])
async def test_suggest_bad_answer_shapes_give_no_options(answer):
    assert await food_lookup.suggest("курт", [SOURCE], FakeJSON(answer)) == []


def test_option_recomputes_kcal_that_contradict_macros():
    o = Option.model_validate(opt(kcal=500))
    assert o.kcal == 4 * 6 + 9 * 4 + 4 * 1  # trusts the macros, like ParsedFood


# ---- amount ----


@pytest.mark.parametrize(
    ("phrase", "term", "expected"),
    [
        ("съел 5 маленьких куртов", "курт", Amount(count=5)),
        ("съел 2 кутаба", "кутаб", Amount(count=2)),
        ("два кутаба и 2 яйца", "кутаб", Amount(count=2)),
        ("тандыр-гошт 200 г", "тандыр-гошт", Amount(grams=200)),
        ("200 г тандыр-гошта", "тандыр-гошт", Amount(grams=200)),
        ("курт 3 шт", "курт", Amount(count=3)),
        ("2 яйца и курт", "курт", Amount(count=1)),
        ("пару бурсаков", "бурсак", Amount(count=2)),
        ("съел курт", "курт", Amount()),
        ("пол кутаба", "кутаб", Amount(count=0.5)),
        ("кутаб 150 грамм", "кутаб", Amount(grams=150)),
        ("200 г гречки и кутаб", "кутаб", Amount()),
        ("", "курт", Amount()),
        ("съел 3 курта", "", Amount()),
    ],
)
def test_amount(phrase, term, expected):
    assert food_lookup.amount(phrase, term) == expected


def test_amount_explicit_weight_wins_over_count_words():
    assert food_lookup.amount("кутаб 150 г", "кутаб") == Amount(grams=150)
    assert food_lookup.amount("150 г кутаба", "кутаб") == Amount(grams=150)


# ---- as_food ----

KURT = Option(name="курт (сушёный сыр)", portion_g=10, kcal=26, protein_g=2.5, fat_g=1.5, carbs_g=0.3)


def test_as_food_scales_by_count():
    f = food_lookup.as_food(KURT, Amount(count=5))
    assert f.description == "курт (сушёный сыр), 5 шт"
    assert f.grams == 50
    assert f.kcal == pytest.approx(130, abs=1)
    assert (f.protein_g, f.fat_g, f.carbs_g) == (12.5, 7.5, 1.5)


def test_as_food_one_piece_keeps_the_name():
    f = food_lookup.as_food(KURT, Amount())
    assert f.description == "курт (сушёный сыр)" and f.grams == 10 and f.kcal == 26


def test_as_food_half_a_piece():
    f = food_lookup.as_food(KURT, Amount(count=0.5))
    assert f.description == "курт (сушёный сыр), 0.5 шт" and f.grams == 5


def test_as_food_scales_by_grams():
    gosht = Option(name="тандыр-гошт", portion_g=100, kcal=247, protein_g=25, fat_g=15, carbs_g=3)
    f = food_lookup.as_food(gosht, Amount(grams=200))
    assert f.description == "тандыр-гошт" and f.grams == 200
    assert (f.kcal, f.protein_g, f.fat_g, f.carbs_g) == (494, 50, 30, 6)


# ---- find_options ----


async def test_find_options_without_sources_does_not_ask_the_model(net):
    llm = FakeJSON({"options": [opt()]})
    assert await food_lookup.find_options("курт", "съел курт", llm, net.http) == []
    assert llm.calls == []


async def test_find_options_with_sources_asks_the_model(net):
    net.search = {"hits": [product("1", "Курт", OFF_NUTRIMENTS)]}
    llm = FakeJSON({"options": [opt()]})
    options = await food_lookup.find_options("курт", "съел 5 маленьких куртов", llm, net.http)
    assert [o.name for o in options] == ["курт (сушёный сыр)"]
    assert len(llm.calls) == 1 and "на 100 г: 380 ккал" in llm.calls[0][-1]["content"]


def test_amount_pieces_of_a_given_weight():
    assert food_lookup.amount("2 кутаба по 150 г", "кутаб") == food_lookup.Amount(grams=300)
    assert food_lookup.amount("съел три курта по 10 грамм", "курт") == food_lookup.Amount(grams=30)
