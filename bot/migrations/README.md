Alembic migrations. The app applies them on startup (`gymbot.db.migrate`); by hand: `cd bot && alembic upgrade head`.
New migration: `alembic revision --autogenerate -m "..."`, read the generated file, then upgrade.
See .claude/skills/db-migrations/SKILL.md.
