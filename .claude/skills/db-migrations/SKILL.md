---
name: db-migrations
description: Работа со схемой БД GymAPP — SQLAlchemy-модели, Alembic-миграции, переезд с SQLite на Postgres. Используй при любом изменении bot/src/gymbot/db/models.py.
---

# Схема БД и миграции

Модели: `bot/src/gymbot/db/models.py`. Группы таблиц: пользователь (`users`), справочник (`exercises`), готовые программы (`programs → program_weeks → program_days → program_items`, `user_programs`), журнал (`workouts → workout_sets`), питание (`food_entries`).

## Правила
- Любое изменение модели = новая миграция. Не правь применённые миграции.
- Только переносимые типы: `Integer`, `BigInteger` (telegram_id), `Numeric` для веса и КБЖУ, `JSON`, `DateTime(timezone=True)`. Никаких `sqlite_*` опций.
- Для SQLite в Alembic включён `render_as_batch=True` (ALTER TABLE в SQLite ограничен).
- Удаление колонки с данными — в два шага (перестать писать, потом удалить) и только с согласия Амира.

## Первичная настройка (если `bot/migrations/env.py` ещё нет)
```bash
cd bot && alembic init -t async migrations
```
В `migrations/env.py`: `from gymbot.db.models import Base; target_metadata = Base.metadata`, URL брать из `gymbot.config.get_settings().database_url`, в `context.configure(...)` добавить `render_as_batch=True`.

## Новая миграция
```bash
cd bot
alembic revision --autogenerate -m "add body weight"
# прочитай сгенерированный файл: autogenerate пропускает переименования и типы JSON
alembic upgrade head
alembic downgrade -1 && alembic upgrade head   # проверка, что откат работает
```

## Переезд на Postgres
`pip install asyncpg`, `DATABASE_URL=postgresql+asyncpg://...`, `alembic upgrade head` на пустой базе, перенос данных скриптом через SQLAlchemy (читать из SQLite, писать в Postgres по таблицам в порядке FK).
