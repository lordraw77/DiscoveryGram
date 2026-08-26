"""The `SessionStore` port.

Telegram caps `callback_data` at 64 bytes, which cannot carry a note path, a
search query or a pending draft. Everything that a button needs to remember
lives here instead, addressed by a short opaque token embedded in the callback.

That makes this store part of the request path, not a cache: losing an entry
means a button stops working. Entries are therefore TTL-bounded rather than
size-bounded, and the Redis backend exists so a restart does not break every
keyboard already sitting in a user's chat history.

Values are JSON-serialisable mappings. Nothing richer, because the Redis backend
has to round-trip them through a string.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from types import TracebackType
from typing import Any

SessionValue = dict[str, Any]


class SessionStore(ABC):
    """Async key/value store with per-entry expiry."""

    @abstractmethod
    async def get(self, key: str) -> SessionValue | None:
        """The stored value, or `None` when it is missing or expired."""

    @abstractmethod
    async def set(self, key: str, value: SessionValue, *, ttl_s: int | None = None) -> None:
        """Store a value. `ttl_s=None` uses the store's default lifetime."""

    @abstractmethod
    async def delete(self, key: str) -> None:
        """Remove a key. Removing a key that is not there is not an error."""

    @abstractmethod
    async def ping(self) -> bool:
        """True when the backend is usable. Never raises — this backs `/readyz`."""

    @abstractmethod
    async def aclose(self) -> None: ...

    async def __aenter__(self) -> SessionStore:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()
