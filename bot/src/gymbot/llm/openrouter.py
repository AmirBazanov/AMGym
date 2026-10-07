"""Minimal chat LLM client over OpenAI-compatible APIs: Groq first, OpenRouter as the last fallback.

A route is (provider, base_url, api_key, model); routes are built from Settings in order: every
GROQ_MODELS model (if there is a Groq key), then OPENROUTER_MODEL and its fallbacks (if there is an
OpenRouter key). Free models are rate-limited and sometimes return prose around the JSON, so we:
  1. ask for json_object output; some providers reject it with 400 ("does not support
     structured-outputs"), then we retry without it and remember that for the route,
  2. extract the first {...} block,
  3. validate with pydantic and retry once / fall back to the next route.
A route that answered 429 is skipped until its limit resets (`cooldown_after`): Groq says when in
`retry-after` or `x-ratelimit-reset-tokens` / `-requests` ("7.66s", "2m59.56s"); without them
RATE_LIMIT_COOLDOWN. Groq's per-minute token limit refills in seconds, so a blanket minute would send a whole
minute of messages to the weaker OpenRouter models. When every route failed and the soonest rate-limited one
frees up within MAX_WAIT seconds, it is tried once more after that wait instead of failing.
400/401/402/403/404/413 also move straight to the next route. Token usage per call is logged (no content).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TypeVar

import httpx
from pydantic import ValidationError

from gymbot.config import Settings
from gymbot.llm.prompts import build_messages
from gymbot.llm.schemas import ParseResult

log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    pass


def extract_json(content: str, prefer: str = "kind") -> dict:
    """First JSON object in the text that has the key `prefer` (a ParseResult by default), else the first
    object at all (models may add prose or reasoning)."""
    decoder = json.JSONDecoder()
    first: dict | None = None
    for m in re.finditer(r"\{", content):
        try:
            obj, _ = decoder.raw_decode(content, m.start())
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            if prefer in obj:
                return obj
            first = first or obj
    if first is None:
        raise LLMError(f"no JSON in model output: {content[:200]!r}")
    return first


_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_FENCE = re.compile(r"^```[\w-]*\s*$", re.MULTILINE)
_HEADING = re.compile(r"^#{1,6}\s*", re.MULTILINE)


def clean_text(content: str) -> str:
    """Plain text for a Telegram message sent without parse_mode: no reasoning, fences or Markdown marks."""
    text = _THINK.sub("", content)
    text = _FENCE.sub("", text)
    text = _HEADING.sub("", text)
    text = text.replace("**", "").replace("__", "")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _rejects_json_mode(resp: httpx.Response) -> bool:
    """A 400 about response_format, not e.g. about context length (that one is the next model's job)."""
    text = resp.text.lower()
    return any(word in text for word in ("response_format", "structured", "json"))


T = TypeVar("T")

RATE_LIMIT_COOLDOWN = 60.0  # seconds a route is skipped after a 429 that does not say when it resets
MIN_COOLDOWN = 1.0
MAX_COOLDOWN = 24 * 3600.0  # a daily quota may reset in hours: retrying every minute would only hit it again
MAX_WAIT = 3.0  # seconds worth waiting for a rate-limited route when every route failed
NEXT_ROUTE_STATUSES = (400, 401, 402, 403, 404, 413, 429)  # bad request / key / quota / model gone / too big


_DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)(ms|h|m|s)")
_TRY_AGAIN = re.compile(r"try again in\s+((?:\d+(?:\.\d+)?(?:ms|h|m|s))+)", re.IGNORECASE)


def parse_duration(value: str | None) -> float | None:
    """Seconds in a Go-style duration ("7.66s", "2m59.56s", "1h2m", "250ms") or a plain number of seconds."""
    if not value:
        return None
    value = value.strip()
    try:
        return float(value)
    except ValueError:
        pass
    parts = _DURATION_PART.findall(value)
    if not parts or "".join(n + u for n, u in parts) != value:
        return None
    scale = {"h": 3600.0, "m": 60.0, "s": 1.0, "ms": 0.001}
    return sum(float(n) * scale[u] for n, u in parts)


def cooldown_after(resp: httpx.Response) -> float:
    """Seconds until a 429'd route may be tried again: `retry-after`, else the reset of the exhausted limit
    (x-ratelimit-reset-requests when no requests remain, else -tokens), else "try again in 7.5s" from the
    error text, else RATE_LIMIT_COOLDOWN; clamped to [MIN_COOLDOWN, MAX_COOLDOWN]."""
    h = resp.headers
    found = parse_duration(h.get("retry-after"))
    if found is None:
        requests_out = h.get("x-ratelimit-remaining-requests", "").strip() == "0"
        first, second = ("requests", "tokens") if requests_out else ("tokens", "requests")
        found = parse_duration(h.get(f"x-ratelimit-reset-{first}")) or parse_duration(h.get(f"x-ratelimit-reset-{second}"))
    if found is None and (m := _TRY_AGAIN.search(resp.text)):
        found = parse_duration(m.group(1))
    if found is None:
        found = RATE_LIMIT_COOLDOWN
    return min(max(found, MIN_COOLDOWN), MAX_COOLDOWN)


@dataclass(frozen=True)
class Route:
    provider: str  # "groq" | "openrouter"
    base_url: str
    api_key: str = field(repr=False)  # never in logs or reprs
    model: str = ""

    @property
    def name(self) -> str:
        return f"{self.provider}/{self.model}"


def routes_from(settings: Settings) -> list[Route]:
    """Groq models first (if there is a key), then the OpenRouter models (if there is a key)."""
    routes: list[Route] = []
    if groq_key := settings.groq_key:
        routes += [Route("groq", settings.groq_base_url, groq_key, m) for m in settings.groq_models]
    if settings.openrouter_api_key:
        models = [settings.openrouter_model, *settings.openrouter_fallback_models]
        routes += [Route("openrouter", settings.openrouter_base_url, settings.openrouter_api_key, m) for m in models]
    return routes


class _EmptyAnswer(LLMError):
    """The model answered with nothing usable; retrying the same route rarely helps."""


class OpenRouterClient:
    """One instance per process: it keeps the HTTP connection pool and what it learned about routes.

    The name is historical: it talks to Groq and OpenRouter (see `LLMClient`).
    """

    def __init__(
        self,
        settings: Settings,
        http: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
    ):
        self.s = settings
        self.http = http or httpx.AsyncClient(timeout=60)
        self.routes = routes_from(settings)
        self._clock = clock
        self._sleep = sleep
        self._no_json_mode: set[str] = set()  # route names that returned 400 on response_format
        self._cooldown_until: dict[str, float] = {}  # route name -> clock() when it may be tried again

    async def aclose(self) -> None:
        await self.http.aclose()

    async def _complete(
        self,
        route: Route,
        messages: list[dict[str, str]],
        json_mode: bool,
        *,
        temperature: float = 0,
        use_reasoning: bool = True,
    ) -> str:
        body: dict = {"model": route.model, "messages": messages, "temperature": temperature}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        headers = {"Authorization": f"Bearer {route.api_key}"}
        if route.provider == "openrouter":
            headers["X-Title"] = "GymAPP"
        resp = await self.http.post(f"{route.base_url}/chat/completions", headers=headers, json=body)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(usage := data.get("usage"), dict):
            log.info(
                "llm %s tokens: prompt %s, completion %s",
                route.name, usage.get("prompt_tokens"), usage.get("completion_tokens"),
            )
        message = data["choices"][0]["message"]
        if not use_reasoning:
            return message.get("content") or ""
        # Reasoning models sometimes leave `content` empty and put the answer after their reasoning.
        return message.get("content") or message.get("reasoning") or ""

    async def _over_routes(self, call: Callable[[Route, bool], Awaitable[T]], *, json_mode: bool) -> T:
        """Try `call(route, json_mode)` on every route in order, two attempts each, then the soonest
        rate-limited route once more if it frees up within MAX_WAIT; see the module doc."""
        if not self.routes:
            raise LLMError("no LLM API key: set GROQ_API_KEY (or STT_API_KEY) or OPENROUTER_API_KEY")
        last_err: Exception | None = None
        for route in self.routes:
            if self._cooldown_until.get(route.name, 0) > self._clock():
                last_err = last_err or LLMError(f"{route.name} is rate-limited")
                continue
            done, result, last_err = await self._try_route(route, call, json_mode, last_err)
            if done:
                return result  # type: ignore[return-value]
        now = self._clock()
        waits = [
            (until - now, i) for i, r in enumerate(self.routes) if (until := self._cooldown_until.get(r.name, 0)) > now
        ]
        if waits and (soonest := min(waits))[0] <= MAX_WAIT:
            route = self.routes[soonest[1]]
            log.info("llm: every route failed, waiting %.1f s for %s", soonest[0], route.name)
            await self._sleep(soonest[0])
            done, result, last_err = await self._try_route(route, call, json_mode, last_err)
            if done:
                return result  # type: ignore[return-value]
        raise LLMError(f"all models failed: {last_err}")

    async def _try_route(
        self,
        route: Route,
        call: Callable[[Route, bool], Awaitable[T]],
        json_mode: bool,
        last_err: Exception | None,
    ) -> tuple[bool, T | None, Exception | None]:
        """(answered, the answer, the last error) after up to two attempts on `route`."""
        for _attempt in range(2):
            use_json = json_mode and route.name not in self._no_json_mode
            try:
                return True, await call(route, use_json), last_err
            except httpx.HTTPStatusError as e:
                status = e.response.status_code
                log.warning("llm %s failed: %s", route.name, status)
                last_err = e
                if status == 400 and use_json and _rejects_json_mode(e.response):
                    # Provider rejects response_format (e.g. Novita: "does not support structured-outputs").
                    self._no_json_mode.add(route.name)
                    continue
                if status == 429:
                    wait = cooldown_after(e.response)
                    self._cooldown_until[route.name] = self._clock() + wait
                    log.info("llm %s rate-limited for %.1f s", route.name, wait)
                if status in NEXT_ROUTE_STATUSES:
                    break
            except _EmptyAnswer as e:
                log.warning("llm %s failed: %s", route.name, e)
                last_err = e
                break
            except (httpx.HTTPError, LLMError, ValidationError, ValueError, KeyError, TypeError) as e:
                log.warning("llm %s failed: %s", route.name, e)
                last_err = e
        return False, None, last_err

    async def complete_text(self, messages: list[dict[str, str]], temperature: float = 0.3) -> str:
        """Plain text generation (no JSON mode) with the same route fallback as parse_message.

        Empty `content` counts as a failure: `reasoning` is the model's chain of thought, not an answer.
        """

        async def call(route: Route, _json_mode: bool) -> str:
            raw = await self._complete(route, messages, False, temperature=temperature, use_reasoning=False)
            text = clean_text(raw)
            if not text:
                raise _EmptyAnswer("empty answer")
            return text

        return await self._over_routes(call, json_mode=False)

    async def parse_message(
        self,
        text: str,
        catalog: list[str],
        history: list[tuple[str, str]] | None = None,
        facts: list[str] | None = None,
    ) -> ParseResult:
        """Parse one chat message; `history` is (user text, assistant JSON) turns of the recent dialog,
        `facts` the user's active facts (see gymbot.services.facts)."""
        messages = build_messages(text, catalog, history, facts)

        async def call(route: Route, json_mode: bool) -> ParseResult:
            content = _THINK.sub("", await self._complete(route, messages, json_mode))  # qwen may think aloud
            return ParseResult.model_validate(extract_json(content))

        return await self._over_routes(call, json_mode=True)


    async def complete_json(self, messages: list[dict[str, str]], prefer: str = "kind") -> dict:
        """A JSON object from the model (json mode where supported), with the same route fallback.

        Only "is it a JSON object" is checked here; the caller validates the content itself, so a wrong
        but well-formed answer costs one request, not a retry on every route. `prefer`: the top-level key of
        the expected answer, so an inner object with a "kind" key is not taken for it (see extract_json).
        """

        async def call(route: Route, json_mode: bool) -> dict:
            return extract_json(_THINK.sub("", await self._complete(route, messages, json_mode)), prefer)

        return await self._over_routes(call, json_mode=True)


LLMClient = OpenRouterClient  # the provider-neutral name
