"""Search use cases: the four modes and the rules about when they may run.

The service is where "search is disabled on this instance" becomes an answer
rather than an exception, so most of these tests are about the degraded paths.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from discoverygram.app.probe import InstanceState
from discoverygram.app.search import (
    ResultSet,
    SearchMode,
    SearchService,
    hit_from_payload,
    hit_to_payload,
)
from discoverygram.config import Settings
from discoverygram.ports.errors import Forbidden
from discoverygram.ports.model import InstanceConfig, NoteRef, SearchHit, SearchMatch


def hit(path: str, *, title: str | None = None, snippet: str = "") -> SearchHit:
    return SearchHit(
        ref=NoteRef.from_path(path) if title is None else NoteRef(path=path, title=title),
        matches=(SearchMatch(1, snippet),) if snippet else (),
    )


class StubNoteStore:
    """Records what the service asked for, and answers with whatever was set."""

    def __init__(
        self,
        *,
        hits: list[SearchHit] | None = None,
        refs: list[NoteRef] | None = None,
        tags: dict[str, int] | None = None,
        raises: Exception | None = None,
    ) -> None:
        self.hits = hits or []
        self.refs = refs or []
        self.tags_map = tags or {}
        self.raises = raises
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def search(
        self, query: str, *, limit: int | None = None, offset: int = 0
    ) -> list[SearchHit]:
        self.calls.append(("search", {"query": query, "limit": limit}))
        if self.raises:
            raise self.raises
        return list(self.hits)

    async def search_literal(self, query: str, *, limit: int | None = None) -> list[SearchHit]:
        self.calls.append(("search_literal", {"query": query, "limit": limit}))
        if self.raises:
            raise self.raises
        return list(self.hits)

    async def get_notes_by_tag(
        self, tag: str, *, limit: int | None = None, offset: int = 0
    ) -> list[NoteRef]:
        self.calls.append(("get_notes_by_tag", {"tag": tag, "limit": limit}))
        return list(self.refs)

    async def recent_notes(self, *, days: int = 7, limit: int = 20) -> list[NoteRef]:
        self.calls.append(("recent_notes", {"days": days, "limit": limit}))
        return list(self.refs)

    async def list_tags(self) -> dict[str, int]:
        self.calls.append(("list_tags", {}))
        return dict(self.tags_map)


def service(
    settings: Settings,
    store: StubNoteStore,
    *,
    search_enabled: bool = True,
    healthy: bool = True,
) -> SearchService:
    state = InstanceState(
        config=InstanceConfig(version="0.31.3", search_enabled=search_enabled),
        healthy=healthy,
    )
    return SearchService(store, settings, state)  # type: ignore[arg-type]


# --- Full-text and literal ------------------------------------------------


async def test_full_text_returns_the_adapter_s_order(settings: Settings) -> None:
    """Ranking already happened in the adapter; re-sorting would break paging."""
    store = StubNoteStore(hits=[hit("a/one.md"), hit("b/two.md")])

    outcome = await service(settings, store).full_text("docker")

    assert [h.path for h in outcome.hits] == ["a/one.md", "b/two.md"]
    assert outcome.mode is SearchMode.FULL_TEXT
    assert outcome.ran


async def test_every_search_carries_an_explicit_limit(settings: Settings) -> None:
    """`/api/search` has no server-side cap: an unbounded query returns the vault."""
    store = StubNoteStore()

    await service(settings, store).full_text("docker")

    assert store.calls[0][1]["limit"] == settings.search_default_limit


async def test_literal_uses_the_literal_path(settings: Settings) -> None:
    store = StubNoteStore(hits=[hit("a/one.md")])

    outcome = await service(settings, store).literal("Docker")

    assert store.calls[0][0] == "search_literal"
    assert outcome.mode is SearchMode.LITERAL


async def test_a_full_result_set_is_reported_as_truncated(settings: Settings) -> None:
    """The user needs to know results were cut, not assume they saw everything."""
    small = settings.model_copy(update={"search_default_limit": 2})
    store = StubNoteStore(hits=[hit("a.md"), hit("b.md")])

    outcome = await service(small, store).full_text("docker")

    assert outcome.truncated is True


# --- Degraded paths -------------------------------------------------------


async def test_a_query_below_the_minimum_length_never_reaches_the_vault(
    settings: Settings,
) -> None:
    store = StubNoteStore()

    outcome = await service(settings, store).full_text("d")

    assert not outcome.ran
    assert "too short" in outcome.notice
    assert str(settings.search_min_query_length) in outcome.notice
    assert store.calls == []


async def test_an_empty_query_asks_for_one(settings: Settings) -> None:
    outcome = await service(settings, StubNoteStore()).full_text("   ")

    assert not outcome.ran
    assert "something to search for" in outcome.notice


async def test_a_search_disabled_instance_explains_itself(settings: Settings) -> None:
    """Known from the startup probe, so no request is made at all."""
    store = StubNoteStore()

    outcome = await service(settings, store, search_enabled=False).full_text("docker")

    assert not outcome.ran
    assert "disabled" in outcome.notice
    assert store.calls == []


async def test_search_disabled_after_startup_is_believed_over_the_probe(
    settings: Settings,
) -> None:
    """The instance can be reconfigured while we run; a 403 is the truth."""
    store = StubNoteStore(raises=Forbidden("Search is disabled"))

    outcome = await service(settings, store).full_text("docker")

    assert not outcome.ran
    assert "disabled" in outcome.notice


# --- Tag and recent -------------------------------------------------------


async def test_tag_search_works_even_when_search_is_disabled(settings: Settings) -> None:
    """It uses `/api/tags`, not `/api/search` — worth knowing when search is off."""
    store = StubNoteStore(refs=[NoteRef.from_path("a/one.md")])

    outcome = await service(settings, store, search_enabled=False).by_tag("planning")

    assert outcome.ran
    assert [h.path for h in outcome.hits] == ["a/one.md"]


async def test_a_leading_hash_on_a_tag_is_accepted(settings: Settings) -> None:
    """People type tags the way they appear in a note."""
    store = StubNoteStore()

    await service(settings, store).by_tag("#planning")

    assert store.calls[0][1]["tag"] == "planning"


async def test_an_empty_tag_points_at_the_listing(settings: Settings) -> None:
    outcome = await service(settings, StubNoteStore()).by_tag("  ")

    assert not outcome.ran
    assert "/tag" in outcome.notice


async def test_recent_uses_the_configured_window_by_default(settings: Settings) -> None:
    store = StubNoteStore()

    await service(settings, store).recent()

    assert store.calls[0][1]["days"] == settings.recent_default_days


async def test_recent_accepts_an_explicit_window(settings: Settings) -> None:
    store = StubNoteStore()

    await service(settings, store).recent(days=30)

    assert store.calls[0][1]["days"] == 30


async def test_tags_are_listed_most_used_first(settings: Settings) -> None:
    store = StubNoteStore(tags={"rare": 1, "common": 9, "also-common": 9})

    tags = await service(settings, store).tags()

    # Count descending, then name — so the order is stable between calls.
    assert list(tags) == ["also-common", "common", "rare"]


async def test_tag_and_recent_results_become_hits_for_one_render_path(
    settings: Settings,
) -> None:
    """Tag and recent return refs, not hits; the renderer must not need two paths."""
    store = StubNoteStore(refs=[NoteRef.from_path("a/one.md")])

    outcome = await service(settings, store).recent()

    assert isinstance(outcome.hits[0], SearchHit)
    assert outcome.hits[0].matches == ()


# --- Serialisation and paging --------------------------------------------


def test_a_hit_survives_the_round_trip_through_json() -> None:
    """The session store is JSON-only, so the domain objects have to fold flat."""
    original = SearchHit(
        ref=NoteRef(
            path="Projects/Ideas.md",
            title="Ideas",
            folder="Projects",
            modified=datetime(2026, 8, 25, 9, 30, tzinfo=UTC),
            tags=("planning", "docker"),
        ),
        matches=(SearchMatch(12, "a note about docker", "docker"),),
    )

    restored = hit_from_payload(hit_to_payload(original))

    assert restored.ref == original.ref
    assert restored.matches == original.matches


def test_a_corrupt_payload_degrades_instead_of_raising() -> None:
    """A value written by an older format must not break a page turn."""
    restored = hit_from_payload({"p": "a.md", "d": "not a date", "g": "nope", "m": "nope"})

    assert restored.ref.path == "a.md"
    assert restored.ref.modified is None
    assert restored.matches == ()


def results_of(count: int, *, page_size: int = 5) -> ResultSet:
    return ResultSet(
        mode=SearchMode.FULL_TEXT,
        query="docker",
        hits=tuple(hit(f"n{index}.md") for index in range(count)),
        page_size=page_size,
    )


@pytest.mark.parametrize(
    ("count", "expected"),
    [(0, 1), (1, 1), (5, 1), (6, 2), (10, 2), (11, 3)],
)
def test_page_count_is_a_ceiling_division(count: int, expected: int) -> None:
    assert results_of(count).pages == expected


def test_pages_slice_the_result_set_without_gaps_or_overlap() -> None:
    results = results_of(12)

    seen = [h.path for page in range(1, results.pages + 1) for h in results.page(page)]

    assert seen == [f"n{index}.md" for index in range(12)]


def test_an_out_of_range_page_is_clamped_not_an_error() -> None:
    """Callback data is whatever arrives; a stale button must not crash a handler."""
    results = results_of(6)

    assert results.page(0) == results.page(1)
    assert results.page(999) == results.page(2)


def test_a_result_set_survives_the_round_trip_through_the_session() -> None:
    results = results_of(7)

    restored = ResultSet.from_payload(results.to_payload())

    assert restored.mode is results.mode
    assert restored.query == results.query
    assert restored.page_size == results.page_size
    assert [h.path for h in restored.hits] == [h.path for h in results.hits]
