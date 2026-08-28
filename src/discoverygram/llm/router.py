"""The router: one request in, one answer out, whatever it takes.

This is where the ladder from `plan.py` is actually walked. For each rung, in
order:

1. **Is the provider's circuit closed?** An open circuit skips the rung with no
   call at all — and because a provider's rungs are contiguous, a provider that
   trips takes all of its remaining models with it.
2. **Try the rung**, up to `LLM_RETRIES_PER_MODEL + 1` times, sleeping between
   attempts with exponential backoff and full jitter, honouring `Retry-After`
   when the provider sent one.
3. **Classify the failure.** A provider-level error (bad key) opens the circuit
   immediately and abandons the provider. A model-level error (unknown model,
   refused prompt) abandons the rung with no retries at all. Anything else is
   retried at the rung and then, on exhaustion, advances one rung.

Three things happen before the first rung is even considered, and all three are
refusals that cost nothing:

* the caller's **daily cap** and **per-minute burst limit** are checked, and
  neither is consumed unless a provider is actually going to be called;
* if **every** provider on the ladder is short-circuited, the request is
  refused immediately with the cool-down, rather than walking rungs the breaker
  has already established are down. That is the back-pressure: a degraded
  provider must not turn into a queue of Telegram users waiting on timeouts.

In flight, `LLM_MAX_CONCURRENT_REQUESTS` bounds how many provider calls the
process makes at once. Sixteen concurrent captures times five pipeline steps is
not a burst any free-tier provider enjoys, and queueing is cheaper than being
rate-limited.

Two properties this ordering is designed to give:

* a Telegram user waits through *retries* only for failures that plausibly go
  away by themselves, never for a wrong API key;
* one bad provider cannot consume the whole ladder, because tripping its
  circuit skips its remaining models in a single step.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass

from discoverygram.config import Settings
from discoverygram.llm.breaker import CircuitBreaker, CircuitStatus
from discoverygram.llm.plan import TASK_DEFAULTS, Attempt, TaskProfile
from discoverygram.llm.usage import DailyCallCap, ProviderUsage, UsageLedger, UserRateLimiter
from discoverygram.ports.llm import Completion, LlmClient, Message
from discoverygram.ports.llm_errors import (
    LlmDegraded,
    LlmError,
    LlmNoProvider,
    LlmRateLimited,
)
from discoverygram.util import metrics
from discoverygram.util.logging import get_logger

log = get_logger(__name__)

# A rung that asks us to wait longer than this is abandoned rather than slept
# through: a Telegram user is waiting, and the next rung is usually faster than
# the wait.
MAX_SLEEP_S = 30.0


@dataclass(frozen=True, slots=True)
class TaskLadder:
    """The ordered rungs for one task, plus why anything was left out."""

    task: TaskProfile
    attempts: tuple[Attempt, ...] = ()
    skipped: tuple[str, ...] = ()

    @property
    def usable(self) -> bool:
        return bool(self.attempts)

    @property
    def providers(self) -> tuple[str, ...]:
        """Distinct providers, in ladder order."""
        seen: list[str] = []
        for attempt in self.attempts:
            if attempt.provider not in seen:
                seen.append(attempt.provider)
        return tuple(seen)


@dataclass(frozen=True, slots=True)
class RouterStatus:
    """Everything `/status` needs to report the LLM layer honestly."""

    ladders: tuple[TaskLadder, ...] = ()
    circuits: tuple[CircuitStatus, ...] = ()
    usage: tuple[ProviderUsage, ...] = ()
    requests: int = 0
    successful_requests: int = 0
    attempts: int = 0
    daily_limit: int = 0

    @property
    def any_open(self) -> bool:
        return any(not circuit.healthy for circuit in self.circuits)


@dataclass
class _RungOutcome:
    """Why a rung was abandoned, so the final error can say something useful."""

    error: LlmError
    attempts: int = 1


class LlmRouter:
    """Retry, failover and accounting over a set of `LlmClient`s."""

    def __init__(
        self,
        settings: Settings,
        clients: Mapping[str, LlmClient],
        ladders: Mapping[TaskProfile, TaskLadder],
        *,
        breaker: CircuitBreaker | None = None,
        ledger: UsageLedger | None = None,
        cap: DailyCallCap | None = None,
        rate: UserRateLimiter | None = None,
        concurrency: asyncio.Semaphore | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[float], float] | None = None,
    ) -> None:
        self._settings = settings
        self._clients = dict(clients)
        self._ladders = dict(ladders)
        self._breaker = breaker or CircuitBreaker(
            failure_threshold=settings.llm_circuit_failure_threshold,
            reset_s=settings.llm_circuit_reset_s,
        )
        self._ledger = ledger or UsageLedger()
        self._cap = cap or DailyCallCap(settings.llm_daily_call_limit_per_user)
        self._rate = rate or UserRateLimiter(settings.llm_user_rate_per_minute)
        # Created eagerly: a semaphore built on first use would be built inside
        # whichever task got there first, and 3.12 binds it to that loop.
        self._concurrency = concurrency or (
            asyncio.Semaphore(settings.llm_max_concurrent_requests)
            if settings.llm_max_concurrent_requests > 0
            else None
        )
        self._sleep = sleep
        # Full jitter by default; injectable so tests are not flaky.
        self._jitter = jitter or (lambda ceiling: random.uniform(0.0, ceiling))  # noqa: S311

    # --- Introspection ---------------------------------------------------

    @property
    def ledger(self) -> UsageLedger:
        return self._ledger

    @property
    def cap(self) -> DailyCallCap:
        return self._cap

    @property
    def rate(self) -> UserRateLimiter:
        return self._rate

    def ladder(self, task: TaskProfile) -> TaskLadder:
        return self._ladders.get(task, TaskLadder(task=task))

    def available(self, task: TaskProfile) -> bool:
        """Whether this task has anything to call at all."""
        return self.ladder(task).usable

    def _providers(self) -> list[str]:
        providers: list[str] = []
        for ladder in self._ladders.values():
            for provider in ladder.providers:
                if provider not in providers:
                    providers.append(provider)
        return providers

    def _publish_circuits(self) -> None:
        """Mirror the breaker into the gauge, so a tripped circuit is alertable.

        `/status` names a degraded provider to whoever typed the command; this
        is the same fact for whoever is not in the chat at 3am.
        """
        for circuit in self._breaker.snapshot(self._providers()):
            metrics.LLM_CIRCUIT_STATE.set(
                0.0 if circuit.healthy else 1.0,
                provider=circuit.provider,
                state=circuit.state.value,
            )

    def status(self) -> RouterStatus:
        providers = self._providers()
        self._publish_circuits()
        return RouterStatus(
            ladders=tuple(self._ladders.values()),
            circuits=tuple(self._breaker.snapshot(providers)),
            usage=tuple(self._ledger.per_provider()),
            requests=self._ledger.requests,
            successful_requests=self._ledger.successful_requests,
            attempts=self._ledger.attempts,
            daily_limit=self._cap.limit,
        )

    # --- The call --------------------------------------------------------

    async def complete(
        self,
        task: TaskProfile,
        messages: Sequence[Message],
        *,
        user_id: int | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> Completion:
        """Answer `messages`, walking the ladder until something works.

        Raises `LlmNoProvider` when nothing was configured, `LlmQuotaExceeded`
        when the caller is out of daily budget, `LlmThrottled` when they are
        asking too fast, `LlmDegraded` when every provider is short-circuited,
        and otherwise the error from the **last** rung tried — the most
        informative one, because earlier rungs were abandoned in favour of it.
        """
        ladder = self.ladder(task)
        if not ladder.usable:
            raise LlmNoProvider(self._nothing_configured(ladder))

        # Three refusals that cost nothing, checked before anything is
        # consumed: a request that never reaches a provider must not cost the
        # user a call, and must not be charged against their burst budget.
        self._check_limits(user_id)
        self._check_back_pressure(task, ladder)

        defaults = TASK_DEFAULTS[task]
        tokens = defaults.max_tokens if max_tokens is None else max_tokens
        warmth = defaults.temperature if temperature is None else temperature

        self._cap.consume(user_id)
        self._rate.consume(user_id)

        skipped_providers: set[str] = set()
        last: LlmError | None = None

        for position, attempt in enumerate(ladder.attempts, start=1):
            if attempt.provider in skipped_providers:
                continue

            client = self._clients.get(attempt.provider)
            if client is None:
                # Configured but never built — a missing account id, say.
                skipped_providers.add(attempt.provider)
                continue

            if not self._breaker.allows(attempt.provider):
                log.info(
                    "llm_rung_skipped",
                    task=task.value,
                    rung=str(attempt),
                    reason="circuit_open",
                )
                skipped_providers.add(attempt.provider)
                continue

            outcome = await self._try_rung(
                client,
                attempt,
                task=task,
                messages=messages,
                max_tokens=tokens,
                temperature=warmth,
                user_id=user_id,
            )
            if isinstance(outcome, Completion):
                self._breaker.record_success(attempt.provider)
                self._ledger.note_request(succeeded=True)
                metrics.LLM_REQUESTS.inc(task=task.value, outcome="ok")
                if position > 1:
                    metrics.LLM_FAILOVERS.inc(task=task.value, provider=attempt.provider)
                self._publish_circuits()
                log.info(
                    "llm_request_served",
                    task=task.value,
                    rung=str(attempt),
                    position=position,
                    latency_s=round(outcome.latency_s, 3),
                )
                return outcome

            last = outcome.error
            if outcome.error.provider_level:
                self._breaker.record_failure(
                    attempt.provider, reason=type(outcome.error).__name__, immediate=True
                )
                skipped_providers.add(attempt.provider)
                log.warning(
                    "llm_provider_abandoned",
                    task=task.value,
                    provider=attempt.provider,
                    error=str(outcome.error),
                )
            elif outcome.error.retryable:
                # Retryable and still failing after every retry: the rung is
                # spent, and the provider has earned a mark against it.
                self._breaker.record_failure(attempt.provider, reason=type(outcome.error).__name__)

        self._ledger.note_request(succeeded=False)
        metrics.LLM_REQUESTS.inc(task=task.value, outcome="failed")
        self._publish_circuits()
        raise self._exhausted(task, ladder, last)

    # --- Refusals that cost nothing --------------------------------------

    def _check_limits(self, user_id: int | None) -> None:
        """The two per-user limits, in the order that gives the better message.

        The daily cap first: a user who is out of budget for the day should be
        told that, not asked to wait a minute for an allowance that is already
        spent.
        """
        try:
            self._cap.check(user_id)
        except LlmError:
            metrics.LLM_THROTTLED.inc(limit="daily")
            raise
        try:
            self._rate.check(user_id)
        except LlmError:
            metrics.LLM_THROTTLED.inc(limit="per_minute")
            raise

    def _check_back_pressure(self, task: TaskProfile, ladder: TaskLadder) -> None:
        """Refuse immediately when every provider on the ladder is open.

        Walking the ladder here would call nothing — every rung is skipped —
        and end in "every model failed", which reads like the request was
        tried. Saying *cooling down, N seconds* instead is both true and
        actionable, and it is what stops a provider outage from becoming a
        queue of users waiting on a bot that looks stuck.
        """
        providers = [provider for provider in ladder.providers if provider in self._clients]
        # `blocks`, not `allows`: asking `allows` would consume the half-open
        # probe of a provider that is due for one, and a look must never be a
        # call.
        if not providers or not all(self._breaker.blocks(provider) for provider in providers):
            return

        circuits = self._breaker.snapshot(providers)
        remaining = min((circuit.opens_remaining_s for circuit in circuits), default=0.0)
        self._publish_circuits()
        metrics.LLM_THROTTLED.inc(limit="degraded")
        metrics.LLM_REQUESTS.inc(task=task.value, outcome="degraded")
        log.warning(
            "llm_back_pressure",
            task=task.value,
            providers=providers,
            retry_in_s=round(remaining, 1),
        )
        raise LlmDegraded(
            "Every AI provider is cooling down after repeated failures. "
            f"Try again in {max(1, int(remaining + 0.999))}s — /status has the details.",
            providers=tuple(providers),
            retry_after=remaining,
        )

    # --- One rung --------------------------------------------------------

    async def _try_rung(
        self,
        client: LlmClient,
        attempt: Attempt,
        *,
        task: TaskProfile,
        messages: Sequence[Message],
        max_tokens: int,
        temperature: float,
        user_id: int | None,
    ) -> Completion | _RungOutcome:
        """Call one (provider, model) pair, retrying only what is worth retrying."""
        tries = self._settings.llm_retries_per_model + 1
        last: LlmError | None = None

        for try_number in range(1, tries + 1):
            started = time.monotonic()
            try:
                completion = await self._call(
                    client,
                    model=attempt.model,
                    messages=messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
            except LlmError as error:
                elapsed = time.monotonic() - started
                last = error
                metrics.LLM_ATTEMPTS.inc(provider=attempt.provider, outcome=_outcome_name(error))
                metrics.LLM_LATENCY.observe(elapsed, provider=attempt.provider)
                self._ledger.record_failure(
                    provider=attempt.provider,
                    model=attempt.model,
                    task=task,
                    outcome=_outcome_name(error),
                    latency_s=elapsed,
                    user_id=user_id,
                )

                if not error.retryable or error.provider_level:
                    # Nothing a repeat would change.
                    return _RungOutcome(error=error, attempts=try_number)

                if try_number >= tries:
                    return _RungOutcome(error=error, attempts=try_number)

                delay = self._delay(try_number, error)
                if delay is None:
                    # The provider asked for longer than we are willing to
                    # wait; the next rung is the faster answer.
                    return _RungOutcome(error=error, attempts=try_number)

                log.warning(
                    "llm_retry",
                    task=task.value,
                    rung=str(attempt),
                    attempt=try_number,
                    of=tries,
                    reason=type(error).__name__,
                    delay_s=round(delay, 2),
                )
                await self._sleep(delay)
                continue

            self._ledger.record_completion(
                provider=attempt.provider,
                model=attempt.model,
                task=task,
                usage=completion.usage,
                latency_s=completion.latency_s,
                user_id=user_id,
            )
            metrics.LLM_ATTEMPTS.inc(provider=attempt.provider, outcome="ok")
            metrics.LLM_LATENCY.observe(completion.latency_s, provider=attempt.provider)
            metrics.TOKENS.inc(
                completion.usage.prompt_tokens or 0, provider=attempt.provider, kind="prompt"
            )
            metrics.TOKENS.inc(
                completion.usage.completion_tokens or 0,
                provider=attempt.provider,
                kind="completion",
            )
            return completion

        # Unreachable: the loop returns on both the success and failure paths.
        assert last is not None
        return _RungOutcome(error=last, attempts=tries)

    async def _call(
        self,
        client: LlmClient,
        *,
        model: str,
        messages: Sequence[Message],
        max_tokens: int,
        temperature: float,
    ) -> Completion:
        """One provider call, inside the process-wide concurrency bound."""
        if self._concurrency is None:
            return await client.complete(
                model=model, messages=messages, max_tokens=max_tokens, temperature=temperature
            )
        async with self._concurrency:
            return await client.complete(
                model=model, messages=messages, max_tokens=max_tokens, temperature=temperature
            )

    def _delay(self, try_number: int, error: LlmError) -> float | None:
        """How long to wait before repeating this rung, or `None` for "don't".

        `Retry-After` wins when the provider sent one — it is the only party
        that knows when the quota window rolls — but a value beyond `MAX_SLEEP_S`
        means the ladder is the better move.
        """
        if isinstance(error, LlmRateLimited) and error.retry_after is not None:
            if error.retry_after > MAX_SLEEP_S:
                return None
            return error.retry_after

        ceiling = min(
            self._settings.llm_backoff_base_s * (2 ** (try_number - 1)),
            MAX_SLEEP_S,
        )
        return self._jitter(ceiling)

    # --- Failure messages ------------------------------------------------

    def _nothing_configured(self, ladder: TaskLadder) -> str:
        chain = "LLM_CHAIN_VISION" if ladder.task.requires_vision else "LLM_CHAIN_CHAT"
        message = f"No {ladder.task.value} model is configured. Check {chain} in .env."
        if ladder.skipped:
            message += " Skipped: " + "; ".join(ladder.skipped) + "."
        return message

    def _exhausted(self, task: TaskProfile, ladder: TaskLadder, last: LlmError | None) -> LlmError:
        """The error a user sees when every rung failed.

        The last rung's message is kept: it is the most recent evidence, and
        naming how many were tried is what tells an operator this was failover
        exhaustion rather than a single provider having a bad second.
        """
        rungs = len(ladder.attempts)
        detail = f" Last error: {last}" if last else ""
        log.error(
            "llm_ladder_exhausted",
            task=task.value,
            rungs=rungs,
            last_error=str(last) if last else "",
        )
        error = LlmError(
            f"Every configured {task.value} model failed ({rungs} tried).{detail}",
            provider=last.provider if last else "",
            model=last.model if last else "",
        )
        return error

    async def aclose(self) -> None:
        """Close every client once, even when one of them raises."""
        for client in self._clients.values():
            try:
                await client.aclose()
            except Exception as exc:  # shutdown must not be derailed by one client
                log.warning("llm_client_close_failed", provider=client.name, error=str(exc))


def _outcome_name(error: LlmError) -> str:
    """A short, stable label for the ledger: `rate_limited`, `timeout`, ..."""
    name = type(error).__name__.removeprefix("Llm")
    return "".join(
        f"_{char.lower()}" if char.isupper() and index else char.lower()
        for index, char in enumerate(name)
    )


__all__ = [
    "MAX_SLEEP_S",
    "LlmRouter",
    "RouterStatus",
    "TaskLadder",
]
