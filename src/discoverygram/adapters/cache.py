"""TTL caching for the vault's hot reads.

Two reads dominate everything the bot does and neither changes often: the
folder tree (rebuilt from a full note listing, because NoteDiscovery has no
tree endpoint) and the tag index. Both are re-read on nearly every `/browse`
tap and every `/tag` with no argument, so both are cached.

The rules are the same for both, and they are the interesting part:

* **A write invalidates, the TTL only bounds staleness.** A note created from
  Telegram appears in `/browse` on the next tap, not after five minutes,
  because every mutating adapter method drops the caches it affects. The TTL
  exists for the *other* writer — someone editing the vault in the
  NoteDiscovery web UI — whom we never hear about.
* **One loader runs, however many callers are waiting.** A cold cache and a
  burst of taps would otherwise become a burst of full note listings against
  an instance that is already the slow part. Callers queue on the lock and the
  ones that arrive late find the value already there.
* **A failed load is not cached.** The exception propagates and the next
  caller tries again, because caching an outage would keep the bot broken
  after the vault came back.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

from discoverygram.util import metrics
from discoverygram.util.logging import get_logger

log = get_logger(__name__)


class TtlCache[T]:
    """One cached value, loaded on demand, safe under concurrent callers."""

    def __init__(
        self,
        loader: Callable[[], Awaitable[T]],
        *,
        ttl_s: float,
        name: str = "cache",
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._loader = loader
        self._ttl_s = ttl_s
        self._name = name
        self._clock = clock
        self._lock = asyncio.Lock()
        self._value: T | None = None
        self._loaded_at = 0.0

    @property
    def name(self) -> str:
        return self._name

    @property
    def is_fresh(self) -> bool:
        if self._value is None:
            return False
        if self._ttl_s <= 0:
            return False
        return (self._clock() - self._loaded_at) < self._ttl_s

    async def get(self, *, refresh: bool = False) -> T:
        if not refresh and self.is_fresh and self._value is not None:
            metrics.CACHE_EVENTS.inc(cache=self._name, event="hit")
            return self._value

        async with self._lock:
            # Another caller may have refreshed it while we waited for the lock.
            if not refresh and self.is_fresh and self._value is not None:
                metrics.CACHE_EVENTS.inc(cache=self._name, event="hit")
                return self._value
            metrics.CACHE_EVENTS.inc(cache=self._name, event="miss")
            # Assigned only on success: an exception leaves the previous value
            # in place and is raised to the caller, who will try again.
            value = await self._loader()
            self._value = value
            self._loaded_at = self._clock()
            return value

    def invalidate(self) -> None:
        if self._value is not None:
            metrics.CACHE_EVENTS.inc(cache=self._name, event="invalidate")
        self._value = None
        self._loaded_at = 0.0


__all__ = ["TtlCache"]
