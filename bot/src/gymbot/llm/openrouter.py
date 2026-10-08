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
400/401/402/403/404/413 also move straight to the next route, and so does a timeout (a hung free endpoint
would only hang again). Token usage per call is logged (no content, never an image).

Food photos (`parse_photo`) go over their own routes (`vision_routes_from`: VISION_MODELS on Groq, then
OPENROUTER_VISION_MODELS) with a short prompt and a VISION_TIMEOUT per request; the text routes reject images.
A model shared by both lists (qwen on Groq) shares its 429 cooldown too, as it shares the quota.

Claude (ANTHROPIC_API_KEY, gymbot.llm.claude) goes first in both lists, over the official SDK with
max_retries=0 (the route fallback is the retry). Its failures map onto the same machinery: 429 -> cooldown from
retry-after; "credit balance is too low" (the owner's prepaid credits are spent) -> skipped for CREDIT_COOLDOWN;
401/403 -> skipped until restart (logged once); 529 -> short cooldown; a refusal, max_tokens, a timeout or a
connection error -> the next route at once. One attempt per call on Claude (a retry costs seconds and money),
except a 400 about the JSON schema: that call type then goes without the schema. Usage and USD cost of every
call go to the log line and to `stats` (/llm).
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, TypeVar
from zoneinfo import ZoneInfo

import anthropic
import httpx
from pydantic import ValidationError

from gymbot.config import Settings
from gymbot.llm import claude
from gymbot.llm.claude import PURPOSES, Purpose, Usage
from gymbot.llm.prompts import EXAMPLES, build_messages, build_vision_messages, parser_system
from gymbot.llm.schemas import ParsedLabel, ParseResult, PhotoParse
from gymbot.llm.stats import LLMStats
from gymbot.services.tg_format import plain

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


def clean_text(content: str) -> str:
    """The model's text without reasoning and code fences. Bold, lists and headers stay: the callers make
    Telegram HTML of them (gymbot.services.tg_html) and a plain version for checks and the fallback
    (gymbot.services.tg_format.plain)."""
    text = _THINK.sub("", content)
    text = _FENCE.sub("", text)
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
VISION_TIMEOUT = 20.0  # seconds per photo request: Groq answers in 1-2 s, a stuck free endpoint must not hold the user


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
    provider: str  # "anthropic" | "groq" | "openrouter"
    base_url: str
    api_key: str = field(repr=False)  # never in logs or reprs
    model: str = ""

    @property
    def name(self) -> str:
        return f"{self.provider}/{self.model}"

    @property
    def is_claude(self) -> bool:
        """Anthropic Messages API (gymbot.llm.claude); every other provider speaks OpenAI chat completions."""
        return self.provider == "anthropic"


def _claude_routes(settings: Settings) -> list[Route]:
    key = settings.claude_key
    return [Route("anthropic", claude.API_URL, key, settings.anthropic_model)] if key else []


def routes_from(settings: Settings) -> list[Route]:
    """Claude first (if there is a key and it is on), then the Groq models, then the OpenRouter models."""
    routes = _claude_routes(settings)
    if groq_key := settings.groq_key:
        routes += [Route("groq", settings.groq_base_url, groq_key, m) for m in settings.groq_models]
    if settings.openrouter_api_key:
        models = [settings.openrouter_model, *settings.openrouter_fallback_models]
        routes += [Route("openrouter", settings.openrouter_base_url, settings.openrouter_api_key, m) for m in models]
    return routes


def vision_routes_from(settings: Settings) -> list[Route]:
    """Routes that accept images: Claude, then VISION_MODELS on Groq, then OPENROUTER_VISION_MODELS (same keys)."""
    routes = _claude_routes(settings)
    if groq_key := settings.groq_key:
        routes += [Route("groq", settings.groq_base_url, groq_key, m) for m in settings.vision_models]
    if settings.openrouter_api_key:
        routes += [
            Route("openrouter", settings.openrouter_base_url, settings.openrouter_api_key, m)
            for m in settings.openrouter_vision_models
        ]
    return routes


class _EmptyAnswer(LLMError):
    """The model answered with nothing usable; retrying the same route rarely helps."""


# Why a route is skipped (RouteStatus.state; "ok" = it is tried).
OK, RATE_LIMITED, NO_CREDITS, AUTH_FAILED, OVERLOADED = "ok", "rate", "credits", "auth", "overloaded"


@dataclass(frozen=True)
class RouteStatus:
    route: Route
    state: str
    until: datetime | None = None  # UTC; None for "ok" and for "until restart"


@dataclass(frozen=True)
class Probe:
    """/llm test: one tiny call to the first route."""

    route: str | None
    seconds: float = 0.0
    answer: str | None = None
    usage: Usage | None = None
    error: str | None = None  # the exception class; for Claude HTTP errors also the API's message (secrets cut)


PROBE_TEXT = "Ответь одним словом: ок"


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


class OpenRouterClient:
    """One instance per process: it keeps the HTTP connection pools and what it learned about routes.

    The name is historical: it talks to Claude, Groq and OpenRouter (see `LLMClient`).
    """

    def __init__(
        self,
        settings: Settings,
        http: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
        *,
        claude_client: anthropic.AsyncAnthropic | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self.s = settings
        self.http = http or httpx.AsyncClient(timeout=60)
        self.routes = routes_from(settings)
        self.vision_routes = vision_routes_from(settings)
        self._clock = clock
        self._sleep = sleep
        self._now = now
        self._claude = claude_client  # created on the first Claude call
        self._no_json_mode: set[str] = set()  # route names (Claude: route#purpose) without JSON mode / schema
        self._cooldown_until: dict[str, float] = {}  # route name -> clock() when it may be tried again
        self._reason: dict[str, str] = {}  # route name -> why it is cooling down
        self.stats = LLMStats(ZoneInfo(settings.timezone), now)

    @property
    def claude_api(self) -> anthropic.AsyncAnthropic:
        if self._claude is None:
            # max_retries=0: the route fallback is the retry; the SDK's own backoff would hold the user for minutes.
            self._claude = anthropic.AsyncAnthropic(
                api_key=self.s.claude_key, base_url=claude.API_URL, max_retries=0, timeout=60.0
            )
        return self._claude

    async def aclose(self) -> None:
        await self.http.aclose()
        if self._claude is not None:
            await self._claude.close()

    async def _complete(
        self,
        route: Route,
        messages: list[dict[str, Any]],
        json_mode: bool,
        *,
        purpose: Purpose = PURPOSES["json"],
        temperature: float = 0,
        use_reasoning: bool = True,
        timeout: float | None = None,
        stable_system: str | None = None,
        stable_messages: int = 0,
    ) -> str:
        """The model's answer text. `messages` content is a string or, for images, a list of parts;
        `timeout` overrides the client's default for this request (Claude: `purpose.timeout`). `purpose` and
        the stable parts matter to Claude only (effort, schema, cache breakpoints); temperature to the others."""
        if route.is_claude:
            return await self._complete_claude(route, messages, json_mode, purpose, stable_system, stable_messages)
        body: dict = {"model": route.model, "messages": messages, "temperature": temperature}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        headers = {"Authorization": f"Bearer {route.api_key}"}
        if route.provider == "openrouter":
            headers["X-Title"] = "GymAPP"
        extra = {} if timeout is None else {"timeout": timeout}
        resp = await self.http.post(f"{route.base_url}/chat/completions", headers=headers, json=body, **extra)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(usage := data.get("usage"), dict):
            log.info(
                "llm %s tokens: prompt %s, completion %s",
                route.name, usage.get("prompt_tokens"), usage.get("completion_tokens"),
            )
            free = Usage(input=_int(usage.get("prompt_tokens")), output=_int(usage.get("completion_tokens")))
            self.stats.record(route.name, route.provider, free)
        choices = data.get("choices") or []
        if not choices:
            raise LLMError(f"{route.name}: no choices in the answer")
        message = choices[0]["message"]
        if not use_reasoning:
            return message.get("content") or ""
        # Reasoning models sometimes leave `content` empty and put the answer after their reasoning.
        return message.get("content") or message.get("reasoning") or ""

    async def _complete_claude(
        self,
        route: Route,
        messages: list[dict[str, Any]],
        json_mode: bool,
        purpose: Purpose,
        stable_system: str | None,
        stable_messages: int,
    ) -> str:
        kwargs = claude.request(
            route.model, messages, purpose,
            use_schema=json_mode, stable_system=stable_system, stable_messages=stable_messages,
        )
        # An organization-level key must name the workspace on every request (a 400 otherwise).
        if workspace := self.s.anthropic_workspace_id.strip():
            kwargs["extra_headers"] = {**kwargs.get("extra_headers", {}), "anthropic-workspace-id": workspace}
        message = await self.claude_api.messages.create(**kwargs)
        usage = claude.usage_of(route.model, message.usage)
        log.info(
            "llm %s %s tokens: prompt %s (cache read %s, write %s), completion %s, $%.4f",
            route.name, purpose.name, usage.prompt, usage.cache_read, usage.cache_write, usage.output, usage.cost,
        )
        self.stats.record(route.name, route.provider, usage)
        try:
            return claude.answer_text(message)
        except claude.NoAnswer as e:
            raise _EmptyAnswer(str(e)) from e

    def _pause(self, route: Route, seconds: float, reason: str) -> None:
        self._cooldown_until[route.name] = self._clock() + seconds
        self._reason[route.name] = reason

    def _claude_failed(self, route: Route, e: anthropic.APIStatusError) -> bool:
        """Note a Claude HTTP error; True when it is about the route (limits, credits, key), not the request."""
        status = e.status_code
        log.warning("llm %s failed: %s %s: %s", route.name, status, claude.error_type(e) or "", claude.error_detail(e))
        if isinstance(e, anthropic.RateLimitError):
            wait = cooldown_after(e.response)  # type: ignore[arg-type]  # httpx2.Response, same interface
            self._pause(route, wait, RATE_LIMITED)
            log.info("llm %s rate-limited for %.1f s", route.name, wait)
        elif claude.out_of_credits(e):
            self._pause(route, claude.CREDIT_COOLDOWN, NO_CREDITS)
            log.warning("llm %s: API credits are spent, skipped for %.0f min", route.name, claude.CREDIT_COOLDOWN / 60)
        elif isinstance(e, anthropic.AuthenticationError | anthropic.PermissionDeniedError):
            self._pause(route, math.inf, AUTH_FAILED)
            log.error("llm %s: the API key was rejected (%s), skipped until restart", route.name, status)
        elif status == 529:
            wait = parse_duration(e.response.headers.get("retry-after")) or claude.OVERLOAD_COOLDOWN
            self._pause(route, min(max(wait, MIN_COOLDOWN), MAX_COOLDOWN), OVERLOADED)
        else:
            return False
        return True

    def _json_key(self, route: Route, purpose: Purpose) -> str:
        return f"{route.name}#{purpose.name}" if route.is_claude else route.name

    async def _over_routes(
        self,
        call: Callable[[Route, bool], Awaitable[T]],
        *,
        json_mode: bool,
        routes: list[Route] | None = None,
        purpose: Purpose = PURPOSES["json"],
    ) -> T:
        """Try `call(route, json_mode)` on every route in order (`routes`, default the text routes), two
        attempts each (one on Claude), then the soonest rate-limited route once more if it frees up within
        MAX_WAIT; see the module doc."""
        routes = self.routes if routes is None else routes
        if not routes:
            raise LLMError(
                "no LLM API key: set ANTHROPIC_API_KEY, GROQ_API_KEY (or STT_API_KEY) or OPENROUTER_API_KEY"
            )
        last_err: Exception | None = None
        for route in routes:
            if self._cooldown_until.get(route.name, 0) > self._clock():
                last_err = last_err or LLMError(f"{route.name} is rate-limited")
                continue
            done, result, last_err = await self._try_route(route, call, json_mode, last_err, purpose)
            if done:
                return result  # type: ignore[return-value]
        now = self._clock()
        waits = [
            (until - now, i) for i, r in enumerate(routes) if (until := self._cooldown_until.get(r.name, 0)) > now
        ]
        if waits and (soonest := min(waits))[0] <= MAX_WAIT:
            route = routes[soonest[1]]
            log.info("llm: every route failed, waiting %.1f s for %s", soonest[0], route.name)
            await self._sleep(soonest[0])
            done, result, last_err = await self._try_route(route, call, json_mode, last_err, purpose)
            if done:
                return result  # type: ignore[return-value]
        raise LLMError(f"all models failed: {last_err}")

    async def _try_route(
        self,
        route: Route,
        call: Callable[[Route, bool], Awaitable[T]],
        json_mode: bool,
        last_err: Exception | None,
        purpose: Purpose = PURPOSES["json"],
    ) -> tuple[bool, T | None, Exception | None]:
        """(answered, the answer, the last error) after up to two attempts on `route` (one on Claude, plus a
        retry without the schema when Claude rejects it)."""
        for _attempt in range(2):
            key = self._json_key(route, purpose)
            use_json = json_mode and key not in self._no_json_mode
            try:
                result = await call(route, use_json)
            except anthropic.APIStatusError as e:
                self.stats.failure(route.name)
                last_err = e
                if self._claude_failed(route, e) or not (use_json and claude.rejects_schema(e)):
                    break
                self._no_json_mode.add(key)  # this call type goes without the schema from now on
                log.info("llm %s: schema rejected for %s, plain JSON instructions instead", route.name, purpose.name)
                continue
            except anthropic.APIError as e:  # connection errors and timeouts, an unreadable response
                log.warning("llm %s failed: %s", route.name, type(e).__name__)
                self.stats.failure(route.name)
                last_err = e
                break
            except httpx.HTTPStatusError as e:
                status = e.response.status_code
                log.warning("llm %s failed: %s", route.name, status)
                self.stats.failure(route.name)
                last_err = e
                if status == 400 and use_json and _rejects_json_mode(e.response):
                    # Provider rejects response_format (e.g. Novita: "does not support structured-outputs").
                    self._no_json_mode.add(key)
                    continue
                if status == 429:
                    wait = cooldown_after(e.response)
                    self._pause(route, wait, RATE_LIMITED)
                    log.info("llm %s rate-limited for %.1f s", route.name, wait)
                if status in NEXT_ROUTE_STATUSES:
                    break
            except (_EmptyAnswer, httpx.TimeoutException) as e:
                # Retrying the same route rarely helps: an empty answer repeats, a hung endpoint hangs again.
                log.warning("llm %s failed: %s", route.name, type(e).__name__ if isinstance(e, httpx.HTTPError) else e)
                self.stats.failure(route.name)
                last_err = e
                break
            except (httpx.HTTPError, LLMError, ValidationError, ValueError, KeyError, TypeError) as e:
                # Claude's answers echo the user's words (a ValidationError prints its input): the class only.
                log.warning("llm %s failed: %s", route.name, type(e).__name__ if route.is_claude else e)
                self.stats.failure(route.name)
                last_err = e
                if route.is_claude:  # a second Claude attempt costs seconds and money; the next route is free
                    break
            else:
                self._reason.pop(route.name, None)
                self.stats.answered(route.name)
                return True, result, last_err
        return False, None, last_err

    def status(self, route: Route) -> RouteStatus:
        """Whether `route` is tried now, and if not, why and until when (UTC)."""
        until, now = self._cooldown_until.get(route.name, 0.0), self._clock()
        if until <= now:
            return RouteStatus(route, OK)
        reason = self._reason.get(route.name, RATE_LIMITED)
        wall = None if math.isinf(until) else self._now() + timedelta(seconds=until - now)
        return RouteStatus(route, reason, wall)

    async def probe(self) -> Probe:
        """One tiny call to the first text route, whatever its status (a success clears its pause: credits
        topped up, a new key); any failure is reported by its exception class only."""
        if not self.routes:
            return Probe(None, error="no routes")
        route = self.routes[0]
        purpose = PURPOSES["probe"]
        start = self._clock()
        self.stats.last_usage = None
        try:
            text = await self._complete(
                route, [{"role": "user", "content": PROBE_TEXT}], False,
                purpose=purpose, use_reasoning=False, timeout=purpose.timeout,
            )
            if not clean_text(text):
                raise _EmptyAnswer("empty answer")
        except Exception as e:  # noqa: BLE001 - the probe reports any failure, by class
            self.stats.failure(route.name)
            if isinstance(e, anthropic.APIStatusError):
                self._claude_failed(route, e)
            elif isinstance(e, httpx.HTTPStatusError) and e.response.status_code == 429:
                self._pause(route, cooldown_after(e.response), RATE_LIMITED)
            error = type(e).__name__
            if isinstance(e, anthropic.APIStatusError):
                error += f" {e.status_code}: {claude.error_detail(e)}"
            return Probe(route.name, self._clock() - start, error=error)
        self._cooldown_until.pop(route.name, None)
        self._reason.pop(route.name, None)
        self.stats.answered(route.name)
        return Probe(route.name, self._clock() - start, clean_text(text)[:40], self.stats.last_usage)

    async def complete_text(
        self, messages: list[dict[str, str]], temperature: float = 0.3, *, purpose: str = "answer"
    ) -> str:
        """Plain text generation (no JSON mode) with the same route fallback as parse_message.

        Empty `content` counts as a failure: `reasoning` is the model's chain of thought, not an answer.
        `purpose` ("answer", "advice") picks Claude's effort and timeout; temperature goes to the others only.
        """
        spec = PURPOSES[purpose]

        async def call(route: Route, _json_mode: bool) -> str:
            raw = await self._complete(
                route, messages, False, purpose=spec, temperature=temperature, use_reasoning=False
            )
            text = clean_text(raw)
            if not text or not plain(text).strip():
                raise _EmptyAnswer("empty answer")
            return text

        return await self._over_routes(call, json_mode=False, purpose=spec)

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
        spec = PURPOSES["parse"]
        # Claude caches the rules (without the facts) and, behind them, the few-shot examples.
        stable_system = parser_system(text, catalog, history)
        stable_messages = 1 + 2 * len(EXAMPLES)

        async def call(route: Route, json_mode: bool) -> ParseResult:
            raw = await self._complete(
                route, messages, json_mode,
                purpose=spec, stable_system=stable_system, stable_messages=stable_messages,
            )
            content = _THINK.sub("", raw)  # qwen may think aloud
            return ParseResult.model_validate(extract_json(content))

        return await self._over_routes(call, json_mode=True, purpose=spec)

    async def parse_photo(
        self,
        image_b64: str,
        mime: str = "image/jpeg",
        caption: str = "",
        facts: list[str] | None = None,
    ) -> PhotoParse:
        """Foods on a photo (`result`, kind="food"; empty `foods` when the model sees no food), or the numbers
        of a package label (`label`; then `result.foods` is empty). A label wins over foods in one answer.

        Only the vision routes are tried. `caption` is the user's hint ("17 штук, 250 г"), `facts` the user's
        active facts (portion sizes). The image goes to the provider only: never logged or stored.
        """
        messages = build_vision_messages(f"data:{mime};base64,{image_b64}", caption, facts)
        spec = PURPOSES["photo"]

        async def call(route: Route, json_mode: bool) -> PhotoParse:
            raw = await self._complete(
                route, messages, json_mode, purpose=spec, use_reasoning=False, timeout=VISION_TIMEOUT
            )
            # A broken answer goes straight to the next route: a retry would spend another ~2K image tokens
            # of the per-minute quota that text parsing shares. An unclear label is still an answer: the
            # handler asks for another photo (gymbot.services.products.check_label).
            try:
                content = _THINK.sub("", raw)
                data = extract_json(content, prefer="label" if '"label"' in content else "foods")
                if isinstance(label := data.get("label"), dict):
                    return PhotoParse(result=ParseResult(kind="food"), label=ParsedLabel.model_validate(label))
                foods = data.get("foods")
                if not isinstance(foods, list):
                    raise LLMError("neither foods nor label")
                note = data.get("note")
                note = note if isinstance(note, str) and note.strip() else None
                return PhotoParse(result=ParseResult(kind="food", foods=foods, note=note))
            except (LLMError, ValidationError, ValueError, TypeError) as e:
                raise _EmptyAnswer(f"unusable vision answer: {type(e).__name__}") from e

        return await self._over_routes(call, json_mode=True, routes=self.vision_routes, purpose=spec)

    async def complete_json(
        self, messages: list[dict[str, str]], prefer: str = "kind", *, purpose: str = "json"
    ) -> dict:
        """A JSON object from the model (json mode where supported), with the same route fallback.

        Only "is it a JSON object" is checked here; the caller validates the content itself, so a wrong
        but well-formed answer costs one request, not a retry on every route. `prefer`: the top-level key of
        the expected answer, so an inner object with a "kind" key is not taken for it (see extract_json).
        `purpose` ("settings", "edit", "baselines", "lookup", "plan") gives Claude its schema and effort.
        """
        spec = PURPOSES[purpose]

        async def call(route: Route, json_mode: bool) -> dict:
            raw = await self._complete(route, messages, json_mode, purpose=spec)
            return extract_json(_THINK.sub("", raw), prefer)

        return await self._over_routes(call, json_mode=True, purpose=spec)


LLMClient = OpenRouterClient  # the provider-neutral name
