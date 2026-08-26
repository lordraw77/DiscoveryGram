"""The normalised domain model.

NoteDiscovery's JSON is inconsistent across endpoints — `name` is sometimes the
file stem and sometimes the file name, timestamps are ISO strings, search
snippets are HTML fragments, tags come back as a `{tag: count}` map. Everything
is normalised here once so the application layer sees one shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from discoverygram.util.paths import note_title, parent_folder


@dataclass(frozen=True, slots=True)
class NoteRef:
    """A note without its body — enough to list, rank and render a result row."""

    path: str
    title: str
    folder: str = ""
    modified: datetime | None = None
    size: int = 0
    tags: tuple[str, ...] = ()

    @classmethod
    def from_path(cls, path: str) -> NoteRef:
        return cls(path=path, title=note_title(path), folder=parent_folder(path))


@dataclass(frozen=True, slots=True)
class SearchMatch:
    """One matched line, already stripped of NoteDiscovery's HTML highlighting."""

    line_number: int
    snippet: str
    matched_text: str = ""


@dataclass(frozen=True, slots=True)
class Backlink:
    """A note linking to another one, with the lines where the link appears."""

    path: str
    title: str
    references: tuple[SearchMatch, ...] = ()


@dataclass(frozen=True, slots=True)
class Note:
    """A note with its body."""

    ref: NoteRef
    content: str
    created: datetime | None = None
    modified: datetime | None = None
    lines: int = 0
    backlinks: tuple[Backlink, ...] = ()

    @property
    def path(self) -> str:
        return self.ref.path

    @property
    def title(self) -> str:
        return self.ref.title

    @property
    def tags(self) -> tuple[str, ...]:
        return self.ref.tags


@dataclass(frozen=True, slots=True)
class SearchHit:
    """A search result.

    `score` is computed **client-side**: NoteDiscovery returns no relevance
    signal at all. It exists to order results, never to be shown as a percentage.
    """

    ref: NoteRef
    matches: tuple[SearchMatch, ...] = ()
    score: float = 0.0
    title_match: bool = False

    @property
    def path(self) -> str:
        return self.ref.path


@dataclass(frozen=True, slots=True)
class NoteListing:
    """`GET /api/notes` — the note records plus the vault's folder paths."""

    notes: tuple[NoteRef, ...] = ()
    folders: tuple[str, ...] = ()
    total: int | None = None


@dataclass(frozen=True, slots=True)
class TreeNode:
    """A folder in the client-derived tree. NoteDiscovery exposes no tree endpoint."""

    path: str
    name: str
    folders: tuple[TreeNode, ...] = ()
    notes: tuple[NoteRef, ...] = ()

    @property
    def is_root(self) -> bool:
        return self.path == ""

    @property
    def child_count(self) -> int:
        return len(self.folders) + len(self.notes)


@dataclass(frozen=True, slots=True)
class GraphNode:
    id: str
    label: str


@dataclass(frozen=True, slots=True)
class GraphEdge:
    source: str
    target: str
    type: str = ""


@dataclass(frozen=True, slots=True)
class Graph:
    nodes: tuple[GraphNode, ...] = ()
    edges: tuple[GraphEdge, ...] = ()

    def neighbours(self, path: str) -> tuple[str, ...]:
        """Notes adjacent to `path` in either direction, de-duplicated, order kept."""
        seen: dict[str, None] = {}
        for edge in self.edges:
            if edge.source == path:
                seen.setdefault(edge.target, None)
            elif edge.target == path:
                seen.setdefault(edge.source, None)
        return tuple(seen)


@dataclass(frozen=True, slots=True)
class TemplateRef:
    name: str
    path: str = ""
    modified: datetime | None = None


@dataclass(frozen=True, slots=True)
class Template:
    name: str
    content: str


@dataclass(frozen=True, slots=True)
class MediaUpload:
    path: str
    filename: str
    media_type: str = ""


@dataclass(frozen=True, slots=True)
class ShareLink:
    url: str
    token: str
    path: str
    theme: str = "light"


@dataclass(frozen=True, slots=True)
class VaultStats:
    notes_count: int = 0
    folders_count: int = 0
    tags_count: int = 0
    templates_count: int = 0
    media_count: int = 0
    total_size_bytes: int = 0
    last_modified: datetime | None = None
    plugins_enabled: int = 0
    version: str = "unknown"


@dataclass(frozen=True, slots=True)
class InstanceConfig:
    """What `GET /api/config` and `GET /health` tell us at startup.

    `min_query_length` is **not** exposed by the API: it is a server constant
    (2 in 0.31.3). It is carried here so the value has one home, sourced from
    `SEARCH_MIN_QUERY_LENGTH` in the bot's own configuration.
    """

    name: str = "NoteDiscovery"
    version: str = "unknown"
    search_enabled: bool = True
    auth_enabled: bool = False
    demo_mode: bool = False
    min_query_length: int = 2
    reachable: bool = True
    raw: dict[str, object] = field(default_factory=dict)
