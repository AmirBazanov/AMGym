"""Minimal chat LLM client over OpenAI-compatible APIs: Groq first, OpenRouter as the last fallback.

A route is (provider, base_url, api_key, model); routes are built from Settings in order: every
GROQ_MODELS model (if there is a Groq key), then OPENROUTER_MODEL and its fallbacks (if there is an
OpenRouter key). Free models are rate-limited and sometimes return prose around the JSON, so we:
  1. ask for json_object output; some providers reject it with 400 ("does not support
     structured-outputs"), then we retry without it and remember that for the route,
  2. extract the first {...} block,
  3. validate with pydantic and retry once / fall back to the next route.
A route that answered 429 is skipped for RATE_LIMIT_COOLDOWN (no waiting for retry-after: there are
other routes); 400/401/402/403/404/413 also move straight to the next route.
"""

from __future__ import annotations

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


def extract_json(content: str) -> dict:
    """First JSON object in the text that looks like a ParseResult (models may add prose or reasoning)."""
    decoder = json.JSONDecoder()
    first: dict | None = None
    for m in re.finditer(r"\{", content):
        try:
            obj, _ = decoder.raw_decode(content, m.start())
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            if "kind" in obj:
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

RATE_LIMIT_COOLDOWN = 60.0  # seconds a route is skipped after a 429
NEXT_ROUTE_STATUSES = (400, 401, 402, 403, 404, 413, 429)  # bad request / key / quota / model gone / too big


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
    ):
        self.s = settings
        self.http = http or httpx.AsyncClient(timeout=60)
        self.routes = routes_from(settings)
        self._clock = clock
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
        message = resp.json()["choices"][0]["message"]
        if not use_reasoning:
            return message.get("content") or ""
        # Reasoning models sometimes leave `content` empty and put the answer after their reasoning.
        return message.get("content") or message.get("reasoning") or ""

    async def _over_routes(self, call: Callable[[Route, bool], Awaitable[T]], *, json_mode: bool) -> T:
        """Try `call(route, json_mode)` on every route in order, two attempts each, see the module doc."""
        if not self.routes:
            raise LLMError("no LLM API key: set GROQ_API_KEY (or STT_API_KEY) or OPENROUTER_API_KEY")
        last_err: Exception | None = None
        for route in self.routes:
            if self._cooldown_until.get(route.name, 0) > self._clock():
                last_err = last_err or LLMError(f"{route.name} is rate-limited")
                continue
            for _attempt in range(2):
                use_json = json_mode and route.name not in self._no_json_mode
                try:
                    return await call(route, use_json)
                except httpx.HTTPStatusError as e:
                    log.warning("llm %s failed: %s", route.name, e.response.status_code)
                    last_err = e
                    status = e.response.status_code
                    if status == 400 and use_json and _rejects_json_mode(e.response):
                        # Provider rejects response_format (e.g. Novita: "does not support structured-outputs").
                        self._no_json_mode.add(route.name)
                        continue
                    if status == 429:
                        self._cooldown_until[route.name] = self._clock() + RATE_LIMIT_COOLDOWN
                    if status in NEXT_ROUTE_STATUSES:
                        break
                except _EmptyAnswer as e:
                    log.warning("llm %s failed: %s", route.name, e)
                    last_err = e
                    break
                except (httpx.HTTPError, LLMError, ValidationError, ValueError, KeyError, TypeError) as e:
                    log.warning("llm %s failed: %s", route.name, e)
                    last_err = e
        raise LLMError(f"all models failed: {last_err}")

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


LLMClient = OpenRouterClient  # the provider-neutral name
