"""Navigation use cases: browsing the tree, opening notes, and the links between them.

The tree is **derived client-side** — NoteDiscovery has no tree endpoint — from
the one `GET /api/notes` call that returns both the note records and the vault's
folder list. It is cached and dropped on every write, so a note created from
Telegram shows up immediately.

Nothing here knows about Telegram. Paging arithmetic lives with the view object
so the handler never computes an offset.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from discoverygram.adapters.tree import breadcrumb, find_node
from discoverygram.config import Settings
from discoverygram.ports.errors import NoteStoreError, NotFound, Unsupported
from discoverygram.ports.model import Backlink, Note, NoteRef, TreeNode
from discoverygram.ports.note_store import NoteStore
from discoverygram.util.logging import get_logger
from discoverygram.util.paths import normalise_note_path, note_title, parent_folder

log = get_logger(__name__)

# `[[target]]` and `[[target|label]]`, the wiki-link syntax NoteDiscovery indexes.
WIKI_LINK = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")

# More than this on one note and the keyboard is unusable.
MAX_WIKI_LINK_BUTTONS = 8


class EntryKind(StrEnum):
    FOLDER = "folder"
    NOTE = "note"


@dataclass(frozen=True, slots=True)
class Entry:
    """One row in a folder listing: a subfolder or a note."""

    kind: EntryKind
    path: str
    title: str
    children: int = 0

    @property
    def is_folder(self) -> bool:
        return self.kind is EntryKind.FOLDER


@dataclass(frozen=True, slots=True)
class FolderView:
    """A folder's children, with the paging arithmetic over them."""

    path: str
    entries: tuple[Entry, ...]
    page_size: int

    @property
    def crumbs(self) -> tuple[tuple[str, str], ...]:
        return breadcrumb(self.path)

    @property
    def is_root(self) -> bool:
        return self.path == ""

    @property
    def parent(self) -> str:
        return parent_folder(self.path)

    @property
    def name(self) -> str:
        return self.path.rpartition("/")[2] if self.path else "Vault root"

    @property
    def is_empty(self) -> bool:
        return not self.entries

    @property
    def pages(self) -> int:
        if not self.entries:
            return 1
        return -(-len(self.entries) // self.page_size)

    def clamp(self, page: int) -> int:
        return max(1, min(page, self.pages))

    def page(self, number: int) -> tuple[Entry, ...]:
        page = self.clamp(number)
        start = (page - 1) * self.page_size
        return self.entries[start : start + self.page_size]


@dataclass(frozen=True, slots=True)
class WikiLink:
    """A `[[link]]` in a note body, and the note it resolves to."""

    target: str
    label: str
    path: str = ""

    @property
    def resolved(self) -> bool:
        return bool(self.path)


class NavigationService:
    """Browsing, opening and the links between notes."""

    def __init__(self, notes: NoteStore, settings: Settings) -> None:
        self._notes = notes
        self._settings = settings

    # --- Browsing --------------------------------------------------------

    async def folder(self, path: str = "", *, refresh: bool = False) -> FolderView:
        """The children of `path`. Folders first, then notes, both by name."""
        tree = await self._notes.get_tree(refresh=refresh)
        node = find_node(tree, path)
        if node is None:
            raise NotFound(f"No folder at {path}")

        entries = [
            Entry(
                kind=EntryKind.FOLDER,
                path=child.path,
                title=child.name,
                children=child.child_count,
            )
            for child in node.folders
        ]
        entries += [
            Entry(kind=EntryKind.NOTE, path=note.path, title=note.title) for note in node.notes
        ]
        return FolderView(
            path=node.path, entries=tuple(entries), page_size=self._settings.tree_page_size
        )

    async def siblings(self, note_path: str) -> FolderView:
        """The folder a note lives in — the "up" step from a note."""
        return await self.folder(parent_folder(normalise_note_path(note_path)))

    # --- Opening ---------------------------------------------------------

    async def open_note(self, path: str) -> Note:
        return await self._notes.get_note(normalise_note_path(path))

    # --- Links -----------------------------------------------------------

    async def backlinks(self, path: str) -> list[Backlink]:
        return await self._notes.get_backlinks(normalise_note_path(path))

    async def related(self, path: str) -> list[NoteRef]:
        """Graph-adjacent notes, in either direction.

        `GET /api/graph` returns the whole vault graph, which is why this is a
        command rather than something rendered on every note.
        """
        note_path = normalise_note_path(path)
        graph = await self._notes.get_graph()
        labels = {node.id: node.label for node in graph.nodes}
        return [
            NoteRef(
                path=neighbour,
                title=labels.get(neighbour) or note_title(neighbour),
                folder=parent_folder(neighbour),
            )
            for neighbour in graph.neighbours(note_path)
        ]

    async def wiki_links(self, note: Note) -> list[WikiLink]:
        """The `[[links]]` in a note body, resolved against the real tree.

        NoteDiscovery resolves these by file stem, path, or path without the
        extension; the same three rules are applied here so a button goes where
        the vault's own backlink index says it goes.
        """
        found: dict[str, WikiLink] = {}
        for match in WIKI_LINK.finditer(note.content):
            target = match.group(1).strip()
            if not target or target in found:
                continue
            found[target] = WikiLink(target=target, label=(match.group(2) or target).strip())
            if len(found) >= MAX_WIKI_LINK_BUTTONS:
                break

        if not found:
            return []

        index = await self._note_index()
        return [
            WikiLink(target=link.target, label=link.label, path=_resolve(link.target, index))
            for link in found.values()
        ]

    async def _note_index(self) -> dict[str, str]:
        """Lower-cased lookup keys to real paths, for wiki-link resolution."""
        tree = await self._notes.get_tree()
        index: dict[str, str] = {}
        for ref in _walk_notes(tree):
            path = ref.path
            for key in (path.lower(), path.lower().removesuffix(".md"), note_title(path).lower()):
                # First writer wins, so a stem shared by two notes resolves to
                # the one that sorts first rather than flipping between them.
                index.setdefault(key, path)
        return index

    # --- Folder operations (REST only) -----------------------------------

    async def create_folder(self, path: str) -> str:
        created = await self._notes.create_folder(path)
        log.info("folder_created", path=created)
        return created

    async def rename_folder(self, old: str, new: str) -> str:
        result = await self._notes.rename_folder(old, new)
        log.info("folder_renamed", old=old, new=result)
        return result

    async def move_folder(self, old: str, new: str) -> str:
        result = await self._notes.move_folder(old, new)
        log.info("folder_moved", old=old, new=result)
        return result

    async def delete_folder(self, path: str) -> None:
        await self._notes.delete_folder(path)
        log.info("folder_deleted", path=path)

    async def folder_size(self, path: str) -> int:
        """How much a folder delete would destroy, for the confirmation prompt."""
        try:
            view = await self.folder(path)
        except NoteStoreError:
            return 0
        return len(view.entries)

    @staticmethod
    def unsupported_reason(error: Unsupported) -> str:
        return str(error)


def _walk_notes(node: TreeNode) -> list[NoteRef]:
    notes = list(node.notes)
    for child in node.folders:
        notes.extend(_walk_notes(child))
    return notes


def _resolve(target: str, index: dict[str, str]) -> str:
    key = target.strip().lower()
    return index.get(key) or index.get(key.removesuffix(".md")) or index.get(f"{key}.md") or ""


__all__ = [
    "MAX_WIKI_LINK_BUTTONS",
    "Entry",
    "EntryKind",
    "FolderView",
    "NavigationService",
    "WikiLink",
]
