"""ORM models. Keep them dialect-neutral so SQLite -> Postgres is a URL change + migration.

Rules: no SQLite-only types, timestamps stored in UTC, weights in kg as Numeric.
Every schema change goes through an Alembic migration (see .claude/skills/db-migrations).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import JSON, BigInteger, Date, DateTime, ForeignKey, Integer, MetaData, Numeric, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    # Named constraints: SQLite batch migrations can only drop/alter constraints that have names.
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(column_0_label)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    name: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # Daily nutrition targets (stage 3)
    kcal_target: Mapped[int | None] = mapped_column(Integer)
    protein_target_g: Mapped[int | None] = mapped_column(Integer)
    fat_target_g: Mapped[int | None] = mapped_column(Integer)
    carbs_target_g: Mapped[int | None] = mapped_column(Integer)
    rest_seconds: Mapped[int] = mapped_column(Integer, default=90)
    # Profile for AI advice; every field optional.
    weight_kg: Mapped[Decimal | None] = mapped_column(Numeric(5, 1))
    height_cm: Mapped[int | None] = mapped_column(Integer)
    birth_year: Mapped[int | None] = mapped_column(Integer)
    goal: Mapped[str | None] = mapped_column(String(16))  # mass | cut | strength | health
    about: Mapped[str | None] = mapped_column(Text)  # injuries, sleep, limits (free text, <= 500 chars)


class Exercise(Base):
    """Canonical exercise. `aliases` holds spellings the LLM/user may use ("жим", "бench")."""

    __tablename__ = "exercises"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    muscle_group: Mapped[str | None] = mapped_column(String(64))
    aliases: Mapped[list[str]] = mapped_column(JSON, default=list)


# --- Ready-made programs (imported from xlsx, see docs/program-format.md) ---

class Program(Base):
    __tablename__ = "programs"
    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(100), unique=True)  # data/programs/<slug>.json
    name: Mapped[str] = mapped_column(String(200))
    source: Mapped[str | None] = mapped_column(String(200))
    weeks: Mapped[list[ProgramWeek]] = relationship(back_populates="program", cascade="all, delete-orphan")


class ProgramWeek(Base):
    __tablename__ = "program_weeks"
    id: Mapped[int] = mapped_column(primary_key=True)
    program_id: Mapped[int] = mapped_column(ForeignKey("programs.id", ondelete="CASCADE"))
    number: Mapped[int] = mapped_column(Integer)
    program: Mapped[Program] = relationship(back_populates="weeks")
    days: Mapped[list[ProgramDay]] = relationship(back_populates="week", cascade="all, delete-orphan")


class ProgramDay(Base):
    __tablename__ = "program_days"
    id: Mapped[int] = mapped_column(primary_key=True)
    week_id: Mapped[int] = mapped_column(ForeignKey("program_weeks.id", ondelete="CASCADE"))
    weekday: Mapped[int] = mapped_column(Integer)  # 1=Mon .. 7=Sun
    week: Mapped[ProgramWeek] = relationship(back_populates="days")
    items: Mapped[list[ProgramItem]] = relationship(back_populates="day", cascade="all, delete-orphan")


class ProgramItem(Base):
    __tablename__ = "program_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    day_id: Mapped[int] = mapped_column(ForeignKey("program_days.id", ondelete="CASCADE"))
    exercise_id: Mapped[int] = mapped_column(ForeignKey("exercises.id"))
    order: Mapped[int] = mapped_column(Integer)
    intensity: Mapped[str | None] = mapped_column(String(16))  # heavy | medium | light
    sets: Mapped[int] = mapped_column(Integer)
    reps_min: Mapped[int | None] = mapped_column(Integer)
    reps_max: Mapped[int | None] = mapped_column(Integer)
    drop_reps: Mapped[list[int] | None] = mapped_column(JSON)  # e.g. [12, 6, 6] for a drop set
    day: Mapped[ProgramDay] = relationship(back_populates="items")
    exercise: Mapped[Exercise] = relationship()


class UserProgram(Base):
    """Which program the user is running and since when (to know today's planned day)."""

    __tablename__ = "user_programs"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    program_id: Mapped[int] = mapped_column(ForeignKey("programs.id"))
    started_on: Mapped[date] = mapped_column(Date)  # Monday of program week 1
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    program: Mapped[Program] = relationship()


# --- Actual training log ---

class Workout(Base):
    __tablename__ = "workouts"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    performed_on: Mapped[date] = mapped_column(Date, index=True)  # local date in TIMEZONE
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String(16), default="miniapp")  # miniapp | chat
    # Id generated by the Mini App, makes POST /api/workouts idempotent on retries.
    client_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    program_day_id: Mapped[int | None] = mapped_column(ForeignKey("program_days.id"))
    note: Mapped[str | None] = mapped_column(Text)
    program_day: Mapped[ProgramDay | None] = relationship()
    sets: Mapped[list[WorkoutSet]] = relationship(
        back_populates="workout", cascade="all, delete-orphan", order_by="WorkoutSet.set_index"
    )


class WorkoutSet(Base):
    __tablename__ = "workout_sets"
    id: Mapped[int] = mapped_column(primary_key=True)
    workout_id: Mapped[int] = mapped_column(ForeignKey("workouts.id", ondelete="CASCADE"), index=True)
    exercise_id: Mapped[int] = mapped_column(ForeignKey("exercises.id"), index=True)
    set_index: Mapped[int] = mapped_column(Integer)
    reps: Mapped[int] = mapped_column(Integer)
    weight_kg: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    drop_index: Mapped[int] = mapped_column(Integer, default=0)  # 0 = main set, 1.. = drops
    raw_text: Mapped[str | None] = mapped_column(Text)  # original message, for audit/re-parse
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    workout: Mapped[Workout] = relationship(back_populates="sets")
    exercise: Mapped[Exercise] = relationship()


# --- Nutrition (stage 3) ---

class FoodEntry(Base):
    __tablename__ = "food_entries"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    eaten_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    description: Mapped[str] = mapped_column(Text)
    grams: Mapped[Decimal | None] = mapped_column(Numeric(7, 1))
    kcal: Mapped[Decimal] = mapped_column(Numeric(7, 1))
    protein_g: Mapped[Decimal] = mapped_column(Numeric(6, 1))
    fat_g: Mapped[Decimal] = mapped_column(Numeric(6, 1))
    carbs_g: Mapped[Decimal] = mapped_column(Numeric(6, 1))
    estimated: Mapped[bool] = mapped_column(default=True)  # LLM estimate vs. label data
    raw_text: Mapped[str | None] = mapped_column(Text)  # original message


# --- Reminders (daily, local time in TIMEZONE) ---

class Reminder(Base):
    """Reminder sent by the bot at `minute_of_day` local time (09:30 -> 570), daily or on one `weekday`.

    `last_sent_on` is the local date of the last claimed send; it guards against duplicates
    (see gymbot.services.reminders.tick).
    """

    __tablename__ = "reminders"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    minute_of_day: Mapped[int] = mapped_column(Integer)  # 0..1439 in TIMEZONE
    kind: Mapped[str] = mapped_column(String(16))  # text | nutrition | advice
    text: Mapped[str | None] = mapped_column(Text)  # for kind=text only
    weekday: Mapped[int | None] = mapped_column(Integer)  # 0=Mon..6=Sun in TIMEZONE; None = every day
    enabled: Mapped[bool] = mapped_column(default=True)
    last_sent_on: Mapped[date | None] = mapped_column(Date)  # local date in TIMEZONE
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
