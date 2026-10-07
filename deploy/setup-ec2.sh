#!/usr/bin/env bash
# One-shot setup of GymAPP on a fresh Ubuntu 22.04/24.04 or Amazon Linux 2023 box (EC2 t2/t3.micro is enough).
# Usage (as the default user: ubuntu or ec2-user):  bash setup-ec2.sh [branch]
# Idempotent: re-run to update the checkout and restart the service.
# With PUBLIC_URL=https://your.domain in ~/amgym/.env the service runs gymbot.main (use BOT_MODE=webhook there);
# HTTPS comes from a named Cloudflare tunnel set up from the Cloudflare dashboard (EDGE=tunnel, the default,
# nothing installed here) or from Caddy on this box (EDGE=caddy: installed and configured here, ports 80/443).
# Without PUBLIC_URL it runs gymbot.dev with a Cloudflare quick tunnel (cloudflared installed here).
set -euo pipefail

BRANCH="${1:-main}"
REPO="https://github.com/AmirBazanov/AMGym.git"
APP_DIR="$HOME/amgym"
SERVICE=gymbot

env_value() {  # last KEY=value from .env, quotes and CR stripped; never `source` it (JSON lists break the shell)
  [ -f "$APP_DIR/.env" ] || return 0
  sed -n "s/^$1=//p" "$APP_DIR/.env" | tail -n 1 | tr -d '\r' | sed -e "s/^[\"']//" -e "s/[\"']\$//" -e 's/[[:space:]]*$//'
}

install_caddy_binary() {  # last resort: the official static build + the unit from Caddy's own packages
  local arch
  case "$(uname -m)" in x86_64) arch=amd64 ;; aarch64) arch=arm64 ;; *) arch="$(uname -m)" ;; esac
  curl -fsSL -o /tmp/caddy "https://caddyserver.com/api/download?os=linux&arch=$arch"
  sudo install -m 755 /tmp/caddy /usr/bin/caddy
  getent group caddy >/dev/null || sudo groupadd --system caddy
  id caddy >/dev/null 2>&1 || sudo useradd --system --gid caddy --create-home --home-dir /var/lib/caddy \
    --shell /usr/sbin/nologin caddy
  sudo mkdir -p /etc/caddy
  sudo tee /etc/systemd/system/caddy.service >/dev/null <<'UNIT'
[Unit]
Description=Caddy
Documentation=https://caddyserver.com/docs/
After=network.target network-online.target
Requires=network-online.target

[Service]
Type=notify
User=caddy
Group=caddy
ExecStart=/usr/bin/caddy run --environ --config /etc/caddy/Caddyfile
ExecReload=/usr/bin/caddy reload --config /etc/caddy/Caddyfile --force
TimeoutStopSec=5s
LimitNOFILE=1048576
PrivateTmp=true
ProtectSystem=full
AmbientCapabilities=CAP_NET_ADMIN CAP_NET_BIND_SERVICE

[Install]
WantedBy=multi-user.target
UNIT
  sudo systemctl daemon-reload
}

install_cloudflared() {  # only for the quick tunnel (no PUBLIC_URL)
  command -v cloudflared >/dev/null && return 0
  if [ "$PM" = dnf ]; then
    curl -fsSL -o /tmp/cloudflared.rpm "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$(uname -m).rpm"
    sudo dnf install -y -q /tmp/cloudflared.rpm >/dev/null
  else
    curl -fsSL -o /tmp/cloudflared.deb "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$(dpkg --print-architecture).deb"
    sudo dpkg -i /tmp/cloudflared.deb >/dev/null
  fi
}

install_caddy() {
  command -v caddy >/dev/null && return 0
  if [ "$PM" = dnf ]; then
    if sudo dnf install -y -q caddy >/dev/null 2>&1; then return 0; fi
    # Not in the stock Amazon Linux 2023 repos: Caddy's official COPR build for EL9 (a static Go binary) works.
    if sudo curl -fsSL -o /etc/yum.repos.d/caddy.repo \
         "https://copr.fedorainfracloud.org/coprs/g/caddy/caddy/repo/epel-9/group_caddy-caddy-epel-9.repo" \
       && sudo dnf install -y -q caddy >/dev/null; then
      return 0
    fi
    sudo rm -f /etc/yum.repos.d/caddy.repo
    install_caddy_binary
  else
    # https://caddyserver.com/docs/install#debian-ubuntu-raspbian
    sudo apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https gnupg >/dev/null
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
      | sudo gpg --batch --yes --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
      | sudo tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
    sudo chmod o+r /usr/share/keyrings/caddy-stable-archive-keyring.gpg /etc/apt/sources.list.d/caddy-stable.list
    sudo apt-get update -qq
    sudo apt-get install -y -qq caddy >/dev/null
  fi
}

if command -v dnf >/dev/null; then PM=dnf; else PM=apt; fi
echo "== system packages ($PM)"
if [ "$PM" = dnf ]; then
  sudo dnf install -y -q git ca-certificates gcc make sqlite >/dev/null  # curl-minimal is preinstalled; full curl conflicts with it
else
  sudo apt-get update -qq
  sudo apt-get install -y -qq git curl ca-certificates build-essential sqlite3 >/dev/null
fi

echo "== swap (1 GB) so npm/uv don't get OOM-killed on a 1 GB box"
if ! sudo swapon --show | grep -q '/swapfile'; then
  sudo fallocate -l 1G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile >/dev/null && sudo swapon /swapfile
  grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
fi

echo "== node 22"
if ! command -v node >/dev/null || [ "$(node -v | cut -c2-3)" -lt 20 ]; then
  if [ "$PM" = dnf ]; then
    sudo dnf install -y -q nodejs22 >/dev/null
  else
    curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash - >/dev/null
    sudo apt-get install -y -qq nodejs >/dev/null
  fi
fi

echo "== uv"
command -v "$HOME/.local/bin/uv" >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null

echo "== checkout $BRANCH"
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" fetch -q origin && git -C "$APP_DIR" checkout -q "$BRANCH" && git -C "$APP_DIR" pull -q --ff-only
else
  git clone -q --branch "$BRANCH" "$REPO" "$APP_DIR"
fi

echo "== python deps"
"$HOME/.local/bin/uv" sync --project "$APP_DIR/bot" -q

echo "== mini app deps + build (npm ci keeps package-lock.json untouched so git pull stays fast-forward)"
(cd "$APP_DIR/miniapp" && npm ci --silent && npm run build --silent)

mkdir -p "$APP_DIR/data"

if [ ! -f "$APP_DIR/.env" ]; then
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  chmod 600 "$APP_DIR/.env"
  echo "!! $APP_DIR/.env created from the example: copy your real .env here (scp) before starting"
fi

chmod 600 "$APP_DIR/.env"

PUBLIC_URL="$(env_value PUBLIC_URL)"
EDGE="$(env_value EDGE)"; EDGE="${EDGE:-tunnel}"
if [ -n "$PUBLIC_URL" ]; then
  MODULE=gymbot.main
  [ "$(env_value BOT_MODE)" = webhook ] || echo "!! PUBLIC_URL is set but BOT_MODE is not webhook: the bot will long-poll"
  if [ "$EDGE" = caddy ]; then
    DOMAIN="${PUBLIC_URL#https://}"; DOMAIN="${DOMAIN#http://}"; DOMAIN="${DOMAIN%%/*}"
    if ! [[ "$DOMAIN" =~ ^[A-Za-z0-9.-]+$ ]]; then
      echo "!! PUBLIC_URL=$PUBLIC_URL: expected https://your.domain" >&2
      exit 1
    fi
    PORT="$(env_value API_PORT)"; PORT="${PORT:-8000}"
    echo "== caddy: https://$DOMAIN -> 127.0.0.1:$PORT"
    install_caddy
    sed "s#__DOMAIN__#$DOMAIN#g; s#__PORT__#$PORT#g" "$APP_DIR/deploy/Caddyfile" > /tmp/Caddyfile.gymbot
    caddy validate --adapter caddyfile --config /tmp/Caddyfile.gymbot >/dev/null 2>&1 \
      || { caddy validate --adapter caddyfile --config /tmp/Caddyfile.gymbot; exit 1; }
    sudo install -m 644 /tmp/Caddyfile.gymbot /etc/caddy/Caddyfile
    sudo systemctl enable --now caddy >/dev/null
    sudo systemctl reload-or-restart caddy
  else
    echo "== EDGE=tunnel: HTTPS for $PUBLIC_URL comes from the named Cloudflare tunnel (cloudflared service)"
    systemctl is-active --quiet cloudflared \
      || echo "!! cloudflared service is not running: install it with the command from the Cloudflare dashboard"
  fi
else
  echo "== no PUBLIC_URL in .env: gymbot.dev with a Cloudflare quick tunnel"
  install_cloudflared
  MODULE=gymbot.dev
fi

echo "== systemd service ($MODULE)"
sed "s#__HOME__#$HOME#g; s#__USER__#$USER#g; s#__MODULE__#$MODULE#g" "$APP_DIR/deploy/gymbot.service" \
  | sudo tee /etc/systemd/system/$SERVICE.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable $SERVICE >/dev/null
if grep -q '^BOT_TOKEN=.\+' "$APP_DIR/.env"; then
  sudo systemctl restart $SERVICE
  echo "== started; logs: journalctl -u $SERVICE -f"
else
  echo "== not started: fill $APP_DIR/.env, then: sudo systemctl start $SERVICE"
fi
