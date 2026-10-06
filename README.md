# GymAPP

Личный дневник тренировок и питания в Telegram: пишешь боту «жим 3 по 10 на 60», нейросеть разбирает, ты подтверждаешь кнопкой. Мини-апп открывает тренировку дня по программе, хранит историю и рисует прогресс.

## Запуск на своём компьютере

Нужно один раз установить:
- [uv](https://docs.astral.sh/uv/getting-started/installation/) (ставит нужный Python сам),
- Node.js 20+,
- `cloudflared` — бесплатный HTTPS-туннель, без него Telegram не откроет мини-апп:
  - Windows: `winget install Cloudflare.cloudflared`
  - macOS: `brew install cloudflared`
  - Linux / WSL: `curl -L -o cloudflared.deb https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb && sudo dpkg -i cloudflared.deb`

Затем:
```bash
git clone https://github.com/AmirBazanov/AMGym.git && cd AMGym
cp .env.example .env          # впиши BOT_TOKEN и OPENROUTER_API_KEY
uv run --project bot python -m gymbot.dev
```
Команда собирает мини-апп, поднимает туннель, применяет миграции БД, загружает программы из `data/programs/` и запускает бота. В Telegram открой бота, нажми /start, потом кнопку «Дневник» слева от поля ввода. Пока терминал открыт, всё работает; адрес туннеля меняется при каждом запуске, бот обновляет кнопку сам.

Данные лежат в `data/gym.db` (SQLite). Бэкап — просто копия файла.

## Полезное
- Проверить нейросеть на примерах: `uv run --project bot python -m gymbot.llm.check` (или со своим текстом в кавычках). Если бесплатные модели перестали отвечать, выбери другую на https://openrouter.ai/models?max_price=0 и впиши в `OPENROUTER_MODEL`.
- Только мини-апп в браузере, без Telegram: в `.env` поставь `RUN_BOT=false` и `DEV_USER_ID=1`, запусти `uv run --project bot python -m gymbot.main` и открой http://localhost:8000. Не включай `DEV_USER_ID` вместе с туннелем.
- Закрыть бота от чужих: впиши свой Telegram id (бот покажет его на /start) в `ALLOWED_USER_IDS=[...]`.
- Проверки перед коммитом: `cd bot && uv run ruff check src tests && uv run pytest -q`; `cd miniapp && npm run typecheck && npm run build`.

Устройство проекта и правила: `CLAUDE.md`. План: `ROADMAP.md`.
