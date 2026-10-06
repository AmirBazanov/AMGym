"""Minimal OpenRouter client (OpenAI-compatible chat completions API).

Free models are rate-limited and sometimes return prose around the JSON, so we:
  1. ask for json_object output (ignored by models that don't support it),
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


class OpenRouterClient:
    def __init__(self, settings: Settings, http: httpx.AsyncClient | None = None):
        self.s = settings
        self.http = http or httpx.AsyncClient(timeout=60)

    async def _complete(self, model: str, messages: list[dict[str, str]]) -> str:
        resp = await self.http.post(
            f"{self.s.openrouter_base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {self.s.openrouter_api_key}",
                "X-Title": "GymAPP",
            },
            json={
                "model": model,
                "messages": messages,
                "temperature": 0,
                "response_format": {"type": "json_object"},
            },
        )
        resp.raise_for_status()
        message = resp.json()["choices"][0]["message"]
        # Reasoning models sometimes leave `content` empty and put the answer after their reasoning.
        return message.get("content") or message.get("reasoning") or ""

    async def parse_message(self, text: str, catalog: list[str]) -> ParseResult:
        if not self.s.openrouter_api_key:
            raise LLMError("OPENROUTER_API_KEY is not set")
        messages = build_messages(text, catalog)
        last_err: Exception | None = None
        for model in [self.s.openrouter_model, *self.s.openrouter_fallback_models]:
            for _attempt in range(2):
                try:
                    return ParseResult.model_validate(extract_json(await self._complete(model, messages)))
                except httpx.HTTPStatusError as e:
                    log.warning("openrouter %s failed: %s", model, e)
                    last_err = e
                    if e.response.status_code in (402, 404, 429):  # out of quota / model gone: next model
                        break
                except (httpx.HTTPError, LLMError, ValidationError, json.JSONDecodeError, KeyError, TypeError) as e:
                    log.warning("openrouter %s failed: %s", model, e)
                    last_err = e
        raise LLMError(f"all models failed: {last_err}")
