"""Minimal OpenRouter client (OpenAI-compatible chat completions API).

Free models are rate-limited and sometimes return prose around the JSON, so we:
  1. ask for json_object output; some providers reject it with 400 ("does not support
     structured-outputs"), then we retry without it and remember that for the model,
  2. extract the first {...} block,
  3. validate with pydantic and retry once / fall back to the next model.
"""

from __future__ import annotations

import json
import logging
import re

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


def _rejects_json_mode(resp: httpx.Response) -> bool:
    """A 400 about response_format, not e.g. about context length (that one is the next model's job)."""
    text = resp.text.lower()
    return any(word in text for word in ("response_format", "structured", "json"))


class OpenRouterClient:
    """One instance per process: it keeps the HTTP connection pool and what it learned about models."""

    def __init__(self, settings: Settings, http: httpx.AsyncClient | None = None):
        self.s = settings
        self.http = http or httpx.AsyncClient(timeout=60)
        self._no_json_mode: set[str] = set()  # models that returned 400 on response_format

    async def aclose(self) -> None:
        await self.http.aclose()

    async def _complete(self, model: str, messages: list[dict[str, str]], json_mode: bool) -> str:
        body: dict = {"model": model, "messages": messages, "temperature": 0}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        resp = await self.http.post(
            f"{self.s.openrouter_base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {self.s.openrouter_api_key}",
                "X-Title": "GymAPP",
            },
            json=body,
        )
        resp.raise_for_status()
        message = resp.json()["choices"][0]["message"]
        # Reasoning models sometimes leave `content` empty and put the answer after their reasoning.
        return message.get("content") or message.get("reasoning") or ""

    async def parse_message(
        self, text: str, catalog: list[str], history: list[tuple[str, str]] | None = None
    ) -> ParseResult:
        """Parse one chat message; `history` is (user text, assistant JSON) turns of the recent dialog."""
        if not self.s.openrouter_api_key:
            raise LLMError("OPENROUTER_API_KEY is not set")
        messages = build_messages(text, catalog, history)
        last_err: Exception | None = None
        for model in [self.s.openrouter_model, *self.s.openrouter_fallback_models]:
            for _attempt in range(2):
                json_mode = model not in self._no_json_mode
                try:
                    return ParseResult.model_validate(extract_json(await self._complete(model, messages, json_mode)))
                except httpx.HTTPStatusError as e:
                    log.warning("openrouter %s failed: %s", model, e)
                    last_err = e
                    if e.response.status_code == 400 and json_mode and _rejects_json_mode(e.response):
                        # Provider rejects response_format (e.g. Novita: "does not support structured-outputs").
                        self._no_json_mode.add(model)
                        continue
                    if e.response.status_code in (400, 402, 404, 429):  # bad request / quota / model gone
                        break
                except (httpx.HTTPError, LLMError, ValidationError, json.JSONDecodeError, KeyError, TypeError) as e:
                    log.warning("openrouter %s failed: %s", model, e)
                    last_err = e
        raise LLMError(f"all models failed: {last_err}")
