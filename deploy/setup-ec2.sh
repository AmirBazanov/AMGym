#!/usr/bin/env bash
# One-shot setup of GymAPP on a fresh Ubuntu 22.04/24.04 box (AWS EC2 t2/t3.micro is enough).
# Usage (as the default user, e.g. ubuntu):  bash setup-ec2.sh [branch]
# Idempotent: re-run to update the checkout and restart the service.
set -euo pipefail

BRANCH="${1:-main}"
REPO="https://github.com/AmirBazanov/AMGym.git"
APP_DIR="$HOME/amgym"
SERVICE=gymbot

echo "== system packages"
sudo apt-get update -qq
sudo apt-get install -y -qq git curl ca-certificates build-essential sqlite3 >/dev/null

echo "== swap (1 GB) so npm/uv don't get OOM-killed on a 1 GB box"
if ! sudo swapon --show | grep -q '/swapfile'; then
  sudo fallocate -l 1G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile >/dev/null && sudo swapon /swapfile
  grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
fi

echo "== node 22"
if ! command -v node >/dev/null || [ "$(node -v | cut -c2-3)" -lt 20 ]; then
  curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash - >/dev/null
  sudo apt-get install -y -qq nodejs >/dev/null
fi

echo "== uv"
command -v "$HOME/.local/bin/uv" >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null

echo "== cloudflared"
if ! command -v cloudflared >/dev/null; then
  curl -fsSL -o /tmp/cloudflared.deb https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb
  sudo dpkg -i /tmp/cloudflared.deb >/dev/null
fi

echo "== checkout $BRANCH"
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" fetch -q origin && git -C "$APP_DIR" checkout -q "$BRANCH" && git -C "$APP_DIR" pull -q --ff-only
else
  git clone -q --branch "$BRANCH" "$REPO" "$APP_DIR"
fi

echo "== python deps"
"$HOME/.local/bin/uv" sync --project "$APP_DIR/bot" -q

if [ ! -f "$APP_DIR/.env" ]; then
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  echo "!! $APP_DIR/.env created from the example: copy your real .env here (scp) before starting"
fi

echo "== systemd service"
sed "s#__HOME__#$HOME#g; s#__USER__#$USER#g" "$APP_DIR/deploy/gymbot.service" | sudo tee /etc/systemd/system/$SERVICE.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable $SERVICE >/dev/null
if grep -q '^BOT_TOKEN=.\+' "$APP_DIR/.env"; then
  sudo systemctl restart $SERVICE
  echo "== started; logs: journalctl -u $SERVICE -f"
else
  echo "== not started: fill $APP_DIR/.env, then: sudo systemctl start $SERVICE"
fi
