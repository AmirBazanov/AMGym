"""Free-text logging: message -> LLM -> preview -> user confirms -> saved.

Saving is behind a confirm button on purpose: free models make mistakes, and a wrong
set silently written to the log ruins progress charts.
"""

from aiogram import F, Router
from aiogram.types import Message

from gymbot.config import get_settings
from gymbot.llm.openrouter import LLMError, OpenRouterClient
from gymbot.llm.schemas import ParseResult

router = Router(name="log_text")


def render_preview(result: ParseResult) -> str:
    if result.kind == "workout":
        lines = []
        for ex in result.exercises:
            sets = ", ".join(f"{s.reps}×{s.weight_kg:g}" if s.weight_kg else f"{s.reps}" for s in ex.sets)
            lines.append(f"• {ex.exercise}: {sets}")
        return "Записать?\n" + "\n".join(lines)
    if result.kind == "food":
        lines = [f"• {f.description}: {f.kcal:.0f} ккал, Б{f.protein_g:.0f} Ж{f.fat_g:.0f} У{f.carbs_g:.0f}"
                 for f in result.foods]
        return "Записать еду?\n" + "\n".join(lines)
    return result.clarification or "Не понял. Напиши, например: «присед 4х8 по 80»."


@router.message(F.text & ~F.text.startswith("/"))
async def log_free_text(message: Message) -> None:
    client = OpenRouterClient(get_settings())
    catalog: list[str] = []  # TODO(stage1): load exercise names from DB
    try:
        result = await client.parse_message(message.text or "", catalog)
    except LLMError:
        await message.answer("Нейросеть сейчас недоступна, попробуй ещё раз чуть позже.")
        return
    # TODO(stage1): stash `result` in FSM state and add Сохранить / Исправить inline buttons
    await message.answer(render_preview(result))
