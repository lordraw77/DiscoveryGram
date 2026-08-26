"""Cloudflare Workers AI.

Close to the OpenAI shape but not the same, and different in one way that
shows up in configuration rather than in code: the **account id is part of the
URL**, so `CLOUDFLARE_API_KEY` alone is not enough to make a request. A
configuration missing `CLOUDFLARE_ACCOUNT_ID` is refused when the client is
built, with the variable named, rather than producing a 404 per attempt.

Workers AI also answers `200` for application errors, putting the failure in
`success: false` with an `errors` array, so the status code alone never
decides whether a call worked.
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

DEFAULT_BASE_URL = "https://api.cloudflare.com/client/v4"

# Workers AI error codes that mean "this key cannot do this", whatever the
# HTTP status says.
_AUTH_CODES = frozenset({1000, 9106, 10000})


class CloudflareClient(LlmClient):
    """Cloudflare Workers AI (`/accounts/{id}/ai/run/{model}`)."""

    name = "cloudflare"

    def __init__(
        self,
        config: ProviderConfig,
        *,
        timeout_s: float = 60.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not config.account_id:
            raise ValueError(
                "CLOUDFLARE_ACCOUNT_ID is required when cloudflare is in an LLM chain: "
                "Workers AI puts the account id in the request URL"
            )
        self.name = config.name
        self._account_id = config.account_id
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
        payload: dict[str, Any] = {"messages": [self._encode(m) for m in messages]}
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if temperature is not None:
            payload["temperature"] = temperature

        url = f"{self._base_url}/accounts/{self._account_id}/ai/run/{model}"

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

    @staticmethod
    def _encode(message: Message) -> dict[str, Any]:
        """Workers AI image models take base64 strings in an `image` field.

        The OpenAI parts form is not accepted, so an image travels alongside
        the text rather than inside it.
        """
        encoded: dict[str, Any] = {"role": message.role, "content": message.text}
        if message.has_images:
            encoded["image"] = [image.as_base64() for image in message.images]
        return encoded

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

        # A 200 with `success: false` is Workers AI's normal way of failing.
        if payload.get("success") is False:
            raise self._from_errors(payload.get("errors"), model)

        result = payload.get("result")
        if not isinstance(result, Mapping):
            raise LlmBadResponse(f"{self.name} returned no result", provider=self.name, model=model)

        text = result.get("response")
        if not isinstance(text, str) or not text.strip():
            raise LlmBadResponse(
                f"{self.name} returned an empty completion", provider=self.name, model=model
            )

        return Completion(
            text=text.strip(),
            provider=self.name,
            model=model,
            usage=_parse_usage(result.get("usage")),
            latency_s=latency,
        )

    def _from_errors(self, errors: Any, model: str) -> LlmError:
        message = ""
        code: int | None = None
        if isinstance(errors, list) and errors and isinstance(errors[0], Mapping):
            message = str(errors[0].get("message") or "")[:200]
            raw_code = errors[0].get("code")
            code = raw_code if isinstance(raw_code, int) else None

        if code in _AUTH_CODES:
            return LlmAuthError(
                f"{self.name} rejected the credentials. Check CLOUDFLARE_API_KEY and "
                f"CLOUDFLARE_ACCOUNT_ID." + (f" ({message})" if message else ""),
                provider=self.name,
                model=model,
            )
        return LlmInvalidRequest(
            message or f"{self.name} refused the request",
            provider=self.name,
            model=model,
        )

    def _to_error(self, response: httpx.Response, model: str) -> LlmError:
        status = response.status_code
        detail = _detail(response)

        if status in (401, 403):
            return LlmAuthError(
                f"{self.name} rejected the credentials. Check CLOUDFLARE_API_KEY and "
                f"CLOUDFLARE_ACCOUNT_ID." + (f" ({detail})" if detail else ""),
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


def _parse_usage(raw: Any) -> Usage:
    if not isinstance(raw, Mapping):
        return Usage()
    prompt = raw.get("prompt_tokens")
    completion = raw.get("completion_tokens")
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
        errors = payload.get("errors")
        if isinstance(errors, list) and errors and isinstance(errors[0], Mapping):
            return str(errors[0].get("message") or "")[:200]
    return ""


__all__ = ["DEFAULT_BASE_URL", "CloudflareClient"]
