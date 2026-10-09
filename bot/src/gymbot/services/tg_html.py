"""Model text -> Telegram HTML, and sending it with a plain-text fallback.

Answers and advice go to Telegram with `parse_mode=HTML`, so the bold names and weights the model marks
with `**…**` show as bold. `to_html` converts the same Markdown subset that gymbot.services.tg_format.plain
removes, with the same block rules (`tg_format.render`) and the same regexps, so arithmetic (`20*8`),
`snake_case` and «+ 2,5 кг» stay text here too:

- `**x**` / `__x__` -> <b>, `*x*` / `_x_` -> <i>, `***x***` -> <b><i>, `~~x~~` -> <s>;
- `# header` -> a bold line; `-`, `*`, `+` list markers -> «• » («+ 2,5 кг» stays), «1.» stays;
- tables -> «• a — b, c» lines; ``` fences dropped (their lines kept), `code` -> its text;
- `[t](https://…)` -> <a href>; other link schemes -> «t (url)».

`<`, `>` and `&` of the text are escaped before any tag is added; text without markup comes back as the
same text, escaped. Nesting the model gets wrong may still be rejected by Telegram: `send_html` then sends
the plain version (`plain()`), never loses the message.
"""

from __future__ import annotations

import html
import re
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram.exceptions import TelegramBadRequest

from gymbot.services.tg_format import (
    _BOLD_ITALIC,
    _BOLD_STAR,
    _BOLD_UNDER,
    _BULLET,
    _CODE_SPAN,
    _ITALIC_STAR,
    _ITALIC_UNDER,
    _LINK,
    _STRIKE,
    _URL,
    Style,
    _inline,
    plain,
    render,
)

# plain()'s "*" and "+" markers, and "- " too (plain keeps it: the advice format uses it as is).
_HTML_BULLET = re.compile(_BULLET.pattern.replace("(?:", "(?:-(?!\\s*\\d)|", 1))  # "- 2,5 кг" keeps its minus
_SAFE_HREF = re.compile(r"^(?:https?|tg)://", re.IGNORECASE)
_EMPHASIS = (
    (_BOLD_ITALIC, r"<b><i>\1</i></b>"),
    (_BOLD_STAR, r"<b>\1</b>"),
    (_BOLD_UNDER, r"<b>\1</b>"),
    (_ITALIC_STAR, r"<i>\1</i>"),
    (_ITALIC_UNDER, r"<i>\1</i>"),
    (_STRIKE, r"<s>\1</s>"),
)


def escape(text: str) -> str:
    """Text for a Telegram HTML message: only <, > and & need it outside attributes."""
    return html.escape(text, quote=False)


def _inline_html(text: str) -> str:
    """Inline markup -> tags. Code spans, links and URLs are set aside on the raw text first (a URL may
    hold "&" or "_"), the rest is escaped, then emphasis becomes tags, then the set-aside parts return."""
    spans: list[str] = []
    text = text.replace("\x00", "")  # the placeholders below use NUL

    def stash(value: str) -> str:
        spans.append(value)
        return f"\x00{len(spans) - 1}\x00"

    def link(m: re.Match[str]) -> str:
        label, url = m.group(1), m.group(2)
        if not _SAFE_HREF.match(url):
            return stash(escape(f"{_inline(label)} ({url})"))
        return stash(f'<a href="{html.escape(url, quote=True)}">{escape(_inline(label))}</a>')

    text = _CODE_SPAN.sub(lambda m: stash(escape(m.group(1))), text)
    text = _LINK.sub(link, text)
    text = _URL.sub(lambda m: stash(escape(m.group(0))), text)
    text = escape(text)
    for pattern, tag in _EMPHASIS:
        text = pattern.sub(tag, text)
    return re.sub(r"\x00(\d+)\x00", lambda m: spans[int(m.group(1))], text)


def _header(text: str) -> str:
    """A header line: bold, its own markup dropped (no <b> inside <b>)."""
    return f"<b>{escape(_inline(text))}</b>"


HTML = Style(inline=_inline_html, header=_header, verbatim=escape, bullet=_HTML_BULLET)


def to_html(text: str) -> str:
    """Model Markdown -> Telegram HTML (see the module docstring)."""
    return render(text, HTML)


_NUMBERS = ("1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟")


def num(i: int) -> str:
    """The keycap emoji of a 1-based row number («1️⃣»); «▫️» past 10."""
    return _NUMBERS[i - 1] if 1 <= i <= len(_NUMBERS) else "▫️"


def card_row(i: int, title: str, meta: str = "", detail: str = "", *, struck: bool = False) -> str:
    """One exercise of a day card: «1️⃣ <b>Жим лёжа</b>» (or struck through), then the meta line
    («3×8–12 · <b>60 кг</b>», already HTML) and an italic detail line (plain text, escaped here)."""
    name = f"<s>{escape(title)}</s>" if struck else f"<b>{escape(title)}</b>"
    lines = [f"{num(i)} {name}"]
    if meta:
        lines.append(meta)
    if detail:
        lines.append(f"<i>{escape(detail)}</i>")
    return "\n".join(lines)


def bold_head(text: str) -> str:
    """Text from the database -> HTML: the first line of a reply with more lines is its heading and goes
    bold («План на …:», «Сегодня: 6 упражнений, …»); everything is escaped, nothing else changes."""
    head, sep, rest = text.partition("\n")
    if sep and rest.strip() and head.strip():
        return f"<b>{escape(head)}</b>\n{escape(rest)}"
    return escape(text)


_TAG = re.compile(r"</?[a-zA-Z][^<>]*>")


def strip_tags(html_text: str) -> str:
    """Readable plain text of Telegram HTML written by someone else (the MCP send_message tool): the
    fallback when Telegram rejects its markup."""
    return html.unescape(_TAG.sub("", html_text))


def is_parse_error(error: TelegramBadRequest) -> bool:
    """Telegram rejected the markup ("can't parse entities: …"), not the chat or the message itself."""
    message = error.message.lower()
    return "parse entities" in message or "can't parse" in message or "can't find end" in message


async def send_html[T](
    send: Callable[..., Awaitable[T]], html_text: str, fallback: str, **kwargs: Any
) -> tuple[T, bool]:
    """Send `html_text` with parse_mode=HTML through `send` (`message.answer`, or
    `functools.partial(bot.send_message, chat_id)`); if Telegram cannot parse it, send `fallback` as plain
    text. Text that needs no HTML (the same as `fallback`) goes without parse_mode at once. Returns what
    `send` returned and whether HTML was used. Other errors (chat not found, blocked) propagate."""
    if html_text == fallback:
        return await send(fallback, **kwargs), False
    try:
        return await send(html_text, parse_mode="HTML", **kwargs), True
    except TelegramBadRequest as e:
        if not is_parse_error(e):
            raise
    return await send(fallback, parse_mode=None, **kwargs), False


async def send_model_text[T](send: Callable[..., Awaitable[T]], text: str, **kwargs: Any) -> T:
    """Model Markdown to the chat as HTML, falling back to `plain(text)` (see send_html)."""
    sent, _ = await send_html(send, to_html(text), plain(text), **kwargs)
    return sent
