"""The core commands.

Every reply goes out as MarkdownV2, so each test also asserts the text would
survive the Bot API — an unescaped reserved character is a 400, not a typo.
"""

from __future__ import annotations

import pytest

from discoverygram.adapters.session import MemorySessionStore
from discoverygram.app.probe import InstanceState
from discoverygram.bot.browse import COMMANDS as BROWSE_COMMANDS
from discoverygram.bot.commands import (
    COMMAND_MENU,
    cancel,
    help_command,
    start,
    status,
    unknown_command,
    whoami,
)
from discoverygram.bot.commands import (
    COMMANDS as CORE_COMMANDS,
)
from discoverygram.bot.deps import DEPS_KEY, BotDeps
from discoverygram.bot.search import COMMANDS as SEARCH_COMMANDS
from discoverygram.bot.tokens import CallbackTokens
from discoverygram.config import Settings
from discoverygram.llm.breaker import CircuitBreaker
from discoverygram.llm.plan import Attempt, TaskProfile
from discoverygram.llm.router import LlmRouter, TaskLadder
from discoverygram.llm.usage import DailyCallCap, UsageLedger
from discoverygram.ports.errors import Unavailable
from discoverygram.ports.model import InstanceConfig, VaultStats
from tests.fixtures.telegram import (
    ALLOWED_USER_ID,
    FakeBot,
    FakeContext,
    as_context,
    assert_markdown_v2_safe,
    make_update,
)

# Every command the bot actually answers, from the two registries themselves —
# so a new command cannot be advertised without also being handled.
ALL_COMMANDS = {**CORE_COMMANDS, **SEARCH_COMMANDS, **BROWSE_COMMANDS}


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


@pytest.fixture
def bot() -> FakeBot:
    return FakeBot()


def make_context(
    settings: Settings,
    bot: FakeBot,
    *,
    notes: StubNoteStore | None = None,
    instance: InstanceState | None = None,
    llm: LlmRouter | None = None,
) -> FakeContext:
    sessions = MemorySessionStore(default_ttl_s=3600)
    deps = BotDeps(
        settings=settings,
        notes=notes or StubNoteStore(),  # type: ignore[arg-type]
        sessions=sessions,
        tokens=CallbackTokens(sessions, ttl_s=3600),
        instance=instance or InstanceState(config=InstanceConfig(version="0.31.3"), healthy=True),
        llm=llm,
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
    for command in ALL_COMMANDS:
        if command == "start":
            continue  # Telegram sends /start itself; the help text need not.
        assert f"/{command}" in text, command
    for unimplemented in ("/new", "/quick", "/summarize", "/ask"):
        assert unimplemented not in text
    assert_markdown_v2_safe(text)


async def test_every_advertised_command_has_a_handler() -> None:
    """The BotFather menu is a promise; an entry with no handler breaks it."""
    advertised = {command.command for command in COMMAND_MENU}

    assert advertised <= set(ALL_COMMANDS)


async def test_the_search_commands_are_advertised() -> None:
    advertised = {command.command for command in COMMAND_MENU}

    assert {"search", "find", "tag", "recent"} <= advertised


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


# --- The AI section of /status ------------------------------------------


def make_router(
    settings: Settings,
    *,
    chat: list[tuple[str, str]],
    vision: list[tuple[str, str]] | None = None,
    breaker: CircuitBreaker | None = None,
    cap: DailyCallCap | None = None,
) -> LlmRouter:
    def ladder(task: TaskProfile, rungs: list[tuple[str, str]]) -> TaskLadder:
        return TaskLadder(task=task, attempts=tuple(Attempt(provider=p, model=m) for p, m in rungs))

    return LlmRouter(
        settings,
        {},
        {
            TaskProfile.CHAT: ladder(TaskProfile.CHAT, chat),
            TaskProfile.VISION: ladder(TaskProfile.VISION, vision or []),
        },
        breaker=breaker,
        ledger=UsageLedger(),
        cap=cap,
    )


async def test_status_names_the_rung_that_would_serve_the_next_request(
    settings: Settings, bot: FakeBot
) -> None:
    router = make_router(
        settings,
        chat=[("groq", "llama-3.3-70b"), ("ollama", "llama3")],
        vision=[("gemini", "gemini-2.0-flash")],
    )

    await status(make_update(bot), as_context(make_context(settings, bot, llm=router)))

    text = bot.last_text
    assert "groq/llama\\-3\\.3\\-70b" in text
    assert "1 more" in text
    assert "gemini/gemini\\-2\\.0\\-flash" in text
    assert_markdown_v2_safe(text)


async def test_status_says_when_a_task_has_no_model_at_all(
    settings: Settings, bot: FakeBot
) -> None:
    router = make_router(settings, chat=[("ollama", "llama3")], vision=[])

    await status(make_update(bot), as_context(make_context(settings, bot, llm=router)))

    assert "none configured" in bot.last_text
    assert_markdown_v2_safe(bot.last_text)


async def test_status_names_a_tripped_provider_and_its_cool_down(
    settings: Settings, bot: FakeBot
) -> None:
    """A tripped breaker is otherwise invisible: the bot merely seems slow."""
    breaker = CircuitBreaker(failure_threshold=1, reset_s=120.0)
    breaker.record_failure("groq", reason="LlmAuthError", immediate=True)
    router = make_router(settings, chat=[("groq", "a"), ("ollama", "b")], breaker=breaker)

    await status(make_update(bot), as_context(make_context(settings, bot, llm=router)))

    text = bot.last_text
    assert "groq: circuit open" in text
    assert "retrying in" in text
    assert_markdown_v2_safe(text)


async def test_status_reports_the_callers_remaining_quota(settings: Settings, bot: FakeBot) -> None:
    cap = DailyCallCap(10)
    for _ in range(4):
        cap.consume(ALLOWED_USER_ID)
    router = make_router(settings, chat=[("ollama", "a")], cap=cap)

    await status(make_update(bot), as_context(make_context(settings, bot, llm=router)))

    assert "6 of 10 left" in bot.last_text
    assert_markdown_v2_safe(bot.last_text)


async def test_status_hides_the_quota_line_when_the_cap_is_disabled(
    settings: Settings, bot: FakeBot
) -> None:
    router = make_router(settings, chat=[("ollama", "a")], cap=DailyCallCap(0))

    await status(make_update(bot), as_context(make_context(settings, bot, llm=router)))

    assert "daily quota" not in bot.last_text


async def test_status_says_ai_is_not_configured_when_there_is_no_router(
    settings: Settings, bot: FakeBot
) -> None:
    """Milestone M1 — a bot with no LLM — must still produce a sendable /status."""
    await status(make_update(bot), as_context(make_context(settings, bot, llm=None)))

    assert "Not configured" in bot.last_text
    assert_markdown_v2_safe(bot.last_text)
