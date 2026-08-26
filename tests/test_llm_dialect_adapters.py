"""The three providers that do not speak OpenAI: gemini, cloudflare, puter.

Each gets the same fault-injection sweep as the OpenAI-compatible adapter —
401, 429, 5xx, timeout, malformed — plus the dialect-specific traps that make
them worth writing separately: Gemini's absent `system` role, and the `200 with
success:false` that both Cloudflare and Puter use to report failure.
"""

from __future__ import annotations

import base64

import httpx
import pytest
import respx

from discoverygram.llm.cloudflare import CloudflareClient
from discoverygram.llm.gemini import GeminiClient
from discoverygram.llm.plan import ProviderConfig
from discoverygram.llm.puter import PuterClient
from discoverygram.ports.llm import ImagePart, Message
from discoverygram.ports.llm_errors import (
    LlmAuthError,
    LlmBadResponse,
    LlmInvalidRequest,
    LlmRateLimited,
    LlmTimeout,
    LlmUnavailable,
)

# --- Gemini --------------------------------------------------------------

GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"
)


def _gemini() -> GeminiClient:
    return GeminiClient(ProviderConfig(name="gemini", api_key="AIza-test"), timeout_s=1.0)


def _gemini_ok(text: str = "A cat.") -> dict[str, object]:
    return {
        "candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 20, "candidatesTokenCount": 4},
        "modelVersion": "gemini-2.0-flash",
    }


@respx.mock
async def test_gemini_parses_a_completion() -> None:
    respx.post(GEMINI_URL).mock(return_value=httpx.Response(200, json=_gemini_ok()))

    completion = await _gemini().complete(
        model="gemini-2.0-flash", messages=[Message(role="user", text="what is this?")]
    )

    assert completion.text == "A cat."
    assert completion.provider == "gemini"
    assert completion.usage.total_tokens == 24


@respx.mock
async def test_gemini_sends_the_key_in_a_header_not_the_query_string() -> None:
    """A query string ends up in access logs; this one is a secret."""
    route = respx.post(GEMINI_URL).mock(return_value=httpx.Response(200, json=_gemini_ok()))

    await _gemini().complete(model="gemini-2.0-flash", messages=[Message(role="user", text="hi")])

    request = route.calls.last.request
    assert request.headers["x-goog-api-key"] == "AIza-test"
    assert "key=" not in str(request.url)


@respx.mock
async def test_gemini_lifts_the_system_message_out_of_the_conversation() -> None:
    """Gemini has no system role: a system turn sent as `contents` is rejected."""
    route = respx.post(GEMINI_URL).mock(return_value=httpx.Response(200, json=_gemini_ok()))

    await _gemini().complete(
        model="gemini-2.0-flash",
        messages=[
            Message(role="system", text="Be brief."),
            Message(role="user", text="hi"),
            Message(role="assistant", text="hello"),
        ],
    )

    payload = httpx.Response(200, content=route.calls.last.request.content).json()
    assert payload["systemInstruction"]["parts"][0]["text"] == "Be brief."
    assert [content["role"] for content in payload["contents"]] == ["user", "model"]


@respx.mock
async def test_gemini_inlines_an_image_as_raw_base64_without_a_data_url_prefix() -> None:
    route = respx.post(GEMINI_URL).mock(return_value=httpx.Response(200, json=_gemini_ok()))

    await _gemini().complete(
        model="gemini-2.0-flash",
        messages=[
            Message(
                role="user",
                text="read it",
                images=(ImagePart(data=b"\x89PNG", mime_type="image/png"),),
            )
        ],
    )

    parts = httpx.Response(200, content=route.calls.last.request.content).json()["contents"][0][
        "parts"
    ]
    assert parts[1]["inline_data"]["mime_type"] == "image/png"
    assert parts[1]["inline_data"]["data"] == base64.b64encode(b"\x89PNG").decode()


@respx.mock
async def test_gemini_accepts_a_model_already_carrying_the_models_prefix() -> None:
    route = respx.post(GEMINI_URL).mock(return_value=httpx.Response(200, json=_gemini_ok()))

    await _gemini().complete(
        model="models/gemini-2.0-flash", messages=[Message(role="user", text="hi")]
    )

    assert route.calls.last.request.url.path.count("/models/") == 1


@respx.mock
async def test_a_gemini_safety_block_advances_the_rung_rather_than_retrying_it() -> None:
    """Retrying a refused prompt gets refused again; only the model can change."""
    respx.post(GEMINI_URL).mock(
        return_value=httpx.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}})
    )

    with pytest.raises(LlmInvalidRequest) as caught:
        await _gemini().complete(
            model="gemini-2.0-flash", messages=[Message(role="user", text="hi")]
        )

    assert caught.value.retryable is False
    assert "safety" in str(caught.value)


@respx.mock
async def test_a_gemini_candidate_blocked_after_generation_is_also_fatal() -> None:
    respx.post(GEMINI_URL).mock(
        return_value=httpx.Response(200, json={"candidates": [{"finishReason": "RECITATION"}]})
    )

    with pytest.raises(LlmInvalidRequest):
        await _gemini().complete(
            model="gemini-2.0-flash", messages=[Message(role="user", text="hi")]
        )


@respx.mock
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, LlmAuthError),
        (403, LlmAuthError),
        (404, LlmInvalidRequest),
        (429, LlmRateLimited),
        (400, LlmInvalidRequest),
        (500, LlmUnavailable),
        (503, LlmUnavailable),
    ],
)
async def test_gemini_maps_every_status_to_a_routing_decision(
    status: int, expected: type[Exception]
) -> None:
    respx.post(GEMINI_URL).mock(
        return_value=httpx.Response(status, json={"error": {"message": "nope"}})
    )

    with pytest.raises(expected):
        await _gemini().complete(
            model="gemini-2.0-flash", messages=[Message(role="user", text="hi")]
        )


@respx.mock
async def test_gemini_timeouts_and_malformed_bodies() -> None:
    respx.post(GEMINI_URL).mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(LlmTimeout):
        await _gemini().complete(model="gemini-2.0-flash", messages=[Message("user", "hi")])

    respx.post(GEMINI_URL).mock(return_value=httpx.Response(200, json={"candidates": []}))
    with pytest.raises(LlmBadResponse):
        await _gemini().complete(model="gemini-2.0-flash", messages=[Message("user", "hi")])

    respx.post(GEMINI_URL).mock(return_value=httpx.Response(200, text="not json"))
    with pytest.raises(LlmBadResponse):
        await _gemini().complete(model="gemini-2.0-flash", messages=[Message("user", "hi")])


# --- Cloudflare ----------------------------------------------------------

CF_URL = "https://api.cloudflare.com/client/v4/accounts/acc-1/ai/run/@cf/meta/llama-3-8b"


def _cloudflare() -> CloudflareClient:
    return CloudflareClient(
        ProviderConfig(name="cloudflare", api_key="cf-test", account_id="acc-1"), timeout_s=1.0
    )


def test_cloudflare_without_an_account_id_is_refused_when_the_client_is_built() -> None:
    """Named at build time, not as a 404 per attempt."""
    with pytest.raises(ValueError, match="CLOUDFLARE_ACCOUNT_ID"):
        CloudflareClient(ProviderConfig(name="cloudflare", api_key="cf-test"))


@respx.mock
async def test_cloudflare_parses_its_nested_result() -> None:
    respx.post(CF_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "success": True,
                "result": {
                    "response": "Sure.",
                    "usage": {"prompt_tokens": 5, "completion_tokens": 2},
                },
            },
        )
    )

    completion = await _cloudflare().complete(
        model="@cf/meta/llama-3-8b", messages=[Message(role="user", text="hi")]
    )

    assert completion.text == "Sure."
    assert completion.usage.total_tokens == 7


@respx.mock
async def test_a_cloudflare_200_with_success_false_is_still_a_failure() -> None:
    """The status code alone never decides whether a Workers AI call worked."""
    respx.post(CF_URL).mock(
        return_value=httpx.Response(
            200,
            json={"success": False, "errors": [{"code": 7003, "message": "no such model"}]},
        )
    )

    with pytest.raises(LlmInvalidRequest, match="no such model"):
        await _cloudflare().complete(model="@cf/meta/llama-3-8b", messages=[Message("user", "hi")])


@respx.mock
async def test_a_cloudflare_auth_error_code_is_provider_level_despite_the_200() -> None:
    respx.post(CF_URL).mock(
        return_value=httpx.Response(
            200, json={"success": False, "errors": [{"code": 10000, "message": "bad token"}]}
        )
    )

    with pytest.raises(LlmAuthError) as caught:
        await _cloudflare().complete(model="@cf/meta/llama-3-8b", messages=[Message("user", "hi")])

    assert caught.value.provider_level is True


@respx.mock
async def test_cloudflare_sends_images_alongside_the_text_not_inside_it() -> None:
    route = respx.post(CF_URL).mock(
        return_value=httpx.Response(200, json={"success": True, "result": {"response": "ok"}})
    )

    await _cloudflare().complete(
        model="@cf/meta/llama-3-8b",
        messages=[Message(role="user", text="read", images=(ImagePart(data=b"xy"),))],
    )

    message = httpx.Response(200, content=route.calls.last.request.content).json()["messages"][0]
    assert message["content"] == "read"
    assert message["image"] == [base64.b64encode(b"xy").decode()]


@respx.mock
@pytest.mark.parametrize(
    ("status", "expected"),
    [(401, LlmAuthError), (404, LlmInvalidRequest), (429, LlmRateLimited), (502, LlmUnavailable)],
)
async def test_cloudflare_maps_every_status(status: int, expected: type[Exception]) -> None:
    respx.post(CF_URL).mock(return_value=httpx.Response(status, json={"errors": []}))

    with pytest.raises(expected):
        await _cloudflare().complete(model="@cf/meta/llama-3-8b", messages=[Message("user", "hi")])


@respx.mock
async def test_cloudflare_timeouts_and_malformed_bodies() -> None:
    respx.post(CF_URL).mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(LlmUnavailable):
        await _cloudflare().complete(model="@cf/meta/llama-3-8b", messages=[Message("user", "hi")])

    respx.post(CF_URL).mock(
        return_value=httpx.Response(200, json={"success": True, "result": {"response": ""}})
    )
    with pytest.raises(LlmBadResponse):
        await _cloudflare().complete(model="@cf/meta/llama-3-8b", messages=[Message("user", "hi")])


# --- Puter ---------------------------------------------------------------

PUTER_URL = "https://api.puter.com/drivers/call"


def _puter() -> PuterClient:
    return PuterClient(ProviderConfig(name="puter", api_key="puter-test"), timeout_s=1.0)


@respx.mock
async def test_puter_wraps_the_request_in_a_driver_call() -> None:
    route = respx.post(PUTER_URL).mock(
        return_value=httpx.Response(
            200, json={"success": True, "result": {"message": {"content": "Hi."}}}
        )
    )

    completion = await _puter().complete(
        model="gpt-4o-mini", messages=[Message(role="user", text="hi")]
    )

    payload = httpx.Response(200, content=route.calls.last.request.content).json()
    assert payload["interface"] == "puter-chat-completion"
    assert payload["method"] == "complete"
    assert payload["args"]["model"] == "gpt-4o-mini"
    assert completion.text == "Hi."


@respx.mock
async def test_puter_reads_the_answer_from_either_shape_its_backends_produce() -> None:
    respx.post(PUTER_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "success": True,
                "result": {"message": {"content": [{"type": "text", "text": "parts form"}]}},
            },
        )
    )
    assert (await _puter().complete(model="m", messages=[Message("user", "hi")])).text == (
        "parts form"
    )

    respx.post(PUTER_URL).mock(
        return_value=httpx.Response(200, json={"success": True, "result": {"text": "bare form"}})
    )
    assert (await _puter().complete(model="m", messages=[Message("user", "hi")])).text == (
        "bare form"
    )


@respx.mock
async def test_a_puter_permission_error_is_provider_level_despite_the_200() -> None:
    respx.post(PUTER_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "success": False,
                "error": {"code": "permission_denied", "message": "no access"},
            },
        )
    )

    with pytest.raises(LlmAuthError) as caught:
        await _puter().complete(model="m", messages=[Message("user", "hi")])

    assert caught.value.provider_level is True


@respx.mock
async def test_puter_out_of_funds_is_treated_as_a_rate_limit() -> None:
    """Retryable at the rung, but the next model may be cheaper."""
    respx.post(PUTER_URL).mock(
        return_value=httpx.Response(
            200, json={"success": False, "error": {"code": "insufficient_funds"}}
        )
    )

    with pytest.raises(LlmRateLimited):
        await _puter().complete(model="m", messages=[Message("user", "hi")])


@respx.mock
@pytest.mark.parametrize(
    ("status", "expected"),
    [(401, LlmAuthError), (429, LlmRateLimited), (400, LlmInvalidRequest), (500, LlmUnavailable)],
)
async def test_puter_maps_every_status(status: int, expected: type[Exception]) -> None:
    respx.post(PUTER_URL).mock(return_value=httpx.Response(status, json={"message": "nope"}))

    with pytest.raises(expected):
        await _puter().complete(model="m", messages=[Message("user", "hi")])


@respx.mock
async def test_puter_timeouts_and_malformed_bodies() -> None:
    respx.post(PUTER_URL).mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(LlmTimeout):
        await _puter().complete(model="m", messages=[Message("user", "hi")])

    respx.post(PUTER_URL).mock(return_value=httpx.Response(200, json={"success": True}))
    with pytest.raises(LlmBadResponse):
        await _puter().complete(model="m", messages=[Message("user", "hi")])


# --- Shared contract -----------------------------------------------------


async def test_every_dialect_adapter_closes_its_own_client() -> None:
    for client in (
        _gemini(),
        _cloudflare(),
        _puter(),
    ):
        await client.aclose()
        # Idempotent: shutdown calls it, and a context manager may too.
        await client.aclose()
        assert client.supports_vision() is True


# --- Shape resilience ----------------------------------------------------
#
# A provider that changes a field name must produce a typed error, never an
# unhandled `KeyError` or a `TypeError` escaping into the Telegram error
# handler. These sweep the defensive branches of each decoder.


@respx.mock
@pytest.mark.parametrize(
    "body",
    [
        {"candidates": [{"content": {"parts": "not a list"}}]},
        {"candidates": [{"content": "not a mapping"}]},
        {"candidates": [{"content": {"parts": [{"inlineData": {}}]}}]},
        {"candidates": ["not a mapping"]},
        {"candidates": [{}]},
        "a bare string",
    ],
)
async def test_a_gemini_body_of_the_wrong_shape_is_a_typed_error(body: object) -> None:
    respx.post(GEMINI_URL).mock(return_value=httpx.Response(200, json=body))

    with pytest.raises(LlmBadResponse):
        await _gemini().complete(model="gemini-2.0-flash", messages=[Message("user", "hi")])


@respx.mock
async def test_gemini_reports_no_tokens_rather_than_zero_when_none_are_given() -> None:
    respx.post(GEMINI_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "candidates": [{"content": {"parts": [{"text": "hi"}]}}],
                "usageMetadata": "unexpected",
            },
        )
    )

    completion = await _gemini().complete(
        model="gemini-2.0-flash", messages=[Message("user", "hi")]
    )

    assert completion.usage.total_tokens is None


@respx.mock
async def test_a_gemini_message_with_neither_text_nor_images_is_not_sent() -> None:
    """An empty turn would be rejected by the API as a content-less part."""
    route = respx.post(GEMINI_URL).mock(return_value=httpx.Response(200, json=_gemini_ok()))

    await _gemini().complete(
        model="gemini-2.0-flash",
        messages=[Message(role="user", text=""), Message(role="user", text="real")],
    )

    payload = httpx.Response(200, content=route.calls.last.request.content).json()
    assert len(payload["contents"]) == 1


@respx.mock
@pytest.mark.parametrize(
    "body",
    [
        {"success": True, "result": "not a mapping"},
        {"success": True, "result": {"response": 42}},
        {"success": True},
        "a bare string",
    ],
)
async def test_a_cloudflare_body_of_the_wrong_shape_is_a_typed_error(body: object) -> None:
    respx.post(CF_URL).mock(return_value=httpx.Response(200, json=body))

    with pytest.raises(LlmBadResponse):
        await _cloudflare().complete(model="@cf/meta/llama-3-8b", messages=[Message("user", "hi")])


@respx.mock
async def test_a_cloudflare_failure_with_no_usable_error_array_still_explains_itself() -> None:
    respx.post(CF_URL).mock(return_value=httpx.Response(200, json={"success": False}))

    with pytest.raises(LlmInvalidRequest, match="refused the request"):
        await _cloudflare().complete(model="@cf/meta/llama-3-8b", messages=[Message("user", "hi")])


@respx.mock
async def test_a_cloudflare_non_json_error_body_falls_back_to_the_text() -> None:
    respx.post(CF_URL).mock(return_value=httpx.Response(500, text="gateway exploded"))

    with pytest.raises(LlmUnavailable, match="gateway exploded"):
        await _cloudflare().complete(model="@cf/meta/llama-3-8b", messages=[Message("user", "hi")])


@respx.mock
@pytest.mark.parametrize(
    "body",
    [
        {"success": True, "result": {"message": {"content": 42}}},
        {"success": True, "result": {"message": "not a mapping"}},
        {"success": True, "result": {"text": ""}},
        {"success": True, "result": "not a mapping"},
        "a bare string",
    ],
)
async def test_a_puter_body_of_the_wrong_shape_is_a_typed_error(body: object) -> None:
    respx.post(PUTER_URL).mock(return_value=httpx.Response(200, json=body))

    with pytest.raises(LlmBadResponse):
        await _puter().complete(model="m", messages=[Message("user", "hi")])


@respx.mock
async def test_puter_carries_an_image_in_the_openai_parts_form() -> None:
    route = respx.post(PUTER_URL).mock(
        return_value=httpx.Response(
            200, json={"success": True, "result": {"message": {"content": "ok"}}}
        )
    )

    await _puter().complete(
        model="m", messages=[Message(role="user", text="read", images=(ImagePart(data=b"xy"),))]
    )

    parts = httpx.Response(200, content=route.calls.last.request.content).json()["args"][
        "messages"
    ][0]["content"]
    assert parts[0]["type"] == "text"
    assert parts[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


@respx.mock
async def test_puter_reads_the_alternative_token_field_names() -> None:
    """Its back ends disagree on `prompt_tokens` versus `input_tokens`."""
    respx.post(PUTER_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "success": True,
                "result": {
                    "message": {"content": "ok"},
                    "usage": {"input_tokens": 8, "output_tokens": 2},
                },
            },
        )
    )

    completion = await _puter().complete(model="m", messages=[Message("user", "hi")])

    assert completion.usage.total_tokens == 10


@respx.mock
async def test_a_puter_error_given_as_a_bare_string_is_still_reported() -> None:
    respx.post(PUTER_URL).mock(
        return_value=httpx.Response(200, json={"success": False, "error": "something broke"})
    )

    with pytest.raises(LlmInvalidRequest, match="something broke"):
        await _puter().complete(model="m", messages=[Message("user", "hi")])


@respx.mock
async def test_a_puter_non_json_error_body_falls_back_to_the_text() -> None:
    respx.post(PUTER_URL).mock(return_value=httpx.Response(502, text="bad gateway"))

    with pytest.raises(LlmUnavailable, match="bad gateway"):
        await _puter().complete(model="m", messages=[Message("user", "hi")])
