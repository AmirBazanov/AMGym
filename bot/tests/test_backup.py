import gzip
import sqlite3
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest
from aiogram.exceptions import TelegramForbiddenError, TelegramNetworkError
from aiogram.methods import SendMessage
from conftest import make_settings
from pydantic import ValidationError

from gymbot.api import app as app_module
from gymbot.api.app import create_app
from gymbot.handlers import backup as handler
from gymbot.services import backup
from gymbot.services.users import get_or_create_user

MSK = ZoneInfo("Europe/Moscow")
# 04:00 in Moscow (UTC+3) on 8 October.
AT_HOUR = datetime(2026, 10, 8, 1, 0, tzinfo=UTC)
BEFORE_HOUR = datetime(2026, 10, 8, 0, 30, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _clear_pending():
    backup._pending.clear()
    yield
    backup._pending.clear()


class FakeBot:
    def __init__(self, document_error: Exception | None = None):
        self.documents: list[tuple[int, object, str | None]] = []
        self.messages: list[tuple[int, str]] = []
        self.document_error = document_error

    async def send_document(self, chat_id, document, caption=None):
        if self.document_error is not None:
            raise self.document_error
        self.documents.append((chat_id, document, caption))

    async def send_message(self, chat_id, text):
        self.messages.append((chat_id, text))


def forbidden() -> TelegramForbiddenError:
    return TelegramForbiddenError(
        method=SendMessage(chat_id=1, text="x"), message="Forbidden: bot was blocked"
    )


def network_error() -> TelegramNetworkError:
    return TelegramNetworkError(method=SendMessage(chat_id=1, text="x"), message="timeout")


def make_db(path, rows: int = 1000) -> None:
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    con.executemany("INSERT INTO t (v) VALUES (?)", [(f"row {i}",) for i in range(rows)])
    con.commit()
    con.close()


def unpack(gz_path, dest) -> sqlite3.Connection:
    dest.write_bytes(gzip.decompress(gz_path.read_bytes()))
    return sqlite3.connect(dest)


# --- make_backup -----------------------------------------------------------------------------------------------


def test_make_backup_is_valid_gzip_with_all_rows(tmp_path):
    src = tmp_path / "src.db"
    make_db(src)
    out = tmp_path / "out"
    now = datetime(2026, 10, 8, 1, 2, 3, tzinfo=UTC)

    path = backup.make_backup(src, out, now)

    assert path == out / "gym-20261008-010203Z.db.gz"
    con = unpack(path, tmp_path / "restored.db")
    assert con.execute("SELECT count(*) FROM t").fetchone() == (1000,)
    assert con.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    con.close()
    assert list(out.glob("*.tmp")) == []
    assert [p.name for p in out.iterdir()] == [path.name]


def test_make_backup_name_uses_utc_for_aware_non_utc_time(tmp_path):
    src = tmp_path / "src.db"
    make_db(src, 3)
    now = datetime(2026, 10, 8, 4, 0, tzinfo=MSK)
    assert backup.make_backup(src, tmp_path / "out", now).name == "gym-20261008-010000Z.db.gz"


def test_make_backup_skips_uncommitted_rows_of_another_connection(tmp_path):
    src = tmp_path / "src.db"
    make_db(src, 1000)
    writer = sqlite3.connect(src, isolation_level=None)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("INSERT INTO t (v) VALUES ('uncommitted')")
    try:
        path = backup.make_backup(src, tmp_path / "out", AT_HOUR)
    finally:
        writer.execute("ROLLBACK")
        writer.close()
    con = unpack(path, tmp_path / "restored.db")
    assert con.execute("SELECT count(*) FROM t").fetchone() == (1000,)
    assert con.execute("SELECT count(*) FROM t WHERE v = 'uncommitted'").fetchone() == (0,)
    con.close()


def test_make_backup_missing_source_raises_and_creates_nothing(tmp_path):
    src = tmp_path / "nope.db"
    with pytest.raises(FileNotFoundError):
        backup.make_backup(src, tmp_path / "out", AT_HOUR)
    assert not src.exists()


# --- rotate ----------------------------------------------------------------------------------------------------


def test_rotate_keeps_seven_newest_and_foreign_files(tmp_path):
    src = tmp_path / "src.db"
    make_db(src, 5)
    out = tmp_path / "out"
    made = [backup.make_backup(src, out, AT_HOUR + timedelta(seconds=i)) for i in range(9)]
    (out / "notes.txt").write_text("keep me")
    (out / backup.MARKER).write_text("2026-10-08")

    backup.rotate(out)

    left = sorted(out.glob(backup.PATTERN))
    assert left == sorted(made)[-7:]
    assert (out / "notes.txt").read_text() == "keep me"
    assert (out / backup.MARKER).exists()


def test_make_backup_rotates_by_itself(tmp_path):
    src = tmp_path / "src.db"
    make_db(src, 5)
    out = tmp_path / "out"
    for i in range(9):
        backup.make_backup(src, out, AT_HOUR + timedelta(seconds=i))
    assert len(list(out.glob(backup.PATTERN))) == backup.KEEP


# --- caption ---------------------------------------------------------------------------------------------------


def test_caption_exact_text_one_line():
    text = backup.caption(AT_HOUR.astimezone(MSK), 6 * 1024, "gym-20261008-010000Z.db.gz")
    assert text == (
        "Копия базы 08.10 04:00 — 6 КБ. "
        "Восстановить: остановить gymbot, gunzip -c gym-20261008-010000Z.db.gz > ~/amgym/data/gym.db, "
        "удалить gym.db-wal и gym.db-shm, запустить gymbot (deploy/README.md)."
    )
    assert "\n" not in text


def test_caption_tiny_file_shows_at_least_one_kb():
    assert "— 1 КБ." in backup.caption(AT_HOUR.astimezone(MSK), 10, "x.db.gz")


# --- sqlite_file -----------------------------------------------------------------------------------------------


def test_sqlite_file_absolute_url(tmp_path):
    assert backup.sqlite_file(f"sqlite+aiosqlite:///{tmp_path}/gym.db") == tmp_path / "gym.db"


@pytest.mark.parametrize(
    "url",
    ["sqlite+aiosqlite:///:memory:", "sqlite+aiosqlite://", "postgresql+asyncpg://u:p@localhost/gym"],
)
def test_sqlite_file_none_for_other_databases(url):
    assert backup.sqlite_file(url) is None


# --- due_day ---------------------------------------------------------------------------------------------------


def test_due_day_before_hour_is_none():
    assert backup.due_day(BEFORE_HOUR, MSK, 4, None) is None


def test_due_day_at_and_after_hour_is_today():
    assert backup.due_day(AT_HOUR, MSK, 4, None) == date(2026, 10, 8)
    assert backup.due_day(AT_HOUR + timedelta(hours=10), MSK, 4, None) == date(2026, 10, 8)


def test_due_day_uses_local_date_not_utc():
    # 22:00 UTC on the 8th is already 01:00 on the 9th in Moscow, before the hour; 01:00 UTC the 9th is 04:00.
    assert backup.due_day(datetime(2026, 10, 8, 22, 0, tzinfo=UTC), MSK, 4, None) is None
    assert backup.due_day(datetime(2026, 10, 9, 1, 0, tzinfo=UTC), MSK, 4, None) == date(2026, 10, 9)


def test_due_day_marker_today_is_none():
    assert backup.due_day(AT_HOUR, MSK, 4, date(2026, 10, 8)) is None


def test_due_day_marker_yesterday_is_today():
    assert backup.due_day(AT_HOUR, MSK, 4, date(2026, 10, 7)) == date(2026, 10, 8)


# --- daily_tick ------------------------------------------------------------------------------------------------


def daily_settings(tmp_path, **kw):
    return make_settings(tmp_path, allowed_user_ids=[42], timezone="Europe/Moscow", **kw)


async def test_daily_tick_before_hour_sends_nothing(tmp_path, db):
    bot = FakeBot()
    assert await backup.daily_tick(bot, db, daily_settings(tmp_path), BEFORE_HOUR) is False
    assert bot.documents == [] and bot.messages == []
    assert not (tmp_path / "backups").exists()


async def test_daily_tick_sends_once_per_day_then_again_next_day(tmp_path, db):
    settings = daily_settings(tmp_path)
    bot = FakeBot()
    marker = tmp_path / "backups" / backup.MARKER

    assert await backup.daily_tick(bot, db, settings, AT_HOUR) is True
    assert len(bot.documents) == 1
    chat_id, document, cap = bot.documents[0]
    assert chat_id == 42
    assert cap == backup.caption(
        AT_HOUR.astimezone(MSK), (tmp_path / "backups" / document.filename).stat().st_size, document.filename
    )
    assert marker.read_text() == "2026-10-08"

    # Double-send guard.
    assert await backup.daily_tick(bot, db, settings, AT_HOUR + timedelta(hours=1)) is False
    assert len(bot.documents) == 1

    # Next local day after the hour.
    assert await backup.daily_tick(bot, db, settings, AT_HOUR + timedelta(days=1)) is True
    assert len(bot.documents) == 2
    assert marker.read_text() == "2026-10-09"


async def test_daily_tick_document_is_a_restorable_copy(tmp_path, db):
    bot = FakeBot()
    await backup.daily_tick(bot, db, daily_settings(tmp_path), AT_HOUR)
    gz = tmp_path / "backups" / bot.documents[0][1].filename
    con = unpack(gz, tmp_path / "restored.db")
    assert con.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    assert con.execute("SELECT count(*) FROM exercises").fetchone()[0] > 0
    con.close()


async def test_daily_tick_owner_falls_back_to_first_user(tmp_path, db):
    settings = make_settings(tmp_path, allowed_user_ids=[], timezone="Europe/Moscow")
    async with db() as session:
        await get_or_create_user(session, 555, "Amir")
        await get_or_create_user(session, 556, "Other")
        await session.commit()
    bot = FakeBot()

    assert await backup.daily_tick(bot, db, settings, AT_HOUR) is True

    assert [d[0] for d in bot.documents] == [555]


async def test_daily_tick_without_owner_sends_nothing_and_no_marker(tmp_path, db):
    settings = make_settings(tmp_path, allowed_user_ids=[], timezone="Europe/Moscow")
    bot = FakeBot()

    assert await backup.daily_tick(bot, db, settings, AT_HOUR) is False

    assert bot.documents == [] and bot.messages == []
    assert not (tmp_path / "backups").exists()


async def test_daily_tick_non_sqlite_does_nothing(tmp_path, db):
    settings = make_settings(tmp_path, allowed_user_ids=[42], timezone="Europe/Moscow")
    settings.database_url = "postgresql+asyncpg://u:p@localhost/gym"
    bot = FakeBot()
    assert await backup.daily_tick(bot, db, settings, AT_HOUR) is False
    assert bot.documents == []


async def test_daily_tick_too_big_sends_warning_and_marks_day(tmp_path, db, monkeypatch):
    monkeypatch.setattr(backup, "MAX_SEND_BYTES", 10)
    bot = FakeBot()

    assert await backup.daily_tick(bot, db, daily_settings(tmp_path), AT_HOUR) is True

    assert bot.documents == []
    assert len(bot.messages) == 1
    chat_id, text = bot.messages[0]
    assert chat_id == 42
    assert "больше лимита Telegram" in text
    assert (tmp_path / "backups" / backup.MARKER).read_text() == "2026-10-08"
    # No retry spam on the next check.
    assert await backup.daily_tick(bot, db, daily_settings(tmp_path), AT_HOUR + timedelta(minutes=1)) is False
    assert len(bot.messages) == 1


async def test_daily_tick_transient_error_keeps_day_open_and_reuses_the_file(tmp_path, db):
    settings = daily_settings(tmp_path)
    out = tmp_path / "backups"

    with pytest.raises(TelegramNetworkError):
        await backup.daily_tick(FakeBot(document_error=network_error()), db, settings, AT_HOUR)
    assert not (out / backup.MARKER).exists()
    first = sorted(out.glob(backup.PATTERN))
    assert len(first) == 1

    bot = FakeBot()
    retry_at = AT_HOUR + backup.RETRY
    assert await backup.daily_tick(bot, db, settings, retry_at) is True

    assert sorted(out.glob(backup.PATTERN)) == first  # no second copy made
    assert bot.documents[0][1].filename == first[0].name
    assert (out / backup.MARKER).read_text() == "2026-10-08"
    assert backup._pending == {}


async def test_daily_tick_makes_a_new_copy_if_pending_file_vanished(tmp_path, db):
    settings = daily_settings(tmp_path)
    out = tmp_path / "backups"
    with pytest.raises(TelegramNetworkError):
        await backup.daily_tick(FakeBot(document_error=network_error()), db, settings, AT_HOUR)
    for f in out.glob(backup.PATTERN):
        f.unlink()

    bot = FakeBot()
    assert await backup.daily_tick(bot, db, settings, AT_HOUR + backup.RETRY) is True
    assert len(bot.documents) == 1


async def test_daily_tick_permanent_error_does_not_raise_and_marks_day(tmp_path, db):
    bot = FakeBot(document_error=forbidden())

    assert await backup.daily_tick(bot, db, daily_settings(tmp_path), AT_HOUR) is True

    assert (tmp_path / "backups" / backup.MARKER).read_text() == "2026-10-08"
    assert backup._pending == {}


# --- /backup handler -------------------------------------------------------------------------------------------


def make_message(user_id: int, chat_id: int | None = None):
    answers: list[str] = []

    async def answer(text, *a, **kw):
        answers.append(text)

    msg = SimpleNamespace(
        from_user=SimpleNamespace(id=user_id, full_name="Amir"),
        chat=SimpleNamespace(id=chat_id if chat_id is not None else user_id),
        bot=FakeBot(),
        answer=answer,
    )
    return msg, answers


async def test_backup_command_sends_document_to_owner_without_marker(tmp_path, db):
    settings = make_settings(tmp_path, allowed_user_ids=[42], timezone="Europe/Moscow")
    msg, answers = make_message(42)

    await handler.backup_now(msg, settings, db)

    assert len(msg.bot.documents) == 1
    assert msg.bot.documents[0][0] == 42
    assert answers == []
    assert not (tmp_path / "backups" / backup.MARKER).exists()
    assert len(list((tmp_path / "backups").glob(backup.PATTERN))) == 1


@pytest.mark.parametrize("allowed", [[42], [42, 77]])
async def test_backup_command_refuses_non_owner(tmp_path, db, allowed):
    settings = make_settings(tmp_path, allowed_user_ids=allowed, timezone="Europe/Moscow")
    msg, answers = make_message(77)

    await handler.backup_now(msg, settings, db)

    assert answers == [handler.OWNER_ONLY]
    assert msg.bot.documents == []
    assert not (tmp_path / "backups").exists()


async def test_backup_command_non_sqlite_answers_not_sqlite(tmp_path, db, monkeypatch):
    settings = make_settings(tmp_path, allowed_user_ids=[42], timezone="Europe/Moscow")
    monkeypatch.setattr(backup, "sqlite_file", lambda url: None)
    msg, answers = make_message(42)

    await handler.backup_now(msg, settings, db)

    assert answers == [handler.NOT_SQLITE]
    assert msg.bot.documents == []


async def test_backup_command_failure_answers_failed(tmp_path, db):
    settings = make_settings(tmp_path, allowed_user_ids=[42], timezone="Europe/Moscow")
    msg, answers = make_message(42)
    msg.bot = FakeBot(document_error=network_error())

    await handler.backup_now(msg, settings, db)

    assert answers == [handler.FAILED]


# --- /api/health -----------------------------------------------------------------------------------------------


async def test_health_ok(client):
    r = await client.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


class _BrokenSession:
    def __init__(self, delay: float, error: Exception | None):
        self.delay = delay
        self.error = error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, *a, **kw):
        if self.delay:
            import asyncio

            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error


def _health_client(settings, session: _BrokenSession) -> httpx.AsyncClient:
    app = create_app(settings, lambda: session)  # type: ignore[arg-type]
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def test_health_503_when_database_ping_fails(settings):
    async with _health_client(settings, _BrokenSession(0, RuntimeError("db down"))) as c:
        r = await c.get("/api/health")
    assert r.status_code == 503


async def test_health_503_when_database_ping_hangs(settings, monkeypatch):
    monkeypatch.setattr(app_module, "HEALTH_TIMEOUT", 0.05)
    async with _health_client(settings, _BrokenSession(5, None)) as c:
        r = await c.get("/api/health")
    assert r.status_code == 503


# --- Settings.backup_enabled -----------------------------------------------------------------------------------


def test_backup_enabled_defaults_off_outside_webhook(tmp_path):
    assert make_settings(tmp_path).backup_enabled is False


def test_backup_enabled_defaults_on_for_webhook(tmp_path):
    s = make_settings(tmp_path, bot_mode="webhook", public_url="https://x.test")
    assert s.backup_enabled is True


def test_backup_enabled_explicit_value_wins(tmp_path):
    off = make_settings(tmp_path, bot_mode="webhook", public_url="https://x.test", backup_enabled=False)
    on = make_settings(tmp_path, backup_enabled=True)
    assert off.backup_enabled is False
    assert on.backup_enabled is True


@pytest.mark.parametrize("hour", [24, -1])
def test_backup_hour_out_of_range_rejected(tmp_path, hour):
    with pytest.raises(ValidationError):
        make_settings(tmp_path, backup_hour=hour)
