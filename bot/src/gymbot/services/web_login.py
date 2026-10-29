"""Sign-in to the Mini App from a computer browser, where Telegram gives no initData.

/web (handlers/web_login.py) sends a link `PUBLIC_URL/?login=<token>`; the Mini App keeps the token and sends it
as `X-App-Token`, which the API takes where it takes initData (api/app.py tg_user). The token is
`<telegram id>.<expires unix>.<signature>`: HMAC with a key derived from the bot token under its own label (useless
for initData or the live stream), valid TTL; the API still checks ALLOWED_USER_IDS on every request.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time

TTL = 30 * 24 * 3600
_PURPOSE = "web-login"


def signing_key(bot_token: str) -> bytes:
    return hmac.new(b"GymWebLoginToken", bot_token.encode(), hashlib.sha256).digest()


def _sig(key: bytes, telegram_id: int, exp: int) -> str:
    mac = hmac.new(key, f"{_PURPOSE}|{telegram_id}|{exp}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).rstrip(b"=").decode()


def make_token(key: bytes, telegram_id: int, now: float | None = None, ttl: int = TTL) -> str:
    exp = int(now if now is not None else time.time()) + ttl
    return f"{telegram_id}.{exp}.{_sig(key, telegram_id, exp)}"


def verify(key: bytes, token: str, now: float | None = None) -> int | None:
    """The Telegram id, or None for a malformed, forged or expired token."""
    parts = token.split(".")
    if len(parts) != 3 or not parts[0].isdigit() or not parts[1].isdigit():
        return None
    telegram_id, exp = int(parts[0]), int(parts[1])
    if not hmac.compare_digest(_sig(key, telegram_id, exp), parts[2]):
        return None
    if (now if now is not None else time.time()) > exp:
        return None
    return telegram_id
