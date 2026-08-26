"""Opt-in suite against real LLM providers.

    make test-live            # or: uv run pytest -m live

Separate from `test_live.py` because it needs a *different* credential: that
suite needs a vault, this one needs a provider key. Either can run without the
other, and each skips itself when its own configuration is absent.

Nothing here writes to the vault, and nothing asserts on the *content* of a
completion — a language model is not a fixture. What is asserted is the
contract the router depends on: a rung answers, the answer is stamped with the
rung that served it, and the ladder is walked in the configured order.
"""

from __future__ import annotations

import base64
import os
from collections.abc import AsyncIterator

import pytest

from discoverygram.config import Settings
from discoverygram.llm.factory import build_router
from discoverygram.llm.plan import TaskProfile
from discoverygram.llm.router import LlmRouter
from discoverygram.ports.llm import ImagePart, Message
from discoverygram.ports.llm_errors import LlmError

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not os.getenv("LLM_CHAIN_CHAT"),
        reason="LLM_CHAIN_CHAT is not set; the live LLM suite needs a configured chain",
    ),
]


@pytest.fixture
async def router() -> AsyncIterator[LlmRouter]:
    built = build_router(Settings())  # type: ignore[call-arg]
    try:
        yield built
    finally:
        await built.aclose()


async def test_the_chat_ladder_is_not_empty(router: LlmRouter) -> None:
    """The first thing to check when a generation command misbehaves."""
    ladder = router.ladder(TaskProfile.CHAT)
    if not ladder.usable:
        pytest.fail("LLM_CHAIN_CHAT produced no usable rung. Skipped: " + "; ".join(ladder.skipped))


async def test_a_chat_request_is_answered_and_stamped_with_its_rung(router: LlmRouter) -> None:
    completion = await router.complete(
        TaskProfile.CHAT,
        [Message(role="user", text="Reply with the single word: ready")],
        max_tokens=16,
    )

    assert completion.text
    assert completion.provider in {a.provider for a in router.ladder(TaskProfile.CHAT).attempts}
    assert completion.latency_s > 0


async def test_a_title_request_comes_back_short(router: LlmRouter) -> None:
    """The task profile's own `max_tokens` is what keeps a title a title."""
    completion = await router.complete(
        TaskProfile.TITLE,
        [
            Message(role="system", text="Reply with a short title and nothing else."),
            Message(role="user", text="Notes from the Q1 planning meeting about hiring."),
        ],
    )

    assert 0 < len(completion.text) < 200


async def test_a_vision_request_reads_an_image(router: LlmRouter) -> None:
    """A 1x1 PNG: the point is that the image is *accepted*, not what it says."""
    if not router.available(TaskProfile.VISION):
        pytest.skip("no vision rung configured")

    pixel = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )

    completion = await router.complete(
        TaskProfile.VISION,
        [
            Message(
                role="user",
                text="Describe this image in one short sentence.",
                images=(ImagePart(data=pixel, mime_type="image/png"),),
            )
        ],
        max_tokens=64,
    )

    assert completion.text


async def test_the_usage_ledger_records_what_actually_happened(router: LlmRouter) -> None:
    try:
        await router.complete(
            TaskProfile.CHAT, [Message(role="user", text="Say ok.")], max_tokens=8
        )
    except LlmError as exc:
        pytest.fail(f"the whole ladder failed against real providers: {exc}")

    status = router.status()
    assert status.requests == 1
    assert status.attempts >= 1
    assert status.usage
