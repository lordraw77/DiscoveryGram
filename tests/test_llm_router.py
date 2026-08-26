"""The router: retry, failover, the circuit breaker and the daily cap.

The headline test is `test_the_third_rung_serves_when_the_first_two_fail` —
phase 5's Definition of Done, asserted against a fake provider rather than
assumed from the code.

Every test here uses a fake `LlmClient` and an injected sleep, so the suite
exercises real retry counts and real backoff arithmetic without ever waiting.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from discoverygram.config import Settings
from discoverygram.llm.breaker import CircuitBreaker, CircuitState
from discoverygram.llm.plan import TASK_DEFAULTS, Attempt, TaskProfile
from discoverygram.llm.router import MAX_SLEEP_S, LlmRouter, TaskLadder
from discoverygram.llm.usage import DailyCallCap, UsageLedger
from discoverygram.ports.llm import Completion, LlmClient, Message, Usage
from discoverygram.ports.llm_errors import (
    LlmAuthError,
    LlmBadResponse,
    LlmError,
    LlmInvalidRequest,
    LlmNoProvider,
    LlmQuotaExceeded,
    LlmRateLimited,
    LlmTimeout,
    LlmUnavailable,
)

PROMPT = [Message(role="user", text="hello")]


class FakeClient(LlmClient):
    """A provider whose every model can be scripted independently."""

    def __init__(self, name: str, *, vision: bool = True) -> None:
        self.name = name
        self._vision = vision
        #: model -> list of outcomes, consumed in order. A `LlmError` is
        #: raised; anything else is returned as the completion text. The last
        #: entry repeats once the list runs out.
        self.script: dict[str, list[object]] = {}
        self.calls: list[tuple[str, int | None, float | None]] = []
        self.closed = False

    def program(self, model: str, *outcomes: object) -> None:
        self.script[model] = list(outcomes)

    async def complete(
        self,
        *,
        model: str,
        messages: Sequence[Message],
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> Completion:
        self.calls.append((model, max_tokens, temperature))
        outcomes = self.script.get(model, ["ok"])
        outcome = outcomes.pop(0) if len(outcomes) > 1 else outcomes[0]
        if isinstance(outcome, LlmError):
            outcome.provider = self.name
            outcome.model = model
            raise outcome
        return Completion(
            text=str(outcome),
            provider=self.name,
            model=model,
            usage=Usage(prompt_tokens=7, completion_tokens=3),
            latency_s=0.05,
        )

    def supports_vision(self) -> bool:
        return self._vision

    async def aclose(self) -> None:
        self.closed = True

    @property
    def models_called(self) -> list[str]:
        return [model for model, _, _ in self.calls]


class Sleeper:
    """Records what the router would have waited for, without waiting."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


def build_router(
    settings: Settings,
    clients: dict[str, LlmClient],
    rungs: list[tuple[str, str]],
    *,
    sleeper: Sleeper | None = None,
    breaker: CircuitBreaker | None = None,
    cap: DailyCallCap | None = None,
    skipped: tuple[str, ...] = (),
) -> LlmRouter:
    ladder = TaskLadder(
        task=TaskProfile.CHAT,
        attempts=tuple(Attempt(provider=p, model=m) for p, m in rungs),
        skipped=skipped,
    )
    ladders = {
        TaskProfile.CHAT: ladder,
        TaskProfile.TITLE: TaskLadder(task=TaskProfile.TITLE, attempts=ladder.attempts),
        TaskProfile.VISION: TaskLadder(task=TaskProfile.VISION),
    }
    return LlmRouter(
        settings,
        clients,
        ladders,
        breaker=breaker,
        ledger=UsageLedger(),
        cap=cap,
        sleep=sleeper or Sleeper(),
        # Deterministic jitter: the ceiling itself, so backoff arithmetic is
        # assertable rather than flaky.
        jitter=lambda ceiling: ceiling,
    )


# --- The happy path ------------------------------------------------------


async def test_the_first_rung_serves_when_it_works(settings: Settings) -> None:
    groq = FakeClient("groq")
    router = build_router(settings, {"groq": groq}, [("groq", "a"), ("groq", "b")])

    completion = await router.complete(TaskProfile.CHAT, PROMPT)

    assert completion.text == "ok"
    assert groq.models_called == ["a"]


async def test_the_task_supplies_its_sampling_defaults(settings: Settings) -> None:
    """A caller asks for a *task* and never has to remember the numbers."""
    groq = FakeClient("groq")
    router = build_router(settings, {"groq": groq}, [("groq", "a")])

    await router.complete(TaskProfile.TITLE, PROMPT)

    _, max_tokens, temperature = groq.calls[0]
    assert max_tokens == TASK_DEFAULTS[TaskProfile.TITLE].max_tokens
    assert temperature == TASK_DEFAULTS[TaskProfile.TITLE].temperature


async def test_an_explicit_override_beats_the_task_default(settings: Settings) -> None:
    groq = FakeClient("groq")
    router = build_router(settings, {"groq": groq}, [("groq", "a")])

    await router.complete(TaskProfile.CHAT, PROMPT, max_tokens=7, temperature=0.0)

    assert groq.calls[0] == ("a", 7, 0.0)


# --- The Definition of Done ---------------------------------------------


async def test_the_third_rung_serves_when_the_first_two_fail(settings: Settings) -> None:
    """Phase 5's Definition of Done, across a provider boundary.

    Rung 1 and rung 2 belong to one provider and fail differently; rung 3
    belongs to another. The request still succeeds, and the answer is stamped
    with the rung that actually served it.
    """
    groq = FakeClient("groq")
    groq.program("a", LlmInvalidRequest("no such model"))
    groq.program("b", LlmUnavailable("503"))
    ollama = FakeClient("ollama")
    ollama.program("c", "answered by the third rung")

    router = build_router(
        settings.model_copy(update={"llm_retries_per_model": 1}),
        {"groq": groq, "ollama": ollama},
        [("groq", "a"), ("groq", "b"), ("ollama", "c")],
    )

    completion = await router.complete(TaskProfile.CHAT, PROMPT)

    assert completion.text == "answered by the third rung"
    assert completion.provider == "ollama"
    assert groq.models_called == ["a", "b", "b"]
    assert ollama.models_called == ["c"]


async def test_the_third_rung_serves_within_one_provider_too(settings: Settings) -> None:
    """The same guarantee when the ladder never leaves the provider."""
    groq = FakeClient("groq")
    groq.program("a", LlmBadResponse("empty"))
    groq.program("b", LlmTimeout("slow"))
    groq.program("c", "third model, same provider")

    router = build_router(
        settings.model_copy(update={"llm_retries_per_model": 0}),
        {"groq": groq},
        [("groq", "a"), ("groq", "b"), ("groq", "c")],
    )

    completion = await router.complete(TaskProfile.CHAT, PROMPT)

    assert completion.text == "third model, same provider"
    assert groq.models_called == ["a", "b", "c"]


# --- Retry semantics -----------------------------------------------------


async def test_a_transient_failure_is_retried_at_the_same_rung(settings: Settings) -> None:
    groq = FakeClient("groq")
    groq.program("a", LlmUnavailable("502"), LlmUnavailable("502"), "recovered")
    sleeper = Sleeper()

    router = build_router(
        settings.model_copy(update={"llm_retries_per_model": 3}),
        {"groq": groq},
        [("groq", "a")],
        sleeper=sleeper,
    )

    completion = await router.complete(TaskProfile.CHAT, PROMPT)

    assert completion.text == "recovered"
    assert groq.models_called == ["a", "a", "a"]
    assert len(sleeper.delays) == 2


async def test_the_rung_is_tried_retries_plus_one_times(settings: Settings) -> None:
    """`LLM_RETRIES_PER_MODEL=3` means four calls, not three."""
    groq = FakeClient("groq")
    groq.program("a", LlmUnavailable("502"))

    router = build_router(
        settings.model_copy(update={"llm_retries_per_model": 3}),
        {"groq": groq},
        [("groq", "a")],
    )

    with pytest.raises(LlmError):
        await router.complete(TaskProfile.CHAT, PROMPT)

    assert len(groq.models_called) == 4


async def test_backoff_grows_exponentially_and_is_capped(settings: Settings) -> None:
    groq = FakeClient("groq")
    groq.program("a", LlmUnavailable("502"))
    sleeper = Sleeper()

    router = build_router(
        settings.model_copy(update={"llm_retries_per_model": 8, "llm_backoff_base_s": 1.0}),
        {"groq": groq},
        [("groq", "a")],
        sleeper=sleeper,
    )

    with pytest.raises(LlmError):
        await router.complete(TaskProfile.CHAT, PROMPT)

    assert sleeper.delays[:5] == [1.0, 2.0, 4.0, 8.0, 16.0]
    assert max(sleeper.delays) == MAX_SLEEP_S


async def test_a_model_level_failure_is_never_retried(settings: Settings) -> None:
    """A 400 will be a 400 again; retrying it only makes the user wait."""
    groq = FakeClient("groq")
    groq.program("a", LlmInvalidRequest("context too long"))
    groq.program("b", "next model")
    sleeper = Sleeper()

    router = build_router(
        settings.model_copy(update={"llm_retries_per_model": 5}),
        {"groq": groq},
        [("groq", "a"), ("groq", "b")],
        sleeper=sleeper,
    )

    completion = await router.complete(TaskProfile.CHAT, PROMPT)

    assert completion.text == "next model"
    assert groq.models_called == ["a", "b"]
    assert sleeper.delays == []


async def test_retry_after_is_honoured_over_the_routers_own_backoff(settings: Settings) -> None:
    """The provider is the only party that knows when its window rolls."""
    groq = FakeClient("groq")
    groq.program("a", LlmRateLimited("slow down", retry_after=4.0), "ok")
    sleeper = Sleeper()

    router = build_router(
        settings.model_copy(update={"llm_retries_per_model": 2, "llm_backoff_base_s": 1.0}),
        {"groq": groq},
        [("groq", "a")],
        sleeper=sleeper,
    )

    await router.complete(TaskProfile.CHAT, PROMPT)

    assert sleeper.delays == [4.0]


async def test_a_retry_after_longer_than_a_user_will_wait_advances_the_rung(
    settings: Settings,
) -> None:
    groq = FakeClient("groq")
    groq.program("a", LlmRateLimited("come back tomorrow", retry_after=3600.0))
    groq.program("b", "second model")
    sleeper = Sleeper()

    router = build_router(
        settings.model_copy(update={"llm_retries_per_model": 3}),
        {"groq": groq},
        [("groq", "a"), ("groq", "b")],
        sleeper=sleeper,
    )

    completion = await router.complete(TaskProfile.CHAT, PROMPT)

    assert completion.text == "second model"
    assert groq.models_called == ["a", "b"]
    assert sleeper.delays == []


# --- Provider-level failure and the breaker ------------------------------


async def test_a_bad_api_key_skips_every_remaining_model_of_that_provider(
    settings: Settings,
) -> None:
    """The whole point of the provider/model split: one 401, not nine."""
    groq = FakeClient("groq")
    for model in ("a", "b", "c"):
        groq.program(model, LlmAuthError("invalid key"))
    ollama = FakeClient("ollama")
    ollama.program("d", "served")

    router = build_router(
        settings.model_copy(update={"llm_retries_per_model": 3}),
        {"groq": groq, "ollama": ollama},
        [("groq", "a"), ("groq", "b"), ("groq", "c"), ("ollama", "d")],
    )

    completion = await router.complete(TaskProfile.CHAT, PROMPT)

    assert completion.text == "served"
    assert groq.models_called == ["a"]


async def test_a_provider_level_failure_opens_its_circuit_immediately(
    settings: Settings,
) -> None:
    breaker = CircuitBreaker(failure_threshold=5, reset_s=60.0)
    groq = FakeClient("groq")
    groq.program("a", LlmAuthError("invalid key"))
    ollama = FakeClient("ollama")

    router = build_router(
        settings.model_copy(update={"llm_retries_per_model": 0}),
        {"groq": groq, "ollama": ollama},
        [("groq", "a"), ("ollama", "b")],
        breaker=breaker,
    )

    await router.complete(TaskProfile.CHAT, PROMPT)

    assert breaker.state("groq") is CircuitState.OPEN


async def test_an_open_circuit_skips_the_provider_without_calling_it(
    settings: Settings,
) -> None:
    breaker = CircuitBreaker(failure_threshold=1, reset_s=60.0)
    breaker.record_failure("groq", immediate=True)
    groq = FakeClient("groq")
    ollama = FakeClient("ollama")
    ollama.program("b", "served by the fallback")

    router = build_router(
        settings,
        {"groq": groq, "ollama": ollama},
        [("groq", "a"), ("ollama", "b")],
        breaker=breaker,
    )

    completion = await router.complete(TaskProfile.CHAT, PROMPT)

    assert completion.text == "served by the fallback"
    assert groq.calls == []


async def test_a_working_call_closes_a_circuit_that_had_been_failing(
    settings: Settings,
) -> None:
    breaker = CircuitBreaker(failure_threshold=3, reset_s=60.0)
    breaker.record_failure("groq")
    breaker.record_failure("groq")
    groq = FakeClient("groq")

    router = build_router(settings, {"groq": groq}, [("groq", "a")], breaker=breaker)
    await router.complete(TaskProfile.CHAT, PROMPT)

    assert breaker.status("groq").failures == 0


async def test_a_bad_response_does_not_count_against_the_provider(
    settings: Settings,
) -> None:
    """The provider is answering; an empty completion is the model's fault."""
    breaker = CircuitBreaker(failure_threshold=1, reset_s=60.0)
    groq = FakeClient("groq")
    groq.program("a", LlmInvalidRequest("refused"))
    groq.program("b", "ok")

    router = build_router(
        settings.model_copy(update={"llm_retries_per_model": 0}),
        {"groq": groq},
        [("groq", "a"), ("groq", "b")],
        breaker=breaker,
    )
    await router.complete(TaskProfile.CHAT, PROMPT)

    assert breaker.state("groq") is CircuitState.CLOSED


# --- Exhaustion ----------------------------------------------------------


async def test_an_empty_ladder_names_the_variable_to_fix(settings: Settings) -> None:
    router = build_router(settings, {}, [], skipped=("groq: no API key configured",))

    with pytest.raises(LlmNoProvider) as caught:
        await router.complete(TaskProfile.CHAT, PROMPT)

    assert "LLM_CHAIN_CHAT" in str(caught.value)
    assert "no API key configured" in str(caught.value)


async def test_exhausting_the_ladder_says_how_many_rungs_were_tried(
    settings: Settings,
) -> None:
    groq = FakeClient("groq")
    groq.program("a", LlmUnavailable("502"))
    groq.program("b", LlmTimeout("no answer from b"))

    router = build_router(
        settings.model_copy(update={"llm_retries_per_model": 0}),
        {"groq": groq},
        [("groq", "a"), ("groq", "b")],
    )

    with pytest.raises(LlmError) as caught:
        await router.complete(TaskProfile.CHAT, PROMPT)

    assert "2 tried" in str(caught.value)
    assert "no answer from b" in str(caught.value)


async def test_a_rung_whose_client_was_never_built_is_stepped_over(
    settings: Settings,
) -> None:
    """Cloudflare without an account id, say: configured but unbuildable."""
    ollama = FakeClient("ollama")
    ollama.program("b", "served")

    router = build_router(settings, {"ollama": ollama}, [("cloudflare", "a"), ("ollama", "b")])

    assert (await router.complete(TaskProfile.CHAT, PROMPT)).text == "served"


# --- The daily cap -------------------------------------------------------


async def test_the_cap_counts_requests_not_attempts(settings: Settings) -> None:
    """A four-rung failover is one call against the user's budget."""
    cap = DailyCallCap(2)
    groq = FakeClient("groq")
    groq.program("a", LlmUnavailable("502"))
    groq.program("b", LlmUnavailable("502"))
    groq.program("c", "ok")

    router = build_router(
        settings.model_copy(update={"llm_retries_per_model": 1}),
        {"groq": groq},
        [("groq", "a"), ("groq", "b"), ("groq", "c")],
        cap=cap,
    )

    await router.complete(TaskProfile.CHAT, PROMPT, user_id=111)

    assert cap.used(111) == 1


async def test_the_cap_refuses_once_it_is_spent(settings: Settings) -> None:
    cap = DailyCallCap(1)
    groq = FakeClient("groq")
    router = build_router(settings, {"groq": groq}, [("groq", "a")], cap=cap)

    await router.complete(TaskProfile.CHAT, PROMPT, user_id=111)
    with pytest.raises(LlmQuotaExceeded):
        await router.complete(TaskProfile.CHAT, PROMPT, user_id=111)

    assert len(groq.calls) == 1


async def test_a_request_that_never_reaches_a_provider_costs_the_user_nothing(
    settings: Settings,
) -> None:
    cap = DailyCallCap(5)
    router = build_router(settings, {}, [], cap=cap)

    with pytest.raises(LlmNoProvider):
        await router.complete(TaskProfile.CHAT, PROMPT, user_id=111)

    assert cap.used(111) == 0


# --- Accounting and reporting -------------------------------------------


async def test_every_attempt_reaches_the_ledger_with_its_outcome(
    settings: Settings,
) -> None:
    groq = FakeClient("groq")
    groq.program("a", LlmRateLimited("429"))
    groq.program("b", "ok")

    router = build_router(
        settings.model_copy(update={"llm_retries_per_model": 0}),
        {"groq": groq},
        [("groq", "a"), ("groq", "b")],
    )
    await router.complete(TaskProfile.CHAT, PROMPT)

    assert router.ledger.outcomes() == {"rate_limited": 1, "ok": 1}
    assert router.ledger.requests == 1
    assert router.ledger.successful_requests == 1


async def test_status_reports_the_ladder_the_circuits_and_the_usage(
    settings: Settings,
) -> None:
    breaker = CircuitBreaker(failure_threshold=1, reset_s=60.0)
    groq = FakeClient("groq")
    groq.program("a", LlmAuthError("invalid key"))
    ollama = FakeClient("ollama")

    router = build_router(
        settings.model_copy(update={"llm_retries_per_model": 0}),
        {"groq": groq, "ollama": ollama},
        [("groq", "a"), ("ollama", "b")],
        breaker=breaker,
    )
    await router.complete(TaskProfile.CHAT, PROMPT)

    status = router.status()
    assert status.any_open is True
    assert {circuit.provider for circuit in status.circuits} == {"groq", "ollama"}
    assert status.requests == 1
    assert status.successful_requests == 1
    assert {usage.provider for usage in status.usage} == {"groq", "ollama"}


def test_a_ladder_reports_its_distinct_providers_in_order(settings: Settings) -> None:
    router = build_router(
        settings, {}, [("groq", "a"), ("groq", "b"), ("ollama", "c"), ("groq", "d")]
    )

    assert router.ladder(TaskProfile.CHAT).providers == ("groq", "ollama")


def test_availability_is_per_task(settings: Settings) -> None:
    """A configuration with chat but no vision is normal, not broken."""
    router = build_router(settings, {"groq": FakeClient("groq")}, [("groq", "a")])

    assert router.available(TaskProfile.CHAT) is True
    assert router.available(TaskProfile.VISION) is False


async def test_closing_the_router_closes_every_client_even_if_one_raises(
    settings: Settings,
) -> None:
    class Stubborn(FakeClient):
        async def aclose(self) -> None:
            raise RuntimeError("will not close")

    groq = Stubborn("groq")
    ollama = FakeClient("ollama")
    router = build_router(settings, {"groq": groq, "ollama": ollama}, [("ollama", "a")])

    await router.aclose()

    assert ollama.closed is True
