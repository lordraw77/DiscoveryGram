"""Concrete adapters implementing the ports, wired at startup from settings."""

from discoverygram.adapters.mcp import McpNoteStore
from discoverygram.adapters.rest import RestNoteStore
from discoverygram.adapters.throttle import RateLimiter
from discoverygram.adapters.tree import TreeCache, breadcrumb, build_tree, find_node
from discoverygram.config import Settings, Transport
from discoverygram.ports.note_store import NoteStore

__all__ = [
    "McpNoteStore",
    "RateLimiter",
    "RestNoteStore",
    "TreeCache",
    "breadcrumb",
    "build_note_store",
    "build_tree",
    "find_node",
]


def build_note_store(settings: Settings) -> NoteStore:
    """The single place a transport is chosen.

    REST unless the operator explicitly asked for MCP — and settings validation
    already refuses `NOTEDISCOVERY_TRANSPORT=mcp` without `MCP_ENABLED=true`.
    """
    if settings.notediscovery_transport is Transport.MCP:
        return McpNoteStore(settings)
    return RestNoteStore(settings)
