"""The `NoteStore` port.

Everything the application layer is allowed to know about NoteDiscovery. Two
adapters implement it: `RestNoteStore` (primary, complete) and `McpNoteStore`
(optional, a strict subset that raises `Unsupported` for the rest).

Operations are grouped by the capability they need, so an adapter that cannot
serve a group raises `Unsupported` for the whole group rather than failing
halfway through a flow.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from types import TracebackType

from discoverygram.ports.model import (
    Backlink,
    Graph,
    InstanceConfig,
    MediaUpload,
    Note,
    NoteListing,
    NoteRef,
    SearchHit,
    ShareLink,
    Template,
    TemplateRef,
    TreeNode,
    VaultStats,
)


class NoteStore(ABC):
    """Async interface to a NoteDiscovery vault."""

    # --- System ----------------------------------------------------------

    @abstractmethod
    async def health(self) -> bool:
        """True when the instance answers. Never raises."""

    @abstractmethod
    async def get_config(self) -> InstanceConfig:
        """Instance identity and whether search is enabled."""

    @abstractmethod
    async def get_stats(self) -> VaultStats:
        """Vault counters. REST only."""

    # --- Notes -----------------------------------------------------------

    @abstractmethod
    async def list_notes(self, *, limit: int | None = None, offset: int = 0) -> NoteListing:
        """Notes and folder paths. Always pass a `limit` on user-facing calls."""

    @abstractmethod
    async def get_note(self, path: str, *, include_backlinks: bool = True) -> Note:
        """Fetch one note. Raises `NotFound` when it does not exist."""

    @abstractmethod
    async def create_note(self, path: str, content: str) -> NoteRef:
        """Create **or overwrite** a note.

        `POST /api/notes/{path}` is an upsert in NoteDiscovery 0.31.3 (confirmed
        in `backend/main.py:create_or_update_note`), which is what makes the
        read-modify-write edit flow possible.
        """

    @abstractmethod
    async def append_note(self, path: str, content: str, *, add_timestamp: bool = False) -> None:
        """Append to an existing note. Rate-limited server-side to 60/minute."""

    @abstractmethod
    async def delete_note(self, path: str) -> None: ...

    @abstractmethod
    async def move_note(self, old_path: str, new_path: str) -> NoteRef:
        """Move or rename a note."""

    # --- Folders ---------------------------------------------------------

    @abstractmethod
    async def create_folder(self, path: str) -> str: ...

    @abstractmethod
    async def move_folder(self, old_path: str, new_path: str) -> str:
        """REST only."""

    @abstractmethod
    async def rename_folder(self, old_path: str, new_path: str) -> str:
        """REST only."""

    @abstractmethod
    async def delete_folder(self, path: str) -> None:
        """REST only. Deletes the folder and everything under it."""

    # --- Search and tags -------------------------------------------------

    @abstractmethod
    async def search(
        self, query: str, *, limit: int | None = None, offset: int = 0
    ) -> list[SearchHit]:
        """Full-text search, ranked client-side.

        Raises `Forbidden` when search is disabled server-side, and returns an
        empty list for a query below the instance's minimum length.
        """

    @abstractmethod
    async def list_tags(self) -> dict[str, int]:
        """Every tag in the vault with its note count."""

    @abstractmethod
    async def get_notes_by_tag(
        self, tag: str, *, limit: int | None = None, offset: int = 0
    ) -> list[NoteRef]: ...

    # --- Graph -----------------------------------------------------------

    @abstractmethod
    async def get_graph(self) -> Graph: ...

    # --- Templates -------------------------------------------------------

    @abstractmethod
    async def list_templates(self) -> list[TemplateRef]: ...

    @abstractmethod
    async def get_template(self, name: str) -> Template: ...

    @abstractmethod
    async def create_note_from_template(self, template_name: str, note_path: str) -> NoteRef: ...

    # --- Media, export, sharing (REST only) ------------------------------

    @abstractmethod
    async def upload_media(
        self,
        filename: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
        note_path: str = "",
    ) -> MediaUpload: ...

    @abstractmethod
    async def export_note(self, path: str) -> bytes:
        """Standalone HTML export of a note."""

    @abstractmethod
    async def share_note(self, path: str, *, theme: str = "light") -> ShareLink: ...

    @abstractmethod
    async def unshare_note(self, path: str) -> None: ...

    # --- Client-side compensation ----------------------------------------

    @abstractmethod
    async def get_tree(self, *, refresh: bool = False) -> TreeNode:
        """The folder tree, derived client-side and cached."""

    @abstractmethod
    def invalidate_tree(self) -> None:
        """Drop the cached tree. Called after every write."""

    @abstractmethod
    async def search_literal(self, query: str, *, limit: int | None = None) -> list[SearchHit]:
        """Case-sensitive substring search, filtered client-side over `search`."""

    @abstractmethod
    async def get_backlinks(self, path: str) -> list[Backlink]:
        """Notes linking to `path`. Cheap: the note payload already carries them."""

    @abstractmethod
    async def recent_notes(self, *, days: int = 7, limit: int = 20) -> list[NoteRef]:
        """Notes modified within `days`. No endpoint exists; derived client-side."""

    # --- Lifecycle -------------------------------------------------------

    @abstractmethod
    async def aclose(self) -> None:
        """Release connections or subprocesses."""

    async def __aenter__(self) -> NoteStore:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()
