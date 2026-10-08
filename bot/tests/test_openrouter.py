import json

import httpx
import pytest

from gymbot.config import Settings
from gymbot.llm.openrouter import (
    MAX_COOLDOWN,
    MIN_COOLDOWN,
    RATE_LIMIT_COOLDOWN,
    LLMClient,
    LLMError,
    OpenRouterClient,
    Route,
    cooldown_after,
    extract_json,
    parse_duration,
    routes_from,
)

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
    assert text == "**Питание**\n- добери 40 г белка"  # markup stays: gymbot.services.tg_html makes HTML of it
    assert "response_format" not in bodies[0]
    assert bodies[0]["messages"] == MSGS and bodies[0]["model"] == "m1"


async def test_complete_text_strips_code_fence_keeps_headings():
    text = await client_with(lambda req: reply("```\n## Питание\n- пункт\n```")).complete_text(MSGS)
    assert text == "## Питание\n- пункт"


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


# ---- routes: Groq first, OpenRouter as the last fallback ----


def groq_settings(**kw) -> Settings:
    base = {"stt_api_key": "stt-key", "groq_models": ["g1", "g2"], "openrouter_api_key": "or-key"}
    return Settings(_env_file=None, bot_token="123:abc", openrouter_model="m1", openrouter_fallback_models=["m2"],
                    **{**base, **kw})


def test_routes_groq_key_defaults_to_stt_key_and_comes_first():
    routes = routes_from(groq_settings())
    assert [r.name for r in routes] == ["groq/g1", "groq/g2", "openrouter/m1", "openrouter/m2"]
    assert routes[0] == Route("groq", "https://api.groq.com/openai/v1", "stt-key", "g1")
    assert routes[2].api_key == "or-key" and routes[2].base_url == "https://openrouter.ai/api/v1"
    assert "stt-key" not in repr(routes[0])  # keys never end up in logs via repr


def test_routes_explicit_groq_key_wins():
    assert routes_from(groq_settings(groq_api_key="groq-key"))[0].api_key == "groq-key"


def test_routes_stt_key_of_another_provider_is_not_sent_to_groq():
    routes = routes_from(groq_settings(stt_base_url="https://api.openai.com/v1"))
    assert [r.provider for r in routes] == ["openrouter", "openrouter"]


def test_routes_without_groq_models_or_keys():
    assert [r.name for r in routes_from(groq_settings(groq_models=[]))] == ["openrouter/m1", "openrouter/m2"]
    assert [r.name for r in routes_from(groq_settings(openrouter_api_key=""))] == ["groq/g1", "groq/g2"]
    assert routes_from(groq_settings(stt_api_key="", openrouter_api_key="")) == []


async def test_no_keys_at_all_raises_without_requests():
    calls = []
    c = LLMClient(groq_settings(stt_api_key="", openrouter_api_key=""),
                  httpx.AsyncClient(transport=httpx.MockTransport(lambda r: calls.append(r) or reply("ok"))))
    with pytest.raises(LLMError, match="GROQ_API_KEY"):
        await c.parse_message("hi", [])
    with pytest.raises(LLMError, match="OPENROUTER_API_KEY"):
        await c.complete_text(MSGS)
    assert calls == []


class Recorder:
    """MockTransport handler: answers per route name ("groq/g1"), records (route name, auth, json_mode)."""

    def __init__(self, answers: dict[str, object]):
        self.answers = answers
        self.calls: list[tuple[str, str, bool]] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        provider = "groq" if req.url.host == "api.groq.com" else "openrouter"
        name = f"{provider}/{body['model']}"
        self.calls.append((name, req.headers["Authorization"], "response_format" in body))
        answer = self.answers.get(name, json.dumps(GOOD))
        return answer if isinstance(answer, httpx.Response) else reply(answer)

    @property
    def names(self) -> list[str]:
        return [n for n, _, _ in self.calls]


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def groq_client(rec: Recorder, clock: Clock | None = None, **kw) -> LLMClient:
    return LLMClient(groq_settings(**kw), httpx.AsyncClient(transport=httpx.MockTransport(rec)), clock or Clock())


async def test_groq_is_used_first_with_its_key_and_json_mode():
    rec = Recorder({})
    assert (await groq_client(rec).parse_message("hi", [])).kind == "unknown"
    assert rec.calls == [("groq/g1", "Bearer stt-key", True)]


async def test_groq_429_moves_to_next_groq_model():
    rec = Recorder({"groq/g1": httpx.Response(429, json={"error": {"message": "Please try again in 7.5s"}})})
    assert (await groq_client(rec).parse_message("hi", [])).kind == "unknown"
    assert rec.names == ["groq/g1", "groq/g2"]  # no waiting for retry-after, no second try of g1


async def test_openrouter_is_the_fallback_after_groq():
    rec = Recorder({"groq/g1": httpx.Response(429), "groq/g2": httpx.Response(503)})
    assert await groq_client(rec).complete_text(MSGS) == json.dumps(GOOD)
    assert rec.names == ["groq/g1", "groq/g2", "groq/g2", "openrouter/m1"]  # 5xx gets one retry
    assert rec.calls[-1][1] == "Bearer or-key"


async def test_429_cooldown_skips_route_for_60_seconds():
    clock = Clock()
    rec = Recorder({"groq/g1": httpx.Response(429)})
    c = groq_client(rec, clock)
    await c.parse_message("hi", [])
    assert rec.names == ["groq/g1", "groq/g2"]
    clock.t += RATE_LIMIT_COOLDOWN - 1
    await c.parse_message("hi", [])
    assert rec.names[2:] == ["groq/g2"]  # g1 is cooling down: not even asked
    clock.t += 2
    rec.answers = {}
    await c.parse_message("hi", [])
    assert rec.names[3:] == ["groq/g1"]  # the window is over


async def test_all_routes_cooling_down_fails_fast():
    clock = Clock()
    rec = Recorder({name: httpx.Response(429) for name in ("groq/g1", "groq/g2", "openrouter/m1", "openrouter/m2")})
    c = groq_client(rec, clock)
    with pytest.raises(LLMError, match="all models failed"):
        await c.parse_message("hi", [])
    assert len(rec.calls) == 4
    with pytest.raises(LLMError, match="all models failed"):
        await c.complete_text(MSGS)
    assert len(rec.calls) == 4  # no requests while every route is cooling down


async def test_json_mode_memory_is_per_route():
    rec = Recorder({})

    def handler(req):
        body = json.loads(req.content)
        if req.url.host == "api.groq.com" and "response_format" in body:
            return httpx.Response(400, json=NOVITA_400)
        return rec(req)

    c = LLMClient(groq_settings(groq_models=["m1"]), httpx.AsyncClient(transport=httpx.MockTransport(handler)), Clock())
    await c.parse_message("hi", [])
    assert rec.calls == [("groq/m1", "Bearer stt-key", False)]
    assert c._no_json_mode == {"groq/m1"}  # openrouter/m1 (same model id) still gets json mode


async def test_think_block_is_ignored_when_parsing():
    think = '<think>maybe {"kind": "workout"}</think>' + json.dumps(GOOD)
    rec = Recorder({"groq/g1": think})
    assert (await groq_client(rec).parse_message("hi", [])).kind == "unknown"


# ---- rate limits: when a 429'd route may be tried again ----


@pytest.mark.parametrize(
    ("value", "seconds"),
    [("7", 7.0), ("7.66s", 7.66), ("2m59.56s", 179.56), ("1h2m", 3720.0), ("250ms", 0.25), ("", None),
     ("soon", None), (None, None)],
)
def test_parse_duration(value, seconds):
    got = parse_duration(value)
    assert got == pytest.approx(seconds) if seconds is not None else got is None


@pytest.mark.parametrize(
    ("headers", "text", "seconds"),
    [
        ({"retry-after": "7"}, "", 7.0),
        ({"retry-after": "3", "x-ratelimit-reset-tokens": "40s"}, "", 3.0),  # retry-after wins
        ({"x-ratelimit-remaining-requests": "12", "x-ratelimit-reset-tokens": "7.66s",
          "x-ratelimit-reset-requests": "2m59.56s"}, "", 7.66),  # tokens per minute ran out
        ({"x-ratelimit-remaining-requests": "0", "x-ratelimit-reset-tokens": "7.66s",
          "x-ratelimit-reset-requests": "2m59.56s"}, "", 179.56),  # requests ran out
        ({}, '{"error": {"message": "Rate limit reached. Please try again in 1m26.4s."}}', 86.4),
        ({}, "", RATE_LIMIT_COOLDOWN),
        ({"retry-after": "0.2"}, "", MIN_COOLDOWN),
        ({"retry-after": str(10 * 24 * 3600)}, "", MAX_COOLDOWN),
    ],
)
def test_cooldown_after(headers, text, seconds):
    assert cooldown_after(httpx.Response(429, headers=headers, text=text)) == pytest.approx(seconds)


async def test_429_cooldown_follows_retry_after():
    clock = Clock()
    rec = Recorder({"groq/g1": httpx.Response(429, headers={"retry-after": "7"})})
    c = groq_client(rec, clock)
    await c.parse_message("hi", [])
    assert rec.names == ["groq/g1", "groq/g2"]
    clock.t += 6
    await c.parse_message("hi", [])
    assert rec.names[2:] == ["groq/g2"]  # still cooling down
    clock.t += 2
    rec.answers = {}
    await c.parse_message("hi", [])
    assert rec.names[3:] == ["groq/g1"]  # 8 s later the better model is back, not after a whole minute


async def test_every_route_failed_waits_for_the_soonest_reset():
    clock, slept = Clock(), []
    rec = Recorder({
        "groq/g1": httpx.Response(429, headers={"retry-after": "2"}),
        "groq/g2": httpx.Response(429, headers={"retry-after": "30"}),
        "openrouter/m1": httpx.Response(503),
        "openrouter/m2": httpx.Response(429),
    })

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        clock.t += seconds
        rec.answers = {}  # the limit has reset

    c = LLMClient(groq_settings(), httpx.AsyncClient(transport=httpx.MockTransport(rec)), clock, sleep)
    assert (await c.parse_message("hi", [])).kind == "unknown"
    assert slept == [2.0]
    assert rec.names == ["groq/g1", "groq/g2", "openrouter/m1", "openrouter/m1", "openrouter/m2", "groq/g1"]


async def test_no_wait_when_the_soonest_reset_is_far():
    slept = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    rec = Recorder({name: httpx.Response(429, headers={"retry-after": "20"})
                    for name in ("groq/g1", "groq/g2", "openrouter/m1", "openrouter/m2")})
    c = LLMClient(groq_settings(), httpx.AsyncClient(transport=httpx.MockTransport(rec)), Clock(), sleep)
    with pytest.raises(LLMError, match="all models failed"):
        await c.complete_text(MSGS)
    assert slept == [] and len(rec.calls) == 4


async def test_usage_is_logged_without_content(caplog):
    caplog.set_level("INFO", logger="gymbot.llm.openrouter")
    body = {"choices": [{"message": {"content": "секретный ответ"}}], "usage": {"prompt_tokens": 2418, "completion_tokens": 95}}
    rec = Recorder({"groq/g1": httpx.Response(200, json=body)})
    assert await groq_client(rec).complete_text(MSGS) == "секретный ответ"
    assert "groq/g1 tokens: prompt 2418, completion 95" in caplog.text
    assert "секретный" not in caplog.text and "stt-key" not in caplog.text



async def test_markup_only_answer_and_empty_choices_fail_over():
    """An answer made of markup only ("** **") or a 200 with no choices goes to the next route."""
    models = []

    def handler(req):
        models.append(json.loads(req.content)["model"])
        if len(models) == 1:
            return reply("** **")
        if len(models) == 2:
            return httpx.Response(200, json={"choices": []})
        return reply("ответ")

    assert await client_with(handler).complete_text([{"role": "user", "content": "q"}]) == "ответ"
    assert len(models) == 3
