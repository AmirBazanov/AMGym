"""Claude as the first LLM route (gymbot.llm.claude + openrouter routing): request shape, answers, failures.

No network: Claude goes through the official SDK over an httpx2 MockTransport, Groq/OpenRouter over httpx's.
"""

import json
import logging
import math

import anthropic
import httpx
import httpx2
import pytest

from gymbot.config import Settings
from gymbot.llm import claude, structured
from gymbot.llm.openrouter import (
    AUTH_FAILED,
    NO_CREDITS,
    OK,
    OVERLOADED,
    RATE_LIMITED,
    LLMClient,
    LLMError,
    routes_from,
    vision_routes_from,
)
from gymbot.llm.prompts import ANSWER_SYSTEM_PROMPT, EXAMPLES, VISION_SYSTEM, build_answer_messages

KEY = "sk-ant-test-secret"
GOOD = {"kind": "unknown", "clarification": "что?"}
FOOD = {"foods": [{"description": "плов", "grams": 300, "kcal": 540, "protein_g": 18, "fat_g": 21, "carbs_g": 69}]}
B64 = "aGVsbG8="
USAGE = {"input_tokens": 120, "output_tokens": 80, "cache_read_input_tokens": 2000, "cache_creation_input_tokens": 0}
CREDITS_400 = {
    "type": "error",
    "error": {
        "type": "invalid_request_error",
        "message": "Your credit balance is too low to access the Anthropic API. Please go to Plans & Billing.",
    },
}


def settings(**kw) -> Settings:
    base = {
        "bot_token": "1:a",
        "anthropic_api_key": KEY,
        "groq_api_key": "gk",
        "groq_models": ["q1"],
        "vision_models": ["v1"],
        "openrouter_api_key": "ok",
        "openrouter_model": "m1",
        "openrouter_fallback_models": [],
        "openrouter_vision_models": ["ov1"],
    }
    return Settings(_env_file=None, **{**base, **kw})


def message(text: str | dict, stop: str = "end_turn", usage: dict | None = None, thinking: bool = True) -> dict:
    content = [{"type": "thinking", "thinking": "", "signature": "sig"}] if thinking else []
    if text is not None:
        content.append({"type": "text", "text": text if isinstance(text, str) else json.dumps(text)})
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5-5",
        "content": content,
        "stop_reason": stop,
        "stop_sequence": None,
        "usage": usage or USAGE,
    }


def openai_reply(content: dict | str) -> httpx.Response:
    text = content if isinstance(content, str) else json.dumps(content)
    return httpx.Response(
        200, json={"choices": [{"message": {"content": text}}], "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
    )


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class Harness:
    """Claude answers from `claude_answers` (dicts = 200 bodies, httpx2.Response, or exceptions to raise);
    the OpenAI-compatible routes answer `fallback`. Records both sides."""

    def __init__(self, claude_answers: list, fallback=None, clock: Clock | None = None, **kw):
        self.claude_answers = list(claude_answers)
        self.claude_bodies: list[dict] = []
        self.claude_headers: list[httpx2.Headers] = []
        self.other: list[tuple[str, dict]] = []
        self.fallback = fallback if fallback is not None else GOOD
        self.clock = clock or Clock()

        def claude_handler(request: httpx2.Request) -> httpx2.Response:
            self.claude_bodies.append(json.loads(request.content))
            self.claude_headers.append(request.headers)
            answer = self.claude_answers.pop(0) if self.claude_answers else message(GOOD)
            if isinstance(answer, Exception):
                raise answer
            if isinstance(answer, httpx2.Response):
                return answer
            return httpx2.Response(200, json=answer)

        def other_handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            self.other.append((request.url.host, body))
            return openai_reply(self.fallback)

        sdk = anthropic.AsyncAnthropic(
            api_key=KEY,
            base_url=claude.API_URL,
            max_retries=0,
            http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(claude_handler)),
        )
        http = httpx.AsyncClient(transport=httpx.MockTransport(other_handler))
        self.client = LLMClient(settings(**kw), http, self.clock, claude_client=sdk)


def error(status: int, body: dict | None = None, headers: dict | None = None) -> httpx2.Response:
    body = body or {"type": "error", "error": {"type": "api_error", "message": "boom"}}
    return httpx2.Response(status, json=body, headers=headers or {})


# ---- routes ----


def test_claude_goes_first_in_both_route_lists():
    s = settings()
    assert [r.name for r in routes_from(s)] == ["anthropic/claude-opus-5-5", "groq/q1", "openrouter/m1"]
    assert [r.name for r in vision_routes_from(s)] == ["anthropic/claude-opus-5-5", "groq/v1", "openrouter/ov1"]
    assert routes_from(s)[0].base_url == "https://api.anthropic.com"
    assert KEY not in repr(routes_from(s)[0])


@pytest.mark.parametrize("kw", [{"anthropic_api_key": ""}, {"anthropic_enabled": False}])
def test_no_claude_route_without_key_or_when_disabled(kw):
    s = settings(**kw)
    assert all(not r.is_claude for r in routes_from(s) + vision_routes_from(s))
    assert routes_from(s)[0].name == "groq/q1"


def test_model_from_settings():
    assert routes_from(settings(anthropic_model="claude-sonnet-5-5"))[0].model == "claude-sonnet-5-5"


async def test_no_claude_request_without_key():
    h = Harness([], anthropic_api_key="")
    assert (await h.client.parse_message("привет", [])).kind == "unknown"
    assert h.claude_bodies == []
    assert [host for host, _ in h.other] == ["api.groq.com"]


# ---- request shape ----


async def test_parse_request_shape_and_cache_breakpoints():
    h = Harness([message(GOOD)])
    await h.client.parse_message("жим 3 по 10 на 60", ["жим лёжа"], facts=["самса ~150 г"])
    body = h.claude_bodies[0]
    assert body["model"] == "claude-opus-5-5"
    assert body["output_config"]["effort"] == "low"
    assert body["output_config"]["format"] == {"type": "json_schema", "schema": structured.PARSE}
    for banned in ("temperature", "top_p", "top_k", "thinking", "response_format"):
        assert banned not in body
    assert body["max_tokens"] == claude.PURPOSES["parse"].max_tokens
    # The rules (with the catalog) are cached on their own; the user's facts follow in a separate block.
    rules, facts = body["system"]
    assert rules["cache_control"] == {"type": "ephemeral"} and "жим лёжа" in rules["text"]
    assert "самса ~150 г" in facts["text"] and "самса" not in rules["text"].split("Порции")[0]
    # Few-shot examples as turns; the breakpoint is on the last example, the real message comes after it.
    turns = body["messages"]
    assert len(turns) == 2 * len(EXAMPLES) + 1
    marked = [i for i, t in enumerate(turns) if any("cache_control" in b for b in t["content"])]
    assert marked == [2 * len(EXAMPLES) - 1]
    assert turns[-1] == {"role": "user", "content": [{"type": "text", "text": "жим 3 по 10 на 60"}]}
    breakpoints = json.dumps(body).count('"cache_control"')
    assert breakpoints <= 4
    assert h.claude_headers[0]["x-api-key"] == KEY
    assert h.other == []


async def test_parse_rules_block_is_the_same_bytes_for_different_messages():
    h = Harness([message(GOOD), message(GOOD)])
    await h.client.parse_message("съел 2 яйца", ["жим лёжа"], facts=["самса ~150 г"])
    await h.client.parse_message("съел 3 яйца", ["жим лёжа"], facts=["манты ~90 г/шт", "самса ~150 г"])
    a, b = h.claude_bodies
    assert a["system"][0] == b["system"][0]  # the cached prefix does not depend on the facts
    assert a["messages"][: 2 * len(EXAMPLES)] == b["messages"][: 2 * len(EXAMPLES)]


async def test_answer_uses_medium_effort_no_schema_and_caches_prompt_and_summary():
    h = Harness([message("Сегодня жим 3×8 на 80 кг.")])
    messages = build_answer_messages("Профиль: 85 кг", "что сегодня?", [("а вчера?", "отдых")])
    text = await h.client.complete_text(messages, temperature=0.4)
    assert text == "Сегодня жим 3×8 на 80 кг."
    body = h.claude_bodies[0]
    assert body["output_config"] == {"effort": "medium"}
    assert "temperature" not in body
    prompt, summary = body["system"]
    assert prompt["text"] == ANSWER_SYSTEM_PROMPT and prompt["cache_control"] == {"type": "ephemeral"}
    assert "Профиль: 85 кг" in summary["text"] and summary["cache_control"] == {"type": "ephemeral"}
    assert [t["role"] for t in body["messages"]] == ["user", "assistant", "user"]


@pytest.mark.parametrize(
    ("purpose", "effort", "schema"),
    [
        ("settings", "low", structured.SETTINGS),
        ("baselines", "low", structured.BASELINES),
        ("lookup", "low", structured.LOOKUP),
        ("plan", "medium", structured.PLAN),
        ("json", "low", None),
    ],
)
async def test_complete_json_effort_and_schema_per_purpose(purpose, effort, schema):
    h = Harness([message({"actions": []})])
    await h.client.complete_json([{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
                                 prefer="actions", purpose=purpose)
    config = h.claude_bodies[0]["output_config"]
    assert config["effort"] == effort
    assert config.get("format") == (None if schema is None else {"type": "json_schema", "schema": schema})


async def test_advice_purpose_is_medium():
    h = Harness([message("Питание\n- добери 40 г белка")])
    await h.client.complete_text([{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
                                 purpose="advice")
    assert h.claude_bodies[0]["output_config"] == {"effort": "medium"}


async def test_photo_sends_base64_image_block_first():
    h = Harness([message(FOOD)])
    parsed = await h.client.parse_photo(B64, "image/jpeg", "250 г", ["самса ~150 г"])
    assert parsed.result.foods[0].description == "плов"
    body = h.claude_bodies[0]
    assert body["output_config"]["effort"] == "low"
    assert body["output_config"]["format"]["schema"] == structured.PHOTO
    image, text = body["messages"][0]["content"]
    assert image == {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": B64}}
    assert text == {"type": "text", "text": "Подпись: 250 г"}
    assert body["system"][0] == {"type": "text", "text": VISION_SYSTEM, "cache_control": {"type": "ephemeral"}}
    assert "самса ~150 г" in body["system"][1]["text"]


async def test_photo_label_with_schema_nulls():
    label = {"name": "Йогурт", "brand": None, "per100": {"kcal": 60, "protein_g": 5, "fat_g": 2, "carbs_g": 6},
             "net_weight_g": 150, "serving_g": None}
    h = Harness([message({"foods": [], "note": None, "label": label})])
    parsed = await h.client.parse_photo(B64)
    assert parsed.label is not None and parsed.label.per100.kcal == 60


async def test_photo_without_label_reads_foods_even_with_label_null():
    h = Harness([message({**FOOD, "note": None, "label": None})])
    parsed = await h.client.parse_photo(B64)
    assert parsed.label is None and parsed.result.foods[0].grams == 300


# ---- answers ----


async def test_thinking_blocks_before_text_are_skipped():
    h = Harness([message({"kind": "food", "foods": FOOD["foods"]})])
    result = await h.client.parse_message("плов", [])
    assert result.kind == "food" and result.foods[0].kcal == 540


async def test_refusal_goes_to_next_route_without_cooldown():
    body = message(None, stop="refusal")
    body["stop_details"] = {"type": "refusal", "category": "bio", "explanation": None}
    h = Harness([body])
    assert (await h.client.parse_message("привет", [])).kind == "unknown"
    assert [host for host, _ in h.other] == ["api.groq.com"]
    assert h.client.status(h.client.routes[0]).state == OK


async def test_max_tokens_goes_to_next_route():
    h = Harness([message('{"kind": "fo', stop="max_tokens")])
    assert (await h.client.parse_message("привет", [])).kind == "unknown"
    assert len(h.claude_bodies) == 1 and len(h.other) == 1


async def test_text_without_text_block_goes_to_next_route():
    h = Harness([message(None)])
    assert await h.client.complete_text([{"role": "user", "content": "u"}]) == json.dumps(GOOD)
    assert len(h.other) == 1


async def test_invalid_json_is_not_retried_on_claude():
    h = Harness([message('{"kind": "nonsense"}')])
    assert (await h.client.parse_message("привет", [])).kind == "unknown"
    assert len(h.claude_bodies) == 1  # one Claude attempt, then the free route


# ---- failures ----


async def test_credit_balance_400_pauses_claude_for_an_hour():
    clock = Clock()
    h = Harness([error(400, CREDITS_400)], clock=clock)
    assert (await h.client.parse_message("привет", [])).kind == "unknown"
    st = h.client.status(h.client.routes[0])
    assert st.state == NO_CREDITS and st.until is not None
    await h.client.parse_message("ещё", [])
    assert len(h.claude_bodies) == 1  # skipped while paused
    clock.t += claude.CREDIT_COOLDOWN + 1
    await h.client.parse_message("и ещё", [])
    assert len(h.claude_bodies) == 2


async def test_billing_error_402_counts_as_out_of_credits():
    h = Harness([error(402, {"type": "error", "error": {"type": "billing_error", "message": "billing"}})])
    await h.client.complete_text([{"role": "user", "content": "u"}])
    assert h.client.status(h.client.routes[0]).state == NO_CREDITS


async def test_vision_shares_the_credit_pause():
    h = Harness([error(400, CREDITS_400)], fallback={"kind": "food", **FOOD})
    await h.client.parse_message("привет", [])
    await h.client.parse_photo(B64)
    assert len(h.claude_bodies) == 1


async def test_429_uses_retry_after():
    clock = Clock()
    h = Harness([error(429, {"type": "error", "error": {"type": "rate_limit_error", "message": "slow"}},
                       {"retry-after": "17"})], clock=clock)
    assert (await h.client.parse_message("привет", [])).kind == "unknown"
    assert h.client._cooldown_until["anthropic/claude-opus-5-5"] == pytest.approx(clock.t + 17)
    assert h.client.status(h.client.routes[0]).state == RATE_LIMITED


async def test_529_overloaded_short_pause():
    clock = Clock()
    h = Harness([error(529, {"type": "error", "error": {"type": "overloaded_error", "message": "busy"}})], clock=clock)
    await h.client.parse_message("привет", [])
    assert h.client._cooldown_until["anthropic/claude-opus-5-5"] == pytest.approx(clock.t + claude.OVERLOAD_COOLDOWN)
    assert h.client.status(h.client.routes[0]).state == OVERLOADED


async def test_500_goes_to_next_route_without_pause():
    h = Harness([error(500)])
    assert (await h.client.parse_message("привет", [])).kind == "unknown"
    assert h.client.status(h.client.routes[0]).state == OK


@pytest.mark.parametrize("status", [401, 403])
async def test_auth_error_disables_claude_until_restart_and_logs_once(status, caplog):
    caplog.set_level(logging.INFO)
    h = Harness([error(status, {"type": "error", "error": {"type": "authentication_error", "message": "bad"}})])
    for _ in range(3):
        assert (await h.client.parse_message("привет", [])).kind == "unknown"
    assert len(h.claude_bodies) == 1
    st = h.client.status(h.client.routes[0])
    assert st.state == AUTH_FAILED and st.until is None
    assert math.isinf(h.client._cooldown_until["anthropic/claude-opus-5-5"])
    assert sum("key was rejected" in r.getMessage() for r in caplog.records) == 1
    assert KEY not in caplog.text


async def test_timeout_goes_to_next_route():
    h = Harness([httpx2.ReadTimeout("slow")])
    assert (await h.client.parse_message("привет", [])).kind == "unknown"
    assert len(h.claude_bodies) == 1 and len(h.other) == 1


async def test_connection_error_goes_to_next_route():
    h = Harness([httpx2.ConnectError("down")])
    assert await h.client.complete_text([{"role": "user", "content": "u"}]) == json.dumps(GOOD)


async def test_schema_rejection_retries_without_schema_and_remembers_it():
    bad = {"type": "error", "error": {"type": "invalid_request_error",
                                      "message": "output_config.format.schema: Schema is too complex"}}
    h = Harness([error(400, bad), message({"actions": []}), message({"actions": []}), message(GOOD)])
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    assert await h.client.complete_json(msgs, prefer="actions", purpose="settings") == {"actions": []}
    assert "format" in h.claude_bodies[0]["output_config"]
    assert "format" not in h.claude_bodies[1]["output_config"]
    await h.client.complete_json(msgs, prefer="actions", purpose="settings")
    assert "format" not in h.claude_bodies[2]["output_config"]  # remembered for this call type
    await h.client.parse_message("привет", [])
    assert "format" in h.claude_bodies[3]["output_config"]  # other call types keep their schema
    assert h.other == []


async def test_other_400_goes_to_next_route():
    h = Harness([error(400, {"type": "error", "error": {"type": "invalid_request_error", "message": "too long"}})])
    assert (await h.client.parse_message("привет", [])).kind == "unknown"
    assert h.client.status(h.client.routes[0]).state == OK


async def test_fallback_order_claude_groq_openrouter():
    calls: list[str] = []
    h = Harness([error(500)])

    def other(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        if "groq" in request.url.host:
            return httpx.Response(429, headers={"retry-after": "30"})
        return openai_reply(GOOD)

    h.client.http = httpx.AsyncClient(transport=httpx.MockTransport(other))
    assert (await h.client.parse_message("привет", [])).kind == "unknown"
    assert len(h.claude_bodies) == 1
    assert calls == ["api.groq.com", "openrouter.ai"]


async def test_all_routes_fail_raises_llm_error():
    h = Harness([error(500)])
    h.client.http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    with pytest.raises(LLMError):
        await h.client.complete_text([{"role": "user", "content": "u"}])


# ---- usage, cost, logs ----


def test_cost_of_opus_5_5_call():
    usage = claude.usage_of(
        "claude-opus-5-5",
        anthropic.types.Usage(input_tokens=1000, output_tokens=500, cache_read_input_tokens=10_000,
                              cache_creation_input_tokens=2000),
    )
    # 1000*4 + 2000*5 + 10000*0.2 + 500*20 = 26 000 per million
    assert usage.cost == pytest.approx(0.026)
    assert usage.prompt == 13_000


async def test_usage_log_has_tokens_and_cost_but_no_content_or_key(caplog):
    caplog.set_level(logging.INFO)
    h = Harness([message(GOOD)])
    await h.client.parse_message("секретная самса", [])
    line = next(r.getMessage() for r in caplog.records if "tokens" in r.getMessage())
    assert line == (
        "llm anthropic/claude-opus-5-5 parse tokens: prompt 2120 (cache read 2000, write 0), completion 80, $0.0025"
    )
    assert "секретная" not in caplog.text and KEY not in caplog.text


async def test_stats_count_calls_tokens_cost_and_last_route():
    h = Harness([message(GOOD), error(500)])
    await h.client.parse_message("a", [])
    await h.client.parse_message("b", [])  # Claude fails, Groq answers
    _, today, month = h.client.stats.snapshot()
    c = today["anthropic/claude-opus-5-5"]
    assert (c.calls, c.failures, c.input, c.output, c.cache_read) == (1, 1, 2120, 80, 2000)
    assert c.cost == pytest.approx(0.00248)
    assert today["groq/q1"].calls == 1 and today["groq/q1"].cost == 0
    assert month["anthropic"] == pytest.approx(0.00248)
    assert h.client.stats.last is not None and h.client.stats.last.route == "groq/q1"


# ---- probe (/llm test) ----


async def test_probe_success_reports_usage_and_clears_pause():
    clock = Clock()
    h = Harness([error(400, CREDITS_400), message("ок", thinking=False)], clock=clock)
    await h.client.parse_message("привет", [])
    assert h.client.status(h.client.routes[0]).state == NO_CREDITS
    probe = await h.client.probe()
    assert probe.route == "anthropic/claude-opus-5-5" and probe.error is None and probe.answer == "ок"
    assert probe.usage is not None and probe.usage.output == 80
    assert h.claude_bodies[-1]["output_config"] == {"effort": "low"}
    assert h.client.status(h.client.routes[0]).state == OK


async def test_probe_failure_reports_class_only():
    h = Harness([error(401, {"type": "error", "error": {"type": "authentication_error", "message": KEY}})])
    probe = await h.client.probe()
    assert probe.error == "AuthenticationError" and probe.answer is None
    assert h.client.status(h.client.routes[0]).state == AUTH_FAILED


async def test_probe_without_routes():
    h = Harness([], anthropic_api_key="", groq_api_key="", openrouter_api_key="", vision_models=[])
    assert (await h.client.probe()).route is None


# ---- schemas ----


@pytest.mark.parametrize("name", ["PARSE", "PHOTO", "SETTINGS", "BASELINES", "LOOKUP", "PLAN"])
def test_schemas_are_strict(name):
    def walk(node):
        if isinstance(node, dict):
            assert "const" not in node
            if node.get("type") == "object":
                assert node.get("additionalProperties") is False
            for key in ("minimum", "maximum", "minLength", "maxLength"):
                assert key not in node
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(getattr(structured, name))


async def test_failed_claude_answer_logs_the_class_not_the_content(caplog):
    caplog.set_level(logging.INFO)
    h = Harness([message('{"kind": "food", "foods": [{"description": "секретная самса", "kcal": -5}]}')])
    assert (await h.client.parse_message("секретная самса", [])).kind == "unknown"  # Groq answered
    assert "llm anthropic/claude-opus-5-5 failed: ValidationError" in caplog.text
    assert "секретная" not in caplog.text
