"""to_html(): model Markdown -> Telegram HTML; send_html(): HTML with the plain-text fallback."""

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendMessage

from gymbot.services.tg_format import plain
from gymbot.services.tg_html import bold_head, escape, send_html, send_model_text, strip_tags, to_html


def bad_request(message: str) -> TelegramBadRequest:
    return TelegramBadRequest(method=SendMessage(chat_id=1, text="x"), message=message)


# ---- to_html ----


def test_plain_text_is_the_same_text_escaped():
    text = "Сегодня жим 3×8 на 80 кг.\nОтдых 2 мин."
    assert to_html(text) == text
    assert to_html("я тебя <3, а & b > c") == "я тебя &lt;3, а &amp; b &gt; c"
    assert to_html("") == ""


def test_emphasis():
    assert to_html("**Жим лёжа** — 80 кг") == "<b>Жим лёжа</b> — 80 кг"
    assert to_html("__тоже жирный__ и *курсив* и _тоже_") == "<b>тоже жирный</b> и <i>курсив</i> и <i>тоже</i>"
    assert to_html("***важно*** ~~старое~~") == "<b><i>важно</i></b> <s>старое</s>"
    assert to_html("**жим <80 & 90>**") == "<b>жим &lt;80 &amp; 90&gt;</b>"


def test_arithmetic_snake_case_and_plus_are_not_markup():
    assert to_html("20*8 = 160, 3 * 10 * 2") == "20*8 = 160, 3 * 10 * 2"
    assert to_html("поле snake_case_name") == "поле snake_case_name"
    assert to_html("+ 2,5 кг к жиму") == "+ 2,5 кг к жиму"
    assert to_html("+2,5 кг") == "+2,5 кг"


def test_headers_are_bold_lines_without_inner_tags():
    assert to_html("# Заголовок\nтекст") == "<b>Заголовок</b>\nтекст"
    assert to_html("## **План** <на> день") == "<b>План &lt;на&gt; день</b>"
    assert to_html("#хештег") == "#хештег"


def test_list_markers_become_bullets():
    assert to_html("- первый\n* второй\n+ третий") == "• первый\n• второй\n• третий"
    assert to_html("1. раз\n2. два") == "1. раз\n2. два"
    assert to_html("- **жим** — 80 кг") == "• <b>жим</b> — 80 кг"


def test_nested_lists_keep_their_indent():
    assert to_html("- верх\n  - вложенный\n    * глубже") == "• верх\n  • вложенный\n    • глубже"


def test_tables_become_lines_like_plain():
    src = "| Упражнение | Вес |\n|---|---|\n| **Жим** | 80 кг |\n| Тяга & ко | 100 кг |"
    assert to_html(src) == "• <b>Жим</b> — 80 кг\n• Тяга &amp; ко — 100 кг"
    assert strip_tags(to_html(src)) == plain(src)


def test_code_fences_and_spans_are_dropped_and_escaped():
    assert to_html("```python\nif a < b:\n    x = *y*\n```\nготово") == "if a &lt; b:\n    x = *y*\nготово"
    assert to_html("сделай `**3×8**`") == "сделай **3×8**"


def test_links():
    assert to_html("[статья](https://x.ru/a_b?q=1&r=2)") == '<a href="https://x.ru/a_b?q=1&amp;r=2">статья</a>'
    assert to_html("смотри https://x.ru/a_b_c&d и _это_") == "смотри https://x.ru/a_b_c&amp;d и <i>это</i>"
    assert to_html("[жми](javascript:alert)") == "жми (javascript:alert)"


def test_rules_and_blank_runs_like_plain():
    assert to_html("до\n\n---\n\nпосле") == "до\n\nпосле"
    assert to_html("a\n\n\n\n\nb") == "a\n\nb"


def test_visible_text_matches_plain_for_a_typical_answer():
    src = "**Да, лучше спина-грудь.**\n- руки восстанавливаются до **пт 09.10 17:00**\n- спина отдохнула\nИди на **тягу**."
    html = to_html(src)
    assert html.startswith("<b>Да, лучше спина-грудь.</b>\n• руки")
    assert strip_tags(html).replace("• ", "- ") == plain(src)


# ---- bold_head, strip_tags ----


def test_bold_head_only_for_the_first_line_over_more_lines():
    assert bold_head("План на сегодня:\n• жим <3") == "<b>План на сегодня:</b>\n• жим &lt;3"
    assert bold_head("Сегодня: 6 упр., тоннаж 6530 кг.\n- жим") == "<b>Сегодня: 6 упр., тоннаж 6530 кг.</b>\n- жим"
    assert bold_head("Сегодня тренировки по программе нет.") == "Сегодня тренировки по программе нет."
    assert bold_head("Итого:\n\n") == "Итого:\n\n"
    assert bold_head("Еда & вода\nстрока") == "<b>Еда &amp; вода</b>\nстрока"


def test_strip_tags():
    assert strip_tags("<b>Питание</b> &lt;3 &amp; <a href=\"https://x\">ссылка</a>") == "Питание <3 & ссылка"
    assert escape("a<b>&") == "a&lt;b&gt;&amp;"


# ---- send_html / send_model_text ----


class FakeSend:
    def __init__(self, *errors: Exception) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.errors = list(errors)

    async def __call__(self, text, **kw):
        self.calls.append((text, kw))
        if self.errors:
            raise self.errors.pop(0)
        return "sent"


async def test_html_goes_with_parse_mode():
    send = FakeSend()
    assert await send_html(send, "<b>Жим</b>", "Жим", reply_markup="kb") == ("sent", True)
    assert send.calls == [("<b>Жим</b>", {"parse_mode": "HTML", "reply_markup": "kb"})]


async def test_text_without_html_goes_without_parse_mode():
    send = FakeSend()
    assert await send_html(send, "просто текст", "просто текст") == ("sent", False)
    assert send.calls == [("просто текст", {})]


async def test_parse_error_falls_back_to_plain_text():
    send = FakeSend(bad_request("Bad Request: can't parse entities: unclosed start tag at byte offset 3"))
    assert await send_html(send, "<b>Жим", "Жим", reply_markup="kb") == ("sent", False)
    assert send.calls[1] == ("Жим", {"parse_mode": None, "reply_markup": "kb"})


async def test_other_bad_requests_propagate():
    send = FakeSend(bad_request("Bad Request: chat not found"))
    with pytest.raises(TelegramBadRequest):
        await send_html(send, "<b>Жим</b>", "Жим")
    assert len(send.calls) == 1


async def test_send_model_text_converts_and_falls_back_to_plain():
    send = FakeSend()
    await send_model_text(send, "**Жим** 80 кг & тяга")
    assert send.calls == [("<b>Жим</b> 80 кг &amp; тяга", {"parse_mode": "HTML"})]

    send = FakeSend(bad_request("Bad Request: can't parse entities"))
    await send_model_text(send, "**Жим** 80 кг & тяга")
    assert send.calls[1] == ("Жим 80 кг & тяга", {"parse_mode": None})


def test_nul_and_minus_lines() -> None:
    from gymbot.services.tg_html import to_html

    assert to_html("a\x000\x00b") == "a0b"
    assert to_html("- 2,5 кг на руку") == "- 2,5 кг на руку"
    assert to_html("- жим") == "• жим"
