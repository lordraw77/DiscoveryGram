"""Client-side throttling for NoteDiscovery's per-endpoint rate limits.

The limits are declared server-side with `slowapi` and are real: appends are
capped at 60/minute, deletes and moves at 20-30/minute, media uploads at
20/minute. A Telegram user driving a bulk operation would hit them, so the
adapter paces itself instead of collecting 429s.

The limiter is a per-bucket sliding window. It is deliberately a little stricter
than the server (`_SAFETY_MARGIN`), because the server's window and ours are not
aligned and a burst at the boundary would otherwise slip through.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Callable

# Server-side limits read from NoteDiscovery 0.31.3 `backend/main.py`.
# Anything absent here is unlimited server-side and needs no pacing.
ENDPOINT_LIMITS_PER_MINUTE: dict[str, int] = {
    "note_write": 300,
    "note_append": 60,
    "note_delete": 30,
    "note_move": 30,
    "folder_create": 30,
    "folder_move": 20,
    "folder_rename": 30,
    "folder_delete": 20,
    "media_upload": 20,
    "template_read": 120,
    "template_create": 60,
    "share_write": 30,
    "share_read": 120,
    "stats": 30,
    "export": 30,
    "plugin_toggle": 10,
}

_WINDOW_S = 60.0
_SAFETY_MARGIN = 0.9


class RateLimiter:
    """Sliding-window limiter over the named buckets above."""

    def __init__(
        self,
        limits: dict[str, int] | None = None,
        *,
        window_s: float = _WINDOW_S,
        margin: float = _SAFETY_MARGIN,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        source = ENDPOINT_LIMITS_PER_MINUTE if limits is None else limits
        self._limits = {bucket: max(1, int(limit * margin)) for bucket, limit in source.items()}
        self._window_s = window_s
        self._clock = clock
        self._sleep = sleep or asyncio.sleep
        self._calls: dict[str, deque[float]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def limit_for(self, bucket: str) -> int | None:
        return self._limits.get(bucket)

    def _lock(self, bucket: str) -> asyncio.Lock:
        lock = self._locks.get(bucket)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[bucket] = lock
        return lock

    async def acquire(self, bucket: str) -> float:
        """Block until a call to `bucket` is allowed. Returns seconds waited."""
        limit = self._limits.get(bucket)
        if limit is None:
            return 0.0

        waited = 0.0
        async with self._lock(bucket):
            calls = self._calls.setdefault(bucket, deque())
            while True:
                now = self._clock()
                while calls and now - calls[0] >= self._window_s:
                    calls.popleft()

                if len(calls) < limit:
                    calls.append(now)
                    return waited

                delay = self._window_s - (now - calls[0])
                # A stale clock could yield a non-positive delay; never spin.
                delay = max(delay, 0.01)
                await self._sleep(delay)
                waited += delay
