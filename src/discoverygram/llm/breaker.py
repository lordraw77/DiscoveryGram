"""Per-provider circuit breaker.

The unit is the **provider**, not the (provider, model) pair, because the
failures worth short-circuiting are provider-wide: a revoked key, an
unreachable host, a sustained outage. When one of those is happening, every
remaining rung of that provider is going to fail the same way, and walking
them costs a Telegram user `models x retries` timeouts before the next
provider is even tried.

Three states, the classic ones:

* **closed** — calls go through, failures are counted;
* **open** — every call is refused immediately, for `reset_s`;
* **half-open** — exactly *one* probe is admitted. It closes the circuit if it
  works and re-opens it for a full cool-down if it does not.

The single-probe rule is the part worth being careful about: without it, a
burst of requests arriving the instant the cool-down expires all become probes
and hammer a provider that is still down.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from discoverygram.util.logging import get_logger

log = get_logger(__name__)


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half-open"


@dataclass(frozen=True, slots=True)
class CircuitStatus:
    """A snapshot fit for `/status`."""

    provider: str
    state: CircuitState
    failures: int
    opens_remaining_s: float = 0.0
    last_error: str = ""

    @property
    def healthy(self) -> bool:
        return self.state is CircuitState.CLOSED


@dataclass
class _Circuit:
    failures: int = 0
    opened_at: float | None = None
    probe_in_flight: bool = False
    last_error: str = ""
    # Repeated failure lengthens nothing: the cool-down is fixed, so a
    # provider that recovers is found again promptly.
    consecutive_opens: int = field(default=0)


class CircuitBreaker:
    """Failure tracking for every provider the router may call."""

    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        reset_s: float = 120.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be at least 1")
        self._threshold = failure_threshold
        self._reset_s = reset_s
        self._clock = clock
        self._circuits: dict[str, _Circuit] = {}

    # --- Gate ------------------------------------------------------------

    def allows(self, provider: str) -> bool:
        """Whether a call to this provider may be attempted now.

        Has a side effect by design: admitting the half-open probe marks it in
        flight, so the second caller through this method is refused.
        """
        circuit = self._circuits.get(provider)
        if circuit is None or circuit.opened_at is None:
            return True

        if self._clock() - circuit.opened_at < self._reset_s:
            return False

        if circuit.probe_in_flight:
            return False

        circuit.probe_in_flight = True
        log.info("llm_circuit_half_open", provider=provider)
        return True

    def state(self, provider: str) -> CircuitState:
        circuit = self._circuits.get(provider)
        if circuit is None or circuit.opened_at is None:
            return CircuitState.CLOSED
        if circuit.probe_in_flight:
            return CircuitState.HALF_OPEN
        if self._clock() - circuit.opened_at >= self._reset_s:
            return CircuitState.HALF_OPEN
        return CircuitState.OPEN

    # --- Outcomes --------------------------------------------------------

    def record_success(self, provider: str) -> None:
        """A working call closes the circuit and forgets the failure count."""
        circuit = self._circuits.get(provider)
        if circuit is None:
            return
        was_open = circuit.opened_at is not None
        circuit.failures = 0
        circuit.opened_at = None
        circuit.probe_in_flight = False
        circuit.last_error = ""
        if was_open:
            circuit.consecutive_opens = 0
            log.info("llm_circuit_closed", provider=provider)

    def record_failure(self, provider: str, *, reason: str = "", immediate: bool = False) -> None:
        """Count a failure, and open the circuit when it has earned it.

        `immediate=True` opens it on this one failure, for the errors that are
        already conclusive — a rejected API key does not need four more
        confirmations.
        """
        circuit = self._circuits.setdefault(provider, _Circuit())
        circuit.failures += 1
        circuit.last_error = reason

        # A failed half-open probe re-opens for a fresh full cool-down.
        if circuit.probe_in_flight:
            circuit.probe_in_flight = False
            circuit.opened_at = self._clock()
            circuit.consecutive_opens += 1
            log.warning("llm_circuit_reopened", provider=provider, reason=reason)
            return

        if circuit.opened_at is not None:
            return

        if immediate or circuit.failures >= self._threshold:
            circuit.opened_at = self._clock()
            circuit.consecutive_opens += 1
            log.warning(
                "llm_circuit_opened",
                provider=provider,
                failures=circuit.failures,
                reason=reason,
                cool_down_s=self._reset_s,
                immediate=immediate,
            )

    # --- Reporting -------------------------------------------------------

    def status(self, provider: str) -> CircuitStatus:
        circuit = self._circuits.get(provider)
        if circuit is None:
            return CircuitStatus(provider=provider, state=CircuitState.CLOSED, failures=0)
        remaining = 0.0
        if circuit.opened_at is not None:
            remaining = max(0.0, self._reset_s - (self._clock() - circuit.opened_at))
        return CircuitStatus(
            provider=provider,
            state=self.state(provider),
            failures=circuit.failures,
            opens_remaining_s=remaining,
            last_error=circuit.last_error,
        )

    def snapshot(self, providers: list[str]) -> list[CircuitStatus]:
        """Status for each named provider, in the order given."""
        return [self.status(provider) for provider in providers]

    def reset(self) -> None:
        """Forget everything. For tests and for an operator-triggered re-probe."""
        self._circuits.clear()


__all__ = ["CircuitBreaker", "CircuitState", "CircuitStatus"]
