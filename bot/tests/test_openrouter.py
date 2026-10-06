import json

import httpx
import pytest

from gymbot.config import Settings
from gymbot.llm.openrouter import LLMError, OpenRouterClient, extract_json

GOOD = {"kind": "unknown", "clarification": "что?"}


def settings(**kw) -> Settings:
    return Settings(
        _env_file=None,
        bot_token="123:abc",
        openrouter_api_key=kw.pop("openrouter_api_key", "k"),
        openrouter_model="m1",
        openrouter_fallback_models=["m2", "m3"],
        **kw,
    )


def reply(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def client_with(handler, **kw) -> OpenRouterClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OpenRouterClient(settings(**kw), http)


def test_extract_json_prefers_object_with_kind():
    text = 'Thinking: the user wrote {weird} and maybe {"a": 1}. Answer: {"kind": "unknown", "clarification": "x"} done'
    assert extract_json(text) == {"kind": "unknown", "clarification": "x"}


def test_extract_json_falls_back_to_first_object_without_kind():
    assert extract_json('prose {"a": 1} more') == {"a": 1}


def test_extract_json_no_json():
    with pytest.raises(LLMError):
        extract_json("just words {not json")


async def test_success_first_model():
    models = []

    def handler(req):
        models.append(json.loads(req.content)["model"])
        return reply(json.dumps(GOOD))

    r = await client_with(handler).parse_message("hi", [])
    assert r.kind == "unknown" and models == ["m1"]


async def test_fallback_on_429():
    models = []

    def handler(req):
        m = json.loads(req.content)["model"]
        models.append(m)
        return httpx.Response(429) if m == "m1" else reply(json.dumps(GOOD))

    r = await client_with(handler).parse_message("hi", [])
    assert r.kind == "unknown"
    assert models == ["m1", "m2"]  # 429 skips straight to the next model, no retry


async def test_fallback_on_invalid_json():
    models = []

    def handler(req):
        m = json.loads(req.content)["model"]
        models.append(m)
        return reply("sorry, no json here") if m == "m1" else reply(json.dumps(GOOD))

    r = await client_with(handler).parse_message("hi", [])
    assert r.kind == "unknown"
    assert models[-1] == "m2" and set(models[:-1]) == {"m1"}


async def test_all_models_fail():
    with pytest.raises(LLMError, match="all models failed"):
        await client_with(lambda req: httpx.Response(429)).parse_message("hi", [])


async def test_no_api_key_makes_no_request():
    calls = []

    def handler(req):
        calls.append(req)
        return reply(json.dumps(GOOD))

    with pytest.raises(LLMError, match="OPENROUTER_API_KEY"):
        await client_with(handler, openrouter_api_key="").parse_message("hi", [])
    assert calls == []


async def test_reasoning_field_used_when_content_empty():
    def handler(req):
        return httpx.Response(
            200, json={"choices": [{"message": {"content": None, "reasoning": "hmm {x} " + json.dumps(GOOD)}}]}
        )

    assert (await client_with(handler).parse_message("hi", [])).kind == "unknown"
