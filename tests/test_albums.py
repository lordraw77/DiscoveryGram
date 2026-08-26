"""Collecting an album into one capture.

Telegram sends each photo of an album as its own update. Getting this wrong
means three photos become three notes, which is exactly what the user did not
ask for — so the "first caller takes the group" contract is asserted directly.
"""

from __future__ import annotations

import asyncio

from discoverygram.bot.albums import AlbumBuffer


class Clock:
    """Records the waits instead of performing them."""

    def __init__(self) -> None:
        self.waits: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.waits.append(delay)
        # Yield, so other collectors queued behind this one actually run.
        await asyncio.sleep(0)


async def test_a_single_item_comes_straight_back() -> None:
    buffer: AlbumBuffer[str] = AlbumBuffer(sleep=Clock())

    assert await buffer.collect("g1", "a") == ["a"]


async def test_only_the_first_caller_receives_the_group() -> None:
    """The later updates are absorbed: their photo is already in the list."""
    clock = Clock()
    buffer: AlbumBuffer[str] = AlbumBuffer(sleep=clock)

    first = asyncio.create_task(buffer.collect("g1", "a"))
    await asyncio.sleep(0)
    second = await buffer.collect("g1", "b")
    third = await buffer.collect("g1", "c")

    assert await first == ["a", "b", "c"]
    assert second is None
    assert third is None


async def test_two_albums_do_not_mix() -> None:
    clock = Clock()
    buffer: AlbumBuffer[str] = AlbumBuffer(sleep=clock)

    first = asyncio.create_task(buffer.collect("g1", "a1"))
    second = asyncio.create_task(buffer.collect("g2", "b1"))
    await asyncio.sleep(0)
    await buffer.collect("g1", "a2")
    await buffer.collect("g2", "b2")

    assert await first == ["a1", "a2"]
    assert await second == ["b1", "b2"]


async def test_the_group_is_released_when_the_window_closes() -> None:
    """A leaked group would grow forever in a long-running bot."""
    buffer: AlbumBuffer[str] = AlbumBuffer(sleep=Clock())

    await buffer.collect("g1", "a")

    assert buffer.pending == 0


async def test_the_album_cap_is_respected() -> None:
    """Telegram caps an album at ten; more than that is a malformed client."""
    clock = Clock()
    buffer: AlbumBuffer[str] = AlbumBuffer(sleep=clock, max_size=3)

    first = asyncio.create_task(buffer.collect("g1", "a"))
    await asyncio.sleep(0)
    for extra in ("b", "c", "d", "e"):
        await buffer.collect("g1", extra)

    assert await first == ["a", "b", "c"]


async def test_the_window_is_the_configured_one() -> None:
    clock = Clock()
    buffer: AlbumBuffer[str] = AlbumBuffer(window_s=2.5, sleep=clock)

    await buffer.collect("g1", "a")

    assert clock.waits == [2.5]


async def test_a_second_album_with_the_same_id_starts_a_new_group() -> None:
    """The id is reused only after the first group has been taken."""
    buffer: AlbumBuffer[str] = AlbumBuffer(sleep=Clock())

    assert await buffer.collect("g1", "a") == ["a"]
    assert await buffer.collect("g1", "b") == ["b"]
