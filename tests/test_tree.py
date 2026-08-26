"""The client-derived folder tree and its cache.

NoteDiscovery has no tree endpoint, so this is the whole of `/browse`'s backing
data structure.
"""

from __future__ import annotations

from discoverygram.adapters.tree import TreeCache, breadcrumb, build_tree, find_node
from discoverygram.ports.model import NoteListing, NoteRef

NOTES = (
    NoteRef.from_path("Welcome.md"),
    NoteRef.from_path("Projects/Roadmap.md"),
    NoteRef.from_path("Projects/Ideas.md"),
    NoteRef.from_path("Journal/2026/Daily.md"),
)


def test_build_tree_nests_folders_from_flat_paths() -> None:
    root = build_tree(NOTES)

    assert root.is_root
    assert [note.path for note in root.notes] == ["Welcome.md"]
    assert [folder.name for folder in root.folders] == ["Journal", "Projects"]

    journal = find_node(root, "Journal")
    assert journal is not None
    assert [folder.path for folder in journal.folders] == ["Journal/2026"]


def test_build_tree_keeps_empty_folders_from_the_folders_list() -> None:
    """`GET /api/notes` reports folders separately; deriving from paths alone loses them."""
    root = build_tree(NOTES, ["Projects", "Journal", "Journal/2026", "Archive"])

    assert find_node(root, "Archive") is not None
    assert find_node(root, "Archive").child_count == 0  # type: ignore[union-attr]


def test_build_tree_sorts_case_insensitively() -> None:
    root = build_tree(
        [NoteRef.from_path("zebra.md"), NoteRef.from_path("Apple.md")],
        ["beta", "Alpha"],
    )

    assert [folder.name for folder in root.folders] == ["Alpha", "beta"]
    assert [note.title for note in root.notes] == ["Apple", "zebra"]


def test_find_node_returns_none_for_a_missing_folder() -> None:
    root = build_tree(NOTES)

    assert find_node(root, "Nope/Nowhere") is None
    assert find_node(root, "") is root


def test_breadcrumb_yields_navigable_pairs() -> None:
    assert breadcrumb("Journal/2026") == (
        ("", "🏠"),
        ("Journal", "Journal"),
        ("Journal/2026", "2026"),
    )
    assert breadcrumb("") == (("", "🏠"),)


class Clock:
    """A hand-cranked monotonic clock, so TTL tests do not sleep."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


async def test_tree_cache_serves_a_second_call_from_memory() -> None:
    calls = 0

    async def loader() -> NoteListing:
        nonlocal calls
        calls += 1
        return NoteListing(notes=NOTES)

    cache = TreeCache(loader, ttl_s=300, clock=Clock())

    await cache.get()
    await cache.get()

    assert calls == 1


async def test_tree_cache_reloads_after_the_ttl() -> None:
    calls = 0
    clock = Clock()

    async def loader() -> NoteListing:
        nonlocal calls
        calls += 1
        return NoteListing(notes=NOTES)

    cache = TreeCache(loader, ttl_s=300, clock=clock)

    await cache.get()
    clock.now = 301.0
    await cache.get()

    assert calls == 2


async def test_invalidate_forces_a_reload() -> None:
    """Every write invalidates: a note created from Telegram must appear at once."""
    calls = 0

    async def loader() -> NoteListing:
        nonlocal calls
        calls += 1
        return NoteListing(notes=NOTES)

    cache = TreeCache(loader, ttl_s=300, clock=Clock())

    await cache.get()
    cache.invalidate()
    await cache.get()

    assert calls == 2


async def test_zero_ttl_disables_caching() -> None:
    calls = 0

    async def loader() -> NoteListing:
        nonlocal calls
        calls += 1
        return NoteListing(notes=NOTES)

    cache = TreeCache(loader, ttl_s=0, clock=Clock())

    await cache.get()
    await cache.get()

    assert calls == 2


async def test_concurrent_callers_share_one_load() -> None:
    """Ten users tapping /browse at once must not trigger ten vault scans."""
    import asyncio

    calls = 0

    async def loader() -> NoteListing:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        return NoteListing(notes=NOTES)

    cache = TreeCache(loader, ttl_s=300, clock=Clock())

    await asyncio.gather(*(cache.get() for _ in range(10)))

    assert calls == 1
