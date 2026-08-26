"""Puter — the `drivers/call` dialect.

Puter does not expose a chat-completions endpoint. Everything goes through one
RPC endpoint, `POST /drivers/call`, with the interface and method named in the
body and the real arguments nested under `args`. The result is nested too, and
an application-level failure comes back as `200` with `success: false` — the
same trap as Cloudflare.

Puter is the least standardised of the nine and its API carries no version
guarantee, so it belongs at the *end* of a chain rather than the front. The
adapter reads its endpoint from `PUTER_BASE_URL`, which is what makes it
adjustable without a release when the endpoint moves.
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

DEFAULT_BASE_URL = "https://api.puter.com"
INTERFACE = "puter-chat-completion"


class PuterClient(LlmClient):
    """Puter's driver RPC, wearing the `LlmClient` port."""

    name = "puter"

    def __init__(
        self,
        config: ProviderConfig,
        *,
        timeout_s: float = 60.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.name = config.name
        self._base_url = (config.base_url or DEFAULT_BASE_URL).rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_s),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "DiscoveryGram/1.0",
                "Authorization": f"Bearer {config.api_key}",
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
        args: dict[str, Any] = {
            "messages": [self._encode(message) for message in messages],
            "model": model,
            "stream": False,
        }
        if max_tokens is not None:
            args["max_tokens"] = max_tokens
        if temperature is not None:
            args["temperature"] = temperature

        payload = {
            "interface": INTERFACE,
            "method": "complete",
            "test_mode": False,
            "args": args,
        }

        started = time.monotonic()
        try:
            response = await self._client.post(f"{self._base_url}/drivers/call", json=payload)
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

    @staticmethod
    def _encode(message: Message) -> dict[str, Any]:
        """Puter proxies to OpenAI-shaped back ends, so the parts form applies."""
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

        if payload.get("success") is False:
            raise self._from_error_field(payload.get("error"), model)

        result = payload.get("result")
        if not isinstance(result, Mapping):
            raise LlmBadResponse(f"{self.name} returned no result", provider=self.name, model=model)

        text = _result_text(result)
        if not text.strip():
            raise LlmBadResponse(
                f"{self.name} returned an empty completion", provider=self.name, model=model
            )

        return Completion(
            text=text.strip(),
            provider=self.name,
            model=str(result.get("model") or model),
            usage=_parse_usage(result.get("usage")),
            latency_s=latency,
            finish_reason=str(result.get("finish_reason") or ""),
        )

    def _from_error_field(self, error: Any, model: str) -> LlmError:
        message = ""
        code = ""
        if isinstance(error, Mapping):
            message = str(error.get("message") or "")[:200]
            code = str(error.get("code") or "")
        elif isinstance(error, str):
            message = error[:200]

        if code in {"permission_denied", "token_missing", "invalid_token"}:
            return LlmAuthError(
                f"{self.name} rejected the credentials. Check PUTER_API_KEY."
                + (f" ({message})" if message else ""),
                provider=self.name,
                model=model,
            )
        if code in {"rate_limit_exceeded", "insufficient_funds"}:
            return LlmRateLimited(
                message or f"{self.name} is out of quota",
                provider=self.name,
                model=model,
            )
        return LlmInvalidRequest(
            message or f"{self.name} refused the request", provider=self.name, model=model
        )

    def _to_error(self, response: httpx.Response, model: str) -> LlmError:
        status = response.status_code
        detail = _detail(response)

        if status in (401, 403):
            return LlmAuthError(
                f"{self.name} rejected the credentials. Check PUTER_API_KEY."
                + (f" ({detail})" if detail else ""),
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


def _result_text(result: Mapping[str, Any]) -> str:
    """The answer, wherever this driver put it.

    Puter fronts several back ends and passes their shapes through, so the text
    may be `result.message.content` (a string or parts) or a bare
    `result.text`.
    """
    message = result.get("message")
    if isinstance(message, Mapping):
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(
                part["text"]
                for part in content
                if isinstance(part, Mapping) and isinstance(part.get("text"), str)
            )
    text = result.get("text")
    return text if isinstance(text, str) else ""


def _parse_usage(raw: Any) -> Usage:
    if not isinstance(raw, Mapping):
        return Usage()
    prompt = raw.get("prompt_tokens") or raw.get("input_tokens")
    completion = raw.get("completion_tokens") or raw.get("output_tokens")
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
            return str(error.get("message") or error.get("code") or "")[:200]
        if isinstance(error, str):
            return error[:200]
        message = payload.get("message")
        if message:
            return str(message)[:200]
    return ""


__all__ = ["DEFAULT_BASE_URL", "INTERFACE", "PuterClient"]
