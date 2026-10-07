"""/backup: a fresh copy of the database for the owner, on demand (gymbot.services.backup).

Works with BACKUP_ENABLED off too; does not touch the daily job's marker.
"""

import logging
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from gymbot.config import Settings
from gymbot.db.session import Sessionmaker
from gymbot.services import backup

log = logging.getLogger(__name__)

router = Router(name="backup")

OWNER_ONLY = "Копию базы получает только владелец."
NOT_SQLITE = "Копии делаются только для базы SQLite, а здесь другая."
FAILED = "Не получилось сделать копию базы, подробности в логе сервера."


@router.message(Command("backup"))
async def backup_now(message: Message, settings: Settings, sessionmaker: Sessionmaker) -> None:
    # The middleware lets in every ALLOWED_USER_IDS id; the database goes to the owner only.
    async with sessionmaker() as session:
        owner = await backup.owner_chat_id(session, settings)
    if message.from_user is None or message.from_user.id != owner:
        await message.answer(OWNER_ONLY)
        return
    now = datetime.now(UTC)
    try:
        path = await backup.create(settings, now)
        await backup.send(message.bot, message.chat.id, path, now, ZoneInfo(settings.timezone))  # type: ignore[arg-type]
    except backup.BackupUnavailable:
        await message.answer(NOT_SQLITE)
    except Exception:
        log.exception("backup on demand failed")
        await message.answer(FAILED)
