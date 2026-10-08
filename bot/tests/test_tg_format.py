"""plain(): model Markdown -> Telegram plain text."""

import pytest

from gymbot.services.tg_format import plain


def test_table_with_header():
    src = "| Упражнение | Вес | Подходы |\n|---|---|---|\n| Сгибания на EZ | 27,5 кг | 3×8–12 |"
    assert plain(src) == "• Сгибания на EZ — 27,5 кг, 3×8–12"


def test_table_several_rows_with_alignment_and_text_around():
    src = (
        "Вот план:\n\n"
        "| Упражнение | Вес |\n| :--- | :-: |\n"
        "| Жим лёжа | 80 кг |\n| Тяга | 100 кг |\n\n"
        "Удачи!"
    )
    assert plain(src) == "Вот план:\n\n• Жим лёжа — 80 кг\n• Тяга — 100 кг\n\nУдачи!"


def test_table_without_header_separator():
    src = "| Жим | 80 кг | 3×8 |\n| Тяга | 100 кг | 3×5 |"
    assert plain(src) == "• Жим — 80 кг, 3×8\n• Тяга — 100 кг, 3×5"


def test_table_without_outer_pipes():
    src = "Упражнение | Вес\n--- | ---\nЖим | 80 кг"
    assert plain(src) == "• Жим — 80 кг"


def test_table_empty_and_single_cells_and_markup():
    src = "| **Жим** | | `80 кг` |\n| Отдых |  |  |"
    assert plain(src) == "• Жим — 80 кг\n• Отдых"


def test_bold_italic_strike():
    assert plain("**жирный** и __тоже__") == "жирный и тоже"
    assert plain("*курсив* и _тоже_") == "курсив и тоже"
    assert plain("~~старое~~ новое") == "старое новое"
    assert plain("***важно***") == "важно"
    assert plain("**жим *лёжа* сегодня**") == "жим лёжа сегодня"


def test_headers():
    assert plain("# Заголовок\n### Ещё один\nтекст") == "Заголовок\nЕщё один\nтекст"
    assert plain("## **План** на неделю") == "План на неделю"
    assert plain("#хештег") == "#хештег"


def test_inline_code_and_fences():
    assert plain("Сделай `3×8` подходов") == "Сделай 3×8 подходов"
    assert plain("```python\nx = 1\ny = 2\n```\nготово") == "x = 1\ny = 2\nготово"
    assert plain("```\na *b* c\n```") == "a *b* c"
    assert plain("`**не трогать**`") == "**не трогать**"


@pytest.mark.parametrize(
    "text",
    ["20*8", "3 * 10", "snake_case", "2*3*4", "*", "жим 20*8 и 3 * 10", "my_var_name и other_name", "a * b * c"],
)
def test_arithmetic_and_identifiers_untouched(text):
    assert plain(text) == text


def test_star_and_plus_bullets():
    assert plain("* первый\n* второй\n+ третий") == "• первый\n• второй\n• третий"
    assert plain("* **жим** — 80 кг") == "• жим — 80 кг"
    assert plain("  * вложенный") == "• вложенный"


def test_dash_and_numbered_lists_unchanged():
    text = "- первый\n- второй\n1. раз\n2. два"
    assert plain(text) == text


def test_horizontal_rules_removed():
    assert plain("до\n\n---\n\nпосле") == "до\n\nпосле"
    assert plain("***\n___\nтекст") == "текст"
    assert plain("- - -\nтекст") == "текст"


def test_links():
    assert plain("смотри [статью](https://example.com/a) тут") == "смотри статью (https://example.com/a) тут"


def test_blank_lines_collapse_and_trailing_spaces():
    assert plain("a\n\n\n\n\nb") == "a\n\nb"
    assert plain("a\n\n\n\nb") == "a\n\nb"
    assert plain("a\n\nb") == "a\n\nb"
    assert plain("a   \nb \n") == "a\nb"
    assert plain("\n\n  текст  \n\n") == "текст"


def test_plain_text_unchanged():
    text = "Сегодня жим 3×8 по 60 кг.\n\nОтдых 2 минуты, потом тяга (3 подхода)."
    assert plain(text) == text
    assert plain("") == ""


@pytest.mark.parametrize(
    "src",
    [
        "| Упражнение | Вес |\n|---|---|\n| Жим | **80** кг |",
        "# Итог\n**Белок** — _мало_, ~~много~~\n* пункт\n---\n[ссылка](http://a.b)\n\n\n\n\nконец",
        "```\ncode\n```\n`x` и 20*8",
        "- a\n1. b",
    ],
)
def test_idempotent(src):
    once = plain(src)
    assert plain(once) == once
