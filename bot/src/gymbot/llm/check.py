"""Try the configured OpenRouter models on sample messages: `python -m gymbot.llm.check ["текст" ...]`."""

import asyncio
import sys
import time

from gymbot.config import get_settings
from gymbot.llm.openrouter import LLMError, OpenRouterClient

SAMPLES = [
    "жим лёжа 4 по 8 на 70",
    "сгибания гантели 3х12 по 14, потом французский жим 3 по 10 на 25",
    "дропсет на бицепс 12-6-6 с 16 кг",
    "съел 200 г гречки и 2 яйца",
    "как дела?",
]


async def main(texts: list[str]) -> None:
    settings = get_settings()
    client = OpenRouterClient(settings)
    print("models:", [settings.openrouter_model, *settings.openrouter_fallback_models])
    for text in texts or SAMPLES:
        t = time.monotonic()
        try:
            result = await client.parse_message(text, ["жим лёжа", "французский жим лёжа"])
            print(f"\n> {text}  ({time.monotonic() - t:.1f}s)\n{result.model_dump_json(exclude_defaults=True)}")
        except LLMError as e:
            print(f"\n> {text}\nFAILED: {e}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
