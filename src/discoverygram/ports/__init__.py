"""Abstract ports.

The application layer depends only on these interfaces and on the domain model,
never on a concrete adapter. `NoteStore` lands in phase 1, `LlmClient` in phase 5.
"""

from discoverygram.ports.errors import (
    Conflict,
    Forbidden,
    InvalidRequest,
    NoteStoreError,
    NotFound,
    RateLimited,
    Unauthorized,
    Unavailable,
    Unsupported,
)
from discoverygram.ports.model import (
    Backlink,
    Graph,
    GraphEdge,
    GraphNode,
    InstanceConfig,
    MediaUpload,
    Note,
    NoteListing,
    NoteRef,
    SearchHit,
    SearchMatch,
    ShareLink,
    Template,
    TemplateRef,
    TreeNode,
    VaultStats,
)
from discoverygram.ports.note_store import NoteStore

__all__ = [
    "Backlink",
    "Conflict",
    "Forbidden",
    "Graph",
    "GraphEdge",
    "GraphNode",
    "InstanceConfig",
    "InvalidRequest",
    "MediaUpload",
    "NotFound",
    "Note",
    "NoteListing",
    "NoteRef",
    "NoteStore",
    "NoteStoreError",
    "RateLimited",
    "SearchHit",
    "SearchMatch",
    "ShareLink",
    "Template",
    "TemplateRef",
    "TreeNode",
    "Unauthorized",
    "Unavailable",
    "Unsupported",
    "VaultStats",
]
