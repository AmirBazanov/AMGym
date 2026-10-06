# Развёртывание на VPS (AWS EC2, Oracle, любой Ubuntu)

1. На сервере (пользователь `ubuntu`):
   ```bash
   curl -fsSL https://raw.githubusercontent.com/AmirBazanov/AMGym/main/deploy/setup-ec2.sh -o setup-ec2.sh && bash setup-ec2.sh main
   ```
   Скрипт ставит Node 22, uv, cloudflared (amd64 и arm64), делает swap 1 ГБ, клонирует репозиторий в `~/amgym`, ставит зависимости бота и мини-аппа (`npm ci`), собирает мини-апп и ставит systemd-сервис `gymbot`. Пока ветка не слита в `main`, подставь её имя вместо `main` в обоих местах команды.
2. С локальной машины скопируй секреты и базу (база необязательна, без неё начнётся с чистой):
   ```bash
   scp -i key.pem .env ubuntu@SERVER_IP:~/amgym/.env
   scp -i key.pem data/gym.db ubuntu@SERVER_IP:~/amgym/data/gym.db
   ```
3. На сервере: `sudo systemctl restart gymbot && journalctl -u gymbot -f`. В логе появится адрес туннеля, бот сам обновит кнопку меню. Отправь боту `/start`.

Обновление: повторно запусти `bash setup-ec2.sh main` (или нужную ветку). Остановка: `sudo systemctl stop gymbot`.

Адрес мини-аппа у быстрого туннеля Cloudflare меняется при каждом перезапуске сервиса; бот обновляет кнопку меню, кнопки в старых сообщениях устаревают. Постоянный адрес: именованный туннель с доменом на Cloudflare и `MINIAPP_URL` в `.env`.
