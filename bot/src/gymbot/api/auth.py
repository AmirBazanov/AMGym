"""Telegram Mini App initData validation.

https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
secret = HMAC_SHA256(key="WebAppData", msg=bot_token); hash = HMAC_SHA256(key=secret, msg=data_check_string)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl

# initData stays the same for the whole Mini App session; a week covers an app left open for days.
MAX_AGE_SECONDS = 7 * 24 * 3600


class InitDataError(ValueError):
    pass


@dataclass
class TelegramUser:
    id: int
    name: str | None


def validate_init_data(init_data: str, bot_token: str, now: float | None = None) -> TelegramUser:
    pairs = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=False))
    received = pairs.pop("hash", None)
    if not received:
        raise InitDataError("no hash")
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        raise InitDataError("bad hash")
    try:
        auth_date = int(pairs.get("auth_date", "0"))
    except ValueError as e:
        raise InitDataError("bad auth_date") from e
    if (now or time.time()) - auth_date > MAX_AGE_SECONDS:
        raise InitDataError("expired")
    try:
        user = json.loads(pairs["user"])
        name = " ".join(filter(None, [user.get("first_name"), user.get("last_name")])) or user.get("username")
        return TelegramUser(id=int(user["id"]), name=name)
    except (KeyError, ValueError, TypeError) as e:
        raise InitDataError("no user") from e


def sign_init_data(fields: dict[str, str], bot_token: str) -> str:
    """Build a valid initData string (tests and local tooling)."""
    from urllib.parse import urlencode

    check_string = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    digest = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode({**fields, "hash": digest})
