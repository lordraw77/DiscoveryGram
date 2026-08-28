"""`RestNoteStore` against recorded payloads.

Covers the transport (retries, error mapping, throttling) and the compensation
layer (derived tree, literal search, read-modify-write edit, client-side recent),
because those are the parts that carry real logic rather than a call.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from discoverygram.adapters.rest import RestNoteStore
from discoverygram.adapters.throttle import RateLimiter
from discoverygram.config import Settings
from discoverygram.ports.errors import (
    Forbidden,
    InvalidRequest,
    NotFound,
    RateLimited,
    Unauthorized,
    Unavailable,
)
from tests.fixtures import notediscovery as fx

BASE = "http://notediscovery.test:8000"


@pytest.fixture
def no_throttle() -> RateLimiter:
    """Throttling is covered in test_throttle.py; here it would only add waits."""
    return RateLimiter({})


@pytest.fixture
def rest(settings: Settings, no_throttle: RateLimiter) -> RestNoteStore:
    return RestNoteStore(
        settings.model_copy(update={"notediscovery_max_retries": 0}), limiter=no_throttle
    )


# --- System --------------------------------------------------------------


@respx.mock
async def test_health_true_when_the_instance_answers(rest: RestNoteStore) -> None:
    respx.get(f"{BASE}/health").mock(return_value=httpx.Response(200, json=fx.HEALTH))

    assert await rest.health() is True


@respx.mock
async def test_health_false_when_unreachable(rest: RestNoteStore) -> None:
    """Readiness must degrade, not raise."""
    respx.get(f"{BASE}/health").mock(side_effect=httpx.ConnectError("refused"))

    assert await rest.health() is False


@respx.mock
async def test_health_does_not_burn_the_retry_ladder(settings: Settings) -> None:
    """`/readyz` is polled; retrying there turns an outage into a probe timeout."""
    store = RestNoteStore(settings.model_copy(update={"notediscovery_max_retries": 5}))
    route = respx.get(f"{BASE}/health").mock(side_effect=httpx.ConnectError("refused"))

    await store.health()

    assert route.call_count == 1
    await store.aclose()


@respx.mock
async def test_api_key_is_sent_as_x_api_key(
    env: None, monkeypatch: pytest.MonkeyPatch, no_throttle: RateLimiter
) -> None:
    monkeypatch.setenv("NOTEDISCOVERY_API_KEY", "s3cret")
    store = RestNoteStore(Settings(), limiter=no_throttle)  # type: ignore[call-arg]
    route = respx.get(f"{BASE}/health").mock(return_value=httpx.Response(200, json=fx.HEALTH))

    await store.health()

    assert route.calls.last.request.headers["X-API-Key"] == "s3cret"
    await store.aclose()


@respx.mock
async def test_an_unauthenticated_instance_needs_no_key(rest: RestNoteStore) -> None:
    route = respx.get(f"{BASE}/health").mock(return_value=httpx.Response(200, json=fx.HEALTH))

    await rest.health()

    assert "X-API-Key" not in route.calls.last.request.headers


@respx.mock
async def test_get_config(rest: RestNoteStore) -> None:
    respx.get(f"{BASE}/api/config").mock(return_value=httpx.Response(200, json=fx.CONFIG))

    config = await rest.get_config()

    assert config.version == "0.31.3"
    assert config.search_enabled is True


# --- Errors --------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, Unauthorized),
        (403, Forbidden),
        (404, NotFound),
        (400, InvalidRequest),
        (429, RateLimited),
        (500, Unavailable),
    ],
)
@respx.mock
async def test_http_status_maps_to_a_typed_error(
    rest: RestNoteStore, status: int, expected: type[Exception]
) -> None:
    respx.get(f"{BASE}/api/notes/Projects/Roadmap.md").mock(
        return_value=httpx.Response(status, json={"detail": "nope"})
    )

    with pytest.raises(expected):
        await rest.get_note("Projects/Roadmap")


@respx.mock
async def test_rate_limited_carries_retry_after(rest: RestNoteStore) -> None:
    """The 60/minute PATCH cap is real; the user gets a retry hint, not a stack trace."""
    respx.patch(f"{BASE}/api/notes/Projects/Roadmap.md").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "30"}, json={"detail": "slow"})
    )

    with pytest.raises(RateLimited) as raised:
        await rest.append_note("Projects/Roadmap", "more")

    assert raised.value.retry_after == 30.0


@respx.mock
async def test_search_disabled_surfaces_as_forbidden(rest: RestNoteStore) -> None:
    respx.get(f"{BASE}/api/search").mock(
        return_value=httpx.Response(403, json={"detail": "Search is disabled"})
    )

    with pytest.raises(Forbidden):
        await rest.search("docker")


# --- Retries -------------------------------------------------------------


@respx.mock
async def test_a_transient_5xx_is_retried(
    settings: Settings, no_throttle: RateLimiter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("discoverygram.adapters.rest.random.uniform", lambda *_: 0.0)
    store = RestNoteStore(
        settings.model_copy(update={"notediscovery_max_retries": 2}), limiter=no_throttle
    )
    route = respx.get(f"{BASE}/api/tags").mock(
        side_effect=[httpx.Response(503), httpx.Response(200, json=fx.TAGS)]
    )

    assert await store.list_tags() == {"planning": 2, "docker": 1}
    assert route.call_count == 2
    await store.aclose()


@respx.mock
async def test_a_4xx_is_never_retried(rest: RestNoteStore) -> None:
    """A rejected request will not get better; retrying only burns rate limit."""
    route = respx.get(f"{BASE}/api/notes/Gone.md").mock(return_value=httpx.Response(404))

    with pytest.raises(NotFound):
        await rest.get_note("Gone")

    assert route.call_count == 1


@respx.mock
async def test_exhausted_retries_raise_unavailable(
    settings: Settings, no_throttle: RateLimiter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("discoverygram.adapters.rest.random.uniform", lambda *_: 0.0)
    store = RestNoteStore(
        settings.model_copy(update={"notediscovery_max_retries": 2}), limiter=no_throttle
    )
    respx.get(f"{BASE}/api/tags").mock(side_effect=httpx.ConnectTimeout("timeout"))

    with pytest.raises(Unavailable):
        await store.list_tags()

    await store.aclose()


# --- Notes ---------------------------------------------------------------


@respx.mock
async def test_get_note_normalises_the_path(rest: RestNoteStore) -> None:
    """A user typing `Projects/Roadmap` means the `.md` file."""
    route = respx.get(f"{BASE}/api/notes/Projects/Roadmap.md").mock(
        return_value=httpx.Response(200, json=fx.NOTE)
    )

    note = await rest.get_note("Projects/Roadmap")

    assert note.title == "Roadmap"
    assert route.called


@respx.mock
async def test_create_note_posts_the_body_and_invalidates_the_tree(
    rest: RestNoteStore,
) -> None:
    respx.get(f"{BASE}/api/notes").mock(return_value=httpx.Response(200, json=fx.NOTES_LISTING))
    route = respx.post(f"{BASE}/api/notes/Inbox/New.md").mock(
        return_value=httpx.Response(200, json=fx.SAVE_OK)
    )
    await rest.get_tree()

    ref = await rest.create_note("Inbox/New", "hello")

    assert ref.path == "Inbox/New.md"
    assert json.loads(route.calls.last.request.read()) == {"content": "hello"}
    assert rest._tree.is_fresh is False


@respx.mock
async def test_update_note_is_a_read_modify_write_over_post(rest: RestNoteStore) -> None:
    """PATCH only appends, so a real edit reads first and POSTs the whole body back."""
    read = respx.get(f"{BASE}/api/notes/Projects/Roadmap.md").mock(
        return_value=httpx.Response(200, json=fx.NOTE)
    )
    write = respx.post(f"{BASE}/api/notes/Projects/Roadmap.md").mock(
        return_value=httpx.Response(200, json=fx.SAVE_OK)
    )

    await rest.update_note("Projects/Roadmap", "rewritten")

    assert read.called
    assert json.loads(write.calls.last.request.read()) == {"content": "rewritten"}


@respx.mock
async def test_update_note_refuses_to_resurrect_a_deleted_note(rest: RestNoteStore) -> None:
    """POST is an upsert, so without the read an edit would silently re-create."""
    respx.get(f"{BASE}/api/notes/Gone.md").mock(return_value=httpx.Response(404))
    write = respx.post(f"{BASE}/api/notes/Gone.md")

    with pytest.raises(NotFound):
        await rest.update_note("Gone", "text")

    assert not write.called


@respx.mock
async def test_append_note_sends_the_timestamp_flag(rest: RestNoteStore) -> None:
    route = respx.patch(f"{BASE}/api/notes/Journal/2026/Daily.md").mock(
        return_value=httpx.Response(200, json={"success": True})
    )

    await rest.append_note("Journal/2026/Daily", "a line", add_timestamp=True)

    assert json.loads(route.calls.last.request.read()) == {
        "content": "a line",
        "add_timestamp": True,
    }


async def test_append_refuses_an_empty_body(rest: RestNoteStore) -> None:
    with pytest.raises(InvalidRequest):
        await rest.append_note("Journal/2026/Daily", "")


@respx.mock
async def test_move_note_uses_the_camelcase_body(rest: RestNoteStore) -> None:
    route = respx.post(f"{BASE}/api/notes/move").mock(
        return_value=httpx.Response(200, json={"success": True})
    )

    ref = await rest.move_note("Inbox/Draft", "Projects/Final")

    assert ref.path == "Projects/Final.md"
    assert json.loads(route.calls.last.request.read()) == {
        "oldPath": "Inbox/Draft.md",
        "newPath": "Projects/Final.md",
    }


# --- Search --------------------------------------------------------------


@respx.mock
async def test_search_always_sends_an_explicit_limit(rest: RestNoteStore) -> None:
    """`/api/search` has no server-side cap: an unbounded query returns the vault."""
    route = respx.get(f"{BASE}/api/search").mock(
        return_value=httpx.Response(200, json=fx.SEARCH_RESULTS)
    )

    await rest.search("docker")

    assert route.calls.last.request.url.params["limit"] == "50"


@respx.mock
async def test_search_ranks_the_title_hit_first(rest: RestNoteStore) -> None:
    respx.get(f"{BASE}/api/search").mock(return_value=httpx.Response(200, json=fx.SEARCH_RESULTS))

    hits = await rest.search("docker")

    assert hits[0].path == "Projects/docker-notes.md"
    assert hits[0].title_match is True


@respx.mock
async def test_a_query_below_the_minimum_length_never_leaves_the_process(
    rest: RestNoteStore,
) -> None:
    route = respx.get(f"{BASE}/api/search")

    assert await rest.search("d") == []
    assert await rest.search("   ") == []
    assert not route.called


@respx.mock
async def test_search_literal_is_case_sensitive_and_checks_bodies(
    rest: RestNoteStore,
) -> None:
    """Snippets carry ±15 characters, so a literal hit can fall outside them."""
    respx.get(f"{BASE}/api/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "name": "shouting",
                        "path": "a/shouting.md",
                        "matches": [{"line_number": 1, "context": "DOCKER in caps"}],
                    },
                    {
                        "name": "quiet",
                        "path": "b/quiet.md",
                        "matches": [{"line_number": 9, "context": "far away context"}],
                    },
                ],
                "query": "docker",
            },
        )
    )
    respx.get(f"{BASE}/api/notes/a/shouting.md").mock(
        return_value=httpx.Response(
            200, json={"path": "a/shouting.md", "content": "only DOCKER here", "metadata": {}}
        )
    )
    respx.get(f"{BASE}/api/notes/b/quiet.md").mock(
        return_value=httpx.Response(
            200, json={"path": "b/quiet.md", "content": "a line mentioning docker", "metadata": {}}
        )
    )

    hits = await rest.search_literal("docker")

    assert [hit.path for hit in hits] == ["b/quiet.md"]


# --- Compensation --------------------------------------------------------


@respx.mock
async def test_get_tree_is_derived_from_one_listing_call(rest: RestNoteStore) -> None:
    route = respx.get(f"{BASE}/api/notes").mock(
        return_value=httpx.Response(200, json=fx.NOTES_LISTING)
    )

    root = await rest.get_tree()
    await rest.get_tree()

    assert route.call_count == 1
    # Case-insensitive ordering, and the empty `Archive` folder survives because
    # `/api/notes` reports folders separately from note paths.
    assert [folder.name for folder in root.folders] == [
        "Archive",
        "attachments",
        "Journal",
        "Projects",
    ]


@respx.mock
async def test_recent_notes_is_derived_client_side(rest: RestNoteStore) -> None:
    """NoteDiscovery has no `/api/recent`; the listing already carries `modified`."""
    respx.get(f"{BASE}/api/notes").mock(return_value=httpx.Response(200, json=fx.NOTES_LISTING))

    recent = await rest.recent_notes(days=10_000, limit=2)

    assert [note.path for note in recent] == ["Projects/Ideas.md", "Projects/Roadmap.md"]


@respx.mock
async def test_backlinks_come_free_with_the_note(rest: RestNoteStore) -> None:
    route = respx.get(f"{BASE}/api/notes/Projects/Roadmap.md").mock(
        return_value=httpx.Response(200, json=fx.NOTE)
    )

    backlinks = await rest.get_backlinks("Projects/Roadmap")

    assert [link.path for link in backlinks] == ["Projects/Ideas.md"]
    assert route.call_count == 1


# --- Media, sharing, stats -----------------------------------------------


@respx.mock
async def test_upload_media_posts_multipart(rest: RestNoteStore) -> None:
    route = respx.post(f"{BASE}/api/upload-media").mock(
        return_value=httpx.Response(200, json=fx.UPLOAD)
    )

    upload = await rest.upload_media("photo.jpg", b"\xff\xd8\xff", content_type="image/jpeg")

    assert upload.path == "attachments/photo.jpg"
    assert route.calls.last.request.headers["content-type"].startswith("multipart/form-data")


async def test_upload_media_refuses_an_oversized_file(rest: RestNoteStore) -> None:
    """Telegram caps downloads at 20 MB; tell the user before spending the round trip."""
    with pytest.raises(InvalidRequest):
        await rest.upload_media("big.mp4", b"x" * (21 * 1024 * 1024))


@respx.mock
async def test_share_note_returns_the_public_link(rest: RestNoteStore) -> None:
    respx.post(f"{BASE}/api/share/Projects/Roadmap.md").mock(
        return_value=httpx.Response(200, json=fx.SHARE)
    )

    link = await rest.share_note("Projects/Roadmap")

    assert link.url.endswith("/share/abc123")


@respx.mock
async def test_get_stats(rest: RestNoteStore) -> None:
    respx.get(f"{BASE}/api/stats").mock(return_value=httpx.Response(200, json=fx.STATS))

    assert (await rest.get_stats()).notes_count == 42


@respx.mock
async def test_export_note_returns_bytes(rest: RestNoteStore) -> None:
    respx.get(f"{BASE}/api/export/Projects/Roadmap.md").mock(
        return_value=httpx.Response(200, content=b"<html></html>")
    )

    assert await rest.export_note("Projects/Roadmap") == b"<html></html>"


# --- Throttling ----------------------------------------------------------


@respx.mock
async def test_writes_go_through_the_limiter(settings: Settings) -> None:
    calls: list[str] = []

    class RecordingLimiter(RateLimiter):
        async def acquire(self, bucket: str) -> float:
            calls.append(bucket)
            return 0.0

    store = RestNoteStore(settings, limiter=RecordingLimiter())
    respx.patch(f"{BASE}/api/notes/A.md").mock(return_value=httpx.Response(200, json={}))

    await store.append_note("A", "text")

    assert calls == ["note_append"]
    await store.aclose()


@respx.mock
async def test_reads_are_not_throttled(settings: Settings) -> None:
    """Only the endpoints NoteDiscovery actually limits are paced."""
    calls: list[str] = []

    class RecordingLimiter(RateLimiter):
        async def acquire(self, bucket: str) -> float:
            calls.append(bucket)
            return 0.0

    store = RestNoteStore(settings, limiter=RecordingLimiter())
    respx.get(f"{BASE}/api/notes").mock(return_value=httpx.Response(200, json=fx.NOTES_LISTING))

    await store.list_notes(limit=10)

    assert calls == []
    await store.aclose()


async def test_the_adapter_closes_its_own_client(settings: Settings) -> None:
    store = RestNoteStore(settings)

    async with store:
        pass

    assert store._client.is_closed


async def test_an_injected_client_is_left_open(settings: Settings) -> None:
    """A shared client belongs to whoever created it."""
    client = httpx.AsyncClient(base_url=BASE)
    store = RestNoteStore(settings, client=client)

    await store.aclose()

    assert not client.is_closed
    await client.aclose()


@respx.mock
async def test_a_non_json_body_is_reported_not_swallowed(rest: RestNoteStore) -> None:
    respx.get(f"{BASE}/api/tags").mock(return_value=httpx.Response(200, content=b"<html>"))

    with pytest.raises(Exception, match="non-JSON"):
        await rest.list_tags()


# --- The remaining REST surface ------------------------------------------


@respx.mock
async def test_folder_operations_use_their_endpoints(rest: RestNoteStore) -> None:
    """Folder move/rename/delete exist only over REST — the MCP subset has none of them."""
    create = respx.post(f"{BASE}/api/folders").mock(return_value=httpx.Response(200, json={}))
    move = respx.post(f"{BASE}/api/folders/move").mock(return_value=httpx.Response(200, json={}))
    rename = respx.post(f"{BASE}/api/folders/rename").mock(
        return_value=httpx.Response(200, json={})
    )
    delete = respx.delete(f"{BASE}/api/folders/Archive").mock(
        return_value=httpx.Response(200, json={})
    )

    assert await rest.create_folder("/Projects/2026/") == "Projects/2026"
    assert await rest.move_folder("A", "B/C") == "B/C"
    assert await rest.rename_folder("A", "B") == "B"
    await rest.delete_folder("Archive")

    assert create.called and move.called and rename.called and delete.called
    assert json.loads(create.calls.last.request.read()) == {"path": "Projects/2026"}


@respx.mock
async def test_get_notes_by_tag(rest: RestNoteStore) -> None:
    route = respx.get(f"{BASE}/api/tags/planning").mock(
        return_value=httpx.Response(200, json=fx.TAG_NOTES)
    )

    refs = await rest.get_notes_by_tag("planning", limit=10)

    assert [ref.path for ref in refs] == ["Projects/Roadmap.md", "Projects/Ideas.md"]
    assert route.calls.last.request.url.params["limit"] == "10"


@respx.mock
async def test_graph_backs_related_notes(rest: RestNoteStore) -> None:
    respx.get(f"{BASE}/api/graph").mock(return_value=httpx.Response(200, json=fx.GRAPH))

    graph = await rest.get_graph()

    assert graph.neighbours("Projects/Roadmap.md") == ("Projects/Ideas.md",)


@respx.mock
async def test_template_listing_and_fetch(rest: RestNoteStore) -> None:
    respx.get(f"{BASE}/api/templates").mock(return_value=httpx.Response(200, json=fx.TEMPLATES))
    respx.get(f"{BASE}/api/templates/meeting").mock(
        return_value=httpx.Response(200, json=fx.TEMPLATE)
    )

    assert [t.name for t in await rest.list_templates()] == ["meeting", "daily"]
    assert (await rest.get_template("meeting")).content.startswith("# {{title}}")


@respx.mock
async def test_create_note_from_template_sends_camelcase_keys(rest: RestNoteStore) -> None:
    route = respx.post(f"{BASE}/api/templates/create-note").mock(
        return_value=httpx.Response(200, json={"success": True, "path": "Meetings/Standup.md"})
    )

    ref = await rest.create_note_from_template("meeting", "Meetings/Standup")

    assert ref.path == "Meetings/Standup.md"
    assert json.loads(route.calls.last.request.read()) == {
        "templateName": "meeting",
        "notePath": "Meetings/Standup.md",
    }


@respx.mock
async def test_unshare_note(rest: RestNoteStore) -> None:
    route = respx.delete(f"{BASE}/api/share/Projects/Roadmap.md").mock(
        return_value=httpx.Response(200, json={"success": True})
    )

    await rest.unshare_note("Projects/Roadmap")

    assert route.called


@respx.mock
async def test_delete_note_invalidates_the_tree(rest: RestNoteStore) -> None:
    respx.get(f"{BASE}/api/notes").mock(return_value=httpx.Response(200, json=fx.NOTES_LISTING))
    respx.delete(f"{BASE}/api/notes/Welcome.md").mock(return_value=httpx.Response(200, json={}))
    await rest.get_tree()

    await rest.delete_note("Welcome")

    assert rest._tree.is_fresh is False


@respx.mock
async def test_the_correlation_id_travels_with_the_request(rest: RestNoteStore) -> None:
    """One Telegram action must be followable across the bot and NoteDiscovery."""
    from discoverygram.util.correlation import clear_correlation_id, set_correlation_id

    route = respx.get(f"{BASE}/api/tags").mock(return_value=httpx.Response(200, json=fx.TAGS))
    set_correlation_id("abc123def456")
    try:
        await rest.list_tags()
    finally:
        clear_correlation_id()

    assert route.calls.last.request.headers["X-Correlation-Id"] == "abc123def456"


@respx.mock
async def test_search_literal_gives_up_gracefully_on_an_unreadable_candidate(
    rest: RestNoteStore,
) -> None:
    respx.get(f"{BASE}/api/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {"name": "gone", "path": "a/gone.md", "matches": []},
                ],
                "query": "docker",
            },
        )
    )
    respx.get(f"{BASE}/api/notes/a/gone.md").mock(return_value=httpx.Response(404))

    assert await rest.search_literal("docker") == []


@respx.mock
async def test_search_literal_honours_the_limit(rest: RestNoteStore) -> None:
    results = [
        {"name": f"docker-{index}", "path": f"a/docker-{index}.md", "matches": []}
        for index in range(5)
    ]
    respx.get(f"{BASE}/api/search").mock(
        return_value=httpx.Response(200, json={"results": results, "query": "docker"})
    )

    assert len(await rest.search_literal("docker", limit=2)) == 2


# --- Phase 7: the hot-read caches ----------------------------------------


@respx.mock
async def test_the_tag_index_is_read_once(rest: RestNoteStore) -> None:
    """`/tag` with no argument reads the whole index; twice is once too many."""
    route = respx.get(f"{BASE}/api/tags").mock(return_value=httpx.Response(200, json=fx.TAGS))

    await rest.list_tags()
    await rest.list_tags()

    assert route.call_count == 1


@respx.mock
async def test_a_created_note_invalidates_the_tag_index(rest: RestNoteStore) -> None:
    """A note saved from Telegram carries tags, and /tag must show them now."""
    tags = respx.get(f"{BASE}/api/tags").mock(return_value=httpx.Response(200, json=fx.TAGS))
    respx.post(f"{BASE}/api/notes/Projects/New.md").mock(return_value=httpx.Response(200, json={}))

    await rest.list_tags()
    await rest.create_note("Projects/New.md", "#planning body")
    await rest.list_tags()

    assert tags.call_count == 2


@respx.mock
async def test_an_append_invalidates_the_tags_but_not_the_tree(rest: RestNoteStore) -> None:
    """The path did not change; the tags may have."""
    notes = respx.get(f"{BASE}/api/notes").mock(
        return_value=httpx.Response(200, json=fx.NOTES_LISTING)
    )
    tags = respx.get(f"{BASE}/api/tags").mock(return_value=httpx.Response(200, json=fx.TAGS))
    respx.patch(f"{BASE}/api/notes/Daily.md").mock(return_value=httpx.Response(200, json={}))

    await rest.get_tree()
    await rest.list_tags()
    await rest.append_note("Daily.md", "#idea another thought")
    await rest.get_tree()
    await rest.list_tags()

    assert notes.call_count == 1
    assert tags.call_count == 2


@respx.mock
async def test_a_caller_cannot_corrupt_the_cached_tag_index(rest: RestNoteStore) -> None:
    """The cache hands out its own dictionary, so a caller mutating one is harmless."""
    respx.get(f"{BASE}/api/tags").mock(return_value=httpx.Response(200, json=fx.TAGS))

    first = await rest.list_tags()
    first["planning"] = 999

    assert (await rest.list_tags())["planning"] == 2


@respx.mock
async def test_an_uploaded_filename_can_never_be_a_path(rest: RestNoteStore) -> None:
    """A Telegram client chooses the filename; it must not choose a directory."""
    route = respx.post(f"{BASE}/api/upload-media").mock(
        return_value=httpx.Response(200, json=fx.UPLOAD)
    )

    await rest.upload_media("../../../etc/cron.d/evil.png", b"\x89PNG\r\n\x1a\n")

    body = route.calls.last.request.content.decode("latin-1")
    assert "../../../etc" not in body
    assert "evil.png" in body
