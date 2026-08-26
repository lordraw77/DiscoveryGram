"""Rendering notes and folders.

The Definition of Done says a note must render "without Telegram formatting
errors". Note bodies are arbitrary vault content, so this file leans on the
bodies most likely to break: markdown tables, code fences, every reserved
character, and text well past the 4096-character message limit.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from discoverygram.app.navigation import Entry, EntryKind, FolderView, WikiLink
from discoverygram.bot.notes import (
    BODY_BUDGET,
    body_pages,
    entry_label,
    note_header,
    render_backlinks,
    render_folder,
    render_note,
    render_note_split,
    render_related,
    unresolved_links_note,
)
from discoverygram.bot.render import MESSAGE_LIMIT
from discoverygram.ports.model import Backlink, Note, NoteRef, SearchMatch
from tests.fixtures.telegram import assert_markdown_v2_safe

RESERVED = "_*[]()~`>#+-=|{}.!"

PATHOLOGICAL = f"""# Q1 2026 — costs (draft) [v2]!

| Item | Cost |
|---|---|
| Server | $10.50 |

```python
print("hello #world")
```

Every reserved character: {RESERVED}
A [link](http://example.com) and an ![image](x.png).
Emoji 🍰 and accents: Tiramisù.
"""


def note(
    path: str = "Projects/Roadmap.md",
    *,
    content: str = "body",
    tags: tuple[str, ...] = (),
    lines: int = 3,
) -> Note:
    return Note(
        ref=NoteRef(
            path=path,
            title=path.rpartition("/")[2].removesuffix(".md"),
            folder=path.rpartition("/")[0],
            modified=datetime(2026, 8, 20, 10, tzinfo=UTC),
            tags=tags,
        ),
        content=content,
        created=datetime(2026, 8, 1, 12, tzinfo=UTC),
        modified=datetime(2026, 8, 20, 10, tzinfo=UTC),
        lines=lines,
    )


# --- Header ---------------------------------------------------------------


def test_the_header_carries_title_path_and_timestamps() -> None:
    rendered = note_header(note(tags=("planning",)))

    assert "Roadmap" in rendered
    assert "Projects/Roadmap\\.md" in rendered
    assert "2026\\-08\\-20" in rendered
    assert "planning" in rendered
    assert_markdown_v2_safe(rendered)


def test_a_note_never_edited_shows_one_timestamp() -> None:
    """Created and modified being equal is the common case; saying it twice is noise."""
    same = datetime(2026, 8, 20, 10, tzinfo=UTC)
    rendered = note_header(
        Note(ref=NoteRef.from_path("a.md"), content="", created=same, modified=same)
    )

    assert rendered.count("2026\\-08\\-20") == 1


def test_a_note_with_many_tags_shows_a_count_rather_than_all_of_them() -> None:
    rendered = note_header(note(tags=tuple(f"tag{index}" for index in range(30))))

    assert "\\+18" in rendered
    assert_markdown_v2_safe(rendered)


def test_a_pathological_title_is_still_sendable() -> None:
    rendered = note_header(note("Projects/Q1 2026 — costs (draft) [v2]!.md"))

    assert_markdown_v2_safe(rendered)


# --- Body -----------------------------------------------------------------


def test_a_pathological_body_renders_safely() -> None:
    """Tables, fences, links and every reserved character in one note."""
    rendered = render_note(note(content=PATHOLOGICAL))

    assert_markdown_v2_safe(rendered)
    assert len(rendered) <= MESSAGE_LIMIT


def test_an_empty_note_says_so_rather_than_rendering_blank() -> None:
    assert "empty" in render_note(note(content="   "))


def test_a_long_body_is_split_into_pages_that_fit() -> None:
    body = "\n\n".join(f"Paragraph {index}. " + "x" * 400 for index in range(40))

    pages = body_pages(note(content=body))

    assert len(pages) > 1
    assert all(len(page) <= BODY_BUDGET for page in pages)


def test_escaping_happens_before_chunking() -> None:
    """A chunk boundary between a backslash and its character breaks the message."""
    body = ("." * 200 + "\n\n") * 60

    for page in body_pages(note(content=body)):
        assert_markdown_v2_safe(page)


def test_every_page_of_a_long_note_fits_one_message() -> None:
    body = "\n\n".join("word " * 200 for _ in range(60))
    rendered_note = note(content=body)
    pages = body_pages(rendered_note)

    for page in range(1, len(pages) + 1):
        rendered = render_note(rendered_note, page, pages=pages)
        assert len(rendered) <= MESSAGE_LIMIT
        assert_markdown_v2_safe(rendered)


def test_a_paged_note_says_which_page_it_is_on() -> None:
    body = "\n\n".join("x" * 1000 for _ in range(20))

    assert "page 1 of" in render_note(note(content=body), 1)


def test_a_single_page_note_has_no_page_footer() -> None:
    assert "page 1 of" not in render_note(note(content="short"))


def test_an_out_of_range_page_is_clamped() -> None:
    assert render_note(note(content="short"), 99) == render_note(note(content="short"), 1)


def test_split_mode_puts_the_header_on_the_first_message_only() -> None:
    body = "\n\n".join("x" * 1000 for _ in range(20))

    messages = render_note_split(note(content=body))

    assert len(messages) > 1
    assert "Roadmap" in messages[0]
    assert "Roadmap" not in messages[1]
    for message in messages:
        assert len(message) <= MESSAGE_LIMIT
        assert_markdown_v2_safe(message)


# --- Folders --------------------------------------------------------------


def folder_view(count: int = 3, *, path: str = "Projects", page_size: int = 10) -> FolderView:
    entries = [
        Entry(kind=EntryKind.FOLDER, path=f"{path}/sub{index}", title=f"sub{index}", children=2)
        for index in range(count)
    ]
    entries += [
        Entry(kind=EntryKind.NOTE, path=f"{path}/n{index}.md", title=f"Note {index}")
        for index in range(count)
    ]
    return FolderView(path=path, entries=tuple(entries), page_size=page_size)


def test_a_folder_listing_shows_a_breadcrumb() -> None:
    rendered = render_folder(folder_view(path="Projects/2026"), 1)

    assert "Projects" in rendered
    assert "2026" in rendered
    assert_markdown_v2_safe(rendered)


def test_an_empty_folder_says_so() -> None:
    view = FolderView(path="Archive", entries=(), page_size=10)

    assert "empty" in render_folder(view, 1)


def test_a_folder_shows_how_many_items_it_holds() -> None:
    assert "6 items" in render_folder(folder_view(3), 1)


def test_a_folder_with_awkward_names_is_still_sendable() -> None:
    view = FolderView(
        path="A (b)",
        entries=(Entry(kind=EntryKind.NOTE, path="A (b)/c!.md", title="c! [draft]"),),
        page_size=10,
    )

    assert_markdown_v2_safe(render_folder(view, 1))


def test_entry_labels_fit_a_button() -> None:
    entry = Entry(kind=EntryKind.NOTE, path="a.md", title="a very long note title " * 5)

    assert len(entry_label(entry)) < 40


# --- Backlinks and related ------------------------------------------------


def test_backlinks_show_where_the_link_appears() -> None:
    links = [
        Backlink(
            path="Projects/Ideas.md",
            title="Ideas",
            references=(SearchMatch(4, "see [[Roadmap]] for the plan"),),
        )
    ]

    rendered = render_backlinks("Projects/Roadmap.md", links)

    assert "Ideas" in rendered
    assert "Roadmap" in rendered
    assert_markdown_v2_safe(rendered)


def test_no_backlinks_says_so_plainly() -> None:
    rendered = render_backlinks("Projects/Roadmap.md", [])

    assert "Nothing links to" in rendered
    assert_markdown_v2_safe(rendered)


def test_related_notes_are_listed_with_their_folders() -> None:
    refs = [NoteRef(path="Projects/Ideas.md", title="Ideas", folder="Projects")]

    rendered = render_related("Projects/Roadmap.md", refs)

    assert "Ideas" in rendered
    assert_markdown_v2_safe(rendered)


def test_nothing_related_says_so() -> None:
    assert "No notes are linked" in render_related("a.md", [])


# --- Wiki links -----------------------------------------------------------


def test_broken_links_are_named_rather_than_silently_dropped() -> None:
    links = [
        WikiLink(target="Roadmap", label="Roadmap", path="Projects/Roadmap.md"),
        WikiLink(target="Nowhere", label="Nowhere"),
    ]

    line = unresolved_links_note(links)

    assert "Nowhere" in line
    assert "Roadmap" not in line
    assert_markdown_v2_safe(line)


def test_all_links_resolving_produces_no_line() -> None:
    links = [WikiLink(target="A", label="A", path="A.md")]

    assert unresolved_links_note(links) == ""


@pytest.mark.parametrize("char", list(RESERVED))
def test_every_reserved_character_survives_a_note_body(char: str) -> None:
    """One at a time, so a failure names the character that broke it."""
    rendered = render_note(note(content=f"before {char * 3} after"))

    assert_markdown_v2_safe(rendered)
