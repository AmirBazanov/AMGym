"""Tasks and house chores of the dashboard «Дела Амира», changed from the chat (handlers/hub.py).

The dashboard (AventraLLC/amirs-hub, app.js) keeps them as:
- static tasks and chores in data.js (`todos: [{id, text, tag}]`, `chores: [{id, text, every}]`), read here from
  the clone in HUB_DIR;
- its state (gymbot.services.hub, GET/PUT /api/hub/state): `done` {id: bool}, `custom` [{id, text}] (tasks added on
  the site or here), `removed` [id] (deleted static tasks), `chores` {id: "ГГГГ-ММ-ДД"} (last time done).

Open tasks, delete and reopen mirror app.js todos() and its click handlers exactly, so the site shows the same.
Actions are pure functions of the state: applying them again on a newer state (after a 409) is the merge.
"""

from __future__ import annotations

import copy
import json
import logging
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

TEXT_MAX = 200
KINDS = ("add_task", "complete_task", "reopen_task", "remove_task", "chore_done")


# ---- data.js ----


def _js_to_json(src: str) -> str:
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


def _array(src: str, key: str) -> list[Any]:
    """The `key: [...]` array of data.js as Python; [] when missing."""
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
                return json.loads(_js_to_json(src[start : i + 1]))
        i += 1
    raise ValueError(f"unclosed {key} array")


@dataclass(frozen=True)
class SiteLists:
    todos: list[dict[str, Any]]  # [{id, text, tag?}]
    chores: list[dict[str, Any]]  # [{id, text, every}]


def read_site(hub_dir: Path | None) -> SiteLists:
    """Static tasks and chores from HUB_DIR/data.js; empty lists (logged) when it is missing or unreadable."""
    if hub_dir is None or not (hub_dir / "data.js").is_file():
        return SiteLists([], [])
    try:
        src = (hub_dir / "data.js").read_text(encoding="utf-8")
        todos = [t for t in _array(src, "todos") if isinstance(t, dict) and t.get("id") and t.get("text")]
        chores = [c for c in _array(src, "chores") if isinstance(c, dict) and c.get("id") and c.get("text")]
    except (ValueError, OSError) as e:
        log.warning("hub data.js unreadable: %s", e)
        return SiteLists([], [])
    return SiteLists(todos, chores)


# ---- the state as app.js sees it ----


def _done(state: dict[str, Any]) -> dict[str, Any]:
    return state["done"] if isinstance(state.get("done"), dict) else {}


def _custom(state: dict[str, Any]) -> list[dict[str, Any]]:
    custom = state.get("custom")
    return [t for t in custom if isinstance(t, dict) and t.get("id")] if isinstance(custom, list) else []


def _removed(state: dict[str, Any]) -> list[Any]:
    return state["removed"] if isinstance(state.get("removed"), list) else []


def _chores(state: dict[str, Any]) -> dict[str, Any]:
    return state["chores"] if isinstance(state.get("chores"), dict) else {}


def tasks(state: dict[str, Any], site: SiteLists) -> list[dict[str, Any]]:
    """app.js todos().list with `done`: static tasks not removed, then the custom ones."""
    removed = set(_removed(state))
    done = _done(state)
    listed = [t for t in site.todos if t["id"] not in removed] + _custom(state)
    return [{"id": t["id"], "text": str(t.get("text", "")), "done": bool(done.get(t["id"]))} for t in listed]


def chores(state: dict[str, Any], site: SiteLists, today: date) -> list[dict[str, Any]]:
    """Chores with the last day done and the days left (negative: overdue), as app.js chores()."""
    last = _chores(state)
    out = []
    for c in site.chores:
        when = last.get(c["id"])
        left = 0
        if isinstance(when, str):
            try:
                left = int(c.get("every") or 0) - (today - date.fromisoformat(when)).days
            except ValueError:
                when = None
        out.append({"id": c["id"], "text": str(c["text"]), "every": c.get("every"), "last": when, "left": left})
    return out


def prompt_context(state: dict[str, Any], site: SiteLists, today: date) -> str:
    """The lists the model picks ids from."""
    lines = [f"Сегодня: {today.isoformat()}", "", "Задачи (id: текст):"]
    items = tasks(state, site)
    lines += [f"- {t['id']}: {t['text']}{' [сделано]' if t['done'] else ''}" for t in items] or ["- (нет)"]
    lines += ["", "Дела по дому (id: текст, раз в N дней, последний раз):"]
    lines += [
        f"- {c['id']}: {c['text']}, раз в {c['every']} дн., {c['last'] or 'не отмечалось'}"
        for c in chores(state, site, today)
    ] or ["- (нет)"]
    return "\n".join(lines)


# ---- actions ----


@dataclass(frozen=True)
class Action:
    kind: str  # one of KINDS
    id: str | None = None  # task or chore id; for add_task the new id, minted when staged
    text: str | None = None  # add_task
    day: date | None = None  # chore_done


def parse_actions(
    data: Any, state: dict[str, Any], site: SiteLists, today: date, new_id: Any
) -> tuple[list[Action], list[str]]:
    """Valid actions from the model's {"actions": [...]} and notes about the rejected ones. `new_id()` mints
    task ids at stage time, so a second «Применить» cannot add the task twice."""
    raw = data.get("actions") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return [], []
    task_ids = {t["id"] for t in tasks(state, site)}
    chore_ids = {c["id"] for c in site.chores}
    actions: list[Action] = []
    notes: list[str] = []
    for a in raw:
        if not isinstance(a, dict) or a.get("type") not in KINDS:
            continue
        kind = a["type"]
        if kind == "add_task":
            text = " ".join(str(a.get("text") or "").split())[:TEXT_MAX]
            if text:
                actions.append(Action(kind, id=new_id(), text=text[:1].upper() + text[1:]))
        elif kind == "chore_done":
            if a.get("id") not in chore_ids:
                notes.append("Не нашёл такое дело по дому.")
                continue
            day = today
            if isinstance(a.get("date"), str):
                try:
                    day = min(date.fromisoformat(a["date"]), today)
                except ValueError:
                    pass
            actions.append(Action(kind, id=a["id"], day=day))
        elif a.get("id") in task_ids:
            actions.append(Action(kind, id=a["id"]))
        else:
            notes.append("Не нашёл такую задачу.")
    return actions, sorted(set(notes))


def apply(state: dict[str, Any], actions: list[Action], site: SiteLists) -> dict[str, Any]:
    """A new state with `actions` applied as app.js would; actions on tasks gone meanwhile are skipped."""
    out = copy.deepcopy(state) if isinstance(state, dict) else {}
    static = {t["id"] for t in site.todos}
    for a in actions:
        if a.kind == "add_task":
            custom = _custom(out)
            if not any(t["id"] == a.id for t in custom):
                out["custom"] = [*custom, {"id": a.id, "text": a.text}]
        elif a.kind in ("complete_task", "reopen_task"):
            out["done"] = {**_done(out), a.id: a.kind == "complete_task"}
        elif a.kind == "remove_task":
            custom = _custom(out)
            if any(t["id"] == a.id for t in custom):
                out["custom"] = [t for t in custom if t["id"] != a.id]
            elif a.id in static and a.id not in _removed(out):
                out["removed"] = [*_removed(out), a.id]
        elif a.kind == "chore_done" and a.day is not None:
            out["chores"] = {**_chores(out), a.id: a.day.isoformat()}
    return out


def describe(actions: list[Action], state: dict[str, Any], site: SiteLists, today: date) -> list[str]:
    """Preview lines: «Добавлю задачу «…»», «Отмечу «Уборка» сделанной сегодня»."""
    texts = {t["id"]: t["text"] for t in tasks(state, site)}
    names = {c["id"]: c["text"] for c in site.chores}
    lines = []
    for a in actions:
        if a.kind == "add_task":
            lines.append(f"Добавлю задачу «{a.text}»")
        elif a.kind == "complete_task":
            lines.append(f"Закрою задачу «{texts.get(a.id, a.id)}»")
        elif a.kind == "reopen_task":
            lines.append(f"Верну в работу «{texts.get(a.id, a.id)}»")
        elif a.kind == "remove_task":
            lines.append(f"Удалю задачу «{texts.get(a.id, a.id)}»")
        elif a.kind == "chore_done" and a.day is not None:
            when = "сегодня" if a.day == today else a.day.strftime("%d.%m")
            lines.append(f"Отмечу «{names.get(a.id, a.id)}» — сделано {when}")
    return lines


# ---- routing ----

_COMMAND = re.compile(
    r"задач|туду|todo|\bв\s+дела\b|список\s+дел|дела\s+по\s+дому|"
    r"убрал(?:ся|ась)\b|уборк|постирал|стирк|вынес(?:ла)?\s+мусор|мусор\s+вынес|"
    r"закупил(?:ся|ась)?\s+продукт|купил\s+продукты|продукты\s+купил|коммунал",
    re.IGNORECASE,
)


def is_hub_command(text: str) -> bool:
    """Words that may be about the dashboard's tasks or chores; the model decides (no action = not ours)."""
    return bool(_COMMAND.search(text))
