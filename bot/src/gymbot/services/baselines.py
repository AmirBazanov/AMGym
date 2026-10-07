"""Working weights ("рабочие веса") from the user's own words in facts.

A fact like "жим лёжа 90 на 8, присед максимум 140, рост 185" is saved as text (gymbot.services.facts).
After it is saved, `schedule` runs one small JSON call to the model in the background (not the parser:
its prompt is at its size limit) that lists the lifts and body data in the fact:

- lifts become `ExerciseBaseline` rows; the exercise must be one of the program exercises (`catalog`),
  matched by the name as said (exact or SYNONYMS), then by the model's pick if it shares a significant
  word with what was said (`shares_word`); anything else is dropped, no exercise is ever created.
  "максимум 140" = 140×1, "~100 смогу" = 100 with reps None.
- height, body weight and birth year (or age, counted from the fact's year) only fill EMPTY profile fields
  (gymbot.services.profile.fill_from_fact), and only for facts the user saved himself (the 🧠 button,
  the Mini App); facts written over MCP (source_text "[mcp] ...") give weights only, MCP notes nothing.

A baseline counts only while its fact is active (`current` joins on UserFact.active), so deactivating a
fact hides its weights and reactivating brings them back; deleting a fact deletes them (ORM cascade, and
ON DELETE CASCADE on Postgres). A fact whose text changes is processed again (`reset`).

Background, not inline: the 🧠 button and POST /api/facts answer right away, while the model (with its
fallback routes) may take seconds. Failures are logged and never undo the saved fact. `UserFact.baselines_at`
marks a processed fact (also one without a digit, which skips the model); after a model failure it stays
None and `backfill` (on startup) tries again, so existing facts get their weights after a deploy. The
backfill is paced for free models: a pause between model calls, at most BACKFILL_CAP calls per start, and
it stops at the first LLMError (rate limit, all routes down), leaving the rest for the next start.

Races: the marker is set by a conditional UPDATE (same text, still unprocessed, still active) before
anything is written, so two extractions of one fact, or a PATCH of its text meanwhile, never leave
duplicate or stale rows.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from gymbot.db.models import Exercise, ExerciseBaseline, ProgramItem, UserFact
from gymbot.db.session import Sessionmaker
from gymbot.llm.openrouter import LLMError, OpenRouterClient
from gymbot.llm.prompts import build_baseline_messages
from gymbot.services import profile as prof
from gymbot.services.programs import normalize

log = logging.getLogger(__name__)

WEIGHT_RANGE = (1.0, 500.0)  # kg on the bar or stack
REPS_RANGE = (1, 100)
AGE_RANGE = (prof.MIN_AGE, 100)
IN_CONTEXT = 8  # baselines in the advice/answer summary
CONTEXT_CHARS = 260
BACKFILL_PAUSE = 3.0  # seconds between model calls in the backfill (free models rate-limit)
BACKFILL_CAP = 20  # model calls per start; the rest waits for the next start
MCP_SOURCE = "[mcp]"  # source_text prefix of facts written over MCP (gymbot.mcp_server): no profile from them
MCP_NOTE_SOURCE = "[mcp] log_note"  # MCP notes: not facts about the user, no weights either
_DIGIT = re.compile(r"\d")
_EDGES = " .,;:!?«»\"'()"


def _rx(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern)


# Common ways to say a program exercise -> its catalog names in order of preference (only names present
# in the catalog are used). Checked with fullmatch on programs.normalize() text (lower case, ё -> е).
SYNONYMS: list[tuple[re.Pattern[str], tuple[str, ...]]] = [
    (_rx(r"(жим (штанги )?лежа( на горизонтальной скамье| со штангой| штанги)?|жим в горизонте|"
         r"жим на горизонтальной скамье|горизонтальный жим)"), ("жим лёжа",)),
    (_rx(r"(присед|приседы|приседания?)( со штангой( на плечах| на спине)?| на спине)?"), ("присед со штангой",)),
    (_rx(r"(тяга (нижнего |горизонтального )блока( к поясу| к животу)?|тяга блока к (поясу|животу)|"
         r"горизонтальная тяга)"), ("тяга горизонтального блока",)),
    (_rx(r"(тяга (верхнего |вертикального )?блока( к груди| за голову| широким хватом)?|верхняя тяга|"
         r"вертикальная тяга)"), ("тяга вертикального блока",)),
    (_rx(r"(румынская( становая)?( тяга)?( со штангой| с гантелями)?|румынка|рдл)"), ("румынская тяга",)),
    (_rx(r"(становая( тяга)?|классическая становая( тяга)?)"), ("становая тяга",)),
]


def _key(name: str) -> str:
    return normalize(name).strip(_EDGES)


def match_exercise(name: str | None, catalog: list[str]) -> str | None:
    """The catalog name for `name` (exact, ignoring case and ё/е, or by SYNONYMS), else None."""
    if not isinstance(name, str) or not name.strip():
        return None
    by_key = {_key(c): c for c in catalog}
    key = _key(name)
    if key in by_key:
        return by_key[key]
    for pattern, targets in SYNONYMS:
        if pattern.fullmatch(key):
            return next((by_key[_key(t)] for t in targets if _key(t) in by_key), None)
    return None


# Words that say how, not what: they never make two exercise names "the same exercise". Prefixes of
# normalize() text; words shorter than 4 letters ("жим", "со", "на") never count either.
GENERIC = (
    "штанг", "гантел", "гриф", "хват", "тренаж", "смит", "блок", "тяг", "лежа", "сидя", "стоя", "скам",
    "сверх", "сниз", "широк", "узк", "одной", "рукой", "руки", "весом", "свой", "своим",
)  # fmt: skip
_WORD = re.compile(r"[a-zа-я]+")


def significant_words(text: str) -> list[str]:
    """Words of `text` that name an exercise: 4+ letters, not in GENERIC (lower case, ё -> е)."""
    return [w for w in _WORD.findall(normalize(text)) if len(w) >= 4 and not w.startswith(GENERIC)]


def same_stem(a: str, b: str) -> bool:
    """"присед" ~ "приседания", "дельты" ~ "дельту"; "передняя" !~ "перекрестный"."""
    n = 0
    for x, y in zip(a, b, strict=False):
        if x != y:
            break
        n += 1
    return n >= max(4, min(len(a), len(b)) - 3)


def shares_word(a: str, b: str) -> bool:
    """Whether two exercise names share a significant word (see significant_words, same_stem)."""
    words = significant_words(b)
    return any(same_stem(x, y) for x in significant_words(a) for y in words)


# ---- the model's answer ----


@dataclass
class Lift:
    exercise: str  # exact catalog name
    weight_kg: float
    reps: int | None


@dataclass
class Extraction:
    lifts: list[Lift] = field(default_factory=list)
    profile: dict[str, Any] = field(default_factory=dict)  # keys of prof.ProfileIn: heightCm, weightKg, birthYear


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        m = re.search(r"\d+(?:[.,]\d+)?", value)
        return float(m.group().replace(",", ".")) if m else None
    return None


def _in(value: float | None, bounds: tuple[float, float]) -> bool:
    return value is not None and bounds[0] <= value <= bounds[1]


def parse_answer(data: dict, catalog: list[str], year: int) -> Extraction:
    """Validate the model's JSON: unknown exercises and out-of-range numbers are dropped, never fixed up.

    `year` is the fact's year: "мне 30" said in 2026 is born 1996 whenever the fact is processed."""
    out = Extraction()
    lifts = data.get("lifts") if isinstance(data, dict) else None
    seen: set[str] = set()
    for item in lifts if isinstance(lifts, list) else []:
        if not isinstance(item, dict):
            continue
        said = item.get("said")
        name = match_exercise(said, catalog)
        if name is None:  # the model's pick only when it shares a word with what was said
            pick = match_exercise(item.get("exercise"), catalog)
            name = pick if pick is not None and isinstance(said, str) and shares_word(said, pick) else None
        weight = _number(item.get("weight_kg"))
        reps = _number(item.get("reps"))
        if name is None or name in seen or not _in(weight, WEIGHT_RANGE):
            continue
        assert weight is not None
        seen.add(name)
        out.lifts.append(Lift(name, round(weight, 2), int(reps) if _in(reps, REPS_RANGE) else None))
    body = data.get("profile") if isinstance(data, dict) else None
    if isinstance(body, dict):
        height = _number(body.get("height_cm"))
        weight = _number(body.get("weight_kg"))
        birth = _number(body.get("birth_year"))
        age = _number(body.get("age"))
        if birth is None and _in(age, AGE_RANGE):
            assert age is not None
            birth = year - int(age)
        if _in(height, (120, 250)):
            out.profile["heightCm"] = round(height)  # type: ignore[arg-type]
        if _in(weight, (30, 300)):
            out.profile["weightKg"] = round(weight, 1)  # type: ignore[arg-type]
        if _in(birth, (prof.MIN_BIRTH_YEAR, year - prof.MIN_AGE)):
            out.profile["birthYear"] = int(birth)  # type: ignore[arg-type]
    return out


async def catalog(session: AsyncSession) -> list[str]:
    """Names of exercises in the programs: the ones the Mini App can start with a weight."""
    rows = await session.scalars(
        select(Exercise.name).join(ProgramItem, ProgramItem.exercise_id == Exercise.id).distinct().order_by(Exercise.name)
    )
    return list(rows)


async def extract(llm: OpenRouterClient, text: str, names: list[str], year: int) -> Extraction:
    """Lifts and body data in one fact text. Raises LLMError when no model answered."""
    return parse_answer(await llm.complete_json(build_baseline_messages(text, names)), names, year)


# ---- processing a fact ----


def utcnow() -> datetime:
    return datetime.now(UTC)


async def _claim(session: AsyncSession, fact_id: int, text: str, now: datetime) -> bool:
    """Mark the fact processed if it still has `text`, is active and unprocessed: the first write of the
    transaction, so it takes the lock (SQLite) or re-checks the row after a concurrent commit (Postgres)."""
    result = await session.execute(
        update(UserFact)
        .where(
            UserFact.id == fact_id,
            UserFact.text == text,
            UserFact.baselines_at.is_(None),
            UserFact.active.is_(True),
        )
        .values(baselines_at=now)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1  # type: ignore[attr-defined]


SKIPPED, FREE, RACED, DONE = "skipped", "free", "raced", "done"
CALLED = (RACED, DONE)  # outcomes after a model call
MARKED = (FREE, DONE)  # outcomes that set the marker


async def _process(
    sessionmaker: Sessionmaker,
    llm: OpenRouterClient,
    fact_id: int,
    before_model: Callable[[], Awaitable[None]] | None = None,
) -> str:
    """One fact: SKIPPED (gone, inactive, processed), FREE (marked without the model), RACED (changed or
    processed while the model thought: nothing written) or DONE. Raises LLMError when no model answered."""
    async with sessionmaker() as session:
        fact = await session.get(UserFact, fact_id)
        if fact is None or not fact.active or fact.baselines_at is not None:
            return SKIPPED
        text, user_id = fact.text, fact.user_id
        year = (fact.created_at or utcnow()).year
        source = fact.source_text or ""
        # No weights, height or age without a digit; MCP notes are not about the user: skip the model.
        if not _DIGIT.search(text) or source.startswith(MCP_NOTE_SOURCE):
            marked = await _claim(session, fact_id, text, utcnow())
            await session.commit()
            return FREE if marked else SKIPPED
        names = await catalog(session)
    # No session (and no SQLite write lock) while the model thinks.
    if before_model is not None:
        await before_model()
    found = await extract(llm, text, names, year)
    async with sessionmaker() as session:
        now = utcnow()
        if not await _claim(session, fact_id, text, now):
            await session.rollback()
            return RACED  # deleted, edited, deactivated or processed meanwhile
        await session.execute(delete(ExerciseBaseline).where(ExerciseBaseline.fact_id == fact_id))
        wanted = [x.exercise for x in found.lifts]
        rows = await session.execute(select(Exercise.name, Exercise.id).where(Exercise.name.in_(wanted)))
        ids: dict[str, int] = {name: ex_id for name, ex_id in rows}
        session.add_all(
            ExerciseBaseline(
                user_id=user_id,
                fact_id=fact_id,
                exercise_id=ids[lift.exercise],
                weight_kg=Decimal(str(lift.weight_kg)),
                reps=lift.reps,
                created_at=now,
            )
            for lift in found.lifts
            if lift.exercise in ids
        )
        # Records from chat only after the user's confirmation: a fact Claude wrote over MCP is not one.
        changed: list[str] = []
        if found.profile and not source.startswith(MCP_SOURCE):
            changed = await prof.fill_from_fact(session, user_id, found.profile)
        await session.commit()
    log.info(
        "baselines for fact %s: %s%s",
        fact_id,
        ", ".join(f"{x.exercise} {x.weight_kg}×{x.reps}" for x in found.lifts) or "none",
        f"; profile {', '.join(changed)}" if changed else "",
    )
    return DONE


async def process_fact(sessionmaker: Sessionmaker, llm: OpenRouterClient, fact_id: int) -> bool:
    """Extract and store the fact's baselines (replacing old ones) and fill empty profile values.

    Returns True when the fact got marked processed. Skips facts that are gone, inactive or already
    processed; leaves the marker unset when the model failed, so `backfill` retries.
    """
    try:
        return await _process(sessionmaker, llm, fact_id) in MARKED
    except LLMError as e:
        log.warning("baselines for fact %s: no model answered (%s)", fact_id, e)
        return False


async def reset(session: AsyncSession, fact: UserFact) -> None:
    """Forget the fact's baselines after its text changed (process it again with `schedule`). Flushes."""
    await session.execute(delete(ExerciseBaseline).where(ExerciseBaseline.fact_id == fact.id))
    session.expire(fact, ["baselines"])
    fact.baselines_at = None
    await session.flush()


async def _pause(seconds: float) -> None:
    await asyncio.sleep(seconds)


async def backfill(
    sessionmaker: Sessionmaker, llm: OpenRouterClient, cap: int = BACKFILL_CAP, pause: float = BACKFILL_PAUSE
) -> int:
    """Process active facts not processed yet, oldest first; returns how many got marked.

    Paced for free models: `pause` seconds between model calls, at most `cap` model calls (facts without
    a digit are free and do not count), and a stop at the first LLMError; the rest waits for the next
    start. Idempotent: processed facts are skipped, so running it on every startup costs nothing once done.
    """
    async with sessionmaker() as session:
        ids = list(
            await session.scalars(
                select(UserFact.id)
                .where(UserFact.active.is_(True), UserFact.baselines_at.is_(None))
                .order_by(UserFact.created_at, UserFact.id)
            )
        )
    done = calls = 0

    async def before_model() -> None:
        nonlocal calls
        if calls:
            await _pause(pause)
        calls += 1

    for fact_id in ids:
        if calls >= cap:
            log.info("baselines backfill: %s model calls, the rest waits for the next start", cap)
            break
        try:
            outcome = await _process(sessionmaker, llm, fact_id, before_model)
        except LLMError as e:
            log.warning("baselines backfill stopped at fact %s, the rest waits for the next start (%s)", fact_id, e)
            break
        done += outcome in MARKED
    if ids:
        log.info("baselines backfill: %s of %s facts processed", done, len(ids))
    return done


# ---- background tasks ----

_TASKS: set[asyncio.Task[Any]] = set()


def enabled(llm: OpenRouterClient | None) -> bool:
    return llm is not None and bool(llm.routes)


async def _logged(coro: Coroutine[Any, Any, Any], what: str) -> None:
    try:
        await coro
    except Exception:
        log.warning("%s failed", what, exc_info=True)


def _spawn(coro: Coroutine[Any, Any, Any], what: str) -> asyncio.Task[Any]:
    task = asyncio.create_task(_logged(coro, what))
    _TASKS.add(task)  # keep a reference: the loop holds tasks only weakly
    task.add_done_callback(_TASKS.discard)
    return task


def schedule(sessionmaker: Sessionmaker, llm: OpenRouterClient | None, fact_id: int) -> asyncio.Task[Any] | None:
    """Process a saved (committed) fact in the background; None without a model."""
    if not enabled(llm):
        return None
    assert llm is not None
    return _spawn(process_fact(sessionmaker, llm, fact_id), f"baselines for fact {fact_id}")


def start_backfill(sessionmaker: Sessionmaker, llm: OpenRouterClient | None) -> asyncio.Task[Any] | None:
    if not enabled(llm):
        return None
    assert llm is not None
    return _spawn(backfill(sessionmaker, llm), "baselines backfill")


async def drain() -> None:
    """Wait for the running background tasks (tests)."""
    while _TASKS:
        await asyncio.gather(*list(_TASKS))


async def cancel_all() -> None:
    """Stop background tasks on shutdown; an unfinished fact keeps baselines_at None for the next backfill."""
    tasks = list(_TASKS)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


# ---- reading ----


class BaselineOut(BaseModel):
    """Mini App wire format (/api/state `baselines`), mirrors miniapp/src/store.ts."""

    exercise: str  # exact program exercise name
    weightKg: float
    reps: int | None
    factId: int


async def current(session: AsyncSession, user_id: int) -> list[BaselineOut]:
    """The newest baseline per exercise from the user's active facts, newest fact first."""
    rows = await session.execute(
        select(ExerciseBaseline, Exercise.name)
        .join(UserFact, UserFact.id == ExerciseBaseline.fact_id)
        .join(Exercise, Exercise.id == ExerciseBaseline.exercise_id)
        .where(ExerciseBaseline.user_id == user_id, UserFact.user_id == user_id, UserFact.active.is_(True))
        .order_by(UserFact.created_at.desc(), UserFact.id.desc(), ExerciseBaseline.id)
    )
    out: dict[str, BaselineOut] = {}
    for b, name in rows:
        if name not in out:
            out[name] = BaselineOut(exercise=name, weightKg=float(b.weight_kg), reps=b.reps, factId=b.fact_id)
    return list(out.values())


def _kg(x: float) -> str:
    return str(int(x)) if x == int(x) else f"{x:g}"


def format_baseline(b: BaselineOut) -> str:
    if b.reps is None:
        return f"{b.exercise} ~{_kg(b.weightKg)}"
    return f"{b.exercise} {_kg(b.weightKg)}×{b.reps}" + (" (максимум)" if b.reps == 1 else "")


def context_line(items: list[BaselineOut], max_chars: int = CONTEXT_CHARS) -> str:
    """'Рабочие веса с твоих слов: жим лёжа 90×8, …' with as many as fit; '' without baselines."""
    line = ""
    for b in items[:IN_CONTEXT]:
        candidate = f"{line}, {format_baseline(b)}" if line else f"Рабочие веса с твоих слов: {format_baseline(b)}"
        if len(candidate) + 1 > max_chars:
            break
        line = candidate
    return line + "." if line else ""
