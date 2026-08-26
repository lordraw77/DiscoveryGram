"""`McpNoteStore` — the optional stdio subset adapter.

The subprocess itself is never launched here: a fake session stands in for it,
so the tests cover what the adapter is responsible for — tool mapping, payload
decoding, restart-on-failure, and refusing the operations MCP does not have.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from discoverygram.adapters.mcp import SUPPORTED_TOOLS, McpNoteStore
from discoverygram.config import McpLaunchMode, Settings
from discoverygram.ports.errors import InvalidRequest, NotFound, Unsupported
from tests.fixtures import notediscovery as fx


class Block:
    def __init__(self, text: str) -> None:
        self.text = text


class Result:
    def __init__(self, payload: Any = None, *, error: str | None = None) -> None:
        self.isError = error is not None
        self.content = [Block(error if error else json.dumps(payload))]
        self.structuredContent = None


class FakeSession:
    """Stands in for an `mcp.ClientSession` bound to a live subprocess."""

    def __init__(self, responses: dict[str, Any] | None = None) -> None:
        self.responses = responses or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail_times = 0

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Result:
        self.calls.append((name, arguments))
        if self.fail_times:
            self.fail_times -= 1
            raise RuntimeError("broken pipe")
        response = self.responses.get(name, {})
        if isinstance(response, Result):
            return response
        return Result(response)


@pytest.fixture
def mcp_settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"mcp_enabled": True})


def make_store(mcp_settings: Settings, session: FakeSession) -> McpNoteStore:
    store = McpNoteStore(mcp_settings)
    store._session = session
    store._tools = frozenset(SUPPORTED_TOOLS)
    return store


def test_mcp_requires_the_feature_flag(settings: Settings) -> None:
    with pytest.raises(InvalidRequest):
        McpNoteStore(settings)


def test_the_supported_tool_set_is_the_contract_s_eighteen() -> None:
    assert len(SUPPORTED_TOOLS) == 18


def test_the_docker_launch_passes_the_url_and_key_as_env(mcp_settings: Settings) -> None:
    """The MCP server reaches NoteDiscovery over HTTP itself; it needs the same URL."""
    store = McpNoteStore(mcp_settings.model_copy(update={"notediscovery_api_key": "s3cret"}))

    command, args, env = store._server_command()

    assert command.endswith("docker")
    assert "-i" in args and "--rm" in args
    assert env["NOTEDISCOVERY_URL"] == "http://notediscovery.test:8000"
    assert env["NOTEDISCOVERY_API_KEY"] == "s3cret"
    assert args[-3:] == ["python", "-m", "mcp_server"]


def test_the_local_launch_avoids_docker_entirely(mcp_settings: Settings) -> None:
    """The socket-free alternative from the risk register."""
    store = McpNoteStore(mcp_settings.model_copy(update={"mcp_launch_mode": McpLaunchMode.LOCAL}))

    command, args, _ = store._server_command()

    assert (command, args) == ("python", ["-m", "mcp_server"])


def test_no_api_key_means_no_key_in_the_environment(mcp_settings: Settings) -> None:
    _, _, env = McpNoteStore(mcp_settings)._server_command()

    assert "NOTEDISCOVERY_API_KEY" not in env


async def test_get_note_maps_to_the_get_note_tool(mcp_settings: Settings) -> None:
    session = FakeSession({"get_note": fx.NOTE})
    store = make_store(mcp_settings, session)

    note = await store.get_note("Projects/Roadmap")

    assert note.title == "Roadmap"
    assert session.calls == [("get_note", {"path": "Projects/Roadmap.md"})]


async def test_search_maps_to_search_notes_and_ranks_client_side(
    mcp_settings: Settings,
) -> None:
    session = FakeSession({"search_notes": fx.SEARCH_RESULTS})
    store = make_store(mcp_settings, session)

    hits = await store.search("docker")

    assert hits[0].path == "Projects/docker-notes.md"
    assert session.calls[0][1]["limit"] == 50


async def test_recent_notes_uses_the_dedicated_tool(mcp_settings: Settings) -> None:
    """The one place MCP has something REST does not."""
    session = FakeSession({"get_recent_notes": fx.NOTES_LISTING})
    store = make_store(mcp_settings, session)

    recent = await store.recent_notes(days=3, limit=2)

    assert [note.path for note in recent] == ["Projects/Roadmap.md", "Projects/Ideas.md"]
    assert session.calls[0] == ("get_recent_notes", {"days": 3, "limit": 2})


async def test_update_note_is_a_read_modify_write(mcp_settings: Settings) -> None:
    session = FakeSession({"get_note": fx.NOTE, "create_note": {"success": True}})
    store = make_store(mcp_settings, session)

    await store.update_note("Projects/Roadmap", "rewritten")

    assert [name for name, _ in session.calls] == ["get_note", "create_note"]
    assert session.calls[1][1]["content"] == "rewritten"


async def test_a_write_invalidates_the_derived_tree(mcp_settings: Settings) -> None:
    session = FakeSession({"list_notes": fx.NOTES_LISTING, "create_note": {"success": True}})
    store = make_store(mcp_settings, session)
    await store.get_tree()

    await store.create_note("Inbox/New", "hi")

    assert store._tree.is_fresh is False


async def test_a_tool_error_mentioning_not_found_becomes_notfound(
    mcp_settings: Settings,
) -> None:
    session = FakeSession({"get_note": Result(error="Note not found")})
    store = make_store(mcp_settings, session)

    with pytest.raises(NotFound):
        await store.get_note("Gone")


async def test_a_dropped_session_is_restarted_once(mcp_settings: Settings) -> None:
    """An stdio server that exits takes the session with it; one reconnect hides it."""
    session = FakeSession({"list_tags": fx.TAGS})
    session.fail_times = 1
    store = make_store(mcp_settings, session)
    restarts = 0

    async def fake_restart() -> None:
        nonlocal restarts
        restarts += 1

    store._restart = fake_restart  # type: ignore[method-assign]

    assert await store.list_tags() == {"planning": 2, "docker": 1}
    assert restarts == 1


@pytest.mark.parametrize(
    "operation",
    [
        "upload_media",
        "export_note",
        "share_note",
        "unshare_note",
        "move_folder",
        "rename_folder",
        "delete_folder",
        "get_stats",
    ],
)
async def test_rest_only_operations_report_unsupported(
    mcp_settings: Settings, operation: str
) -> None:
    """MCP is a strict subset; the gap is stated, never silently worked around."""
    store = make_store(mcp_settings, FakeSession())
    arguments: dict[str, tuple[Any, ...]] = {
        "upload_media": ("a.jpg", b"x"),
        "export_note": ("A",),
        "share_note": ("A",),
        "unshare_note": ("A",),
        "move_folder": ("A", "B"),
        "rename_folder": ("A", "B"),
        "delete_folder": ("A",),
        "get_stats": (),
    }

    with pytest.raises(Unsupported, match="REST"):
        await getattr(store, operation)(*arguments[operation])


async def test_calling_a_tool_outside_the_contract_is_unsupported(
    mcp_settings: Settings,
) -> None:
    store = make_store(mcp_settings, FakeSession())

    with pytest.raises(Unsupported):
        await store._call("upload_media_somehow")


async def test_a_non_json_tool_result_is_not_a_crash(mcp_settings: Settings) -> None:
    session = FakeSession({"health_check": Result()})
    session.responses["health_check"] = Result({"status": "healthy"})
    store = make_store(mcp_settings, session)

    assert await store.health() is True


# --- The rest of the 18-tool surface -------------------------------------


async def test_list_notes_and_the_derived_tree(mcp_settings: Settings) -> None:
    """The compensation layer is shared with REST: same derivation, same cache."""
    session = FakeSession({"list_notes": fx.NOTES_LISTING})
    store = make_store(mcp_settings, session)

    listing = await store.list_notes(limit=10)
    root = await store.get_tree()

    assert len(listing.notes) == 4
    assert [folder.name for folder in root.folders] == [
        "Archive",
        "attachments",
        "Journal",
        "Projects",
    ]


async def test_tags_graph_and_templates_map_to_their_tools(mcp_settings: Settings) -> None:
    session = FakeSession(
        {
            "list_tags": fx.TAGS,
            "get_notes_by_tag": fx.TAG_NOTES,
            "get_graph": fx.GRAPH,
            "list_templates": fx.TEMPLATES,
            "get_template": fx.TEMPLATE,
        }
    )
    store = make_store(mcp_settings, session)

    assert await store.list_tags() == {"planning": 2, "docker": 1}
    assert len(await store.get_notes_by_tag("planning", limit=5)) == 2
    assert (await store.get_graph()).neighbours("Projects/Roadmap.md") == ("Projects/Ideas.md",)
    assert [t.name for t in await store.list_templates()] == ["meeting", "daily"]
    assert (await store.get_template("meeting")).name == "meeting"


async def test_get_backlinks_uses_the_dedicated_tool(mcp_settings: Settings) -> None:
    """Unlike REST, MCP has a backlinks tool rather than folding them into the note."""
    session = FakeSession({"get_backlinks": {"backlinks": fx.NOTE["backlinks"]}})
    store = make_store(mcp_settings, session)

    backlinks = await store.get_backlinks("Projects/Roadmap")

    assert [link.path for link in backlinks] == ["Projects/Ideas.md"]
    assert session.calls[0][0] == "get_backlinks"


async def test_append_delete_move_and_folder_creation(mcp_settings: Settings) -> None:
    session = FakeSession()
    store = make_store(mcp_settings, session)

    await store.append_note("A", "line", add_timestamp=True)
    await store.delete_note("A")
    assert (await store.move_note("A", "B/C")).path == "B/C.md"
    assert await store.create_folder("/B/") == "B"

    assert [name for name, _ in session.calls] == [
        "append_to_note",
        "delete_note",
        "move_note",
        "create_folder",
    ]
    assert session.calls[0][1]["add_timestamp"] is True


async def test_append_refuses_an_empty_body(mcp_settings: Settings) -> None:
    store = make_store(mcp_settings, FakeSession())

    with pytest.raises(InvalidRequest):
        await store.append_note("A", "")


async def test_search_literal_filters_case_sensitively(mcp_settings: Settings) -> None:
    session = FakeSession({"search_notes": fx.SEARCH_RESULTS})
    store = make_store(mcp_settings, session)

    hits = await store.search_literal("Docker")

    assert [hit.path for hit in hits] == ["Projects/docker-notes.md"]


async def test_a_short_query_never_reaches_the_subprocess(mcp_settings: Settings) -> None:
    session = FakeSession()
    store = make_store(mcp_settings, session)

    assert await store.search("d") == []
    assert await store.search_literal("  ") == []
    assert session.calls == []


async def test_get_config_reads_the_same_payload_as_rest(mcp_settings: Settings) -> None:
    store = make_store(mcp_settings, FakeSession({"get_config": fx.CONFIG}))

    assert (await store.get_config()).version == "0.31.3"


async def test_health_is_false_when_the_tool_fails(mcp_settings: Settings) -> None:
    session = FakeSession({"health_check": Result(error="server gone")})
    store = make_store(mcp_settings, session)

    assert await store.health() is False


async def test_create_note_from_template(mcp_settings: Settings) -> None:
    session = FakeSession({"create_note_from_template": {"success": True}})
    store = make_store(mcp_settings, session)

    ref = await store.create_note_from_template("meeting", "Meetings/Standup")

    assert ref.path == "Meetings/Standup.md"


async def test_recent_notes_falls_back_when_the_tool_answers_empty(
    mcp_settings: Settings,
) -> None:
    """Builds differ in what `get_recent_notes` returns; the listing is always there."""
    session = FakeSession({"get_recent_notes": {}, "list_notes": fx.NOTES_LISTING})
    store = make_store(mcp_settings, session)

    recent = await store.recent_notes(days=10_000, limit=2)

    assert [note.path for note in recent] == ["Projects/Ideas.md", "Projects/Roadmap.md"]


async def test_aclose_is_safe_without_a_live_subprocess(mcp_settings: Settings) -> None:
    store = make_store(mcp_settings, FakeSession())

    await store.aclose()

    assert store._session is None
