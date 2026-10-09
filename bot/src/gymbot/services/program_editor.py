"""Program editor: the user's own copy of a template, made on the first edit, and the day edits
(PATCH /api/programs/{slug}; docs/superpowers/specs/2026-10-08-program-editor-design.md, phase 2).

Copy on first edit (`fork_program`). Templates from data/programs/*.json are never edited. The first PATCH on
the active template deep-copies it into a program owned by the user (slug `{template}.u{user id}`, name
«{template} · моя»; `-2`/« 2» on a repeat), every copy day remembering its template day in `base_day_id`.
In the same transaction:
- the user's workouts of the current cycle (performed on or after the active UserProgram's `started_on`)
  are re-linked from the template days to the copy days made from them. The days are identical at that
  moment and the workouts keep their prescription snapshot (`targets_json`; one is taken now for a row
  without it), so the history shows the same, only with the copy's slug; earlier cycles stay on the template;
- a new UserProgram starts the copy on the same date;
- the snapshot of the workout in progress (active_workouts) gets the copy's slug and day.
Template item ids in the request are translated to the copy's items (`Fork.items`).

Edits (`apply_ops`), in order, each seeing the previous ones: replace, prescribe, add, remove, reorder,
move_day.
- `week`/`weekday` name the source day; `itemId` is a ProgramItem.id of that day or the `tempId` of an
  `add` earlier in the request (the item it made in the source week).
- `weeks` (optional) are all the weeks to change and must include `week`; without it only `week` changes.
  In another week the counterpart is the day with the same weekday and the item with the exercise the
  source item had before the operation. A week without one is `skipped` with a reason, not an error;
  the same problem in the source week is an error (EditError -> 422).
- Exercise rows are never renamed (sets, baselines and weights hang on them): a replacement points the item
  at another Exercise, found by name or alias (get_or_create_exercise) or created. ProgramDay rows are never
  deleted (workouts reference them); ProgramItem rows may be.
- One exercise at most once per day, 1..20 exercises per day, prescriptions within the limits below.
- `move_day` moves the day to `toWeekday`; a day already there takes the source's weekday (a swap). Only
  `ProgramDay.weekday` changes: the row keeps its id, items, `focus` and `base_day_id`, so the workouts linked
  to it (the ✓ of the current week) move with it, a workout in progress stays on its `programDayId`, and
  `targets_json` snapshots are untouched. Weeks before the current program week (`program_position` on
  `today`) are never moved: skipped, or EditError for the source week.

Optimistic locking: the request carries the version it was made against; a mismatch is `Conflict`
(-> 409 with the current program). The version grows by one per applied PATCH; a new copy starts at 1 with
the edits already in it. The caller owns the session: commit, or roll back for a dry run.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, TypeAdapter, ValidationError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from gymbot.db.models import (
    ActiveWorkout,
    Program,
    ProgramDay,
    ProgramItem,
    ProgramWeek,
    User,
    UserProgram,
    Workout,
)
from gymbot.services.programs import (
    WEEKDAY_TITLES,
    ProgramOut,
    _tree,
    find_day,
    get_or_create_exercise,
    load_program,
    program_position,
    targets_snapshot,
    visible_program,
    visible_to,
)
from gymbot.services.workouts import WorkoutIn

log = logging.getLogger(__name__)

MAX_OPS = 50
MAX_ITEMS = 20  # exercises in a day
MAX_SETS = 20
MAX_REPS = 100
DROPS_MIN, DROPS_MAX = 2, 5
NAME_MAX = 200
SLUG_MAX = 100  # programs.slug
PROGRAM_NAME_MAX = 200  # programs.name
COPY_MARK = " · моя"
LATER_OPS = frozenset({"copy_week", "rename"})  # phase 3

# Reasons for a skipped week (shown in the Mini App's scope preview).
NO_DAY = "нет этого дня"
NO_ITEM = "нет этого упражнения"
ALREADY_THERE = "упражнение уже есть в дне"
LAST_ITEM = "последнее упражнение дня"
DAY_FULL = f"в дне уже {MAX_ITEMS} упражнений"
OTHER_SET = "другой набор упражнений"
PAST_WEEK = "неделя уже прошла"


class EditError(Exception):
    """A request the user can fix; the message (Russian) goes to the Mini App as is (422)."""


class Conflict(Exception):
    """409. `version`: the program changed since the client loaded it, `program` is the current one (for the
    template of an active copy: that copy). `not_active`: only the active program (or its template) is edited."""

    def __init__(self, reason: Literal["version", "not_active"], program: Program | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.program = program


# ---- operations (wire format) ----

ItemRef = StrictInt | Annotated[str, Field(min_length=1, max_length=64)]  # ProgramItem.id or a tempId
Intensity = Literal["heavy", "medium", "light"]


class _DayOp(BaseModel):
    model_config = ConfigDict(extra="ignore")

    week: int
    weekday: int
    weeks: list[int] | None = Field(default=None, max_length=60)


class ReplaceOp(_DayOp):
    op: Literal["replace"]
    itemId: ItemRef
    name: str = Field(max_length=1000)


class _Prescription(_DayOp):
    sets: int
    repsMin: int | None = None
    repsMax: int | None = None  # missing: exactly repsMin
    dropReps: list[int] | None = Field(default=None, max_length=20)
    intensity: Intensity | None = None  # missing: unchanged (prescribe) / none (add)


class PrescribeOp(_Prescription):
    op: Literal["prescribe"]
    itemId: ItemRef


class AddOp(_Prescription):
    op: Literal["add"]
    tempId: Annotated[str, Field(min_length=1, max_length=64)]
    name: str = Field(max_length=1000)
    position: int  # 1-based place in the day like `order`; past the end appends


class RemoveOp(_DayOp):
    op: Literal["remove"]
    itemId: ItemRef


class ReorderOp(_DayOp):
    op: Literal["reorder"]
    itemIds: list[ItemRef] = Field(max_length=MAX_ITEMS * 2)


class MoveDayOp(_DayOp):
    op: Literal["move_day"]
    toWeekday: int


Op = Annotated[
    ReplaceOp | PrescribeOp | AddOp | RemoveOp | ReorderOp | MoveDayOp, Field(discriminator="op")
]
_OPS: TypeAdapter[Any] = TypeAdapter(Op)


def parse_ops(raw: Any) -> list[Any]:
    """The request's ops as models; EditError (Russian) for a bad list, an unknown or later-phase op."""
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_OPS:
        raise EditError(f"Нужно от 1 до {MAX_OPS} правок за раз.")
    out = []
    for n, item in enumerate(raw, 1):
        kind = item.get("op") if isinstance(item, dict) else None
        if kind in LATER_OPS:
            raise EditError(f"Правка {n}: «{kind}» пока не поддерживается.")
        try:
            out.append(_OPS.validate_python(item))
        except ValidationError as e:
            err = e.errors()[0]
            where = ".".join(str(x) for x in err["loc"][1:]) or "op"
            raise EditError(f"Правка {n}: неверный формат ({where}).") from e
    return out


class Skipped(BaseModel):
    week: int
    reason: str


class OpResult(BaseModel):
    op: int  # 0-based index in `ops`
    weeks: list[int]  # where it was applied, ascending
    skipped: list[Skipped]


class PatchIn(BaseModel):
    version: int
    dryRun: bool = False
    ops: list[dict[str, Any]]  # 1..MAX_OPS, validated by parse_ops (Russian 422 messages)


class PatchOut(BaseModel):
    program: ProgramOut  # dryRun: the program as stored, unchanged (no copy made)
    switchedFrom: str | None  # a copy was made (dryRun: would be) and is now active instead of this template
    results: list[OpResult]


# ---- copy on first edit ----


@dataclass
class Fork:
    program: Program  # the new copy, tree loaded
    items: dict[int, int]  # template ProgramItem.id -> the copy's
    days: dict[int, int]  # template ProgramDay.id -> the copy's
    relinked: int  # the current cycle's workouts moved to the copy


async def _copy_slug(session: AsyncSession, base: str, user_id: int) -> str:
    taken = set((await session.scalars(select(Program.slug))).all())
    suffix = f".u{user_id}"
    stem = base[: SLUG_MAX - len(suffix) - 4]  # room for "-NNN"
    slug, n = f"{stem}{suffix}", 1
    while slug in taken:
        n += 1
        slug = f"{stem}{suffix}-{n}"
    return slug


async def _copy_name(session: AsyncSession, base: str, user_id: int) -> str:
    taken = set(
        (
            await session.scalars(
                select(Program.name).where(visible_to(user_id))
            )
        ).all()
    )
    stem = base[: PROGRAM_NAME_MAX - len(COPY_MARK) - 4]
    name, n = f"{stem}{COPY_MARK}", 1
    while name in taken:
        n += 1
        name = f"{stem}{COPY_MARK} {n}"
    return name


async def fork_program(session: AsyncSession, user: User, up: UserProgram, template: Program) -> Fork:
    """The user's own copy of `template` (tree loaded), made active from `up.started_on`; see the module doc.
    Flushes, the caller commits."""
    copy = Program(
        slug=await _copy_slug(session, template.slug, user.id),
        name=await _copy_name(session, template.name, user.id),
        source=template.source,
        owner_user_id=user.id,
        based_on_id=template.id,
        version=1,
    )
    item_pairs: list[tuple[ProgramItem, ProgramItem]] = []
    day_pairs: list[tuple[ProgramDay, ProgramDay]] = []
    for w in template.weeks:
        week = ProgramWeek(number=w.number)
        for d in w.days:
            day = ProgramDay(weekday=d.weekday, focus=d.focus, base_day_id=d.id)
            for i in d.items:
                new = ProgramItem(
                    exercise=i.exercise,
                    order=i.order,
                    intensity=i.intensity,
                    sets=i.sets,
                    reps_min=i.reps_min,
                    reps_max=i.reps_max,
                    drop_reps=list(i.drop_reps) if i.drop_reps else None,
                )
                day.items.append(new)
                item_pairs.append((i, new))
            week.days.append(day)
            day_pairs.append((d, day))
        copy.weeks.append(week)
    session.add(copy)
    await session.flush()
    items = {old.id: new.id for old, new in item_pairs}
    days = {old.id: new.id for old, new in day_pairs}
    template_days = {old.id: old for old, _ in day_pairs}

    # Re-link the current cycle's workouts to the identical copy days.
    rows = (
        await session.scalars(
            select(Workout).where(
                Workout.user_id == user.id,
                Workout.program_day_id.in_(list(days)),
                Workout.performed_on >= up.started_on,
            )
        )
    ).all()
    for w in rows:
        assert w.program_day_id is not None
        if w.targets_json is None:  # freeze what it shows now: later edits of the copy must not change it
            w.targets_json = targets_snapshot(template_days[w.program_day_id])
        w.program_day_id = days[w.program_day_id]

    session.add(UserProgram(user_id=user.id, program_id=copy.id, started_on=up.started_on))
    await _retarget_active(session, user.id, template.slug, copy.slug, days)
    await session.flush()
    return Fork(await _reload(session, copy.id), items, days, len(rows))


async def _retarget_active(
    session: AsyncSession, user_id: int, old_slug: str, new_slug: str, days: dict[int, int]
) -> None:
    """The workout in progress was prepared from the template: point its snapshot at the copy."""
    row = await session.get(ActiveWorkout, user_id)
    if row is None:
        return
    try:
        data = WorkoutIn.model_validate_json(row.payload)
    except ValueError:
        return
    if data.programId != old_slug:
        return
    data.programId = new_slug
    if data.programDayId is not None and data.programDayId in days:
        data.programDayId = days[data.programDayId]
    row.payload = data.model_dump_json()  # updated_at stays: this is not the user's change


async def _reload(session: AsyncSession, program_id: int) -> Program:
    """The program with its tree as stored now (collections already in the session are refreshed)."""
    stmt = (
        select(Program)
        .where(Program.id == program_id)
        .options(_tree())
        .execution_options(populate_existing=True)
    )
    return (await session.scalars(stmt)).one()


# ---- edits ----


@dataclass
class _Rx:
    sets: int
    reps_min: int | None
    reps_max: int | None
    drop_reps: list[int] | None


def _prescription(op: _Prescription) -> _Rx:
    if not 1 <= op.sets <= MAX_SETS:
        raise EditError(f"Подходов должно быть от 1 до {MAX_SETS}.")
    if op.dropReps is not None:
        if not DROPS_MIN <= len(op.dropReps) <= DROPS_MAX:
            raise EditError(f"В дропсете от {DROPS_MIN} до {DROPS_MAX} отрезков.")
        if any(not 1 <= r <= MAX_REPS for r in op.dropReps):
            raise EditError(f"Повторы в дропсете — от 1 до {MAX_REPS}.")
        return _Rx(op.sets, None, None, list(op.dropReps))
    if op.repsMin is None:
        raise EditError("Укажи число повторов.")
    reps_max = op.repsMax if op.repsMax is not None else op.repsMin
    if not (1 <= op.repsMin <= MAX_REPS and 1 <= reps_max <= MAX_REPS):
        raise EditError(f"Повторов должно быть от 1 до {MAX_REPS}.")
    if op.repsMin > reps_max:
        raise EditError("Повторов «от» не может быть больше, чем «до».")
    return _Rx(op.sets, op.repsMin, reps_max, None)


def _clean_name(name: str) -> str:
    clean = " ".join(name.split())
    if not 1 <= len(clean) <= NAME_MAX:
        raise EditError(f"Название упражнения — от 1 до {NAME_MAX} символов.")
    return clean


def _renumber(day: ProgramDay) -> None:
    """`order` 1..n in the list's order; the list stays sorted (order_by only applies on load)."""
    for k, item in enumerate(day.items, 1):
        item.order = k


def _sort(day: ProgramDay) -> None:
    day.items.sort(key=lambda i: (i.order, i.id or 0))  # same members: no collection events needed


@dataclass
class _Ctx:
    session: AsyncSession
    program: Program
    items: dict[int, int]  # template item id -> copy item id (a fork in this request)
    current_week: int  # the program week of `today` (move_day never touches earlier weeks)
    temp: dict[str, int] = field(default_factory=dict)  # tempId -> item id in the source week

    def item(self, day: ProgramDay, ref: int | str) -> ProgramItem:
        if isinstance(ref, str):
            if ref not in self.temp:
                raise EditError(f"Неизвестное новое упражнение «{ref}».")
            item_id = self.temp[ref]
        else:
            item_id = self.items.get(ref, ref)
        found = next((i for i in day.items if i.id == item_id), None)
        if found is None:
            raise EditError("Этого упражнения нет в этом дне. Обнови программу и попробуй ещё раз.")
        return found

    def day(self, week: int, weekday: int) -> ProgramDay | None:
        return find_day(self.program, week, weekday)


def _targets(op: _DayOp, program: Program) -> list[int]:
    """Weeks to change, the source week first."""
    numbers = {w.number for w in program.weeks}
    if not 1 <= op.weekday <= 7:
        raise EditError("День недели должен быть от 1 до 7.")
    if op.week not in numbers:
        raise EditError(f"Недели {op.week} нет в программе.")
    if op.weeks is None:
        return [op.week]
    missing = sorted(set(op.weeks) - numbers)
    if missing:
        raise EditError(f"Недель {', '.join(map(str, missing))} нет в программе.")
    if op.week not in op.weeks:
        raise EditError("Список недель должен включать неделю, которую правишь.")
    return [op.week, *sorted(set(op.weeks) - {op.week})]


def _skip(result: OpResult, week: int, reason: str) -> None:
    result.skipped.append(Skipped(week=week, reason=reason))


async def _replace(ctx: _Ctx, op: ReplaceOp, src: ProgramDay, weeks: list[int], result: OpResult) -> None:
    item = ctx.item(src, op.itemId)
    old = item.exercise_id
    ex = await get_or_create_exercise(ctx.session, _clean_name(op.name))
    for w in weeks:
        day = src if w == op.week else ctx.day(w, op.weekday)
        if day is None:
            _skip(result, w, NO_DAY)
            continue
        it = item if day is src else next((i for i in day.items if i.exercise_id == old), None)
        if it is None:
            _skip(result, w, NO_ITEM)
            continue
        if any(i is not it and i.exercise_id == ex.id for i in day.items):
            if day is src:
                raise EditError(f"«{ex.name}» уже есть в этом дне.")
            _skip(result, w, ALREADY_THERE)
            continue
        it.exercise = ex
        it.exercise_id = ex.id
        result.weeks.append(w)


async def _prescribe(ctx: _Ctx, op: PrescribeOp, src: ProgramDay, weeks: list[int], result: OpResult) -> None:
    item = ctx.item(src, op.itemId)
    rx = _prescription(op)
    ex_id = item.exercise_id
    for w in weeks:
        day = src if w == op.week else ctx.day(w, op.weekday)
        if day is None:
            _skip(result, w, NO_DAY)
            continue
        it = item if day is src else next((i for i in day.items if i.exercise_id == ex_id), None)
        if it is None:
            _skip(result, w, NO_ITEM)
            continue
        it.sets, it.reps_min, it.reps_max, it.drop_reps = rx.sets, rx.reps_min, rx.reps_max, rx.drop_reps
        if "intensity" in op.model_fields_set:
            it.intensity = op.intensity
        result.weeks.append(w)


async def _add(ctx: _Ctx, op: AddOp, src: ProgramDay, weeks: list[int], result: OpResult) -> None:
    if op.tempId in ctx.temp:
        raise EditError(f"Новое упражнение «{op.tempId}» уже добавлено в этой правке.")
    if op.position < 1:
        raise EditError("Место в дне считается с 1.")
    rx = _prescription(op)
    ex = await get_or_create_exercise(ctx.session, _clean_name(op.name))
    for w in weeks:
        day = src if w == op.week else ctx.day(w, op.weekday)
        if day is None:
            _skip(result, w, NO_DAY)
            continue
        if any(i.exercise_id == ex.id for i in day.items):
            if day is src:
                raise EditError(f"«{ex.name}» уже есть в этом дне.")
            _skip(result, w, ALREADY_THERE)
            continue
        if len(day.items) >= MAX_ITEMS:
            if day is src:
                raise EditError(f"В дне не больше {MAX_ITEMS} упражнений.")
            _skip(result, w, DAY_FULL)
            continue
        new = ProgramItem(
            exercise=ex,
            exercise_id=ex.id,
            order=0,
            intensity=op.intensity,
            sets=rx.sets,
            reps_min=rx.reps_min,
            reps_max=rx.reps_max,
            drop_reps=rx.drop_reps,
        )
        day.items.insert(min(op.position, len(day.items) + 1) - 1, new)
        _renumber(day)
        if day is src:
            await ctx.session.flush()
            ctx.temp[op.tempId] = new.id
        result.weeks.append(w)


async def _remove(ctx: _Ctx, op: RemoveOp, src: ProgramDay, weeks: list[int], result: OpResult) -> None:
    item = ctx.item(src, op.itemId)
    ex_id = item.exercise_id
    for w in weeks:
        day = src if w == op.week else ctx.day(w, op.weekday)
        if day is None:
            _skip(result, w, NO_DAY)
            continue
        it = item if day is src else next((i for i in day.items if i.exercise_id == ex_id), None)
        if it is None:
            _skip(result, w, NO_ITEM)
            continue
        if len(day.items) <= 1:
            if day is src:
                raise EditError("Нельзя убрать последнее упражнение дня.")
            _skip(result, w, LAST_ITEM)
            continue
        day.items.remove(it)  # delete-orphan: the row goes on flush
        _renumber(day)
        result.weeks.append(w)


async def _reorder(ctx: _Ctx, op: ReorderOp, src: ProgramDay, weeks: list[int], result: OpResult) -> None:
    ordered = [ctx.item(src, ref) for ref in op.itemIds]
    if len({id(i) for i in ordered}) != len(ordered) or len(ordered) != len(src.items):
        raise EditError("Порядок должен включать все упражнения дня по одному разу.")
    sequence = [i.exercise_id for i in ordered]
    for w in weeks:
        day = src if w == op.week else ctx.day(w, op.weekday)
        if day is None:
            _skip(result, w, NO_DAY)
            continue
        if day is src:
            new_order = ordered
        else:
            if Counter(i.exercise_id for i in day.items) != Counter(sequence):
                _skip(result, w, OTHER_SET)
                continue
            pool: dict[int, list[ProgramItem]] = {}
            for i in day.items:
                pool.setdefault(i.exercise_id, []).append(i)
            new_order = [pool[ex_id].pop(0) for ex_id in sequence]
        for k, it in enumerate(new_order, 1):
            it.order = k
        _sort(day)
        result.weeks.append(w)


async def _move_day(ctx: _Ctx, op: MoveDayOp, src: ProgramDay, weeks: list[int], result: OpResult) -> None:
    if not 1 <= op.toWeekday <= 7:
        raise EditError("День недели должен быть от 1 до 7.")
    if op.toWeekday == op.weekday:
        raise EditError("День уже стоит на этом дне недели.")
    if op.week < ctx.current_week:
        raise EditError(f"Неделя {op.week} уже прошла, переносить в ней нельзя.")
    for w in weeks:
        if w < ctx.current_week:
            _skip(result, w, PAST_WEEK)
            continue
        day = src if w == op.week else ctx.day(w, op.weekday)
        if day is None:
            _skip(result, w, NO_DAY)
            continue
        other = ctx.day(w, op.toWeekday)
        day.weekday = op.toWeekday
        if other is not None:
            other.weekday = op.weekday
        for pw in ctx.program.weeks:  # same members, sorted like on load (order_by only applies then)
            if pw.number == w:
                pw.days.sort(key=lambda d: d.weekday)
        result.weeks.append(w)


_HANDLERS = {
    ReplaceOp: _replace,
    PrescribeOp: _prescribe,
    AddOp: _add,
    RemoveOp: _remove,
    ReorderOp: _reorder,
    MoveDayOp: _move_day,
}


async def apply_ops(
    session: AsyncSession,
    program: Program,
    ops: list[Any],
    items: dict[int, int] | None = None,
    *,
    current_week: int = 1,
) -> list[OpResult]:
    """Apply `ops` (parse_ops) to `program` (tree loaded, the user's copy) in order. `items` translates
    template item ids after a fork in this request; `current_week` is the program week of today. Raises
    EditError on the first invalid op (the caller rolls back). Flushes."""
    ctx = _Ctx(session, program, items or {}, current_week)
    results = []
    for n, op in enumerate(ops):
        weeks = _targets(op, program)
        src = ctx.day(op.week, op.weekday)
        if src is None:
            raise EditError(f"В неделе {op.week} нет тренировки: {WEEKDAY_TITLES[op.weekday]}.")
        result = OpResult(op=n, weeks=[], skipped=[])
        await _HANDLERS[type(op)](ctx, op, src, weeks, result)
        result.weeks.sort()
        results.append(result)
    await session.flush()
    return results


# ---- the PATCH ----


@dataclass
class Outcome:
    program: Program  # the edited program as stored after the edits (the new copy after a fork)
    switched_from: str | None  # the template's slug when a copy was made
    results: list[OpResult]


async def _not_active(session: AsyncSession, user: User, program: Program, active_id: int) -> Conflict:
    """The 409 for a PATCH of `program` while `active_id` is the user's active program."""
    active = await session.get(Program, active_id)
    if (
        program.owner_user_id is None
        and active is not None
        and active.based_on_id == program.id
        and active.owner_user_id == user.id
    ):
        # A client still on the template whose copy is already active (made from another device, or by a
        # concurrent PATCH): never a second copy; the client gets the copy and checks its draft.
        return Conflict("version", await load_program(session, active.id))
    return Conflict("not_active")


async def _lock_choice(session: AsyncSession, user: User, up: UserProgram, program: Program) -> None:
    """Before a fork: make sure `up` is still the user's latest choice, under a lock that holds until the commit.

    The template's version never changes, so the optimistic check cannot tell two first edits apart; without
    this both would fork (a duplicate slug -> 500, or a second copy `-2` that takes over while the cycle's
    workouts stay on the first one). The no-op UPDATE of the `up` row is the lock: on SQLite the first write
    takes the database write lock (a concurrent fork waits for our commit), on Postgres the row lock does the
    same for this row. The SELECT after it then sees the winner's committed UserProgram."""
    await session.execute(
        update(UserProgram)
        .where(UserProgram.id == up.id)
        .values(started_on=UserProgram.started_on)
        .execution_options(synchronize_session=False)
    )
    latest = (
        await session.execute(
            select(UserProgram.id, UserProgram.program_id)
            .where(UserProgram.user_id == user.id)
            .order_by(UserProgram.id.desc())
            .limit(1)
        )
    ).one_or_none()
    if latest is None or latest.id == up.id:
        return
    if latest.program_id == program.id:
        raise Conflict("version", program)  # the template chosen again in between: reload and retry
    raise await _not_active(session, user, program, latest.program_id)


async def edit_program(
    session: AsyncSession,
    user: User,
    up: UserProgram,
    slug: str,
    version: int,
    raw_ops: Any,
    *,
    today: date,
    dry_run: bool = False,
) -> Outcome:
    """PATCH /api/programs/{slug} without the commit. Raises LookupError (unknown or another user's program),
    EditError (422), Conflict (409). `up` is the user's active program choice, `today` the user's local day
    (the current program week for move_day). `dry_run` only changes the log: the caller rolls the session
    back."""
    program = await visible_program(session, user.id, slug)
    if program is None:
        raise LookupError(slug)
    ops = parse_ops(raw_ops)
    if program.id != up.program_id:
        raise await _not_active(session, user, program, up.program_id)
    if version != program.version:
        raise Conflict("version", program)

    fork: Fork | None = None
    if program.owner_user_id is None:
        await _lock_choice(session, user, up, program)
        fork = await fork_program(session, user, up, program)
        target, items, switched = fork.program, fork.items, program.slug
    else:
        bumped = await session.execute(
            update(Program)
            .where(Program.id == program.id, Program.version == version)
            .values(version=Program.version + 1)
            .execution_options(synchronize_session=False)
        )
        if bumped.rowcount != 1:  # type: ignore[attr-defined]  # a concurrent PATCH won
            raise Conflict("version", await _reload(session, program.id))
        target, items, switched = program, {}, None
    current_week = program_position(up.started_on, len(target.weeks), today).week
    results = await apply_ops(session, target, ops, items, current_week=current_week)
    if fork is not None:  # after apply_ops: a request that fails with 422 copied nothing
        log.log(
            logging.DEBUG if dry_run else logging.INFO,
            "program %s %s %s for user %s, %d workouts re-linked",
            program.slug, "would be copied to" if dry_run else "copied to", target.slug, user.id, fork.relinked,
        )
    return Outcome(await _reload(session, target.id), switched, results)
