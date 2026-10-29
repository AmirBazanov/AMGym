"""Changes to «Дела» as actions: the fields each kind takes and their checks (`validate_fields`, shared by the
API and the bot), the model's answer turned into actions (`parse`) and the preview lines (`describe`).
`gymbot.services.life.apply` carries them out.

The model answers `{"actions": [{"op", "entity", "id", "fields"}], "question": null | {"text", "options"}}`:
- add / update / remove: `entity` one of life.KINDS, `id` for update/remove, `fields` from FIELDS;
- done / undone: task, event, deadline, chore (a chore's `fields` may carry the `day` it was done);
- settings: `fields` remind_before (minutes), summary_time "ЧЧ:ММ", summary_enabled;
- write_note {title, brief, project_id?}: the bot writes the text itself; edit_note {id, instruction}.
  Both stay out of `apply`: the bot (part 2) writes the body and turns them into add / update of a note.

Messages for the user are Russian; a rejected action never stops the others, it becomes a note.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from gymbot.services import life
from gymbot.services.life import LifeView

OPS = ("add", "update", "remove", "done", "undone", "settings", "write_note", "edit_note")
ID_OPS = ("update", "remove", "done", "undone", "edit_note")

FIELDS: dict[str, tuple[str, ...]] = {
    "slot": ("weekday", "start", "end", "title", "area", "tag", "place", "vld", "note"),
    "event": ("day", "start", "end", "title", "area", "project_id", "remind_before", "note"),
    "deadline": ("day", "title", "course", "project_id"),
    "task": ("title", "tag", "project_id"),
    "chore": ("title", "every_days", "last_done"),
    "project": ("name", "kind", "status", "description", "link", "next_step"),
    "note": ("title", "body", "tags", "project_id", "author"),
    "settings": ("remind_before", "summary_time", "summary_enabled"),
}
REQUIRED: dict[str, tuple[str, ...]] = {
    "slot": ("weekday", "start", "title"), "event": ("day", "title"), "deadline": ("day", "title"),
    "task": ("title",), "chore": ("title",), "project": ("name",), "note": ("title",), "settings": (),
}
NULLABLE: dict[str, frozenset[str]] = {
    "slot": frozenset({"end", "tag", "place", "vld", "note"}),
    "event": frozenset({"start", "end", "project_id", "remind_before", "note"}),
    "deadline": frozenset({"course", "project_id"}),
    "task": frozenset({"tag", "project_id"}),
    "chore": frozenset({"last_done"}),
    "project": frozenset({"description", "link", "next_step"}),
    "note": frozenset({"project_id"}),
    "settings": frozenset(),
}
NAMES = {
    "weekday": "день недели", "start": "начало", "end": "конец", "title": "название", "area": "раздел",
    "tag": "метка", "place": "место", "vld": "срок журнала", "note": "примечание", "day": "дата",
    "project_id": "проект", "remind_before": "напомнить за", "course": "предмет", "every_days": "раз в N дней",
    "last_done": "последний раз", "name": "название", "kind": "вид", "status": "статус",
    "description": "описание", "link": "ссылка", "next_step": "следующий шаг", "body": "текст", "tags": "метки",
    "author": "автор", "summary_time": "время сводки", "summary_enabled": "сводка",
}
DATE_RANGE = 400  # days from today either way
TITLE_MAX = 200
SHORT_MAX = 500
LONG_MAX = 2000
BODY_MAX = 20000
TAG_MAX = 32
TAGS_MAX = 20
OPTIONS_MAX = 4
_TIME = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


@dataclass(frozen=True)
class Act:
    op: str  # one of OPS
    entity: str | None  # one of life.KINDS (None for settings)
    id: int | None = None
    fields: dict[str, Any] = field(default_factory=dict)  # validated, typed values (date, "ЧЧ:ММ", str, int)


# ---- fields ----


def _name(key: str) -> str:
    return f"«{NAMES.get(key, key)}»"


def _line(value: Any, limit: int, key: str) -> str | None:
    """One line of text, whitespace collapsed; None when empty."""
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        raise ValueError(f"Поле {_name(key)}: нужен текст")  # noqa: TRY004 - one error type for every bad value
    out = " ".join(str(value).split())
    if len(out) > limit:
        raise ValueError(f"Поле {_name(key)}: не длиннее {limit} символов")
    return out or None


def _block(value: Any, limit: int, key: str) -> str:
    """Multi-line text (Markdown), only trimmed."""
    if not isinstance(value, str):
        raise ValueError(f"Поле {_name(key)}: нужен текст")  # noqa: TRY004 - one error type for every bad value
    out = value.strip()
    if len(out) > limit:
        raise ValueError(f"Поле {_name(key)}: не длиннее {limit} символов")
    return out


def check_time(value: Any, key: str = "start") -> str:
    """"9:00" / "09:00" -> "09:00"; ValueError for "25:00" and anything else."""
    m = _TIME.match(value.strip()) if isinstance(value, str) else None
    if m is None:
        raise ValueError(f"Поле {_name(key)}: время в виде ЧЧ:ММ, а не «{value}»")
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def check_date(value: Any, today: date, key: str = "day") -> date:
    """A date "ГГГГ-ММ-ДД" (or a date) at most 400 days from today."""
    if isinstance(value, date):
        day = value
    else:
        try:
            day = date.fromisoformat(value) if isinstance(value, str) else None
        except ValueError:
            day = None
        if day is None:
            raise ValueError(f"Поле {_name(key)}: дата в виде ГГГГ-ММ-ДД, а не «{value}»")
    if abs((day - today).days) > DATE_RANGE:
        raise ValueError(f"Дата {day.isoformat()} дальше {DATE_RANGE} дней от сегодня")
    return day


def _int(value: Any, lo: int, hi: int, key: str) -> int:
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise ValueError(f"Поле {_name(key)}: целое от {lo} до {hi}")
    return value


def _choice(value: Any, options: tuple[str, ...], key: str) -> str:
    if value not in options:
        raise ValueError(f"Поле {_name(key)}: одно из {', '.join(options)}")
    return value


def _tags(value: Any) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"Поле {_name('tags')}: нужен список")  # noqa: TRY004 - one error type for every bad value
    out: list[str] = []
    for t in value:
        tag = _line(t, TAG_MAX, "tags")
        if tag and tag not in out:
            out.append(tag)
    if len(out) > TAGS_MAX:
        raise ValueError(f"Не больше {TAGS_MAX} меток")
    return out


def _check(entity: str, key: str, value: Any, today: date) -> Any:
    out = _value(entity, key, value, today)
    if out is None and key not in NULLABLE[entity]:
        raise ValueError(f"Поле {_name(key)} не может быть пустым")
    return out


def _value(entity: str, key: str, value: Any, today: date) -> Any:
    if key == "body":
        return "" if value is None else _block(value, BODY_MAX, key)
    if key == "tags":
        return [] if value is None else _tags(value)
    if value is None or value == "":
        return None
    if key in ("start", "end", "vld", "summary_time"):
        return check_time(value, key)
    if key in ("day", "last_done"):
        return check_date(value, today, key)
    if key in ("title", "name", "place", "course"):
        return _line(value, TITLE_MAX, key)
    if key == "tag":
        return _line(value, TAG_MAX, key)
    if key in ("link", "next_step"):
        return _line(value, SHORT_MAX, key)
    if key in ("note", "description"):
        return _block(value, LONG_MAX, key) or None
    if key == "weekday":
        return _int(value, 1, 7, key)
    if key == "every_days":
        return _int(value, 1, 365, key)
    if key == "remind_before":
        return _int(value, 0, 1440, key)
    if key == "project_id":
        return _int(value, 1, 2**31 - 1, key)
    if key == "area":
        return _choice(value, life.AREAS_SLOT if entity == "slot" else life.AREAS_EVENT, key)
    if key == "kind":
        return _choice(value, life.PROJECT_KINDS, key)
    if key == "status":
        return _choice(value, life.PROJECT_STATUSES, key)
    if key == "author":
        return _choice(value, life.NOTE_AUTHORS, key)
    if key == "summary_enabled":
        if not isinstance(value, bool):
            raise ValueError(f"Поле {_name(key)}: да или нет")
        return value
    raise ValueError(f"Неизвестное поле «{key}»")


def validate_fields(entity: str, fields: dict, today: date, *, partial: bool) -> dict:
    """Typed values of `fields` of an `entity` (life.KINDS or "settings"): dates as date, times "ЧЧ:ММ", text
    trimmed, "" -> None where the field may be empty. `partial` (update): any subset; else the required fields
    must be there. ValueError with a Russian message for an unknown field, a bad value or a missing one."""
    if entity not in FIELDS:
        raise ValueError(f"Не знаю, что такое «{entity}»")
    if not isinstance(fields, dict):
        raise ValueError("Поля должны быть объектом")  # noqa: TRY004 - one error type for every bad value
    for key in fields:
        if key not in FIELDS[entity]:
            raise ValueError(f"Неизвестное поле «{key}»")
    out = {key: _check(entity, key, value, today) for key, value in fields.items()}
    if not partial:
        for key in REQUIRED[entity]:
            if out.get(key) is None:
                raise ValueError(f"Нужно поле {_name(key)}")
    if out.get("start") and out.get("end") and out["end"] <= out["start"]:
        raise ValueError("Конец раньше начала")
    return out


# ---- the model's answer ----

_IN = {"slot": "в расписании", "event": "в планах", "deadline": "в дедлайнах", "task": "в задачах",
       "chore": "в делах по дому", "project": "в проектах", "note": "в заметках"}


def _ids(view: LifeView) -> dict[str, set[int]]:
    return {
        "slot": {r.id for r in view.slots}, "event": {r.id for r in view.events},
        "deadline": {r.id for r in view.deadlines}, "task": {r.id for r in view.tasks},
        "chore": {r.id for r in view.chores}, "project": {r.id for r in view.projects},
        "note": {r.id for r in view.notes},
    }


def _as_id(value: Any) -> int | None:
    if isinstance(value, str):
        value = value.strip().lstrip("#")
        value = int(value) if value.isdigit() else None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _question(raw: Any) -> dict | None:
    if not isinstance(raw, dict) or not isinstance(raw.get("text"), str) or not raw["text"].strip():
        return None
    options = raw.get("options") if isinstance(raw.get("options"), list) else []
    opts = [" ".join(o.split())[:64] for o in options if isinstance(o, str) and o.strip()][:OPTIONS_MAX]
    return {"text": raw["text"].strip(), "options": opts}


def parse(data: Any, view: LifeView, today: date) -> tuple[list[Act], list[str], dict | None]:
    """Actions of the model's answer that can be carried out, notes about the dropped ones (unknown op, kind,
    field or id, bad values) and the model's question ({"text", "options" <= 4} or None). Ids must be in
    `view`; dates are checked against `today` (local)."""
    raw = data.get("actions") if isinstance(data, dict) else None
    ids = _ids(view)
    acts: list[Act] = []
    notes: list[str] = []
    for a in raw if isinstance(raw, list) else []:
        if not isinstance(a, dict):
            continue
        op, entity = a.get("op"), a.get("entity")
        if op not in OPS:
            notes.append(f"Не понял действие «{op}».")
            continue
        if op == "settings":
            entity = None
        elif op in ("write_note", "edit_note"):
            entity = "note"
        elif entity not in life.KINDS:
            notes.append(f"Не знаю, что такое «{entity}».")
            continue
        elif op in ("done", "undone") and entity not in life.DONE_KINDS:
            notes.append(f"«{entity}» нельзя отметить сделанным.")
            continue
        row_id = None
        if op in ID_OPS:
            row_id = _as_id(a.get("id"))
            if entity is None or row_id not in ids[entity]:
                notes.append(f"Не нашёл #{a.get('id')} {_IN.get(entity or '', '')}.")
                continue
        raw_fields = a.get("fields") if isinstance(a.get("fields"), dict) else {}
        try:
            fields = _fields(op, entity, raw_fields, ids, today, notes)
        except ValueError as e:
            notes.append(f"{e}.")
            continue
        if fields is None:
            continue
        acts.append(Act(op, entity, row_id, fields))
    return acts, list(dict.fromkeys(notes)), _question(data.get("question") if isinstance(data, dict) else None)


def _fields(
    op: str, entity: str | None, raw: dict, ids: dict[str, set[int]], today: date, notes: list[str]
) -> dict | None:
    """Checked fields of one action; None to drop it quietly (an update with nothing to change)."""
    if op in ("add", "update", "settings"):
        kind = entity or "settings"
        for key in raw:
            if key not in FIELDS[kind]:
                notes.append(f"Поле «{key}» не знаю, пропустил.")
        out = validate_fields(kind, {k: v for k, v in raw.items() if k in FIELDS[kind]}, today, partial=op != "add")
        if out.get("project_id") is not None and out["project_id"] not in ids["project"]:
            notes.append(f"Не нашёл проект #{out.pop('project_id')}.")
        return out if out or op == "add" else None
    if op == "done" and entity == "chore":
        when = raw.get("day") or raw.get("date")
        return {"day": min(check_date(when, today), today) if when else today}
    if op == "write_note":
        title = _line(raw.get("title"), TITLE_MAX, "title") if raw.get("title") else None
        if title is None:
            raise ValueError("У заметки нужно название")
        out = {"title": title, "brief": _block(raw.get("brief") or title, LONG_MAX, "body")}
        if (pid := _as_id(raw.get("project_id"))) is not None:
            if pid in ids["project"]:
                out["project_id"] = pid
            else:
                notes.append(f"Не нашёл проект #{pid}.")
        return out
    if op == "edit_note":
        instruction = _block(raw.get("instruction") or "", LONG_MAX, "body")
        if not instruction:
            raise ValueError("Не понял, как переписать заметку")
        return {"instruction": instruction}
    return {}  # remove, done, undone


# ---- preview ----

_AREA_TO = {"study": "В учёбу", "work": "В работу", "other": "В планы"}
_EVERY = ("по понедельникам", "по вторникам", "по средам", "по четвергам", "по пятницам", "по субботам",
          "по воскресеньям")
_WORD = {"slot": "из расписания", "event": "план", "deadline": "дедлайн", "task": "задачу", "chore": "дело по дому",
         "project": "проект", "note": "заметку"}


def day_word(day: date, today: date) -> str:
    """«сегодня», «завтра», «послезавтра», «вчера» or «пн 03.11»."""
    near = {0: "сегодня", 1: "завтра", 2: "послезавтра", -1: "вчера"}.get((day - today).days)
    return near or f"{life.WEEKDAYS[day.weekday()]} {day:%d.%m}"


def _at(start: str | None, end: str | None) -> str:
    if start is None:
        return ""
    return f" в {start}" + (f"–{end}" if end else "")


def _rows(view: LifeView, entity: str) -> dict[int, Any]:
    rows = {"slot": view.slots, "event": view.events, "deadline": view.deadlines, "task": view.tasks,
            "chore": view.chores, "project": view.projects, "note": view.notes}[entity]
    return {r.id: r for r in rows}


def _title(row: Any) -> str:
    return getattr(row, "title", None) or getattr(row, "name", None) or f"#{row.id}"


def _change(key: str, value: Any, today: date) -> str:
    if value is None:
        return f"{NAMES.get(key, key)} — убрать"
    if isinstance(value, date):
        value = day_word(value, today)
    elif key == "weekday":
        value = life.WEEKDAYS[value - 1]
    elif isinstance(value, list):
        value = ", ".join(value) or "нет"
    elif isinstance(value, str) and len(value) > 60:
        value = value[:59] + "…"
    return f"{NAMES.get(key, key)}: {value}"


def _describe_add(a: Act, today: date) -> str:
    f = a.fields
    if a.entity == "event":
        return f"{_AREA_TO.get(f.get('area', 'other'), 'В планы')}: «{f['title']}» — {day_word(f['day'], today)}" \
            + _at(f.get("start"), f.get("end"))
    if a.entity == "slot":
        return f"В расписание: «{f['title']}» — {_EVERY[f['weekday'] - 1]}{_at(f['start'], f.get('end'))}"
    if a.entity == "deadline":
        course = f" ({f['course']})" if f.get("course") else ""
        return f"Дедлайн: «{f['title']}»{course} — {day_word(f['day'], today)}"
    if a.entity == "task":
        return f"Добавлю задачу «{f['title']}»"
    if a.entity == "chore":
        return f"Добавлю дело по дому «{f['title']}» — раз в {f.get('every_days', 7)} дн."
    if a.entity == "project":
        return f"Добавлю проект «{f['name']}»"
    return f"Сохраню заметку «{f['title']}»"


def _describe_update(a: Act, row: Any, today: date) -> str:
    f, name = a.fields, _title(row)
    if a.entity == "slot" and "weekday" in f and f["weekday"] != row.weekday:
        when = f"с {life.WEEKDAYS[row.weekday - 1]} на {life.WEEKDAYS[f['weekday'] - 1]}"
        rest = [_change(k, v, today) for k, v in f.items() if k not in ("weekday", "start")]
        at = f" в {f['start']}" if f.get("start") else ""
        return f"Перенесу «{name}» {when}{at}" + (f"; {', '.join(rest)}" if rest else "")
    if a.entity in ("event", "deadline") and ("day" in f or f.get("start")):
        day = f.get("day", row.day)
        rest = [_change(k, v, today) for k, v in f.items() if k not in ("day", "start", "end")]
        return f"Перенесу «{name}» на {day_word(day, today)}{_at(f.get('start'), f.get('end'))}" \
            + (f"; {', '.join(rest)}" if rest else "")
    return f"Изменю «{name}»: " + ", ".join(_change(k, v, today) for k, v in f.items())


def _describe_settings(f: dict) -> list[str]:
    out = []
    if "remind_before" in f:
        out.append(f"Напоминать за {f['remind_before']} мин до планов" if f["remind_before"]
                   else "Напоминать в момент начала плана")
    if "summary_time" in f:
        out.append(f"Утреннюю сводку присылать в {f['summary_time']}")
    if "summary_enabled" in f:
        out.append("Утреннюю сводку присылать" if f["summary_enabled"] else "Утреннюю сводку не присылать")
    return out


def describe(acts: list[Act], view: LifeView, today: date) -> list[str]:
    """Preview lines: «В учёбу: «Позаниматься HTTP-сервером на C» — сегодня в 11:00», «Закрою задачу «…»»,
    «Перенесу «ИЗ Математика» с пн на вт»."""
    lines: list[str] = []
    for a in acts:
        if a.op == "settings":
            lines += _describe_settings(a.fields)
            continue
        if a.op == "add":
            lines.append(_describe_add(a, today))
            continue
        if a.op == "write_note":
            lines.append(f"Напишу заметку «{a.fields['title']}»")
            continue
        row = _rows(view, a.entity or "note").get(a.id or 0)
        if row is None:
            continue
        name = _title(row)
        if a.op == "update":
            lines.append(_describe_update(a, row, today))
        elif a.op == "remove":
            lines.append(f"Удалю {_WORD[a.entity or 'note']} «{name}»")
        elif a.op == "edit_note":
            lines.append(f"Перепишу заметку «{name}»: {a.fields['instruction'][:120]}")
        elif a.op == "done":
            if a.entity == "chore":
                day = a.fields.get("day", today)
                when = "сегодня" if day == today else f"{day:%d.%m}"
                lines.append(f"Отмечу «{name}» — сделано {when}")
            elif a.entity == "task":
                lines.append(f"Закрою задачу «{name}»")
            elif a.entity == "deadline":
                lines.append(f"Отмечу дедлайн «{name}» сданным")
            else:
                lines.append(f"Отмечу «{name}» сделанным")
        elif a.op == "undone":
            lines.append(f"Верну в работу «{name}»" if a.entity == "task" else f"Сниму отметку «{name}»")
    return lines
