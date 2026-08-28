"""The TTL cache behind the hot reads.

Three properties matter more than the caching itself: one loader per burst, a
write that is visible immediately, and an outage that is never cached.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass, field

from discoverygram.adapters.cache import TtlCache


@dataclass
class Clock:
    now: float = 0.0

    def __call__(self) -> float:
        return self.now


@dataclass
class Loader:
    """A loader that counts, and can be told to fail."""

    value: str = "first"
    calls: int = 0
    fail_next: bool = False
    delay: float = 0.0
    seen: list[str] = field(default_factory=list)

    async def __call__(self) -> str:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("the vault is down")
        return self.value


async def test_a_fresh_value_is_reused() -> None:
    loader = Loader()
    cache = TtlCache(loader, ttl_s=300, clock=Clock())

    assert await cache.get() == "first"
    assert await cache.get() == "first"
    assert loader.calls == 1


async def test_an_expired_value_is_reloaded() -> None:
    loader = Loader()
    clock = Clock()
    cache = TtlCache(loader, ttl_s=300, clock=clock)

    await cache.get()
    clock.now = 301.0
    await cache.get()

    assert loader.calls == 2


async def test_a_zero_ttl_disables_caching_entirely() -> None:
    """`TREE_CACHE_TTL_S=0` is the documented way to turn it off."""
    loader = Loader()
    cache = TtlCache(loader, ttl_s=0, clock=Clock())

    await cache.get()
    await cache.get()

    assert loader.calls == 2


async def test_invalidate_makes_the_next_read_go_to_the_source() -> None:
    """A note created from Telegram must appear in /browse on the next tap."""
    loader = Loader()
    cache = TtlCache(loader, ttl_s=300, clock=Clock())

    await cache.get()
    loader.value = "second"
    cache.invalidate()

    assert await cache.get() == "second"
    assert loader.calls == 2


async def test_a_burst_of_callers_runs_one_loader() -> None:
    """A cold cache and ten taps must not become ten full vault listings."""
    loader = Loader(delay=0.01)
    cache = TtlCache(loader, ttl_s=300, clock=Clock())

    results = await asyncio.gather(*(cache.get() for _ in range(10)))

    assert results == ["first"] * 10
    assert loader.calls == 1


async def test_refresh_bypasses_a_fresh_value() -> None:
    loader = Loader()
    cache = TtlCache(loader, ttl_s=300, clock=Clock())

    await cache.get()
    loader.value = "second"

    assert await cache.get(refresh=True) == "second"


async def test_a_failed_load_is_not_cached() -> None:
    """Caching an outage would keep the bot broken after the vault came back."""
    loader = Loader(fail_next=True)
    cache = TtlCache(loader, ttl_s=300, clock=Clock())

    try:
        await cache.get()
    except RuntimeError:
        pass
    else:  # pragma: no cover - the loader raised
        raise AssertionError("the failure should have propagated")

    assert await cache.get() == "first"
    assert loader.calls == 2


async def test_a_failed_refresh_leaves_the_previous_value_in_place() -> None:
    """Half a tree is worse than a slightly old one."""
    loader = Loader()
    cache = TtlCache(loader, ttl_s=300, clock=Clock())
    await cache.get()

    loader.fail_next = True
    with suppress(RuntimeError):
        await cache.get(refresh=True)

    assert await cache.get() == "first"
