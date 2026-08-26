"""Note mutation use cases.

Everything here writes, and every write invalidates the derived tree — which the
adapter handles, so a note created here appears in `/browse` on the next tap
rather than after the cache TTL.

Two NoteDiscovery facts shape this module:

* `PATCH` **appends only**, so replacing a body is a read-modify-write over
  `POST`, which is an upsert. The read is what makes editing a note someone
  deleted fail as `NotFound` instead of silently re-creating it.
* Tags live in the body as `#tag`, not in a field. Adding one is therefore an
  edit of the text, and it has to be idempotent — tapping `Add tag` twice with
  the same tag must not write it twice.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from discoverygram.ports.errors import InvalidRequest
from discoverygram.ports.model import Note, NoteRef, ShareLink
from discoverygram.ports.note_store import NoteStore
from discoverygram.util.logging import get_logger
from discoverygram.util.paths import normalise_note_path

log = get_logger(__name__)

# A tag as NoteDiscovery's indexer recognises it: `#` then a word, not inside a
# code fence. Unicode letters are allowed because vaults are not all English.
TAG_PATTERN = re.compile(r"#([^\s#`\[\]()]+)")
_VALID_TAG = re.compile(r"^[^\s#`\[\]()]+$")


@dataclass(frozen=True, slots=True)
class WriteResult:
    """What a mutation did, in words fit to show the user."""

    ref: NoteRef
    summary: str


class NoteService:
    """Create, replace, append, tag, move, share and delete."""

    def __init__(self, notes: NoteStore) -> None:
        self._notes = notes

    async def append(self, path: str, text: str, *, timestamp: bool = False) -> WriteResult:
        """Add to the end of a note. The only single-call write NoteDiscovery has."""
        body = text.strip()
        if not body:
            raise InvalidRequest("There is nothing to append.")

        note_path = normalise_note_path(path)
        await self._notes.append_note(note_path, body, add_timestamp=timestamp)
        log.info("note_appended", path=note_path, timestamped=timestamp)
        return WriteResult(NoteRef.from_path(note_path), "Appended.")

    async def replace(self, path: str, text: str) -> WriteResult:
        """Replace a note's whole body.

        `update_note` reads first, so an edit of a note that no longer exists
        fails rather than re-creating it from the editor's buffer.
        """
        note_path = normalise_note_path(path)
        ref = await self._notes.update_note(note_path, text)
        log.info("note_replaced", path=note_path, size=len(text))
        return WriteResult(ref, "Saved.")

    async def add_tag(self, path: str, tag: str) -> WriteResult:
        """Append `#tag` to the body, unless it is already there.

        Idempotent on purpose: a double tap must not leave the note with the tag
        twice, and NoteDiscovery would happily index both.
        """
        name = normalise_tag(tag)
        note_path = normalise_note_path(path)
        note = await self._notes.get_note(note_path, include_backlinks=False)

        if name.casefold() in {existing.casefold() for existing in tags_in(note.content)}:
            return WriteResult(note.ref, f"Already tagged #{name}.")

        body = note.content.rstrip()
        # Tags on their own trailing line, so they survive a later append.
        separator = "\n\n" if body else ""
        await self._notes.create_note(note_path, f"{body}{separator}#{name}\n")
        log.info("note_tagged", path=note_path, tag=name)
        return WriteResult(note.ref, f"Tagged #{name}.")

    async def move(self, old: str, new: str) -> WriteResult:
        source = normalise_note_path(old)
        target = normalise_note_path(new)
        if source == target:
            raise InvalidRequest("The note is already there.")
        ref = await self._notes.move_note(source, target)
        log.info("note_moved", old=source, new=target)
        return WriteResult(ref, f"Moved to {target}.")

    async def delete(self, path: str) -> WriteResult:
        note_path = normalise_note_path(path)
        await self._notes.delete_note(note_path)
        log.info("note_deleted", path=note_path)
        return WriteResult(NoteRef.from_path(note_path), f"Deleted {note_path}.")

    async def share(self, path: str) -> ShareLink:
        note_path = normalise_note_path(path)
        link = await self._notes.share_note(note_path)
        log.info("note_shared", path=note_path)
        return link

    async def read(self, path: str) -> Note:
        return await self._notes.get_note(normalise_note_path(path), include_backlinks=False)


def tags_in(content: str) -> list[str]:
    """Tags found in a note body, ignoring fenced code blocks.

    A `#comment` inside a shell snippet is not a tag, and treating it as one
    would make `Add tag` think a tag was already present.
    """
    without_fences = re.sub(r"```.*?```", "", content, flags=re.DOTALL)
    without_fences = re.sub(r"`[^`\n]*`", "", without_fences)
    return [match.group(1) for match in TAG_PATTERN.finditer(without_fences)]


def normalise_tag(tag: str) -> str:
    """Accept `planning`, `#planning` or ` #planning `; reject what cannot be one."""
    name = tag.strip().lstrip("#").strip()
    if not name or not _VALID_TAG.match(name):
        raise InvalidRequest(
            "A tag cannot contain spaces or brackets. Try something like `planning`."
        )
    return name


__all__ = ["TAG_PATTERN", "NoteService", "WriteResult", "normalise_tag", "tags_in"]
