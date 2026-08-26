"""The global error handler: a sentence for the user, everything for the log."""

from __future__ import annotations

import pytest
from telegram.error import BadRequest
from telegram.error import Forbidden as TelegramForbidden

from discoverygram.adapters.session import MemorySessionStore
from discoverygram.app.probe import InstanceState
from discoverygram.bot.deps import DEPS_KEY, BotDeps
from discoverygram.bot.errors import GENERIC, handle_error, is_expected, user_message
from discoverygram.bot.tokens import CallbackTokens
from discoverygram.config import Settings
from discoverygram.ports.errors import (
    Forbidden,
    InvalidRequest,
    NoteStoreError,
    NotFound,
    RateLimited,
    Unauthorized,
    Unavailable,
    Unsupported,
)
from discoverygram.ports.llm_errors import (
    LlmNoProvider,
    LlmQuotaExceeded,
    LlmUnavailable,
)
from discoverygram.ports.model import InstanceConfig
from discoverygram.util.paths import InvalidPath
from tests.fixtures.telegram import FakeBot, FakeContext, make_update


@pytest.fixture
def bot() -> FakeBot:
    return FakeBot()


def make_context(settings: Settings, bot: FakeBot, error: BaseException) -> FakeContext:
    sessions = MemorySessionStore(default_ttl_s=3600)
    deps = BotDeps(
        settings=settings,
        notes=object(),  # type: ignore[arg-type]
        sessions=sessions,
        tokens=CallbackTokens(sessions, ttl_s=3600),
        instance=InstanceState(config=InstanceConfig(), healthy=True),
    )
    context = FakeContext(bot, {DEPS_KEY: deps})
    context.error = error
    return context


@pytest.mark.parametrize(
    ("error", "expected_fragment"),
    [
        (NotFound("Projects/Gone.md"), "could not find"),
        (Unauthorized("bad key"), "API key"),
        (Forbidden("Search is disabled"), "Search is disabled"),
        (Unsupported("Sharing is not an MCP tool. Set REST."), "MCP"),
        (InvalidRequest("content required"), "rejected"),
        (Unavailable("down"), "cannot reach your notes"),
        (RateLimited("slow down", retry_after=30), "30s"),
        (RateLimited("slow down"), "moment"),
        (NoteStoreError("something odd"), "had a problem"),
        (InvalidPath("path escapes the vault"), "not usable"),
    ],
)
def test_each_handled_failure_gets_an_actionable_message(
    error: BaseException, expected_fragment: str
) -> None:
    assert expected_fragment in user_message(error)


def test_an_unexpected_error_gets_the_generic_message() -> None:
    """A bug must not describe itself to whoever provoked it."""
    message = user_message(ZeroDivisionError("division by zero"))

    assert message == GENERIC
    assert "ZeroDivision" not in message
    assert "division by zero" not in message


def test_handled_conditions_are_distinguished_from_defects() -> None:
    assert is_expected(NotFound("x")) is True
    assert is_expected(InvalidPath("x")) is True
    assert is_expected(KeyError("x")) is False


async def test_the_user_is_told_something_went_wrong(settings: Settings, bot: FakeBot) -> None:
    context = make_context(settings, bot, ZeroDivisionError())

    await handle_error(make_update(bot), context)

    assert bot.last_text == GENERIC


async def test_no_traceback_ever_reaches_the_chat(settings: Settings, bot: FakeBot) -> None:
    error = RuntimeError("secret_token=abcdef at /app/src/discoverygram/bot/thing.py:42")
    context = make_context(settings, bot, error)

    await handle_error(make_update(bot), context)

    assert "secret_token" not in bot.last_text
    assert "/app/src" not in bot.last_text


async def test_errors_are_counted_for_status(settings: Settings, bot: FakeBot) -> None:
    context = make_context(settings, bot, Unavailable("down"))

    await handle_error(make_update(bot), context)

    assert context.bot_data[DEPS_KEY].counters["errors"] == 1


async def test_a_blocked_bot_does_not_re_enter_the_error_handler(
    settings: Settings, bot: FakeBot
) -> None:
    """Notifying about a failure must not itself become a failure, and loop."""
    bot.fail_with = TelegramForbidden("bot was blocked by the user")
    context = make_context(settings, bot, Unavailable("down"))

    await handle_error(make_update(bot), context)

    assert bot.sent == []


async def test_a_deleted_chat_is_survivable(settings: Settings, bot: FakeBot) -> None:
    bot.fail_with = BadRequest("chat not found")
    context = make_context(settings, bot, Unavailable("down"))

    await handle_error(make_update(bot), context)

    assert bot.sent == []


async def test_a_failure_outside_a_handler_has_nobody_to_tell(
    settings: Settings, bot: FakeBot
) -> None:
    """python-telegram-bot passes the raw update, which may not be an Update."""
    context = make_context(settings, bot, RuntimeError("job queue blew up"))

    await handle_error("not an update", context)

    assert bot.sent == []


async def test_no_error_means_nothing_to_do(settings: Settings, bot: FakeBot) -> None:
    context = make_context(settings, bot, Unavailable("x"))
    context.error = None

    await handle_error(make_update(bot), context)

    assert bot.sent == []


# --- LLM failures ---------------------------------------------------------


def test_a_spent_daily_cap_is_stated_plainly_rather_than_as_a_failure() -> None:
    """Nothing is broken: it is the user's own budget, and they should know."""
    message = user_message(LlmQuotaExceeded("You have used your 100 AI requests for today."))

    assert message == "You have used your 100 AI requests for today."


def test_a_missing_chain_keeps_the_variable_it_names() -> None:
    """ "Something went wrong" would hide the one thing that fixes it."""
    message = user_message(LlmNoProvider("No chat model is configured. Check LLM_CHAIN_CHAT."))

    assert "LLM_CHAIN_CHAT" in message


def test_any_other_provider_failure_points_at_status() -> None:
    message = user_message(LlmUnavailable("groq is unreachable"))

    assert "/status" in message
    # The provider's own words are not repeated: they name hosts and models the
    # user cannot act on.
    assert "groq" not in message


def test_a_provider_failure_is_an_expected_condition_not_a_defect() -> None:
    """It must not fill the log with tracebacks when a provider has a bad day."""
    assert is_expected(LlmUnavailable("down")) is True
    assert is_expected(RuntimeError("bug")) is False
