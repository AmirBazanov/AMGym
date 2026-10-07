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


async def owner_user(session: AsyncSession, settings: Settings) -> User | None:
    """The owner's row, read only: the first user ALLOWED_USER_IDS lets in, else the first user at all.

    Same rule as `is_allowed`; None while the owner has not reached the bot or the Mini App yet.
    """
    stmt = select(User).order_by(User.id).limit(1)
    if settings.allowed_user_ids:
        stmt = stmt.where(User.telegram_id.in_(settings.allowed_user_ids))
    return await session.scalar(stmt)
