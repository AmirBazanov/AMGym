import json
import time

import httpx
import pytest
import pytest_asyncio

from gymbot.api.app import create_app
from gymbot.api.auth import sign_init_data
from gymbot.config import Settings
from gymbot.db.migrate import upgrade_head
from gymbot.db.session import make_engine
from gymbot.services.programs import sync_programs

TOKEN = "123:abc"


def make_settings(tmp_path, **kw) -> Settings:
    # _env_file=None: a developer's real .env must not leak into tests.
    return Settings(
        _env_file=None,
        bot_token=TOKEN,
        database_url=f"sqlite+aiosqlite:///{tmp_path}/t.db",
        **kw,
    )


def init_data(user_id: int = 42, name: str = "Amir", token: str = TOKEN) -> str:
    return sign_init_data(
        {"auth_date": str(int(time.time())), "user": json.dumps({"id": user_id, "first_name": name})}, token
    )


@pytest.fixture
def settings(tmp_path) -> Settings:
    return make_settings(tmp_path)


@pytest_asyncio.fixture
async def db(settings):
    engine, sm = make_engine(settings.database_url)
    await upgrade_head(engine)
    async with sm() as session:
        await sync_programs(session, settings.programs_dir)
    yield sm
    await engine.dispose()


@pytest.fixture
def make_client(db):
    clients: list[httpx.AsyncClient] = []

    def factory(settings: Settings) -> httpx.AsyncClient:
        c = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(settings, db)), base_url="http://t")
        clients.append(c)
        return c

    yield factory


@pytest_asyncio.fixture
async def client(settings, make_client):
    async with make_client(settings) as c:
        yield c


@pytest.fixture
def auth():
    return {"X-Telegram-Init-Data": init_data()}
