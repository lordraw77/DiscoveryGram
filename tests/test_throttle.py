"""Client-side throttling ahead of NoteDiscovery's per-endpoint rate limits."""

from __future__ import annotations

from discoverygram.adapters.throttle import ENDPOINT_LIMITS_PER_MINUTE, RateLimiter


class FakeClock:
    """Monotonic time the test drives, advanced by the fake sleep."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def limiter(clock: FakeClock, limit: int) -> RateLimiter:
    return RateLimiter({"note_append": limit}, margin=1.0, clock=clock, sleep=clock.sleep)


async def test_calls_under_the_limit_never_wait() -> None:
    clock = FakeClock()
    limiter_ = limiter(clock, 3)

    for _ in range(3):
        assert await limiter_.acquire("note_append") == 0.0

    assert clock.slept == []


async def test_the_call_over_the_limit_waits_for_the_window_to_slide() -> None:
    clock = FakeClock()
    limiter_ = limiter(clock, 2)

    await limiter_.acquire("note_append")
    clock.now = 10.0
    await limiter_.acquire("note_append")

    waited = await limiter_.acquire("note_append")

    # The window opens 60s after the *first* call, which was 10s before now.
    assert waited == 50.0


async def test_an_unlisted_bucket_is_not_throttled() -> None:
    clock = FakeClock()

    assert await limiter(clock, 1).acquire("search") == 0.0
    assert clock.slept == []


def test_the_safety_margin_keeps_us_under_the_server_limit() -> None:
    """Our window and the server's are unaligned, so we aim below its ceiling."""
    limiter_ = RateLimiter()

    for bucket, server_limit in ENDPOINT_LIMITS_PER_MINUTE.items():
        client_limit = limiter_.limit_for(bucket)
        assert client_limit is not None
        assert client_limit < server_limit, bucket


def test_the_declared_limits_match_the_contract() -> None:
    """Copied from NoteDiscovery 0.31.3's slowapi decorators; drift breaks pacing."""
    assert ENDPOINT_LIMITS_PER_MINUTE["note_append"] == 60
    assert ENDPOINT_LIMITS_PER_MINUTE["note_write"] == 300
    assert ENDPOINT_LIMITS_PER_MINUTE["media_upload"] == 20
