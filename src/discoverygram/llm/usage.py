"""Usage accounting and the per-user limits.

Three jobs that share one clock:

* **the ledger** — every attempt, successful or not, is recorded with its
  provider, model, task, latency, tokens and outcome. `/status` reads the
  aggregate; the log carries the individual records.
* **the daily cap** — `LLM_DAILY_CALL_LIMIT_PER_USER` calls per user per UTC
  day, which bounds *spend*;
* **the burst limit** — `LLM_USER_RATE_PER_MINUTE` calls per user per rolling
  minute, which bounds *rate*. The two answer different questions: a daily cap
  alone lets one user empty their allowance in ten seconds and hold the
  provider connection pool while doing it.

The cap counts *requests*, not attempts: one `/summarize` that fails over
across four rungs is one call against the user's budget. Charging per attempt
would punish a user for a provider outage they did not cause.

Both are in-process and reset on restart. That is the honest scope of a cost
*guard* rather than a billing system — a persistent counter is phase 7's
concern, when Redis is already on the request path.
"""

from __future__ import annotations

import time
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import dataclass

from discoverygram.llm.plan import TaskProfile
from discoverygram.ports.llm import Usage
from discoverygram.ports.llm_errors import LlmQuotaExceeded, LlmThrottled
from discoverygram.util.logging import get_logger

log = get_logger(__name__)

_DAY_S = 86_400.0


@dataclass(frozen=True, slots=True)
class UsageRecord:
    """One attempt against one rung."""

    provider: str
    model: str
    task: str
    outcome: str
    latency_s: float
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    user_id: int | None = None

    @property
    def succeeded(self) -> bool:
        return self.outcome == "ok"


@dataclass(frozen=True, slots=True)
class ProviderUsage:
    """What one provider has done since the process started."""

    provider: str
    calls: int = 0
    failures: int = 0
    tokens: int = 0
    total_latency_s: float = 0.0

    @property
    def average_latency_s(self) -> float:
        return self.total_latency_s / self.calls if self.calls else 0.0


@dataclass
class _Totals:
    calls: int = 0
    failures: int = 0
    tokens: int = 0
    latency_s: float = 0.0


class UsageLedger:
    """In-process accounting for every LLM attempt."""

    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._by_provider: dict[str, _Totals] = {}
        self._outcomes: Counter[str] = Counter()
        self._records: list[UsageRecord] = []
        self._requests = 0
        self._succeeded = 0

    def record(self, record: UsageRecord) -> None:
        totals = self._by_provider.setdefault(record.provider, _Totals())
        totals.calls += 1
        totals.latency_s += record.latency_s
        if not record.succeeded:
            totals.failures += 1
        totals.tokens += (record.prompt_tokens or 0) + (record.completion_tokens or 0)
        self._outcomes[record.outcome] += 1

        # Bounded: the ledger is a diagnostic, not an audit trail, and an
        # unbounded list in a long-running bot is a leak.
        self._records.append(record)
        if len(self._records) > 200:
            del self._records[:-200]

        log.info(
            "llm_attempt",
            provider=record.provider,
            model=record.model,
            task=record.task,
            outcome=record.outcome,
            latency_s=round(record.latency_s, 3),
            prompt_tokens=record.prompt_tokens,
            completion_tokens=record.completion_tokens,
        )

    def record_completion(
        self,
        *,
        provider: str,
        model: str,
        task: TaskProfile,
        usage: Usage,
        latency_s: float,
        user_id: int | None = None,
    ) -> None:
        self.record(
            UsageRecord(
                provider=provider,
                model=model,
                task=task.value,
                outcome="ok",
                latency_s=latency_s,
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                user_id=user_id,
            )
        )

    def record_failure(
        self,
        *,
        provider: str,
        model: str,
        task: TaskProfile,
        outcome: str,
        latency_s: float = 0.0,
        user_id: int | None = None,
    ) -> None:
        self.record(
            UsageRecord(
                provider=provider,
                model=model,
                task=task.value,
                outcome=outcome,
                latency_s=latency_s,
                user_id=user_id,
            )
        )

    def note_request(self, *, succeeded: bool) -> None:
        """One user-visible request, however many rungs it took."""
        self._requests += 1
        if succeeded:
            self._succeeded += 1

    # --- Reporting -------------------------------------------------------

    @property
    def requests(self) -> int:
        return self._requests

    @property
    def successful_requests(self) -> int:
        return self._succeeded

    @property
    def attempts(self) -> int:
        return sum(self._outcomes.values())

    def outcomes(self) -> dict[str, int]:
        return dict(self._outcomes)

    def per_provider(self) -> list[ProviderUsage]:
        """Busiest provider first — the one an operator is looking for."""
        usages = [
            ProviderUsage(
                provider=name,
                calls=totals.calls,
                failures=totals.failures,
                tokens=totals.tokens,
                total_latency_s=totals.latency_s,
            )
            for name, totals in self._by_provider.items()
        ]
        return sorted(usages, key=lambda usage: (-usage.calls, usage.provider))

    def recent(self, limit: int = 10) -> list[UsageRecord]:
        return self._records[-limit:]


@dataclass
class _UserDay:
    day: int
    calls: int = 0


class DailyCallCap:
    """`LLM_DAILY_CALL_LIMIT_PER_USER`, counted per UTC day.

    A limit of 0 disables the cap entirely, matching the documented meaning of
    the variable. The day boundary comes from wall-clock time rather than a
    rolling window, so a user's budget refills at a time they can predict.
    """

    def __init__(self, limit: int, *, clock: Callable[[], float] = time.time) -> None:
        self._limit = max(limit, 0)
        self._clock = clock
        self._users: dict[int, _UserDay] = {}

    @property
    def enabled(self) -> bool:
        return self._limit > 0

    @property
    def limit(self) -> int:
        return self._limit

    def _today(self) -> int:
        return int(self._clock() // _DAY_S)

    def used(self, user_id: int) -> int:
        entry = self._users.get(user_id)
        if entry is None or entry.day != self._today():
            return 0
        return entry.calls

    def remaining(self, user_id: int) -> int | None:
        """Calls left today, or `None` when the cap is disabled."""
        if not self.enabled:
            return None
        return max(0, self._limit - self.used(user_id))

    def check(self, user_id: int | None) -> None:
        """Raise `LlmQuotaExceeded` when this user is out of budget.

        Read-only: it does not consume. The router consumes only once it is
        actually going to call a provider.
        """
        if not self.enabled or user_id is None:
            return
        if self.used(user_id) >= self._limit:
            raise LlmQuotaExceeded(
                f"You have used your {self._limit} AI requests for today. "
                f"The allowance resets at midnight UTC.",
                resets_in_s=self._seconds_to_midnight(),
            )

    def consume(self, user_id: int | None) -> None:
        if not self.enabled or user_id is None:
            return
        today = self._today()
        entry = self._users.get(user_id)
        if entry is None or entry.day != today:
            entry = _UserDay(day=today)
            self._users[user_id] = entry
        entry.calls += 1

    def _seconds_to_midnight(self) -> float:
        now = self._clock()
        return _DAY_S - (now % _DAY_S)


class UserRateLimiter:
    """`LLM_USER_RATE_PER_MINUTE`, as a rolling window per user.

    Rolling rather than fixed: a fixed minute lets a user spend the whole
    allowance at 10:59:59 and the whole of the next one at 11:00:01, which is
    twice the burst the operator configured.

    A limit of 0 disables it. The default is deliberately loose, because one
    photo capture is *several* calls — vision, tidy, title, tags, summary — and
    a limit that refuses halfway through a capture would leave the user with a
    half-written draft and no way to finish it.
    """

    def __init__(
        self,
        limit_per_minute: int,
        *,
        window_s: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._limit = max(limit_per_minute, 0)
        self._window_s = window_s
        self._clock = clock
        self._calls: dict[int, deque[float]] = {}

    @property
    def enabled(self) -> bool:
        return self._limit > 0

    @property
    def limit(self) -> int:
        return self._limit

    def _window(self, user_id: int) -> deque[float]:
        calls = self._calls.setdefault(user_id, deque())
        cutoff = self._clock() - self._window_s
        while calls and calls[0] <= cutoff:
            calls.popleft()
        # Users who stopped asking must not stay in the dictionary forever.
        if not calls:
            del self._calls[user_id]
            return deque()
        return calls

    def used(self, user_id: int) -> int:
        return len(self._window(user_id))

    def check(self, user_id: int | None) -> None:
        """Raise `LlmThrottled` when this user is asking too fast. Read-only."""
        if not self.enabled or user_id is None:
            return
        calls = self._window(user_id)
        if len(calls) < self._limit:
            return
        retry_after = max(0.0, self._window_s - (self._clock() - calls[0]))
        raise LlmThrottled(
            f"You are sending AI requests faster than the {self._limit} per minute "
            f"this bot allows. Try again in {max(1, int(retry_after + 0.999))}s.",
            retry_after=retry_after,
        )

    def consume(self, user_id: int | None) -> None:
        if not self.enabled or user_id is None:
            return
        calls = self._window(user_id)
        calls.append(self._clock())
        self._calls[user_id] = calls


__all__ = [
    "DailyCallCap",
    "ProviderUsage",
    "UsageLedger",
    "UsageRecord",
    "UserRateLimiter",
]
