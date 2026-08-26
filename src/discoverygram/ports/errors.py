"""Typed errors every NoteStore adapter raises.

Adapters translate transport failures (HTTP status codes, MCP error payloads,
subprocess death) into these, so the application layer never sees an `httpx`
or `mcp` exception and can map each case to a user-facing message once.
"""

from __future__ import annotations


class NoteStoreError(Exception):
    """Base class for every NoteDiscovery failure."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status

    def __str__(self) -> str:
        return self.message


class NotFound(NoteStoreError):
    """The note, folder, template or media file does not exist."""


class Unauthorized(NoteStoreError):
    """Missing or wrong API key (HTTP 401)."""


class Forbidden(NoteStoreError):
    """The instance refuses the operation — search disabled, path rejected (HTTP 403)."""


class Conflict(NoteStoreError):
    """The target already exists or is held by someone else (HTTP 409)."""


class InvalidRequest(NoteStoreError):
    """The instance rejected the arguments (HTTP 400)."""


class RateLimited(NoteStoreError):
    """A rate limit was hit (HTTP 429). `retry_after` is in seconds when known."""

    def __init__(
        self, message: str, *, status: int | None = 429, retry_after: float | None = None
    ) -> None:
        super().__init__(message, status=status)
        self.retry_after = retry_after


class Unavailable(NoteStoreError):
    """The instance is unreachable, timed out, or returned 5xx after retries."""


class Unsupported(NoteStoreError):
    """The active transport cannot perform this operation at all.

    Raised by the MCP adapter for everything outside its 18 tools — media upload,
    export, sharing, stats and folder move/rename/delete.
    """
