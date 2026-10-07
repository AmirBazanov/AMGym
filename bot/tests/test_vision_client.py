"""Food by photo, LLM side: vision routes, request shape, timeouts, result parsing, no image in logs."""

import json
import logging

import httpx
import pytest

from gymbot.config import Settings
from gymbot.llm.openrouter import (
    VISION_TIMEOUT,
    LLMError,
    OpenRouterClient,
    vision_routes_from,
)
from gymbot.llm.prompts import VISION_DEFAULT_TEXT, VISION_FACTS_MAX_CHARS, VISION_SYSTEM

QWEN = "qwen/qwen3.8-27b"
GEMMA_31 = "google/gemma-4-31b-it:free"
GEMMA_26 = "google/gemma-4-26b-a4b-it:free"
B64 = "QUJDREVGR0hJSktMTU5PUA" * 20  # a distinctive "image": must never reach a log record

FOOD = {
    "foods": [
        {"description": "самса", "grams": 250, "kcal": 650, "protein_g": 25, "fat_g": 40, "carbs_g": 45},
    ],
    "note": "порция на глаз",
}


def settings(**kw) -> Settings:
    """Both keys by default; the real .env and the shell's keys never leak in. Model lists stay at defaults."""
    kw.setdefault("groq_api_key", "g")
    kw.setdefault("stt_api_key", "")
    kw.setdefault("openrouter_api_key", "o")
    return Settings(_env_file=None, bot_token="123:abc", **kw)


def reply(content) -> httpx.Response:
    text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
    return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})


def make_client(handler, **kw) -> OpenRouterClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=60)  # as in production
    return OpenRouterClient(settings(**kw), http)


def models_of(routes) -> list[str]:
    return [r.model for r in routes]


# ---- vision_routes_from ----


def test_routes_groq_vision_first_then_openrouter_vision():
    routes = vision_routes_from(settings())
    assert [(r.provider, r.model) for r in routes] == [
        ("groq", QWEN),
        ("openrouter", GEMMA_31),
        ("openrouter", GEMMA_26),
    ]
    assert routes[0].base_url == "https://api.groq.com/openai/v1"
    assert routes[1].base_url == "https://openrouter.ai/api/v1"


def test_routes_never_contain_text_only_models():
    names = " ".join(r.name for r in vision_routes_from(settings()))
    assert "gpt-oss" not in names and "ling" not in names and "apodex" not in names


def test_routes_without_keys_are_empty():
    assert vision_routes_from(settings(groq_api_key="", openrouter_api_key="")) == []


def test_routes_only_openrouter_key_gives_gemma_only():
    routes = vision_routes_from(settings(groq_api_key=""))
    assert models_of(routes) == [GEMMA_31, GEMMA_26]
    assert {r.provider for r in routes} == {"openrouter"}


def test_routes_only_groq_key_gives_qwen_only():
    assert models_of(vision_routes_from(settings(openrouter_api_key=""))) == [QWEN]


def test_routes_use_the_stt_groq_key():
    routes = vision_routes_from(settings(groq_api_key="", stt_api_key="stt", openrouter_api_key=""))
    assert [(r.provider, r.api_key) for r in routes] == [("groq", "stt")]


def test_routes_overrides():
    routes = vision_routes_from(settings(vision_models=["v1", "v2"], openrouter_vision_models=["o1"]))
    assert [(r.provider, r.model) for r in routes] == [("groq", "v1"), ("groq", "v2"), ("openrouter", "o1")]


def test_client_has_separate_text_and_vision_routes():
    client = make_client(lambda req: reply(FOOD))
    assert models_of(client.vision_routes) == [QWEN, GEMMA_31, GEMMA_26]
    assert not set(models_of(client.vision_routes)) & {"openai/gpt-oss-20b", "openai/gpt-oss-120b"}
    assert "inclusionai/ling-3.1-flash" in models_of(client.routes)


# ---- request shape ----


async def one_request(client_kw=None, **photo_kw) -> dict:
    seen: list[httpx.Request] = []

    def handler(req):
        seen.append(req)
        return reply(FOOD)

    await make_client(handler, **(client_kw or {})).parse_photo(B64, **photo_kw)
    assert len(seen) == 1
    return json.loads(seen[0].content)


async def test_request_body_shape_with_caption():
    body = await one_request(caption="17 штук, 250 г")
    assert body["model"] == QWEN
    assert body["response_format"] == {"type": "json_object"}
    system, user = body["messages"]
    assert system["role"] == "system" and system["content"].startswith(VISION_SYSTEM)
    assert user["role"] == "user"
    text_part, image_part = user["content"]  # the text comes BEFORE the image
    assert text_part == {"type": "text", "text": "Подпись: 17 штук, 250 г"}
    assert image_part == {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{B64}"}}


async def test_request_without_caption_uses_default_text():
    body = await one_request()
    parts = body["messages"][1]["content"]
    assert parts[0] == {"type": "text", "text": VISION_DEFAULT_TEXT}
    assert "Подпись" not in parts[0]["text"]
    assert parts[1]["type"] == "image_url"


async def test_request_uses_given_mime():
    body = await one_request(mime="image/png")
    assert body["messages"][1]["content"][1]["image_url"]["url"] == f"data:image/png;base64,{B64}"


async def test_request_authorization_matches_provider():
    seen: list[httpx.Request] = []

    def handler(req):
        seen.append(req)
        return httpx.Response(429, headers={"retry-after": "100"}) if len(seen) == 1 else reply(FOOD)

    await make_client(handler).parse_photo(B64)
    assert [r.headers["authorization"] for r in seen] == ["Bearer g", "Bearer o"]
    assert str(seen[0].url).startswith("https://api.groq.com/openai/v1/chat/completions")
    assert str(seen[1].url).startswith("https://openrouter.ai/api/v1/chat/completions")


async def test_facts_go_into_the_system_prompt():
    body = await one_request(facts=["самса ~150 г", "порция каши 300 г"])
    system = body["messages"][0]["content"]
    assert system.startswith(VISION_SYSTEM)
    assert "самса ~150 г" in system and "порция каши 300 г" in system


async def test_without_facts_system_prompt_is_exactly_the_vision_prompt():
    body = await one_request(facts=[])
    assert body["messages"][0]["content"] == VISION_SYSTEM


async def test_long_facts_are_capped():
    facts = [f"факт номер {i}: " + "ж" * 60 for i in range(200)]
    body = await one_request(facts=facts)
    system = body["messages"][0]["content"]
    assert "факт номер 0:" in system  # newest first: the first ones stay
    assert "факт номер 199" not in system
    assert len(system) <= len(VISION_SYSTEM) + VISION_FACTS_MAX_CHARS + 2  # "\n" and the final "."


# ---- timeout ----


async def test_photo_request_has_the_vision_timeout_and_text_keeps_the_default():
    timeouts: dict[str, dict] = {}

    def handler(req):
        body = json.loads(req.content)
        is_photo = isinstance(body["messages"][1]["content"], list)
        timeouts["photo" if is_photo else "text"] = dict(req.extensions["timeout"])
        return reply(FOOD if is_photo else {"kind": "unknown", "clarification": "что?"})

    client = make_client(handler)
    await client.parse_photo(B64)
    await client.parse_message("привет", [])

    assert VISION_TIMEOUT == 20
    assert timeouts["photo"]["read"] == VISION_TIMEOUT
    assert timeouts["text"]["read"] != VISION_TIMEOUT
    assert timeouts["text"]["read"] == 60  # the client default


# ---- routes: selection and fallback ----


async def test_429_on_first_vision_route_goes_to_gemma_and_never_to_text_models():
    models: list[str] = []

    def handler(req):
        models.append(json.loads(req.content)["model"])
        if len(models) == 1:
            return httpx.Response(429, headers={"retry-after": "100"}, json={"error": "rate"})
        return reply(FOOD)

    result = (await make_client(handler).parse_photo(B64)).result

    assert models == [QWEN, GEMMA_31]
    assert result.kind == "food"


async def test_every_vision_route_fails_without_touching_text_models():
    models: list[str] = []

    def handler(req):
        models.append(json.loads(req.content)["model"])
        return httpx.Response(429, headers={"retry-after": "100"}, json={"error": "rate"})

    with pytest.raises(LLMError):
        await make_client(handler).parse_photo(B64)

    assert models == [QWEN, GEMMA_31, GEMMA_26]  # one attempt each: a 429 moves on at once
    assert not any("gpt-oss" in m or "ling" in m for m in models)


async def test_read_timeout_tries_next_route_after_one_attempt():
    models: list[str] = []

    def handler(req):
        models.append(json.loads(req.content)["model"])
        if len(models) == 1:
            raise httpx.ReadTimeout("slow", request=req)
        return reply(FOOD)

    result = (await make_client(handler).parse_photo(B64)).result

    assert models == [QWEN, GEMMA_31]  # the hung route once, not twice
    assert result.foods[0].description == "самса"


async def test_timeouts_everywhere_make_one_attempt_per_route_then_fail():
    models: list[str] = []

    def handler(req):
        models.append(json.loads(req.content)["model"])
        raise httpx.ConnectTimeout("slow", request=req)

    with pytest.raises(LLMError):
        await make_client(handler).parse_photo(B64)

    assert models == [QWEN, GEMMA_31, GEMMA_26]


async def test_400_without_json_mode_support_retries_the_same_route_without_it():
    bodies: list[dict] = []

    def handler(req):
        bodies.append(json.loads(req.content))
        if len(bodies) == 1:
            return httpx.Response(400, json={"error": {"message": "does not support response_format"}})
        return reply(FOOD)

    result = (await make_client(handler).parse_photo(B64)).result

    assert [b["model"] for b in bodies] == [QWEN, QWEN]
    assert "response_format" in bodies[0] and "response_format" not in bodies[1]
    assert result.kind == "food"


# ---- results ----


async def test_valid_answer_becomes_a_food_result():
    result = (await make_client(lambda req: reply(FOOD)).parse_photo(B64, caption="250 г")).result
    assert result.kind == "food"
    assert len(result.foods) == 1
    f = result.foods[0]
    assert (f.description, f.grams, f.kcal) == ("самса", 250, 650)
    assert result.note == "порция на глаз"
    assert result.unknown_terms == []


async def test_blank_or_missing_note_is_none():
    for note in ({"note": None}, {"note": "  "}, {}):
        result = (await make_client(lambda req, n=note: reply({"foods": FOOD["foods"], **n})).parse_photo(B64)).result
        assert result.note is None


async def test_no_food_is_an_empty_result_not_an_error():
    result = (await make_client(lambda req: reply({"foods": []})).parse_photo(B64)).result
    assert result.kind == "food"
    assert result.foods == [] and result.unknown_terms == []


async def test_food_with_null_kcal_moves_to_unknown_terms():
    answer = {"foods": [
        FOOD["foods"][0],
        {"description": "гульчатай, 2 шт", "grams": None, "kcal": None, "protein_g": None, "fat_g": None,
         "carbs_g": None},
    ]}
    result = (await make_client(lambda req: reply(answer)).parse_photo(B64)).result
    assert [f.description for f in result.foods] == ["самса"]
    assert result.unknown_terms == ["гульчатай"]


async def test_prose_and_fences_around_the_json_are_ignored():
    content = "Вот оценка:\n```json\n" + json.dumps(FOOD, ensure_ascii=False) + "\n```\nПриятного!"
    result = (await make_client(lambda req: reply(content)).parse_photo(B64)).result
    assert result.foods[0].description == "самса"


async def test_thinking_block_before_the_json_is_ignored():
    content = '<think>может быть {"foods": 1}?</think>' + json.dumps(FOOD, ensure_ascii=False)
    result = (await make_client(lambda req: reply(content)).parse_photo(B64)).result
    assert result.foods[0].kcal == 650


async def test_json_with_unrelated_object_first_prefers_the_foods_object():
    content = '{"x": 1} ' + json.dumps(FOOD, ensure_ascii=False)
    result = (await make_client(lambda req: reply(content)).parse_photo(B64)).result
    assert len(result.foods) == 1


async def test_answer_without_a_foods_list_falls_to_the_next_route():
    models: list[str] = []

    def handler(req):
        models.append(json.loads(req.content)["model"])
        return reply({"kind": "unknown"}) if len(models) <= 1 else reply(FOOD)

    result = (await make_client(handler).parse_photo(B64)).result
    # one attempt on qwen (a retry would cost another image of the shared quota), then gemma answers
    assert models == [QWEN, GEMMA_31]
    assert result.kind == "food"


async def test_server_errors_everywhere_raise_llm_error():
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(500, json={"error": "boom"})

    with pytest.raises(LLMError):
        await make_client(handler).parse_photo(B64)
    assert len(calls) == 6  # two attempts on each of the three routes


async def test_no_keys_raises_without_requests():
    calls = []

    def handler(req):
        calls.append(req)
        return reply(FOOD)

    client = make_client(handler, groq_api_key="", openrouter_api_key="")
    with pytest.raises(LLMError):
        await client.parse_photo(B64)
    assert calls == []


async def test_only_openrouter_key_never_calls_groq():
    urls: list[str] = []

    def handler(req):
        urls.append(str(req.url))
        return reply(FOOD)

    await make_client(handler, groq_api_key="").parse_photo(B64)
    assert len(urls) == 1 and urls[0].startswith("https://openrouter.ai/")


# ---- privacy: the image is never logged ----


def assert_no_image(caplog) -> None:
    assert caplog.records, "the failure paths should log something"
    for rec in caplog.records:
        text = rec.getMessage()
        assert B64 not in text and B64[:40] not in text
        assert B64[:40] not in str(rec.args) and B64[:40] not in (str(rec.exc_info[1]) if rec.exc_info else "")


async def test_image_is_not_logged_on_success(caplog):
    caplog.set_level(logging.DEBUG)
    body = {"choices": [{"message": {"content": json.dumps(FOOD)}}], "usage": {"prompt_tokens": 2000, "completion_tokens": 90}}
    await make_client(lambda req: httpx.Response(200, json=body)).parse_photo(B64, caption="x")
    assert_no_image(caplog)


async def test_image_is_not_logged_on_400(caplog):
    caplog.set_level(logging.DEBUG)
    with pytest.raises(LLMError) as exc:
        await make_client(lambda req: httpx.Response(400, json={"error": "image too big"})).parse_photo(B64)
    assert_no_image(caplog)
    assert B64[:40] not in str(exc.value)


async def test_image_is_not_logged_on_invalid_json(caplog):
    caplog.set_level(logging.DEBUG)
    with pytest.raises(LLMError) as exc:
        await make_client(lambda req: reply("не могу разобрать {это не json")).parse_photo(B64)
    assert_no_image(caplog)
    assert B64[:40] not in str(exc.value)


async def test_image_is_not_logged_on_timeout_and_429(caplog):
    caplog.set_level(logging.DEBUG)
    calls = []

    def handler(req):
        calls.append(req)
        if len(calls) == 1:
            raise httpx.ReadTimeout("slow", request=req)
        return httpx.Response(429, headers={"retry-after": "100"})

    with pytest.raises(LLMError) as exc:
        await make_client(handler).parse_photo(B64)
    assert_no_image(caplog)
    assert B64[:40] not in str(exc.value)


@pytest.mark.parametrize(
    "content",
    [
        "no json here at all",
        '{"foods": "плов"}',
        '{"foods": [{"description": "плов", "grams": 300, "kcal": -5, "protein_g": 1, "fat_g": 1, "carbs_g": 1}]}',
        '{"foods": [{"description": "плов", "kcal": 500}]}',
    ],
    ids=["prose", "foods-not-a-list", "negative-kcal", "missing-fields"],
)
async def test_unusable_answer_goes_to_the_next_route_without_a_retry(content):
    models: list[str] = []

    def handler(req):
        models.append(json.loads(req.content)["model"])
        return reply(content) if len(models) == 1 else reply(FOOD)

    result = (await make_client(handler).parse_photo(B64)).result
    assert models == [QWEN, GEMMA_31]  # a retry on qwen would spend another image of the shared quota
    assert result.kind == "food" and result.foods


# ---- package labels (PhotoParse.label) ----

LABEL = {
    "name": "Протеиновый батончик",
    "brand": "Bombbar",
    "per100": {"kcal": 360, "protein_g": 33, "fat_g": 12, "carbs_g": 30},
    "net_weight_g": 60,
    "serving_g": None,
}


async def test_label_answer_becomes_a_parsed_label_and_an_empty_result():
    parsed = await make_client(lambda req: reply({"label": LABEL})).parse_photo(B64)
    assert parsed.result.kind == "food"
    assert parsed.result.foods == [] and parsed.result.unknown_terms == []
    label = parsed.label
    assert label is not None
    assert (label.name, label.brand) == ("Протеиновый батончик", "Bombbar")
    assert (label.per100.kcal, label.per100.protein_g, label.per100.fat_g, label.per100.carbs_g) == (360, 33, 12, 30)
    assert label.net_weight_g == 60 and label.serving_g is None


async def test_food_answer_has_no_label():
    parsed = await make_client(lambda req: reply(FOOD)).parse_photo(B64)
    assert parsed.label is None
    assert len(parsed.result.foods) == 1


async def test_label_numbers_may_be_strings_with_comma_and_units():
    label = {
        "name": "Йогурт",
        "per100": {"kcal": "449 ккал", "protein_g": "12,5", "fat_g": "20,5 г", "carbs_g": "50"},
        "net_weight_g": "125 г",
        "serving_g": "0",
    }
    parsed = await make_client(lambda req: reply({"label": label})).parse_photo(B64)
    assert parsed.label is not None
    p = parsed.label.per100
    assert (p.kcal, p.protein_g, p.fat_g, p.carbs_g) == (449, 12.5, 20.5, 50)
    assert parsed.label.net_weight_g == 125
    assert parsed.label.serving_g is None  # 0 = unknown


async def test_label_and_foods_in_one_answer_label_wins():
    parsed = await make_client(lambda req: reply({"label": LABEL, "foods": FOOD["foods"]})).parse_photo(B64)
    assert parsed.label is not None and parsed.label.brand == "Bombbar"
    assert parsed.result.foods == []


async def test_label_after_foods_in_the_text_is_still_found():
    content = json.dumps({"foods": FOOD["foods"], "label": LABEL}, ensure_ascii=False)
    parsed = await make_client(lambda req: reply(content)).parse_photo(B64)
    assert parsed.label is not None and parsed.result.foods == []


@pytest.mark.parametrize(
    "answer",
    [{"label": "Bombbar"}, {"label": None}, {"note": "не вижу"}, {"foods": "плов", "label": 5}],
    ids=["label-string", "label-null", "neither", "foods-not-a-list"],
)
async def test_neither_foods_nor_label_goes_to_the_next_route_after_one_attempt(answer):
    models: list[str] = []

    def handler(req):
        models.append(json.loads(req.content)["model"])
        return reply(answer) if len(models) == 1 else reply(FOOD)

    parsed = await make_client(handler).parse_photo(B64)
    assert models == [QWEN, GEMMA_31]
    assert parsed.result.foods and parsed.label is None


async def test_an_unreadable_label_is_still_an_answer_no_next_route():
    """kcal that disagree with the macros are the handler's business (check_label), not a reason to ask again."""
    bad = {**LABEL, "per100": {"kcal": 900, "protein_g": 3, "fat_g": 2, "carbs_g": 5}}
    models: list[str] = []

    def handler(req):
        models.append(json.loads(req.content)["model"])
        return reply({"label": bad})

    parsed = await make_client(handler).parse_photo(B64)
    assert models == [QWEN]
    assert parsed.label is not None and parsed.label.per100.kcal == 900


async def test_label_with_missing_numbers_is_returned_as_read():
    parsed = await make_client(lambda req: reply({"label": {"name": "Что-то", "per100": {"kcal": 300}}})).parse_photo(B64)
    assert parsed.label is not None
    assert parsed.label.per100.kcal == 300 and parsed.label.per100.protein_g is None


async def test_label_without_per100_is_an_empty_label_not_an_error():
    parsed = await make_client(lambda req: reply({"label": {"name": "Что-то", "per100": None}})).parse_photo(B64)
    assert parsed.label is not None and parsed.label.per100.kcal is None


def test_vision_prompt_asks_for_a_label_when_the_photo_is_a_package():
    assert '"label"' in VISION_SYSTEM and "per100" in VISION_SYSTEM
    assert "net_weight_g" in VISION_SYSTEM and "serving_g" in VISION_SYSTEM
