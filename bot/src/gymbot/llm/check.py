"""Try the configured LLM routes (Groq, then OpenRouter) on sample messages.

`python -m gymbot.llm.check ["текст" ...]` parses each text on its own;
`python -m gymbot.llm.check --dialog "три куриные самсы" "три штуки"` sends each next text with the
previous exchange as history, like the bot does. Every request prints its provider/model, whether it
asked for json mode and the HTTP status (headers are never printed: they carry the API key).
"""

import asyncio
import json
import sys
import time

import httpx

from gymbot.config import get_settings
from gymbot.llm.openrouter import LLMClient, LLMError

SAMPLES = [
    "жим лёжа 4 по 8 на 70",
    "сгибания гантели 3х12 по 14, потом французский жим 3 по 10 на 25",
    "дропсет на бицепс 12-6-6 с 16 кг",
    "съел 200 г гречки и 2 яйца",
    "как дела?",
]


def provider(url: httpx.URL) -> str:
    return "groq" if url.host.endswith("groq.com") else "openrouter" if "openrouter" in url.host else url.host


async def show_request(request: httpx.Request) -> None:
    body = json.loads(request.content)
    print(f"  request: {provider(request.url)}/{body['model']} json_mode={'response_format' in body}")


async def show_response(response: httpx.Response) -> None:
    print(f"  response: {response.status_code}")


async def main(args: list[str]) -> None:
    dialog = "--dialog" in args
    texts = [a for a in args if a != "--dialog"] or SAMPLES
    settings = get_settings()
    hooks = {"request": [show_request], "response": [show_response]}
    client = LLMClient(settings, httpx.AsyncClient(timeout=60, event_hooks=hooks))
    print("routes:", [r.name for r in client.routes])
    history: list[tuple[str, str]] = []
    chain: list[str] = []  # like the bot: the history turn is all texts of the current record
    for text in texts:
        print(f"\n> {text}")
        t = time.monotonic()
        try:
            result = await client.parse_message(text, ["жим лёжа", "французский жим лёжа"], history or None)
        except LLMError as e:
            print(f"FAILED: {e}")
            continue
        print(f"({time.monotonic() - t:.1f}s) {result.model_dump_json(exclude_defaults=True)}")
        if dialog and result.kind != "question":
            chain = [*chain, text][-3:]
            history = [("\n".join(chain), result.model_dump_json())]
    await client.aclose()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
