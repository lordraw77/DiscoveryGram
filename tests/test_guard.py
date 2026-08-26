"""The allow-list — the only thing standing between the vault and everyone else."""

from __future__ import annotations

import pytest
from telegram.ext import ApplicationHandlerStop

from discoverygram.adapters.session import MemorySessionStore
from discoverygram.app.probe import InstanceState
from discoverygram.bot.deps import DEPS_KEY, BotDeps
from discoverygram.bot.guard import enforce_allow_list
from discoverygram.bot.tokens import CallbackTokens
from discoverygram.config import Settings
from discoverygram.ports.model import InstanceConfig
from tests.fixtures.telegram import (
    ALLOWED_USER_ID,
    DENIED_USER_ID,
    FakeBot,
    FakeContext,
    as_context,
    make_update,
)


class StubNoteStore:
    """The guard never touches the note store; it only has to exist."""

    async def health(self) -> bool:
        return True


@pytest.fixture
def bot() -> FakeBot:
    return FakeBot()


def make_context(settings: Settings, bot: FakeBot) -> FakeContext:
    sessions = MemorySessionStore(default_ttl_s=3600)
    deps = BotDeps(
        settings=settings,
        notes=StubNoteStore(),  # type: ignore[arg-type]
        sessions=sessions,
        tokens=CallbackTokens(sessions, ttl_s=3600),
        instance=InstanceState(config=InstanceConfig(), healthy=True),
    )
    return FakeContext(bot, {DEPS_KEY: deps})


async def test_an_allow_listed_user_passes_through(settings: Settings, bot: FakeBot) -> None:
    context = make_context(settings, bot)

    await enforce_allow_list(make_update(bot), as_context(context))

    assert bot.sent == []


async def test_a_stranger_is_stopped_before_any_handler_runs(
    settings: Settings, bot: FakeBot
) -> None:
    """`ApplicationHandlerStop` is what prevents the update reaching group 0."""
    context = make_context(settings, bot)

    with pytest.raises(ApplicationHandlerStop):
        await enforce_allow_list(make_update(bot, user_id=DENIED_USER_ID), as_context(context))


async def test_the_refusal_tells_the_user_their_own_id(settings: Settings, bot: FakeBot) -> None:
    """That id is the number the operator needs; the user already has it."""
    context = make_context(settings, bot)

    with pytest.raises(ApplicationHandlerStop):
        await enforce_allow_list(make_update(bot, user_id=DENIED_USER_ID), as_context(context))

    assert str(DENIED_USER_ID) in bot.last_text
    assert "not authorised" in bot.last_text


async def test_the_refusal_leaks_nothing_about_the_deployment(
    settings: Settings, bot: FakeBot
) -> None:
    context = make_context(settings, bot)

    with pytest.raises(ApplicationHandlerStop):
        await enforce_allow_list(make_update(bot, user_id=DENIED_USER_ID), as_context(context))

    text = bot.last_text.lower()
    assert "notediscovery" not in text
    assert str(settings.notediscovery_url).lower() not in text
    assert str(ALLOWED_USER_ID) not in text


async def test_a_stranger_is_answered_once_not_on_every_message(
    settings: Settings, bot: FakeBot
) -> None:
    """Otherwise an unknown account can keep the bot replying to it forever."""
    context = make_context(settings, bot)

    for _ in range(5):
        with pytest.raises(ApplicationHandlerStop):
            await enforce_allow_list(make_update(bot, user_id=DENIED_USER_ID), as_context(context))

    assert len(bot.sent) == 1


async def test_an_allow_listed_user_in_a_non_listed_chat_is_stopped(
    settings: Settings, bot: FakeBot
) -> None:
    """Chat allow-listing is what keeps the bot out of groups it was added to."""
    restricted = settings.model_copy(update={"telegram_allowed_chat_ids": [-100123]})
    context = make_context(restricted, bot)

    with pytest.raises(ApplicationHandlerStop):
        await enforce_allow_list(make_update(bot), as_context(context))


async def test_accepted_and_rejected_updates_are_counted(settings: Settings, bot: FakeBot) -> None:
    """`/status` reports these, so they have to be real."""
    context = make_context(settings, bot)
    deps = context.bot_data[DEPS_KEY]

    await enforce_allow_list(make_update(bot), as_context(context))
    with pytest.raises(ApplicationHandlerStop):
        await enforce_allow_list(make_update(bot, user_id=DENIED_USER_ID), as_context(context))

    assert deps.counters == {"updates_accepted": 1, "updates_rejected": 1}


async def test_a_correlation_id_is_bound_for_every_accepted_update(
    settings: Settings, bot: FakeBot
) -> None:
    """One id per user action, followable through NoteDiscovery and the LLM calls."""
    from discoverygram.util.correlation import clear_correlation_id, get_correlation_id

    clear_correlation_id()
    context = make_context(settings, bot)

    await enforce_allow_list(make_update(bot), as_context(context))

    assert get_correlation_id()
    clear_correlation_id()
