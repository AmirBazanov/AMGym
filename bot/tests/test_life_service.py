"""«Дела»: the life_* tables and migration 0017, the snapshot and the day agenda, the import of the old dashboard
(data.js + hub_state), the model's actions and life.apply (gymbot.services.life, gymbot.services.life_actions)."""

from datetime import UTC, date, datetime, timedelta

import pytest
from alembic import command
from conftest import make_settings
from sqlalchemy import inspect, select, text

from gymbot.db import migrate
from gymbot.db.models import (
    LifeChore,
    LifeDeadline,
    LifeEvent,
    LifeNote,
    LifeProject,
    LifeSlot,
    LifeTask,
    User,
)
from gymbot.db.session import make_engine
from gymbot.services import hub, life, live
from gymbot.services.users import get_or_create_user

TODAY = date(2026, 10, 29)  # Thursday
NOW = datetime(2026, 10, 29, 7, 30, tzinfo=UTC)

# The dashboard's real data.js (AventraLLC/amirs-hub, 2026-10-29): its classes and deadlines are all examples.
REAL_DATA_JS = """// Все данные сайта. Редактируй этот файл и пушь в GitHub.
// Время уроков — по Ташкенту (UTC+5). day: 1=Пн … 7=Вс.
window.DATA = {
  owner: "Амир",

  lessons: [
    { day: 1, time: "10:00", title: "ИЗ Математика", tag: "math", note: "Восход · куратор", vld: "15:00" },
    { day: 2, time: "17:00", title: "Фронтенд вторник (БЛГ)", tag: "web", note: "Онлайн", vld: "22:00" },
    { day: 3, time: "13:30", title: "ИЗ онлайн Юнити", tag: "unity", note: "Восход", vld: "18:30" },
    { day: 3, time: "15:00", title: "PS онлайн ср", tag: "py", note: "Python Start", vld: "20:00" },
    { day: 4, time: "12:00", title: "Питон Старт 2", tag: "py", note: "СК Океан, офлайн", vld: "17:00" },
    { day: 4, time: "14:00", title: "ВП онлайн четверг", tag: "vp", note: "Визуальное программирование", vld: "19:00" },
    { day: 4, time: "15:00", title: "ИЗ Математика", tag: "math", note: "Восход · куратор", vld: "20:00" },
    { day: 5, time: "12:00", end: "13:30", title: "Геймдизайн онлайн", tag: "game", note: "Калинина", vld: "17:00" },
    { day: 5, time: "14:15", title: "ИЗ КГ", tag: "kg", note: "Восход · куратор", vld: "19:15" },
    { day: 6, time: "09:00", title: "Свб Геймдизайн онлайн", tag: "game", note: "СК Океан", vld: "14:00" },
    { day: 6, time: "10:00", title: "Юнити ИЗ онлайн (БЛГ)", tag: "unity", note: "Unity", vld: "15:00" },
    { day: 6, time: "14:00", title: "Юнити (Восход)", tag: "unity", note: "Unity", vld: "19:00" }
  ],

  // Пары в университете. Пример — замени на своё расписание.
  study: {
    classes: [
      { day: 1, time: "08:30", title: "Пример: Математический анализ", place: "Ауд. 101", example: true },
      { day: 3, time: "10:00", title: "Пример: Реверс-инжиниринг (веду практику)", place: "Ауд. 204", example: true }
    ],
    deadlines: [
      { date: "2026-11-10", title: "Пример: лабораторная №1", course: "Satbayev, Software Engineering", example: true }
    ]
  },

  projects: [
    { name: "Менторика", status: "в работе", desc: "ИИ-репетитор по математике. Участник программы Sber500 × DISRUPT. Пишу фактически один.", link: "https://mentorika.fun", next: "" },
    { name: "AMGym", status: "в работе", desc: "ИИ-дневник тренировок и питания: Telegram-бот + мини-приложение.", link: "https://github.com/AmirBazanov/AMGym", next: "" },
    { name: "HTTP-сервер на C", status: "учёба", desc: "Пишу HTTP-сервер на чистом C, чтобы прокачать low-level. Роадмап в Obsidian.", link: "", next: "" },
    { name: "Трекер идей для команды", status: "идея", desc: "Идеи и фиксы, привязанные к коду, статус git и прода в одном месте. Для команды с Александром и Русланом.", link: "", next: "" }
  ],

  // Задачи по умолчанию. Новые можно добавлять прямо на сайте (хранятся в браузере).
  todos: [
    { id: "t1", text: "Обновить резюме (Backend & AI)", tag: "работа" },
    { id: "t2", text: "Написать обратную связь родителям", tag: "алгоритмика" }
  ],

  // Повторяющиеся дела по дому: every — раз в сколько дней.
  chores: [
    { id: "c1", text: "Уборка", every: 7 },
    { id: "c2", text: "Стирка", every: 4 },
    { id: "c3", text: "Продукты", every: 3 },
    { id: "c4", text: "Вынести мусор", every: 2 },
    { id: "c5", text: "Оплатить коммуналку", every: 30 }
  ]
};
"""
# Plus one real class and one real deadline, to see them imported next to the examples that are not.
DATA_JS = REAL_DATA_JS.replace(
    "classes: [\n",
    'classes: [\n      { day: 2, time: "9:40", end: "11:10", title: "Компьютерные сети", place: "Ауд. 305" },\n',
).replace(
    "deadlines: [\n",
    'deadlines: [\n      { date: "2026-11-20", title: "Курсовая", course: "Satbayev, Software Engineering" },\n',
)
HUB_STATE = {
    "done": {"t1": True, "x9": True, "x10": False},
    "removed": ["t2"],
    "custom": [{"id": "x9", "text": "Купить билеты"}, {"id": "x10", "text": "Позвонить   маме "}],
    "chores": {"c1": "2026-10-25", "c4": "вчера"},
}

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


# ---- helpers ----


async def owner_id(db, telegram_id: int = 42) -> int:
    async with db() as s:
        user = await get_or_create_user(s, telegram_id, "Amir")
        await s.commit()
        return user.id


async def put(db, *rows):
    """Insert ORM rows (user_id set by the caller); returns their ids."""
    async with db() as s:
        s.add_all(rows)
        await s.commit()
        return [r.id for r in rows]


async def view(db, uid: int, today: date = TODAY, **kw) -> life.LifeView:
    async with db() as s:
        return await life.snapshot(s, uid, today, **kw)


# ---- snapshot and the day agenda ----


async def test_snapshot_hides_deleted_old_and_done(db):
    uid = await owner_id(db)
    other = await owner_id(db, 7)
    gone = NOW - timedelta(hours=1)
    await put(
        db,
        LifeSlot(user_id=uid, weekday=4, start="15:00", title="ИЗ Математика", area="work"),
        LifeSlot(user_id=uid, weekday=1, start="10:00", title="ИЗ Математика", area="work"),
        LifeSlot(user_id=uid, weekday=2, start="09:00", title="Удалённый", area="work", deleted_at=gone),
        LifeSlot(user_id=other, weekday=2, start="09:00", title="Чужой", area="work"),
        LifeEvent(user_id=uid, day=TODAY - timedelta(days=2), title="Позавчера", area="other"),
        LifeEvent(user_id=uid, day=TODAY - timedelta(days=1), title="Вчера", area="other"),
        LifeEvent(user_id=uid, day=TODAY + timedelta(days=14), title="Через две недели", area="other"),
        LifeEvent(user_id=uid, day=TODAY + timedelta(days=15), title="Дальше", area="other"),
        LifeDeadline(user_id=uid, day=TODAY, title="Сдан", done_at=gone),
        LifeDeadline(user_id=uid, day=TODAY + timedelta(days=3), title="Курсовая"),
        LifeTask(user_id=uid, title="Вторая", position=2),
        LifeTask(user_id=uid, title="Первая", position=1),
        LifeTask(user_id=uid, title="Закрыта вчера", position=0, done_at=NOW - timedelta(days=1)),
        LifeTask(user_id=uid, title="Закрыта давно", position=0, done_at=NOW - timedelta(days=9)),
        LifeProject(user_id=uid, name="Готов", kind="own", status="done"),
        LifeProject(user_id=uid, name="HTTP-сервер", kind="learning", status="active"),
        LifeProject(user_id=uid, name="AMGym", kind="work", status="active"),
    )
    v = await view(db, uid)
    assert [(s.weekday, s.start) for s in v.slots] == [(1, "10:00"), (4, "15:00")]
    assert [e.title for e in v.events] == ["Вчера", "Через две недели"]
    assert [d.title for d in v.deadlines] == ["Курсовая"]
    assert [t.title for t in v.tasks] == ["Первая", "Вторая", "Закрыта вчера"]
    assert [p.name for p in v.projects] == ["AMGym", "HTTP-сервер"]
    wide = await view(db, uid, events_from=TODAY - timedelta(days=2), events_to=TODAY + timedelta(days=15))
    assert len(wide.events) == 4


async def test_notes_newest_30(db):
    uid = await owner_id(db)
    await put(db, *(
        LifeNote(user_id=uid, title=f"n{i}", updated_at=NOW - timedelta(minutes=i)) for i in range(32)
    ))
    titles = [n.title for n in (await view(db, uid)).notes]
    assert len(titles) == 30 and titles[0] == "n0" and titles[-1] == "n29"


def test_chore_left():
    chore = LifeChore(title="Уборка", every_days=7, last_done=date(2026, 10, 25))
    assert life.chore_left(chore, TODAY) == 3
    assert life.chore_left(chore, date(2026, 11, 1)) == 0
    assert life.chore_left(chore, date(2026, 11, 3)) == -2
    assert life.chore_left(LifeChore(title="x", every_days=2, last_done=None), TODAY) == 0


async def test_day_agenda(db):
    uid = await owner_id(db)
    gone = NOW - timedelta(hours=1)
    await put(
        db,
        LifeSlot(user_id=uid, weekday=4, start="15:00", title="ИЗ Математика", area="work", place="Восход"),
        LifeSlot(user_id=uid, weekday=4, start="12:00", end="13:30", title="Питон Старт 2", area="work"),
        LifeSlot(user_id=uid, weekday=5, start="12:00", title="Геймдизайн", area="work"),
        LifeSlot(user_id=uid, weekday=4, start="08:00", title="Удалённый", area="work", deleted_at=gone),
        LifeEvent(user_id=uid, day=TODAY, start="11:00", title="HTTP-сервер", area="study", done_at=gone),
        LifeEvent(user_id=uid, day=TODAY, title="Без времени", area="other"),
        LifeEvent(user_id=uid, day=TODAY + timedelta(days=1), start="09:00", title="Завтра", area="work"),
        LifeDeadline(user_id=uid, day=TODAY, title="Лабораторная"),
        LifeChore(user_id=uid, title="Уборка", every_days=7, last_done=TODAY - timedelta(days=7)),
        LifeChore(user_id=uid, title="Мусор", every_days=2, last_done=TODAY - timedelta(days=4)),
        LifeChore(user_id=uid, title="Стирка", every_days=4, last_done=TODAY - timedelta(days=1)),
        LifeChore(user_id=uid, title="Коммуналка", every_days=30),
    )
    v = await view(db, uid)
    items = life.day_agenda(v, TODAY)
    assert [(i.kind, i.title, i.start) for i in items] == [
        ("event", "HTTP-сервер", "11:00"),
        ("slot", "Питон Старт 2", "12:00"),
        ("slot", "ИЗ Математика", "15:00"),
        ("event", "Без времени", None),
        ("deadline", "Лабораторная", None),
        ("chore", "Уборка", None),
        ("chore", "Мусор", None),
        ("chore", "Коммуналка", None),
    ]
    assert items[0].done is True and items[0].area == "study"
    assert items[1].end == "13:30" and items[2].place == "Восход"
    assert {i.title: i.overdue_days for i in items if i.kind == "chore"} == {"Уборка": 0, "Мусор": 2, "Коммуналка": 0}

    tomorrow = life.day_agenda(v, TODAY + timedelta(days=1))
    assert [(i.kind, i.title) for i in tomorrow] == [
        ("event", "Завтра"), ("slot", "Геймдизайн"),
        ("chore", "Уборка"), ("chore", "Мусор"), ("chore", "Коммуналка"),
    ]
    assert {i.title: i.overdue_days for i in tomorrow if i.kind == "chore"} == {"Уборка": 1, "Мусор": 3, "Коммуналка": 0}
    assert [i.kind for i in life.day_agenda(v, TODAY - timedelta(days=1))] == []  # no chores on a past day


async def test_prompt_text_has_ids(db):
    uid = await owner_id(db)
    sid, eid, cid = await put(
        db,
        LifeSlot(user_id=uid, weekday=1, start="10:00", title="ИЗ Математика", area="work", tag="math"),
        LifeEvent(user_id=uid, day=TODAY, start="11:00", title="HTTP-сервер", area="study"),
        LifeChore(user_id=uid, title="Мусор", every_days=2, last_done=TODAY - timedelta(days=4)),
    )
    out = life.prompt_text(await view(db, uid))
    assert f"#{sid} пн 10:00 ИЗ Математика [work, math]" in out
    assert f"#{eid} 2026-10-29 чт (сегодня) 11:00 HTTP-сервер [study]" in out
    assert f"#{cid} Мусор, раз в 2 дн., последний раз 2026-10-25, просрочено на 2 дн." in out
    assert "Заметки (note):\n- (нет)" in out


# ---- import of the old dashboard ----


def hub_settings(tmp_path, data_js: str | None = DATA_JS):
    site = tmp_path / "my-hub"
    site.mkdir(exist_ok=True)
    if data_js is not None:
        (site / "data.js").write_text(data_js, encoding="utf-8")
    return make_settings(tmp_path, hub_dir=site)


async def run_import(db, settings, uid: int) -> int:
    async with db() as s:
        user = await s.get(User, uid)
        return await life.import_legacy(s, settings, user, NOW)


async def test_import_legacy(tmp_path, db, monkeypatch):
    published: list[tuple] = []
    monkeypatch.setattr(live, "publish", lambda *a: published.append(a))
    uid = await owner_id(db)
    async with db() as s:
        await hub.save_state(s, uid, 0, HUB_STATE, NOW)
    settings = hub_settings(tmp_path)

    assert await run_import(db, settings, uid) == 26
    assert published == [(uid, "life")]
    v = await view(db, uid)
    work = [s for s in v.slots if s.area == "work"]
    assert len(work) == 12
    first = work[0]
    assert (first.weekday, first.start, first.end, first.title, first.tag, first.place, first.vld) == (
        1, "10:00", None, "ИЗ Математика", "math", "Восход · куратор", "15:00"
    )
    assert first.source == "import" and first.external_id == "lesson:1:10:00:ИЗ Математика"
    assert next(s for s in work if s.title == "Геймдизайн онлайн").end == "13:30"
    study = [(s.weekday, s.start, s.end, s.title, s.place, s.tag) for s in v.slots if s.area == "study"]
    assert study == [(2, "09:40", "11:10", "Компьютерные сети", "Ауд. 305", "study")]  # examples stay out
    assert [(d.day, d.title, d.course) for d in v.deadlines] == [
        (date(2026, 11, 20), "Курсовая", "Satbayev, Software Engineering")
    ]
    assert [(t.title, t.tag, t.done_at is not None, t.external_id) for t in v.tasks] == [
        ("Позвонить маме", None, False, "custom:x10"),
        ("Обновить резюме (Backend & AI)", "работа", True, "todo:t1"),
        ("Купить билеты", None, True, "custom:x9"),
    ]  # t2 was removed on the site
    assert [(c.title, c.every_days, c.last_done) for c in v.chores] == [
        ("Уборка", 7, date(2026, 10, 25)), ("Стирка", 4, None), ("Продукты", 3, None),
        ("Вынести мусор", 2, None), ("Оплатить коммуналку", 30, None),
    ]
    async with db() as s:
        projects = list(await s.scalars(select(LifeProject).order_by(LifeProject.id)))
        user = await s.get(User, uid)
    assert [(p.name, p.kind, p.status) for p in projects] == [
        ("Менторика", "work", "active"), ("AMGym", "work", "active"),
        ("HTTP-сервер на C", "learning", "active"), ("Трекер идей для команды", "own", "idea"),
    ]
    assert projects[0].link == "https://mentorika.fun" and projects[0].description.startswith("ИИ-репетитор")
    assert projects[2].link is None and projects[2].next_step is None
    assert life.aware(user.life_imported_at) == NOW

    # Once only; and even with the mark gone, rows imported before are not made twice.
    assert await run_import(db, settings, uid) == 0
    async with db() as s:
        (await s.get(User, uid)).life_imported_at = None
        await s.commit()
    assert await run_import(db, settings, uid) == 0
    v2 = await view(db, uid)
    assert (len(v2.slots), len(v2.tasks), len(v2.chores)) == (13, 3, 5)


async def test_import_real_file_without_state(tmp_path, db):
    uid = await owner_id(db)
    assert await run_import(db, hub_settings(tmp_path, REAL_DATA_JS), uid) == 12 + 4 + 2 + 5
    v = await view(db, uid)
    assert v.deadlines == [] and all(s.area == "work" for s in v.slots)
    assert [t.done_at for t in v.tasks] == [None, None]


async def test_import_without_data_js_retries_later(tmp_path, db):
    uid = await owner_id(db)
    assert await run_import(db, hub_settings(tmp_path, None), uid) == 0
    assert await run_import(db, make_settings(tmp_path, hub_dir=""), uid) == 0
    assert await run_import(db, hub_settings(tmp_path, "window.DATA = { lessons: [ {day: 1"), uid) == 0  # broken
    async with db() as s:
        assert (await s.get(User, uid)).life_imported_at is None
    assert await run_import(db, hub_settings(tmp_path), uid) == 25  # no hub_state: t1 and t2 open, no own tasks


async def test_import_on_start(tmp_path, db, settings):
    s = hub_settings(tmp_path)
    assert await life.import_on_start(db, s, NOW) == 0  # nobody reached the bot yet
    await owner_id(db)
    assert await life.import_on_start(db, s, NOW) == 25
    assert await life.import_on_start(db, s, NOW) == 0
