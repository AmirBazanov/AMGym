"""The owner's personal dashboard «Дела Амира» (repository AventraLLC/amirs-hub, a static site).

- Workouts (GET /api/hub): read only, never creates rows: the owner as in the MCP server
  (gymbot.services.access.owner_user), the program through next_weights.program_ref / day_weights, so the week
  shows the day plan, the deload and the manual day adjustments the way the Mini App does. No food, wellbeing
  or body weight here.
- State (GET/PUT /api/hub/state): the dashboard's localStorage as one JSON object per owner, with a version
  for optimistic locking (HubState).
- Files: the site itself, a clone of the repository in HUB_DIR served on /hub/ (HubFiles), updated by
  POST /api/hub/deploy (Deployer: git pull --ff-only, or the first clone).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path, PurePosixPath
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

from gymbot.config import Settings
from gymbot.db.models import Exercise, HubState, ProgramDay, User, Workout, WorkoutSet
from gymbot.mcp_server import num, set_text
from gymbot.services import day_adjustments as dayadj
from gymbot.services import deload
from gymbot.services import next_weights as nw
from gymbot.services.access import owner_user
from gymbot.services.nutrition import _aware
from gymbot.services.programs import find_day, monday_of

LOG_DAYS = 90
PLAN_EXERCISES = 5  # exercises named in a plan day's note
NOTE_MAX = 200
DEFAULT_TITLE = "Тренировка"
REST_NOTE = "Отдых по плану дня"


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]


def _short(text: str) -> str:
    return text if len(text) <= NOTE_MAX else text[: NOTE_MAX - 1].rstrip() + "…"


def _sets(sets: list[str]) -> str:
    """Runs of equal sets collapse: ["60×5"] * 5 -> "60×5×5"; different ones joined with " + "."""
    runs: list[list[Any]] = []
    for s in sets:
        if runs and runs[-1][0] == s:
            runs[-1][1] += 1
        else:
            runs.append([s, 1])
    return " + ".join(s if n == 1 else f"{s}×{n}" for s, n in runs)


def _tonnage(kg: Decimal) -> str:
    """3200 -> «3,2 т», 850 -> «850 кг»."""
    if kg >= 1000:
        return f"{num(kg / 1000)} т".replace(".", ",")
    return f"{num(kg)} кг".replace(".", ",")


async def _changes(session: AsyncSession, user_id: int, day: date) -> str | None:
    """«разгрузка: веса −15 %», «веса −20 %, без ног»: the deload and the day adjustment only. Not
    DayWeights.summary: today it is the day plan's note, which names sleep and pains, and the dashboard is
    public."""
    parts = []
    if await deload.active_until(session, user_id, day) is not None:
        parts.append(f"разгрузка: веса −{round((1 - deload.WEIGHT_FACTOR) * 100)} %")
    if (adj := await dayadj.get(session, user_id, day)) is not None and not adj.empty():
        parts.append(dayadj.describe(adj))
    return ", ".join(parts) or None


def _plan_note(names: list[str], changes: str | None) -> str:
    """Up to PLAN_EXERCISES names, fewer when the note would pass NOTE_MAX: the changes are never cut."""
    tail = f" · {changes}" if changes else ""
    for n in range(min(len(names), PLAN_EXERCISES), 0, -1):
        note = _cap(", ".join(names[:n]) + (", …" if len(names) > n else "") + tail)
        if len(note) <= NOTE_MAX:
            return note
    return _short(_cap(names[0] + ", …" + tail))


async def plan_week(
    session: AsyncSession, settings: Settings, user: User, tz: ZoneInfo, now_utc: datetime
) -> list[dict[str, Any]]:
    """One item per training day of the current week (Mon..Sun in TIMEZONE)."""
    ref = await nw.program_ref(session, user, settings)
    if ref is None:
        return []
    monday = monday_of(now_utc.astimezone(tz).date())
    out: list[dict[str, Any]] = []
    for k in range(7):
        day = monday + timedelta(days=k)
        dw = await nw.day_weights(session, user, settings, tz, now_utc, day, ref)
        if dw is None:
            continue
        program_day = find_day(ref.program, dw.week, dw.weekday)
        title = (program_day.focus if program_day else None) or DEFAULT_TITLE
        names = [r.name for r in dw.rows if r.suggestion is not None]
        if dw.rest or not names:
            note = REST_NOTE
        else:
            note = _plan_note(names, await _changes(session, user.id, day))
        # No "time": program days have none, and the dashboard expects the key left out.
        out.append({"day": dw.weekday, "title": title, "note": note})
    return out


async def workout_log(session: AsyncSession, user_id: int, today: date) -> list[dict[str, Any]]:
    """Saved workouts of the last LOG_DAYS local days; workouts without sets are left out."""
    rows = (
        await session.execute(
            select(Workout.id, Workout.performed_on, Workout.started_at, Workout.finished_at, ProgramDay.focus,
                   Exercise.name, WorkoutSet.weight_kg, WorkoutSet.reps, WorkoutSet.drop_index)
            .join(WorkoutSet, WorkoutSet.workout_id == Workout.id)
            .join(Exercise, Exercise.id == WorkoutSet.exercise_id)
            .outerjoin(ProgramDay, ProgramDay.id == Workout.program_day_id)
            .where(Workout.user_id == user_id, Workout.performed_on >= today - timedelta(days=LOG_DAYS - 1))
            .order_by(Workout.performed_on, Workout.id, WorkoutSet.set_index)
        )
    ).all()
    workouts: dict[int, dict[str, Any]] = {}
    exercises: defaultdict[int, dict[str, list[str]]] = defaultdict(dict)
    for wid, day, started, finished, focus, name, weight, reps, drop_index in rows:
        w = workouts.setdefault(
            wid, {"date": day, "title": focus or DEFAULT_TITLE, "started": started, "finished": finished,
                  "volume": Decimal(0)}
        )
        if weight is not None:
            w["volume"] += Decimal(weight) * reps
        sets = exercises[wid].setdefault(name, [])
        if drop_index and sets:  # same as mcp_server.training_summary
            sets[-1] += " → " + set_text(weight, reps)
        else:
            sets.append(set_text(weight, reps))
    out = []
    for wid, w in workouts.items():
        parts = [", ".join(f"{name} {_sets(sets)}" for name, sets in exercises[wid].items())]
        if w["volume"]:
            parts.append("тоннаж " + _tonnage(w["volume"]))
        if w["finished"] is not None:
            minutes = int((_aware(w["finished"]) - _aware(w["started"])).total_seconds() // 60)
            if minutes > 0:
                parts.append(f"{minutes} мин")
        out.append({"date": w["date"].isoformat(), "title": w["title"], "note": _cap(" · ".join(parts))})
    return out


async def build(session: AsyncSession, settings: Settings, tz: ZoneInfo, now_utc: datetime) -> dict[str, Any]:
    """The whole GET /api/hub answer; empty lists until the owner has reached the bot or the Mini App."""
    user = await owner_user(session, settings)
    if user is None:
        return {"workouts": {"plan": [], "log": []}}
    return {
        "workouts": {
            "plan": await plan_week(session, settings, user, tz, now_utc),
            "log": await workout_log(session, user.id, now_utc.astimezone(tz).date()),
        }
    }


# ---- state: the dashboard's localStorage, one blob per owner ----

STATE_MAX_BYTES = 256 * 1024  # PUT body limit (413 above)


class Conflict(Exception):
    """PUT from a version that is not the stored one; `current` is the stored state as GET answers it."""

    def __init__(self, current: dict[str, Any]) -> None:
        super().__init__("version conflict")
        self.current = current


def state_out(row: HubState | None) -> dict[str, Any]:
    if row is None:
        return {"version": 0, "updated_at": None, "state": {}}
    return {
        "version": row.version,
        "updated_at": _aware(row.updated_at).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "state": json.loads(row.state),
    }


async def _state_row(session: AsyncSession, user_id: int) -> HubState | None:
    return await session.scalar(select(HubState).where(HubState.user_id == user_id))


async def get_state(session: AsyncSession, user_id: int) -> dict[str, Any]:
    return state_out(await _state_row(session, user_id))


class BadBody(Exception):
    pass


def parse_put(raw: bytes) -> tuple[int, dict[str, Any]]:
    """(base_version, state) of a PUT body {"base_version": 12, "state": {...}}; BadBody when malformed."""
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as e:
        raise BadBody("body is not JSON") from e
    if not isinstance(body, dict):
        raise BadBody("body must be an object")
    base, state = body.get("base_version"), body.get("state")
    if not isinstance(base, int) or isinstance(base, bool) or base < 0:
        raise BadBody("base_version must be a non-negative integer")
    if not isinstance(state, dict):
        raise BadBody("state must be an object")
    return base, state


async def save_state(
    session: AsyncSession, user_id: int, base_version: int, state: dict[str, Any], now: datetime
) -> dict[str, Any]:
    """Store `state` as version base_version + 1 when base_version is the stored one (0: nothing stored yet),
    else raise Conflict. Compare-and-set in one UPDATE, so two devices saving at once cannot both win."""
    text = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
    result = await session.execute(
        update(HubState)
        .where(HubState.user_id == user_id, HubState.version == base_version)
        .values(version=base_version + 1, state=text, updated_at=now)
    )
    if result.rowcount == 0:  # type: ignore[attr-defined]
        if base_version != 0:
            await session.rollback()
            raise Conflict(await get_state(session, user_id))
        session.add(HubState(user_id=user_id, version=1, state=text, updated_at=now))
        try:
            await session.flush()
        except IntegrityError:  # a row exists already: the dashboard is behind
            await session.rollback()
            raise Conflict(await get_state(session, user_id)) from None
    await session.commit()
    return {"version": base_version + 1, "updated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "state": state}


# ---- files: the site from HUB_DIR on /hub/, updated by git ----

HIDDEN_DIRS = frozenset({"docs"})  # first path segment never served; dot names (.git, .claude, .env) neither


class HubFiles(StaticFiles):
    """StaticFiles over HUB_DIR without the repository's own files. A missing HUB_DIR answers 404 (the first
    clone comes from setup-ec2.sh or POST /api/hub/deploy, after the app has started)."""

    def __init__(self, directory: Path) -> None:
        super().__init__(directory=directory, html=True, check_dir=False)

    async def check_config(self) -> None:
        if Path(str(self.directory)).is_dir():
            await super().check_config()

    async def get_response(self, path: str, scope: Scope) -> Response:
        parts = [p for p in PurePosixPath(path).parts if p != "."]
        if (
            not Path(str(self.directory)).is_dir()
            or any(p.startswith(".") for p in parts)
            or (parts and parts[0] in HIDDEN_DIRS)
        ):
            raise HTTPException(404)
        return await super().get_response(path, scope)


GIT_TIMEOUT = 60.0  # seconds for one git command
DEPLOY_EVERY = 10.0  # seconds between two deploys


class GitError(Exception):
    pass


class TooSoon(Exception):
    def __init__(self, retry_after: int) -> None:
        super().__init__("too soon")
        self.retry_after = retry_after


def git_env(ssh_key: Path | None) -> dict[str, str]:
    """Never prompt; with a deploy key, ssh uses only it and trusts github.com on first use."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    if ssh_key is not None:
        env["GIT_SSH_COMMAND"] = (
            f"ssh -i {ssh_key} -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=accept-new"
        )
    return env


async def git(*args: str, env: dict[str, str] | None = None, timeout: float = GIT_TIMEOUT) -> str:
    """stdout of `git *args`; GitError with git's message on a non-zero exit or the timeout."""
    proc = await asyncio.create_subprocess_exec(
        "git", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env or git_env(None),
        start_new_session=True,  # its own process group: a timeout kills ssh too, which holds the pipes
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        await proc.wait()
        raise GitError(f"git timed out after {timeout:.0f} s") from None
    if proc.returncode != 0:
        raise GitError((err or out).decode(errors="replace").strip() or f"git exited with {proc.returncode}")
    return out.decode(errors="replace").strip()


async def pull(hub_dir: Path, repo: str, ssh_key: Path | None = None) -> dict[str, Any]:
    """git pull --ff-only in `hub_dir`, or the first clone of `repo` there: {"commit", "changed"}.

    When HUB_REPO names another repository than the clone's origin (the dashboard moved), origin is switched
    and the clone reset to the new repository's branch: its history need not continue the old one, and the
    clone is deploy-only, nothing local to keep."""
    env = git_env(ssh_key)
    d = str(hub_dir)
    if not (hub_dir / ".git").exists():
        await git("clone", "-q", repo, d, env=env)
        return {"commit": await git("-C", d, "rev-parse", "HEAD", env=env), "changed": True}
    before = await git("-C", d, "rev-parse", "HEAD", env=env)
    if await git("-C", d, "remote", "get-url", "origin", env=env) != repo:
        branch = await git("-C", d, "rev-parse", "--abbrev-ref", "HEAD", env=env)
        await git("-C", d, "remote", "set-url", "origin", repo, env=env)
        await git("-C", d, "fetch", "-q", "origin", branch, env=env)
        await git("-C", d, "reset", "-q", "--hard", "FETCH_HEAD", env=env)
    else:
        await git("-C", d, "pull", "-q", "--ff-only", env=env)
    after = await git("-C", d, "rev-parse", "HEAD", env=env)
    return {"commit": after, "changed": after != before}


class Deployer:
    """One deploy at a time and at most one per DEPLOY_EVERY seconds (TooSoon otherwise)."""

    def __init__(self, hub_dir: Path, repo: str, ssh_key: Path | None = None, every: float = DEPLOY_EVERY) -> None:
        self.hub_dir, self.repo, self.ssh_key, self.every = hub_dir, repo, ssh_key, every
        self.last = float("-inf")
        self.lock = asyncio.Lock()

    async def run(self) -> dict[str, Any]:
        wait = self.last + self.every - time.monotonic()
        if self.lock.locked() or wait > 0:
            raise TooSoon(max(1, round(wait)))
        async with self.lock:
            self.last = time.monotonic()
            return await pull(self.hub_dir, self.repo, self.ssh_key)
