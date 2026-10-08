"""Migration 0014 (program editor): the history the Mini App gets is the same before and after it.

Golden test: owner-like history saved through the API, /api/state.history captured, then `downgrade -1`
(drops day focus and the copy columns, keeps the prescription snapshots), the day items changed, and
`upgrade head` (the backfill only fills rows without a snapshot): the history is identical, and equal to
what the pre-0014 code computed from the live items.
"""

import json
from datetime import UTC, date, datetime

from alembic import command
from sqlalchemy import select, text, update

from gymbot.db.migrate import _config
from gymbot.db.models import Exercise, Program, ProgramDay, User, Workout, WorkoutSet
from gymbot.db.session import make_engine
from gymbot.services.programs import backfill_program_meta, load_program

SLUG = "arms_specialization_8w"


def legacy_target(item) -> tuple[str, bool]:
    """workouts._item_target before 0014, frozen."""
    if item.drop_reps:
        return f"дропсет {item.sets}х {'-'.join(map(str, item.drop_reps))}", True
    return f"{item.sets}х{item.reps_min}-{item.reps_max}", False


def program_json(settings) -> dict:
    return json.loads((settings.programs_dir / f"{SLUG}.json").read_text(encoding="utf-8"))


def day_json(data: dict, week: int, weekday: int) -> list[dict]:
    w = next(w for w in data["weeks"] if w["number"] == week)
    return next(d for d in w["days"] if d["weekday"] == weekday)["exercises"]


def body(wid: str, week: int, weekday: int, names: list[str], *, program_id: str = SLUG, day: int = 5) -> dict:
    return {
        "id": wid,
        "programId": program_id,
        "week": week,
        "weekday": weekday,
        "startedAt": f"2026-09-{day:02d}T15:00:00Z",
        "finishedAt": f"2026-09-{day:02d}T16:10:00Z",
        "exercises": [
            {
                "name": name,
                "target": "что угодно",  # the client's target is never stored: the server computes it
                "dropset": False,
                "sets": [
                    {"weight": 20 + k, "reps": 10, "done": True},
                    {"weight": 22 + k, "reps": 8, "done": True},
                    {"weight": None, "reps": 6, "done": False},
                ],
            }
            for k, name in enumerate(names)
        ],
    }


async def seed_history(client, auth, db, settings) -> None:
    """Two weeks of the program like the owner logs them, plus the odd cases."""
    data = program_json(settings)
    st = (await client.get("/api/state", headers=auth)).json()
    assert st["programId"] == SLUG
    n = 0
    for week in (1, 2, 3):
        for weekday in (1, 3, 5):
            names = [e["name"] for e in day_json(data, week, weekday)]
            if week == 2 and weekday == 1:
                names = [names[0].upper(), *names[1:-1], "Совсем новое упражнение"]  # case + an extra one
            if week == 3 and weekday == 3:
                names = names[:2]  # skipped half the day
            n += 1
            r = await client.post("/api/workouts", json=body(f"w{n}", week, weekday, names, day=n), headers=auth)
            assert r.status_code == 200, r.text
    # Unknown program: no day, placed by date.
    r = await client.post(
        "/api/workouts", json=body("lost", 1, 1, ["жим лёжа"], program_id="nope", day=20), headers=auth
    )
    assert r.status_code == 200, r.text
    # A chat workout (no planned day) with a drop.
    async with db() as s:
        user = await s.scalar(select(User).where(User.telegram_id == 42))
        ex = await s.scalar(select(Exercise).order_by(Exercise.id).limit(1))
        w = Workout(
            user_id=user.id, performed_on=date(2026, 9, 21), source="chat",
            started_at=datetime(2026, 9, 21, 15, tzinfo=UTC), finished_at=datetime(2026, 9, 21, 16, tzinfo=UTC),
        )
        w.sets = [
            WorkoutSet(exercise_id=ex.id, set_index=0, reps=10, weight_kg=30, raw_text="чат"),
            WorkoutSet(exercise_id=ex.id, set_index=1, reps=6, weight_kg=20, drop_index=1, raw_text="чат"),
        ]
        s.add(w)
        await s.commit()


async def history(client, auth) -> list[dict]:
    r = await client.get("/api/state", headers=auth)
    assert r.status_code == 200, r.text
    return r.json()["history"]


async def migrate(settings, revision: str, *, down: bool) -> None:
    engine, _ = make_engine(settings.database_url)

    def run(connection) -> None:  # type: ignore[no-untyped-def]
        cfg = _config()
        cfg.attributes["connection"] = connection
        (command.downgrade if down else command.upgrade)(cfg, revision)

    try:
        async with engine.begin() as conn:
            await conn.run_sync(run)
    finally:
        await engine.dispose()


async def test_history_is_identical_before_and_after_the_migration(client, auth, db, settings):
    await seed_history(client, auth, db, settings)
    before = await history(client, auth)
    assert len(before) == 11

    # What the pre-0014 serialize showed: targets from the live items of the workout's day.
    async with db() as s:
        program = await load_program(s, await s.scalar(select(Program.id).where(Program.slug == SLUG)))
        days = {d.id: d for w in program.weeks for d in w.days}
        rows = {str(w.id): w for w in (await s.scalars(select(Workout))).all()}
        snapshots_before = {wid: w.targets_json for wid, w in rows.items()}
        names = {e.id: e.name for e in (await s.scalars(select(Exercise))).all()}
    for h in before:
        row = rows[h["id"]]
        items = {i.exercise_id: i for i in days[row.program_day_id].items} if row.program_day_id else {}
        by_name = {names[i.exercise_id]: legacy_target(i) for i in items.values()}
        for ex in h["exercises"]:
            assert (ex["target"], ex["dropset"]) == by_name.get(ex["name"], ("", False)), (h["id"], ex["name"])
    assert any(ex["dropset"] for h in before for ex in h["exercises"])  # a dropset day is covered
    assert any(ex["target"] == "" for h in before for ex in h["exercises"])  # and an exercise off the day
    assert sum(1 for h in before if h["programDayId"] is None) == 2  # "nope" and the chat workout

    await migrate(settings, "-1", down=True)
    async with db() as s:  # 0013: no focus, no copy columns; the snapshots are kept on purpose
        cols = {r[1] for r in (await s.execute(text("PRAGMA table_info(workouts)"))).all()}
        assert "targets_json" in cols
        day_cols = {r[1] for r in (await s.execute(text("PRAGMA table_info(program_days)"))).all()}
        assert "focus" not in day_cols and "base_day_id" not in day_cols
        # The days change while downgraded (like an edited copy): the re-upgrade must not rebuild snapshots.
        await s.execute(text("UPDATE program_items SET sets = 1, reps_min = 5, reps_max = 5, drop_reps = NULL"))
        await s.commit()
    await migrate(settings, "head", down=False)

    after = await history(client, auth)
    assert after == before
    async with db() as s:
        rows_after = {str(w.id): w.targets_json for w in (await s.scalars(select(Workout))).all()}
        assert all(d.focus is None for d in (await s.scalars(select(ProgramDay))).all())  # dropped with 0014
        assert await backfill_program_meta(s, settings.programs_dir) == 24
        assert {p.version for p in (await s.scalars(select(Program))).all()} == {1}
    assert {k: json.loads(v) if v else None for k, v in rows_after.items()} == {
        k: json.loads(v) if v else None for k, v in snapshots_before.items()
    }


async def test_history_without_a_snapshot_falls_back_to_the_day_items(client, auth, db, settings):
    await seed_history(client, auth, db, settings)
    before = await history(client, auth)
    async with db() as s:
        await s.execute(update(Workout).values(targets_json=None))
        await s.commit()
    assert await history(client, auth) == before


async def test_snapshot_keeps_history_when_the_program_changes(client, auth, db, settings):
    await seed_history(client, auth, db, settings)
    before = await history(client, auth)
    async with db() as s:
        program = await load_program(s, await s.scalar(select(Program.id).where(Program.slug == SLUG)))
        for w in program.weeks:
            for d in w.days:
                for i in d.items:
                    i.sets, i.reps_min, i.reps_max, i.drop_reps = 1, 5, 5, None
        await s.commit()
    assert await history(client, auth) == before


async def test_reupgrade_fills_only_rows_without_a_snapshot(client, auth, db, settings):
    await seed_history(client, auth, db, settings)
    before = await history(client, auth)
    await migrate(settings, "-1", down=True)
    async with db() as s:  # a workout saved by the 0013 code: no snapshot
        first = await s.scalar(text("SELECT id FROM workouts WHERE program_day_id IS NOT NULL ORDER BY id LIMIT 1"))
        kept = await s.scalar(text("SELECT targets_json FROM workouts WHERE id = :i").bindparams(i=first))
        await s.execute(text("UPDATE workouts SET targets_json = NULL WHERE id = :i").bindparams(i=first))
        await s.commit()
    await migrate(settings, "head", down=False)
    async with db() as s:
        refilled = await s.scalar(text("SELECT targets_json FROM workouts WHERE id = :i").bindparams(i=first))
    assert json.loads(refilled) == json.loads(kept)
    assert await history(client, auth) == before
