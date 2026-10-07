# Развёртывание на VPS (AWS EC2, Oracle; Ubuntu или Amazon Linux 2023)

1. На сервере (пользователь `ubuntu` на Ubuntu или `ec2-user` на Amazon Linux):
   ```bash
   curl -fsSL https://raw.githubusercontent.com/AmirBazanov/AMGym/main/deploy/setup-ec2.sh -o setup-ec2.sh && bash setup-ec2.sh main
   ```
   Скрипт ставит Node 22, uv, cloudflared для быстрого туннеля (только если в `.env` нет `PUBLIC_URL`), делает swap 1 ГБ, клонирует репозиторий в `~/amgym`, ставит зависимости бота и мини-аппа (`npm ci`), собирает мини-апп и ставит systemd-сервис `gymbot`. Пока ветка не слита в `main`, подставь её имя вместо `main` в обоих местах команды.
2. С локальной машины скопируй секреты и базу (база необязательна, без неё начнётся с чистой):
   ```bash
   scp -i key.pem .env ec2-user@SERVER_IP:~/amgym/.env
   scp -i key.pem data/gym.db ec2-user@SERVER_IP:~/amgym/data/gym.db
   ```
3. На сервере: `sudo systemctl restart gymbot && journalctl -u gymbot -f`. В логе появится адрес туннеля, бот сам обновит кнопку меню. Отправь боту `/start`.

Обновление: повторно запусти `bash setup-ec2.sh main` (или нужную ветку). Остановка: `sudo systemctl stop gymbot`.

Адрес мини-аппа у быстрого туннеля Cloudflare меняется при каждом перезапуске сервиса; бот обновляет кнопку меню, кнопки в старых сообщениях устаревают. Постоянный адрес описан ниже.

## Постоянный адрес и вебхук

Сервер получает свой домен (например, `gym.algex.ru`), Telegram присылает апдейты вебхуком на `https://gym.algex.ru/telegram/webhook`, мини-апп открывается по тому же адресу. Сервис `gymbot` запускает `python -m gymbot.main` (миграции, импорт программ, бот, HTTP на `127.0.0.1:8000`), быстрый туннель не нужен. HTTPS даёт один из двух вариантов ниже.

1. В `~/amgym/.env` на сервере дописать:
   ```bash
   PUBLIC_URL=https://gym.algex.ru
   BOT_MODE=webhook
   WEBHOOK_SECRET=<вывод openssl rand -hex 32>
   MINIAPP_URL=
   # EDGE=caddy   # только для варианта Б
   ```
   `MINIAPP_URL` пустой: мини-апп открывается по `PUBLIC_URL`. `WEBHOOK_SECRET` можно не задавать, тогда при каждом старте генерируется случайный (вебхук всё равно переустанавливается при старте).
2. Настроить HTTPS (вариант А или Б).
3. Применить: `bash ~/amgym/deploy/setup-ec2.sh main`. Скрипт переключает сервис на `gymbot.main` и перезапускает его.

### Вариант А: именованный туннель Cloudflare (рекомендуется, `EDGE=tunnel` по умолчанию)
Домен должен быть на DNS Cloudflare: NS-серверы `algex.ru` у регистратора переключены на Cloudflare. Запись `gym` (CNAME на `<id>.cfargotunnel.com`) Cloudflare создаёт сам при добавлении Public Hostname; старую A-запись `gym`, если есть, удалить. Входящие порты 80/443 не нужны, IP сервера может меняться.
1. Cloudflare Zero Trust → Networks → Tunnels → Create tunnel (cloudflared) → выполнить на сервере предложенные команды установки cloudflared и `sudo cloudflared service install <токен>`. Токен — секрет, в репозиторий и `.env` проекта не кладётся.
2. В туннеле Public Hostname: `gym.algex.ru` → `HTTP` → `127.0.0.1:8000` (именно `127.0.0.1`, не `localhost`: приложение слушает только IPv4-адрес и доверяет заголовкам `X-Forwarded-*` только с него).
3. Скрипт в этом режиме ничего не ставит и только предупреждает, если сервис `cloudflared` не запущен. Если Telegram не может достучаться до вебхука (`last_error_message` в `getWebhookInfo`), проверь, что Bot Fight Mode / WAF Cloudflare не режут запросы на `/telegram/webhook`.

### Вариант Б: Caddy на сервере (`EDGE=caddy`)
1. DNS: у регистратора A-запись `gym.algex.ru` → публичный IP сервера (лучше Elastic IP, иначе IP меняется после остановки инстанса). Проверка: `dig +short gym.algex.ru`.
2. Security group: входящие TCP 80 и 443 с `0.0.0.0/0` (80 нужен Let's Encrypt). Порт 8000 наружу не открывать.
3. `EDGE=caddy` в `.env`. Скрипт ставит Caddy (Amazon Linux 2023: `dnf`, если пакета нет — официальный COPR-репозиторий Caddy для EL9, в крайнем случае статический бинарник с systemd-юнитом; Ubuntu: apt-репозиторий Caddy), пишет `/etc/caddy/Caddyfile` из `deploy/Caddyfile` и включает Caddy. Сертификат Caddy выпускает и продлевает сам.

### Проверка
```bash
curl -I https://gym.algex.ru                      # 200, мини-апп
curl https://gym.algex.ru/api/health             # {"status":"ok"}
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://gym.algex.ru/telegram/webhook   # 403 без секрета
journalctl -u gymbot -n 30 --no-pager           # "webhook -> https://gym.algex.ru/telegram/webhook", "menu button -> ..."
```
Состояние вебхука со стороны Telegram: `curl "https://api.telegram.org/bot<BOT_TOKEN>/getWebhookInfo"` (`url`, `pending_update_count`, `last_error_message`). Токен в истории оболочки не оставлять.

Вебхук при остановке сервиса не снимается: пока сервис перезапускается, Telegram копит апдейты и дошлёт их. Локальный запуск в режиме polling с тем же токеном снимает вебхук сервера (в логе будет предупреждение), поэтому локально используй отдельного тестового бота; если всё же запускал с боевым токеном, перезапусти сервис на сервере: `sudo systemctl restart gymbot`.

## MCP

Приложение отдаёт MCP-сервер на `https://gym.algex.ru/mcp` (Streamable HTTP, без сессий, ответы JSON) — через него Claude читает дневник и делает узкий набор записей от имени владельца: норма КБЖУ, факты, напоминания, план дня, заметки, сообщение в Telegram. Шелла и произвольной записи в базу нет; `query` — только SELECT на read-only подключении. Код: `bot/src/gymbot/mcp_server.py`, подробности SDK: `docs/reference/mcp-python-sdk.md`.

1. Включить: в `~/amgym/.env` на сервере `MCP_TOKEN=<вывод openssl rand -hex 32>` и `sudo systemctl restart gymbot`. Пустой `MCP_TOKEN` — маршрута `/mcp` нет (в логе строка `MCP_TOKEN is empty: /mcp is not mounted`). Токен даёт полный доступ к дневнику: только в `.env` и в настройках клиента, не в git и не в URL.
2. Проверка (ждём 401 без токена и список инструментов с ним; `MCP_TOKEN` — переменная оболочки с токеном):
   ```bash
   curl -s -o /dev/null -w '%{http_code}\n' -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
     -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' https://gym.algex.ru/mcp        # 401
   curl -s -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
     -H "Authorization: Bearer $MCP_TOKEN" -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' https://gym.algex.ru/mcp
   ```
3. Claude Code (на своём компьютере):
   ```bash
   claude mcp add --transport http amgym https://gym.algex.ru/mcp --header "Authorization: Bearer <token>"
   claude mcp list    # или /mcp внутри Claude Code
   ```
   По умолчанию область `local` (только у тебя, `~/.claude.json`). В `.mcp.json` репозитория токен не писать, только через переменную: `"headers": {"Authorization": "Bearer ${GYM_MCP_TOKEN}"}`.
4. Claude.ai (custom connectors): запросы идут из облака Anthropic, URL — ровно `https://gym.algex.ru/mcp`, без слэша в конце. Статический заголовок задаётся в разделе «Request headers» (бета, доступен не всем организациям): поле `Authorization`, значение `Bearer <token>` целиком. Если такого раздела нет, этот сервер к Claude.ai не подключить: вариант «No sign-in» открыл бы дневник любому, кто знает URL, а OAuth сервер не поддерживает. Токен в query-строке (`?token=`) сервер не принимает.

Caddy и туннель Cloudflare менять не нужно: ответы — обычный JSON на POST, без долгих SSE-потоков; оба пробрасывают `Host` как есть (у туннеля — пока в Public Hostname не задан свой HTTP Host Header), и приложение пускает на `/mcp` только `Host` из `PUBLIC_URL` и локальные адреса (иначе 421). После смены домена в `PUBLIC_URL` нужен рестарт сервиса.

## CI/CD (GitHub Actions)

`.github/workflows/ci-deploy.yml`: на каждый pull request и пуш в `main` гоняются проверки бота и мини-аппа; после зелёных проверок пуш в `main` деплоит на сервер по SSH (`bash ~/amgym/deploy/setup-ec2.sh main` и рестарт сервиса).

Один раз настроить:
1. Ключ для деплоя (на своём компьютере): `ssh-keygen -t ed25519 -f ~/.ssh/amgym_deploy -N ""`.
2. На сервере добавить публичный ключ: содержимое `~/.ssh/amgym_deploy.pub` дописать в `~/.ssh/authorized_keys`.
3. В GitHub: Settings → Secrets and variables → Actions → New repository secret: `EC2_HOST` (IP сервера), `EC2_USER` (`ec2-user` или `ubuntu`), `EC2_SSH_KEY` (содержимое приватного файла `~/.ssh/amgym_deploy` целиком).
4. Порт 22 на сервере должен быть открыт для раннеров GitHub (правило SSH с источником 0.0.0.0/0).

## Резервные копии базы

Бот раз в день (`BACKUP_HOUR`, по умолчанию 4:00 по `TIMEZONE`) делает согласованную копию SQLite-базы (online backup API SQLite, работает без остановки сервиса), сжимает её в `gym-YYYYMMDD-HHMMSSZ.db.gz` (время в имени — UTC) и присылает владельцу в Telegram документом с подписью «Копия базы 08.10 04:00 — N КБ. Восстановить: …». Последние 7 копий лежат на сервере в `~/amgym/data/backups/`. Код: `bot/src/gymbot/services/backup.py`.

- Включено по умолчанию при `BOT_MODE=webhook` (сервер), выключено локально; явно: `BACKUP_ENABLED=true|false`, час — `BACKUP_HOUR=4` в `.env`.
- Одна копия в день: дата последней отправки в `data/backups/.last_daily`. Если в 4:00 сервис лежал, копия придёт, когда он поднимется в тот же день. После первого деплоя позже 4:00 копия придёт сразу.
- Копия больше 50 МБ (лимит Telegram для ботов) в чат не уходит: вместо неё приходит сообщение с путём к файлу на сервере.
- `/backup` в чате с ботом — свежая копия по запросу (только владельцу; работает и при `BACKUP_ENABLED=false`).
- Только для SQLite: с Postgres в `DATABASE_URL` бот ничего не копирует.

Копии при деплое (`gym.db.bak-*`, последние 5) делает `setup-ec2.sh`, это отдельный механизм.

### Восстановление из копии
1. Скачай файл `gym-….db.gz` из чата и скопируй на сервер (или возьми из `~/amgym/data/backups/`):
   ```bash
   scp -i key.pem gym-20261008-010000Z.db.gz ec2-user@SERVER_IP:~/
   ```
2. На сервере:
   ```bash
   sudo systemctl stop gymbot
   cd ~/amgym/data
   cp -p gym.db gym.db.before-restore           # текущая база на всякий случай
   gunzip -c ~/gym-20261008-010000Z.db.gz > gym.db
   rm -f gym.db-wal gym.db-shm gym.db-journal    # старые служебные файлы испортят восстановленную базу
   sqlite3 gym.db 'PRAGMA integrity_check'       # ok (если sqlite3 установлен)
   sudo systemctl start gymbot && journalctl -u gymbot -n 20 --no-pager
   ```
   Копия старой версии схемы обновится сама: миграции выполняются при старте.

## Uptime: оповещения о падении

`.github/workflows/uptime.yml` каждые 10 минут (GitHub иногда запускает расписание на несколько минут позже) проверяет снаружи:
- `https://gym.algex.ru/api/health` — 3 попытки с паузой 20 с; эндпоинт делает `select 1` в базе с таймаутом 2 с и отвечает 503, если база недоступна;
- вебхук глазами Telegram (`getWebhookInfo`): не установлен, `last_error_date` за последние 15 минут или `pending_update_count` больше 20.

Сообщение в Telegram приходит один раз при поломке (с HTTP-кодом и причинами), повторно раз в 6 часов, пока не починится, и один раз при восстановлении. Последнее состояние хранится в кеше Actions (`uptime-state-*`). Если сообщение отправить не удалось, запуск становится красным и следующий повторит попытку.

Один раз настроить (GitHub → Settings → Secrets and variables → Actions → New repository secret):
- `TG_ALERT_BOT_TOKEN` — токен бота, от имени которого придёт оповещение. Подходит тот же `BOT_TOKEN`, что на сервере, и только с ним работает проверка вебхука. Если берёшь отдельного бота, заведи переменную (вкладка Variables) `UPTIME_SKIP_WEBHOOK=true`, иначе проверка решит, что вебхук не установлен.
- `TG_ALERT_CHAT_ID` — твой Telegram id (личный чат с ботом). В workflow он не прописан, только секрет. Боту нужно хоть раз написать `/start`, иначе Telegram не даст ему написать первым.

Без секретов workflow не шлёт сообщений (в логе notice), а при поломке запуск становится красным, и GitHub присылает своё письмо о неудачном запуске.

Проверка: Actions → Uptime → Run workflow. Расписание работает только из ветки `main`. В публичном репозитории GitHub отключает расписание после 60 дней без коммитов (письмо придёт заранее): если оповещения затихли, проверь, что workflow включён. В публичном репозитории минуты Actions бесплатны; в приватном 144 запуска в день превысят бесплатный лимит. Если `/api/health` стабильно отвечает 403 только из GitHub, это Bot Fight Mode / WAF Cloudflare: разреши путь `/api/health` в правилах.
