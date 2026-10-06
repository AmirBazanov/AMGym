"""One-command local start: `uv run --project bot python -m gymbot.dev` from the repo root.

1. Builds the Mini App (npm install + build) when miniapp/dist is missing or older than the sources.
2. Opens a free Cloudflare quick tunnel to the local server (Telegram only opens Mini Apps over HTTPS)
   unless MINIAPP_URL is already set, and passes its URL to the bot, which updates the menu button.
3. Runs the app (gymbot.main): migrations, program import, bot and HTTP server.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path

from gymbot.config import ROOT, get_settings

MINIAPP = ROOT / "miniapp"
TUNNEL_URL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def newest_mtime(path: Path) -> float:
    return max((p.stat().st_mtime for p in path.rglob("*") if p.is_file()), default=0.0)


def build_miniapp() -> None:
    npm = shutil.which("npm")
    if npm is None:
        sys.exit("npm not found: install Node.js 20+ (https://nodejs.org) and run again")
    dist = MINIAPP / "dist"
    sources = max(newest_mtime(MINIAPP / "src"), newest_mtime(ROOT / "data" / "programs"))
    if dist.is_dir() and newest_mtime(dist) >= sources:
        print("Mini App build is up to date")
        return
    if not (MINIAPP / "node_modules").is_dir():
        subprocess.run([npm, "install"], cwd=MINIAPP, check=True)
    subprocess.run([npm, "run", "build"], cwd=MINIAPP, check=True)


def start_tunnel(port: int) -> tuple[subprocess.Popen[str], str]:
    exe = shutil.which("cloudflared")
    if exe is None:
        sys.exit(
            "cloudflared not found. Install it (Windows: `winget install Cloudflare.cloudflared`, "
            "macOS: `brew install cloudflared`, Linux/WSL: see README) or set MINIAPP_URL in .env."
        )
    proc = subprocess.Popen(
        [exe, "tunnel", "--no-autoupdate", "--url", f"http://localhost:{port}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        if m := TUNNEL_URL.search(line):
            url = m.group(0)
            # Keep draining the output so cloudflared never blocks on a full pipe.
            threading.Thread(target=lambda: [None for _ in proc.stdout], daemon=True).start()  # type: ignore[union-attr]
            return proc, url
    sys.exit("cloudflared exited before printing a tunnel URL")


def main() -> None:
    settings = get_settings()
    build_miniapp()
    tunnel = None
    if not settings.miniapp_url:
        tunnel, url = start_tunnel(settings.api_port)
        os.environ["MINIAPP_URL"] = url
        print(f"\nMini App: {url}\nOpen the bot in Telegram, send /start and press «Дневник».\n")
    try:
        from gymbot.main import run

        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    finally:
        if tunnel:
            tunnel.terminate()


if __name__ == "__main__":
    main()
