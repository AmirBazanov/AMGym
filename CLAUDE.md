# GymAPP

Личное приложение Амира для зала: Telegram-бот (записи текстом через бесплатную нейросеть) + Telegram Mini App (программы, упражнения, сеты, прогресс), позже КБЖУ. План этапов: `ROADMAP.md`.

## Архитектура

```
Telegram чат ──> bot (aiogram 3) ──> llm/openrouter ──> ParseResult (pydantic)
Telegram voice ──> handlers/voice ──> stt (Groq) ──> тот же разбор
                     │                                       │ подтверждение пользователя
Mini App (React) ──> API (FastAPI, этап 2, тот же процесс) ──> SQLAlchemy ──> SQLite (потом Postgres)
                                                              ^
data/programs/*.json <── importers/xlsx_program <── xlsx-программы

reminder loop (asyncio) ──> services/reminders ──> Telegram (ежедневные или по дням недели)
```

| Путь | Что там |
|---|---|
| `bot/src/gymbot/handlers/` | хендлеры бота; `log_text.py` ловит любой текст, подключается последним; `chat_edit.py` — правки программы из чата (гейт до `saved_edits`) |
| `bot/src/gymbot/llm/` | клиент OpenRouter, промпт, схема ответа; `prompts_edit.py` — промпт правок программы из чата |
| `bot/src/gymbot/db/models.py` | все таблицы |
| `bot/src/gymbot/services/` | логика: nutrition, reminders, advice, facts, wellbeing, plan, chat_edit (правки программы из чата), day_adjustments (поправка дня из чата), progression (miniapp/src/{progression,plan}.ts) |
| `bot/src/gymbot/stt.py` | распознавание речи (Groq Whisper) |
| `bot/src/gymbot/importers/` | импорт программ из xlsx |
| `miniapp/` | React 19 + Vite + TS |
| `data/programs/` | готовые программы (JSON) и исходники (`source/*.xlsx`) |
| `docs/program-format.md` | формат таблички программ |
| `deploy/` | развёртывание на VPS (setup-ec2.sh, systemd-сервис) |

## Соглашения
- Секреты только в `.env` (шаблон `.env.example`), читаются через `gymbot.config.Settings`. В коде, логах, коммитах и промптах к LLM их нет.
- Запись из свободного текста только после подтверждения пользователя кнопкой; исходный текст сохраняется в `raw_text`.
- Модели БД переносимые (SQLite → Postgres без переписывания). Изменение схемы = Alembic-миграция.
- Вес в кг, время в UTC, отображение в `TIMEZONE`.
- Мини-апп доверяет только серверу; сервер проверяет Telegram initData по HMAC.
- Пользовательские тексты по-русски, код и комментарии по-английски.
- Проверки перед сдачей: `cd bot && ruff check src tests && pytest -q && pytest -q --shift-days 5` (тесты идут в 6 процессов, ~30 с; второй прогон сдвигает часы и ловит тесты, привязанные к реальной дате; `-n0` для одного процесса); `cd miniapp && npm run typecheck && npm run build && npm test`.

## Как делится работа между агентами

Главная сессия оркестрирует и раздаёт задачи. Ведущие агенты на Opus думают и пишут сложное, рутину отдают дешёвым моделям.

| Агент | Модель | Зона |
|---|---|---|
| `architect` | opus | план фичи, схема БД, контракты API, ROADMAP |
| `bot-backend` | opus | бот, API, БД, OpenRouter, импорт программ |
| `miniapp-frontend` | opus | экраны мини-аппа |
| `reviewer` | opus | ревью перед коммитом, ничего не правит |
| `implementer` | sonnet | механические правки по точной инструкции |
| `test-writer` | sonnet | тесты |
| `code-scout` | haiku | поиск по коду, сбор контекста, линтеры |
| `docs-writer` | haiku | README, docstring, docs/ |

Типовой цикл фичи: `architect` (план) → `bot-backend` / `miniapp-frontend` (реализация, делегируя `code-scout`, `implementer`, `test-writer`) → `reviewer` → коммит.

Если вложенный запуск агентов недоступен (в Claude Code сабагенты могут не уметь запускать своих сабагентов), ведущий агент возвращает в конце блок «Делегировать:» со строками `агент: задача`, и главная сессия сама запускает эти задачи на Sonnet/Haiku.

## Скиллы (`.claude/skills/`)
`run-bot`, `run-miniapp`, `db-migrations`, `openrouter-parsing`, `program-format`.
