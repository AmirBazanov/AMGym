"""ORM models. Keep them dialect-neutral so SQLite -> Postgres is a URL change + migration.

Rules: no SQLite-only types, timestamps stored in UTC, weights in kg as Numeric.
Every schema change goes through an Alembic migration (see .claude/skills/db-migrations).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    BigInteger,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    MetaData,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
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
    # NULL: a template imported from data/programs/*.json (never edited); else the user's own copy.
    owner_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    based_on_id: Mapped[int | None] = mapped_column(ForeignKey("programs.id", ondelete="SET NULL"))  # template
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")  # optimistic locking of edits
    weeks: Mapped[list[ProgramWeek]] = relationship(
        back_populates="program", cascade="all, delete-orphan", order_by="ProgramWeek.number"
    )


class ProgramWeek(Base):
    __tablename__ = "program_weeks"
    id: Mapped[int] = mapped_column(primary_key=True)
    program_id: Mapped[int] = mapped_column(ForeignKey("programs.id", ondelete="CASCADE"))
    number: Mapped[int] = mapped_column(Integer)
    program: Mapped[Program] = relationship(back_populates="weeks")
    days: Mapped[list[ProgramDay]] = relationship(
        back_populates="week", cascade="all, delete-orphan", order_by="ProgramDay.weekday"
    )


class ProgramDay(Base):
    __tablename__ = "program_days"
    id: Mapped[int] = mapped_column(primary_key=True)
    week_id: Mapped[int] = mapped_column(ForeignKey("program_weeks.id", ondelete="CASCADE"))
    weekday: Mapped[int] = mapped_column(Integer)  # 1=Mon .. 7=Sun
    focus: Mapped[str | None] = mapped_column(String(64))  # day label ("Руки и плечи", "База"); moves with the day
    # The template day a copy's day was made from (re-linking workouts, "reset to original", old programIds).
    base_day_id: Mapped[int | None] = mapped_column(ForeignKey("program_days.id", ondelete="SET NULL"))
    week: Mapped[ProgramWeek] = relationship(back_populates="days")
    items: Mapped[list[ProgramItem]] = relationship(
        back_populates="day", cascade="all, delete-orphan", order_by="ProgramItem.order"
    )


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
    # Snapshot of the day's prescriptions when the workout was saved, JSON [{exerciseId, target, dropset}]
    # (gymbot.services.workouts): later program edits never change how a past workout is shown.
    targets_json: Mapped[str | None] = mapped_column(Text)
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


# --- Wellbeing: sleep, pains, energy, mood from chat messages (used by AI advice) ---

class WellbeingEntry(Base):
    """One "how do I feel" message. Scales are 1..5; every value optional, but at least one is set.

    `pains` is a JSON list as text, [{"place": "левое плечо", "severity": 3 | null}], so the column
    stays the same on SQLite and Postgres; see gymbot.services.wellbeing for (de)serialization.
    """

    __tablename__ = "wellbeing_entries"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    noted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    sleep_hours: Mapped[Decimal | None] = mapped_column(Numeric(3, 1))
    sleep_quality: Mapped[int | None] = mapped_column(Integer)
    energy: Mapped[int | None] = mapped_column(Integer)
    mood: Mapped[int | None] = mapped_column(Integer)
    pains: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)
    raw_text: Mapped[str] = mapped_column(Text)  # original message(s), "[voice] ..." for voice


# --- Facts about the user (preferences, allergies, portion sizes...), used by the parser and advice ---

class UserFact(Base):
    """A lasting fact from the chat ("запомни: ..." or the model's `remember`) or the Mini App.

    Only active facts are sent to the LLM; at most gymbot.services.facts.MAX_ACTIVE of them.
    """

    __tablename__ = "user_facts"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    text: Mapped[str] = mapped_column(Text)  # 1..200 characters, whitespace collapsed
    category: Mapped[str] = mapped_column(String(16), default="other")  # food | training | health | schedule | other
    active: Mapped[bool] = mapped_column(default=True)
    source_text: Mapped[str | None] = mapped_column(Text)  # the chat message it came from
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # When working weights were extracted from the text (gymbot.services.baselines); None = not yet.
    baselines_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # ORM cascade: SQLite does not enforce ON DELETE CASCADE without PRAGMA foreign_keys.
    baselines: Mapped[list[ExerciseBaseline]] = relationship(
        back_populates="fact", cascade="all, delete-orphan"
    )


class ExerciseBaseline(Base):
    """A working weight the user named in a fact ("жим лёжа 90 на 8"): the Mini App's start weight while
    an exercise has no logged sets. Counts only while its fact is active; one fact may give several."""

    __tablename__ = "exercise_baselines"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    fact_id: Mapped[int] = mapped_column(ForeignKey("user_facts.id", ondelete="CASCADE"), index=True)
    exercise_id: Mapped[int] = mapped_column(ForeignKey("exercises.id"))
    weight_kg: Mapped[Decimal] = mapped_column(Numeric(6, 2))
    reps: Mapped[int | None] = mapped_column(Integer)  # None = not said ("~100 смогу"); 1 = a max
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    fact: Mapped[UserFact] = relationship(back_populates="baselines")
    exercise: Mapped[Exercise] = relationship()


class WeightOverride(Base):
    """A weight the user set for one exercise on one local day from the chat ("поставь сегодня жим 85").

    The Mini App starts that exercise with it on that day instead of the progression or baseline weight
    (gymbot.services.overrides). One row per user, exercise and day: a new command replaces the weight.
    """

    __tablename__ = "weight_overrides"
    __table_args__ = (UniqueConstraint("user_id", "exercise_id", "day"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    exercise_id: Mapped[int] = mapped_column(ForeignKey("exercises.id"))
    day: Mapped[date] = mapped_column(Date)  # local date in TIMEZONE
    weight_kg: Mapped[Decimal] = mapped_column(Numeric(6, 2))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    exercise: Mapped[Exercise] = relationship()


class ActiveWorkout(Base):
    """The workout in progress in the Mini App, a snapshot sent on every change (PUT /api/workouts/active).

    Read by the diary answer, chat workout previews and GET /api/state (gymbot.services.active_workout):
    it never becomes Workout/WorkoutSet rows, so history, PRs and plan inputs ignore it. Finishing
    (POST /api/workouts) or cancelling removes it.
    """

    __tablename__ = "active_workouts"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    client_id: Mapped[str] = mapped_column(String(64))  # the Mini App's workout id
    payload: Mapped[str] = mapped_column(Text)  # JSON of gymbot.services.workouts.WorkoutIn, done flags included
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# --- Adaptive day plan (gymbot.services.plan): today's program day adjusted to wellbeing, food, recovery ---

class DayPlan(Base):
    """One plan per user and local day; rebuilt when `inputs_hash` (what it was built from) changes."""

    __tablename__ = "day_plans"
    __table_args__ = (UniqueConstraint("user_id", "plan_date"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    plan_date: Mapped[date] = mapped_column(Date)  # local date in TIMEZONE
    readiness: Mapped[str] = mapped_column(String(8))  # normal | light | rest
    summary: Mapped[str | None] = mapped_column(Text)
    exercises_json: Mapped[str] = mapped_column(Text)  # JSON list in the API format; [] = not adjusted
    inputs_hash: Mapped[str] = mapped_column(String(64))  # sha256 hex
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DayAdjustment(Base):
    """A one-day manual change of the plan said in the chat ("сегодня облегчённо, −20 %", "в пятницу без
    ног"): an input of the adaptive day plan (gymbot.services.plan, gymbot.services.day_adjustments), never
    overwritten by its rebuild. One row per user and local day; a new command for the day merges into it.

    `weight_factor` < 1 scales the day's weights, `sets_delta` <= -1 takes sets off (never below 1),
    `skip_json` is {"exercises": [program names], "groups": [plan.muscle_group keys]}."""

    __tablename__ = "day_adjustments"
    __table_args__ = (UniqueConstraint("user_id", "day"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    day: Mapped[date] = mapped_column(Date)  # local date in TIMEZONE
    weight_factor: Mapped[Decimal | None] = mapped_column(Numeric(4, 2))
    sets_delta: Mapped[int | None] = mapped_column(Integer)
    skip_json: Mapped[dict | None] = mapped_column(JSON)
    note: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(16), default="chat")  # chat | miniapp
    raw_text: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# --- Auto deload (gymbot.services.deload): the deload week and when to offer one again ---

class DeloadState(Base):
    """One row per user. A deload runs on local days `started_on`..`until` (inclusive); the last one stays
    stored after it ends (the "6 weeks without a deload" count and stall detection start after it).
    `ask_after`: no offer before this moment (an offer was sent, «Позже», «Нет», or a deload ran)."""

    __tablename__ = "deload_states"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    started_on: Mapped[date | None] = mapped_column(Date)  # local date in TIMEZONE
    until: Mapped[date | None] = mapped_column(Date)  # last local day of the deload, inclusive
    ask_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    offered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# --- Body weight: one measurement per local day, from the chat or the Mini App (gymbot.services.body_weight) ---

class BodyWeight(Base):
    """The owner's body weight on one local day. A second measurement the same day replaces the first
    (upsert by user and `day`); the newest day also sets User.weight_kg (the profile)."""

    __tablename__ = "body_weights"
    __table_args__ = (UniqueConstraint("user_id", "day"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    day: Mapped[date] = mapped_column(Date)  # local date of measured_at in TIMEZONE
    measured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    weight_kg: Mapped[Decimal] = mapped_column(Numeric(5, 2))
    source: Mapped[str] = mapped_column(String(16))  # chat | miniapp | mcp
    note: Mapped[str | None] = mapped_column(Text)
    raw_text: Mapped[str | None] = mapped_column(Text)  # the chat message ("[voice] ..." for voice)


# --- Packaged products the user has eaten (barcode / label / manual), for exact repeats ---

class Product(Base):
    """A packaged product with label numbers per 100 g, remembered when its food entry is saved.

    `barcode` is unique per user (NULLs allowed: a label photo may have none). `updated_at` is also the last
    time the product was eaten, so "тот же батончик" picks the newest. `aliases`: extra names, comma-separated.
    See gymbot.services.products.
    """

    __tablename__ = "products"
    __table_args__ = (UniqueConstraint("user_id", "barcode"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    brand: Mapped[str | None] = mapped_column(String(200))
    barcode: Mapped[str | None] = mapped_column(String(32))
    kcal_100g: Mapped[Decimal] = mapped_column(Numeric(6, 1))
    protein_100g: Mapped[Decimal] = mapped_column(Numeric(5, 1))
    fat_100g: Mapped[Decimal] = mapped_column(Numeric(5, 1))
    carbs_100g: Mapped[Decimal] = mapped_column(Numeric(5, 1))
    net_weight_g: Mapped[Decimal | None] = mapped_column(Numeric(7, 1))  # the whole package
    serving_g: Mapped[Decimal | None] = mapped_column(Numeric(6, 1))  # one serving / scoop
    source: Mapped[str] = mapped_column(String(16))  # off | label | manual
    aliases: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


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
    kind: Mapped[str] = mapped_column(String(16))  # text | nutrition | advice | checkin
    text: Mapped[str | None] = mapped_column(Text)  # for kind=text only
    weekday: Mapped[int | None] = mapped_column(Integer)  # 0=Mon..6=Sun in TIMEZONE; None = every day
    enabled: Mapped[bool] = mapped_column(default=True)
    last_sent_on: Mapped[date | None] = mapped_column(Date)  # local date in TIMEZONE
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
