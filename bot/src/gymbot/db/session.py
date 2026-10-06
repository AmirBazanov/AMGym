from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from gymbot.config import get_settings

engine = create_async_engine(get_settings().database_url)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
