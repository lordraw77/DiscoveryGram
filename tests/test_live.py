"""Opt-in contract suite against a real NoteDiscovery instance.

    make test-live            # or: uv run pytest -m live

Excluded from `make test` and from CI, because it needs credentials and it
writes. Everything it creates lives under `_DiscoveryGram_live/` and is deleted
in teardown; it touches nothing else in the vault.

This is the suite the phase 1 Definition of Done refers to: every `NoteStore`
method exercised end-to-end over REST against the live instance.
"""

from __future__ import annotations

import contextlib
import os
import uuid
from collections.abc import AsyncIterator

import pytest

from discoverygram.adapters.rest import RestNoteStore
from discoverygram.app.probe import InstanceState, probe_instance
from discoverygram.app.search import SearchService
from discoverygram.config import Settings
from discoverygram.ports.errors import Forbidden, NotFound

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not os.getenv("NOTEDISCOVERY_URL"),
        reason="NOTEDISCOVERY_URL is not set; the live suite needs a real instance",
    ),
]

SCRATCH_FOLDER = "_DiscoveryGram_live"


@pytest.fixture
async def live_store() -> AsyncIterator[RestNoteStore]:
    store = RestNoteStore(Settings())  # type: ignore[call-arg]
    try:
        yield store
    finally:
        # Best effort: a failed test must not leave the vault dirty, but a
        # missing scratch folder must not fail the teardown either.
        with contextlib.suppress(Exception):
            await store.delete_folder(SCRATCH_FOLDER)
        await store.aclose()


@pytest.fixture
def scratch_path() -> str:
    return f"{SCRATCH_FOLDER}/note-{uuid.uuid4().hex[:8]}.md"


async def test_instance_is_reachable_and_identifies_itself(live_store: RestNoteStore) -> None:
    state = await probe_instance(live_store)

    assert state.healthy is True
    assert state.config.version != "unknown"


async def test_post_overwrites_an_existing_note(
    live_store: RestNoteStore, scratch_path: str
) -> None:
    """The behaviour the whole edit flow rests on."""
    await live_store.create_note(scratch_path, "probe-v1")
    await live_store.create_note(scratch_path, "probe-v2")

    note = await live_store.get_note(scratch_path)

    assert note.content.strip() == "probe-v2"


async def test_patch_appends_rather_than_replacing(
    live_store: RestNoteStore, scratch_path: str
) -> None:
    await live_store.create_note(scratch_path, "first")
    await live_store.append_note(scratch_path, "second")

    content = (await live_store.get_note(scratch_path)).content

    assert "first" in content
    assert "second" in content


async def test_update_note_replaces_the_whole_body(
    live_store: RestNoteStore, scratch_path: str
) -> None:
    await live_store.create_note(scratch_path, "original")

    await live_store.update_note(scratch_path, "replaced")

    assert (await live_store.get_note(scratch_path)).content.strip() == "replaced"


async def test_delete_then_get_is_not_found(live_store: RestNoteStore, scratch_path: str) -> None:
    await live_store.create_note(scratch_path, "temporary")
    await live_store.delete_note(scratch_path)

    with pytest.raises(NotFound):
        await live_store.get_note(scratch_path)


async def test_move_note(live_store: RestNoteStore, scratch_path: str) -> None:
    target = scratch_path.replace(".md", "-moved.md")
    await live_store.create_note(scratch_path, "movable")

    await live_store.move_note(scratch_path, target)

    assert (await live_store.get_note(target)).content.strip() == "movable"


async def test_the_derived_tree_contains_a_newly_created_note(
    live_store: RestNoteStore, scratch_path: str
) -> None:
    """Write invalidation is what makes /browse feel live."""
    from discoverygram.adapters.tree import find_node

    await live_store.get_tree()
    await live_store.create_note(scratch_path, "in the tree")

    node = find_node(await live_store.get_tree(), SCRATCH_FOLDER)

    assert node is not None
    assert scratch_path in {note.path for note in node.notes}


async def test_search_finds_a_freshly_written_marker(
    live_store: RestNoteStore, scratch_path: str
) -> None:
    state = await probe_instance(live_store)
    if not state.search_available:
        pytest.skip("search is disabled on this instance")

    marker = f"discoverygram{uuid.uuid4().hex[:10]}"
    await live_store.create_note(scratch_path, f"marker {marker}")

    hits = await live_store.search(marker, limit=10)

    assert scratch_path in {hit.path for hit in hits}


async def test_a_query_below_the_minimum_length_returns_nothing(
    live_store: RestNoteStore,
) -> None:
    assert await live_store.search("a") == []


async def test_search_disabled_reports_forbidden_not_a_crash(
    live_store: RestNoteStore,
) -> None:
    state = await probe_instance(live_store)
    if state.search_available:
        pytest.skip("search is enabled on this instance")

    with pytest.raises(Forbidden):
        await live_store.search("anything")


async def test_read_only_surface_answers(live_store: RestNoteStore) -> None:
    """One pass over everything that only reads."""
    assert (await live_store.list_notes(limit=5)).notes is not None
    assert isinstance(await live_store.list_tags(), dict)
    assert (await live_store.get_graph()).nodes is not None
    assert (await live_store.get_stats()).version
    assert isinstance(await live_store.list_templates(), list)
    assert isinstance(await live_store.recent_notes(days=3650, limit=5), list)


async def test_share_round_trip(live_store: RestNoteStore, scratch_path: str) -> None:
    await live_store.create_note(scratch_path, "shared")

    link = await live_store.share_note(scratch_path)
    assert link.url.startswith("http")

    await live_store.unshare_note(scratch_path)


async def test_upload_media_returns_a_referenceable_path(live_store: RestNoteStore) -> None:
    """The endpoint the image-to-note flow depends on — REST only."""
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000a49444154789c6300010000050001"
        "0d0a2db4"
        "0000000049454e44ae426082"
    )

    upload = await live_store.upload_media(
        f"discoverygram-{uuid.uuid4().hex[:8]}.png", png, content_type="image/png"
    )

    assert upload.path


# --- Phase 3: the four search modes ---------------------------------------


def live_service(store: RestNoteStore, state: InstanceState) -> SearchService:
    return SearchService(store, Settings(), state)  # type: ignore[call-arg]


async def test_every_search_mode_answers(live_store: RestNoteStore) -> None:
    """The phase 3 Definition of Done, against the real vault."""
    state = await probe_instance(live_store)
    service = live_service(live_store, state)

    tags = await service.tags()
    assert isinstance(tags, dict)

    recent = await service.recent(days=3650)
    assert recent.ran

    if tags:
        by_tag = await service.by_tag(next(iter(tags)))
        assert by_tag.ran
        assert not by_tag.is_empty

    if not state.search_available:
        pytest.skip("search is disabled on this instance")

    marker = f"discoverygram{uuid.uuid4().hex[:10]}"
    await live_store.create_note(f"{SCRATCH_FOLDER}/search-modes.md", f"marker {marker}")

    full_text = await service.full_text(marker)
    literal = await service.literal(marker)

    assert not full_text.is_empty
    assert not literal.is_empty


async def test_a_short_query_degrades_with_a_notice_not_an_error(
    live_store: RestNoteStore,
) -> None:
    outcome = await live_service(live_store, await probe_instance(live_store)).full_text("a")

    assert not outcome.ran
    assert "too short" in outcome.notice


async def test_search_disabled_degrades_with_a_notice(live_store: RestNoteStore) -> None:
    state = await probe_instance(live_store)
    if state.search_available:
        pytest.skip("search is enabled on this instance")

    outcome = await live_service(live_store, state).full_text("anything")

    assert not outcome.ran
    assert "disabled" in outcome.notice
