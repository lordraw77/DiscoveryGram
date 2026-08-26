"""Typed errors every LLM adapter raises.

The router does not read HTTP status codes: adapters translate them into these,
and the router routes on two questions only —

* **is another attempt at this same (provider, model) pair worth making?**
  (`retryable`), and
* **is this provider itself broken, rather than this one model?**
  (`provider_level`).

Getting that split right is what stops a bad API key from burning nine retries
across three models that were never going to answer.
"""

from __future__ import annotations


class LlmError(Exception):
    """Base class for every LLM failure."""

    #: Another attempt at the *same* (provider, model) pair may succeed.
    retryable = False
    #: The provider as a whole is unusable, not merely this model.
    provider_level = False

    def __init__(
        self,
        message: str,
        *,
        provider: str = "",
        model: str = "",
        status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.provider = provider
        self.model = model
        self.status = status

    def __str__(self) -> str:
        return self.message

    @property
    def where(self) -> str:
        """`provider/model`, for logs and for `/status`."""
        if self.provider and self.model:
            return f"{self.provider}/{self.model}"
        return self.provider or self.model or "unknown"


class LlmAuthError(LlmError):
    """Missing, wrong or revoked credentials (401/403).

    Provider-level and never retryable: the key will not become valid between
    two requests, and every other model of that provider uses the same key.
    """

    provider_level = True


class LlmRateLimited(LlmError):
    """The provider is rate-limiting us (429). `retry_after` is seconds when known.

    Retryable but *not* provider-level: quotas are usually per model, and the
    next model of the same provider often has budget left.
    """

    retryable = True

    def __init__(
        self,
        message: str,
        *,
        provider: str = "",
        model: str = "",
        status: int | None = 429,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message, provider=provider, model=model, status=status)
        self.retry_after = retry_after


class LlmTimeout(LlmError):
    """The provider did not answer within `LLM_REQUEST_TIMEOUT_S`."""

    retryable = True


class LlmUnavailable(LlmError):
    """Unreachable, connection reset, or 5xx.

    Retryable at the rung, and counted by the circuit breaker: a provider that
    keeps doing this crosses the threshold and is skipped wholesale.
    """

    retryable = True


class LlmBadResponse(LlmError):
    """A 200 whose body is not a completion — malformed JSON, no choices, empty text.

    Retryable once or twice, because sampling can produce an empty completion,
    but it never trips the breaker: the provider is answering.
    """

    retryable = True


class LlmInvalidRequest(LlmError):
    """The provider rejected the arguments (400), or does not know this model (404).

    Fatal for this rung and pointless to retry — but the next model of the same
    provider is still worth trying, so it is not provider-level.
    """


class LlmUnsupported(LlmError):
    """This adapter cannot serve the task at all — vision on a text-only provider."""


class LlmNoProvider(LlmError):
    """The ladder for this task is empty, or every rung was skipped.

    Distinct from a failure: nothing was even attempted, and the fix is
    configuration rather than a retry.
    """


class LlmQuotaExceeded(LlmError):
    """The caller has spent their `LLM_DAILY_CALL_LIMIT_PER_USER` for today."""

    def __init__(self, message: str, *, resets_in_s: float | None = None) -> None:
        super().__init__(message)
        self.resets_in_s = resets_in_s


__all__ = [
    "LlmAuthError",
    "LlmBadResponse",
    "LlmError",
    "LlmInvalidRequest",
    "LlmNoProvider",
    "LlmQuotaExceeded",
    "LlmRateLimited",
    "LlmTimeout",
    "LlmUnavailable",
    "LlmUnsupported",
]
