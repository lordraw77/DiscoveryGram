"""The OpenAI-compatible adapter, including every fault it must classify.

The classification is the point: the router decides whether to retry, whether
to advance a rung and whether to abandon a provider purely from the error type
these tests pin down.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from discoverygram.llm.base import OpenAiCompatibleClient, ProviderProfile, parse_retry_after
from discoverygram.llm.plan import ProviderConfig
from discoverygram.ports.llm import ImagePart, Message
from discoverygram.ports.llm_errors import (
    LlmAuthError,
    LlmBadResponse,
    LlmInvalidRequest,
    LlmRateLimited,
    LlmTimeout,
    LlmUnavailable,
    LlmUnsupported,
)

BASE = "https://api.groq.com/openai/v1"
URL = f"{BASE}/chat/completions"


def _client(**overrides: object) -> OpenAiCompatibleClient:
    config = ProviderConfig(name="groq", api_key="gsk-test", **overrides)  # type: ignore[arg-type]
    return OpenAiCompatibleClient(config, timeout_s=1.0)


def _ok(text: str = "Hello.") -> dict[str, object]:
    return {
        "model": "llama-3.3-70b",
        "choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 3},
    }


# --- The happy path ------------------------------------------------------


@respx.mock
async def test_completion_is_parsed_and_stamped_with_its_rung() -> None:
    respx.post(URL).mock(return_value=httpx.Response(200, json=_ok()))

    completion = await _client().complete(
        model="llama-3.3-70b", messages=[Message(role="user", text="hi")]
    )

    assert completion.text == "Hello."
    assert completion.provider == "groq"
    assert completion.model == "llama-3.3-70b"
    assert completion.usage.total_tokens == 14
    assert completion.finish_reason == "stop"


@respx.mock
async def test_the_request_carries_the_key_the_model_and_the_sampling_settings() -> None:
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=_ok()))

    await _client().complete(
        model="llama-3.3-70b",
        messages=[Message(role="system", text="Be brief."), Message(role="user", text="hi")],
        max_tokens=64,
        temperature=0.2,
    )

    request = route.calls.last.request
    assert request.headers["Authorization"] == "Bearer gsk-test"
    payload = httpx.Response(200, content=request.content).json()
    assert payload["model"] == "llama-3.3-70b"
    assert payload["max_tokens"] == 64
    assert payload["temperature"] == 0.2
    assert payload["stream"] is False
    assert [message["role"] for message in payload["messages"]] == ["system", "user"]


@respx.mock
async def test_text_only_messages_keep_the_plain_string_content_form() -> None:
    """Not every OpenAI-compatible server implements the parts form."""
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=_ok()))

    await _client().complete(model="m", messages=[Message(role="user", text="hi")])

    payload = httpx.Response(200, content=route.calls.last.request.content).json()
    assert payload["messages"][0]["content"] == "hi"


@respx.mock
async def test_an_image_travels_as_a_data_url() -> None:
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=_ok()))
    image = ImagePart(data=b"\xff\xd8\xff", mime_type="image/jpeg")

    await _client().complete(
        model="m", messages=[Message(role="user", text="what is this?", images=(image,))]
    )

    parts = httpx.Response(200, content=route.calls.last.request.content).json()["messages"][0][
        "content"
    ]
    assert parts[0] == {"type": "text", "text": "what is this?"}
    assert parts[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


@respx.mock
async def test_the_parts_content_form_is_flattened_back_to_text() -> None:
    """Providers in thinking mode answer with parts even for plain text."""
    respx.post(URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": [{"type": "text", "text": "a"}, {"text": "b"}]},
                        "finish_reason": "stop",
                    }
                ]
            },
        )
    )

    completion = await _client().complete(model="m", messages=[Message(role="user", text="hi")])

    assert completion.text == "ab"


# --- Capability ----------------------------------------------------------


async def test_a_text_only_provider_refuses_images_without_calling_it() -> None:
    client = OpenAiCompatibleClient(
        ProviderConfig(name="cerebras", api_key="csk-test"),
        profile=ProviderProfile(base_url="https://example.test/v1", vision=False),
    )

    with pytest.raises(LlmUnsupported) as caught:
        await client.complete(
            model="m",
            messages=[Message(role="user", text="?", images=(ImagePart(data=b"x"),))],
        )

    assert "LLM_CHAIN_VISION" in str(caught.value)
    assert client.supports_vision() is False


def test_a_provider_with_no_known_default_and_no_base_url_is_refused_at_build_time() -> None:
    with pytest.raises(ValueError, match="INVENTED_BASE_URL"):
        OpenAiCompatibleClient(ProviderConfig(name="invented", api_key="k"))


def test_ollama_gets_the_openai_compatible_suffix_added() -> None:
    client = OpenAiCompatibleClient(
        ProviderConfig(name="ollama", base_url="http://ollama.test:11434")
    )
    assert client._base_url == "http://ollama.test:11434/v1"


def test_ollama_sends_no_authorization_header() -> None:
    """An empty bearer is worse than none: some proxies reject it outright."""
    client = OpenAiCompatibleClient(ProviderConfig(name="ollama"))
    assert "Authorization" not in client._client.headers


# --- Fault injection -----------------------------------------------------


@respx.mock
async def test_401_is_provider_level_and_never_retryable() -> None:
    respx.post(URL).mock(
        return_value=httpx.Response(401, json={"error": {"message": "Invalid API Key"}})
    )

    with pytest.raises(LlmAuthError) as caught:
        await _client().complete(model="m", messages=[Message(role="user", text="hi")])

    assert caught.value.provider_level is True
    assert caught.value.retryable is False
    assert "GROQ_API_KEY" in str(caught.value)


@respx.mock
async def test_403_is_treated_as_an_auth_failure_too() -> None:
    respx.post(URL).mock(return_value=httpx.Response(403, json={}))

    with pytest.raises(LlmAuthError):
        await _client().complete(model="m", messages=[Message(role="user", text="hi")])


@respx.mock
async def test_429_is_retryable_but_not_provider_level() -> None:
    """The next model of the same provider often still has quota."""
    respx.post(URL).mock(return_value=httpx.Response(429, headers={"Retry-After": "7"}, json={}))

    with pytest.raises(LlmRateLimited) as caught:
        await _client().complete(model="m", messages=[Message(role="user", text="hi")])

    assert caught.value.retryable is True
    assert caught.value.provider_level is False
    assert caught.value.retry_after == 7.0


@respx.mock
@pytest.mark.parametrize("status", [500, 502, 503, 504])
async def test_5xx_is_retryable(status: int) -> None:
    respx.post(URL).mock(return_value=httpx.Response(status, text="upstream exploded"))

    with pytest.raises(LlmUnavailable) as caught:
        await _client().complete(model="m", messages=[Message(role="user", text="hi")])

    assert caught.value.retryable is True
    assert caught.value.provider_level is False


@respx.mock
async def test_404_names_the_model_and_does_not_condemn_the_provider() -> None:
    respx.post(URL).mock(return_value=httpx.Response(404, json={"error": "no such model"}))

    with pytest.raises(LlmInvalidRequest) as caught:
        await _client().complete(model="ghost", messages=[Message(role="user", text="hi")])

    assert caught.value.retryable is False
    assert caught.value.provider_level is False


@respx.mock
async def test_400_is_fatal_for_the_rung_and_carries_the_providers_own_message() -> None:
    respx.post(URL).mock(
        return_value=httpx.Response(400, json={"error": {"message": "context too long"}})
    )

    with pytest.raises(LlmInvalidRequest, match="context too long"):
        await _client().complete(model="m", messages=[Message(role="user", text="hi")])


@respx.mock
async def test_a_timeout_is_its_own_error() -> None:
    respx.post(URL).mock(side_effect=httpx.ReadTimeout("too slow"))

    with pytest.raises(LlmTimeout) as caught:
        await _client().complete(model="m", messages=[Message(role="user", text="hi")])

    assert caught.value.retryable is True


@respx.mock
async def test_an_unreachable_host_is_unavailable() -> None:
    respx.post(URL).mock(side_effect=httpx.ConnectError("refused"))

    with pytest.raises(LlmUnavailable):
        await _client().complete(model="m", messages=[Message(role="user", text="hi")])


@respx.mock
@pytest.mark.parametrize(
    "body",
    [
        {"choices": []},
        {"choices": [{"message": {"content": ""}}]},
        {"choices": [{"message": {"content": "   "}}]},
        {"choices": ["not a mapping"]},
        {"nothing": "useful"},
        [1, 2, 3],
    ],
)
async def test_a_200_that_is_not_a_completion_is_a_bad_response(body: object) -> None:
    respx.post(URL).mock(return_value=httpx.Response(200, json=body))

    with pytest.raises(LlmBadResponse) as caught:
        await _client().complete(model="m", messages=[Message(role="user", text="hi")])

    # Retryable — sampling can produce an empty answer — but the provider is
    # answering, so it must not count against its circuit.
    assert caught.value.retryable is True
    assert caught.value.provider_level is False


@respx.mock
async def test_a_non_json_200_is_a_bad_response() -> None:
    respx.post(URL).mock(return_value=httpx.Response(200, text="<html>oops</html>"))

    with pytest.raises(LlmBadResponse):
        await _client().complete(model="m", messages=[Message(role="user", text="hi")])


# --- Retry-After parsing -------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("5", 5.0),
        (" 2.5 ", 2.5),
        ("0", 0.0),
        ("-1", None),
        ("Wed, 21 Oct 2015 07:28:00 GMT", None),
        ("", None),
        (None, None),
        ("999999", 3600.0),
    ],
)
def test_retry_after_is_read_only_when_it_is_a_number_we_can_trust(
    raw: str | None, expected: float | None
) -> None:
    assert parse_retry_after(raw) == expected
