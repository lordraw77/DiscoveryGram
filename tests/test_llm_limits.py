"""Per-user limits and back-pressure.

The daily cap bounds spend and was proven in phase 5. Phase 7 adds the two
things that bound *rate*: a per-minute burst limit per user, and a refusal that
costs nothing when every provider is already known to be down.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from discoverygram.config import Settings
from discoverygram.llm.breaker import CircuitBreaker
from discoverygram.llm.plan import TaskProfile
from discoverygram.llm.usage import UserRateLimiter
from discoverygram.ports.llm import Completion, LlmClient, Message, Usage
from discoverygram.ports.llm_errors import (
    LlmDegraded,
    LlmQuotaExceeded,
    LlmThrottled,
    LlmUnavailable,
)
from tests.test_llm_router import PROMPT, FakeClient, build_router


@dataclass
class Clock:
    now: float = 0.0

    def __call__(self) -> float:
        return self.now


# --- The burst limit -----------------------------------------------------


def test_calls_under_the_limit_pass() -> None:
    limiter = UserRateLimiter(3, clock=Clock())

    for _ in range(3):
        limiter.check(7)
        limiter.consume(7)

    assert limiter.used(7) == 3


def test_the_call_over_the_limit_is_refused() -> None:
    limiter = UserRateLimiter(2, clock=Clock())
    for _ in range(2):
        limiter.consume(7)

    with pytest.raises(LlmThrottled) as caught:
        limiter.check(7)

    assert "2 per minute" in str(caught.value)


def test_the_refusal_says_how_long_to_wait() -> None:
    clock = Clock()
    limiter = UserRateLimiter(1, clock=clock)
    limiter.consume(7)
    clock.now = 15.0

    with pytest.raises(LlmThrottled) as caught:
        limiter.check(7)

    assert caught.value.retry_after == pytest.approx(45.0)
    assert "45s" in str(caught.value)


def test_the_window_rolls_rather_than_resetting() -> None:
    """A fixed minute would allow twice the configured burst across a boundary."""
    clock = Clock()
    limiter = UserRateLimiter(2, clock=clock)
    limiter.consume(7)
    clock.now = 59.0
    limiter.consume(7)

    clock.now = 60.5  # the first call has aged out, the second has not
    limiter.check(7)
    limiter.consume(7)

    with pytest.raises(LlmThrottled):
        limiter.check(7)


def test_one_user_cannot_throttle_another() -> None:
    limiter = UserRateLimiter(1, clock=Clock())
    limiter.consume(7)

    limiter.check(8)


def test_a_limit_of_zero_disables_it() -> None:
    limiter = UserRateLimiter(0, clock=Clock())

    for _ in range(100):
        limiter.consume(7)
    limiter.check(7)

    assert limiter.enabled is False


def test_an_anonymous_caller_is_never_throttled() -> None:
    """A callback with no user is a wiring problem, not a budget problem."""
    limiter = UserRateLimiter(1, clock=Clock())
    limiter.consume(None)
    limiter.consume(None)

    limiter.check(None)


def test_a_user_who_stops_asking_is_forgotten() -> None:
    """The dictionary is per-user and the process runs for months."""
    clock = Clock()
    limiter = UserRateLimiter(5, clock=clock)
    limiter.consume(7)

    clock.now = 120.0

    assert limiter.used(7) == 0
    assert limiter._calls == {}


# --- The router enforces both -------------------------------------------


async def test_the_router_refuses_a_user_who_is_asking_too_fast(settings: Settings) -> None:
    tuned = settings.model_copy(update={"llm_user_rate_per_minute": 2})
    groq = FakeClient("groq")
    router = build_router(tuned, {"groq": groq}, [("groq", "a")])

    await router.complete(TaskProfile.CHAT, PROMPT, user_id=7)
    await router.complete(TaskProfile.CHAT, PROMPT, user_id=7)
    with pytest.raises(LlmThrottled):
        await router.complete(TaskProfile.CHAT, PROMPT, user_id=7)

    assert groq.models_called == ["a", "a"]


async def test_a_throttled_request_costs_nothing(settings: Settings) -> None:
    """Refusing must not spend the daily allowance the user did not get to use."""
    tuned = settings.model_copy(
        update={"llm_user_rate_per_minute": 1, "llm_daily_call_limit_per_user": 10}
    )
    router = build_router(tuned, {"groq": FakeClient("groq")}, [("groq", "a")])

    await router.complete(TaskProfile.CHAT, PROMPT, user_id=7)
    with pytest.raises(LlmThrottled):
        await router.complete(TaskProfile.CHAT, PROMPT, user_id=7)

    assert router.cap.used(7) == 1


async def test_the_daily_cap_is_reported_before_the_burst_limit(settings: Settings) -> None:
    """Asking someone who is out of budget to wait a minute would be a lie."""
    tuned = settings.model_copy(
        update={"llm_user_rate_per_minute": 1, "llm_daily_call_limit_per_user": 1}
    )
    router = build_router(tuned, {"groq": FakeClient("groq")}, [("groq", "a")])

    await router.complete(TaskProfile.CHAT, PROMPT, user_id=7)

    with pytest.raises(LlmQuotaExceeded):
        await router.complete(TaskProfile.CHAT, PROMPT, user_id=7)


async def test_another_user_is_unaffected_by_a_throttled_one(settings: Settings) -> None:
    tuned = settings.model_copy(update={"llm_user_rate_per_minute": 1})
    router = build_router(tuned, {"groq": FakeClient("groq")}, [("groq", "a")])

    await router.complete(TaskProfile.CHAT, PROMPT, user_id=7)
    completion = await router.complete(TaskProfile.CHAT, PROMPT, user_id=8)

    assert completion.text == "ok"


# --- Back-pressure -------------------------------------------------------


async def test_a_fully_degraded_ladder_is_refused_immediately(settings: Settings) -> None:
    """Walking rungs the breaker has already opened calls nothing and helps no one."""
    clock = Clock()
    breaker = CircuitBreaker(failure_threshold=1, reset_s=120.0, clock=clock)
    breaker.record_failure("groq", reason="down", immediate=True)
    breaker.record_failure("gemini", reason="down", immediate=True)
    groq, gemini = FakeClient("groq"), FakeClient("gemini")
    router = build_router(
        settings,
        {"groq": groq, "gemini": gemini},
        [("groq", "a"), ("gemini", "b")],
        breaker=breaker,
    )

    with pytest.raises(LlmDegraded) as caught:
        await router.complete(TaskProfile.CHAT, PROMPT, user_id=7)

    assert caught.value.providers == ("groq", "gemini")
    assert "120s" in str(caught.value)
    assert groq.calls == [] and gemini.calls == []


async def test_back_pressure_does_not_spend_the_users_budget(settings: Settings) -> None:
    """The user asked for something the bot refused to attempt; that is free."""
    tuned = settings.model_copy(update={"llm_daily_call_limit_per_user": 10})
    breaker = CircuitBreaker(failure_threshold=1, reset_s=60.0, clock=Clock())
    breaker.record_failure("groq", reason="down", immediate=True)
    router = build_router(tuned, {"groq": FakeClient("groq")}, [("groq", "a")], breaker=breaker)

    with pytest.raises(LlmDegraded):
        await router.complete(TaskProfile.CHAT, PROMPT, user_id=7)

    assert router.cap.used(7) == 0
    assert router.rate.used(7) == 0


async def test_one_healthy_provider_is_enough_to_proceed(settings: Settings) -> None:
    breaker = CircuitBreaker(failure_threshold=1, reset_s=60.0, clock=Clock())
    breaker.record_failure("groq", reason="down", immediate=True)
    gemini = FakeClient("gemini")
    router = build_router(
        settings,
        {"groq": FakeClient("groq"), "gemini": gemini},
        [("groq", "a"), ("gemini", "b")],
        breaker=breaker,
    )

    completion = await router.complete(TaskProfile.CHAT, PROMPT, user_id=7)

    assert completion.text == "ok"
    assert gemini.models_called == ["b"]


async def test_the_circuit_reopens_and_traffic_resumes(settings: Settings) -> None:
    """Back-pressure has to end by itself, or it is just an outage we caused."""
    clock = Clock()
    breaker = CircuitBreaker(failure_threshold=1, reset_s=60.0, clock=clock)
    breaker.record_failure("groq", reason="down", immediate=True)
    groq = FakeClient("groq")
    router = build_router(settings, {"groq": groq}, [("groq", "a")], breaker=breaker)

    with pytest.raises(LlmDegraded):
        await router.complete(TaskProfile.CHAT, PROMPT, user_id=7)

    clock.now = 61.0
    completion = await router.complete(TaskProfile.CHAT, PROMPT, user_id=7)

    assert completion.text == "ok"


# --- The concurrency bound ----------------------------------------------


class CountingClient(LlmClient):
    """Reports the highest number of calls it ever had in flight at once."""

    def __init__(self) -> None:
        self.name = "groq"
        self.in_flight = 0
        self.peak = 0
        self._release = asyncio.Event()

    async def complete(
        self,
        *,
        model: str,
        messages: list[Message] | object = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> Completion:
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            await asyncio.sleep(0)
            await self._release.wait()
        finally:
            self.in_flight -= 1
        return Completion(
            text="ok",
            provider=self.name,
            model=model,
            usage=Usage(prompt_tokens=1, completion_tokens=1),
            latency_s=0.0,
        )

    def release(self) -> None:
        self._release.set()

    def supports_vision(self) -> bool:
        return True

    async def aclose(self) -> None:
        return None


async def test_provider_calls_are_bounded_process_wide(settings: Settings) -> None:
    """Sixteen concurrent captures must not become sixteen simultaneous calls."""
    tuned = settings.model_copy(
        update={"llm_max_concurrent_requests": 2, "llm_user_rate_per_minute": 0}
    )
    client = CountingClient()
    router = build_router(tuned, {"groq": client}, [("groq", "a")])

    calls = [
        asyncio.create_task(router.complete(TaskProfile.CHAT, PROMPT, user_id=index))
        for index in range(6)
    ]
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    peak_while_blocked = client.peak
    client.release()
    await asyncio.gather(*calls)

    assert peak_while_blocked == 2
    assert client.peak == 2


async def test_a_zero_bound_means_unbounded(settings: Settings) -> None:
    tuned = settings.model_copy(
        update={"llm_max_concurrent_requests": 0, "llm_user_rate_per_minute": 0}
    )
    client = CountingClient()
    router = build_router(tuned, {"groq": client}, [("groq", "a")])

    calls = [
        asyncio.create_task(router.complete(TaskProfile.CHAT, PROMPT, user_id=index))
        for index in range(6)
    ]
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    client.release()
    await asyncio.gather(*calls)

    assert client.peak == 6


async def test_the_bound_is_released_when_a_call_fails(settings: Settings) -> None:
    """A leaked permit would wedge every later request behind a dead one."""
    tuned = settings.model_copy(update={"llm_max_concurrent_requests": 1})
    groq = FakeClient("groq")
    groq.program("a", LlmUnavailable("down"), LlmUnavailable("down"), "recovered")
    router = build_router(tuned, {"groq": groq}, [("groq", "a")])

    completion = await router.complete(TaskProfile.CHAT, PROMPT, user_id=7)

    assert completion.text == "recovered"
