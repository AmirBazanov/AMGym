"""Sanitizer for model text that goes to Telegram as plain text.

Telegram without `parse_mode` shows Markdown literally: `**bold**`, `| a | b |` tables and `## headers`
arrive as raw characters. `plain()` turns that markup into readable plain text: the answer check
(gymbot.services.answer_check) reads it, and it is the fallback when Telegram rejects the HTML version
(gymbot.services.tg_html, which shares `render` and the regexps below). Tables become bullet lines,
emphasis and code markers are dropped, links become `text (url)`. Text without markup is returned unchanged and `plain` is idempotent.
Arithmetic like `20*8`, `3 * 10` or `snake_case` is not markup and is left alone.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

_FENCE = re.compile(r"^\s*```")
_HEADER = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)(?:\s+#+)?\s*$")
_HRULE = re.compile(r"^\s*([-*_])(?:\s*\1){2,}\s*$")
_BULLET = re.compile(r"^(\s*)(?:\*|\+(?!\s*\d))\s+")  # "+ 2,5 кг к жиму" keeps its plus
_SEPARATOR_CELL = re.compile(r"^:?-+:?$")
_CODE_SPAN = re.compile(r"`([^`\n]+)`")
_URL = re.compile(r"(?:https?://|www\.)[^\s<>()]+")
_LINK = re.compile(r"\[([^\]\n]+)\]\(([^)\s]+)\)")
_BOLD_ITALIC = re.compile(r"(?<![\w*])\*{3}(?=\S)(.+?)(?<=\S)\*{3}(?![\w*])")
_BOLD_STAR = re.compile(r"(?<![\w*])\*{2}(?=\S)(.+?)(?<=\S)\*{2}(?![\w*])")
_BOLD_UNDER = re.compile(r"(?<![\w_])__(?=\S)(.+?)(?<=\S)__(?![\w_])")
_ITALIC_STAR = re.compile(r"(?<![\w*])\*(?=[^\s*])([^*\n]+?)(?<=[^\s*])\*(?![\w*])")
_ITALIC_UNDER = re.compile(r"(?<![\w_])_(?=[^\s_])([^_\n]+?)(?<=[^\s_])_(?![\w_])")
_STRIKE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~")
_BLANK_RUN = re.compile(r"\n{4,}")  # 3+ blank lines in a row


def _inline(text: str) -> str:
    """Strip inline markup: code spans, links, bold, italic, strikethrough."""
    spans: list[str] = []

    def keep(match: re.Match[str]) -> str:
        spans.append(match.group(1))
        return f"\x00{len(spans) - 1}\x00"

    def keep_all(match: re.Match[str]) -> str:
        spans.append(match.group(0))
        return f"\x00{len(spans) - 1}\x00"

    text = _CODE_SPAN.sub(keep, text)  # code content is not touched by the other rules
    text = _LINK.sub(r"\1 (\2)", text)
    text = _URL.sub(keep_all, text)  # nor are URLs: "https://x.ru/a_b_c" keeps its underscores
    for pattern in (_BOLD_ITALIC, _BOLD_STAR, _BOLD_UNDER, _ITALIC_STAR, _ITALIC_UNDER, _STRIKE):
        text = pattern.sub(r"\1", text)
    return re.sub(r"\x00(\d+)\x00", lambda m: spans[int(m.group(1))], text)


def _cells(line: str) -> list[str]:
    row = line.strip()
    row = row.removeprefix("|").removesuffix("|")
    return [c.strip() for c in row.split("|")]


def _is_separator(line: str) -> bool:
    if "|" not in line:
        return False
    cells = _cells(line)
    return bool(cells) and all(_SEPARATOR_CELL.match(c) for c in cells)


def _is_table_line(line: str) -> bool:
    return line.lstrip().startswith("|")


def _table_row(line: str, inline: Callable[[str], str]) -> str | None:
    cells = [c for c in (inline(c) for c in _cells(line)) if c]
    if not cells:
        return None
    if len(cells) == 1:
        return f"• {cells[0]}"
    return f"• {cells[0]} — {', '.join(cells[1:])}"


def _table(block: list[str], inline: Callable[[str], str]) -> list[str]:
    """Rows of a Markdown table -> bullet lines; separator rows and a header (above a separator) go."""
    out: list[str] = []
    for i, line in enumerate(block):
        if _is_separator(line):
            continue
        if i + 1 < len(block) and _is_separator(block[i + 1]) and i == 0:
            continue  # header row
        row = _table_row(line, inline)
        if row is not None:
            out.append(row)
    return out


@dataclass(frozen=True)
class Style:
    """How `render` writes each kind of line: plain text here, Telegram HTML in gymbot.services.tg_html."""

    inline: Callable[[str], str]  # an ordinary line (and a table cell) with its inline markup
    header: Callable[[str], str]  # the text of a "# header"
    verbatim: Callable[[str], str]  # a line inside a ``` fence
    bullet: re.Pattern[str]  # list markers replaced with "• " (group 1 keeps the indent)


def render(text: str, style: Style) -> str:
    """The block structure shared by plain() and tg_html.to_html(): fences dropped (their lines kept as
    `style.verbatim`), tables -> bullet lines, rules dropped, headers, list markers -> "• ", inline markup."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    in_fence = False
    after_rule = False  # a removed rule between blank lines must not leave two of them
    i = 0
    while i < len(lines):
        line = lines[i]
        if _FENCE.match(line):
            in_fence = not in_fence
            i += 1
            continue
        if in_fence:
            out.append(style.verbatim(line))
            i += 1
            continue
        # A table: lines starting with "|", or a "a | b" line directly above a separator row.
        starts_table = _is_table_line(line) or (
            "|" in line and i + 1 < len(lines) and _is_separator(lines[i + 1]) and not _is_separator(line)
        )
        if starts_table:
            block = [line]
            i += 1
            while i < len(lines) and (_is_table_line(lines[i]) or ("|" in lines[i] and lines[i].strip())):
                block.append(lines[i])
                i += 1
            out.extend(_table(block, style.inline))
            continue
        i += 1
        if _HRULE.match(line):
            after_rule = bool(out) and not out[-1].strip()
            continue
        if after_rule and not line.strip():
            after_rule = False
            continue
        after_rule = False
        if m := _HEADER.match(line):
            out.append(style.header(m.group(1)))
            continue
        line = style.bullet.sub(lambda m: f"{m.group(1)}• ", line)
        out.append(style.inline(line))
    result = "\n".join(s.rstrip() for s in out)
    return _BLANK_RUN.sub("\n\n", result).strip()


PLAIN = Style(inline=_inline, header=_inline, verbatim=lambda line: line, bullet=_BULLET)


def plain(text: str) -> str:
    """Model text -> Telegram plain text: no tables, no Markdown markers (see the module docstring)."""
    return render(text, PLAIN)
