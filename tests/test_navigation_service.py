"""Navigation use cases: the derived tree, wiki-link resolution and folder ops."""

from __future__ import annotations

import pytest

from discoverygram.adapters.tree import build_tree
from discoverygram.app.navigation import (
    MAX_WIKI_LINK_BUTTONS,
    EntryKind,
    NavigationService,
)
from discoverygram.config import Settings
from discoverygram.ports.errors import NotFound, Unsupported
from discoverygram.ports.model import (
    Backlink,
    Graph,
    GraphEdge,
    GraphNode,
    Note,
    NoteRef,
    SearchMatch,
    TreeNode,
)

PATHS = [
    "Welcome.md",
    "Projects/Roadmap.md",
    "Projects/Ideas.md",
    "Projects/2026/Q1.md",
    "Journal/Daily.md",
]
FOLDERS = ["Projects", "Projects/2026", "Journal", "Archive"]


class StubNoteStore:
    def __init__(
        self,
        *,
        tree: TreeNode | None = None,
        note: Note | None = None,
        graph: Graph | None = None,
        backlinks: list[Backlink] | None = None,
        raises: Exception | None = None,
    ) -> None:
        self._tree = tree or build_tree([NoteRef.from_path(path) for path in PATHS], FOLDERS)
        self._note = note
        self._graph = graph or Graph()
        self._backlinks = backlinks or []
        self._raises = raises
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    async def get_tree(self, *, refresh: bool = False) -> TreeNode:
        self.calls.append(("get_tree", (str(refresh),)))
        return self._tree

    async def get_note(self, path: str, *, include_backlinks: bool = True) -> Note:
        self.calls.append(("get_note", (path,)))
        return self._note or Note(ref=NoteRef.from_path(path), content="")

    async def get_backlinks(self, path: str) -> list[Backlink]:
        self.calls.append(("get_backlinks", (path,)))
        return list(self._backlinks)

    async def get_graph(self) -> Graph:
        self.calls.append(("get_graph", ()))
        return self._graph

    async def create_folder(self, path: str) -> str:
        self.calls.append(("create_folder", (path,)))
        if self._raises:
            raise self._raises
        return path

    async def rename_folder(self, old: str, new: str) -> str:
        self.calls.append(("rename_folder", (old, new)))
        if self._raises:
            raise self._raises
        return new

    async def move_folder(self, old: str, new: str) -> str:
        self.calls.append(("move_folder", (old, new)))
        if self._raises:
            raise self._raises
        return new

    async def delete_folder(self, path: str) -> None:
        self.calls.append(("delete_folder", (path,)))
        if self._raises:
            raise self._raises


def service(settings: Settings, store: StubNoteStore) -> NavigationService:
    return NavigationService(store, settings)  # type: ignore[arg-type]


# --- Browsing -------------------------------------------------------------


async def test_the_root_lists_folders_before_notes(settings: Settings) -> None:
    view = await service(settings, StubNoteStore()).folder("")

    kinds = [entry.kind for entry in view.entries]
    assert kinds == sorted(kinds, key=lambda kind: kind is EntryKind.NOTE)
    assert view.is_root


async def test_an_empty_folder_is_still_reachable(settings: Settings) -> None:
    """`GET /api/notes` lists folders separately, so empty ones exist in the tree."""
    view = await service(settings, StubNoteStore()).folder("Archive")

    assert view.is_empty
    assert view.path == "Archive"


async def test_a_folder_reports_its_children_count(settings: Settings) -> None:
    view = await service(settings, StubNoteStore()).folder("")

    projects = next(entry for entry in view.entries if entry.title == "Projects")
    assert projects.children == 3  # 2026, Roadmap, Ideas


async def test_a_missing_folder_is_not_found(settings: Settings) -> None:
    with pytest.raises(NotFound):
        await service(settings, StubNoteStore()).folder("Nowhere/At/All")


async def test_breadcrumbs_lead_back_to_the_root(settings: Settings) -> None:
    view = await service(settings, StubNoteStore()).folder("Projects/2026")

    assert [path for path, _ in view.crumbs] == ["", "Projects", "Projects/2026"]
    assert view.parent == "Projects"


async def test_siblings_of_a_note_are_its_folder(settings: Settings) -> None:
    view = await service(settings, StubNoteStore()).siblings("Projects/Roadmap")

    assert view.path == "Projects"
    assert any(entry.title == "Ideas" for entry in view.entries)


async def test_every_note_is_reachable_from_the_root_in_bounded_taps(
    settings: Settings,
) -> None:
    """The Definition of Done: depth is the path depth, not the vault size."""
    navigation = service(settings, StubNoteStore())

    for path in PATHS:
        folder = ""
        for segment in path.split("/")[:-1]:
            folder = f"{folder}/{segment}" if folder else segment
            view = await navigation.folder(folder)
            assert view.path == folder
        leaf = await navigation.folder(folder)
        assert any(entry.path == path for entry in leaf.entries)


# --- Paging ---------------------------------------------------------------


async def test_folder_pages_cover_the_children_without_gaps(settings: Settings) -> None:
    paths = [f"Big/n{index}.md" for index in range(23)]
    store = StubNoteStore(tree=build_tree([NoteRef.from_path(p) for p in paths], ["Big"]))
    small = settings.model_copy(update={"tree_page_size": 5})

    view = await service(small, store).folder("Big")
    seen = [e.path for page in range(1, view.pages + 1) for e in view.page(page)]

    assert view.pages == 5
    assert sorted(seen) == sorted(paths)


async def test_an_out_of_range_folder_page_is_clamped(settings: Settings) -> None:
    view = await service(settings, StubNoteStore()).folder("")

    assert view.page(0) == view.page(1)
    assert view.page(999) == view.page(view.pages)


# --- Wiki links -----------------------------------------------------------


async def test_a_wiki_link_resolves_by_stem(settings: Settings) -> None:
    note = Note(ref=NoteRef.from_path("a.md"), content="see [[Roadmap]] for the plan")

    links = await service(settings, StubNoteStore(note=note)).wiki_links(note)

    assert [link.path for link in links] == ["Projects/Roadmap.md"]


async def test_a_wiki_link_resolves_by_full_path(settings: Settings) -> None:
    note = Note(ref=NoteRef.from_path("a.md"), content="[[Projects/Ideas.md]]")

    links = await service(settings, StubNoteStore(note=note)).wiki_links(note)

    assert links[0].path == "Projects/Ideas.md"


async def test_a_labelled_wiki_link_keeps_its_label(settings: Settings) -> None:
    note = Note(ref=NoteRef.from_path("a.md"), content="[[Roadmap|the plan]]")

    links = await service(settings, StubNoteStore(note=note)).wiki_links(note)

    assert links[0].label == "the plan"
    assert links[0].target == "Roadmap"


async def test_a_broken_wiki_link_is_reported_not_dropped(settings: Settings) -> None:
    """A dangling link is a fact about the vault worth surfacing."""
    note = Note(ref=NoteRef.from_path("a.md"), content="[[Nowhere]]")

    links = await service(settings, StubNoteStore(note=note)).wiki_links(note)

    assert links[0].resolved is False


async def test_repeated_wiki_links_appear_once(settings: Settings) -> None:
    note = Note(ref=NoteRef.from_path("a.md"), content="[[Roadmap]] and [[Roadmap]] again")

    links = await service(settings, StubNoteStore(note=note)).wiki_links(note)

    assert len(links) == 1


async def test_wiki_link_buttons_are_capped(settings: Settings) -> None:
    """A note that links to forty others must not produce forty buttons."""
    body = " ".join(f"[[note{index}]]" for index in range(40))
    note = Note(ref=NoteRef.from_path("a.md"), content=body)

    links = await service(settings, StubNoteStore(note=note)).wiki_links(note)

    assert len(links) <= MAX_WIKI_LINK_BUTTONS


async def test_a_note_without_links_costs_no_tree_read(settings: Settings) -> None:
    store = StubNoteStore()
    note = Note(ref=NoteRef.from_path("a.md"), content="no links here")

    await service(settings, store).wiki_links(note)

    assert [name for name, _ in store.calls] == []


# --- Backlinks and related ------------------------------------------------


async def test_backlinks_are_passed_through(settings: Settings) -> None:
    store = StubNoteStore(
        backlinks=[Backlink(path="a.md", title="A", references=(SearchMatch(1, "[[B]]"),))]
    )

    links = await service(settings, store).backlinks("Projects/Roadmap")

    assert links[0].path == "a.md"
    assert store.calls[0] == ("get_backlinks", ("Projects/Roadmap.md",))


async def test_related_follows_edges_in_either_direction(settings: Settings) -> None:
    graph = Graph(
        nodes=(GraphNode(id="Projects/Ideas.md", label="Ideas"),),
        edges=(
            GraphEdge(source="Projects/Roadmap.md", target="Projects/Ideas.md"),
            GraphEdge(source="Welcome.md", target="Projects/Roadmap.md"),
        ),
    )

    refs = await service(settings, StubNoteStore(graph=graph)).related("Projects/Roadmap")

    assert {ref.path for ref in refs} == {"Projects/Ideas.md", "Welcome.md"}


async def test_related_falls_back_to_the_path_for_an_unlabelled_node(
    settings: Settings,
) -> None:
    graph = Graph(edges=(GraphEdge(source="a.md", target="Projects/Roadmap.md"),))

    refs = await service(settings, StubNoteStore(graph=graph)).related("Projects/Roadmap")

    assert refs[0].title == "a"


# --- Folder operations ----------------------------------------------------


async def test_folder_operations_delegate_to_the_store(settings: Settings) -> None:
    store = StubNoteStore()
    navigation = service(settings, store)

    await navigation.create_folder("New")
    await navigation.rename_folder("New", "Renamed")
    await navigation.move_folder("Renamed", "Projects/Renamed")
    await navigation.delete_folder("Projects/Renamed")

    assert [name for name, _ in store.calls] == [
        "create_folder",
        "rename_folder",
        "move_folder",
        "delete_folder",
    ]


async def test_folder_operations_report_unsupported_over_mcp(settings: Settings) -> None:
    """MCP has no folder move, rename or delete; the gap is stated, not hidden."""
    store = StubNoteStore(raises=Unsupported("Renaming a folder is not an MCP tool."))

    with pytest.raises(Unsupported):
        await service(settings, store).rename_folder("A", "B")


async def test_folder_size_counts_what_a_delete_would_destroy(settings: Settings) -> None:
    assert await service(settings, StubNoteStore()).folder_size("Projects") == 3


async def test_folder_size_of_something_missing_is_zero(settings: Settings) -> None:
    """The confirmation prompt must not fail because the folder already went."""
    assert await service(settings, StubNoteStore()).folder_size("Nowhere") == 0
