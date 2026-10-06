"""Import a training program from the xlsx format Amir uses.

Sheet layout (3 columns, header in row 1):
    упражнения | интенсивность | подходы х повторения
Marker rows have only column A filled: "неделя N" starts a week, a weekday name
("понедельник", "среда", ...) starts a training day. Every other row is an exercise.

Prescription formats seen so far (column C):
    "6х8-12"              -> 6 working sets, 8..12 reps
    "дропсет 3х 12-6-6"   -> 3 drop sets, each 12 reps then two drops of 6

Run as a script to convert xlsx -> JSON:
    python -m gymbot.importers.xlsx_program in.xlsx out.json --name "..."
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

WEEKDAYS = {
    "понедельник": 1,
    "вторник": 2,
    "среда": 3,
    "четверг": 4,
    "пятница": 5,
    "суббота": 6,
    "воскресенье": 7,
}

INTENSITY = {"тяжелая": "heavy", "тяжёлая": "heavy", "средняя": "medium", "легкая": "light", "лёгкая": "light"}

# Typos found in the source sheet, fixed on import so exercises dedupe correctly.
NAME_FIXES = {"тяжа ": "тяга "}

WEEK_RE = re.compile(r"^неделя\s*(\d+)$", re.I)
SETS_RE = re.compile(r"^(\d+)\s*[хx]\s*(\d+)\s*-\s*(\d+)$", re.I)
DROP_RE = re.compile(r"^дропсет\s*(\d+)\s*[хx]\s*(\d+(?:\s*-\s*\d+)+)$", re.I)


@dataclass
class Prescription:
    sets: int
    reps_min: int | None = None
    reps_max: int | None = None
    drop_reps: list[int] | None = None  # set only for drop sets, e.g. [12, 6, 6]
    raw: str = ""

    @property
    def is_dropset(self) -> bool:
        return self.drop_reps is not None


@dataclass
class ProgramExercise:
    name: str
    intensity: str | None
    prescription: Prescription
    order: int


@dataclass
class ProgramDay:
    weekday: int
    title: str
    exercises: list[ProgramExercise] = field(default_factory=list)


@dataclass
class ProgramWeek:
    number: int
    days: list[ProgramDay] = field(default_factory=list)


@dataclass
class Program:
    name: str
    source: str
    weeks: list[ProgramWeek] = field(default_factory=list)


def normalize_name(name: str) -> str:
    name = " ".join(name.strip().lower().split())
    for bad, good in NAME_FIXES.items():
        if name.startswith(bad):
            name = good + name[len(bad):]
    return name


def parse_prescription(raw: str) -> Prescription:
    text = " ".join(str(raw).strip().lower().split())
    if m := SETS_RE.match(text):
        return Prescription(sets=int(m[1]), reps_min=int(m[2]), reps_max=int(m[3]), raw=str(raw))
    if m := DROP_RE.match(text):
        drops = [int(x) for x in re.split(r"\s*-\s*", m[2])]
        return Prescription(sets=int(m[1]), drop_reps=drops, raw=str(raw))
    raise ValueError(f"unknown prescription format: {raw!r}")


def parse_rows(rows, name: str, source: str) -> Program:
    program = Program(name=name, source=source)
    week: ProgramWeek | None = None
    day: ProgramDay | None = None
    for line_no, row in enumerate(rows, start=2):
        a, b, c = (list(row) + [None, None, None])[:3]
        if a is None or not str(a).strip():
            continue
        label = " ".join(str(a).strip().lower().split())
        if b is None and c is None:
            if m := WEEK_RE.match(label):
                week = ProgramWeek(number=int(m[1]))
                program.weeks.append(week)
                day = None
                continue
            if label in WEEKDAYS:
                if week is None:
                    raise ValueError(f"row {line_no}: day {label!r} before any week")
                day = ProgramDay(weekday=WEEKDAYS[label], title=label)
                week.days.append(day)
                continue
            raise ValueError(f"row {line_no}: unknown marker row {a!r}")
        if day is None:
            raise ValueError(f"row {line_no}: exercise {a!r} before any day")
        intensity = INTENSITY.get(str(b).strip().lower()) if b else None
        day.exercises.append(
            ProgramExercise(
                name=normalize_name(str(a)),
                intensity=intensity,
                prescription=parse_prescription(c),
                order=len(day.exercises) + 1,
            )
        )
    return program


def load_xlsx(path: Path, name: str | None = None) -> Program:
    import openpyxl

    wb = openpyxl.load_workbook(path, read_only=True)
    ws = wb.worksheets[0]
    rows = ws.iter_rows(min_row=2, values_only=True)
    return parse_rows(rows, name=name or path.stem, source=path.name)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("xlsx", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--name")
    args = ap.parse_args(argv)
    program = load_xlsx(args.xlsx, args.name)
    args.out.write_text(json.dumps(asdict(program), ensure_ascii=False, indent=2), encoding="utf-8")
    n_ex = sum(len(d.exercises) for w in program.weeks for d in w.days)
    print(f"{program.name}: {len(program.weeks)} weeks, {n_ex} exercise slots -> {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
