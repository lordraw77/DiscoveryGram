"""Google Gemini — its own dialect, not OpenAI-compatible.

Three differences that matter:

* the key rides in the `x-goog-api-key` header, not a bearer token;
* the model is in the **path**, so every model is a different URL;
* roles are `user` / `model`, there is no `system` role, and the system prompt
  is a separate `systemInstruction` field.

Gemini is also the most likely vision rung in a default configuration, so its
image encoding (`inline_data`, base64, no data-URL prefix) is exercised hard.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from typing import Any

import httpx

from discoverygram.llm.base import parse_retry_after
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
)
from discoverygram.util.logging import get_logger

log = get_logger(__name__)

DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

# Gemini's own words for "I stopped early", mapped to the reason a caller sees.
_BLOCKED_REASONS = {"SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII"}


class GeminiClient(LlmClient):
    """Google's Generative Language API."""

    name = "gemini"

    def __init__(
        self,
        config: ProviderConfig,
        *,
        timeout_s: float = 60.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.name = config.name
        self._api_key = config.api_key
        self._base_url = (config.base_url or DEFAULT_BASE_URL).rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_s),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "DiscoveryGram/1.0",
                # The header form, not `?key=`: a query string ends up in
                # access logs and proxy traces, and this one is a secret.
                "x-goog-api-key": self._api_key,
            },
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )

    def supports_vision(self) -> bool:
        return True

    async def complete(
        self,
        *,
        model: str,
        messages: Sequence[Message],
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> Completion:
        payload = self._encode(messages, max_tokens=max_tokens, temperature=temperature)
        # A model may be configured with or without the `models/` prefix.
        model_path = model if model.startswith("models/") else f"models/{model}"
        url = f"{self._base_url}/{model_path}:generateContent"

        started = time.monotonic()
        try:
            response = await self._client.post(url, json=payload)
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

    def _encode(
        self,
        messages: Sequence[Message],
        *,
        max_tokens: int | None,
        temperature: float | None,
    ) -> dict[str, Any]:
        contents: list[dict[str, Any]] = []
        system_parts: list[str] = []

        for message in messages:
            if message.role == "system":
                # Gemini has no system turn: collected and sent separately.
                system_parts.append(message.text)
                continue

            parts: list[dict[str, Any]] = []
            if message.text:
                parts.append({"text": message.text})
            parts.extend(
                {"inline_data": {"mime_type": image.mime_type, "data": image.as_base64()}}
                for image in message.images
            )
            if not parts:
                continue
            contents.append(
                {"role": "model" if message.role == "assistant" else "user", "parts": parts}
            )

        payload: dict[str, Any] = {"contents": contents}
        if system_parts:
            payload["systemInstruction"] = {"parts": [{"text": "\n\n".join(system_parts)}]}

        generation: dict[str, Any] = {}
        if max_tokens is not None:
            generation["maxOutputTokens"] = max_tokens
        if temperature is not None:
            generation["temperature"] = temperature
        if generation:
            payload["generationConfig"] = generation

        return payload

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

        blocked = self._blocked_reason(payload)
        if blocked:
            # A safety block is a fact about the prompt, not about the provider:
            # retrying the same rung, or failing over to another one, will get
            # the same answer. `LlmInvalidRequest` advances past this model.
            raise LlmInvalidRequest(
                f"{self.name} refused to answer ({blocked.lower()})",
                provider=self.name,
                model=model,
            )

        candidates = payload.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            raise LlmBadResponse(
                f"{self.name} returned no candidates", provider=self.name, model=model
            )

        first = candidates[0]
        if not isinstance(first, Mapping):
            raise LlmBadResponse(
                f"{self.name} returned a malformed candidate", provider=self.name, model=model
            )

        text = _candidate_text(first)
        if not text.strip():
            raise LlmBadResponse(
                f"{self.name} returned an empty completion", provider=self.name, model=model
            )

        return Completion(
            text=text.strip(),
            provider=self.name,
            model=str(payload.get("modelVersion") or model),
            usage=_parse_usage(payload.get("usageMetadata")),
            latency_s=latency,
            finish_reason=str(first.get("finishReason") or ""),
        )

    @staticmethod
    def _blocked_reason(payload: Mapping[str, Any]) -> str:
        feedback = payload.get("promptFeedback")
        if isinstance(feedback, Mapping) and feedback.get("blockReason"):
            return str(feedback["blockReason"])
        candidates = payload.get("candidates")
        if isinstance(candidates, list) and candidates and isinstance(candidates[0], Mapping):
            reason = str(candidates[0].get("finishReason") or "")
            if reason in _BLOCKED_REASONS:
                return reason
        return ""

    def _to_error(self, response: httpx.Response, model: str) -> LlmError:
        status = response.status_code
        detail = _detail(response)

        if status in (401, 403):
            return LlmAuthError(
                f"{self.name} rejected the credentials. Check GEMINI_API_KEY."
                + (f" ({detail})" if detail else ""),
                provider=self.name,
                model=model,
                status=status,
            )
        if status == 404:
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


def _candidate_text(candidate: Mapping[str, Any]) -> str:
    content = candidate.get("content")
    if not isinstance(content, Mapping):
        return ""
    parts = content.get("parts")
    if not isinstance(parts, list):
        return ""
    return "".join(
        part["text"]
        for part in parts
        if isinstance(part, Mapping) and isinstance(part.get("text"), str)
    )


def _parse_usage(raw: Any) -> Usage:
    if not isinstance(raw, Mapping):
        return Usage()
    prompt = raw.get("promptTokenCount")
    completion = raw.get("candidatesTokenCount")
    return Usage(
        prompt_tokens=int(prompt) if isinstance(prompt, int) else None,
        completion_tokens=int(completion) if isinstance(completion, int) else None,
    )


def _detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text.strip()[:200]
    if isinstance(payload, Mapping):
        error = payload.get("error")
        if isinstance(error, Mapping):
            return str(error.get("message") or error.get("status") or "")[:200]
    return ""


__all__ = ["DEFAULT_BASE_URL", "GeminiClient"]
