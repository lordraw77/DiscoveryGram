"""Usage accounting and the per-user daily cap."""

from __future__ import annotations

import pytest

from discoverygram.llm.plan import TaskProfile
from discoverygram.llm.usage import DailyCallCap, UsageLedger, UsageRecord
from discoverygram.ports.llm import Usage
from discoverygram.ports.llm_errors import LlmQuotaExceeded


class Clock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# --- Ledger --------------------------------------------------------------


def test_a_completion_is_recorded_against_its_provider() -> None:
    ledger = UsageLedger()

    ledger.record_completion(
        provider="groq",
        model="llama-3.3-70b",
        task=TaskProfile.CHAT,
        usage=Usage(prompt_tokens=10, completion_tokens=5),
        latency_s=0.4,
    )

    usage = ledger.per_provider()[0]
    assert usage.provider == "groq"
    assert usage.calls == 1
    assert usage.failures == 0
    assert usage.tokens == 15
    assert usage.average_latency_s == pytest.approx(0.4)


def test_failures_are_counted_separately_and_labelled() -> None:
    ledger = UsageLedger()

    ledger.record_failure(provider="groq", model="m", task=TaskProfile.CHAT, outcome="rate_limited")
    ledger.record_completion(
        provider="groq", model="m", task=TaskProfile.CHAT, usage=Usage(), latency_s=0.1
    )

    assert ledger.per_provider()[0].failures == 1
    assert ledger.outcomes() == {"rate_limited": 1, "ok": 1}
    assert ledger.attempts == 2


def test_unreported_tokens_are_not_counted_as_zero_in_a_way_that_lies() -> None:
    """`None` means "not reported"; it must not be summed as a real zero."""
    ledger = UsageLedger()

    ledger.record_completion(
        provider="ollama", model="m", task=TaskProfile.CHAT, usage=Usage(), latency_s=0.2
    )

    assert Usage().total_tokens is None
    assert ledger.per_provider()[0].tokens == 0


def test_providers_are_reported_busiest_first() -> None:
    ledger = UsageLedger()
    for _ in range(3):
        ledger.record_failure(provider="groq", model="m", task=TaskProfile.CHAT, outcome="timeout")
    ledger.record_failure(provider="gemini", model="m", task=TaskProfile.CHAT, outcome="timeout")

    assert [usage.provider for usage in ledger.per_provider()] == ["groq", "gemini"]


def test_one_request_over_four_rungs_is_one_request_and_four_attempts() -> None:
    ledger = UsageLedger()
    for _ in range(4):
        ledger.record_failure(provider="groq", model="m", task=TaskProfile.CHAT, outcome="timeout")
    ledger.note_request(succeeded=False)

    assert ledger.requests == 1
    assert ledger.successful_requests == 0
    assert ledger.attempts == 4


def test_the_record_list_is_bounded_so_a_long_running_bot_does_not_leak() -> None:
    ledger = UsageLedger()

    for index in range(500):
        ledger.record(
            UsageRecord(
                provider="groq", model=f"m{index}", task="chat", outcome="ok", latency_s=0.0
            )
        )

    recent = ledger.recent(limit=1000)
    assert len(recent) == 200
    assert recent[-1].model == "m499"


# --- Daily cap -----------------------------------------------------------


def test_a_limit_of_zero_disables_the_cap_entirely() -> None:
    cap = DailyCallCap(0)

    for _ in range(1000):
        cap.consume(111)
    cap.check(111)

    assert cap.enabled is False
    assert cap.remaining(111) is None


def test_the_cap_refuses_the_call_after_the_limit() -> None:
    cap = DailyCallCap(3)

    for _ in range(3):
        cap.check(111)
        cap.consume(111)

    with pytest.raises(LlmQuotaExceeded) as caught:
        cap.check(111)

    assert "3 AI requests" in str(caught.value)
    assert cap.remaining(111) == 0


def test_users_have_separate_budgets() -> None:
    cap = DailyCallCap(1)
    cap.consume(111)

    with pytest.raises(LlmQuotaExceeded):
        cap.check(111)
    cap.check(222)


def test_the_budget_refills_at_midnight_utc() -> None:
    """A wall-clock day, so a user can predict when their allowance returns."""
    clock = Clock(now=86_400.0 * 100 + 3600)
    cap = DailyCallCap(2, clock=clock)

    cap.consume(111)
    cap.consume(111)
    with pytest.raises(LlmQuotaExceeded):
        cap.check(111)

    clock.advance(86_400.0)

    cap.check(111)
    assert cap.used(111) == 0


def test_the_refusal_says_how_long_until_the_reset() -> None:
    clock = Clock(now=86_400.0 * 100 + 86_400.0 - 60)
    cap = DailyCallCap(1, clock=clock)
    cap.consume(111)

    with pytest.raises(LlmQuotaExceeded) as caught:
        cap.check(111)

    assert caught.value.resets_in_s == pytest.approx(60.0)


def test_an_anonymous_caller_is_not_capped() -> None:
    """A call with no user id comes from the bot itself, not from a person."""
    cap = DailyCallCap(1)
    cap.consume(None)
    cap.check(None)
    assert cap.used(111) == 0
