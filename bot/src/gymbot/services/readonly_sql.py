"""Read-only SQL for the MCP `query` tool, and the schema as DDL.

Defence in depth: the text must be one SELECT (or WITH ... SELECT) without `;`, PRAGMA or ATTACH, and it
runs on a connection that cannot write anyway: SQLite is opened separately with `mode=ro`, Postgres in a
READ ONLY transaction that is rolled back. Rows are capped and a query is stopped after TIMEOUT_S.
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.schema import CreateIndex, CreateTable

from gymbot.db.models import Base

MAX_LIMIT = 1000
TIMEOUT_S = 5.0
_START = re.compile(r"(select|with)\b", re.IGNORECASE)
_FORBIDDEN = re.compile(r"\b(pragma|attach|detach|load_extension)\b", re.IGNORECASE)


class QueryError(ValueError):
    """The query is refused or failed; the message is safe to show to the model."""


@dataclass
class Rows:
    columns: list[str]
    rows: list[list[Any]]
    truncated: bool


def check_select(sql: str) -> str:
    """The statement to run, or QueryError. One trailing `;` is tolerated and removed."""
    s = sql.strip()
    if s.endswith(";"):
        s = s[:-1].rstrip()
    if not _START.match(s):
        raise QueryError("only SELECT (or WITH ... SELECT) is allowed")
    if ";" in s:
        raise QueryError("one statement only: ';' is not allowed")
    if m := _FORBIDDEN.search(s):
        raise QueryError(f"{m.group(1).upper()} is not allowed")
    return s


def _cell(value: Any) -> Any:
    if isinstance(value, bytes | bytearray | memoryview):
        return f"<{len(value)} bytes>"
    return value


def _sqlite_rows(path: Path, sql: str, limit: int) -> Rows:
    # A separate read-only connection: even a statement that slipped through check_select cannot write.
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=TIMEOUT_S)
    try:
        deadline = time.monotonic() + TIMEOUT_S
        conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 10_000)
        cur = conn.execute(sql)
        columns = [d[0] for d in cur.description or []]
        fetched = cur.fetchmany(limit + 1)
    except sqlite3.OperationalError as e:
        if "interrupted" in str(e):
            raise QueryError(f"query took longer than {TIMEOUT_S:g} s") from e
        raise QueryError(str(e)) from e
    except sqlite3.Error as e:
        raise QueryError(str(e)) from e
    finally:
        conn.close()
    return Rows(columns, [[_cell(v) for v in r] for r in fetched[:limit]], len(fetched) > limit)


async def _postgres_rows(engine: AsyncEngine, sql: str, limit: int) -> Rows:
    async with engine.connect() as conn:  # leaving without commit rolls the transaction back
        await conn.execute(text("SET TRANSACTION READ ONLY"))
        await conn.execute(text(f"SET LOCAL statement_timeout = {int(TIMEOUT_S * 1000)}"))
        try:
            result = await conn.exec_driver_sql(sql)  # not text(): ':name' in the query is not a bind param
            columns = list(result.keys())
            fetched = result.fetchmany(limit + 1)
        except Exception as e:  # driver errors differ per driver; the message is what the model needs
            raise QueryError(str(getattr(e, "orig", e))) from e
        finally:
            await conn.rollback()
    return Rows(columns, [[_cell(v) for v in r] for r in fetched[:limit]], len(fetched) > limit)


def sqlite_path(engine: AsyncEngine) -> Path | None:
    """The SQLite file of `engine`, None for other dialects or an in-memory database."""
    if engine.url.get_backend_name() != "sqlite":
        return None
    db = engine.url.database
    return Path(db) if db and db != ":memory:" else None


async def run_select(engine: AsyncEngine, sql: str, limit: int) -> Rows:
    statement = check_select(sql)
    limit = max(1, min(limit, MAX_LIMIT))
    backend = engine.url.get_backend_name()
    if backend == "sqlite":
        path = sqlite_path(engine)
        if path is None:
            raise QueryError("in-memory SQLite cannot be opened read-only")
        return await asyncio.to_thread(_sqlite_rows, path, statement, limit)
    if backend == "postgresql":
        return await _postgres_rows(engine, statement, limit)
    raise QueryError(f"read-only queries are not supported for {backend}")


def schema_ddl(engine: AsyncEngine) -> str:
    """CREATE TABLE / CREATE INDEX for every model table, in the dialect of `engine`."""
    parts = []
    for table in Base.metadata.sorted_tables:
        parts.append(str(CreateTable(table).compile(dialect=engine.dialect)).strip() + ";")
        parts += [str(CreateIndex(ix).compile(dialect=engine.dialect)).strip() + ";" for ix in table.indexes]
    return "\n\n".join(parts)


async def database_size(engine: AsyncEngine) -> int | None:
    """Bytes on disk: the SQLite file plus its WAL, or pg_database_size; None when unknown."""
    path = sqlite_path(engine)
    if path is not None:
        return sum(p.stat().st_size for p in (path, Path(f"{path}-wal")) if p.exists())
    if engine.url.get_backend_name() == "postgresql":
        async with engine.connect() as conn:
            return await conn.scalar(text("SELECT pg_database_size(current_database())"))
    return None
