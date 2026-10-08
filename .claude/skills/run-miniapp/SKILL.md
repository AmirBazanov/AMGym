---
name: run-miniapp
description: Запуск и проверка Telegram Mini App GymAPP (React + Vite) локально и внутри Telegram через HTTPS-туннель. Используй перед сдачей изменений в miniapp/.
---

# Запуск мини-аппа

Все команды из `miniapp/`.

1. `npm install` (один раз).
2. Проверки перед сдачей: `npm run typecheck && npm run build`.
3. В браузере: `npm run dev` → http://localhost:5173. Вне Telegram `window.Telegram.WebApp.initData` пустой, API ответит 401; для вёрстки этого достаточно.
4. Внутри Telegram нужен HTTPS:
   - запусти dev-сервер так: `VITE_HOST=0.0.0.0 npm run dev` (по умолчанию он слушает только localhost);
   - подними туннель на порт 5173 (`cloudflared tunnel --url http://localhost:5173` или `ngrok http 5173`);
   - впиши URL в `.env` как `MINIAPP_URL` и перезапусти бота; кнопка «Открыть дневник» появится по `/start`;
   - либо задай URL через @BotFather → Bot Settings → Menu Button.
5. Бэкенд API ожидается на `localhost:8000`, Vite проксирует `/api` туда (`vite.config.ts`).
6. Отладка в Telegram Desktop: Settings → Advanced → Experimental → Enable webview inspection, затем правый клик → Inspect.

Тема: только `var(--tg-theme-*)`, чтобы приложение выглядело нормально в светлой и тёмной теме Telegram.
