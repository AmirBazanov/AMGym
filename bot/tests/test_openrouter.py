import json

import httpx
import pytest

from gymbot.config import Settings
from gymbot.llm.openrouter import LLMError, OpenRouterClient, extract_json

GOOD = {"kind": "unknown", "clarification": "что?"}
NOVITA_400 = {
    "error": {
        "message": "Provider returned error",
        "code": 400,
        "metadata": {"raw": "Model x does not support feature: structured-outputs"},
    }
}
CONTEXT_400 = {"error": {"message": "This endpoint's maximum context length is 8192 tokens", "code": 400}}


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


async def test_400_retries_same_model_without_response_format():
    bodies = []

    def handler(req):
        body = json.loads(req.content)
        bodies.append(body)
        return httpx.Response(400, json=NOVITA_400) if "response_format" in body else reply(json.dumps(GOOD))

    c = client_with(handler)
    assert (await c.parse_message("hi", [])).kind == "unknown"
    assert [b["model"] for b in bodies] == ["m1", "m1"]
    assert "response_format" in bodies[0] and "response_format" not in bodies[1]

    # The client remembers that m1 rejects json mode and skips it next time.
    assert (await c.parse_message("hi", [])).kind == "unknown"
    assert len(bodies) == 3 and bodies[2]["model"] == "m1" and "response_format" not in bodies[2]


async def test_400_without_response_format_moves_to_next_model():
    models = []

    def handler(req):
        m = json.loads(req.content)["model"]
        models.append(m)
        return httpx.Response(400) if m == "m1" else reply(json.dumps(GOOD))

    assert (await client_with(handler).parse_message("hi", [])).kind == "unknown"
    assert models == ["m1", "m2"]  # a 400 without a word about json mode: straight to the next model


async def test_400_on_json_mode_then_400_without_moves_on():
    models = []

    def handler(req):
        m = json.loads(req.content)["model"]
        models.append(m)
        return httpx.Response(400, json=NOVITA_400) if m == "m1" else reply(json.dumps(GOOD))

    assert (await client_with(handler).parse_message("hi", [])).kind == "unknown"
    assert models == ["m1", "m1", "m2"]  # with json mode, without, then next model


async def test_other_400_keeps_json_mode():
    bodies = []

    def handler(req):
        body = json.loads(req.content)
        bodies.append(body)
        return httpx.Response(400, json=CONTEXT_400) if body["model"] == "m1" else reply(json.dumps(GOOD))

    c = client_with(handler)
    await c.parse_message("hi", [])
    await c.parse_message("hi", [])
    m1 = [b for b in bodies if b["model"] == "m1"]
    assert len(m1) == 2 and all("response_format" in b for b in m1)


async def test_history_is_sent_before_new_text():
    bodies = []

    def handler(req):
        bodies.append(json.loads(req.content))
        return reply(json.dumps(GOOD))

    await client_with(handler).parse_message("три штуки", [], history=[("три самсы", '{"kind":"food"}')])
    assert [m["content"] for m in bodies[0]["messages"][-3:]] == ["три самсы", '{"kind":"food"}', "три штуки"]


async def test_aclose_closes_http():
    c = client_with(lambda req: reply(json.dumps(GOOD)))
    await c.aclose()
    assert c.http.is_closed


# ---- complete_text: plain generation (advice) ----

MSGS = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]


async def test_complete_text_plain_body_and_clean_text():
    bodies = []

    def handler(req):
        bodies.append(json.loads(req.content))
        return reply("<think>hmm, let me think</think>\n\n**Питание**\n- добери 40 г белка\n")

    text = await client_with(handler).complete_text(MSGS)
    assert text == "Питание\n- добери 40 г белка"
    assert "response_format" not in bodies[0]
    assert bodies[0]["messages"] == MSGS and bodies[0]["model"] == "m1"


async def test_complete_text_strips_code_fence_and_headings():
    text = await client_with(lambda req: reply("```\n## Питание\n- пункт\n```")).complete_text(MSGS)
    assert text == "Питание\n- пункт"


async def test_complete_text_fallback_on_429():
    models = []

    def handler(req):
        m = json.loads(req.content)["model"]
        models.append(m)
        return httpx.Response(429) if m == "m1" else reply("ok")

    assert await client_with(handler).complete_text(MSGS) == "ok"
    assert models == ["m1", "m2"]


async def test_complete_text_400_moves_to_next_model():
    models = []

    def handler(req):
        m = json.loads(req.content)["model"]
        models.append(m)
        return httpx.Response(400, json=CONTEXT_400) if m == "m1" else reply("ok")

    assert await client_with(handler).complete_text(MSGS) == "ok"
    assert models == ["m1", "m2"]


async def test_complete_text_ignores_reasoning_when_content_empty():
    # The chain of thought must never reach the user as the answer.
    models = []

    def handler(req):
        m = json.loads(req.content)["model"]
        models.append(m)
        if m == "m1":
            return httpx.Response(200, json={"choices": [{"message": {"content": "", "reasoning": "secret thoughts"}}]})
        return reply("ok")

    assert await client_with(handler).complete_text(MSGS) == "ok"
    assert models[0] == "m1" and models[-1] == "m2"


async def test_complete_text_all_fail():
    with pytest.raises(LLMError, match="all models failed"):
        await client_with(lambda req: httpx.Response(429)).complete_text(MSGS)


async def test_complete_text_no_api_key_makes_no_request():
    calls = []
    with pytest.raises(LLMError, match="OPENROUTER_API_KEY"):
        await client_with(lambda req: calls.append(req) or reply("ok"), openrouter_api_key="").complete_text(MSGS)
    assert calls == []
