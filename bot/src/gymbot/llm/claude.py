"""Claude (Anthropic Messages API, official `anthropic` SDK) as an LLM route: request shape, answer, cost.

The route machinery (fallback, cooldowns, stats) lives in gymbot.llm.openrouter; this module only knows Claude:
- Calls arrive as OpenAI-style chat messages (gymbot.llm.prompts builds them for every provider) and are
  converted here: system messages -> `system` text blocks, image_url data URLs -> base64 image blocks (before the
  text of the turn).
- claude-opus-5-5 always thinks: no `thinking` field (disabled or a budget is a 400), depth via
  `output_config.effort` per call type (PURPOSES). No temperature/top_p (400 on this model), no assistant prefill.
- JSON calls send `output_config.format` with the call's schema (gymbot.llm.structured); the callers still
  validate. A 400 about the schema switches that call type to plain JSON instructions (openrouter._try_route).
- Prompt caching: cache_control on the stable system prompt (`stable_system`, the part before per-user facts or
  the diary summary), on the last system block, and on the last stable chat turn (`stable_messages`: the parser's
  few-shot examples). Minimum cacheable prefix on Opus 5.5 is 512 tokens; shorter prefixes just don't cache.
- The answer: `stop_reason` is checked before the content (refusal and max_tokens raise NoAnswer, the route
  fails over); thinking blocks (empty under the default display) are skipped, text blocks joined.
- Cost per call from `usage` (input, output incl. thinking, cache reads and 5-minute cache writes).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

import anthropic

from gymbot.llm import structured
from gymbot.llm.prompts import ANSWER_SYSTEM_PROMPT, VISION_SYSTEM

API_URL = "https://api.anthropic.com"  # explicit: the SDK would otherwise follow ANTHROPIC_BASE_URL from the env
CREDIT_COOLDOWN = 3600.0  # seconds Claude is skipped after "credit balance is too low"
OVERLOAD_COOLDOWN = 30.0  # 529 overloaded without retry-after
EPHEMERAL = {"type": "ephemeral"}  # 5-minute TTL: messages of one meal or one workout come minutes apart
MAX_BREAKPOINTS = 4

Effort = Literal["low", "medium", "high"]


@dataclass(frozen=True)
class Purpose:
    """One call type: how hard Claude thinks, how long we wait, what JSON comes back."""

    name: str
    effort: Effort
    max_tokens: int  # thinking counts toward it
    timeout: float  # seconds per request; the route fails over after it
    schema: dict[str, Any] | None = None
    stable_system: str | None = None  # the cacheable head of the system prompt when the rest varies


# low: structured extraction where the latency is felt (the user waits for a preview); medium: free-text
# reasoning over the diary. Timeouts: low effort answers in seconds; medium thinks longer.
PURPOSES: dict[str, Purpose] = {
    p.name: p
    for p in (
        Purpose("parse", "low", 4096, 30.0, structured.PARSE),  # chat parser, saved-edit re-estimate, repair
        Purpose("photo", "low", 4096, 40.0, structured.PHOTO, VISION_SYSTEM),
        Purpose("settings", "low", 4096, 30.0, structured.SETTINGS),
        Purpose("baselines", "low", 4096, 30.0, structured.BASELINES),
        Purpose("lookup", "low", 4096, 30.0, structured.LOOKUP),
        Purpose("json", "low", 4096, 30.0),  # complete_json without a known schema
        Purpose("plan", "low", 4096, 25.0, structured.PLAN),  # the Mini App waits for it: keep it short
        Purpose("answer", "medium", 8192, 60.0, None, ANSWER_SYSTEM_PROMPT),
        Purpose("advice", "medium", 8192, 60.0),
        Purpose("probe", "low", 1024, 30.0),  # /llm test
    )
}

# USD per million tokens: (input, output, cache read). 5-minute cache writes cost 1.25x input.
PRICES: dict[str, tuple[float, float, float]] = {
    "claude-opus-5-5": (4.0, 20.0, 0.20),
}
DEFAULT_PRICE = PRICES["claude-opus-5-5"]  # an unknown model is priced as Opus 5.5 (an estimate)


class NoAnswer(RuntimeError):
    """The model answered with nothing usable (refusal, cut at max_tokens, no text)."""


@dataclass(frozen=True)
class Usage:
    input: int = 0  # uncached input tokens
    output: int = 0  # output tokens, thinking included
    cache_read: int = 0
    cache_write: int = 0
    cost: float = 0.0  # USD

    @property
    def prompt(self) -> int:
        return self.input + self.cache_read + self.cache_write


def usage_of(model: str, usage: Any) -> Usage:
    inp = int(getattr(usage, "input_tokens", 0) or 0)
    out = int(getattr(usage, "output_tokens", 0) or 0)
    read = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
    write = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
    p_in, p_out, p_read = PRICES.get(model, DEFAULT_PRICE)
    cost = (inp * p_in + write * p_in * 1.25 + read * p_read + out * p_out) / 1_000_000
    return Usage(inp, out, read, write, cost)


# ---- request ----

_DATA_URL = re.compile(r"^data:(image/[\w.+-]+);base64,(.*)$", re.DOTALL)


def _text(text: str, cache: bool = False) -> dict[str, Any]:
    block: dict[str, Any] = {"type": "text", "text": text}
    if cache:
        block["cache_control"] = dict(EPHEMERAL)
    return block


def _content(content: str | list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Blocks of one turn; images go first (Claude reads them better before the question)."""
    if isinstance(content, str):
        return [_text(content)]
    images: list[dict[str, Any]] = []
    texts: list[dict[str, Any]] = []
    for part in content:
        if part.get("type") == "text":
            texts.append(_text(part["text"]))
        elif part.get("type") == "image_url":
            url = part["image_url"]["url"]
            if m := _DATA_URL.match(url):
                images.append({"type": "image", "source": {"type": "base64", "media_type": m[1], "data": m[2]}})
            else:
                images.append({"type": "image", "source": {"type": "url", "url": url}})
    return images + texts


def convert(
    messages: list[dict[str, Any]], stable_system: str | None = None, stable_messages: int = 0
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(system blocks, messages) for the Messages API with cache breakpoints at the stability boundaries.

    `stable_system`: the head of the system text that never changes (split off into its own cached block);
    `stable_messages`: how many leading OpenAI messages (system included) are stable, e.g. system + few-shot
    examples; the last of them gets a breakpoint.
    """
    system: list[dict[str, Any]] = []
    turns: list[dict[str, Any]] = []
    marks = 0
    for i, m in enumerate(messages):
        if m["role"] == "system":
            text = m["content"]
            if stable_system and text.startswith(stable_system) and text[len(stable_system):].strip():
                system += [_text(stable_system, cache=True), _text(text[len(stable_system):])]
                marks += 1
            elif text.strip():
                system.append(_text(text))
            continue
        blocks = _content(m["content"])
        if i == stable_messages - 1 and blocks and marks < MAX_BREAKPOINTS - 1:
            blocks[-1]["cache_control"] = dict(EPHEMERAL)
            marks += 1
        turns.append({"role": m["role"], "content": blocks})
    if system and "cache_control" not in system[-1] and marks < MAX_BREAKPOINTS:
        system[-1]["cache_control"] = dict(EPHEMERAL)
    return system, turns


def request(
    model: str,
    messages: list[dict[str, Any]],
    purpose: Purpose,
    *,
    use_schema: bool,
    stable_system: str | None = None,
    stable_messages: int = 0,
) -> dict[str, Any]:
    """Keyword arguments for `messages.create` (no temperature, no thinking: see the module doc)."""
    system, turns = convert(messages, stable_system or purpose.stable_system, stable_messages)
    output_config: dict[str, Any] = {"effort": purpose.effort}
    if use_schema and purpose.schema is not None:
        output_config["format"] = {"type": "json_schema", "schema": purpose.schema}
    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": purpose.max_tokens,
        "messages": turns,
        "output_config": output_config,
        "timeout": purpose.timeout,
    }
    if system:
        kwargs["system"] = system
    return kwargs


# ---- answer ----


def answer_text(message: Any) -> str:
    """The text of a Message; NoAnswer on refusal, max_tokens or a stop that is not an answer."""
    stop = message.stop_reason
    if stop == "refusal":
        details = getattr(message, "stop_details", None)
        raise NoAnswer(f"refusal ({getattr(details, 'category', None) or 'no category'})")
    if stop == "max_tokens":
        raise NoAnswer("cut at max_tokens")
    if stop not in ("end_turn", "stop_sequence"):
        raise NoAnswer(f"stop_reason {stop}")
    text = "".join(block.text for block in message.content if block.type == "text")
    if not text.strip():
        raise NoAnswer("no text")
    return text


# ---- errors ----


def error_type(e: anthropic.APIStatusError) -> str | None:
    body = e.body
    if isinstance(body, dict) and isinstance(err := body.get("error"), dict):
        kind = err.get("type")
        return kind if isinstance(kind, str) else None
    return None


def out_of_credits(e: anthropic.APIStatusError) -> bool:
    """The prepaid API credits are spent: 400 "Your credit balance is too low…" or a 402 billing_error."""
    return e.status_code == 402 or error_type(e) == "billing_error" or "credit balance" in e.message.lower()


def rejects_schema(e: anthropic.APIStatusError) -> bool:
    """A 400 about the structured-output schema (too complex, unsupported keyword), not about the prompt."""
    text = e.message.lower()
    return e.status_code == 400 and any(w in text for w in ("schema", "output_config", "output_format"))
