"""The old dashboard «Дела Амира» (AventraLLC/amirs-hub) as rows of the «Дела» tables, for the one-time import
(gymbot.services.life.import_legacy, which writes them).

The dashboard kept its data in two places:
- data.js in the clone in HUB_DIR (`window.DATA = {...}`, a JS object literal): `lessons` (my weekly lessons),
  `study.classes` and `study.deadlines` (university; items with `example: true` are placeholders), `projects`,
  `todos` (static tasks) and `chores`;
- its browser state (gymbot.services.hub, hub_state): `done` {id: bool}, `custom` [{id, text}] (tasks added on
  the site or from the chat), `removed` [id] (deleted static tasks), `chores` {id: "ГГГГ-ММ-ДД"} (last done).

Pure functions: no database here. Every row gets a stable `external_id` (data.js ids where there are any, else
day/time/title), so a repeated import can skip what it made before.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

TITLE_MAX = 200
EXTERNAL_ID_MAX = 128
_TIME = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


# ---- data.js ----


def js_to_json(src: str) -> str:
    """A JS object literal as JSON: strings in either quote, comments, unquoted keys, trailing commas."""
    out: list[str] = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c in "\"'":
            j, buf = i + 1, []
            while j < n and src[j] != c:
                if src[j] == "\\" and j + 1 < n:
                    buf.append(src[j : j + 2])
                    j += 2
                    continue
                buf.append(src[j])
                j += 1
            body = "".join(buf)
            if c == "'":
                body = body.replace("\\'", "'").replace('"', '\\"')
            out.append('"' + body + '"')
            i = j + 1
        elif src.startswith("//", i):
            i = src.find("\n", i) if "\n" in src[i:] else n
        elif src.startswith("/*", i):
            end = src.find("*/", i + 2)
            i = n if end < 0 else end + 2
        elif c.isalpha() or c in "_$":
            m = re.match(r"[A-Za-z_$][\w$]*", src[i:])
            assert m is not None
            word = m.group(0)
            rest = src[i + len(word) :].lstrip()
            out.append(f'"{word}"' if rest.startswith(":") else word)
            i += len(word)
        else:
            out.append(c)
            i += 1
    return re.sub(r",(\s*[\]}])", r"\1", "".join(out))


def js_array(src: str, key: str) -> list[Any]:
    """The first `key: [...]` array of data.js as Python; [] when missing; ValueError when unclosed or broken."""
    m = re.search(rf"\b{key}\s*:\s*\[", src)
    if m is None:
        return []
    start = m.end() - 1
    depth, i, quote = 0, start, ""
    while i < len(src):
        c = src[i]
        if quote:
            if c == "\\":
                i += 1
            elif c == quote:
                quote = ""
        elif c in "\"'":
            quote = c
        elif src.startswith("//", i):
            i = src.find("\n", i)
            if i < 0:
                break
        elif c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                return json.loads(js_to_json(src[start : i + 1]))
        i += 1
    raise ValueError(f"unclosed {key} array")


def read_data_js(hub_dir: Path | None) -> str | None:
    """HUB_DIR/data.js as text; None when HUB_DIR is off, not cloned or the file is unreadable."""
    if hub_dir is None or not (hub_dir / "data.js").is_file():
        return None
    try:
        return (hub_dir / "data.js").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        log.warning("hub data.js unreadable: %s", e)
        return None


# ---- rows ----


@dataclass(frozen=True)
class LegacyRow:
    kind: str  # one of gymbot.services.life.KINDS
    external_id: str
    fields: dict[str, Any] = field(default_factory=dict)  # ORM attribute -> value


def _text(value: Any, limit: int = TITLE_MAX) -> str | None:
    """Collapsed whitespace, cut to `limit`; None when empty or not text."""
    if not isinstance(value, str | int | float) or isinstance(value, bool):
        return None
    out = " ".join(str(value).split())[:limit]
    return out or None


def _time(value: Any) -> str | None:
    """"9:00" / "09:00" -> "09:00"; None when not a time."""
    m = _TIME.match(str(value).strip()) if isinstance(value, str) else None
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m else None


def _date(value: Any) -> date | None:
    try:
        return date.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _weekday(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 7 else None


def _ext(*parts: Any) -> str:
    return ":".join(str(p) for p in parts)[:EXTERNAL_ID_MAX]


def _items(src: str, key: str) -> list[dict[str, Any]]:
    return [x for x in js_array(src, key) if isinstance(x, dict) and not x.get("example")]


def _slot(item: dict[str, Any], area: str, prefix: str, place_key: str) -> LegacyRow | None:
    weekday, start, title = _weekday(item.get("day")), _time(item.get("time")), _text(item.get("title"))
    if weekday is None or start is None or title is None:
        return None
    fields = {
        "weekday": weekday, "start": start, "end": _time(item.get("end")), "title": title, "area": area,
        "tag": _text(item.get("tag"), 32) or ("study" if area == "study" else None),
        "place": _text(item.get(place_key)), "vld": _time(item.get("vld")),
    }
    return LegacyRow("slot", _ext(prefix, weekday, start, title), fields)


# Project status words of data.js; kind follows from the status: «учёба» -> learning, «идея» -> own, else work.
_STATUS = {"в работе": "active", "учёба": "active", "идея": "idea", "пауза": "paused", "на паузе": "paused",
           "готово": "done", "сделано": "done"}
_KIND = {"учёба": "learning", "идея": "own"}


def _project(item: dict[str, Any]) -> LegacyRow | None:
    name = _text(item.get("name"))
    if name is None:
        return None
    status_word = (_text(item.get("status")) or "").lower()
    fields = {
        "name": name, "kind": _KIND.get(status_word, "work"), "status": _STATUS.get(status_word, "active"),
        "description": _text(item.get("desc"), 2000), "link": _text(item.get("link"), 500),
        "next_step": _text(item.get("next"), 500),
    }
    return LegacyRow("project", _ext("project", name), fields)


def legacy_rows(src: str, state: dict[str, Any], now: datetime) -> list[LegacyRow]:
    """Rows of data.js `src` with the dashboard's `state` applied (done, removed, own tasks, chore dates).
    Items without the fields a row needs are skipped; ValueError when data.js is broken."""
    state = state if isinstance(state, dict) else {}
    done = state.get("done") if isinstance(state.get("done"), dict) else {}
    removed = set(state["removed"]) if isinstance(state.get("removed"), list) else set()
    custom = state.get("custom") if isinstance(state.get("custom"), list) else []
    chore_days = state.get("chores") if isinstance(state.get("chores"), dict) else {}

    rows: list[LegacyRow | None] = []
    rows += [_slot(x, "work", "lesson", "note") for x in _items(src, "lessons")]
    rows += [_slot(x, "study", "class", "place") for x in _items(src, "classes")]
    for x in _items(src, "deadlines"):
        day, title = _date(x.get("date")), _text(x.get("title"))
        if day and title:
            rows.append(LegacyRow("deadline", _ext("deadline", day, title),
                                  {"day": day, "title": title, "course": _text(x.get("course"))}))
    rows += [_project(x) for x in _items(src, "projects")]

    tasks = [(f"todo:{t['id']}", t) for t in _items(src, "todos") if t.get("id") and t["id"] not in removed]
    tasks += [(f"custom:{t['id']}", t) for t in custom if isinstance(t, dict) and t.get("id")]
    position = 0
    for ext, t in tasks:
        title = _text(t.get("text"))
        if title is None:
            continue
        fields = {"title": title, "tag": _text(t.get("tag"), 32), "position": position,
                  "done_at": now if done.get(t["id"]) else None}
        rows.append(LegacyRow("task", _ext(ext), fields))
        position += 1

    for c in _items(src, "chores"):
        title, every = _text(c.get("text")), c.get("every")
        if not c.get("id") or title is None:
            continue
        every_days = every if isinstance(every, int) and not isinstance(every, bool) and 1 <= every <= 365 else 7
        fields = {"title": title, "every_days": every_days, "last_done": _date(chore_days.get(c["id"]))}
        rows.append(LegacyRow("chore", _ext("chore", c["id"]), fields))
    return [r for r in rows if r is not None]
