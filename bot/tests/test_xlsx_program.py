from pathlib import Path

import pytest

from gymbot.importers.xlsx_program import load_xlsx, normalize_name, parse_prescription, parse_rows

SAMPLE = Path(__file__).resolve().parents[2] / "data/programs/source/arms_specialization_8w.xlsx"


def test_parse_straight_sets():
    p = parse_prescription("6х8-12")
    assert (p.sets, p.reps_min, p.reps_max, p.is_dropset) == (6, 8, 12, False)


def test_parse_dropset():
    p = parse_prescription("дропсет 3х 12-6-6")
    assert p.sets == 3 and p.drop_reps == [12, 6, 6] and p.is_dropset


def test_parse_latin_x():
    assert parse_prescription("4x12-15").reps_max == 15


def test_unknown_format_raises():
    with pytest.raises(ValueError):
        parse_prescription("до отказа")


def test_typo_fix():
    assert normalize_name("Тяжа вертикального блока") == "тяга вертикального блока"


def test_rows_structure():
    rows = [("неделя 1", None, None), ("понедельник", None, None), ("жим лёжа", "тяжёлая", "4х8-12")]
    prog = parse_rows(rows, "t", "t.xlsx")
    ex = prog.weeks[0].days[0].exercises[0]
    assert ex.intensity == "heavy" and ex.prescription.sets == 4


@pytest.mark.skipif(not SAMPLE.exists(), reason="sample program not present")
def test_sample_program():
    prog = load_xlsx(SAMPLE)
    assert len(prog.weeks) == 8
    assert all([d.weekday for d in w.days] == [1, 3, 5] for w in prog.weeks)
    assert sum(len(d.exercises) for w in prog.weeks for d in w.days) == 128
