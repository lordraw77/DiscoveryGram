"""The `LlmClient` port.

Everything the application layer is allowed to know about a language model.
One adapter covers every OpenAI-compatible provider; three providers speak
their own dialect and get their own.

The port is deliberately narrow — one call, `complete` — because the router
above it is where the interesting behaviour lives (retry, failover, breaker,
accounting) and an adapter that also retried would multiply the ladder.
"""

from __future__ import annotations

import base64
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from types import TracebackType
from typing import Literal

Role = Literal["system", "user", "assistant"]

# What Telegram can hand us and every vision provider accepts.
SUPPORTED_IMAGE_TYPES = frozenset({"image/jpeg", "image/png", "image/webp", "image/gif"})


@dataclass(frozen=True, slots=True)
class ImagePart:
    """One image attached to a message, carried as bytes rather than a URL.

    Telegram's file URLs contain the bot token and expire, so handing one to a
    third-party provider would both leak the secret and often fail. The bytes
    are downloaded once and inlined.
    """

    data: bytes
    mime_type: str = "image/jpeg"

    def as_data_url(self) -> str:
        encoded = base64.b64encode(self.data).decode("ascii")
        return f"data:{self.mime_type};base64,{encoded}"

    def as_base64(self) -> str:
        return base64.b64encode(self.data).decode("ascii")


@dataclass(frozen=True, slots=True)
class Message:
    """One turn of a conversation, with optional images."""

    role: Role
    text: str
    images: tuple[ImagePart, ...] = field(default_factory=tuple)

    @property
    def has_images(self) -> bool:
        return bool(self.images)


@dataclass(frozen=True, slots=True)
class Usage:
    """Token counts, when the provider reports them.

    `None` rather than `0` for "not reported": zero is a real answer and the
    two must not be added together in the ledger.
    """

    prompt_tokens: int | None = None
    completion_tokens: int | None = None

    @property
    def total_tokens(self) -> int | None:
        if self.prompt_tokens is None and self.completion_tokens is None:
            return None
        return (self.prompt_tokens or 0) + (self.completion_tokens or 0)


@dataclass(frozen=True, slots=True)
class Completion:
    """What a successful call returns, stamped with the rung that served it."""

    text: str
    provider: str
    model: str
    usage: Usage = field(default_factory=Usage)
    latency_s: float = 0.0
    finish_reason: str = ""

    @property
    def truncated(self) -> bool:
        """The model stopped because it ran out of room, not because it was done."""
        return self.finish_reason in {"length", "max_tokens", "MAX_TOKENS"}


class LlmClient(ABC):
    """Async interface to one provider.

    Implementations do **not** retry, do not fail over and do not sleep on a
    429: they translate one request into one provider call and one typed
    result. Everything else is the router's job.
    """

    #: Provider name as it appears in `LLM_CHAIN_*` and `<P>_API_KEY`.
    name: str = ""

    @abstractmethod
    async def complete(
        self,
        *,
        model: str,
        messages: Sequence[Message],
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> Completion:
        """One completion, or a typed `LlmError`."""

    @abstractmethod
    def supports_vision(self) -> bool:
        """Whether this adapter can send images at all.

        Checked when the ladder is built, so a text-only provider is skipped
        with a reason instead of failing on the first photo.
        """

    @abstractmethod
    async def aclose(self) -> None:
        """Release transport resources. Safe to call twice."""

    async def __aenter__(self) -> LlmClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()


__all__ = [
    "SUPPORTED_IMAGE_TYPES",
    "Completion",
    "ImagePart",
    "LlmClient",
    "Message",
    "Role",
    "Usage",
]
