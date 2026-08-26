"""Session store backends.

`memory` is the default and is correct for a single replica: it is a dict with
expiry, and it loses everything on restart. `redis` survives restarts and is
shared across replicas, which is what makes the callback tokens in a user's
chat history keep working after a deploy.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

from discoverygram.config import SessionBackend, Settings
from discoverygram.ports.session_store import SessionStore, SessionValue
from discoverygram.util.logging import get_logger

log = get_logger(__name__)

# Expired entries are dropped on read, but a key nobody reads again would leak.
# A sweep every N writes keeps the dict bounded without a background task.
_SWEEP_EVERY_WRITES = 100


class MemorySessionStore(SessionStore):
    """In-process store with per-entry expiry. Single replica only."""

    def __init__(self, *, default_ttl_s: int, clock: Callable[[], float] = time.monotonic) -> None:
        self._default_ttl_s = default_ttl_s
        self._clock = clock
        self._entries: dict[str, tuple[float, SessionValue]] = {}
        self._writes_since_sweep = 0

    async def get(self, key: str) -> SessionValue | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if self._clock() >= expires_at:
            self._entries.pop(key, None)
            return None
        # A copy, so a caller mutating the result cannot corrupt the store —
        # the Redis backend cannot leak references, and neither should this one.
        return dict(value)

    async def set(self, key: str, value: SessionValue, *, ttl_s: int | None = None) -> None:
        ttl = self._default_ttl_s if ttl_s is None else ttl_s
        self._entries[key] = (self._clock() + ttl, dict(value))

        self._writes_since_sweep += 1
        if self._writes_since_sweep >= _SWEEP_EVERY_WRITES:
            self._sweep()

    async def delete(self, key: str) -> None:
        self._entries.pop(key, None)

    async def ping(self) -> bool:
        return True

    async def aclose(self) -> None:
        self._entries.clear()

    def _sweep(self) -> None:
        now = self._clock()
        expired = [key for key, (expires_at, _) in self._entries.items() if now >= expires_at]
        for key in expired:
            del self._entries[key]
        self._writes_since_sweep = 0
        if expired:
            log.debug("session_sweep", removed=len(expired), remaining=len(self._entries))

    def __len__(self) -> int:
        return len(self._entries)


class RedisSessionStore(SessionStore):
    """Redis-backed store. Survives restarts and is shared across replicas.

    `redis` is an optional dependency (`uv sync --extra redis`), imported lazily
    so a memory-backed deployment never needs it installed.
    """

    def __init__(self, url: str, *, default_ttl_s: int, prefix: str = "dg:") -> None:
        self._url = url
        self._default_ttl_s = default_ttl_s
        self._prefix = prefix
        self._client: Any = None

    def _connect(self) -> Any:
        if self._client is None:
            try:
                from redis.asyncio import Redis
            except ImportError as exc:  # pragma: no cover - only without the extra
                raise RuntimeError(
                    "SESSION_BACKEND=redis needs the optional extra: uv sync --extra redis"
                ) from exc
            self._client = Redis.from_url(self._url, decode_responses=True)
        return self._client

    def _key(self, key: str) -> str:
        return f"{self._prefix}{key}"

    async def get(self, key: str) -> SessionValue | None:
        raw = await self._connect().get(self._key(key))
        if raw is None:
            return None
        try:
            decoded: SessionValue = json.loads(raw)
        except json.JSONDecodeError:
            # Someone else's key, or a value written by an older format. Drop it
            # rather than crashing a handler over it.
            log.warning("session_value_unreadable", key=key)
            await self.delete(key)
            return None
        return decoded

    async def set(self, key: str, value: SessionValue, *, ttl_s: int | None = None) -> None:
        ttl = self._default_ttl_s if ttl_s is None else ttl_s
        await self._connect().set(self._key(key), json.dumps(value), ex=max(ttl, 1))

    async def delete(self, key: str) -> None:
        await self._connect().delete(self._key(key))

    async def ping(self) -> bool:
        try:
            return bool(await self._connect().ping())
        # Readiness must never raise, whatever redis-py decides to throw.
        except Exception as exc:
            log.warning("redis_unreachable", error=str(exc))
            return False

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


def build_session_store(settings: Settings) -> SessionStore:
    """The single place a session backend is chosen."""
    if settings.session_backend is SessionBackend.REDIS:
        return RedisSessionStore(settings.redis_url, default_ttl_s=settings.session_ttl_s)
    return MemorySessionStore(default_ttl_s=settings.session_ttl_s)


__all__ = ["MemorySessionStore", "RedisSessionStore", "build_session_store"]
