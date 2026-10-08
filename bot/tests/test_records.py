"""Personal records (gymbot.services.records) and where they are announced: the chat save, POST /api/workouts,
saved edits, the live event for the Mini App and the diary answer's "Рекорды за 14 дней" line.

Time is pinned everywhere: the dates below are fixed and the deload offer (which reads the clock when the
callers do not pass `now`) is replaced by a recorder in the wiring tests.
"""

import json
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import select
from test_log_text import T0, USER, FakeLLM, message, token_of

from gymbot.api.app import create_app
from gymbot.db.models import User, Workout, WorkoutSet
from gymbot.handlers import log_text
from gymbot.handlers import saved_edits as hse
from gymbot.llm.schemas import ParsedExercise, ParsedSet, ParseResult
from gymbot.services import answer, deload, live, records, workout_events
from gymbot.services import saved_edits as se
from gymbot.services.programs import get_or_create_exercise
from gymbot.services.users import get_or_create_user
from gymbot.services.workouts import WorkoutIn

MSK = ZoneInfo("Europe/Moscow")
TODAY = date(2026, 10, 6)  # the local day of T0 (15:00 in Moscow)
NOW = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
BENCH = "жим лёжа"
GYM = "Жим штанги лёжа"  # what the parser/FakeLLM sends; stored normalized
SQUAT = "присед со штангой"
DEAD = "становая тяга"


class FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW if tz is None else NOW.astimezone(tz)


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    """Fresh process-wide state: the announce guard (every test DB numbers its workouts from 1), the hub, the
    chat stores; the diary answer stubbed; the deload offer recorded (and its clock pinned)."""
    records._announced.clear()
    live.hub = live.Hub()
    for store in (log_text.PENDING, log_text.CONTEXT, log_text.FACTS, hse.OFFERS, hse.CHOICES):
        store.clear()

    async def parser_answer(message, text, result, *args):
        return result, None

    monkeypatch.setattr(log_text, "_diary_answer", parser_answer)
    monkeypatch.setattr(hse, "utcnow", lambda: T0 + timedelta(minutes=5))
    monkeypatch.setattr(workout_events, "datetime", FrozenDatetime)
    yield
    records._announced.clear()
    live.hub = live.Hub()
    for store in (log_text.PENDING, log_text.CONTEXT, log_text.FACTS, hse.OFFERS, hse.CHOICES):
        store.clear()


@pytest.fixture
def offers(monkeypatch) -> list[tuple]:
    """workout_events.offer_deload replaced: the calls are recorded, nothing is sent."""
    calls: list[tuple] = []

    async def fake(sessionmaker, user_id, send, tz, now, programs_dir=None):
        calls.append((user_id, now))
        return False

    monkeypatch.setattr(workout_events, "offer_deload", fake)
    return calls


@pytest.fixture
def llm(settings):
    return FakeLLM(settings)


# ---- helpers ----


def L(weight, reps):
    return records.Lift(weight, reps)


async def add_user(db, tg: int = USER) -> int:
    async with db() as s:
        user = await get_or_create_user(s, tg, "U")
        await s.commit()
        return user.id


async def add_workout(db, uid, day, exercises, source="miniapp", raw=None) -> list[int]:
    """exercises: [(name, [(reps, weight) or (reps, weight, drop_index)])]. Returns the set ids in order."""
    at = datetime.combine(day, datetime.min.time(), tzinfo=UTC).replace(hour=9)
    ids: list[int] = []
    async with db() as s:
        w = Workout(user_id=uid, performed_on=day, started_at=at, source=source)
        s.add(w)
        await s.flush()
        i = 0
        for name, sets in exercises:
            ex = await get_or_create_exercise(s, name)
            for st in sets:
                reps, weight, *drop = st
                row = WorkoutSet(workout_id=w.id, exercise_id=ex.id, set_index=i, reps=reps, weight_kg=weight,
                                 drop_index=drop[0] if drop else 0, raw_text=raw,
                                 created_at=at + timedelta(minutes=i))
                s.add(row)
                await s.flush()
                ids.append(row.id)
                i += 1
        await s.commit()
    return ids


async def find(db, uid, ids) -> list[records.Record]:
    async with db() as s:
        return await records.find_new(s, uid, ids)


def callback(data: str, user_id: int = USER):
    """Like test_log_text.callback, but its message can also `answer` (the record lines go there)."""
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=user_id, full_name="Amir"),
        message=SimpleNamespace(edit_text=AsyncMock(), edit_reply_markup=AsyncMock(), answer=AsyncMock()),
        answer=AsyncMock(),
    )


def sent_texts(cb) -> list[str]:
    return [c.args[0] for c in cb.message.answer.await_args_list]


def workout_answer(*exercises) -> dict:
    return {"kind": "workout", "exercises": [
        {"exercise": n, "sets": [{"reps": r, "weight_kg": w} for r, w in sets]} for n, sets in exercises
    ]}


async def say_and_save(text, llm, settings, db, at=T0):
    msg = message(text, at)
    await log_text.log_free_text(msg, settings, db, llm.client)
    cb = callback(f"save:{token_of(msg)}")
    await log_text.save(cb, settings, db)
    return cb


# ---- 1. best_record: the rules ----


def rec(history, new, name=BENCH):
    return records.best_record(name, history, new)


def test_first_time_is_never_a_record():
    assert rec([], [L(100, 5)]) is None
    assert rec([], [L(None, 20)]) is None


def test_e1rm_record_and_its_line():
    r = rec([L(90, 6)], [L(92.5, 6)])
    assert (r.kind, r.weight, r.reps) == ("e1rm", 92.5, 6)
    assert r.old == pytest.approx(108) and r.new == pytest.approx(111)
    assert records.line(r) == "🏆 Новый рекорд: жим лёжа 92,5×6 — 1ПМ 108 → 111 кг"


def test_epley_single_rep_is_the_weight_itself():
    assert records.e1rm(100, 1) == 100
    assert records.e1rm(90, 6) == pytest.approx(108)
    r = rec([L(100, 1)], [L(101, 1)])
    assert (r.kind, r.old, r.new) == ("e1rm", 100, 101)


def test_weight_record_when_the_one_rep_max_is_not_higher():
    r = rec([L(100, 5)], [L(105, 1)])  # 1RM 116.7 -> 105, but nobody lifted 105 before
    assert (r.kind, r.weight, r.reps, r.old, r.new) == ("weight", 105, 1, 100, 105)
    assert records.line(r) == "🏆 Новый рекорд: жим лёжа 105×1 — максимальный вес 100 → 105 кг"


def test_reps_record_is_the_most_reps_at_this_weight_or_more():
    r = rec([L(90, 7), L(100, 5)], [L(90, 8)])  # 1RM 114 < 116.7, weight 90 < 100
    assert (r.kind, r.weight, r.reps, r.old, r.old_weight) == ("reps", 90, 8, 7, 90)
    assert records.line(r) == "🏆 Новый рекорд: жим лёжа 90×8 — 8 повторов с 90 кг (было 90×7)"


def test_reps_record_at_a_lighter_weight_against_a_heavier_set():
    r = rec([L(90, 8)], [L(80, 9)])  # nobody did 80+ kg for 9 reps
    assert (r.kind, r.old, r.old_weight) == ("reps", 8, 90)
    assert records.detail(r) == "9 повторов с 80 кг (было 90×8)"


@pytest.mark.parametrize(
    ("history", "new"),
    [
        ([L(100, 5)], [L(100, 5)]),  # identical set
        ([L(100, 5)], [L(100, 4)]),  # same weight, fewer reps
        ([L(90, 7), L(100, 5)], [L(90, 7)]),  # reps tie
        ([L(90, 8)], [L(80, 8)]),  # dominated: heavier set with the same reps
        ([L(90, 8)], [L(80, 7)]),
        ([L(None, 10)], [L(None, 10)]),  # bodyweight tie
        ([L(0, 10)], [L(None, 10)]),  # 0 kg is bodyweight too
        ([L(100, 5)], [L(100, 0)]),  # a set without reps is nothing
    ],
)
def test_ties_and_weaker_sets_are_not_records(history, new):
    assert rec(history, new) is None


def test_e1rm_tie_is_not_an_e1rm_record():
    # 110x1 and 100x3 are both 110 by Epley: the tie is not a 1RM record (it is a reps record instead)
    r = rec([L(110, 1)], [L(100, 3)])
    assert r.kind == "reps"


def test_e1rm_compared_at_two_decimals_not_by_float_noise():
    # 100x3 is 110.00000000000001 in floats; the same 1RM again must not count
    assert rec([L(100, 3)], [L(100, 3)]) is None
    assert rec([L(110, 1)], [L(100, 3), L(90, 3)]) is not None  # 100x3 ties, but reps beat 110x1
    assert rec([L(100, 3)], [L(110, 1)]).kind == "weight"  # equal 1RM, heavier weight


def test_one_record_per_exercise_in_priority_order():
    # 85x8 is a 1RM, weight and reps record at once: the 1RM one is reported
    assert rec([L(80, 8)], [L(85, 8)]).kind == "e1rm"
    # heavier weight AND more reps than anything, but a lower 1RM is impossible: weight comes before reps
    assert rec([L(100, 10)], [L(105, 1)]).kind == "weight"


def test_best_of_several_new_sets():
    r = rec([L(80, 8)], [L(80, 8), L(85, 6), L(90, 4)])  # 1RM: 101.3 (85x6), 102 (90x4)
    assert (r.kind, r.weight, r.reps) == ("e1rm", 90, 4)


def test_bodyweight_record():
    r = rec([L(None, 10)], [L(None, 12)], "подтягивания")
    assert (r.kind, r.weight, r.reps, r.old, r.new) == ("bw_reps", None, 12, 10, 12)
    assert records.line(r) == "🏆 Новый рекорд: подтягивания 12 повт. — было 10 повторов"
    assert rec([L(0, 10)], [L(0.0, 11)]).kind == "bw_reps"


def test_bodyweight_and_weighted_sets_do_not_mix():
    assert rec([L(20, 10)], [L(None, 30)]) is None  # no bodyweight history
    assert rec([L(None, 10)], [L(20, 10)]) is None  # no weighted history
    # weighted pull-ups: bodyweight sets are compared with bodyweight sets only
    assert rec([L(20, 5), L(None, 10)], [L(None, 11)]).kind == "bw_reps"
    assert rec([L(20, 5), L(None, 10)], [L(None, 9)]) is None


# ---- 2. text ----


@pytest.mark.parametrize(
    ("weight", "text"), [(92.5, "92,5×6"), (60.0, "60×6"), (102.25, "102,25×6"), (5, "5×6")]
)
def test_set_text_uses_a_decimal_comma(weight, text):
    assert records.Record("x", weight, 6, "weight", 1, 2).set_text == text


@pytest.mark.parametrize(
    ("old", "new", "shown"),
    [
        (108.0, 111.0, "1ПМ 108 → 111 кг"),
        (116.67, 117.4, "1ПМ 116,7 → 117,4 кг"),  # both round to 117: one decimal
        (110.2, 110.4, "1ПМ 110,2 → 110,4 кг"),  # never «110 → 110»
        (109.6, 110.4, "1ПМ 109,6 → 110,4 кг"),
        (99.6, 100.4, "1ПМ 99,6 → 100,4 кг"),
        (100.0, 100.04, "1ПМ 100 → 100 кг"),  # the same to the decimal: rounding only
    ],
)
def test_e1rm_detail_never_prints_the_same_number_twice(old, new, shown):
    r = records.Record("жим", 90, 8, "e1rm", old, new)
    assert records.detail(r) == shown


@pytest.mark.parametrize(
    ("n", "word"),
    [(1, "повтор"), (2, "повтора"), (4, "повтора"), (5, "повторов"), (11, "повторов"), (12, "повторов"),
     (14, "повторов"), (21, "повтор"), (22, "повтора"), (25, "повторов")],
)
def test_reps_word_agrees_with_the_number(n, word):
    assert records._reps_word(n) == word


def test_bodyweight_detail_for_one_rep():
    r = records.Record("подтягивания", None, 2, "bw_reps", 1, 2)
    assert records.detail(r) == "было 1 повтор"


def test_message_text_is_at_most_five_lines():
    many = [records.Record(f"упр {i}", 50, 5, "weight", 40, 50) for i in range(7)]
    lines = records.message_text(many).split("\n")
    assert len(lines) == records.MAX_LINES == 5
    assert lines[0].startswith("🏆 Новый рекорд: упр 0 ") and lines[-1].startswith("🏆 Новый рекорд: упр 4 ")
    assert records.message_text(many[:2]).count("\n") == 1
    assert records.message_text([]) == ""


def test_wire_is_what_the_toast_needs():
    r = rec([L(90, 6)], [L(92.5, 6)])
    assert r.wire() == {"exercise": BENCH, "weight": 92.5, "reps": 6, "kind": "e1rm", "text": "жим лёжа 92,5×6"}
    bw = records.Record("подтягивания", None, 12, "bw_reps", 10, 12)
    assert bw.wire()["text"] == "подтягивания 12 повт." and bw.wire()["weight"] is None


# ---- 3. find_new on the database ----


async def test_first_workout_of_an_exercise_is_never_a_record(db):
    uid = await add_user(db)
    ids = await add_workout(db, uid, TODAY, [(BENCH, [(8, 80), (8, 90), (8, 100)])])
    assert await find(db, uid, ids) == []


async def test_a_heavier_workout_beats_the_old_one(db):
    uid = await add_user(db)
    await add_workout(db, uid, TODAY - timedelta(days=3), [(BENCH, [(6, 90)])])
    ids = await add_workout(db, uid, TODAY, [(BENCH, [(6, 92.5)])])
    (r,) = await find(db, uid, ids)
    assert (r.exercise, r.kind, r.weight, r.reps) == (BENCH, "e1rm", 92.5, 6)
    assert records.line(r) == "🏆 Новый рекорд: жим лёжа 92,5×6 — 1ПМ 108 → 111 кг"


async def test_sets_not_in_set_ids_are_history_including_the_same_workout(db):
    uid = await add_user(db)
    await add_workout(db, uid, TODAY - timedelta(days=3), [(BENCH, [(8, 80)])])
    # a chat workout grows message by message: 90x8 was saved by the first message, 88x8 by the second
    ids = await add_workout(db, uid, TODAY, [(BENCH, [(8, 90), (8, 88)])], source="chat")
    assert await find(db, uid, ids) == [records.Record(BENCH, 90, 8, "e1rm", 80 * (1 + 8 / 30), 90 * (1 + 8 / 30))]
    assert await find(db, uid, ids[1:]) == []  # 88x8 is below the 90x8 of the same workout
    (r,) = await find(db, uid, ids[:1])
    assert (r.weight, r.reps) == (90, 8)  # and 90x8 beat the old workout (88x8 is history for it, still lower)


async def test_only_this_workout_has_the_exercise_means_no_record(db):
    uid = await add_user(db)
    await add_workout(db, uid, TODAY - timedelta(days=3), [(SQUAT, [(5, 100)])])  # another exercise
    ids = await add_workout(db, uid, TODAY, [(BENCH, [(8, 80), (8, 90)])], source="chat")
    assert await find(db, uid, ids[1:]) == []  # 90x8 beats 80x8, but both are the first time on the bench


async def test_drops_are_not_main_sets(db):
    uid = await add_user(db)
    # an old heavy drop is not the old best
    await add_workout(db, uid, TODAY - timedelta(days=3), [(BENCH, [(8, 80), (6, 100, 1)])])
    ids = await add_workout(db, uid, TODAY, [(BENCH, [(8, 85)])])
    (r,) = await find(db, uid, ids)
    assert (r.kind, r.old) == ("e1rm", pytest.approx(80 * (1 + 8 / 30)))
    # a new heavy drop is not a record; a drop is not history for the main set either
    ids = await add_workout(db, uid, TODAY + timedelta(days=1), [(BENCH, [(8, 80), (6, 120, 1), (4, 130, 2)])])
    assert await find(db, uid, ids) == []


async def test_a_drop_only_selection_has_nothing_to_compare(db):
    uid = await add_user(db)
    await add_workout(db, uid, TODAY - timedelta(days=3), [(BENCH, [(8, 80)])])
    ids = await add_workout(db, uid, TODAY, [(BENCH, [(8, 80), (6, 120, 1)])])
    assert await find(db, uid, ids[1:]) == []


async def test_one_record_per_exercise_in_set_order(db):
    uid = await add_user(db)
    await add_workout(db, uid, TODAY - timedelta(days=3), [(BENCH, [(8, 80)]), (SQUAT, [(5, 100)]), (DEAD, [(5, 120)])])
    ids = await add_workout(
        db, uid, TODAY, [(SQUAT, [(5, 105), (5, 110)]), (BENCH, [(8, 85)]), (DEAD, [(5, 120)])]
    )
    found = await find(db, uid, ids)
    assert [(r.exercise, r.weight) for r in found] == [(SQUAT, 110), (BENCH, 85)]  # one squat record, deadlift tie


async def test_other_users_sets_neither_count_nor_are_checked(db):
    me, other = await add_user(db), await add_user(db, 7)
    await add_workout(db, other, TODAY - timedelta(days=3), [(BENCH, [(10, 200)])])
    await add_workout(db, me, TODAY - timedelta(days=3), [(BENCH, [(8, 80)])])
    mine = await add_workout(db, me, TODAY, [(BENCH, [(8, 85)])])
    theirs = await add_workout(db, other, TODAY, [(BENCH, [(10, 210)])])
    (r,) = await find(db, me, mine)
    assert r.old == pytest.approx(80 * (1 + 8 / 30))  # not the other user's 200x10
    assert await find(db, me, theirs) == []  # someone else's set ids are not mine to announce


async def test_no_ids_or_unknown_ids(db):
    uid = await add_user(db)
    await add_workout(db, uid, TODAY, [(BENCH, [(8, 80)])])
    assert await find(db, uid, []) == []
    assert await find(db, uid, {9999}) == []


async def test_bodyweight_record_on_the_database(db):
    uid = await add_user(db)
    await add_workout(db, uid, TODAY - timedelta(days=2), [("подтягивания", [(10, None), (8, None)])])
    ids = await add_workout(db, uid, TODAY, [("подтягивания", [(9, None), (12, 0)])])
    (r,) = await find(db, uid, ids)
    assert (r.kind, r.reps, r.old) == ("bw_reps", 12, 10)


async def test_several_new_sets_at_once_use_the_best(db):
    uid = await add_user(db)
    await add_workout(db, uid, TODAY - timedelta(days=2), [(BENCH, [(8, 80)])])
    ids = await add_workout(db, uid, TODAY, [(BENCH, [(8, 80), (6, 85), (4, 90)])])
    (r,) = await find(db, uid, ids)
    assert (r.weight, r.reps) == (90, 4)  # the highest 1RM of the three


# ---- 4. announce ----


async def seven_records(db) -> tuple[int, list[int]]:
    uid = await add_user(db)
    names = [f"упражнение {i}" for i in range(7)]
    await add_workout(db, uid, TODAY - timedelta(days=3), [(n, [(8, 50)]) for n in names])
    ids = await add_workout(db, uid, TODAY, [(n, [(8, 60)]) for n in names])
    return uid, ids


async def announce(db, uid, ids, send, **kw):
    async with db() as s:
        return await records.announce(s, uid, ids, send, **kw)


async def test_announce_sends_the_lines_and_publishes_to_the_mini_app(db):
    uid = await add_user(db)
    await add_workout(db, uid, TODAY - timedelta(days=3), [(BENCH, [(6, 90)])])
    ids = await add_workout(db, uid, TODAY, [(BENCH, [(6, 92.5)])])
    sub = live.hub.subscribe(uid)
    sent: list[str] = []

    async def send(text):
        sent.append(text)

    found = await announce(db, uid, ids, send)
    assert sent == ["🏆 Новый рекорд: жим лёжа 92,5×6 — 1ПМ 108 → 111 кг"]
    assert len(found) == 1 and "records" in sub.topics
    assert sub.take_records() == [found[0].wire()]


async def test_announce_caps_the_chat_and_the_live_payload_at_five(db):
    uid, ids = await seven_records(db)
    sub = live.hub.subscribe(uid)
    sent: list[str] = []

    async def send(text):
        sent.append(text)

    found = await announce(db, uid, ids, send)
    assert len(found) == 7  # all are found...
    assert len(sent) == 1 and len(sent[0].split("\n")) == 5  # ...five are shown
    assert [r["exercise"] for r in sub.take_records()] == [f"упражнение {i}" for i in range(5)]


async def test_announce_without_records_does_nothing(db):
    uid = await add_user(db)
    ids = await add_workout(db, uid, TODAY, [(BENCH, [(8, 80)])])  # the first time
    sub = live.hub.subscribe(uid)
    before = live.changes(uid)
    send = AsyncMock()
    assert await announce(db, uid, ids, send) == []
    send.assert_not_awaited()
    assert sub.take() == [] and live.changes(uid) == before


async def test_same_key_is_announced_once(db):
    uid = await add_user(db)
    await add_workout(db, uid, TODAY - timedelta(days=3), [(BENCH, [(6, 90)])])
    ids = await add_workout(db, uid, TODAY, [(BENCH, [(6, 92.5)])])
    send = AsyncMock()
    assert len(await announce(db, uid, ids, send, key="workout:1")) == 1
    assert await announce(db, uid, ids, send, key="workout:1") == []
    assert send.await_count == 1
    assert len(await announce(db, uid, ids, send, key="workout:2")) == 1  # another key
    assert len(await announce(db, uid, ids, send)) == 1  # no key: no guard
    assert len(await announce(db, uid, ids, send)) == 1
    assert send.await_count == 4


def test_claim_remembers_a_bounded_number_of_keys(monkeypatch):
    monkeypatch.setattr(records, "ANNOUNCED_MAX", 2)
    assert records.claim("a") and records.claim("b") and not records.claim("a")
    assert records.claim("c")  # evicts the oldest ("a")
    assert not records.claim("c") and not records.claim("b")
    assert records.claim("a")  # forgotten, so it is new again


async def test_a_failed_send_still_returns_and_publishes(db, caplog):
    uid = await add_user(db)
    await add_workout(db, uid, TODAY - timedelta(days=3), [(BENCH, [(6, 90)])])
    ids = await add_workout(db, uid, TODAY, [(BENCH, [(6, 92.5)])])
    sub = live.hub.subscribe(uid)
    send = AsyncMock(side_effect=RuntimeError("telegram is down"))
    found = await announce(db, uid, ids, send)
    assert len(found) == 1 and len(sub.take_records()) == 1


async def test_a_detection_failure_is_swallowed(db, monkeypatch):
    uid = await add_user(db)
    ids = await add_workout(db, uid, TODAY, [(BENCH, [(6, 90)])])

    async def boom(*a, **kw):
        raise RuntimeError("db is gone")

    monkeypatch.setattr(records, "find_new", boom)
    send = AsyncMock()
    assert await announce(db, uid, ids, send) == []
    send.assert_not_awaited()


async def test_announce_without_a_sender_only_publishes(db):
    uid = await add_user(db)
    await add_workout(db, uid, TODAY - timedelta(days=3), [(BENCH, [(6, 90)])])
    ids = await add_workout(db, uid, TODAY, [(BENCH, [(6, 92.5)])])
    sub = live.hub.subscribe(uid)
    assert len(await announce(db, uid, ids, None)) == 1
    assert len(sub.take_records()) == 1


# ---- 5. the live event ----


def payload(chunk: str) -> dict:
    return json.loads(chunk.split("data: ", 1)[1])


async def test_publish_records_event_carries_the_records():
    rs = [{"exercise": "жим лёжа", "weight": 92.5, "reps": 6, "kind": "e1rm", "text": "жим лёжа 92,5×6"}]
    it = live.events(7, live.hub, coalesce=0.01, ping=5)
    assert await anext(it) == "event: hello\ndata: {}\n\n"
    live.publish_records(7, rs)
    chunk = await anext(it)
    assert chunk.startswith("event: change\n")
    assert payload(chunk) == {"topics": ["records"], "records": rs}
    live.publish(7, "state")
    assert payload(await anext(it)) == {"topics": ["state"]}  # records are taken once, plain events stay plain
    await it.aclose()


async def test_records_and_other_topics_in_one_window_make_one_event():
    rs = [{"exercise": "x", "weight": 1, "reps": 1, "kind": "weight", "text": "x 1×1"}]
    it = live.events(7, live.hub, coalesce=0.05, ping=5)
    await anext(it)
    live.publish(7, "workouts", "state")
    live.publish_records(7, rs)
    assert payload(await anext(it)) == {"topics": ["records", "state", "workouts"], "records": rs}
    await it.aclose()


async def test_records_reach_only_their_user():
    sub1, sub2 = live.hub.subscribe(1), live.hub.subscribe(2)
    live.publish_records(1, [{"exercise": "x"}])
    assert sub1.topics == {"records"} and sub1.take_records() == [{"exercise": "x"}]
    assert sub2.topics == set() and sub2.take_records() == []


def test_publish_records_ignores_nobody_and_nothing():
    sub = live.hub.subscribe(1)
    before = live.changes(1)  # a process-wide counter, not reset between tests
    live.publish_records(None, [{"exercise": "x"}])
    live.publish_records(1, [])
    assert sub.topics == set() and live.changes(1) == before
    live.publish_records(1, [{"exercise": "x"}])
    assert live.changes(1) == before + 1  # the answer cache sees a change


def test_one_event_carries_at_most_five_records():
    sub = live.hub.subscribe(1)
    live.publish_records(1, [{"n": i} for i in range(8)])
    assert [r["n"] for r in sub.take_records()] == [0, 1, 2, 3, 4]
    assert sub.take_records() == []  # taken once


def test_records_pile_up_between_takes_keeping_the_newest_five():
    sub = live.hub.subscribe(1)
    live.publish_records(1, [{"n": i} for i in range(4)])
    live.publish_records(1, [{"n": i} for i in range(10, 13)])
    assert [r["n"] for r in sub.take_records()] == [2, 3, 10, 11, 12]
    assert sub.take_records() == []


def test_publish_records_never_raises(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("x")

    monkeypatch.setattr(live.hub, "publish", boom)
    live.publish_records(1, [{"exercise": "x"}])


def test_records_is_a_known_topic():
    assert "records" in live.TOPICS
    sub = live.hub.subscribe(1)
    live.hub.publish(1, ["records"])
    assert sub.take() == ["records"]


# ---- 6. workout_events.after_save ----


async def test_after_save_announces_then_offers_the_deload(db, monkeypatch):
    uid = await add_user(db)
    await add_workout(db, uid, TODAY - timedelta(days=3), [(BENCH, [(6, 90)])])
    ids = await add_workout(db, uid, TODAY, [(BENCH, [(6, 92.5)])])
    sent: list[tuple[str, object]] = []

    async def send(text, kb):
        sent.append((text, kb))

    async def maybe_offer(session, user_id, now, tz, programs_dir=None):
        assert (user_id, now, tz) == (uid, NOW, MSK)
        return "OFFER"

    monkeypatch.setattr(deload, "maybe_offer", maybe_offer)
    found = await workout_events.after_save(db, uid, ids, send, MSK, now=NOW)
    assert len(found) == 1
    assert [t for t, _ in sent] == ["🏆 Новый рекорд: жим лёжа 92,5×6 — 1ПМ 108 → 111 кг", "OFFER"]
    assert sent[0][1] is None  # the record line has no keyboard
    assert [b.callback_data for b in sent[1][1].inline_keyboard[0]] == ["deload:yes", "deload:later", "deload:no"]


async def test_after_save_without_the_deload_check_or_a_sender(db, monkeypatch):
    uid = await add_user(db)
    await add_workout(db, uid, TODAY - timedelta(days=3), [(BENCH, [(6, 90)])])
    ids = await add_workout(db, uid, TODAY, [(BENCH, [(6, 92.5)])])
    asked = []

    async def maybe_offer(*a, **kw):
        asked.append(a)
        return "OFFER"

    monkeypatch.setattr(deload, "maybe_offer", maybe_offer)
    send = AsyncMock()
    await workout_events.after_save(db, uid, ids, send, MSK, check_deload=False, now=NOW)
    assert asked == [] and send.await_count == 1
    sub = live.hub.subscribe(uid)
    assert len(await workout_events.after_save(db, uid, ids, None, MSK, now=NOW)) == 1  # no sender: live only
    assert asked == [] and len(sub.take_records()) == 1


async def test_after_save_never_raises_and_the_offer_still_comes(db, monkeypatch):
    uid = await add_user(db)
    sent = []

    async def send(text, kb):
        sent.append(text)

    async def broken(*a, **kw):
        raise RuntimeError("records are broken")

    async def maybe_offer(*a, **kw):
        return "OFFER"

    monkeypatch.setattr(records, "announce", broken)
    monkeypatch.setattr(deload, "maybe_offer", maybe_offer)
    assert await workout_events.after_save(db, uid, [1], send, MSK, now=NOW) == []
    assert sent == ["OFFER"]


async def test_a_failing_offer_does_not_raise(db, monkeypatch):
    uid = await add_user(db)

    async def broken(*a, **kw):
        raise RuntimeError("deload is broken")

    monkeypatch.setattr(deload, "maybe_offer", broken)
    send = AsyncMock()
    assert await workout_events.after_save(db, uid, [], send, MSK, now=NOW) == []
    send.assert_not_awaited()

    async def failing_send(text, kb):
        raise RuntimeError("telegram is down")

    async def maybe_offer(*a, **kw):
        return "OFFER"

    monkeypatch.setattr(deload, "maybe_offer", maybe_offer)
    assert await workout_events.offer_deload(db, uid, failing_send, MSK, NOW) is False


async def test_after_save_uses_the_clock_only_without_now(db, monkeypatch):
    uid = await add_user(db)
    seen = []

    async def maybe_offer(session, user_id, now, tz, programs_dir=None):
        seen.append(now)

    monkeypatch.setattr(deload, "maybe_offer", maybe_offer)
    send = AsyncMock()
    await workout_events.after_save(db, uid, [], send, MSK)  # FrozenDatetime pins datetime.now here
    await workout_events.after_save(db, uid, [], send, MSK, now=NOW + timedelta(days=1))
    assert seen == [NOW, NOW + timedelta(days=1)]


# ---- 7. chat save ----


async def test_chat_save_announces_a_record(llm, settings, db, offers):
    uid = await add_user(db)
    await add_workout(db, uid, date(2026, 10, 1), [(GYM, [(8, 80)])])
    llm.answers = [workout_answer((GYM, [(8, 85)]))]
    cb = await say_and_save("жим 85 на 8", llm, settings, db)
    assert sent_texts(cb) == ["🏆 Новый рекорд: жим штанги лёжа 85×8 — 1ПМ 101 → 108 кг"]
    assert len(offers) == 1 and offers[0][0] == uid  # then the deload offer is considered


async def test_chat_save_without_a_record_says_nothing_more(llm, settings, db, offers):
    uid = await add_user(db)
    await add_workout(db, uid, date(2026, 10, 1), [(GYM, [(8, 85)])])
    llm.answers = [workout_answer((GYM, [(8, 80)]))]
    cb = await say_and_save("жим 80 на 8", llm, settings, db)
    assert cb.message.answer.await_count == 0
    assert len(offers) == 1  # a saved workout still checks the deload offer


async def test_first_workout_in_the_chat_is_not_a_record(llm, settings, db, offers):
    llm.answers = [workout_answer((GYM, [(8, 80)]))]
    cb = await say_and_save("жим 80 на 8", llm, settings, db)
    assert cb.message.answer.await_count == 0


async def test_each_chat_save_announces_only_its_own_sets(llm, settings, db, offers):
    uid = await add_user(db)
    await add_workout(db, uid, date(2026, 10, 1), [(GYM, [(8, 80)])])
    llm.answers = [workout_answer((GYM, [(8, 85)])), workout_answer((GYM, [(8, 87.5)])), workout_answer((GYM, [(8, 85)]))]
    first = await say_and_save("жим 85 на 8", llm, settings, db)
    second = await say_and_save("жим 87,5 на 8", llm, settings, db, T0 + timedelta(minutes=3))
    third = await say_and_save("ещё жим 85 на 8", llm, settings, db, T0 + timedelta(minutes=6))
    assert len(sent_texts(first)) == 1 and "85×8" in sent_texts(first)[0]
    assert len(sent_texts(second)) == 1 and "87,5×8" in sent_texts(second)[0]
    assert "85×8" not in sent_texts(second)[0].replace("87,5×8", "")  # the first set is not announced again
    assert third.message.answer.await_count == 0  # same chat workout, no new best


async def test_save_returns_the_note_the_user_and_only_the_new_set_ids(llm, settings, db, offers):
    uid = await add_user(db)
    llm.answers = [workout_answer((GYM, [(8, 80)])), workout_answer((GYM, [(8, 85), (6, 85)]))]
    await say_and_save("жим 80 на 8", llm, settings, db)
    msg = message("жим 85 на 8 и 6", T0 + timedelta(minutes=3))
    await log_text.log_free_text(msg, settings, db, llm.client)
    pending = log_text.PENDING[token_of(msg)]
    today = pending.sent_at.astimezone(MSK).date()
    note, user_id, new_sets = await log_text._save(pending, callback("x"), today, db)
    assert note.startswith("Сохранено") and user_id == uid
    async with db() as s:
        chat_sets = list(await s.scalars(select(WorkoutSet).order_by(WorkoutSet.set_index)))
    assert [x.id for x in chat_sets][-2:] == new_sets and len(chat_sets) == 3


async def test_non_workout_saves_return_no_set_ids_and_no_extra_messages(llm, settings, db, offers):
    from test_log_text import food

    llm.answers = [food(2)]
    cb = await say_and_save("две самсы", llm, settings, db)
    assert cb.message.answer.await_count == 0 and offers == []
    llm.answers = [food(1)]
    msg = message("самса", T0 + timedelta(minutes=2))
    await log_text.log_free_text(msg, settings, db, llm.client)
    pending = log_text.PENDING[token_of(msg)]
    note, user_id, new_sets = await log_text._save(pending, callback("x"), TODAY, db)
    assert new_sets == [] and isinstance(user_id, int) and note == "Еда сохранена ✅"


# ---- 8. POST /api/workouts ----


class FakeBot:
    def __init__(self, fail: Exception | None = None):
        self.messages: list[tuple[int, str, object]] = []
        self.fail = fail

    async def send_message(self, chat_id, text, reply_markup=None):
        if self.fail is not None:
            raise self.fail
        self.messages.append((chat_id, text, reply_markup))


def api_workout(wid: str, day: str, weight: float, reps: int = 8, name: str = "жим лёжа") -> dict:
    return {
        "id": wid, "programId": "x", "week": 1, "weekday": 1,
        "startedAt": f"{day}T09:00:00Z", "finishedAt": f"{day}T10:00:00Z",
        "exercises": [{"name": name, "target": "", "dropset": False,
                       "sets": [{"weight": weight, "reps": reps, "done": True}]}],
    }


@pytest.fixture
def api(settings, db):
    clients: list[httpx.AsyncClient] = []

    def make(bot=None) -> httpx.AsyncClient:
        c = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(settings, db, bot=bot)), base_url="http://t"
        )
        clients.append(c)
        return c

    return make


async def post(client, body):
    from conftest import init_data

    r = await client.post("/api/workouts", json=body, headers={"X-Telegram-Init-Data": init_data()})
    assert r.status_code == 200, r.text
    return r.json()


async def test_api_workout_messages_the_owner_about_a_record(api, offers):
    bot = FakeBot()
    async with api(bot) as client:
        await post(client, api_workout("w1", "2026-10-05", 80))
        assert bot.messages == []  # the first workout of the exercise: no record
        await post(client, api_workout("w2", "2026-10-06", 85))
    assert [(chat, text, kb) for chat, text, kb in bot.messages] == [
        (42, "🏆 Новый рекорд: жим лёжа 85×8 — 1ПМ 101 → 108 кг", None)
    ]
    assert [c[1] for c in offers] == [NOW, NOW]  # the deload offer was considered once per new workout


async def test_api_retry_of_the_same_client_id_is_not_announced_again(api, offers, monkeypatch):
    keys: list[str] = []
    real = records.claim
    monkeypatch.setattr(records, "claim", lambda key: keys.append(key) or real(key))
    bot = FakeBot()
    async with api(bot) as client:
        await post(client, api_workout("w1", "2026-10-05", 80))
        await post(client, api_workout("w2", "2026-10-06", 85))
        await post(client, api_workout("w2", "2026-10-06", 85))  # the offline queue retried
        await post(client, api_workout("w2", "2026-10-06", 85))
    assert len(bot.messages) == 1
    assert keys == ["workout:1", "workout:2"]  # keyed by the saved workout id, only for new client ids
    assert len(offers) == 2


async def test_api_in_process_guard_stops_a_second_announcement_of_a_workout(api, offers):
    records.claim("workout:2")  # e.g. the same workout announced by another path of this process
    bot = FakeBot()
    async with api(bot) as client:
        await post(client, api_workout("w1", "2026-10-05", 80))
        await post(client, api_workout("w2", "2026-10-06", 85))
    assert bot.messages == []


async def test_api_without_a_bot_only_publishes_live(api, offers):
    sub = live.hub.subscribe(1)  # the first user of a fresh database
    async with api(None) as client:
        await post(client, api_workout("w1", "2026-10-05", 80))
        assert sub.take_records() == []
        await post(client, api_workout("w2", "2026-10-06", 85))
    (r,) = sub.take_records()
    assert (r["exercise"], r["kind"], r["weight"], r["reps"]) == ("жим лёжа", "e1rm", 85, 8)
    assert {"records", "workouts", "state"} <= sub.topics
    assert offers == []  # nothing to send the offer with


async def test_api_with_a_bot_also_publishes_live(api, offers):
    sub = live.hub.subscribe(1)
    async with api(FakeBot()) as client:
        await post(client, api_workout("w1", "2026-10-05", 80))
        await post(client, api_workout("w2", "2026-10-06", 85))
    assert len(sub.take_records()) == 1


async def test_api_sends_the_deload_offer_after_the_records(api, monkeypatch):
    async def maybe_offer(session, user_id, now, tz, programs_dir=None):
        assert now == NOW
        return "OFFER"

    monkeypatch.setattr(deload, "maybe_offer", maybe_offer)
    bot = FakeBot()
    async with api(bot) as client:
        await post(client, api_workout("w1", "2026-10-05", 80))
        bot.messages.clear()
        await post(client, api_workout("w2", "2026-10-06", 85))
    assert [(c, t) for c, t, _ in bot.messages] == [
        (42, "🏆 Новый рекорд: жим лёжа 85×8 — 1ПМ 101 → 108 кг"), (42, "OFFER"),
    ]
    assert bot.messages[1][2].inline_keyboard[0][0].callback_data == "deload:yes"


async def test_api_survives_a_broken_bot(api, offers):
    bot = FakeBot(fail=RuntimeError("telegram is down"))
    async with api(bot) as client:
        await post(client, api_workout("w1", "2026-10-05", 80))
        out = await post(client, api_workout("w2", "2026-10-06", 85))
    assert out["exercises"][0]["sets"][0]["weight"] == 85  # the save itself is fine


def test_the_workout_model_the_api_takes_is_unchanged():
    assert WorkoutIn.model_validate(api_workout("w", "2026-10-06", 80)).id == "w"


# ---- 9. saved edits ----


def test_raised_set_rules():
    def old(weight, reps, drop=0, ex=1):
        return WorkoutSet(exercise_id=ex, weight_kg=weight, reps=reps, drop_index=drop, set_index=0)

    def new(weight, reps):
        return ParsedSet(reps=reps, weight_kg=weight)

    assert se._raised(old(80, 8), new(85, 8), 1)  # heavier
    assert se._raised(old(80, 8), new(80, 9), 1)  # more reps
    assert se._raised(old(80, 8), new(85, 5), 1)  # heavier, fewer reps: the check decides later
    assert se._raised(old(80, 8), new(80, 8), 2)  # another exercise
    assert se._raised(old(80, 8, drop=1), new(80, 8), 1)  # a drop made a main set
    assert se._raised(old(None, 8), new(20, 8), 1)  # weight added to a bodyweight set
    assert not se._raised(old(80, 8), new(75, 8), 1)  # lighter
    assert not se._raised(old(80, 8), new(80, 8), 1)  # the same
    assert not se._raised(old(80, 8), new(80, 6), 1)  # fewer reps
    assert not se._raised(old(80, 8), new(75, 12), 1)  # more reps but lighter: not a raise of this set


async def edit(db, uid, text, after: ParseResult | None) -> list[int]:
    intent = se.detect(text, TODAY)
    async with db() as s:
        unit = (await se.find(s, uid, intent, T0, MSK)).units[0]
        raised = await se.apply(s, uid, unit, "edit" if after else "delete", after, text, T0, MSK)
        await s.commit()
    return raised


def swap(*sets) -> ParseResult:
    return ParseResult(kind="workout", exercises=[
        ParsedExercise(exercise=GYM, sets=[ParsedSet(reps=r, weight_kg=w) for r, w in sets])])


async def seeded_for_edit(db) -> int:
    uid = await add_user(db)
    await add_workout(db, uid, TODAY - timedelta(days=5), [(GYM, [(8, 80)])])
    await add_workout(db, uid, TODAY, [(GYM, [(8, 80), (8, 80)])], source="chat", raw="жим 2 по 8 на 80")
    return uid


async def test_apply_returns_the_ids_of_raised_sets(db):
    uid = await seeded_for_edit(db)
    raised = await edit(db, uid, "в жиме было 85, а не 80", swap((8, 85), (8, 80)))
    async with db() as s:
        sets = list(await s.scalars(select(WorkoutSet).where(WorkoutSet.raw_text.like("жим 2 по%")).order_by(WorkoutSet.set_index)))
    assert raised == [sets[0].id]  # only the first set got heavier


@pytest.mark.parametrize(
    ("after", "count"),
    [
        ([(8, 80), (8, 80)], 0),  # nothing changed
        ([(8, 75), (8, 75)], 0),  # lighter
        ([(6, 80), (6, 80)], 0),  # fewer reps
        ([(8, 80)], 0),  # a set removed
        ([(9, 80), (8, 80)], 1),  # one more rep
        ([(8, 82.5), (8, 85)], 2),  # both heavier
        ([(8, 80), (8, 80), (8, 80)], 1),  # a set added: new sets count as raised
        ([(8, 80), (8, 80), (8, 80), (8, 80)], 2),
    ],
)
async def test_apply_counts_raised_sets(db, after, count):
    uid = await seeded_for_edit(db)
    # the unit is found by the deterministic swap text; `after` is what the preview would have applied
    assert len(await edit(db, uid, "в жиме было 85, а не 80", swap(*after))) == count


async def test_apply_returns_nothing_for_deletes_and_other_kinds(db):
    uid = await seeded_for_edit(db)
    assert await edit(db, uid, "удали последний подход", None) == []


async def test_confirm_announces_a_record_from_a_corrected_set(llm, settings, db, offers):
    await seeded_for_edit(db)
    msg = message("в жиме было 85, а не 80", T0 + timedelta(minutes=1))
    await log_text.log_free_text(msg, settings, db, llm.client)
    kb = msg.answer.await_args.kwargs["reply_markup"]
    token = kb.inline_keyboard[0][0].callback_data.split(":", 1)[1]
    cb = callback(f"fixok:{token}")
    await hse.confirm(cb, settings, db)
    assert any(t.startswith("🏆 Новый рекорд: жим штанги лёжа 85×8") for t in sent_texts(cb))
    assert offers == []  # a fix is not a reason for the deload offer


async def test_confirm_without_a_raise_announces_nothing(llm, settings, db, offers):
    uid = await seeded_for_edit(db)
    msg = message("удали последний подход", T0 + timedelta(minutes=1))
    await log_text.log_free_text(msg, settings, db, llm.client)
    kb = msg.answer.await_args.kwargs["reply_markup"]
    cb = callback(f"fixok:{kb.inline_keyboard[0][0].callback_data.split(':', 1)[1]}")
    await hse.confirm(cb, settings, db)
    assert cb.message.answer.await_count == 0 and offers == []
    assert uid


# ---- 10. the diary answer ----


async def context(db, settings) -> str:
    async with db() as s:
        user = await s.scalar(select(User))
        return await answer.build_context(s, user, settings, None, MSK, NOW)


async def test_recent_replays_the_history_workout_by_workout(db):
    uid = await add_user(db)
    await add_workout(db, uid, TODAY - timedelta(days=20), [(BENCH, [(8, 80)])])  # the first time: no record
    await add_workout(db, uid, TODAY - timedelta(days=10), [(BENCH, [(8, 85)])])  # record
    await add_workout(db, uid, TODAY - timedelta(days=3), [(BENCH, [(8, 85)])])  # tie
    await add_workout(db, uid, TODAY - timedelta(days=1), [(BENCH, [(8, 90)])])  # record
    async with db() as s:
        found = await records.recent(s, uid, TODAY)
    assert [(d.day, d.record.weight) for d in found] == [(TODAY - timedelta(days=1), 90), (TODAY - timedelta(days=10), 85)]
    assert found[0].record.old == pytest.approx(85 * (1 + 8 / 30))  # compared with the whole history before it


async def test_recent_window_is_fourteen_days_including_today(db):
    uid = await add_user(db)
    await add_workout(db, uid, TODAY - timedelta(days=30), [(BENCH, [(8, 80)])])
    await add_workout(db, uid, TODAY - timedelta(days=14), [(BENCH, [(8, 85)])])  # a record, but 15 days of window
    await add_workout(db, uid, TODAY - timedelta(days=13), [(BENCH, [(8, 90)])])  # the first day inside
    await add_workout(db, uid, TODAY + timedelta(days=1), [(BENCH, [(8, 100)])])  # the future is not today's
    async with db() as s:
        found = await records.recent(s, uid, TODAY)
    assert [(d.day, d.record.weight, d.record.old) for d in found] == [
        (TODAY - timedelta(days=13), 90, pytest.approx(85 * (1 + 8 / 30)))
    ]


async def test_recent_ignores_drops_and_other_users(db):
    me, other = await add_user(db), await add_user(db, 7)
    await add_workout(db, me, TODAY - timedelta(days=5), [(BENCH, [(8, 80), (6, 120, 1)])])
    await add_workout(db, me, TODAY - timedelta(days=2), [(BENCH, [(8, 85), (6, 130, 1)])])
    await add_workout(db, other, TODAY - timedelta(days=5), [(BENCH, [(8, 200)])])
    async with db() as s:
        found = await records.recent(s, me, TODAY)
    assert [(d.record.weight, d.record.kind) for d in found] == [(85, "e1rm")]


def test_recent_text_shapes():
    assert records.recent_text([]) == "Новых рекордов за 14 дней нет."
    r = records.Record(BENCH, 92.5, 6, "e1rm", 108, 111)
    one = [records.Dated(date(2026, 10, 5), r)]
    assert records.recent_text(one) == "Рекорды за 14 дней: жим лёжа 92,5×6 (1ПМ 108 → 111 кг, 05.10)."
    seven = [records.Dated(date(2026, 10, 5), r)] * 7
    text = records.recent_text(seven)
    assert text.count("жим лёжа 92,5×6") == 5 and text.endswith(" и ещё 2.")


async def test_the_answer_context_has_the_records_line(db, settings):
    uid = await add_user(db)
    await add_workout(db, uid, TODAY - timedelta(days=8), [(BENCH, [(6, 90)])])
    await add_workout(db, uid, TODAY - timedelta(days=2), [(BENCH, [(6, 92.5)])])
    text = await context(db, settings)
    assert "Рекорды за 14 дней: жим лёжа 92,5×6 (1ПМ 108 → 111 кг, 04.10)." in text
    lines = text.split("\n")
    assert lines.index(next(x for x in lines if x.startswith("Рекорды за 14 дней:"))) < lines.index(
        next(x for x in lines if x.startswith("План на сегодня"))
    )


async def test_the_answer_context_says_when_there_are_no_records(db, settings):
    uid = await add_user(db)
    await add_workout(db, uid, TODAY - timedelta(days=2), [(BENCH, [(6, 90)])])
    assert "Новых рекордов за 14 дней нет." in await context(db, settings)
