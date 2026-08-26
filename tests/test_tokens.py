"""Callback tokens — the workaround for Telegram's 64-byte `callback_data`."""

from __future__ import annotations

import pytest

from discoverygram.adapters.session import MemorySessionStore
from discoverygram.bot.tokens import (
    CALLBACK_DATA_MAX_BYTES,
    CallbackDataTooLongError,
    CallbackTokens,
    fits_in_callback_data,
)

# Longer than 64 bytes on its own, which is the entire reason this exists.
LONG_PATH = "Projects/2026/Research/Machine Learning/Papers/attention-is-all-you-need.md"


@pytest.fixture
def tokens() -> CallbackTokens:
    return CallbackTokens(MemorySessionStore(default_ttl_s=3600), ttl_s=3600)


async def test_a_payload_far_over_the_limit_still_fits_in_a_button(
    tokens: CallbackTokens,
) -> None:
    assert len(LONG_PATH.encode()) > CALLBACK_DATA_MAX_BYTES

    data = await tokens.issue("open", {"path": LONG_PATH})

    assert fits_in_callback_data(data)
    assert await tokens.resolve(data) == {"path": LONG_PATH}


async def test_the_action_stays_readable_in_the_callback_data(
    tokens: CallbackTokens,
) -> None:
    """A handler routes on the action without a store round trip."""
    data = await tokens.issue("page", {"cursor": 2})

    action, token = CallbackTokens.parse(data)

    assert action == "page"
    assert token


async def test_two_issues_of_the_same_payload_get_different_tokens(
    tokens: CallbackTokens,
) -> None:
    """Tokens are random, not derived: one user's button reveals nothing."""
    first = await tokens.issue("open", {"path": "A.md"})
    second = await tokens.issue("open", {"path": "A.md"})

    assert first != second
    assert await tokens.resolve(first) == await tokens.resolve(second)


async def test_an_unknown_token_resolves_to_none(tokens: CallbackTokens) -> None:
    assert await tokens.resolve("open:deadbeefcafe") is None


async def test_callback_data_without_a_token_resolves_to_none(
    tokens: CallbackTokens,
) -> None:
    assert await tokens.resolve("noop:") is None
    assert await tokens.resolve("garbage") is None


async def test_revoke_makes_a_one_shot_button_inert(tokens: CallbackTokens) -> None:
    """A double tap on `Delete` must not delete twice."""
    data = await tokens.issue("delete", {"path": "A.md"})

    await tokens.revoke(data)

    assert await tokens.resolve(data) is None


async def test_extend_refreshes_a_token_still_in_use() -> None:
    """Paging must not expire mid-flow because page one was issued an hour ago."""

    class Clock:
        def __init__(self) -> None:
            self.now = 0.0

        def __call__(self) -> float:
            return self.now

    clock = Clock()
    store = MemorySessionStore(default_ttl_s=100, clock=clock)
    tokens = CallbackTokens(store, ttl_s=100)
    data = await tokens.issue("page", {"cursor": 1})

    clock.now = 90.0
    assert await tokens.extend(data) is True

    clock.now = 150.0
    assert await tokens.resolve(data) == {"cursor": 1}


async def test_extend_reports_a_token_that_is_already_gone(
    tokens: CallbackTokens,
) -> None:
    assert await tokens.extend("page:notatoken") is False


async def test_an_expired_token_still_names_the_action() -> None:
    """ "That button expired" is useless; "that search expired" is not."""
    action, _ = CallbackTokens.parse("search:0011223344ff")

    assert action == "search"


@pytest.mark.parametrize("action", ["", "with:colon", "x" * 60])
async def test_an_unusable_action_is_refused_at_issue_time(
    tokens: CallbackTokens, action: str
) -> None:
    """Fail where the bug is, not with a Telegram 400 in front of the user."""
    with pytest.raises(CallbackDataTooLongError):
        await tokens.issue(action, {})


async def test_every_issued_button_is_within_telegram_s_limit(
    tokens: CallbackTokens,
) -> None:
    for action in ("open", "page", "delete", "confirm", "backlinks"):
        data = await tokens.issue(action, {"path": LONG_PATH * 3})
        assert fits_in_callback_data(data)


def test_fits_in_callback_data_rejects_empty_and_oversized() -> None:
    assert not fits_in_callback_data("")
    assert not fits_in_callback_data("x" * 65)
    assert fits_in_callback_data("x" * 64)
