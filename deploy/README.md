# Развёртывание на VPS (AWS EC2, Oracle; Ubuntu или Amazon Linux 2023)

1. На сервере (пользователь `ubuntu` на Ubuntu или `ec2-user` на Amazon Linux):
   ```bash
   curl -fsSL https://raw.githubusercontent.com/AmirBazanov/AMGym/main/deploy/setup-ec2.sh -o setup-ec2.sh && bash setup-ec2.sh main
   ```
   Скрипт ставит Node 22, uv, cloudflared (amd64 и arm64), делает swap 1 ГБ, клонирует репозиторий в `~/amgym`, ставит зависимости бота и мини-аппа (`npm ci`), собирает мини-апп и ставит systemd-сервис `gymbot`. Пока ветка не слита в `main`, подставь её имя вместо `main` в обоих местах команды.
2. С локальной машины скопируй секреты и базу (база необязательна, без неё начнётся с чистой):
   ```bash
   scp -i key.pem .env ec2-user@SERVER_IP:~/amgym/.env
   scp -i key.pem data/gym.db ec2-user@SERVER_IP:~/amgym/data/gym.db
   ```
3. На сервере: `sudo systemctl restart gymbot && journalctl -u gymbot -f`. В логе появится адрес туннеля, бот сам обновит кнопку меню. Отправь боту `/start`.

Обновление: повторно запусти `bash setup-ec2.sh main` (или нужную ветку). Остановка: `sudo systemctl stop gymbot`.

Адрес мини-аппа у быстрого туннеля Cloudflare меняется при каждом перезапуске сервиса; бот обновляет кнопку меню, кнопки в старых сообщениях устаревают. Постоянный адрес: именованный туннель с доменом на Cloudflare и `MINIAPP_URL` в `.env`.

## CI/CD (GitHub Actions)

`.github/workflows/ci-deploy.yml`: на каждый pull request и пуш в `main` гоняются проверки бота и мини-аппа; после зелёных проверок пуш в `main` деплоит на сервер по SSH (`bash ~/amgym/deploy/setup-ec2.sh main` и рестарт сервиса).

Один раз настроить:
1. Ключ для деплоя (на своём компьютере): `ssh-keygen -t ed25519 -f ~/.ssh/amgym_deploy -N ""`.
2. На сервере добавить публичный ключ: содержимое `~/.ssh/amgym_deploy.pub` дописать в `~/.ssh/authorized_keys`.
3. В GitHub: Settings → Secrets and variables → Actions → New repository secret: `EC2_HOST` (IP сервера), `EC2_USER` (`ec2-user` или `ubuntu`), `EC2_SSH_KEY` (содержимое приватного файла `~/.ssh/amgym_deploy` целиком).
4. Порт 22 на сервере должен быть открыт для раннеров GitHub (правило SSH с источником 0.0.0.0/0).
