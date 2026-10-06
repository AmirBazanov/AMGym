---
name: openrouter-parsing
description: Вызов OpenRouter и формат промпта для разбора свободного текста о тренировках и еде в структурированные записи. Используй при правке промпта, схемы ParseResult, выборе бесплатной модели или отладке ошибок разбора.
---

# Разбор текста через OpenRouter

Файлы: `bot/src/gymbot/llm/openrouter.py` (клиент), `prompts.py` (системный промпт + few-shot), `schemas.py` (`ParseResult`).

## Поток
сообщение → `build_messages(text, catalog)` → `POST {OPENROUTER_BASE_URL}/chat/completions` (`temperature: 0`, `response_format: json_object`) → вырезаем первый `{...}` → `ParseResult.model_validate` → предпросмотр в чате → запись только после подтверждения.

`catalog` — названия упражнений из БД, чтобы модель писала «жим лёжа», а не «жим штанги лёжа на горизонтальной скамье». Сопоставление с `exercises` по `name` и `aliases`; неизвестное упражнение предложи создать, а не создавай молча.

## Бесплатные модели
- Список меняется: https://openrouter.ai/models?max_price=0 . Модель задаётся `OPENROUTER_MODEL`, запасные `OPENROUTER_FALLBACK_MODELS` (JSON-список). Суффикс `:free`.
- Лимиты у бесплатных строгие (запросы в минуту и в день). На 429 клиент переходит к следующей модели; не добавляй агрессивных ретраев.
- Слабые модели путают «3 по 10» (подходы × повторы) и вес. Каждый новый тип ошибки = новый пример в `EXAMPLES` и тест в `bot/tests/test_llm_parse.py`.

## Правка промпта
1. Промпт короткий, по-русски, схема JSON прямо в тексте.
2. Пример в `EXAMPLES` обязан проходить `ParseResult` (тест `test_examples_validate`).
3. Проверка вживую без бота:
   ```bash
   cd bot && python -c "import asyncio; from gymbot.config import get_settings; from gymbot.llm.openrouter import OpenRouterClient as C; print(asyncio.run(C(get_settings()).parse_message('присед 4х8 по 80, потом 3 подхода выпадов по 12', ['присед со штангой'])))"
   ```
4. КБЖУ от модели — оценка (`FoodEntry.estimated = True`); показывай это пользователю.

Никогда не отправляй в OpenRouter токен бота, id пользователей или что-то кроме текста сообщения и каталога упражнений.
