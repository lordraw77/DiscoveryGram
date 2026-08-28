"""The client-derived folder tree.

NoteDiscovery has no tree endpoint. `GET /api/notes` returns every note record
*and* the vault's folder list, which is enough to rebuild the hierarchy in one
call — including folders that hold no notes, which a path-only derivation would
lose.

The tree is cached with a TTL and dropped on every write, so a note created from
Telegram shows up in `/browse` immediately rather than after the TTL expires.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Iterable

from discoverygram.adapters.cache import TtlCache
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


class TreeCache(TtlCache[TreeNode]):
    """The folder tree, rebuilt from one `NoteListing` and cached.

    A `TtlCache` whose loader does the assembling, so the invalidation rules
    live in one place for every hot read rather than one per cache.
    """

    def __init__(
        self,
        loader: Callable[[], Awaitable[NoteListing]],
        *,
        ttl_s: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        async def load() -> TreeNode:
            listing = await loader()
            return build_tree(listing.notes, listing.folders)

        super().__init__(load, ttl_s=ttl_s, name="tree", clock=clock)
