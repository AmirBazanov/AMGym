import datetime as dt
import json
import shutil
import time
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
import time_machine

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


def pytest_addoption(parser):
    parser.addoption(
        "--shift-days", type=int, default=0,
        help="run every test with the clock moved this many days ahead (catches tests tied to the real date)",
    )


@pytest.fixture(autouse=True)
def _shift_clock(request):
    days = request.config.getoption("--shift-days")
    if not days:
        yield
        return
    with time_machine.travel(dt.timedelta(days=days)):
        yield


@pytest.fixture(autouse=True)
def _no_real_claude(monkeypatch):
    """Settings read the process environment even with _env_file=None: a developer's ANTHROPIC_API_KEY (or a
    Claude Code shell's ANTHROPIC_MODEL) must not add a real Claude route to the tests."""
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_MODEL", "ANTHROPIC_ENABLED"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return make_settings(tmp_path)


SQLITE = "sqlite+aiosqlite:///"
# A migrated database with the programs imported, built once per programs dir and copied for every test:
# migrations and the import take ~0.25 s, a copy ~1 ms.
_TEMPLATES: dict[Path, Path] = {}


async def _template(programs_dir: Path, tmp_path_factory) -> Path:
    if (path := _TEMPLATES.get(programs_dir)) is None:
        path = tmp_path_factory.mktemp("template") / "t.db"
        engine, sm = make_engine(SQLITE + str(path))
        await upgrade_head(engine)
        async with sm() as session:
            await sync_programs(session, programs_dir)
        await engine.dispose()
        _TEMPLATES[programs_dir] = path
    return path


@pytest_asyncio.fixture
async def db(settings, tmp_path_factory):
    assert settings.database_url.startswith(SQLITE)
    shutil.copyfile(await _template(settings.programs_dir, tmp_path_factory), settings.database_url[len(SQLITE):])
    engine, sm = make_engine(settings.database_url)
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
