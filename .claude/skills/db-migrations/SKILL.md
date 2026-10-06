---
name: db-migrations
description: Работа со схемой БД GymAPP — SQLAlchemy-модели, Alembic-миграции, переезд с SQLite на Postgres. Используй при любом изменении bot/src/gymbot/db/models.py.
---

# Схема БД и миграции

Модели: `bot/src/gymbot/db/models.py`. Группы таблиц:
- **Пользователь** (`users`): основные данные, профиль для советов (вес, рост, год рождения, цель, «о себе»), норма КБЖУ, таймер отдыха.
- **Справочник** (`exercises`): упражнения с алиасами.
- **Программы** (`programs → program_weeks → program_days → program_items`, `user_programs`): готовые программы.
- **Журнал тренировок** (`workouts → workout_sets`): записи из чата и мини-апп.
- **Питание** (`food_entries`): еда с КБЖУ, оценённая нейросетью или с этикетки.
- **Самочувствие** (`wellbeing_entries`): сон, боли, энергия, настроение, заметки.
- **Факты** (`user_facts`): активные факты пользователя (до 50 шт, по 200 символов), категория, источник.
- **Напоминания** (`reminders`): ежедневные или на конкретный день недели, типы: text, nutrition, advice, checkin.
- **Адаптивный план дня** (`day_plans`): сегодняшняя программа, адаптированная к самочувствию, питанию и восстановлению; уникально по `(user_id, plan_date)`.

## Миграции (0001–0007)
- **0001** (2026-10-06): упражнения, программы, тренировки, пользователь.
- **0002** (2026-10-06): КБЖУ-норма и еда (nutrition targets, food_entries).
- **0003** (2026-10-06): напоминания без weekday (reminders: minute_of_day, kind, text, enabled, last_sent_on).
- **0004** (2026-10-07): профиль пользователя (вес, рост, год рождения, цель, о себе) + `weekday` для напоминаний.
- **0005** (2026-10-07): самочувствие (wellbeing_entries: сон, боли, энергия, настроение, заметка).
- **0006** (2026-10-07): факты (user_facts: текст, категория, активен, источник).
- **0007** (2026-10-08): адаптивный план дня (day_plans: день, readiness, summary, exercises_json, inputs_hash, уникально по user_id + plan_date).

## Правила
- Любое изменение модели = новая миграция. Не правь применённые миграции.
- Только переносимые типы: `Integer`, `BigInteger` (telegram_id), `Numeric` для веса и КБЖУ, `JSON`, `DateTime(timezone=True)`. Никаких `sqlite_*` опций.
- Для SQLite в Alembic включён `render_as_batch=True` (ALTER TABLE в SQLite ограничен).
- Удаление колонки с данными — в два шага (перестать писать, потом удалить) и только с согласия Амира.
- **Важно**: `Reminder.weekday` (0=Пн..6=Вс), а `ProgramDay.weekday` (1=Пн..7=Вс). Разные нумерации из-за Python weekday() и isoweekday().

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
