"""Sanitizer for model text that goes to Telegram as plain text.

The bot sends answers without `parse_mode`, so Telegram shows Markdown literally: `**bold**`, `| a | b |`
tables and `## headers` arrive as raw characters. The models still emit them despite the prompt, so
`plain()` turns that markup into readable plain text (tables -> bullet lines, emphasis and code markers
dropped, links -> `text (url)`). Text without markup is returned unchanged and `plain` is idempotent.
Arithmetic like `20*8`, `3 * 10` or `snake_case` is not markup and is left alone.
"""

from __future__ import annotations

import re

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


def _table_row(line: str) -> str | None:
    cells = [c for c in (_inline(c) for c in _cells(line)) if c]
    if not cells:
        return None
    if len(cells) == 1:
        return f"• {cells[0]}"
    return f"• {cells[0]} — {', '.join(cells[1:])}"


def _table(block: list[str]) -> list[str]:
    """Rows of a Markdown table -> bullet lines; separator rows and a header (above a separator) go."""
    out: list[str] = []
    for i, line in enumerate(block):
        if _is_separator(line):
            continue
        if i + 1 < len(block) and _is_separator(block[i + 1]) and i == 0:
            continue  # header row
        row = _table_row(line)
        if row is not None:
            out.append(row)
    return out


def plain(text: str) -> str:
    """Model text -> Telegram plain text: no tables, no Markdown markers (see the module docstring)."""
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
            out.append(line)
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
            out.extend(_table(block))
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
            out.append(_inline(m.group(1)))
            continue
        line = _BULLET.sub(lambda m: f"{m.group(1)}• ", line)
        out.append(_inline(line))
    result = "\n".join(s.rstrip() for s in out)
    return _BLANK_RUN.sub("\n\n", result).strip()
