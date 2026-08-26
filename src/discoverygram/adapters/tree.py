"""The client-derived folder tree.

NoteDiscovery has no tree endpoint. `GET /api/notes` returns every note record
*and* the vault's folder list, which is enough to rebuild the hierarchy in one
call — including folders that hold no notes, which a path-only derivation would
lose.

The tree is cached with a TTL and dropped on every write, so a note created from
Telegram shows up in `/browse` immediately rather than after the TTL expires.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Iterable

from discoverygram.ports.model import NoteListing, NoteRef, TreeNode
from discoverygram.util.paths import parent_folder


def build_tree(notes: Iterable[NoteRef], folders: Iterable[str] = ()) -> TreeNode:
    """Assemble a `TreeNode` hierarchy from flat note paths and folder paths."""
    children: dict[str, set[str]] = {"": set()}
    contents: dict[str, list[NoteRef]] = {"": []}

    def ensure(folder: str) -> None:
        if folder in children:
            return
        children[folder] = set()
        contents.setdefault(folder, [])
        parent = parent_folder(folder)
        ensure(parent)
        children[parent].add(folder)

    for folder in folders:
        if folder:
            ensure(folder)

    for note in notes:
        folder = note.folder or parent_folder(note.path)
        if folder:
            ensure(folder)
        contents.setdefault(folder, []).append(note)

    def assemble(folder: str) -> TreeNode:
        name = folder.rpartition("/")[2] if folder else ""
        sub = sorted(children.get(folder, set()), key=str.casefold)
        notes_here = sorted(contents.get(folder, []), key=lambda ref: ref.title.casefold())
        return TreeNode(
            path=folder,
            name=name,
            folders=tuple(assemble(child) for child in sub),
            notes=tuple(notes_here),
        )

    return assemble("")


def find_node(root: TreeNode, path: str) -> TreeNode | None:
    """Locate a folder in the tree by its vault-relative path."""
    if path in ("", "."):
        return root
    node = root
    for segment in path.strip("/").split("/"):
        for child in node.folders:
            if child.name == segment:
                node = child
                break
        else:
            return None
    return node


def breadcrumb(path: str) -> tuple[tuple[str, str], ...]:
    """`Projects/2026/Q1` -> `(("", "🏠"), ("Projects", "Projects"), ...)` pairs.

    Each pair is `(path, label)`, ready to become a navigation button in phase 4.
    """
    crumbs: list[tuple[str, str]] = [("", "🏠")]
    accumulated = ""
    for segment in [part for part in path.split("/") if part]:
        accumulated = f"{accumulated}/{segment}" if accumulated else segment
        crumbs.append((accumulated, segment))
    return tuple(crumbs)


class TreeCache:
    """TTL cache around one `NoteListing` fetch, safe under concurrent callers."""

    def __init__(
        self,
        loader: Callable[[], Awaitable[NoteListing]],
        *,
        ttl_s: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._loader = loader
        self._ttl_s = ttl_s
        self._clock = clock
        self._lock = asyncio.Lock()
        self._tree: TreeNode | None = None
        self._loaded_at = 0.0

    @property
    def is_fresh(self) -> bool:
        if self._tree is None:
            return False
        if self._ttl_s <= 0:
            return False
        return (self._clock() - self._loaded_at) < self._ttl_s

    async def get(self, *, refresh: bool = False) -> TreeNode:
        if not refresh and self.is_fresh and self._tree is not None:
            return self._tree

        async with self._lock:
            # Another caller may have refreshed it while we waited for the lock.
            if not refresh and self.is_fresh and self._tree is not None:
                return self._tree
            listing = await self._loader()
            self._tree = build_tree(listing.notes, listing.folders)
            self._loaded_at = self._clock()
            return self._tree

    def invalidate(self) -> None:
        self._tree = None
        self._loaded_at = 0.0
