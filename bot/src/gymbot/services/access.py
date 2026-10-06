"""Who may use this personal app.

ALLOWED_USER_IDS set: only those Telegram ids. Empty: the first Telegram user who reaches the bot or
the Mini App becomes the owner (the first row in `users`), everyone else is turned away.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gymbot.config import Settings
from gymbot.db.models import User


async def is_allowed(session: AsyncSession, settings: Settings, telegram_id: int) -> bool:
    if settings.allowed_user_ids:
        return telegram_id in settings.allowed_user_ids
    owner = await session.scalar(select(User.telegram_id).order_by(User.id).limit(1))
    return owner is None or owner == telegram_id
