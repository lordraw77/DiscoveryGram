"""Collecting a Telegram album into one capture.

Telegram has no "album" update. Sending three photos at once produces **three
separate updates**, related only by a shared `media_group_id`, and the Bot API
promises nothing about how far apart they arrive. A handler that treated each
one on its own would create three notes from what the user sent as one thing.

So the first update of a group opens a short window, waits, and then takes
everything that arrived in it. The later updates return `None` and do nothing
— their photo is already in the first caller's list.

The window is a real trade-off and it is deliberately short: too long and the
user waits for a bot that looks stuck, too short and the last photo of a slow
album starts a second note. `ALBUM_WINDOW_S` is the value that holds for the
albums Telegram actually delivers, and a photo that misses the window becomes
its own draft rather than being lost.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from discoverygram.util.logging import get_logger

log = get_logger(__name__)

# Telegram's own cap on an album.
MAX_ALBUM_SIZE = 10
ALBUM_WINDOW_S = 1.5


class AlbumBuffer[T]:
    """Groups updates that share a `media_group_id`."""

    def __init__(
        self,
        *,
        window_s: float = ALBUM_WINDOW_S,
        max_size: int = MAX_ALBUM_SIZE,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._window_s = window_s
        self._max_size = max_size
        self._sleep = sleep
        self._groups: dict[str, list[T]] = {}

    async def collect(self, group_id: str, item: T) -> list[T] | None:
        """Add `item`; return the whole group to the **first** caller only.

        Returns `None` to every later caller, meaning "absorbed, do nothing".
        The dictionary entry is created before the first `await`, which is what
        makes "first" well defined: the event loop cannot interleave another
        caller between the check and the insert.
        """
        existing = self._groups.get(group_id)
        if existing is not None:
            if len(existing) < self._max_size:
                existing.append(item)
            else:
                log.info("album_item_dropped", group=group_id, cap=self._max_size)
            return None

        self._groups[group_id] = [item]
        await self._sleep(self._window_s)
        items = self._groups.pop(group_id, [])
        log.info("album_collected", group=group_id, items=len(items))
        return items

    @property
    def pending(self) -> int:
        """Groups still inside their window. For tests and for shutdown checks."""
        return len(self._groups)


__all__ = ["ALBUM_WINDOW_S", "MAX_ALBUM_SIZE", "AlbumBuffer"]
