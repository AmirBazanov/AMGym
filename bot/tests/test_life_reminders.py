"""«Дела» reminders (gymbot.services.life_reminders, gymbot.handlers.life_reminders): a timed plan before it
starts with «✅ Сделано» / «+30 мин», tomorrow's deadlines at 20:00, the morning summary. The clock is pinned:
every check gets its moment, the buttons' `utcnow` is patched."""

import asyncio
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError
from aiogram.methods import SendMessage
from sqlalchemy import select

from gymbot.config import Settings
from gymbot.db.models import LifeDeadline, LifeEvent, Program, User, UserProgram
from gymbot.handlers import life_reminders as hlr
from gymbot.services import life, live
from gymbot.services import life_reminders as lr
from gymbot.services import reminders as rem

TZ = ZoneInfo("Asia/Tashkent")
DAY = date(2026, 10, 29)  # Thursday
OWNER = 42
URL = "https://example.test/app"


def at(h: int, m: int = 0, day: date = DAY, s: int = 0) -> datetime:
    """UTC moment of the given Tashkent wall-clock time."""
    return datetime(day.year, day.month, day.day, h, m, s, tzinfo=TZ).astimezone(UTC)


def make_settings(miniapp_url: str = URL, allowed: list[int] | None = None) -> Settings:
    # _env_file=None: a developer's real .env must not leak into tests.
    return Settings(
        _env_file=None, bot_token="1:x", miniapp_url=miniapp_url, timezone="Asia/Tashkent",
        allowed_user_ids=[OWNER] if allowed is None else allowed,
    )


class FakeBot:
    def __init__(self) -> None:
        self.calls: list[tuple[int, str, object, object]] = []

    async def send_message(self, chat_id, text, reply_markup=None, parse_mode=None, **kw):
        self.calls.append((chat_id, text, reply_markup, parse_mode))


class FailingBot(FakeBot):
    def __init__(self, exc: Exception) -> None:
        super().__init__()
        self.exc = exc

    async def send_message(self, chat_id, text, reply_markup=None, parse_mode=None, **kw):
        raise self.exc


@pytest.fixture(autouse=True)
def _quiet_live(monkeypatch):
    monkeypatch.setattr(live, "publish", lambda *a, **k: None)


async def _owner(db, **fields) -> int:
    async with db() as s:
        user = User(telegram_id=OWNER, rest_seconds=90, **fields)
        s.add(user)
        await s.commit()
        return user.id


async def _add(db, uid: int, entity: str, **fields) -> int:
    async with db() as s:
        row = await life.add(s, uid, entity, fields, at(7))
        await s.commit()
        return row.id


async def _event(db, eid: int) -> LifeEvent:
    async with db() as s:
        return await s.get(LifeEvent, eid)


async def _tick(db, bot, now: datetime, settings: Settings | None = None) -> int:
    return await lr.tick(bot.send_message if bot else None, db, settings or make_settings(), now)


def callback(data: str, user_id: int = OWNER):
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=user_id, full_name="Amir"),
        message=SimpleNamespace(edit_text=AsyncMock()),
        answer=AsyncMock(),
    )


def buttons(markup) -> list[str]:
    return [b.callback_data or b.text for row in markup.inline_keyboard for b in row]


# ---- plans ----


async def test_plan_reminded_once_at_start_minus_default(db):
    uid = await _owner(db)
    eid = await _add(db, uid, "event", day=DAY, start="11:00", end="12:00", title="Позаниматься HTTP-сервером на C")
    bot = FakeBot()
    assert await _tick(db, bot, at(10, 29, s=59)) == 0
    assert await _tick(db, bot, at(10, 30)) == 1
    assert await _tick(db, bot, at(10, 30, s=30)) == 0  # the second check of the minute
    assert await _tick(db, bot, at(10, 50)) == 0
    [(chat, text, kb, parse_mode)] = bot.calls
    assert chat == OWNER and parse_mode is None
    assert text == "Через 30 мин (11:00): Позаниматься HTTP-сервером на C"
    assert buttons(kb) == [f"lrdone:{eid}", f"lrsnooze:{eid}"]
    assert (await _event(db, eid)).reminded_at is not None


async def test_plan_created_inside_the_window_says_the_real_minutes(db):
    uid = await _owner(db)
    await _add(db, uid, "event", day=DAY, start="11:00", title="Созвон")
    bot = FakeBot()
    assert await _tick(db, bot, at(10, 45, s=20)) == 1
    assert bot.calls[0][1] == "Через 15 мин (11:00): Созвон"


async def test_plan_own_remind_before_and_user_default(db):
    uid = await _owner(db, life_remind_before=60)
    await _add(db, uid, "event", day=DAY, start="11:00", title="По умолчанию")
    await _add(db, uid, "event", day=DAY, start="11:00", title="Своё", remind_before=10)
    bot = FakeBot()
    assert await _tick(db, bot, at(10, 0)) == 1
    assert bot.calls[-1][1] == "Через 1 ч (11:00): По умолчанию"
    assert await _tick(db, bot, at(10, 49)) == 0
    assert await _tick(db, bot, at(10, 50)) == 1
    assert bot.calls[-1][1] == "Через 10 мин (11:00): Своё"


async def test_not_reminded_untimed_slots_done_deleted_past_other_day(db):
    uid = await _owner(db)
    await _add(db, uid, "event", day=DAY, title="Без времени")
    await _add(db, uid, "slot", weekday=DAY.isoweekday(), start="11:00", title="Урок", area="work")
    done = await _add(db, uid, "event", day=DAY, start="11:00", title="Сделано")
    gone = await _add(db, uid, "event", day=DAY, start="11:00", title="Удалено")
    await _add(db, uid, "event", day=DAY, start="10:30", title="Уже началось")
    await _add(db, uid, "event", day=DAY + timedelta(days=1), start="11:00", title="Завтра")
    async with db() as s:
        await life.set_done(s, uid, "event", done, True, at(8))
        await life.remove(s, uid, "event", gone, at(8))
        await s.commit()
    bot = FakeBot()
    assert await _tick(db, bot, at(10, 30)) == 0
    assert bot.calls == []


async def test_without_bot_or_owner_nothing(db):
    bot = FakeBot()
    assert await _tick(db, bot, at(10, 30)) == 0  # no owner yet
    uid = await _owner(db)
    eid = await _add(db, uid, "event", day=DAY, start="11:00", title="План")
    assert await _tick(db, None, at(10, 30)) == 0  # RUN_BOT=false
    assert (await _event(db, eid)).reminded_at is None
    # Someone else (not in ALLOWED_USER_IDS) is not the owner.
    assert await _tick(db, bot, at(10, 30), make_settings(allowed=[7])) == 0
    assert bot.calls == []


async def test_transient_send_error_releases_permanent_keeps(db):
    uid = await _owner(db)
    eid = await _add(db, uid, "event", day=DAY, start="11:00", title="План")
    net = TelegramNetworkError(method=SendMessage(chat_id=1, text="x"), message="timeout")
    assert await _tick(db, FailingBot(net), at(10, 30)) == 0
    assert (await _event(db, eid)).reminded_at is None  # retried by the next check
    bot = FakeBot()
    assert await _tick(db, bot, at(10, 31)) == 1
    eid2 = await _add(db, uid, "event", day=DAY, start="12:00", title="Второй")
    blocked = TelegramForbiddenError(method=SendMessage(chat_id=1, text="x"), message="Forbidden: bot was blocked")
    assert await _tick(db, FailingBot(blocked), at(11, 30)) == 0
    assert (await _event(db, eid2)).reminded_at is not None  # not retried


async def test_concurrent_checks_send_once(db):
    uid = await _owner(db)
    await _add(db, uid, "event", day=DAY, start="11:00", title="План")
    bot = FakeBot()
    sent = await asyncio.gather(*(_tick(db, bot, at(10, 30)) for _ in range(3)))
    assert sum(sent) == 1 and len(bot.calls) == 1


# ---- buttons ----


async def test_snooze_moves_and_reminds_again(db, monkeypatch):
    uid = await _owner(db)
    eid = await _add(db, uid, "event", day=DAY, start="11:00", end="12:00", title="HTTP-сервер")
    bot = FakeBot()
    assert await _tick(db, bot, at(10, 30)) == 1
    monkeypatch.setattr(hlr, "utcnow", lambda: at(10, 31))
    cb = callback(f"lrsnooze:{eid}")
    await hlr.on_snooze(cb, make_settings(), db)
    cb.answer.assert_awaited_once_with("Перенёс на 11:30")
    cb.message.edit_text.assert_awaited_once_with("Перенёс на 11:30: HTTP-сервер")
    e = await _event(db, eid)
    assert (e.day, e.start, e.end, e.reminded_at) == (DAY, "11:30", "12:30", None)
    again = callback(f"lrsnooze:{eid}")  # a second tap does not move it twice
    await hlr.on_snooze(again, make_settings(), db)
    again.answer.assert_awaited_once_with(hlr.STALE)
    assert (await _event(db, eid)).start == "11:30"
    assert await _tick(db, bot, at(10, 59)) == 0
    assert await _tick(db, bot, at(11, 0)) == 1
    assert bot.calls[-1][1] == "Через 30 мин (11:30): HTTP-сервер"
    assert await _tick(db, bot, at(11, 0, s=30)) == 0


async def test_snooze_past_midnight(db, monkeypatch):
    uid = await _owner(db)
    late = await _add(db, uid, "event", day=DAY, start="23:45", end="23:55", title="Поздно")
    wraps = await _add(db, uid, "event", day=DAY, start="23:00", end="23:50", title="Конец за полночь")
    monkeypatch.setattr(hlr, "utcnow", lambda: at(22, 40))
    async with db() as s:  # both reminded already
        for eid in (late, wraps):
            (await s.get(LifeEvent, eid)).reminded_at = at(22, 30)
        await s.commit()
    cb = callback(f"lrsnooze:{late}")
    await hlr.on_snooze(cb, make_settings(), db)
    e = await _event(db, late)
    assert (e.day, e.start, e.end) == (DAY + timedelta(days=1), "00:15", "00:25")
    cb.answer.assert_awaited_once_with("Перенёс на 30.10 00:15")
    await hlr.on_snooze(callback(f"lrsnooze:{wraps}"), make_settings(), db)
    e = await _event(db, wraps)
    assert (e.day, e.start, e.end) == (DAY, "23:30", None)


async def test_done_button(db, monkeypatch):
    uid = await _owner(db)
    eid = await _add(db, uid, "event", day=DAY, start="11:00", title="План")
    assert await _tick(db, FakeBot(), at(10, 30)) == 1
    monkeypatch.setattr(hlr, "utcnow", lambda: at(10, 40))
    cb = callback(f"lrdone:{eid}")
    await hlr.on_done(cb, make_settings(), db)
    cb.message.edit_text.assert_awaited_once_with("✅ Сделано: План")
    assert life.aware((await _event(db, eid)).done_at) == at(10, 40)
    again = callback(f"lrdone:{eid}")
    await hlr.on_done(again, make_settings(), db)
    again.answer.assert_awaited_once_with(hlr.STALE)
    snooze = callback(f"lrsnooze:{eid}")  # done: nothing to move
    await hlr.on_snooze(snooze, make_settings(), db)
    snooze.answer.assert_awaited_once_with(hlr.STALE)


async def test_buttons_only_for_the_owner_and_live_plans(db, monkeypatch):
    uid = await _owner(db)
    eid = await _add(db, uid, "event", day=DAY, start="11:00", title="План")
    assert await _tick(db, FakeBot(), at(10, 30)) == 1
    monkeypatch.setattr(hlr, "utcnow", lambda: at(10, 40))
    for data, user in ((f"lrdone:{eid}", 7), (f"lrsnooze:{eid}", 7), ("lrdone:999", OWNER), ("lrsnooze:x", OWNER)):
        cb = callback(data, user)
        await (hlr.on_done if data.startswith("lrdone") else hlr.on_snooze)(cb, make_settings(), db)
        cb.answer.assert_awaited_once_with(hlr.STALE)
    e = await _event(db, eid)
    assert e.done_at is None and e.start == "11:00"


# ---- deadlines ----


async def test_deadline_eve_once_at_20(db):
    uid = await _owner(db)
    tomorrow = DAY + timedelta(days=1)
    d1 = await _add(db, uid, "deadline", day=tomorrow, title="Лаба №1", course="Сети")
    await _add(db, uid, "deadline", day=DAY + timedelta(days=2), title="Позже")
    bot = FakeBot()
    assert await _tick(db, bot, at(19, 59)) == 0
    assert await _tick(db, bot, at(20, 0)) == 1
    assert await _tick(db, bot, at(20, 0, s=30)) == 0
    assert await _tick(db, bot, at(21, 0)) == 0
    [(_, text, kb, _)] = bot.calls
    assert text == "Завтра дедлайн: Лаба №1 (Сети)"
    assert kb.inline_keyboard[0][0].web_app.url == URL + "?section=life"
    async with db() as s:
        assert (await s.get(LifeDeadline, d1)).reminded_on == DAY


async def test_deadlines_eve_in_one_message_and_moved_again(db):
    uid = await _owner(db)
    tomorrow = DAY + timedelta(days=1)
    d1 = await _add(db, uid, "deadline", day=tomorrow, title="Лаба")
    await _add(db, uid, "deadline", day=tomorrow, title="Эссе")
    bot = FakeBot()
    assert await _tick(db, bot, at(20, 5)) == 1
    assert bot.calls[0][1] == "Завтра дедлайны:\n• Лаба\n• Эссе"
    async with db() as s:  # moved a day later: reminded again on its new eve
        await life.update(s, uid, "deadline", d1, {"day": tomorrow + timedelta(days=1)}, at(20, 10))
        await s.commit()
    assert await _tick(db, bot, at(20, 0, day=tomorrow)) == 1
    assert bot.calls[-1][1] == "Завтра дедлайн: Лаба"


# ---- the loop ----


async def test_loop_survives_a_failing_life_check(db, monkeypatch):
    calls: list[str] = []

    async def failing(*a, **k):
        calls.append("life")
        raise RuntimeError("boom")

    async def no_reminders(*a, **k):
        calls.append("reminders")
        return 0

    async def stop(_):
        if calls.count("life") >= 2:
            raise asyncio.CancelledError
    monkeypatch.setattr(lr, "tick", failing)
    monkeypatch.setattr(rem, "tick", no_reminders)
    monkeypatch.setattr(rem.asyncio, "sleep", stop)
    with pytest.raises(asyncio.CancelledError):
        await rem.reminder_loop(FakeBot(), db, make_settings())  # type: ignore[arg-type]
    assert calls == ["reminders", "life", "reminders", "life"]


def test_life_url():
    assert lr.life_url(make_settings(URL)) == URL + "?section=life"
    assert lr.life_url(make_settings(URL + "?x=1")) == URL + "?x=1&section=life"
    assert lr.life_url(make_settings("")) is None


async def test_reminded_at_stays_after_restart_of_the_window(db):
    """Two processes or a restart inside the window: the mark is in the database, not in memory."""
    uid = await _owner(db)
    eid = await _add(db, uid, "event", day=DAY, start="11:00", title="План")
    assert await _tick(db, FakeBot(), at(10, 30)) == 1
    async with db() as s:
        stamp = await s.scalar(select(LifeEvent.reminded_at).where(LifeEvent.id == eid))
    assert life.aware(stamp) == at(10, 30)
    assert await _tick(db, FakeBot(), at(10, 40)) == 0


# ---- the morning summary ----

MON = date(2026, 11, 2)  # week 2 of the program below, «Руки и плечи»


async def _on_program(db, uid: int, start: date = date(2026, 10, 26)) -> None:
    async with db() as s:
        program = await s.scalar(select(Program).where(Program.slug == "arms_specialization_8w"))
        s.add(UserProgram(user_id=uid, program_id=program.id, started_on=start))
        await s.commit()


async def _sent_on(db, uid: int) -> date | None:
    async with db() as s:
        return await s.scalar(select(User.life_summary_sent_on).where(User.id == uid))


async def test_summary_once_a_day_with_everything(db):
    uid = await _owner(db)
    await _on_program(db, uid)
    await _add(db, uid, "slot", weekday=1, start="10:00", end="11:30", title="ИЗ Математика", area="work",
               place="Восход")
    await _add(db, uid, "slot", weekday=2, start="17:00", title="Не сегодня", area="work")
    await _add(db, uid, "event", day=MON, start="14:00", title="HTTP-сервер <на C>", area="study")
    await _add(db, uid, "event", day=MON, start="09:00", title="Созвон", area="work")
    await _add(db, uid, "event", day=MON, title="Купить билеты")
    await _add(db, uid, "deadline", day=MON, title="Лаба №1", course="Сети")
    await _add(db, uid, "deadline", day=MON + timedelta(days=3), title="Эссе")
    await _add(db, uid, "deadline", day=MON + timedelta(days=4), title="Далеко")
    await _add(db, uid, "deadline", day=MON - timedelta(days=2), title="Забыл")
    await _add(db, uid, "chore", title="Вынести мусор", every_days=2, last_done=MON - timedelta(days=4))
    await _add(db, uid, "chore", title="Уборка", every_days=7, last_done=MON - timedelta(days=1))
    for i in range(1, 7):
        await _add(db, uid, "task", title=f"Задача {i}")
    await _add(db, uid, "project", name="HTTP-сервер на C", kind="learning", next_step="epoll")
    await _add(db, uid, "project", name="Пауза", kind="learning", status="paused", next_step="не сейчас")
    await _add(db, uid, "project", name="AMGym", kind="work", next_step="ревью")
    bot = FakeBot()
    assert await _tick(db, bot, at(7, 59, day=MON)) == 0
    assert await _tick(db, bot, at(8, 0, day=MON)) == 1
    assert await _tick(db, bot, at(8, 0, s=30, day=MON)) == 0
    assert await _tick(db, bot, at(8, 20, day=MON)) == 0
    assert await _sent_on(db, uid) == MON
    [(chat, text, kb, parse_mode)] = bot.calls
    assert chat == OWNER and parse_mode == "HTML"
    expected = [
        "<b>Сводка на понедельник, 02.11</b>",
        "",
        "<b>09:00</b> Созвон",
        "<b>10:00–11:30</b> ИЗ Математика · Восход",
        "<b>14:00</b> HTTP-сервер &lt;на C&gt;",
        "• Купить билеты",
        "",
        "🏋️ Тренировка: Руки и плечи",
        "",
        "<b>Дедлайны</b>",
        "• просрочен с 31.10 — Забыл",
        "• сегодня — Лаба №1 (Сети)",
        "• 05.11, чт — Эссе",
        "",
        "<b>Дом</b>",
        "• Вынести мусор — просрочено на 2 дн.",
        "",
        "<b>Задачи</b>",
        "• Задача 1",
        "• Задача 2",
        "• Задача 3",
        "• Задача 4",
        "• Задача 5",
        "и ещё 1",
        "",
        "<b>Учёба</b>",
        "• HTTP-сервер на C: epoll",
    ]
    assert text == "\n".join(expected)
    assert kb.inline_keyboard[0][0].text == "Открыть дела"
    assert kb.inline_keyboard[0][0].web_app.url == URL + "?section=life"
    assert await _tick(db, bot, at(8, 0, day=MON + timedelta(days=1))) == 1  # the next day again


async def test_summary_empty_day_and_rest_day(db):
    uid = await _owner(db)
    await _on_program(db, uid)
    await _add(db, uid, "slot", weekday=1, start="10:00", title="Не в четверг", area="work")
    bot = FakeBot()
    assert await _tick(db, bot, at(8, 0)) == 1
    assert bot.calls[0][1] == "<b>Сводка на четверг, 29.10</b>\n\nСегодня свободно\n\n🏋️ Тренировка: день отдыха"


async def test_summary_without_program_and_without_url(db):
    await _owner(db)
    bot = FakeBot()
    assert await _tick(db, bot, at(8, 0), make_settings(miniapp_url="")) == 1
    [(_, text, kb, _)] = bot.calls
    assert text == "<b>Сводка на четверг, 29.10</b>\n\nСегодня свободно"
    assert kb is None


async def test_summary_disabled_and_own_time(db):
    uid = await _owner(db, life_summary_enabled=False)
    bot = FakeBot()
    assert await _tick(db, bot, at(8, 0)) == 0
    assert await _sent_on(db, uid) is None
    async with db() as s:
        user = await s.get(User, uid)
        life.set_settings(user, {"summary_enabled": True, "summary_time": "07:00"})
        await s.commit()
    assert await _tick(db, bot, at(6, 59)) == 0
    assert await _tick(db, bot, at(7, 0)) == 1
    assert await _tick(db, bot, at(8, 0)) == 0  # once a day


async def test_summary_falls_back_to_plain_text(db):
    """Telegram rejecting the markup must not lose the summary (send_html)."""
    await _owner(db)

    class PickyBot(FakeBot):
        async def send_message(self, chat_id, text, reply_markup=None, parse_mode=None, **kw):
            if parse_mode == "HTML":
                raise TelegramBadRequest(method=SendMessage(chat_id=1, text="x"), message="can't parse entities")
            await super().send_message(chat_id, text, reply_markup, parse_mode)

    bot = PickyBot()
    assert await _tick(db, bot, at(8, 0)) == 1
    assert bot.calls[0][1] == "Сводка на четверг, 29.10\n\nСегодня свободно"
