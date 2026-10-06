"""Apply Alembic migrations from inside the running app (one-command start, no manual step)."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncEngine

BOT_DIR = Path(__file__).resolve().parents[3]


def _config() -> Config:
    cfg = Config(str(BOT_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BOT_DIR / "migrations"))
    cfg.attributes["configure_logger"] = False
    return cfg


async def upgrade_head(engine: AsyncEngine) -> None:
    def run(connection) -> None:  # type: ignore[no-untyped-def]
        cfg = _config()
        cfg.attributes["connection"] = connection
        command.upgrade(cfg, "head")

    async with engine.begin() as conn:
        await conn.run_sync(run)
