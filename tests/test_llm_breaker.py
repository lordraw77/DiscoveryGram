"""The per-provider circuit breaker.

The behaviour worth pinning down is not "it opens after N failures" — it is
what happens at the edges: the single half-open probe, what a failed probe
costs, and the fact that a successful call forgets the count entirely.
"""

from __future__ import annotations

import pytest

from discoverygram.llm.breaker import CircuitBreaker, CircuitState


class Clock:
    """A hand-wound monotonic clock, so cool-downs need no sleeping."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def breaker(clock: Clock) -> CircuitBreaker:
    return CircuitBreaker(failure_threshold=3, reset_s=60.0, clock=clock)


def test_an_unknown_provider_is_allowed_and_reads_as_closed(breaker: CircuitBreaker) -> None:
    assert breaker.allows("groq") is True
    assert breaker.state("groq") is CircuitState.CLOSED
    assert breaker.status("groq").healthy is True


def test_failures_below_the_threshold_do_not_open_it(breaker: CircuitBreaker) -> None:
    breaker.record_failure("groq")
    breaker.record_failure("groq")

    assert breaker.allows("groq") is True
    assert breaker.status("groq").failures == 2


def test_the_threshold_opens_it_and_every_call_is_refused(breaker: CircuitBreaker) -> None:
    for _ in range(3):
        breaker.record_failure("groq", reason="LlmUnavailable")

    assert breaker.state("groq") is CircuitState.OPEN
    assert breaker.allows("groq") is False
    assert breaker.status("groq").last_error == "LlmUnavailable"


def test_an_immediate_failure_opens_it_on_the_first_one(breaker: CircuitBreaker) -> None:
    """A rejected API key does not need four more confirmations."""
    breaker.record_failure("groq", reason="LlmAuthError", immediate=True)

    assert breaker.allows("groq") is False
    assert breaker.status("groq").failures == 1


def test_a_success_forgets_the_failure_count(breaker: CircuitBreaker) -> None:
    breaker.record_failure("groq")
    breaker.record_failure("groq")
    breaker.record_success("groq")
    breaker.record_failure("groq")
    breaker.record_failure("groq")

    assert breaker.allows("groq") is True


def test_the_cool_down_is_honoured_to_the_second(breaker: CircuitBreaker, clock: Clock) -> None:
    breaker.record_failure("groq", immediate=True)

    clock.advance(59.9)
    assert breaker.allows("groq") is False

    clock.advance(0.2)
    assert breaker.allows("groq") is True


def test_only_one_caller_becomes_the_half_open_probe(breaker: CircuitBreaker, clock: Clock) -> None:
    """Without this, every request queued during the outage becomes a probe."""
    breaker.record_failure("groq", immediate=True)
    clock.advance(61)

    admitted = [breaker.allows("groq") for _ in range(5)]

    assert admitted == [True, False, False, False, False]
    assert breaker.state("groq") is CircuitState.HALF_OPEN


def test_a_successful_probe_closes_the_circuit(breaker: CircuitBreaker, clock: Clock) -> None:
    breaker.record_failure("groq", immediate=True)
    clock.advance(61)
    assert breaker.allows("groq") is True

    breaker.record_success("groq")

    assert breaker.state("groq") is CircuitState.CLOSED
    assert breaker.allows("groq") is True
    assert breaker.status("groq").failures == 0


def test_a_failed_probe_re_opens_for_a_full_cool_down(
    breaker: CircuitBreaker, clock: Clock
) -> None:
    breaker.record_failure("groq", immediate=True)
    clock.advance(61)
    breaker.allows("groq")

    breaker.record_failure("groq", reason="LlmUnavailable")

    assert breaker.allows("groq") is False
    clock.advance(59)
    assert breaker.allows("groq") is False
    clock.advance(2)
    assert breaker.allows("groq") is True


def test_providers_are_tracked_independently(breaker: CircuitBreaker) -> None:
    breaker.record_failure("groq", immediate=True)

    assert breaker.allows("groq") is False
    assert breaker.allows("gemini") is True


def test_the_status_snapshot_reports_the_remaining_cool_down(
    breaker: CircuitBreaker, clock: Clock
) -> None:
    breaker.record_failure("groq", immediate=True)
    clock.advance(20)

    status = breaker.status("groq")

    assert status.state is CircuitState.OPEN
    assert status.opens_remaining_s == pytest.approx(40.0)
    assert status.healthy is False


def test_a_snapshot_keeps_the_order_it_was_asked_for(breaker: CircuitBreaker) -> None:
    breaker.record_failure("gemini", immediate=True)

    snapshot = breaker.snapshot(["groq", "gemini", "ollama"])

    assert [status.provider for status in snapshot] == ["groq", "gemini", "ollama"]
    assert [status.healthy for status in snapshot] == [True, False, True]


def test_reset_forgets_everything(breaker: CircuitBreaker) -> None:
    breaker.record_failure("groq", immediate=True)
    breaker.reset()
    assert breaker.allows("groq") is True


def test_a_threshold_below_one_is_refused() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        CircuitBreaker(failure_threshold=0)
