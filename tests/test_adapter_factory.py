"""Transport selection — the one place a NoteStore implementation is chosen."""

from __future__ import annotations

import pytest

from discoverygram.adapters import build_note_store
from discoverygram.adapters.mcp import McpNoteStore
from discoverygram.adapters.rest import RestNoteStore
from discoverygram.config import Settings, Transport


async def test_rest_is_the_default(settings: Settings) -> None:
    store = build_note_store(settings)

    assert isinstance(store, RestNoteStore)
    await store.aclose()


async def test_mcp_is_selected_only_when_asked_for(settings: Settings) -> None:
    store = build_note_store(
        settings.model_copy(update={"notediscovery_transport": Transport.MCP, "mcp_enabled": True})
    )

    assert isinstance(store, McpNoteStore)
    await store.aclose()


def test_settings_refuse_the_mcp_transport_without_the_flag(
    env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MCP is a strict subset; choosing it must be deliberate on both switches."""
    monkeypatch.setenv("NOTEDISCOVERY_TRANSPORT", "mcp")

    with pytest.raises(ValueError, match="MCP_ENABLED"):
        Settings()  # type: ignore[call-arg]
