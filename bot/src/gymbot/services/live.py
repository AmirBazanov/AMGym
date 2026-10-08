"""Live updates for the open Mini App: a per-user in-process pub/sub and the Server-Sent Events stream.

One process serves the bot, the API and MCP, so an in-memory hub is enough: whatever changes data from
the chat, MCP or the API calls `publish(user_id, *topics)` after its commit, and every open stream of that
user gets `event: change` with the topics; the Mini App refetches them. New personal records
(gymbot.services.records) go out with `publish_records`: topic "records" plus the records themselves
(`{"topics": ["records"], "records": [{"exercise", "weight", "reps", "text"}]}`), so the Mini App can show
a toast without a request; events without records keep the plain `{"topics": [...]}` payload.
Publishing is best-effort and never raises: a lost event only means the Mini App shows the change on the
next refresh.

EventSource cannot send headers, so the stream is opened with a short-lived token from
POST /api/live/token (`make_token` / `verify_token`), HMAC-signed with a key derived from BOT_TOKEN.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import time
from collections.abc import AsyncIterator, Iterable
from typing import Any, Literal

log = logging.getLogger(__name__)

Topic = Literal[
    "state", "nutrition", "reminders", "facts", "wellbeing", "plan", "workouts", "weight", "records", "program"
]
TOPICS: frozenset[str] = frozenset(
    {"state", "nutrition", "reminders", "facts", "wellbeing", "plan", "workouts", "weight", "records", "program"}
)
MAX_RECORDS = 5  # records carried by one event (gymbot.services.records announces at most 5 lines)

TOKEN_TTL = 60  # seconds; only needed to open the stream, which then lives on
MAX_STREAMS_PER_USER = 3  # a 4th stream closes the oldest one (Mini App reopened, stale tabs)
COALESCE_SECONDS = 0.3  # publishes within this window after the first one go out as one event
PING_SECONDS = 20.0  # heartbeat comment; keeps proxies (Caddy, Cloudflare: 100 s idle) from closing it

_TOKEN_PURPOSE = "live-stream"


# ---- token ----


def signing_key(bot_token: str) -> bytes:
    """Own label, so the key is useless for initData; stable across restarts (tokens survive a deploy)."""
    return hmac.new(b"GymLiveStreamToken", bot_token.encode(), hashlib.sha256).digest()


def _sig(key: bytes, user_id: int, exp: int) -> str:
    mac = hmac.new(key, f"{_TOKEN_PURPOSE}|{user_id}|{exp}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).rstrip(b"=").decode()


def make_token(key: bytes, user_id: int, now: float | None = None, ttl: int = TOKEN_TTL) -> str:
    """`<user_id>.<expires unix>.<signature>`; `user_id` is the database User.id."""
    exp = int(now if now is not None else time.time()) + ttl
    return f"{user_id}.{exp}.{_sig(key, user_id, exp)}"


def verify_token(key: bytes, token: str, now: float | None = None) -> int | None:
    """The database user id, or None for a malformed, forged or expired token."""
    parts = token.split(".")
    if len(parts) != 3 or not parts[0].isdigit() or not parts[1].isdigit():
        return None
    user_id, exp = int(parts[0]), int(parts[1])
    if not hmac.compare_digest(_sig(key, user_id, exp), parts[2]):
        return None
    if (now if now is not None else time.time()) > exp:
        return None
    return user_id


class RedactTokens(logging.Filter):
    """Hide `token=` in uvicorn access log lines (the stream URL carries the token)."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and any(isinstance(a, str) and "token=" in a for a in args):
            record.args = tuple(_redact(a) if isinstance(a, str) else a for a in args)
        return True


def _redact(text: str) -> str:
    head, sep, tail = text.partition("token=")
    if not sep:
        return text
    _value, amp, rest = tail.partition("&")
    return f"{head}token=***{amp}{_redact(rest) if amp else ''}"


def install_log_redaction() -> None:
    """Idempotent: create_app may run more than once per process (tests)."""
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, RedactTokens) for f in access.filters):
        access.addFilter(RedactTokens())


# ---- hub ----


class Subscriber:
    """One open stream. Topics accumulate in a set until the stream takes them: bursts merge for free."""

    def __init__(self, user_id: int) -> None:
        self.user_id = user_id
        self.topics: set[str] = set()
        self.records: list[dict[str, Any]] = []  # carried with the "records" topic until taken
        self.wake = asyncio.Event()
        self.closed = False

    def push(self, topics: Iterable[str], records: Iterable[dict[str, Any]] = ()) -> None:
        self.topics.update(topics)
        self.records = [*self.records, *records][-MAX_RECORDS:]
        self.wake.set()

    def take(self) -> list[str]:
        topics, self.topics = sorted(self.topics), set()
        self.wake.clear()
        return topics

    def take_records(self) -> list[dict[str, Any]]:
        records, self.records = self.records, []
        return records

    def close(self) -> None:
        self.closed = True
        self.wake.set()


class Hub:
    def __init__(self, max_per_user: int = MAX_STREAMS_PER_USER) -> None:
        self.max_per_user = max_per_user
        self._subs: dict[int, list[Subscriber]] = {}
        self.closing = False

    def subscribe(self, user_id: int) -> Subscriber:
        sub = Subscriber(user_id)
        if self.closing:
            sub.close()  # shutting down: the stream ends right after hello
            return sub
        subs = self._subs.setdefault(user_id, [])
        subs.append(sub)
        while len(subs) > self.max_per_user:
            subs.pop(0).close()  # oldest first
        return sub

    def unsubscribe(self, sub: Subscriber) -> None:
        sub.closed = True
        subs = self._subs.get(sub.user_id)
        if subs is None:
            return
        if sub in subs:
            subs.remove(sub)
        if not subs:
            del self._subs[sub.user_id]

    def publish(self, user_id: int, topics: Iterable[str], records: Iterable[dict[str, Any]] = ()) -> None:
        wanted = [t for t in topics if t in TOPICS]
        if not wanted:
            return
        records = list(records)
        for sub in self._subs.get(user_id, ()):
            sub.push(wanted, records)

    def count(self, user_id: int | None = None) -> int:
        if user_id is not None:
            return len(self._subs.get(user_id, ()))
        return sum(len(s) for s in self._subs.values())

    def close_all(self) -> None:
        """Server shutdown: end every stream so uvicorn's graceful shutdown does not wait on them."""
        self.closing = True
        for subs in list(self._subs.values()):
            for sub in list(subs):
                sub.close()


hub = Hub()


_changes: dict[int, int] = {}  # user id -> number of publishes so far (in this process)


def changes(user_id: int) -> int:
    """A counter that grows on every `publish` for the user: in-process caches of the user's data
    (gymbot.services.answer) compare it to know that something changed."""
    return _changes.get(user_id, 0)


def publish(user_id: int | None, *topics: Topic) -> None:
    """Tell the user's open Mini Apps what changed. Call after the commit. Never raises."""
    if user_id is None:
        return
    _changes[user_id] = _changes.get(user_id, 0) + 1
    try:
        hub.publish(user_id, topics)
    except Exception:
        log.warning("live publish failed", exc_info=True)


def publish_records(user_id: int | None, records: list[dict[str, Any]]) -> None:
    """Topic "records" with the new records (exercise, weight, reps, text) for the Mini App's toast. Never raises."""
    if user_id is None or not records:
        return
    _changes[user_id] = _changes.get(user_id, 0) + 1
    try:
        hub.publish(user_id, ("records",), records[:MAX_RECORDS])
    except Exception:
        log.warning("live publish failed", exc_info=True)


def close_all() -> None:
    try:
        hub.close_all()
    except Exception:
        log.warning("closing live streams failed", exc_info=True)


# ---- stream ----


def sse(event: str, data: object) -> str:
    # EventSource drops events without a data line, so hello carries {} too.
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


async def events(
    user_id: int,
    target: Hub | None = None,
    *,
    coalesce: float = COALESCE_SECONDS,
    ping: float = PING_SECONDS,
) -> AsyncIterator[str]:
    """The SSE body. Subscribes on first iteration (nothing leaks if the response never starts) and
    unsubscribes in `finally`: Starlette cancels the generator when the client disconnects."""
    h = target or hub
    sub = h.subscribe(user_id)
    try:
        yield sse("hello", {})
        while not sub.closed:
            try:
                await asyncio.wait_for(sub.wake.wait(), ping)
            except TimeoutError:
                yield ": ping\n\n"
                continue
            if sub.closed:
                break
            await asyncio.sleep(coalesce)  # let a burst (food + plan + state) arrive as one event
            if sub.closed:
                break
            topics = sub.take()
            records = sub.take_records()
            if topics:
                payload: dict[str, Any] = {"topics": topics}
                if records and "records" in topics:
                    payload["records"] = records
                yield sse("change", payload)
    finally:
        h.unsubscribe(sub)
