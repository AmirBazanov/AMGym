"""Alembic environment. The URL comes from gymbot settings (DATABASE_URL), never from alembic.ini.

The app runs `upgrade head` on startup through `gymbot.db.migrate`, passing an open sync connection
in `config.attributes["connection"]`; the CLI path (`alembic upgrade head`) creates its own engine.
"""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from gymbot.config import get_settings
from gymbot.db.models import Base

config = context.config
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def url() -> str:
    return get_settings().database_url


def do_run_migrations(connection: Connection) -> None:
    # render_as_batch: SQLite can't ALTER most things in place.
    context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_offline() -> None:
    context.configure(url=url(), target_metadata=target_metadata, literal_binds=True, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    engine = create_async_engine(url(), poolclass=pool.NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
elif (conn := config.attributes.get("connection")) is not None:
    do_run_migrations(conn)
else:
    asyncio.run(run_async_migrations())
