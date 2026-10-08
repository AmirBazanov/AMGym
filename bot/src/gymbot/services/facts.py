"""Facts about the user: lasting preferences, allergies, portion sizes, schedule, limits.

Shared by the chat (handlers/log_text.py: "запомни: ..." and the model's `remember`, handlers/facts.py:
/facts) and the Mini App API. Active facts go to the parser prompt and to the advice summary.

Rules: text is cleaned like the Mini App does (trimmed, whitespace runs collapsed) and is 1..TEXT_MAX
characters; an active fact is never duplicated (compared by `normalize`); at most MAX_ACTIVE are active.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from gymbot.db.models import User, UserFact
from gymbot.llm.schemas import REMEMBER_MAX

Category = Literal["food", "training", "health", "schedule", "other"]
CATEGORIES: tuple[str, ...] = ("food", "training", "health", "schedule", "other")
TEXT_MAX = REMEMBER_MAX  # 200; the parser drops longer `remember` values
MAX_ACTIVE = 50


def clean(text: str) -> str:
    return " ".join(text.split())


_EDGES = " .,!?…;:—–-\"'«»„“”"


def normalize(text: str) -> str:
    """Comparison key for duplicates: case, ё/е, surrounding quotes and punctuation are ignored."""
    return clean(text).casefold().replace("ё", "е").strip(_EDGES)


def _words(*stems: str) -> re.Pattern[str]:
    return re.compile(r"(?<!\w)(?:" + "|".join(stems) + ")", re.IGNORECASE)


# Cheap keyword heuristic, checked in this order (an allergy to nuts is health, not food). Stems match
# from a word start; entries ending in (?!\w) are whole words.
_CATEGORY_WORDS: list[tuple[str, re.Pattern[str]]] = [
    ("health", _words("аллерги", "болит", "боль", "травм", "колен", "спин", "плеч", "поясниц", "шея", "шеи",
                      "давлени", "операци", "грыж", "астм", "диабет", "сердц", "врач", "лекарств", "таблет")),
    ("food", _words(r"ем(?!\w)", r"ест(?!\w)", r"ешь(?!\w)", "еда", "еду", "пищ", "порци", "грамм", r"\d+\s*г(?!\w)",
                    "самс", "творог", "молок", "молоч", "мяс", "рыб", "кофе", "чай", "сахар", "сладк", "ккал",
                    "белок", "белка", "перекус", "завтрак", "обед", "ужин", "плов", "лактоз", "глютен",
                    "вегетариан", "веган", "алкогол", "пью", "пить", "кбжу", "калори")),
    ("training", _words("трен", "зал", "жим", "присед", "станов", "тяг", "упражнен", "подход", "повтор",
                        "кардио", "бег", "гантел", "штанг", "турник", "подтяг", "разминк", "растяжк", "фитнес")),
    ("schedule", _words("утр", "вечер", "ночь", "ночн", "понедельник", "вторник", "сред", "четверг", "пятниц",
                        "суббот", "воскресень", "выходн", "будн", "смен", "работ", "расписани", r"\d{1,2}[:.]\d{2}")),
]


def guess_category(text: str) -> str:
    for category, pattern in _CATEGORY_WORDS:
        if pattern.search(text):
            return category
    return "other"


async def active_facts(session: AsyncSession, user_id: int) -> list[UserFact]:
    """Newest first."""
    rows = await session.scalars(
        select(UserFact)
        .where(UserFact.user_id == user_id, UserFact.active.is_(True))
        .order_by(UserFact.created_at.desc(), UserFact.id.desc())
    )
    return list(rows)


async def prompt_facts(session: AsyncSession, telegram_id: int) -> list[str]:
    """Active fact texts of a Telegram user for the LLM, newest first (empty for an unknown user)."""
    rows = await session.scalars(
        select(UserFact.text)
        .join(User, User.id == UserFact.user_id)
        .where(User.telegram_id == telegram_id, UserFact.active.is_(True))
        .order_by(UserFact.created_at.desc(), UserFact.id.desc())
    )
    return list(rows)


async def find_duplicate(
    session: AsyncSession, user_id: int, text: str, exclude_id: int | None = None
) -> UserFact | None:
    key = normalize(text)
    return next((f for f in await active_facts(session, user_id) if f.id != exclude_id and normalize(f.text) == key), None)


async def count_active(session: AsyncSession, user_id: int) -> int:
    count = await session.scalar(
        select(func.count()).select_from(UserFact).where(UserFact.user_id == user_id, UserFact.active.is_(True))
    )
    return count or 0


def checked_text(text: str) -> str:
    """Cleaned text, or ValueError if it is empty or longer than TEXT_MAX."""
    text = clean(text)
    if not text:
        raise ValueError("fact text is empty")
    if len(text) > TEXT_MAX:
        raise ValueError(f"fact text is longer than {TEXT_MAX}")
    return text


@dataclass
class AddResult:
    status: Literal["created", "duplicate", "limit"]
    fact: UserFact | None  # the new fact, the existing duplicate, or None at the limit


async def add_fact(
    session: AsyncSession,
    user_id: int,
    text: str,
    category: str | None = None,
    source_text: str | None = None,
) -> AddResult:
    """Add an active fact unless an equal one is active or MAX_ACTIVE are. Flushes, does not commit."""
    text = checked_text(text)
    if (dup := await find_duplicate(session, user_id, text)) is not None:
        return AddResult("duplicate", dup)
    if await count_active(session, user_id) >= MAX_ACTIVE:
        return AddResult("limit", None)
    fact = UserFact(
        user_id=user_id,
        text=text,
        category=category or guess_category(text),
        active=True,
        source_text=source_text,
        created_at=datetime.now(UTC),
    )
    session.add(fact)
    await session.flush()
    return AddResult("created", fact)


# ---- Mini App wire format (mirrors miniapp/src/api.ts) ----


class FactOut(BaseModel):
    id: int
    text: str
    category: str
    createdAt: datetime  # UTC
    active: bool


def fact_out(f: UserFact) -> FactOut:
    created = f.created_at if f.created_at.tzinfo else f.created_at.replace(tzinfo=UTC)  # SQLite drops the offset
    return FactOut(id=f.id, text=f.text, category=f.category, createdAt=created.astimezone(UTC), active=f.active)


# ---- Offers to remember: model-derived numbers are not facts about the user ----

_DERIVED_WORDS = re.compile(
    r"(?<!\w)(?:1\s?пм|пм|1\s?rm|rm)(?!\w)|e1rm|эпли|≈|по расчет|расчетн|оценочн|примерный максимум"
)
_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")
# A lift or a working weight: kg, the bar, sets and reps. Food portions («самса ~150 г») are not: the parser
# is asked to turn "они у нас большие" into a portion, and the user confirms it with the button.
_LIFT = re.compile(r"(?<!\w)кг(?!\w)|килограм|штанг|гантел|гриф|смит|блок|тренаж|жим|присед|тяг|сгибан|разгибан|"
                   r"подход|повтор|рабоч|макс")


def _numbers(text: str) -> set[float]:
    return {float(n.replace(",", ".")) for n in _NUMBER.findall(text)}


def derived_offer(offer: str, said: str) -> bool:
    """True when the offered fact («Запомнить? «…»») is a number the model computed, not the user's own words.

    Incident: the diary answer offered «Запомнить: 1ПМ … на штанге ~50 кг», its own estimate, as if the
    user had said it. Such an offer must not be shown. Compared lower-cased, ё = е.

    - Always derived: 1ПМ / 1RM / e1RM, «Эпли», «≈», «по расчёту», «расчётн…», «оценочн…», «примерный максимум».
    - With «~» or «%» about a lift or a weight in kg (`_LIFT`): derived only if some number of the offer
      (12,5 == 12.5) is absent from `said`, the user's own message («жим ~80 кг» after «жму примерно 80»
      is the user's number). Food portions («самса ~150 г», «творог 5 %») are never derived here.
    - Anything else is not derived.
    """
    text = " ".join(offer.casefold().replace("ё", "е").split())
    if _DERIVED_WORDS.search(text):
        return True
    if ("~" in text or "%" in text) and _LIFT.search(text):
        return not _numbers(text) <= _numbers(said)
    return False
