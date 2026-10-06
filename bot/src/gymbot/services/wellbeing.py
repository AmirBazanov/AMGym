"""Wellbeing log: sleep, pains, energy and mood from chat messages.

Entries are written by the free-text handler after confirmation (handlers/log_text.py), listed and
deleted by the Mini App (api/app.py) and summarized for AI advice (services/advice.py).
`pains` is stored as a JSON list in a Text column; reading tolerates anything malformed.
Windows are local days in TIMEZONE converted to UTC bounds, like nutrition.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gymbot.db.models import User, WellbeingEntry
from gymbot.llm.schemas import ParsedWellbeing
from gymbot.services.nutrition import _aware, local_day_bounds

ADVICE_DAYS = 14
ADVICE_PAINS = 5  # most recent pain mentions shown to the model
NOTE_IN_CONTEXT = 150
SHORT_SLEEP = 7  # hours


def wellbeing_entry(user_id: int, w: ParsedWellbeing, raw_text: str, noted_at: datetime) -> WellbeingEntry:
    pains = [{"place": p.place, "severity": p.severity} for p in w.pains]
    return WellbeingEntry(
        user_id=user_id,
        noted_at=noted_at,
        sleep_hours=Decimal(str(w.sleep_hours)) if w.sleep_hours is not None else None,
        sleep_quality=w.sleep_quality,
        energy=w.energy,
        mood=w.mood,
        pains=json.dumps(pains, ensure_ascii=False) if pains else None,
        note=w.note,
        raw_text=raw_text,
    )


class PainOut(BaseModel):
    place: str
    severity: int | None


def parse_pains(value: str | None) -> list[PainOut]:
    """Stored JSON -> pains; anything malformed is skipped, never raised."""
    try:
        items = json.loads(value) if value else []
    except ValueError:
        return []
    out = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or not str(item.get("place") or "").strip():
            continue
        severity = item.get("severity")
        out.append(PainOut(place=str(item["place"]).strip(), severity=severity if isinstance(severity, int) else None))
    return out


# ---- Mini App wire format (mirrors miniapp/src/api.ts) ----


class WellbeingOut(BaseModel):
    id: int
    notedAt: datetime  # UTC
    date: date  # local day in TIMEZONE
    sleepHours: float | None
    sleepQuality: int | None
    energy: int | None
    mood: int | None
    pains: list[PainOut]
    note: str | None


def entry_out(e: WellbeingEntry, tz: ZoneInfo) -> WellbeingOut:
    noted = _aware(e.noted_at)
    return WellbeingOut(
        id=e.id,
        notedAt=noted,
        date=noted.astimezone(tz).date(),
        sleepHours=float(e.sleep_hours) if e.sleep_hours is not None else None,
        sleepQuality=e.sleep_quality,
        energy=e.energy,
        mood=e.mood,
        pains=parse_pains(e.pains),
        note=e.note,
    )


async def recent_entries(session: AsyncSession, user: User, today: date, days: int, tz: ZoneInfo) -> list[WellbeingEntry]:
    """Entries of the last `days` local days including `today`, newest first."""
    start, _ = local_day_bounds(today - timedelta(days=days - 1), tz)
    _, end = local_day_bounds(today, tz)
    rows = await session.scalars(
        select(WellbeingEntry)
        .where(WellbeingEntry.user_id == user.id, WellbeingEntry.noted_at >= start, WellbeingEntry.noted_at < end)
        .order_by(WellbeingEntry.noted_at.desc(), WellbeingEntry.id.desc())
    )
    return list(rows)


# ---- summary for AI advice ----


def _n(x: float) -> str:
    return str(round(x)) if x == round(x) else f"{x:.1f}"


def _pains_line(entries: list[WellbeingEntry], tz: ZoneInfo) -> str | None:
    """'Боли: левое плечо 06.10 (4/5), 05.10; колено 03.10.' from the ADVICE_PAINS latest mentions."""
    mentions = [
        (_aware(e.noted_at).astimezone(tz).date(), p) for e in entries for p in parse_pains(e.pains)
    ][:ADVICE_PAINS]
    groups: dict[str, tuple[str, list[str]]] = {}  # normalized place -> (shown name, dates), first seen first
    for day, p in mentions:
        key = " ".join(p.place.casefold().split())
        _, dates = groups.setdefault(key, (p.place, []))
        dates.append(f"{day:%d.%m}" + (f" ({p.severity}/5)" if p.severity else ""))
    if not groups:
        return None
    return "Боли: " + "; ".join(f"{name} {', '.join(dates)}" for name, dates in groups.values()) + "."


async def context_lines(session: AsyncSession, user: User, today: date, tz: ZoneInfo) -> list[str]:
    entries = await recent_entries(session, user, today, ADVICE_DAYS, tz)
    if not entries:
        return [f"Самочувствие за {ADVICE_DAYS} дней: записей нет."]
    line = f"Самочувствие за {ADVICE_DAYS} дней: записей {len(entries)}"
    sleep = [float(e.sleep_hours) for e in entries if e.sleep_hours is not None]
    if sleep:
        short = sum(1 for h in sleep if h < SHORT_SLEEP)
        line += (
            f"; сон в среднем {_n(sum(sleep) / len(sleep))} ч (записей о сне {len(sleep)}), "
            f"ночей меньше {SHORT_SLEEP} ч: {short}"
        )
    energy = [e.energy for e in entries if e.energy is not None]
    if energy:
        line += f"; энергия в среднем {_n(sum(energy) / len(energy))}/5"
    lines = [line + "."]
    if pains := _pains_line(entries, tz):
        lines.append(pains)
    noted = next((e for e in entries if e.note), None)
    if noted is not None:
        note = " ".join((noted.note or "").split())
        if len(note) > NOTE_IN_CONTEXT:
            note = note[: NOTE_IN_CONTEXT - 1] + "…"
        lines.append(f"Последняя заметка {_aware(noted.noted_at).astimezone(tz):%d.%m}: {note}")
    return lines
