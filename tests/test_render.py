"""Message rendering: escaping, chunking and keyboards.

A single unescaped reserved character makes the Bot API reject the whole
message, so these tests lean on pathological input rather than tidy input.
"""

from __future__ import annotations

import pytest

from discoverygram.bot.render import (
    MESSAGE_LIMIT,
    button_grid,
    chunk_text,
    code_block,
    escape,
    escape_html,
    escape_markdown_v2,
    keyboard,
    pagination_row,
    truncate,
)

# Every character MarkdownV2 reserves, in one string.
RESERVED = "_*[]()~`>#+-=|{}.!"


def test_every_reserved_character_is_escaped() -> None:
    escaped = escape_markdown_v2(RESERVED)

    assert escaped == "".join(f"\\{char}" for char in RESERVED)


def test_ordinary_text_survives_untouched() -> None:
    assert escape_markdown_v2("Meeting notes for Q1") == "Meeting notes for Q1"


def test_a_realistic_note_title_is_safe() -> None:
    """The kind of title that breaks a naive escaper."""
    escaped = escape_markdown_v2("Q1 2026 — costs (draft) [v2]!")

    assert "\\(" in escaped and "\\[" in escaped and "\\!" in escaped
    assert "—" in escaped  # not reserved, must not be mangled


def test_unicode_and_emoji_pass_through() -> None:
    assert escape_markdown_v2("Tiramisù 🍰 café") == "Tiramisù 🍰 café"


def test_html_escaping_covers_its_three_characters() -> None:
    assert escape_html("<b>a & b</b>") == "&lt;b&gt;a &amp; b&lt;/b&gt;"


def test_escape_follows_the_configured_parse_mode() -> None:
    assert escape("a.b", "MarkdownV2") == "a\\.b"
    assert escape("a<b", "HTML") == "a&lt;b"


def test_code_block_escapes_only_what_a_fence_needs() -> None:
    fenced = code_block("print('a`b')", language="python")

    assert fenced.startswith("```python\n")
    assert "\\`" in fenced


def test_short_text_is_one_chunk() -> None:
    assert chunk_text("hello") == ["hello"]


def test_empty_text_still_yields_something_to_send() -> None:
    assert chunk_text("") == [""]


def test_chunks_never_exceed_the_limit() -> None:
    text = "\n\n".join(f"paragraph {index} " + "x" * 300 for index in range(60))

    chunks = chunk_text(text)

    assert len(chunks) > 1
    assert all(len(chunk) <= MESSAGE_LIMIT for chunk in chunks)


def test_chunking_prefers_paragraph_boundaries() -> None:
    first = "a" * 3000
    second = "b" * 3000

    chunks = chunk_text(f"{first}\n\n{second}")

    assert chunks == [first, second]


def test_chunking_falls_back_to_line_boundaries() -> None:
    lines = [f"line {index} " + "y" * 100 for index in range(60)]

    chunks = chunk_text("\n".join(lines))

    assert len(chunks) > 1
    assert all(len(chunk) <= MESSAGE_LIMIT for chunk in chunks)
    assert chunks[0].startswith("line 0")


def test_a_single_oversized_line_is_split_as_a_last_resort() -> None:
    """A base64 blob or a giant URL has no boundary to break on."""
    blob = "z" * (MESSAGE_LIMIT * 2 + 10)

    chunks = chunk_text(blob)

    assert len(chunks) == 3
    assert "".join(chunks) == blob


def test_chunking_loses_no_characters_of_the_body() -> None:
    text = "\n\n".join("x" * 1000 for _ in range(20))

    rejoined = "".join(chunk_text(text))

    assert rejoined.replace("\n", "") == text.replace("\n", "")


def test_chunk_text_rejects_a_nonsense_limit() -> None:
    with pytest.raises(ValueError, match="positive"):
        chunk_text("abc", limit=0)


def test_truncate() -> None:
    assert truncate("short", 10) == "short"
    assert truncate("a very long title indeed", 10) == "a very lo…"
    assert truncate("abc", 1) == "…"


def test_keyboard_builds_rows_of_buttons() -> None:
    markup = keyboard([[("Open", "open:abc")], [("◀", "page:1"), ("▶", "page:3")]])

    rows = markup.inline_keyboard
    assert [len(row) for row in rows] == [1, 2]
    assert rows[0][0].callback_data == "open:abc"


def test_button_grid_lays_out_in_columns() -> None:
    grid = button_grid([(f"b{i}", f"d{i}") for i in range(5)], columns=2)

    assert [len(row) for row in grid] == [2, 2, 1]


def test_button_grid_rejects_zero_columns() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        button_grid([("a", "b")], columns=0)


def test_pagination_row_omits_the_arrow_it_cannot_use() -> None:
    first = pagination_row(previous_data=None, next_data="page:2", page=1, pages=3)
    middle = pagination_row(previous_data="page:1", next_data="page:3", page=2, pages=3)
    last = pagination_row(previous_data="page:2", next_data=None, page=3, pages=3)

    assert [label for label, _ in first] == ["1/3", "▶"]
    assert [label for label, _ in middle] == ["◀", "2/3", "▶"]
    assert [label for label, _ in last] == ["◀", "3/3"]


def test_the_page_counter_carries_an_inert_action() -> None:
    """Telegram demands callback data on every button, including a label."""
    row = pagination_row(previous_data=None, next_data=None, page=1, pages=1)

    assert row == [("1/1", "noop:")]
