"""Application wiring and lifecycle.

No network: the application is built with a fake token and never initialised
against Telegram, so what is asserted here is the wiring — handler groups,
registered commands, update filtering — and the start/stop ordering.
"""

from __future__ import annotations

import pytest
from telegram import Update
from telegram.ext import CallbackQueryHandler, CommandHandler, MessageHandler, TypeHandler

from discoverygram.adapters.session import MemorySessionStore
from discoverygram.app.probe import InstanceState
from discoverygram.bot.application import (
    ALLOWED_UPDATES,
    BotRunner,
    build_application,
    build_deps,
)
from discoverygram.bot.browse import (
    ACT_ACTION,
    NAV_ACTION,
    NOTE_ACTION,
    pending_input,
)
from discoverygram.bot.commands import COMMANDS, unknown_command
from discoverygram.bot.deps import DEPS_KEY, BotDeps, deps_of
from discoverygram.bot.guard import enforce_allow_list
from discoverygram.bot.search import PAGE_ACTION, text_message
from discoverygram.config import Settings, TelegramMode
from discoverygram.ports.model import InstanceConfig
from tests.fixtures.telegram import FakeBot, FakeContext


@pytest.fixture
def deps(settings: Settings) -> BotDeps:
    sessions = MemorySessionStore(default_ttl_s=3600)
    return build_deps(
        settings,
        notes=object(),  # type: ignore[arg-type]
        sessions=sessions,
        instance=InstanceState(config=InstanceConfig(), healthy=True),
    )


def test_the_allow_list_runs_before_every_other_handler(deps: BotDeps) -> None:
    """Group -1 is what makes the check impossible to forget in a new handler."""
    application = build_application(deps)

    guard_group = min(application.handlers)
    guards = application.handlers[guard_group]

    assert guard_group == -1
    assert len(guards) == 1
    assert isinstance(guards[0], TypeHandler)
    assert guards[0].callback is enforce_allow_list


def test_every_core_command_is_registered(deps: BotDeps) -> None:
    application = build_application(deps)

    registered = {
        next(iter(handler.commands))
        for handler in application.handlers[0]
        if isinstance(handler, CommandHandler) and handler.commands
    }

    assert set(COMMANDS) <= registered


def test_the_inert_page_counter_has_a_handler(deps: BotDeps) -> None:
    """Telegram spins until a callback query is answered, even a no-op one."""
    application = build_application(deps)

    patterns = [
        str(getattr(handler.pattern, "pattern", handler.pattern))
        for handler in application.handlers[0]
        if isinstance(handler, CallbackQueryHandler) and handler.pattern
    ]

    assert any("noop" in pattern for pattern in patterns)


def test_an_error_handler_is_installed(deps: BotDeps) -> None:
    application = build_application(deps)

    assert application.error_handlers


def test_the_dependencies_are_reachable_from_a_handler(deps: BotDeps) -> None:
    application = build_application(deps)
    context = FakeContext(FakeBot(), application.bot_data)

    assert deps_of(context) is deps


def test_missing_dependencies_fail_loudly(settings: Settings) -> None:
    """A handler running without them is a wiring bug, not a runtime condition."""
    context = FakeContext(FakeBot(), {})

    with pytest.raises(RuntimeError, match="BotDeps"):
        deps_of(context)


def test_only_the_update_types_the_bot_uses_are_requested(deps: BotDeps) -> None:
    """Not receiving polls and chat-member churn is cheaper than discarding it."""
    assert set(ALLOWED_UPDATES) == {
        Update.MESSAGE,
        Update.EDITED_MESSAGE,
        Update.CALLBACK_QUERY,
    }


def test_the_bot_data_key_is_namespaced(deps: BotDeps) -> None:
    application = build_application(deps)

    assert DEPS_KEY in application.bot_data
    assert DEPS_KEY.startswith("discoverygram")


class FakeUpdater:
    def __init__(self) -> None:
        self.running = False
        self.polling_kwargs: dict[str, object] | None = None
        self.webhook_kwargs: dict[str, object] | None = None
        self.stopped = False

    async def start_polling(self, **kwargs: object) -> None:
        self.polling_kwargs = kwargs
        self.running = True

    async def start_webhook(self, **kwargs: object) -> None:
        self.webhook_kwargs = kwargs
        self.running = True

    async def stop(self) -> None:
        self.running = False
        self.stopped = True


class FakeApplication:
    def __init__(self) -> None:
        self.updater = FakeUpdater()
        self.events: list[str] = []
        self.running = False

    async def initialize(self) -> None:
        self.events.append("initialize")

    async def start(self) -> None:
        self.events.append("start")
        self.running = True

    async def stop(self) -> None:
        self.events.append("stop")
        self.running = False

    async def shutdown(self) -> None:
        self.events.append("shutdown")


async def test_polling_start_and_stop_run_in_the_right_order(settings: Settings) -> None:
    application = FakeApplication()
    runner = BotRunner(application, settings)  # type: ignore[arg-type]

    await runner.start()
    assert runner.is_running
    assert application.events == ["initialize", "start"]
    assert application.updater.polling_kwargs is not None

    await runner.stop()

    assert application.events == ["initialize", "start", "stop", "shutdown"]
    assert application.updater.stopped
    assert not runner.is_running


async def test_pending_updates_are_dropped_on_start(settings: Settings) -> None:
    """A queued update from an hour ago acts on intent the user never repeated."""
    application = FakeApplication()

    await BotRunner(application, settings).start()  # type: ignore[arg-type]

    assert application.updater.polling_kwargs is not None
    assert application.updater.polling_kwargs["drop_pending_updates"] is True


async def test_webhook_mode_registers_the_public_url_and_secret(
    settings: Settings,
) -> None:
    webhook = settings.model_copy(
        update={
            "telegram_mode": TelegramMode.WEBHOOK,
            "telegram_webhook_url": "https://bot.example.com/",
            "telegram_webhook_secret": "s3cret",
            "telegram_webhook_path": "telegram",
        }
    )
    application = FakeApplication()

    await BotRunner(application, webhook).start()  # type: ignore[arg-type]

    kwargs = application.updater.webhook_kwargs
    assert kwargs is not None
    assert kwargs["webhook_url"] == "https://bot.example.com/telegram"
    assert kwargs["url_path"] == "telegram"
    assert kwargs["secret_token"] == "s3cret"


async def test_no_webhook_secret_sends_none_rather_than_an_empty_string(
    settings: Settings,
) -> None:
    """An empty secret would be registered as a secret that never matches."""
    webhook = settings.model_copy(
        update={
            "telegram_mode": TelegramMode.WEBHOOK,
            "telegram_webhook_url": "https://bot.example.com",
            "telegram_webhook_secret": "",
        }
    )
    application = FakeApplication()

    await BotRunner(application, webhook).start()  # type: ignore[arg-type]

    assert application.updater.webhook_kwargs is not None
    assert application.updater.webhook_kwargs["secret_token"] is None


async def test_stopping_a_runner_that_never_started_is_a_no_op(
    settings: Settings,
) -> None:
    application = FakeApplication()

    await BotRunner(application, settings).stop()  # type: ignore[arg-type]

    assert application.events == []


def test_webhook_mode_requires_a_public_url(env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_MODE", "webhook")

    with pytest.raises(ValueError, match="TELEGRAM_WEBHOOK_URL"):
        Settings()  # type: ignore[call-arg]


def test_the_webhook_and_health_ports_cannot_collide(
    env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both bind inside the same container; sharing a port fails at bind time."""
    monkeypatch.setenv("TELEGRAM_MODE", "webhook")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_URL", "https://bot.example.com")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_PORT", "8080")
    monkeypatch.setenv("HEALTH_PORT", "8080")

    with pytest.raises(ValueError, match="same port"):
        Settings()  # type: ignore[call-arg]


# --- Search wiring --------------------------------------------------------


def test_the_search_commands_are_registered(deps: BotDeps) -> None:
    application = build_application(deps)

    registered = {
        next(iter(handler.commands))
        for handler in application.handlers[0]
        if isinstance(handler, CommandHandler) and handler.commands
    }

    assert {"search", "find", "tag", "recent"} <= registered


def test_the_pagination_callback_is_wired(deps: BotDeps) -> None:
    application = build_application(deps)

    patterns = [
        str(getattr(handler.pattern, "pattern", handler.pattern))
        for handler in application.handlers[0]
        if isinstance(handler, CallbackQueryHandler) and handler.pattern
    ]

    assert any(PAGE_ACTION in pattern for pattern in patterns)


def test_commands_are_offered_before_the_catch_all_text_handler(deps: BotDeps) -> None:
    """Otherwise every `/whatever` would be swallowed as a search query."""
    application = build_application(deps)
    message_handlers = [
        handler for handler in application.handlers[0] if isinstance(handler, MessageHandler)
    ]

    callbacks = [handler.callback for handler in message_handlers]

    assert callbacks.index(unknown_command) < callbacks.index(text_message)


# --- Navigation wiring ----------------------------------------------------


def test_the_navigation_commands_are_registered(deps: BotDeps) -> None:
    application = build_application(deps)

    registered = {
        next(iter(handler.commands))
        for handler in application.handlers[0]
        if isinstance(handler, CommandHandler) and handler.commands
    }

    assert {"browse", "open", "backlinks", "related", "move", "folder"} <= registered


def test_every_callback_action_has_exactly_one_handler(deps: BotDeps) -> None:
    """Two handlers matching one prefix would run both on a single tap."""
    application = build_application(deps)
    patterns = [
        str(getattr(handler.pattern, "pattern", handler.pattern))
        for handler in application.handlers[0]
        if isinstance(handler, CallbackQueryHandler) and handler.pattern
    ]

    for action in (NAV_ACTION, NOTE_ACTION, ACT_ACTION, PAGE_ACTION, "noop"):
        matching = [pattern for pattern in patterns if pattern == f"^{action}:"]
        assert len(matching) == 1, action


def test_a_pending_edit_claims_the_message_before_the_search_handler(
    deps: BotDeps,
) -> None:
    """Otherwise text meant as a note body would also be run as a query."""
    application = build_application(deps)
    callbacks = [
        handler.callback
        for handler in application.handlers[0]
        if isinstance(handler, MessageHandler)
    ]

    assert callbacks.index(pending_input) < callbacks.index(text_message)


def test_the_callback_actions_do_not_shadow_each_other() -> None:
    """`note:` must not also match `noop:` or the pagination action."""
    actions = [NAV_ACTION, NOTE_ACTION, ACT_ACTION, PAGE_ACTION, "noop"]

    for action in actions:
        others = [other for other in actions if other != action]
        assert not any(f"{action}:".startswith(f"{other}:") for other in others), action
