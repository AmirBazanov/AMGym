from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gymbot.db.models import Program, User, UserProgram
from gymbot.services.programs import monday_of, visible_to


async def get_or_create_user(session: AsyncSession, telegram_id: int, name: str | None = None) -> User:
    user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
    if user is None:
        user = User(telegram_id=telegram_id, name=name, rest_seconds=90)
        session.add(user)
        await session.flush()
    return user


async def active_program(session: AsyncSession, user: User, today: date) -> UserProgram:
    """The latest program choice; on first use start the first template this week."""
    up = await session.scalar(
        select(UserProgram).where(UserProgram.user_id == user.id).order_by(UserProgram.id.desc()).limit(1)
    )
    if up is None:
        program = await session.scalar(
            select(Program).where(Program.owner_user_id.is_(None)).order_by(Program.id).limit(1)
        )
        if program is None:
            raise RuntimeError("no programs imported; check PROGRAMS_DIR")
        up = UserProgram(user_id=user.id, program_id=program.id, started_on=monday_of(today))
        session.add(up)
        await session.flush()
    await session.refresh(up, ["program"])
    return up


async def set_program(session: AsyncSession, user: User, slug: str, started_on: date) -> UserProgram:
    """Raises LookupError for an unknown slug or another user's own copy."""
    program = await session.scalar(select(Program).where(Program.slug == slug, visible_to(user.id)))
    if program is None:
        raise LookupError(slug)
    current = await session.scalar(
        select(UserProgram).where(UserProgram.user_id == user.id).order_by(UserProgram.id.desc()).limit(1)
    )
    started_on = monday_of(started_on)
    if current and current.program_id == program.id and current.started_on == started_on:
        return current
    up = UserProgram(user_id=user.id, program_id=program.id, started_on=started_on)
    session.add(up)
    await session.flush()
    await session.refresh(up, ["program"])
    return up
