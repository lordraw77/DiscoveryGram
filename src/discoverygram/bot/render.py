"""Turning content into messages Telegram will actually accept.

Two limits shape everything here:

* **MarkdownV2 is unforgiving.** Eighteen characters are reserved, and a single
  unescaped one makes the Bot API reject the whole message with a 400. Note
  bodies are arbitrary user text, so nothing is ever interpolated unescaped.
* **A message is capped at 4096 characters.** Real notes exceed that, so long
  text is split — on paragraph and line boundaries where possible, and only
  mid-line when a single line is itself too long.

Rendering notes themselves lands in phase 4; this module is the primitives it
will be built on.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

# Telegram's own limits.
MESSAGE_LIMIT = 4096
CAPTION_LIMIT = 1024

# Every character MarkdownV2 reserves, per the Bot API formatting reference.
_MARKDOWN_V2_RESERVED = set("_*[]()~`>#+-=|{}.!")


def escape_markdown_v2(text: str) -> str:
    """Escape every MarkdownV2 reserved character.

    Deliberately total rather than clever: the alternative is deciding which
    occurrences are "really" markup, and getting that wrong on one note body
    means the message fails to send at all.
    """
    return "".join(f"\\{char}" if char in _MARKDOWN_V2_RESERVED else char for char in text)


def escape_html(text: str) -> str:
    """Escape the three characters Telegram's HTML mode reserves."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def escape(text: str, parse_mode: str) -> str:
    """Escape for whichever parse mode is configured."""
    return escape_html(text) if parse_mode == "HTML" else escape_markdown_v2(text)


def code_block(text: str, *, language: str = "") -> str:
    """A fenced block. Only backslash and backtick need escaping inside one."""
    body = text.replace("\\", "\\\\").replace("`", "\\`")
    return f"```{language}\n{body}\n```"


def _split_oversized_line(line: str, limit: int) -> list[str]:
    """Hard-split a single line that cannot fit. Last resort — a URL, a base64 blob."""
    return [line[start : start + limit] for start in range(0, len(line), limit)]


def chunk_text(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    """Split `text` into messages of at most `limit` characters.

    Breaks are preferred at blank lines, then at line ends, and only inside a
    line when that line alone exceeds the limit. Empty input yields one empty
    chunk, so callers always have something to send.
    """
    if limit <= 0:
        raise ValueError("limit must be positive")
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current = ""

    for paragraph in text.split("\n\n"):
        candidate = f"{current}\n\n{paragraph}" if current else paragraph
        if len(candidate) <= limit:
            current = candidate
            continue

        if current:
            chunks.append(current)
            current = ""

        if len(paragraph) <= limit:
            current = paragraph
            continue

        # The paragraph itself is too big: fall back to line boundaries.
        for line in paragraph.split("\n"):
            candidate = f"{current}\n{line}" if current else line
            if len(candidate) <= limit:
                current = candidate
                continue
            if current:
                chunks.append(current)
                current = ""
            if len(line) <= limit:
                current = line
            else:
                pieces = _split_oversized_line(line, limit)
                chunks.extend(pieces[:-1])
                current = pieces[-1]

    if current:
        chunks.append(current)
    return chunks or [""]


def truncate(text: str, limit: int, *, suffix: str = "…") -> str:
    """Shorten `text` to `limit` characters, ending with `suffix` when cut."""
    if len(text) <= limit:
        return text
    if limit <= len(suffix):
        return suffix[:limit]
    return text[: limit - len(suffix)].rstrip() + suffix


def keyboard(rows: Sequence[Sequence[tuple[str, str]]]) -> InlineKeyboardMarkup:
    """Build an inline keyboard from `(label, callback_data)` pairs."""
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(label, callback_data=data) for label, data in row] for row in rows]
    )


def button_grid(
    buttons: Iterable[tuple[str, str]], *, columns: int = 1
) -> list[list[tuple[str, str]]]:
    """Lay buttons out in rows of `columns`."""
    if columns < 1:
        raise ValueError("columns must be at least 1")
    items = list(buttons)
    return [items[start : start + columns] for start in range(0, len(items), columns)]


def pagination_row(
    *,
    previous_data: str | None,
    next_data: str | None,
    page: int,
    pages: int,
) -> list[tuple[str, str]]:
    """A `◀ n/m ▶` row. The counter is inert — Telegram needs callback data on
    every button, so it carries a no-op action the router ignores."""
    row: list[tuple[str, str]] = []
    if previous_data:
        row.append(("◀", previous_data))
    row.append((f"{page}/{pages}", "noop:"))
    if next_data:
        row.append(("▶", next_data))
    return row


__all__ = [
    "CAPTION_LIMIT",
    "MESSAGE_LIMIT",
    "button_grid",
    "chunk_text",
    "code_block",
    "escape",
    "escape_html",
    "escape_markdown_v2",
    "keyboard",
    "pagination_row",
    "truncate",
]
