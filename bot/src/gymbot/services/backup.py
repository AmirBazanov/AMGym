"""Database backups: a consistent gzip copy of the SQLite file, sent to the owner's Telegram chat.

The copy is made with SQLite's online backup API on a separate read-only connection, so it is consistent
while the bot keeps writing; backup and gzip run in a worker thread to keep the event loop free.
Every copy is also kept next to the database in `backups/` (the newest KEEP files `gym-*.db.gz`).

Daily job (backup_loop, BACKUP_ENABLED / BACKUP_HOUR): the backup is due once the local time in TIMEZONE
reaches BACKUP_HOUR and the marker file `backups/.last_daily` holds an earlier day. It is catch-up, not a
window: a server that was down at 04:00 sends the copy when it comes back the same day. The marker is
written only after the copy was delivered (or Telegram refused it for good, or it is too big to send),
so a transient send error is retried after RETRY with the same file. One process sends; a crash between
the send and the marker write may send that day's copy twice.

Postgres or in-memory SQLite: nothing to copy, the loop and /backup say so and stop.
"""

from __future__ import annotations

import asyncio
import gzip
import logging
import os
import shutil
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import FSInputFile
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

from gymbot.config import Settings
from gymbot.db.session import Sessionmaker
from gymbot.services.access import owner_user

log = logging.getLogger(__name__)

KEEP = 7
# Telegram Bot API limit for documents a bot sends; a module constant so tests can lower it.
MAX_SEND_BYTES = 50 * 1024 * 1024
CHECK_SECONDS = 60
RETRY = timedelta(minutes=10)
MARKER = ".last_daily"
PATTERN = "gym-*.db.gz"

_lock = asyncio.Lock()  # one copy at a time: the daily job and /backup share the folder
_pending: dict[date, Path] = {}  # daily copy made but not delivered yet, reused by the retry


class BackupUnavailable(Exception):
    """DATABASE_URL is not a SQLite file."""


def sqlite_file(database_url: str) -> Path | None:
    """The SQLite file behind DATABASE_URL; None for other databases and in-memory SQLite."""
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite":
        return None
    db = url.database
    return Path(db) if db and db != ":memory:" else None


def backup_dir(db_file: Path) -> Path:
    return db_file.parent / "backups"


def rotate(out_dir: Path, keep: int = KEEP) -> list[Path]:
    """Delete all but the newest `keep` copies (names sort by time). Other files are never touched."""
    copies = sorted(out_dir.glob(PATTERN))
    removed = copies[:-keep] if keep > 0 else copies
    for path in removed:
        path.unlink(missing_ok=True)
    return removed


def make_backup(src: Path, out_dir: Path, now_utc: datetime, keep: int = KEEP) -> Path:
    """Copy `src` consistently into `out_dir/gym-YYYYmmdd-HHMMSSZ.db.gz` (UTC), rotate, return the path.

    Blocking: call it in a thread. The source is opened read-only, so a wrong path fails instead of
    creating an empty database.
    """
    if not src.is_file():
        raise FileNotFoundError(f"database file not found: {src}")
    out_dir.mkdir(parents=True, exist_ok=True)
    final = out_dir / f"gym-{now_utc.astimezone(UTC):%Y%m%d-%H%M%S}Z.db.gz"
    raw = out_dir / f".{final.name}.db.tmp"
    packed = out_dir / f".{final.name}.tmp"
    try:
        source = sqlite3.connect(f"{src.resolve().as_uri()}?mode=ro", uri=True, timeout=30)
        try:
            target = sqlite3.connect(raw)
            try:
                source.backup(target)  # pages=-1: one step, a single consistent snapshot
            finally:
                target.close()
        finally:
            source.close()
        # filename: `gunzip -N` restores the name gym.db; mtime: the moment of the copy.
        mtime = int(now_utc.timestamp())
        with (
            raw.open("rb") as f_in,
            packed.open("wb") as f_out,
            gzip.GzipFile(filename="gym.db", mode="wb", fileobj=f_out, compresslevel=6, mtime=mtime) as gz,
        ):
            shutil.copyfileobj(f_in, gz, 1024 * 1024)
        os.replace(packed, final)
    finally:
        raw.unlink(missing_ok=True)
        packed.unlink(missing_ok=True)
    rotate(out_dir, keep)
    return final


def caption(now_local: datetime, size: int, name: str) -> str:
    """One line under the document: when, how big, how to restore."""
    kb = max(1, round(size / 1024))
    return (
        f"Копия базы {now_local:%d.%m %H:%M} — {kb} КБ. "
        f"Восстановить: остановить gymbot, gunzip -c {name} > ~/amgym/data/gym.db, "
        "удалить gym.db-wal и gym.db-shm, запустить gymbot (deploy/README.md)."
    )


def too_big_text(now_local: datetime, size: int, path: Path) -> str:
    return (
        f"Копия базы {now_local:%d.%m %H:%M} — {size / 1024 / 1024:.1f} МБ, больше лимита Telegram "
        f"({MAX_SEND_BYTES // 1024 // 1024} МБ), в чат не отправлена. Лежит на сервере: {path}"
    )


def read_marker(out_dir: Path) -> date | None:
    try:
        return date.fromisoformat((out_dir / MARKER).read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


def write_marker(out_dir: Path, day: date) -> None:
    """Atomic: a crash leaves either the old or the new date."""
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = out_dir / f"{MARKER}.tmp"
    tmp.write_text(day.isoformat())
    os.replace(tmp, out_dir / MARKER)


def due_day(now_utc: datetime, tz: ZoneInfo, hour: int, last: date | None) -> date | None:
    """Local day whose daily backup is due now: from `hour` local time until it is marked done."""
    local = now_utc.astimezone(tz)
    if local.hour < hour:
        return None
    today = local.date()
    return None if last is not None and last >= today else today


async def owner_chat_id(session: AsyncSession, settings: Settings) -> int | None:
    """The owner's Telegram id (private chat id = user id): same rule as access.owner_user."""
    owner = await owner_user(session, settings)
    if owner is not None:
        return owner.telegram_id
    return settings.allowed_user_ids[0] if settings.allowed_user_ids else None


async def create(settings: Settings, now_utc: datetime) -> Path:
    """Make a local copy now (BackupUnavailable for non-SQLite databases)."""
    src = sqlite_file(settings.database_url)
    if src is None:
        raise BackupUnavailable("backups are only for SQLite")
    async with _lock:
        return await asyncio.to_thread(make_backup, src, backup_dir(src), now_utc)


async def send(bot: Bot, chat_id: int, path: Path, now_utc: datetime, tz: ZoneInfo) -> bool:
    """Send the copy as a document; a warning text instead when it is over the Telegram limit.

    Returns True when the document itself went out. Only the size and the name are logged.
    """
    size = path.stat().st_size
    local = now_utc.astimezone(tz)
    if size > MAX_SEND_BYTES:
        log.warning("backup %s is %s bytes, over the Telegram limit: not sent", path.name, size)
        await bot.send_message(chat_id, too_big_text(local, size, path))
        return False
    await bot.send_document(
        chat_id, FSInputFile(path, filename=path.name), caption=caption(local, size, path.name)
    )
    log.info("backup %s sent (%s bytes)", path.name, size)
    return True


async def daily_tick(bot: Bot, sessionmaker: Sessionmaker, settings: Settings, now_utc: datetime) -> bool:
    """One check of the daily job. True when the day got marked done now.

    Raises on transient failures (copy or send); the caller retries after RETRY.
    """
    src = sqlite_file(settings.database_url)
    if src is None:
        return False
    out_dir = backup_dir(src)
    tz = ZoneInfo(settings.timezone)
    day = due_day(now_utc, tz, settings.backup_hour, read_marker(out_dir))
    if day is None:
        return False
    async with sessionmaker() as session:
        chat_id = await owner_chat_id(session, settings)
    if chat_id is None:
        log.debug("backups: no owner yet, nothing to send to")
        return False
    for old in [d for d in _pending if d != day]:
        del _pending[old]
    path = _pending.get(day)
    if path is None or not path.exists():
        path = _pending[day] = await create(settings, now_utc)
    try:
        await send(bot, chat_id, path, now_utc, tz)
    except (TelegramForbiddenError, TelegramBadRequest) as e:
        # Permanent (bot blocked, chat not found): retrying all day would only spam the log.
        log.warning("backups: not delivered to %s, skipped for today: %s", chat_id, e)
    write_marker(out_dir, day)
    _pending.pop(day, None)
    return True


async def backup_loop(bot: Bot, sessionmaker: Sessionmaker, settings: Settings) -> None:
    """Check every CHECK_SECONDS until cancelled; a failed attempt waits RETRY."""
    src = sqlite_file(settings.database_url)
    if src is None:
        log.info("backups: DATABASE_URL is not a SQLite file, daily backups are off")
        return
    log.info(
        "backups: daily at %02d:00 %s to the owner's chat, copies in %s",
        settings.backup_hour,
        settings.timezone,
        backup_dir(src),
    )
    retry_at: datetime | None = None
    while True:
        now = datetime.now(UTC)
        if retry_at is None or now >= retry_at:
            try:
                await daily_tick(bot, sessionmaker, settings, now)
                retry_at = None
            except Exception:
                retry_at = now + RETRY
                log.exception("backups: daily backup failed, next attempt in %s", RETRY)
        await asyncio.sleep(CHECK_SECONDS)
