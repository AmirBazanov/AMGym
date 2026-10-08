"""/llm and /llm test (handlers/llm_status.py): owner only, route states, today's usage and cost, the probe."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import anthropic
import httpx
import httpx2
from conftest import make_settings

from gymbot.handlers import llm_status as handler
from gymbot.llm import claude
from gymbot.llm.openrouter import LLMClient

KEY = "sk-ant-status-secret"
USAGE = {"input_tokens": 1000, "output_tokens": 500, "cache_read_input_tokens": 10_000, "cache_creation_input_tokens": 0}
CREDITS_400 = {"type": "error", "error": {"type": "invalid_request_error", "message": "Your credit balance is too low"}}
NOW = datetime(2026, 10, 8, 10, 0, tzinfo=UTC)  # 13:00 in Moscow


def claude_message(text: str) -> dict:
    return {
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
        "content": [{"type": "thinking", "thinking": "", "signature": "s"}, {"type": "text", "text": text}],
        "stop_reason": "end_turn", "stop_sequence": None, "usage": USAGE,
    }


class Clock:
    def __init__(self) -> None:
        self.t = 500.0

    def __call__(self) -> float:
        return self.t


def make_llm(tmp_path, claude_answers: list, **kw) -> tuple[LLMClient, Clock]:
    answers = list(claude_answers)

    def claude_handler(request: httpx2.Request) -> httpx2.Response:
        answer = answers.pop(0)
        return answer if isinstance(answer, httpx2.Response) else httpx2.Response(200, json=answer)

    def other(request: httpx.Request) -> httpx.Response:
        content = json.dumps({"kind": "unknown", "clarification": "?"})
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}],
                                         "usage": {"prompt_tokens": 300, "completion_tokens": 40}})

    settings = make_settings(
        tmp_path, allowed_user_ids=[42], timezone="Europe/Moscow", anthropic_api_key=KEY,
        groq_api_key="gk", groq_models=["q1"], vision_models=["v1"], **kw,
    )
    sdk = anthropic.AsyncAnthropic(
        api_key=KEY, base_url=claude.API_URL, max_retries=0,
        http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(claude_handler)),
    )
    clock = Clock()
    llm = LLMClient(settings, httpx.AsyncClient(transport=httpx.MockTransport(other)), clock,
                    claude_client=sdk, now=lambda: NOW + timedelta(seconds=clock.t - 500))
    return llm, clock


def make_message(user_id: int):
    answers: list[str] = []

    async def answer(text, *a, **kw):
        answers.append(text)

    msg = SimpleNamespace(
        from_user=SimpleNamespace(id=user_id, full_name="Amir"),
        chat=SimpleNamespace(id=user_id, type="private"),
        bot=SimpleNamespace(send_chat_action=AsyncMock()),
        answer=answer,
    )
    return msg, answers


def command(args: str | None = None):
    return SimpleNamespace(args=args)


async def test_status_lists_routes_usage_cost_and_the_memory_note(tmp_path, db):
    llm, clock = make_llm(tmp_path, [claude_message("{\"kind\": \"unknown\"}"), httpx2.Response(400, json=CREDITS_400)])
    await llm.parse_message("привет", [])  # Claude answers
    clock.t += 60
    await llm.parse_message("ещё", [])  # Claude is out of credits, Groq answers
    msg, answers = make_message(42)

    await handler.llm_status(msg, command(), llm.s, db, llm)

    text = answers[0]
    assert text.splitlines()[:3] == [
        "Нейросети по порядку (текст):",
        "1. anthropic/claude-opus-5-5: нет кредитов до 14:01",
        "2. groq/q1: ок",
    ]
    assert "Фото:" in text
    assert "Последним ответил groq/q1 в 13:01." in text
    # 1000 + 10 000 cached input, 500 output: 1000*4 + 10000*0.2 + 500*20 = 16 000 per million = $0.016;
    # the failed call (400) has no usage: one call, one failure
    assert "anthropic/claude-opus-5-5: вызовов 1, ошибок 1, вход 11 000, из кэша 10 000, в кэш 0, выход 500, $0.0160" \
        in text
    assert "groq/q1: вызовов 1, вход 300, выход 40, $0.0000" in text
    assert "Claude за октябрь: $0.0160." in text
    assert "обнуляются при перезапуске" in text
    assert KEY not in text and "привет" not in text


async def test_status_before_any_call(tmp_path, db):
    llm, _ = make_llm(tmp_path, [])
    msg, answers = make_message(42)
    await handler.llm_status(msg, command(), llm.s, db, llm)
    assert "Ответов ещё не было." in answers[0] and "вызовов не было" in answers[0]
    assert "Claude за октябрь: $0.0000." in answers[0]


async def test_status_says_claude_is_switched_off(tmp_path, db):
    llm, _ = make_llm(tmp_path, [], anthropic_enabled=False)
    msg, answers = make_message(42)
    await handler.llm_status(msg, command(), llm.s, db, llm)
    assert "1. groq/q1: ок" in answers[0]
    assert "Claude выключен (ANTHROPIC_ENABLED=false)." in answers[0]


async def test_status_auth_failure_until_restart(tmp_path, db):
    unauthorized = httpx2.Response(401, json={"type": "error", "error": {"type": "authentication_error", "message": "x"}})
    llm, _ = make_llm(tmp_path, [unauthorized])
    await llm.parse_message("привет", [])
    msg, answers = make_message(42)
    await handler.llm_status(msg, command(), llm.s, db, llm)
    assert "1. anthropic/claude-opus-5-5: ключ не принят, выключен до перезапуска" in answers[0]


async def test_status_without_any_key(tmp_path, db):
    settings = make_settings(tmp_path, allowed_user_ids=[42])
    msg, answers = make_message(42)
    await handler.llm_status(msg, command(), settings, db, LLMClient(settings))
    assert answers == [handler.NO_ROUTES]


async def test_non_owner_gets_nothing(tmp_path, db):
    llm, _ = make_llm(tmp_path, [])
    msg, answers = make_message(77)
    await handler.llm_status(msg, command("test"), llm.s, db, llm)
    assert answers == [handler.OWNER_ONLY]
    assert llm.stats.today == {}  # the probe did not run


async def test_probe_reports_route_latency_tokens_cost(tmp_path, db):
    llm, _ = make_llm(tmp_path, [claude_message("Ок.")])
    msg, answers = make_message(42)
    await handler.llm_status(msg, command(" Test "), llm.s, db, llm)
    assert answers == [
        "Проверка anthropic/claude-opus-5-5: ответ «Ок.» за 0,0 с, вход 11 000, выход 500, $0.0160."
    ]
    msg.bot.send_chat_action.assert_awaited()  # "typing" while the model thinks


async def test_probe_reports_the_api_error_message(tmp_path, db):
    llm, _ = make_llm(tmp_path, [httpx2.Response(400, json=CREDITS_400)])
    msg, answers = make_message(42)
    await handler.llm_status(msg, command("test"), llm.s, db, llm)
    assert answers[0].startswith("Проверка anthropic/claude-opus-5-5: ошибка BadRequestError 400: ")
    assert "credit balance" in answers[0].lower()  # the API's own reason, to diagnose without a server shell


async def test_counters_roll_over_at_local_midnight(tmp_path, db):
    llm, clock = make_llm(tmp_path, [claude_message("{\"kind\": \"unknown\"}")])
    await llm.parse_message("привет", [])
    clock.t += 12 * 3600  # 01:00 next day in Moscow
    day, today, month = llm.stats.snapshot()
    assert day.isoformat() == "2026-10-09" and today == {}
    assert month["anthropic"] > 0  # the month goes on
