from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

Sessionmaker = async_sessionmaker[AsyncSession]


def make_engine(url: str) -> tuple[AsyncEngine, Sessionmaker]:
    if url.startswith("sqlite"):
        # Make sure the folder for the SQLite file exists (data/ is gitignored and may be missing).
        from pathlib import Path

        path = url.split(":///", 1)[-1]
        if path and path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
    engine = create_async_engine(url)
    return engine, async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
