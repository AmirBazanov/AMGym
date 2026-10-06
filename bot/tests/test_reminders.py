from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError
from aiogram.methods import SendMessage
from sqlalchemy import select

from gymbot.config import Settings
from gymbot.db.models import FoodEntry, Reminder, User
from gymbot.llm.openrouter import LLMError
from gymbot.services import advice as advice_module
from gymbot.services import reminders as rem_module
from gymbot.services.reminders import (
    CLOSED,
    GRACE,
    NO_TARGETS,
    OPEN_NUTRITION,
    due_day,
    hhmm_to_minute,
    initial_last_sent,
    minute_to_hhmm,
    nutrition_text,
    tick,
)

MSK = ZoneInfo("Europe/Moscow")
DAY = date(2026, 10, 6)
YESTERDAY = DAY - timedelta(days=1)
TOMORROW = DAY + timedelta(days=1)
M0930 = 9 * 60 + 30
URL = "https://example.test/app"
ALLOWED = [42, 77, 555]  # every telegram id the tests create


def at(h: int, m: int = 0, day: date = DAY) -> datetime:
    """UTC moment of the given Moscow wall-clock time."""
    return datetime(day.year, day.month, day.day, h, m, tzinfo=MSK).astimezone(UTC)


class FakeBot:
    def __init__(self) -> None:
        self.calls: list[tuple[int, str, object]] = []

    async def send_message(self, chat_id, text, reply_markup=None, **kw):
        self.calls.append((chat_id, text, reply_markup))


class FailingBot(FakeBot):
    def __init__(self, exc: Exception) -> None:
        super().__init__()
        self.exc = exc

    async def send_message(self, chat_id, text, reply_markup=None, **kw):
        raise self.exc


def forbidden() -> TelegramForbiddenError:
    return TelegramForbiddenError(method=SendMessage(chat_id=1, text="x"), message="Forbidden: bot was blocked")


def network_error() -> TelegramNetworkError:
    return TelegramNetworkError(method=SendMessage(chat_id=1, text="x"), message="timeout")


async def _user(db, telegram_id: int = 42, **fields) -> int:
    async with db() as s:
        user = await s.scalar(select(User).where(User.telegram_id == telegram_id))
        if user is None:
            user = User(telegram_id=telegram_id, rest_seconds=90)
            s.add(user)
        for k, v in fields.items():
            setattr(user, k, v)
        await s.commit()
        return user.id


async def _rem(db, user_id: int, minute: int = M0930, kind="text", text="Креатин", enabled=True,
               last_sent_on: date | None = None, weekday: int | None = None) -> int:
    async with db() as s:
        r = Reminder(user_id=user_id, minute_of_day=minute, kind=kind, text=text, enabled=enabled,
                     last_sent_on=last_sent_on, weekday=weekday)
        s.add(r)
        await s.commit()
        return r.id


async def _food(db, user_id: int, eaten_at: datetime, kcal: str, protein: str) -> None:
    async with db() as s:
        s.add(FoodEntry(user_id=user_id, eaten_at=eaten_at, description="еда", grams=Decimal(100),
                        kcal=Decimal(kcal), protein_g=Decimal(protein), fat_g=Decimal(1),
                        carbs_g=Decimal(1), estimated=True))
        await s.commit()


def _settings(miniapp_url: str = "", allowed: list[int] | None = None) -> Settings:
    # _env_file=None: a developer's real .env must not leak into tests.
    return Settings(
        _env_file=None, bot_token="1:x", miniapp_url=miniapp_url,
        allowed_user_ids=ALLOWED if allowed is None else allowed,
    )


async def _tick(db, bot, now: datetime, tz: ZoneInfo = MSK, miniapp_url: str = "",
                allowed: list[int] | None = None, llm=None) -> int:
    async with db() as s:  # a fresh session per call, like the loop
        return await tick(s, bot, now, tz, settings=_settings(miniapp_url, allowed), llm=llm)


async def _last_sent(db, reminder_id: int) -> date | None:
    async with db() as s:
        return await s.scalar(select(Reminder.last_sent_on).where(Reminder.id == reminder_id))


# ---- acceptance: sending ----


async def test_sends_at_due_exactly(db):
    uid = await _user(db)
    rid = await _rem(db, uid)
    bot = FakeBot()
    assert await _tick(db, bot, at(9, 30)) == 1
    assert bot.calls == [(42, "Креатин", None)]
    assert await _last_sent(db, rid) == DAY


async def test_second_tick_same_minute_sends_nothing(db):
    uid = await _user(db)
    await _rem(db, uid)
    bot = FakeBot()
    assert await _tick(db, bot, at(9, 30)) == 1
    assert await _tick(db, bot, at(9, 30)) == 0
    assert await _tick(db, bot, at(9, 31)) == 0
    assert len(bot.calls) == 1


async def test_restart_inside_grace_sends_once(db):
    uid = await _user(db)
    await _rem(db, uid)
    bot = FakeBot()
    # The process was down at 09:30 and starts again at 09:40: nothing sent yet today.
    assert await _tick(db, bot, at(9, 40)) == 1
    # Another restart later in the same window.
    assert await _tick(db, bot, at(9, 41)) == 0
    assert await _tick(db, bot, at(9, 59)) == 0
    assert len(bot.calls) == 1


async def test_not_sent_before_due(db):
    uid = await _user(db)
    rid = await _rem(db, uid, last_sent_on=YESTERDAY)
    bot = FakeBot()
    assert await _tick(db, bot, at(9, 29)) == 0
    assert await _tick(db, bot, at(0, 0)) == 0
    assert bot.calls == []
    assert await _last_sent(db, rid) == YESTERDAY


@pytest.mark.parametrize("h,m", [(10, 0), (10, 1), (12, 0), (21, 0)])
async def test_not_sent_at_or_after_grace_end(db, h, m):
    uid = await _user(db)
    await _rem(db, uid, last_sent_on=YESTERDAY)
    bot = FakeBot()
    assert await _tick(db, bot, at(h, m)) == 0
    assert bot.calls == []


async def test_last_minute_of_grace_still_sends(db):
    uid = await _user(db)
    await _rem(db, uid)
    bot = FakeBot()
    assert await _tick(db, bot, at(9, 59)) == 1
    assert GRACE == timedelta(minutes=30)


async def test_disabled_reminder_not_sent(db):
    uid = await _user(db)
    await _rem(db, uid, enabled=False)
    bot = FakeBot()
    assert await _tick(db, bot, at(9, 30)) == 0
    assert bot.calls == []


async def test_sent_again_next_day(db):
    uid = await _user(db)
    rid = await _rem(db, uid)
    bot = FakeBot()
    assert await _tick(db, bot, at(9, 30)) == 1
    assert await _tick(db, bot, at(9, 30, TOMORROW)) == 1
    assert await _last_sent(db, rid) == TOMORROW
    assert len(bot.calls) == 2


# ---- created at an already-passed time ----


async def test_created_after_due_waits_until_next_day(db):
    uid = await _user(db)
    now = at(9, 40)  # created 10 minutes after 09:30
    last = initial_last_sent(M0930, now, MSK)
    assert last == DAY
    rid = await _rem(db, uid, last_sent_on=last)
    bot = FakeBot()
    assert await _tick(db, bot, now) == 0
    assert await _tick(db, bot, at(9, 55)) == 0
    assert await _tick(db, bot, at(9, 30, TOMORROW)) == 1
    assert await _last_sent(db, rid) == TOMORROW
    assert len(bot.calls) == 1


async def test_created_before_due_sends_today(db):
    uid = await _user(db)
    now = at(8, 0)
    last = initial_last_sent(M0930, now, MSK)
    assert last == YESTERDAY
    await _rem(db, uid, last_sent_on=last)
    bot = FakeBot()
    assert await _tick(db, bot, now) == 0
    assert await _tick(db, bot, at(9, 30)) == 1


def test_initial_last_sent_boundaries():
    assert initial_last_sent(M0930, at(9, 29), MSK) == YESTERDAY
    assert initial_last_sent(M0930, at(9, 30), MSK) == DAY
    assert initial_last_sent(M0930, at(23, 59), MSK) == DAY
    assert initial_last_sent(0, at(0, 0), MSK) == DAY
    assert initial_last_sent(23 * 60 + 59, at(0, 0), MSK) == YESTERDAY


async def test_created_late_evening_for_spilling_time_not_fired_for_yesterday(db):
    # Created 00:10 for 23:50: today's 23:50 is in the future, yesterday's window is still open,
    # but yesterday must not fire.
    uid = await _user(db)
    now = at(0, 10, TOMORROW)
    last = initial_last_sent(23 * 60 + 50, now, MSK)
    assert last == DAY
    await _rem(db, uid, minute=23 * 60 + 50, last_sent_on=last)
    bot = FakeBot()
    assert await _tick(db, bot, now) == 0
    assert await _tick(db, bot, at(23, 50, TOMORROW)) == 1


# ---- failures ----


async def test_send_failure_reverts_claim_and_retries(db):
    uid = await _user(db)
    rid = await _rem(db, uid, last_sent_on=YESTERDAY)
    assert await _tick(db, FailingBot(RuntimeError("boom")), at(9, 30)) == 0
    assert await _last_sent(db, rid) == YESTERDAY
    assert await _tick(db, FailingBot(network_error()), at(9, 31)) == 0
    assert await _last_sent(db, rid) == YESTERDAY
    bot = FakeBot()
    assert await _tick(db, bot, at(9, 40)) == 1
    assert bot.calls == [(42, "Креатин", None)]
    assert await _last_sent(db, rid) == DAY
    assert await _tick(db, bot, at(9, 41)) == 0


async def test_send_failure_reverts_to_none_when_never_sent(db):
    uid = await _user(db)
    rid = await _rem(db, uid, last_sent_on=None)
    assert await _tick(db, FailingBot(RuntimeError("boom")), at(9, 30)) == 0
    assert await _last_sent(db, rid) is None


async def test_failure_of_one_reminder_does_not_block_others(db):
    uid = await _user(db)
    other = await _user(db, telegram_id=77)
    await _rem(db, uid, text="первое")
    await _rem(db, other, text="второе")

    class FlakyBot(FakeBot):
        async def send_message(self, chat_id, text, reply_markup=None, **kw):
            if chat_id == 42:
                raise RuntimeError("boom")
            await super().send_message(chat_id, text, reply_markup)

    bot = FlakyBot()
    assert await _tick(db, bot, at(9, 30)) == 1
    assert bot.calls == [(77, "второе", None)]


async def test_forbidden_keeps_claim_and_does_not_retry(db):
    uid = await _user(db)
    rid = await _rem(db, uid, last_sent_on=YESTERDAY)
    assert await _tick(db, FailingBot(forbidden()), at(9, 30)) == 0
    assert await _last_sent(db, rid) == DAY
    bot = FakeBot()
    assert await _tick(db, bot, at(9, 31)) == 0
    assert bot.calls == []
    # Tomorrow it is tried again.
    assert await _tick(db, bot, at(9, 30, TOMORROW)) == 1


# ---- several users, midnight spill ----


async def test_goes_to_owners_telegram_id(db):
    a = await _user(db, telegram_id=42)
    b = await _user(db, telegram_id=555)
    await _rem(db, a, text="для А")
    await _rem(db, b, text="для Б")
    bot = FakeBot()
    assert await _tick(db, bot, at(9, 30)) == 2
    assert sorted((c[0], c[1]) for c in bot.calls) == [(42, "для А"), (555, "для Б")]


async def test_midnight_spill_sends_at_00_10_once(db):
    uid = await _user(db)
    rid = await _rem(db, uid, minute=23 * 60 + 50, last_sent_on=YESTERDAY)  # sent nothing on Oct 6 yet
    bot = FakeBot()
    now = at(0, 10, TOMORROW)  # 00:10 local on Oct 7
    assert await _tick(db, bot, now) == 1
    assert await _last_sent(db, rid) == DAY  # the local date of the occurrence, not of "now"
    assert await _tick(db, bot, now) == 0
    assert await _tick(db, bot, at(0, 15, TOMORROW)) == 0
    assert len(bot.calls) == 1


async def test_spill_not_sent_if_already_sent_at_2350(db):
    uid = await _user(db)
    await _rem(db, uid, minute=23 * 60 + 50)
    bot = FakeBot()
    assert await _tick(db, bot, at(23, 50)) == 1
    assert await _tick(db, bot, at(0, 10, TOMORROW)) == 0
    assert len(bot.calls) == 1


async def test_spill_window_closes_at_00_20(db):
    uid = await _user(db)
    await _rem(db, uid, minute=23 * 60 + 50, last_sent_on=YESTERDAY)
    bot = FakeBot()
    assert await _tick(db, bot, at(0, 20, TOMORROW)) == 0
    assert await _tick(db, bot, at(0, 19, TOMORROW)) == 1


async def test_midnight_reminder_00_00_sends_at_00_00(db):
    uid = await _user(db)
    rid = await _rem(db, uid, minute=0, last_sent_on=YESTERDAY)
    bot = FakeBot()
    assert await _tick(db, bot, at(0, 0)) == 1
    assert await _last_sent(db, rid) == DAY


def test_due_day():
    assert due_day(M0930, at(9, 30), MSK) == DAY
    assert due_day(M0930, at(9, 59), MSK) == DAY
    assert due_day(M0930, at(9, 29), MSK) is None
    assert due_day(M0930, at(10, 0), MSK) is None
    assert due_day(23 * 60 + 50, at(0, 10, TOMORROW), MSK) == DAY


# ---- nutrition reminders ----


async def test_nutrition_text_with_targets(db):
    uid = await _user(db, kcal_target=2000, protein_target_g=150)
    await _food(db, uid, at(8, 0), "700", "60")
    await _food(db, uid, at(9, 0), "540", "42")
    # Yesterday evening and tomorrow do not count towards today.
    await _food(db, uid, at(23, 0, YESTERDAY), "900", "90")
    await _food(db, uid, at(12, 0, TOMORROW), "900", "90")
    await _rem(db, uid, kind="nutrition", text=None, last_sent_on=YESTERDAY)
    bot = FakeBot()
    assert await _tick(db, bot, at(9, 30), miniapp_url=URL) == 1
    chat_id, text, markup = bot.calls[0]
    assert chat_id == 42
    assert text == "Добей КБЖУ: осталось 760 ккал / 48 г белка"
    assert markup is not None
    buttons = [b for row in markup.inline_keyboard for b in row]
    assert len(buttons) == 1
    assert buttons[0].text == OPEN_NUTRITION == "Открыть питание"
    assert buttons[0].web_app is not None and buttons[0].web_app.url == URL


async def test_nutrition_without_miniapp_url_has_no_keyboard(db):
    uid = await _user(db, kcal_target=2000, protein_target_g=150)
    await _rem(db, uid, kind="nutrition", text=None, last_sent_on=YESTERDAY)
    bot = FakeBot()
    assert await _tick(db, bot, at(9, 30), miniapp_url="") == 1
    assert bot.calls[0][1] == "Добей КБЖУ: осталось 2000 ккал / 150 г белка"
    assert bot.calls[0][2] is None


async def test_nutrition_without_targets(db):
    uid = await _user(db)
    await _rem(db, uid, kind="nutrition", text=None, last_sent_on=YESTERDAY)
    bot = FakeBot()
    assert await _tick(db, bot, at(9, 30), miniapp_url=URL) == 1
    assert bot.calls[0][1] == NO_TARGETS


async def test_nutrition_over_the_norm_is_closed(db):
    uid = await _user(db, kcal_target=2000, protein_target_g=100)
    await _food(db, uid, at(8, 0), "2100", "120")
    await _rem(db, uid, kind="nutrition", text=None, last_sent_on=YESTERDAY)
    bot = FakeBot()
    assert await _tick(db, bot, at(9, 30), miniapp_url=URL) == 1
    assert bot.calls[0][1] == CLOSED


async def test_nutrition_only_kcal_target(db):
    uid = await _user(db, kcal_target=2000)
    await _food(db, uid, at(8, 0), "500", "30")
    await _rem(db, uid, kind="nutrition", text=None, last_sent_on=YESTERDAY)
    bot = FakeBot()
    assert await _tick(db, bot, at(9, 30)) == 1
    assert bot.calls[0][1] == "Добей КБЖУ: осталось 1500 ккал"


async def test_text_reminder_has_no_keyboard_even_with_url(db):
    uid = await _user(db)
    await _rem(db, uid, last_sent_on=YESTERDAY)
    bot = FakeBot()
    assert await _tick(db, bot, at(9, 30), miniapp_url=URL) == 1
    assert bot.calls[0][2] is None


# ---- pure units ----


def test_nutrition_text_exact():
    assert nutrition_text(760, 48) == "Добей КБЖУ: осталось 760 ккал / 48 г белка"
    assert nutrition_text(760.4, 47.6) == "Добей КБЖУ: осталось 760 ккал / 48 г белка"


def test_nutrition_text_no_targets():
    assert nutrition_text(None, None) == NO_TARGETS


def test_nutrition_text_only_kcal_or_only_protein():
    assert nutrition_text(500, None) == "Добей КБЖУ: осталось 500 ккал"
    assert nutrition_text(None, 30) == "Добей КБЖУ: осталось 30 г белка"


def test_nutrition_text_rounding_to_zero_is_closed():
    assert nutrition_text(0.4, 0.4) == CLOSED
    assert nutrition_text(0.4, None) == CLOSED
    assert nutrition_text(None, 0.4) == CLOSED
    assert nutrition_text(0, 0) == CLOSED
    assert nutrition_text(-100, -5) == CLOSED


def test_nutrition_text_one_met_one_open():
    assert nutrition_text(0.4, 30) == "Добей КБЖУ: осталось 30 г белка"
    assert nutrition_text(-50, 30) == "Добей КБЖУ: осталось 30 г белка"
    assert nutrition_text(300, -5) == "Добей КБЖУ: осталось 300 ккал"


def test_hhmm_conversion():
    assert hhmm_to_minute("09:30") == 570
    assert hhmm_to_minute("00:00") == 0
    assert hhmm_to_minute("23:59") == 1439
    assert minute_to_hhmm(570) == "09:30"
    assert minute_to_hhmm(0) == "00:00"
    assert minute_to_hhmm(5) == "00:05"
    assert minute_to_hhmm(1439) == "23:59"


def test_hhmm_roundtrip_all_minutes():
    assert all(hhmm_to_minute(minute_to_hhmm(m)) == m for m in range(1440))


async def test_concurrent_checks_send_once(db, monkeypatch):
    """Two processes read the same due reminder before either claims it: only one sends.

    The second process runs its whole check while the first is between reading and claiming,
    so the first one's conditional UPDATE matches no row (rowcount 0) and it must not send.
    """
    from gymbot.services import reminders as rem

    rid = await _rem(db, await _user(db))
    other = FakeBot()
    original = rem._build
    raced = False

    async def build_then_race(session, item, *args):
        nonlocal raced
        if not raced:
            raced = True
            assert await _tick(db, other, at(9, 30)) == 1
        return await original(session, item, *args)

    monkeypatch.setattr(rem, "_build", build_then_race)
    bot = FakeBot()
    assert await _tick(db, bot, at(9, 30)) == 0
    assert bot.calls == [] and len(other.calls) == 1
    assert await _last_sent(db, rid) == DAY


# ---- DST: the window is computed on the UTC timeline ----

BERLIN = ZoneInfo("Europe/Berlin")
SPRING = date(2027, 3, 28)  # 02:00 CET -> 03:00 CEST, 02:10 does not exist
AUTUMN = date(2027, 10, 31)  # 03:00 CEST -> 02:00 CET, 02:10 happens twice
M0210 = 2 * 60 + 10


def utc_at(day: date, h: int, m: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, h, m, tzinfo=UTC)


async def test_dst_spring_gap_time_fires_once_at_shifted_wall_clock(db):
    rid = await _rem(db, await _user(db), minute=M0210)
    bot = FakeBot()
    assert await _tick(db, bot, utc_at(SPRING, 0, 50), BERLIN) == 0  # 01:50 CET, before
    assert await _tick(db, bot, utc_at(SPRING, 1, 10), BERLIN) == 1  # 03:10 CEST
    assert await _tick(db, bot, utc_at(SPRING, 1, 20), BERLIN) == 0
    assert len(bot.calls) == 1 and await _last_sent(db, rid) == SPRING
    assert await _tick(db, bot, utc_at(SPRING, 1, 40), BERLIN) == 0  # window closed (03:40 CEST)


async def test_dst_spring_gap_window_end_on_utc_timeline(db):
    await _rem(db, await _user(db), minute=M0210)
    bot = FakeBot()
    assert await _tick(db, bot, utc_at(SPRING, 1, 39), BERLIN) == 1  # 03:39 CEST, still inside GRACE


def test_initial_last_sent_spring_gap_not_yet_passed():
    # 03:05 CEST: the 02:10 occurrence maps to 03:10 CEST, so it is still ahead today.
    assert initial_last_sent(M0210, utc_at(SPRING, 1, 5), BERLIN) == SPRING - timedelta(days=1)
    assert initial_last_sent(M0210, utc_at(SPRING, 1, 10), BERLIN) == SPRING


async def test_dst_autumn_repeated_time_fires_once(db):
    rid = await _rem(db, await _user(db), minute=M0210)
    bot = FakeBot()
    assert await _tick(db, bot, utc_at(AUTUMN, 0, 10), BERLIN) == 1  # first 02:10 (CEST)
    assert await _tick(db, bot, utc_at(AUTUMN, 1, 10), BERLIN) == 0  # second 02:10 (CET)
    assert await _tick(db, bot, utc_at(AUTUMN, 1, 15), BERLIN) == 0
    assert len(bot.calls) == 1 and await _last_sent(db, rid) == AUTUMN


# ---- the claim re-checks the row, not only last_sent_on ----


async def _race_build(monkeypatch, db, rid: int, **changes) -> None:
    """Edit the reminder from another session while tick() is between reading and claiming."""
    from gymbot.services import reminders as rem

    original = rem._build

    async def build_then_edit(session, item, *args):
        async with db() as other:
            r = await other.get(Reminder, rid)
            for k, v in changes.items():
                setattr(r, k, v)
            await other.commit()
        return await original(session, item, *args)

    monkeypatch.setattr(rem, "_build", build_then_edit)


async def test_claim_skips_reminder_disabled_meanwhile(db, monkeypatch):
    rid = await _rem(db, await _user(db), last_sent_on=YESTERDAY)
    await _race_build(monkeypatch, db, rid, enabled=False)
    bot = FakeBot()
    assert await _tick(db, bot, at(9, 30)) == 0
    assert bot.calls == [] and await _last_sent(db, rid) == YESTERDAY


async def test_claim_skips_reminder_retimed_meanwhile(db, monkeypatch):
    rid = await _rem(db, await _user(db), last_sent_on=YESTERDAY)
    await _race_build(monkeypatch, db, rid, minute_of_day=18 * 60)
    bot = FakeBot()
    assert await _tick(db, bot, at(9, 30)) == 0
    assert bot.calls == [] and await _last_sent(db, rid) == YESTERDAY


# ---- permanent Telegram errors are not retried ----


async def test_bad_request_keeps_claim_and_is_not_retried(db):
    rid = await _rem(db, await _user(db))
    err = TelegramBadRequest(method=SendMessage(chat_id=1, text="x"), message="Bad Request: chat not found")
    assert await _tick(db, FailingBot(err), at(9, 30)) == 0
    assert await _last_sent(db, rid) == DAY
    bot = FakeBot()
    assert await _tick(db, bot, at(9, 31)) == 0
    assert bot.calls == []
    assert await _tick(db, bot, at(9, 30, TOMORROW)) == 1


# ---- access: same rule as the handlers (gymbot.services.access.is_allowed) ----


async def test_user_removed_from_allowed_ids_gets_nothing(db):
    owner = await _rem(db, await _user(db, telegram_id=42))
    removed = await _rem(db, await _user(db, telegram_id=555))
    bot = FakeBot()
    assert await _tick(db, bot, at(9, 30), allowed=[42]) == 1
    assert [c[0] for c in bot.calls] == [42]
    assert await _last_sent(db, owner) == DAY
    assert await _last_sent(db, removed) is None  # not claimed: nothing was sent


async def test_without_allowed_ids_only_the_owner_gets_reminders(db):
    await _rem(db, await _user(db, telegram_id=555))  # first user = owner
    await _rem(db, await _user(db, telegram_id=42))
    bot = FakeBot()
    assert await _tick(db, bot, at(9, 30), allowed=[]) == 1
    assert [c[0] for c in bot.calls] == [555]


# ---- weekday: weekly reminders ----

OTHER_DAY = (DAY.weekday() + 1) % 7  # the weekday of TOMORROW


async def test_weekday_matching_is_sent(db):
    rid = await _rem(db, await _user(db), weekday=DAY.weekday())
    bot = FakeBot()
    assert DAY.weekday() == 1  # Tuesday: 0=Mon, not isoweekday
    assert await _tick(db, bot, at(9, 30)) == 1
    assert bot.calls == [(42, "Креатин", None)]
    assert await _last_sent(db, rid) == DAY


async def test_weekday_other_day_waits_for_its_day(db):
    rid = await _rem(db, await _user(db), weekday=OTHER_DAY)
    bot = FakeBot()
    assert await _tick(db, bot, at(9, 30)) == 0
    assert bot.calls == [] and await _last_sent(db, rid) is None
    assert await _tick(db, bot, at(9, 30, TOMORROW)) == 1
    assert await _last_sent(db, rid) == TOMORROW


async def test_weekday_fires_once_a_week(db):
    await _rem(db, await _user(db), weekday=DAY.weekday())
    bot = FakeBot()
    assert await _tick(db, bot, at(9, 30)) == 1
    assert await _tick(db, bot, at(9, 30, TOMORROW)) == 0
    for d in range(2, 7):
        assert await _tick(db, bot, at(9, 30, DAY + timedelta(days=d))) == 0
    assert await _tick(db, bot, at(9, 30, DAY + timedelta(days=7))) == 1
    assert len(bot.calls) == 2


async def test_weekday_spill_keeps_the_day_of_the_occurrence(db):
    uid = await _user(db)
    rid = await _rem(db, uid, minute=23 * 60 + 50, weekday=DAY.weekday(), last_sent_on=YESTERDAY)
    bot = FakeBot()
    assert await _tick(db, bot, at(0, 10, TOMORROW)) == 1  # 23:50 of DAY, sent at 00:10 of TOMORROW
    assert await _last_sent(db, rid) == DAY


async def test_weekday_spill_of_another_day_not_sent(db):
    # Due day is DAY (Tuesday); the reminder is for Wednesday, which is the day "now" falls on.
    rid = await _rem(db, await _user(db), minute=23 * 60 + 50, weekday=TOMORROW.weekday(), last_sent_on=YESTERDAY)
    bot = FakeBot()
    assert await _tick(db, bot, at(0, 10, TOMORROW)) == 0
    assert bot.calls == [] and await _last_sent(db, rid) == YESTERDAY


# ---- kind=advice ----


@pytest.fixture(autouse=True)
def _clear_advice_cooldown():
    # The cooldown is per process; reminder ids repeat across tests (fresh DB each time).
    rem_module._advice_retry_at.clear()
    yield
    rem_module._advice_retry_at.clear()


def _fake_advice(monkeypatch, result="СОВЕТ"):
    """Replace advice.generate; returns the list of recorded calls (user, now_utc)."""
    calls: list[tuple[User, datetime]] = []

    async def fake(session, user, settings, llm, tz, now_utc):
        calls.append((user, now_utc))
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(advice_module, "generate", fake)
    return calls


async def test_advice_is_generated_and_sent(db, monkeypatch):
    calls = _fake_advice(monkeypatch)
    uid = await _user(db)
    rid = await _rem(db, uid, kind="advice", text=None)
    bot = FakeBot()
    assert await _tick(db, bot, at(9, 30), llm=object()) == 1
    assert bot.calls == [(42, "СОВЕТ", None)]
    assert len(calls) == 1
    assert calls[0][0].id == uid and calls[0][1] == at(9, 30)
    assert await _last_sent(db, rid) == DAY


async def test_advice_without_llm_is_skipped(db, monkeypatch):
    calls = _fake_advice(monkeypatch)
    rid = await _rem(db, await _user(db), kind="advice", text=None)
    bot = FakeBot()
    assert await _tick(db, bot, at(9, 30), llm=None) == 0
    assert bot.calls == [] and calls == []
    assert await _last_sent(db, rid) is None


async def test_advice_llm_error_does_not_claim_and_retries(db, monkeypatch):
    _fake_advice(monkeypatch, LLMError("model is down"))
    rid = await _rem(db, await _user(db), kind="advice", text=None)
    bot = FakeBot()
    assert await _tick(db, bot, at(9, 30), llm=object()) == 0
    assert bot.calls == [] and await _last_sent(db, rid) is None
    calls = _fake_advice(monkeypatch)
    assert await _tick(db, bot, at(9, 40), llm=object()) == 1  # after the 10-minute cooldown
    assert bot.calls == [(42, "СОВЕТ", None)] and len(calls) == 1
    assert await _last_sent(db, rid) == DAY


async def test_advice_llm_error_cooldown_10_minutes(db, monkeypatch):
    calls = _fake_advice(monkeypatch, LLMError("model is down"))
    rid = await _rem(db, await _user(db), kind="advice", text=None)
    bot = FakeBot()
    assert await _tick(db, bot, at(9, 30), llm=object()) == 0
    assert len(calls) == 1
    # Checks every 30 s right after the failure do not call the model again.
    assert await _tick(db, bot, at(9, 30) + timedelta(seconds=30), llm=object()) == 0
    assert await _tick(db, bot, at(9, 39), llm=object()) == 0
    assert len(calls) == 1
    # 10 minutes later: one more attempt (fails again -> next one at 09:50).
    assert await _tick(db, bot, at(9, 40), llm=object()) == 0
    assert len(calls) == 2
    assert await _tick(db, bot, at(9, 45), llm=object()) == 0
    assert len(calls) == 2
    assert await _tick(db, bot, at(9, 50), llm=object()) == 0
    assert len(calls) == 3  # at most 3 attempts in the 30-minute window
    assert await _tick(db, bot, at(10, 0), llm=object()) == 0  # window closed
    assert len(calls) == 3 and bot.calls == [] and await _last_sent(db, rid) is None


async def test_advice_cooldown_does_not_block_other_reminders(db, monkeypatch):
    calls = _fake_advice(monkeypatch, LLMError("model is down"))
    uid = await _user(db)
    await _rem(db, uid, kind="advice", text=None)
    bot = FakeBot()
    assert await _tick(db, bot, at(9, 30), llm=object()) == 0
    await _rem(db, uid, minute=9 * 60 + 31)  # a text reminder due during the cooldown
    assert await _tick(db, bot, at(9, 31), llm=object()) == 1
    assert bot.calls == [(42, "Креатин", None)] and len(calls) == 1


async def test_advice_is_sent_after_quick_reminders(db, monkeypatch):
    _fake_advice(monkeypatch)
    uid = await _user(db)
    await _rem(db, uid, kind="advice", text=None)  # created first, lower id
    await _rem(db, uid, text="Креатин")
    bot = FakeBot()
    assert await _tick(db, bot, at(9, 30), llm=object()) == 2
    assert [c[1] for c in bot.calls] == ["Креатин", "СОВЕТ"]


async def test_claim_rechecks_weekday(db, monkeypatch):
    rid = await _rem(db, await _user(db), weekday=DAY.weekday())
    await _race_build(monkeypatch, db, rid, weekday=OTHER_DAY)
    bot = FakeBot()
    assert await _tick(db, bot, at(9, 30)) == 0
    assert bot.calls == [] and await _last_sent(db, rid) is None
