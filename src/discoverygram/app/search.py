"""Search use cases.

Four modes, of which NoteDiscovery natively provides one:

| Mode | Backed by |
|---|---|
| full text | `GET /api/search`, ranked client-side |
| literal | the same call, filtered case-sensitively client-side |
| tag | `GET /api/tags/{tag}` |
| recent | the note listing's `modified` timestamps (REST) or `get_recent_notes` (MCP) |

Nothing here knows about Telegram. A mode that cannot run — a query below the
instance's floor, search disabled server-side — returns an outcome carrying a
`notice` rather than raising, because "search is off on this instance" is an
answer, not an error.

Results are ordered by `NoteStore` and re-ordered by nothing here: ranking
already happened in the adapter, and re-sorting would break the pagination
guarantee that page 2 follows page 1.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from discoverygram.app.probe import InstanceState
from discoverygram.config import Settings
from discoverygram.ports.errors import Forbidden
from discoverygram.ports.model import NoteRef, SearchHit, SearchMatch
from discoverygram.ports.note_store import NoteStore
from discoverygram.util.logging import get_logger

log = get_logger(__name__)


class SearchMode(StrEnum):
    FULL_TEXT = "search"
    LITERAL = "find"
    TAG = "tag"
    RECENT = "recent"

    @property
    def label(self) -> str:
        return {
            SearchMode.FULL_TEXT: "Search",
            SearchMode.LITERAL: "Literal search",
            SearchMode.TAG: "Tag",
            SearchMode.RECENT: "Recent",
        }[self]


@dataclass(frozen=True, slots=True)
class SearchOutcome:
    """What a search produced, including why it produced nothing."""

    mode: SearchMode
    query: str
    hits: tuple[SearchHit, ...] = ()
    # Non-empty when the search could not run as asked. Rendered instead of
    # results, so the user learns *why* rather than seeing an empty list.
    notice: str = ""
    truncated: bool = False

    @property
    def ran(self) -> bool:
        return not self.notice

    @property
    def is_empty(self) -> bool:
        return not self.hits


class SearchService:
    """The four search modes, and the rules about when they may run."""

    def __init__(self, notes: NoteStore, settings: Settings, instance: InstanceState) -> None:
        self._notes = notes
        self._settings = settings
        self._instance = instance

    @property
    def instance(self) -> InstanceState:
        return self._instance

    def _limit(self) -> int:
        return self._settings.search_default_limit

    def _check_query(self, query: str) -> str:
        """The notice explaining why this query cannot be searched, or `""`."""
        text = query.strip()
        if not text:
            return "Give me something to search for."

        minimum = self._settings.search_min_query_length
        if len(text) < minimum:
            return f"That query is too short. This instance needs at least {minimum} characters."
        return ""

    def _check_search_enabled(self) -> str:
        if not self._instance.config.search_enabled:
            return self._instance.why_search_unavailable() or "Search is disabled."
        return ""

    async def full_text(self, query: str) -> SearchOutcome:
        return await self._text_search(SearchMode.FULL_TEXT, query)

    async def literal(self, query: str) -> SearchOutcome:
        return await self._text_search(SearchMode.LITERAL, query)

    async def _text_search(self, mode: SearchMode, query: str) -> SearchOutcome:
        text = query.strip()

        notice = self._check_search_enabled() or self._check_query(text)
        if notice:
            return SearchOutcome(mode=mode, query=text, notice=notice)

        limit = self._limit()
        try:
            if mode is SearchMode.LITERAL:
                hits = await self._notes.search_literal(text, limit=limit)
            else:
                hits = await self._notes.search(text, limit=limit)
        except Forbidden as exc:
            # The instance disabled search after our startup probe read it.
            # Believe the instance, not the cached probe.
            log.warning("search_forbidden_at_call_time", error=str(exc))
            return SearchOutcome(
                mode=mode,
                query=text,
                notice="Search is disabled on this NoteDiscovery instance.",
            )

        return SearchOutcome(
            mode=mode,
            query=text,
            hits=tuple(hits),
            truncated=len(hits) >= limit,
        )

    async def by_tag(self, tag: str) -> SearchOutcome:
        """Tag search needs no search endpoint, so it works on a search-disabled instance."""
        name = tag.strip().lstrip("#")
        if not name:
            return SearchOutcome(
                mode=SearchMode.TAG, query="", notice="Which tag? Try /tag to list them."
            )

        limit = self._limit()
        refs = await self._notes.get_notes_by_tag(name, limit=limit)
        return SearchOutcome(
            mode=SearchMode.TAG,
            query=name,
            hits=tuple(SearchHit(ref=ref) for ref in refs),
            truncated=len(refs) >= limit,
        )

    async def recent(self, days: int | None = None, limit: int | None = None) -> SearchOutcome:
        window = days if days is not None else self._settings.recent_default_days
        count = limit if limit is not None else self._limit()
        refs = await self._notes.recent_notes(days=window, limit=count)
        return SearchOutcome(
            mode=SearchMode.RECENT,
            query=str(window),
            hits=tuple(SearchHit(ref=ref) for ref in refs),
            truncated=len(refs) >= count,
        )

    async def tags(self) -> dict[str, int]:
        """Every tag with its note count, most used first."""
        counts = await self._notes.list_tags()
        return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0].casefold())))


# --- Session serialisation ------------------------------------------------
#
# A result set is stored whole so page turns cost nothing and never re-run the
# query against a vault that may have changed underneath. The session store is
# JSON-only, so the domain objects round-trip through plain mappings here.


def _match_to_payload(match: SearchMatch) -> dict[str, Any]:
    return {"l": match.line_number, "s": match.snippet, "m": match.matched_text}


def _match_from_payload(payload: dict[str, Any]) -> SearchMatch:
    return SearchMatch(
        line_number=int(payload.get("l", 0)),
        snippet=str(payload.get("s", "")),
        matched_text=str(payload.get("m", "")),
    )


def hit_to_payload(hit: SearchHit) -> dict[str, Any]:
    ref = hit.ref
    return {
        "p": ref.path,
        "t": ref.title,
        "f": ref.folder,
        "d": ref.modified.isoformat() if ref.modified else None,
        "g": list(ref.tags),
        "m": [_match_to_payload(match) for match in hit.matches],
    }


def hit_from_payload(payload: dict[str, Any]) -> SearchHit:
    raw_modified = payload.get("d")
    modified: datetime | None = None
    if isinstance(raw_modified, str):
        try:
            modified = datetime.fromisoformat(raw_modified)
        except ValueError:
            modified = None

    raw_matches = payload.get("m", [])
    matches = (
        tuple(_match_from_payload(item) for item in raw_matches if isinstance(item, dict))
        if isinstance(raw_matches, list)
        else ()
    )
    raw_tags = payload.get("g", [])
    return SearchHit(
        ref=NoteRef(
            path=str(payload.get("p", "")),
            title=str(payload.get("t", "")),
            folder=str(payload.get("f", "")),
            modified=modified,
            tags=tuple(str(tag) for tag in raw_tags) if isinstance(raw_tags, list) else (),
        ),
        matches=matches,
    )


@dataclass(frozen=True, slots=True)
class ResultSet:
    """A stored outcome plus the paging arithmetic over it."""

    mode: SearchMode
    query: str
    hits: tuple[SearchHit, ...]
    page_size: int
    truncated: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not self.hits

    @property
    def pages(self) -> int:
        if not self.hits:
            return 1
        return -(-len(self.hits) // self.page_size)  # ceiling division

    def clamp(self, page: int) -> int:
        """Keep a page number inside the result set, whatever the callback said."""
        return max(1, min(page, self.pages))

    def page(self, number: int) -> tuple[SearchHit, ...]:
        page = self.clamp(number)
        start = (page - 1) * self.page_size
        return self.hits[start : start + self.page_size]

    def to_payload(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "query": self.query,
            "page_size": self.page_size,
            "truncated": self.truncated,
            "hits": [hit_to_payload(hit) for hit in self.hits],
            "extra": self.extra,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> ResultSet:
        raw_hits = payload.get("hits", [])
        extra = payload.get("extra", {})
        return cls(
            mode=SearchMode(str(payload.get("mode", SearchMode.FULL_TEXT.value))),
            query=str(payload.get("query", "")),
            hits=tuple(hit_from_payload(item) for item in raw_hits if isinstance(item, dict))
            if isinstance(raw_hits, list)
            else (),
            page_size=max(1, int(payload.get("page_size", 5))),
            truncated=bool(payload.get("truncated", False)),
            extra=extra if isinstance(extra, dict) else {},
        )

    @classmethod
    def of(cls, outcome: SearchOutcome, *, page_size: int) -> ResultSet:
        return cls(
            mode=outcome.mode,
            query=outcome.query,
            hits=outcome.hits,
            page_size=page_size,
            truncated=outcome.truncated,
        )


__all__ = [
    "ResultSet",
    "SearchMode",
    "SearchOutcome",
    "SearchService",
    "hit_from_payload",
    "hit_to_payload",
]
