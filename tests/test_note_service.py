"""Note mutations.

Two NoteDiscovery facts drive most of these: `PATCH` appends only, so a replace
is a read-modify-write over `POST`; and tags live in the body text rather than a
field, so adding one is an edit that has to be idempotent.
"""

from __future__ import annotations

from typing import Any

import pytest

from discoverygram.app.notes import NoteService, normalise_tag, tags_in
from discoverygram.ports.errors import InvalidRequest, NotFound
from discoverygram.ports.model import Note, NoteRef, ShareLink


class StubNoteStore:
    def __init__(self, *, content: str = "", missing: bool = False) -> None:
        self.content = content
        self.missing = missing
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def get_note(self, path: str, *, include_backlinks: bool = True) -> Note:
        self.calls.append(("get_note", (path,)))
        if self.missing:
            raise NotFound(path)
        return Note(ref=NoteRef.from_path(path), content=self.content)

    async def append_note(self, path: str, content: str, *, add_timestamp: bool = False) -> None:
        self.calls.append(("append_note", (path, content, add_timestamp)))

    async def create_note(self, path: str, content: str) -> NoteRef:
        self.calls.append(("create_note", (path, content)))
        return NoteRef.from_path(path)

    async def update_note(self, path: str, content: str) -> NoteRef:
        self.calls.append(("update_note", (path, content)))
        if self.missing:
            raise NotFound(path)
        return NoteRef.from_path(path)

    async def move_note(self, old: str, new: str) -> NoteRef:
        self.calls.append(("move_note", (old, new)))
        return NoteRef.from_path(new)

    async def delete_note(self, path: str) -> None:
        self.calls.append(("delete_note", (path,)))

    async def share_note(self, path: str, *, theme: str = "light") -> ShareLink:
        self.calls.append(("share_note", (path,)))
        return ShareLink(url="http://x/share/abc", token="abc", path=path)


def service(store: StubNoteStore) -> NoteService:
    return NoteService(store)  # type: ignore[arg-type]


# --- Append ---------------------------------------------------------------


async def test_append_timestamps_when_asked() -> None:
    store = StubNoteStore()

    await service(store).append("Journal/Daily", "a thought", timestamp=True)

    assert store.calls == [("append_note", ("Journal/Daily.md", "a thought", True))]


async def test_append_refuses_an_empty_body() -> None:
    with pytest.raises(InvalidRequest):
        await service(StubNoteStore()).append("A", "   ")


# --- Replace --------------------------------------------------------------


async def test_replace_goes_through_read_modify_write() -> None:
    """`update_note` reads first, which is what makes the next test possible."""
    store = StubNoteStore(content="old")

    result = await service(store).replace("Projects/Roadmap", "new body")

    assert store.calls == [("update_note", ("Projects/Roadmap.md", "new body"))]
    assert result.summary == "Saved."


async def test_editing_a_deleted_note_fails_rather_than_re_creating_it() -> None:
    """`POST` is an upsert, so without the read an edit would resurrect the note."""
    store = StubNoteStore(missing=True)

    with pytest.raises(NotFound):
        await service(store).replace("Gone", "text")


# --- Tags -----------------------------------------------------------------


async def test_adding_a_tag_appends_it_to_the_body() -> None:
    """Tags are body text in NoteDiscovery, not a field."""
    store = StubNoteStore(content="# Roadmap\n\nShip it.")

    result = await service(store).add_tag("Projects/Roadmap", "planning")

    _, (path, body) = store.calls[-1]
    assert path == "Projects/Roadmap.md"
    assert body.endswith("#planning\n")
    assert "Ship it." in body
    assert "Tagged" in result.summary


async def test_adding_a_tag_twice_does_not_write_it_twice() -> None:
    """A double tap must not leave the note tagged twice — the index would show both."""
    store = StubNoteStore(content="Ship it.\n\n#planning")

    result = await service(store).add_tag("Projects/Roadmap", "planning")

    assert [name for name, _ in store.calls] == ["get_note"]
    assert "Already tagged" in result.summary


async def test_tag_comparison_ignores_case() -> None:
    store = StubNoteStore(content="#Planning")

    result = await service(store).add_tag("A", "planning")

    assert "Already tagged" in result.summary


async def test_a_tag_added_to_an_empty_note_needs_no_leading_blank_lines() -> None:
    store = StubNoteStore(content="")

    await service(store).add_tag("A", "planning")

    _, (_, body) = store.calls[-1]
    assert body == "#planning\n"


def test_tags_are_read_out_of_prose() -> None:
    assert tags_in("a #planning note with #docker") == ["planning", "docker"]


def test_a_hash_inside_a_code_fence_is_not_a_tag() -> None:
    """Otherwise `Add tag` would think a shell comment was already a tag."""
    body = "before\n```bash\n# not a tag\napt install x\n```\nafter #real"

    assert tags_in(body) == ["real"]


def test_a_hash_inside_an_inline_span_is_not_a_tag() -> None:
    assert tags_in("run `#nope` then #yes") == ["yes"]


@pytest.mark.parametrize("raw", ["planning", "#planning", "  #planning  "])
def test_a_tag_is_accepted_however_it_is_typed(raw: str) -> None:
    assert normalise_tag(raw) == "planning"


@pytest.mark.parametrize("raw", ["", "#", "two words", "with[bracket]", "  "])
def test_something_that_cannot_be_a_tag_is_refused(raw: str) -> None:
    with pytest.raises(InvalidRequest):
        normalise_tag(raw)


# --- Move, delete, share --------------------------------------------------


async def test_move_normalises_both_ends() -> None:
    store = StubNoteStore()

    result = await service(store).move("Inbox/Draft", "Projects/Final")

    assert store.calls == [("move_note", ("Inbox/Draft.md", "Projects/Final.md"))]
    assert "Projects/Final.md" in result.summary


async def test_moving_a_note_onto_itself_is_refused() -> None:
    with pytest.raises(InvalidRequest):
        await service(StubNoteStore()).move("A", "A.md")


async def test_delete_names_what_it_deleted() -> None:
    store = StubNoteStore()

    result = await service(store).delete("Projects/Old")

    assert store.calls == [("delete_note", ("Projects/Old.md",))]
    assert "Projects/Old.md" in result.summary


async def test_share_returns_the_public_link() -> None:
    link = await service(StubNoteStore()).share("Projects/Roadmap")

    assert link.url.endswith("/share/abc")
