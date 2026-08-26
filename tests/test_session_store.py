"""Session store backends.

The store is on the request path, not a cache: a lost entry means a button in a
user's chat stops working. These tests are about expiry being exact and values
not leaking between callers.
"""

from __future__ import annotations

import pytest

from discoverygram.adapters.session import (
    MemorySessionStore,
    RedisSessionStore,
    build_session_store,
)
from discoverygram.config import SessionBackend, Settings


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


async def test_round_trip() -> None:
    store = MemorySessionStore(default_ttl_s=60, clock=Clock())

    await store.set("k", {"path": "Projects/Ideas.md"})

    assert await store.get("k") == {"path": "Projects/Ideas.md"}


async def test_a_missing_key_is_none_not_an_error() -> None:
    store = MemorySessionStore(default_ttl_s=60, clock=Clock())

    assert await store.get("nope") is None
    await store.delete("nope")


async def test_an_entry_expires_exactly_at_its_ttl() -> None:
    clock = Clock()
    store = MemorySessionStore(default_ttl_s=60, clock=clock)
    await store.set("k", {"v": 1})

    clock.now = 59.0
    assert await store.get("k") is not None

    clock.now = 60.0
    assert await store.get("k") is None


async def test_a_per_entry_ttl_overrides_the_default() -> None:
    clock = Clock()
    store = MemorySessionStore(default_ttl_s=3600, clock=clock)
    await store.set("short", {"v": 1}, ttl_s=10)

    clock.now = 11.0

    assert await store.get("short") is None


async def test_stored_values_are_copied_in_and_out() -> None:
    """A handler mutating what it got back must not corrupt the store."""
    store = MemorySessionStore(default_ttl_s=60, clock=Clock())
    original = {"page": 1}

    await store.set("k", original)
    original["page"] = 99
    fetched = await store.get("k")
    assert fetched is not None
    fetched["page"] = 42

    assert await store.get("k") == {"page": 1}


async def test_expired_entries_are_swept_so_the_dict_stays_bounded() -> None:
    """Entries nobody reads again would otherwise leak for the process lifetime."""
    clock = Clock()
    store = MemorySessionStore(default_ttl_s=10, clock=clock)

    for index in range(150):
        await store.set(f"k{index}", {"i": index})
        clock.now += 1.0

    assert len(store) < 150


async def test_memory_store_pings_and_closes() -> None:
    store = MemorySessionStore(default_ttl_s=60)
    await store.set("k", {"v": 1})

    assert await store.ping() is True

    await store.aclose()
    assert await store.get("k") is None


def test_the_factory_defaults_to_memory(settings: Settings) -> None:
    assert isinstance(build_session_store(settings), MemorySessionStore)


def test_the_factory_selects_redis_when_configured(settings: Settings) -> None:
    store = build_session_store(
        settings.model_copy(
            update={
                "session_backend": SessionBackend.REDIS,
                "redis_url": "redis://localhost:6379/0",
            }
        )
    )

    assert isinstance(store, RedisSessionStore)


def test_redis_requires_a_url(env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Choosing Redis without a URL is a misconfiguration, caught at startup."""
    monkeypatch.setenv("SESSION_BACKEND", "redis")

    with pytest.raises(ValueError, match="REDIS_URL"):
        Settings()  # type: ignore[call-arg]


class FakeRedis:
    """The slice of redis.asyncio.Redis the adapter uses."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.expiries: dict[str, int] = {}
        self.closed = False
        self.reachable = True

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.values[key] = value
        if ex is not None:
            self.expiries[key] = ex

    async def delete(self, key: str) -> None:
        self.values.pop(key, None)

    async def ping(self) -> bool:
        if not self.reachable:
            raise ConnectionError("refused")
        return True

    async def aclose(self) -> None:
        self.closed = True


def redis_store(fake: FakeRedis, *, ttl: int = 60) -> RedisSessionStore:
    store = RedisSessionStore("redis://test", default_ttl_s=ttl)
    store._client = fake
    return store


async def test_redis_round_trip_and_key_prefix() -> None:
    """The prefix is what keeps the bot's keys out of anyone else's namespace."""
    fake = FakeRedis()
    store = redis_store(fake)

    await store.set("cb:abc", {"path": "A.md"})

    assert await store.get("cb:abc") == {"path": "A.md"}
    assert list(fake.values) == ["dg:cb:abc"]
    assert fake.expiries["dg:cb:abc"] == 60


async def test_redis_expiry_is_delegated_to_the_server() -> None:
    fake = FakeRedis()
    store = redis_store(fake)

    await store.set("k", {"v": 1}, ttl_s=5)

    assert fake.expiries["dg:k"] == 5


async def test_redis_drops_a_value_it_cannot_decode() -> None:
    """A key written by an older format must not crash a handler."""
    fake = FakeRedis()
    fake.values["dg:k"] = "not json"
    store = redis_store(fake)

    assert await store.get("k") is None
    assert "dg:k" not in fake.values


async def test_redis_ping_is_false_when_unreachable() -> None:
    fake = FakeRedis()
    fake.reachable = False

    assert await redis_store(fake).ping() is False


async def test_redis_close_releases_the_client() -> None:
    fake = FakeRedis()
    store = redis_store(fake)

    await store.aclose()

    assert fake.closed is True
