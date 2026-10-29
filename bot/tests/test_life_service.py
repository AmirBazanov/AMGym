"""«Дела»: the life_* tables and migration 0017, the snapshot and the day agenda, the import of the old dashboard
(data.js + hub_state), the model's actions and life.apply (gymbot.services.life, gymbot.services.life_actions)."""

import pytest
from alembic import command
from sqlalchemy import inspect, text

from gymbot.db import migrate
from gymbot.db.session import make_engine

LIFE_TABLES = {f"life_{t}" for t in ("slots", "events", "deadlines", "tasks", "chores", "projects", "notes")}
USER_COLUMNS = {"life_remind_before", "life_summary_minute", "life_summary_enabled", "life_summary_sent_on",
                "life_imported_at"}


# ---- migration 0017 ----


def _schema(conn) -> dict:
    insp = inspect(conn)
    tables = set(insp.get_table_names())
    return {
        "tables": tables,
        "users": {c["name"] for c in insp.get_columns("users")},
        "unique": {t: [u["column_names"] for u in insp.get_unique_constraints(t)] for t in tables & LIFE_TABLES},
    }


async def test_migration_0017_round_trip(tmp_path):
    engine, _ = make_engine(f"sqlite+aiosqlite:///{tmp_path}/m.db")

    async def run_sync(fn):
        async with engine.begin() as conn:
            return await conn.run_sync(fn)

    async def migrate_to(target: str):
        def fn(conn):
            cfg = migrate._config()
            cfg.attributes["connection"] = conn
            (command.upgrade if target == "head" else command.downgrade)(cfg, target)

        await run_sync(fn)

    try:
        await migrate_to("head")
        up = await run_sync(_schema)
        assert LIFE_TABLES <= up["tables"]
        assert USER_COLUMNS <= up["users"]
        assert all(u == [["user_id", "source", "external_id"]] for u in up["unique"].values())
        async with engine.begin() as conn:
            await conn.execute(text("PRAGMA foreign_keys=ON"))
            await conn.execute(text(
                "INSERT INTO users (telegram_id, rest_seconds, created_at) VALUES (1, 90, '2026-10-01')"
            ))
            row = (await conn.execute(text(
                "SELECT life_remind_before, life_summary_minute, life_summary_enabled, life_imported_at FROM users"
            ))).one()
            assert tuple(row) == (30, 480, 1, None)  # server defaults for the owner's existing row
            ins = ("INSERT INTO life_tasks (user_id, title, position, source, external_id, created_at, updated_at) "
                   "VALUES (1, 'x', 0, :source, :ext, '2026-10-01', '2026-10-01')")
            await conn.execute(text(ins), {"source": "me", "ext": None})
            await conn.execute(text(ins), {"source": "me", "ext": None})  # NULL external ids never collide
            await conn.execute(text(ins), {"source": "import", "ext": "todo:t1"})
        with pytest.raises(Exception, match="UNIQUE"):
            async with engine.begin() as conn:
                await conn.execute(text(ins), {"source": "import", "ext": "todo:t1"})

        await migrate_to("0016")
        down = await run_sync(_schema)
        assert up["tables"] - down["tables"] == LIFE_TABLES
        assert "hub_state" in down["tables"]
        assert not USER_COLUMNS & down["users"]
        await migrate_to("head")
        assert await run_sync(_schema) == up
    finally:
        await engine.dispose()
