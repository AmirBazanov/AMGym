---
name: run-bot
description: Запуск и проверка Telegram-бота GymAPP локально — окружение, тесты, линтер, long polling. Используй перед сдачей изменений в bot/ и когда нужно проверить бота вживую.
---

# Запуск бота локально

Все команды из `bot/`.

1. Окружение (один раз):
   ```bash
   python3.12 -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
   pip install -e ".[dev]"
   ```
2. `.env` в корне проекта (копия `.env.example`). Нужны минимум `BOT_TOKEN` (от @BotFather) и `OPENROUTER_API_KEY`. Значения не печатать в логи и не коммитить.
3. Быстрые проверки (обязательно перед сдачей):
   ```bash
   ruff check src tests
   pytest -q
   ```
4. Живой запуск одной командой из корня репо: `uv run --project bot python -m gymbot.dev` (сборка мини-аппа, туннель cloudflared, миграции, импорт программ, бот + HTTP на :8000). Без туннеля: `python -m gymbot.main`. Отправь боту `/start`, затем «жим лёжа 3 по 10 на 60» и проверь предпросмотр с кнопками.
   - Только API и мини-апп без Telegram: `RUN_BOT=false DEV_USER_ID=1 python -m gymbot.main`, затем http://localhost:8000.
   - Проверка бесплатных моделей OpenRouter: `python -m gymbot.llm.check`.
5. Если бот молчит: проверь, что токен не используется другим запущенным процессом (Telegram отдаёт апдейты одному поллеру), и `ALLOWED_USER_IDS`.

Без токена живой запуск невозможен; в этом случае ограничься шагом 3 и скажи об этом.
