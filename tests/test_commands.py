"""The core commands.

Every reply goes out as MarkdownV2, so each test also asserts the text would
survive the Bot API — an unescaped reserved character is a 400, not a typo.
"""

from __future__ import annotations

import re

import pytest

from discoverygram.adapters.session import MemorySessionStore
from discoverygram.app.probe import InstanceState
from discoverygram.bot.commands import (
    COMMAND_MENU,
    cancel,
    help_command,
    start,
    status,
    unknown_command,
    whoami,
)
from discoverygram.bot.deps import DEPS_KEY, BotDeps
from discoverygram.bot.tokens import CallbackTokens
from discoverygram.config import Settings
from discoverygram.ports.errors import Unavailable
from discoverygram.ports.model import InstanceConfig, VaultStats
from tests.fixtures.telegram import ALLOWED_USER_ID, FakeBot, FakeContext, as_context, make_update

RESERVED = set("_*[]()~`>#+-=|{}.!")


class StubNoteStore:
    def __init__(self, *, healthy: bool = True, stats: VaultStats | None = None) -> None:
        self._healthy = healthy
        self._stats = stats

    async def health(self) -> bool:
        return self._healthy

    async def get_stats(self) -> VaultStats:
        if self._stats is None:
            raise Unavailable("no stats")
        return self._stats


def assert_markdown_v2_safe(text: str) -> None:
    """Every reserved character must be escaped or inside a code span."""
    without_code = re.sub(r"`[^`]*`", "", text)
    index = 0
    while index < len(without_code):
        char = without_code[index]
        if char == "\\":
            index += 2
            continue
        if char in RESERVED and char != "*":
            raise AssertionError(f"unescaped {char!r} at {index} in {without_code!r}")
        index += 1


@pytest.fixture
def bot() -> FakeBot:
    return FakeBot()


def make_context(
    settings: Settings,
    bot: FakeBot,
    *,
    notes: StubNoteStore | None = None,
    instance: InstanceState | None = None,
) -> FakeContext:
    sessions = MemorySessionStore(default_ttl_s=3600)
    deps = BotDeps(
        settings=settings,
        notes=notes or StubNoteStore(),  # type: ignore[arg-type]
        sessions=sessions,
        tokens=CallbackTokens(sessions, ttl_s=3600),
        instance=instance or InstanceState(config=InstanceConfig(version="0.31.3"), healthy=True),
    )
    return FakeContext(bot, {DEPS_KEY: deps})


async def test_start_greets_and_points_at_help(settings: Settings, bot: FakeBot) -> None:
    await start(make_update(bot), as_context(make_context(settings, bot)))

    assert "/help" in bot.last_text
    assert_markdown_v2_safe(bot.last_text)


async def test_help_lists_only_commands_that_exist(settings: Settings, bot: FakeBot) -> None:
    """Promising a command that answers "unknown command" is worse than silence."""
    await help_command(make_update(bot), as_context(make_context(settings, bot)))

    text = bot.last_text
    for command in ("/help", "/status", "/whoami", "/cancel"):
        assert command in text
    assert "/search" not in text
    assert_markdown_v2_safe(text)


async def test_the_command_menu_matches_what_help_advertises() -> None:
    assert {command.command for command in COMMAND_MENU} == {
        "start",
        "help",
        "status",
        "whoami",
        "cancel",
    }


async def test_whoami_reports_the_ids_needed_for_the_allow_list(
    settings: Settings, bot: FakeBot
) -> None:
    await whoami(make_update(bot), as_context(make_context(settings, bot)))

    assert str(ALLOWED_USER_ID) in bot.last_text
    assert_markdown_v2_safe(bot.last_text)


async def test_cancel_clears_a_pending_flow(settings: Settings, bot: FakeBot) -> None:
    context = make_context(settings, bot)
    context.user_data["draft"] = {"path": "A.md"}

    await cancel(make_update(bot), as_context(context))

    assert context.user_data == {}
    assert "Cancelled" in bot.last_text


async def test_cancel_says_so_when_there_is_nothing_to_cancel(
    settings: Settings, bot: FakeBot
) -> None:
    context = make_context(settings, bot)

    await cancel(make_update(bot), as_context(context))

    assert "Nothing to cancel" in bot.last_text


async def test_status_reports_a_healthy_instance(settings: Settings, bot: FakeBot) -> None:
    notes = StubNoteStore(stats=VaultStats(notes_count=42, tags_count=7))
    context = make_context(settings, bot, notes=notes)

    await status(make_update(bot), as_context(context))

    text = bot.last_text
    assert "42 notes" in text
    assert "❌" not in text
    assert_markdown_v2_safe(text)


async def test_status_reports_an_unreachable_instance_honestly(
    settings: Settings, bot: FakeBot
) -> None:
    context = make_context(
        settings,
        bot,
        notes=StubNoteStore(healthy=False),
        instance=InstanceState(config=InstanceConfig(version="0.31.3"), healthy=False),
    )

    await status(make_update(bot), as_context(context))

    assert "❌" in bot.last_text
    assert "unreachable" in bot.last_text
    assert_markdown_v2_safe(bot.last_text)


async def test_status_survives_a_stats_call_that_fails(settings: Settings, bot: FakeBot) -> None:
    """A transport that cannot answer must degrade one line, not the command."""
    context = make_context(settings, bot, notes=StubNoteStore(stats=None))

    await status(make_update(bot), as_context(context))

    assert "Vault:" not in bot.last_text
    assert "DiscoveryGram status" in bot.last_text


async def test_status_explains_a_search_disabled_instance(settings: Settings, bot: FakeBot) -> None:
    context = make_context(
        settings,
        bot,
        instance=InstanceState(
            config=InstanceConfig(version="0.31.3", search_enabled=False), healthy=True
        ),
    )

    await status(make_update(bot), as_context(context))

    assert "Search is disabled" in bot.last_text


async def test_status_flags_a_contract_version_mismatch(settings: Settings, bot: FakeBot) -> None:
    context = make_context(
        settings, bot, instance=InstanceState(config=InstanceConfig(version="0.99"), healthy=True)
    )

    await status(make_update(bot), as_context(context))

    assert "⚠️" in bot.last_text


async def test_status_counts_updates(settings: Settings, bot: FakeBot) -> None:
    context = make_context(settings, bot)
    deps = context.bot_data[DEPS_KEY]
    deps.count("updates_accepted")
    deps.count("updates_rejected")

    await status(make_update(bot), as_context(context))

    assert "1 accepted, 1 rejected" in bot.last_text


async def test_an_unknown_command_gets_an_answer(settings: Settings, bot: FakeBot) -> None:
    await unknown_command(make_update(bot, text="/nope"), as_context(make_context(settings, bot)))

    assert "/help" in bot.last_text
    assert_markdown_v2_safe(bot.last_text)
