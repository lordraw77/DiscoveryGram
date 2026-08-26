"""The OpenAI-compatible adapter.

Six of the nine providers speak `POST {base}/chat/completions` with the same
request and response shapes: **nvidia, openrouter, groq, cerebras, mistral,
ollama**. They differ only in the base URL, in whether they accept images, and
in which corner of the error space they favour — so they share one class and
differ by a small table.

The adapter's whole job is one request in, one typed result out. It never
retries and never sleeps: the router owns the ladder, and an adapter that also
retried would silently multiply every configured retry count.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

from discoverygram.llm.plan import ProviderConfig
from discoverygram.ports.llm import Completion, LlmClient, Message, Usage
from discoverygram.ports.llm_errors import (
    LlmAuthError,
    LlmBadResponse,
    LlmError,
    LlmInvalidRequest,
    LlmRateLimited,
    LlmTimeout,
    LlmUnavailable,
    LlmUnsupported,
)
from discoverygram.util.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ProviderProfile:
    """What distinguishes one OpenAI-compatible provider from another."""

    base_url: str
    #: Can the provider accept image parts at all? A model still has to be a
    #: vision model, but a provider that cannot carry an image is skipped when
    #: the vision ladder is built rather than failing on the first photo.
    vision: bool = True
    #: Extra headers the provider requires beyond `Authorization`.
    headers: Mapping[str, str] = field(default_factory=dict)


# Defaults per provider. `<P>_BASE_URL` overrides the URL; nothing overrides
# the capability flag, which is a fact about the provider's API.
OPENAI_COMPATIBLE: dict[str, ProviderProfile] = {
    "nvidia": ProviderProfile(base_url="https://integrate.api.nvidia.com/v1"),
    "openrouter": ProviderProfile(
        base_url="https://openrouter.ai/api/v1",
        # OpenRouter attributes requests by these and rate-limits unattributed
        # traffic harder. They are not secrets.
        headers={
            "HTTP-Referer": "https://github.com/lordraw77/DiscoveryGram",
            "X-Title": "DiscoveryGram",
        },
    ),
    "groq": ProviderProfile(base_url="https://api.groq.com/openai/v1"),
    # Cerebras serves text-only models; it rejects image parts outright.
    "cerebras": ProviderProfile(base_url="https://api.cerebras.ai/v1", vision=False),
    "mistral": ProviderProfile(base_url="https://api.mistral.ai/v1"),
    "ollama": ProviderProfile(base_url="http://localhost:11434/v1"),
}


class OpenAiCompatibleClient(LlmClient):
    """One provider that speaks the OpenAI chat-completions dialect."""

    def __init__(
        self,
        config: ProviderConfig,
        *,
        profile: ProviderProfile | None = None,
        timeout_s: float = 60.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.name = config.name
        self._profile = profile or OPENAI_COMPATIBLE.get(config.name, ProviderProfile(base_url=""))
        base_url = (config.base_url or self._profile.base_url).rstrip("/")
        if not base_url:
            raise ValueError(
                f"{config.name.upper()}_BASE_URL is required: no default base URL is known "
                f"for provider '{config.name}'"
            )
        # Ollama's native API is at the root and its OpenAI-compatible surface
        # at `/v1`. Operators habitually set the bare host, which would 404 on
        # every call, so the suffix is added rather than diagnosed.
        if config.name == "ollama" and not base_url.endswith("/v1"):
            base_url = f"{base_url}/v1"
        self._base_url = base_url
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_s),
            headers=self._headers(config.api_key),
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )

    def _headers(self, api_key: str) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "DiscoveryGram/1.0",
        }
        headers.update(dict(self._profile.headers))
        # Ollama needs no key and ignores the header; sending an empty bearer
        # is worse than sending none, so it is omitted rather than blanked.
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return headers

    def supports_vision(self) -> bool:
        return self._profile.vision

    # --- The call --------------------------------------------------------

    async def complete(
        self,
        *,
        model: str,
        messages: Sequence[Message],
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> Completion:
        if not self._profile.vision and any(message.has_images for message in messages):
            raise LlmUnsupported(
                f"{self.name} cannot accept images; configure a vision-capable provider "
                f"in LLM_CHAIN_VISION",
                provider=self.name,
                model=model,
            )

        payload: dict[str, Any] = {
            "model": model,
            "messages": [self._encode(message) for message in messages],
            "stream": False,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if temperature is not None:
            payload["temperature"] = temperature

        started = time.monotonic()
        try:
            response = await self._client.post(f"{self._base_url}/chat/completions", json=payload)
        except httpx.TimeoutException as exc:
            raise LlmTimeout(
                f"{self.name} did not answer in time", provider=self.name, model=model
            ) from exc
        except httpx.TransportError as exc:
            raise LlmUnavailable(
                f"{self.name} is unreachable: {exc}", provider=self.name, model=model
            ) from exc
        latency = time.monotonic() - started

        if response.status_code >= 400:
            raise self._to_error(response, model)

        return self._parse(response, model=model, latency=latency)

    # --- Encoding --------------------------------------------------------

    @staticmethod
    def _encode(message: Message) -> dict[str, Any]:
        """One message in the wire format.

        Text-only messages keep the plain-string `content` form. The parts form
        is valid everywhere but not accepted everywhere — some OpenAI-compatible
        servers only implement the string form — so it is used only when there
        is genuinely an image to carry.
        """
        if not message.has_images:
            return {"role": message.role, "content": message.text}

        parts: list[dict[str, Any]] = []
        if message.text:
            parts.append({"type": "text", "text": message.text})
        parts.extend(
            {"type": "image_url", "image_url": {"url": image.as_data_url()}}
            for image in message.images
        )
        return {"role": message.role, "content": parts}

    # --- Decoding --------------------------------------------------------

    def _parse(self, response: httpx.Response, *, model: str, latency: float) -> Completion:
        try:
            payload = response.json()
        except ValueError as exc:
            raise LlmBadResponse(
                f"{self.name} returned a non-JSON body", provider=self.name, model=model
            ) from exc

        if not isinstance(payload, Mapping):
            raise LlmBadResponse(
                f"{self.name} returned an unexpected body", provider=self.name, model=model
            )

        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            raise LlmBadResponse(
                f"{self.name} returned no completion choices", provider=self.name, model=model
            )

        first = choices[0]
        if not isinstance(first, Mapping):
            raise LlmBadResponse(
                f"{self.name} returned a malformed choice", provider=self.name, model=model
            )

        message = first.get("message")
        text = ""
        if isinstance(message, Mapping):
            text = _content_text(message.get("content"))

        if not text.strip():
            raise LlmBadResponse(
                f"{self.name} returned an empty completion", provider=self.name, model=model
            )

        return Completion(
            text=text.strip(),
            provider=self.name,
            model=str(payload.get("model") or model),
            usage=_parse_usage(payload.get("usage")),
            latency_s=latency,
            finish_reason=str(first.get("finish_reason") or ""),
        )

    def _to_error(self, response: httpx.Response, model: str) -> LlmError:
        status = response.status_code
        detail = _detail(response)

        if status in (401, 403):
            return LlmAuthError(
                f"{self.name} rejected the credentials. Check {self.name.upper()}_API_KEY."
                + (f" ({detail})" if detail else ""),
                provider=self.name,
                model=model,
                status=status,
            )
        if status == 404:
            # A 404 from a chat-completions endpoint means "no such model" far
            # more often than "no such route", and either way this rung is dead
            # while the provider's other models may be fine.
            return LlmInvalidRequest(
                detail or f"{self.name} does not serve model '{model}'",
                provider=self.name,
                model=model,
                status=status,
            )
        if status == 429:
            return LlmRateLimited(
                detail or f"{self.name} is rate-limiting this model",
                provider=self.name,
                model=model,
                retry_after=parse_retry_after(response.headers.get("Retry-After")),
            )
        if 400 <= status < 500:
            return LlmInvalidRequest(
                detail or f"{self.name} rejected the request ({status})",
                provider=self.name,
                model=model,
                status=status,
            )
        return LlmUnavailable(
            detail or f"{self.name} returned HTTP {status}",
            provider=self.name,
            model=model,
            status=status,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


# --- Shared helpers, reused by the dialect adapters ----------------------


def parse_retry_after(raw: str | None) -> float | None:
    """`Retry-After` in seconds, when it is a number we can trust.

    The HTTP-date form is accepted by the spec but is not worth parsing here:
    an unparseable value simply falls back to the router's own backoff.
    """
    if not raw:
        return None
    try:
        value = float(raw.strip())
    except ValueError:
        return None
    if value < 0:
        return None
    # A provider asking us to wait an hour is not something a Telegram user can
    # wait through; the router treats the rung as failed rather than sleeping.
    return min(value, 3600.0)


def _content_text(content: Any) -> str:
    """Flatten `content`, which may be a string or a list of parts.

    Some providers (and every provider in "thinking" mode) return the parts
    form even for plain text, so reading `content` as a string would drop the
    answer entirely.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        pieces: list[str] = []
        for part in content:
            if isinstance(part, str):
                pieces.append(part)
            elif isinstance(part, Mapping) and isinstance(part.get("text"), str):
                pieces.append(part["text"])
        return "".join(pieces)
    return ""


def _parse_usage(raw: Any) -> Usage:
    if not isinstance(raw, Mapping):
        return Usage()
    return Usage(
        prompt_tokens=_int_or_none(raw.get("prompt_tokens")),
        completion_tokens=_int_or_none(raw.get("completion_tokens")),
    )


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return int(value)


def _detail(response: httpx.Response) -> str:
    """The provider's own message, wherever it decided to put it."""
    try:
        payload = response.json()
    except ValueError:
        return response.text.strip()[:200]
    if not isinstance(payload, Mapping):
        return ""
    error = payload.get("error")
    if isinstance(error, Mapping):
        return str(error.get("message") or error.get("code") or "")[:200]
    if isinstance(error, str):
        return error[:200]
    detail = payload.get("detail") or payload.get("message")
    return str(detail)[:200] if detail else ""


__all__ = [
    "OPENAI_COMPATIBLE",
    "OpenAiCompatibleClient",
    "ProviderProfile",
    "parse_retry_after",
]
