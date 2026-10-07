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

## CI/CD (GitHub Actions)

`.github/workflows/ci-deploy.yml`: на каждый pull request и пуш в `main` гоняются проверки бота и мини-аппа; после зелёных проверок пуш в `main` деплоит на сервер по SSH (`bash ~/amgym/deploy/setup-ec2.sh main` и рестарт сервиса).

Один раз настроить:
1. Ключ для деплоя (на своём компьютере): `ssh-keygen -t ed25519 -f ~/.ssh/amgym_deploy -N ""`.
2. На сервере добавить публичный ключ: содержимое `~/.ssh/amgym_deploy.pub` дописать в `~/.ssh/authorized_keys`.
3. В GitHub: Settings → Secrets and variables → Actions → New repository secret: `EC2_HOST` (IP сервера), `EC2_USER` (`ec2-user` или `ubuntu`), `EC2_SSH_KEY` (содержимое приватного файла `~/.ssh/amgym_deploy` целиком).
4. Порт 22 на сервере должен быть открыт для раннеров GitHub (правило SSH с источником 0.0.0.0/0).
