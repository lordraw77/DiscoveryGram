"""Entry point: settings loading.

Settings loading, plus the startup and shutdown ordering the whole service
depends on. The NoteDiscovery readiness probe itself lives in
`RestNoteStore.health` and is covered by `tests/test_rest_note_store.py`.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, ClassVar

import pytest
from telegram.error import InvalidToken, TelegramError

import discoverygram.__main__ as main_module
from discoverygram.__main__ import load_settings
from discoverygram.config import Settings
from discoverygram.ports.model import InstanceConfig


def test_load_settings_returns_settings(env: None) -> None:
    assert isinstance(load_settings(), Settings)


def test_load_settings_exits_with_code_2_on_bad_config(
    env: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A misconfigured .env must fail fast with a readable message, not a traceback."""
    monkeypatch.delenv("NOTEDISCOVERY_URL", raising=False)

    with pytest.raises(SystemExit) as exit_info:
        load_settings()

    assert exit_info.value.code == 2
    stderr = capsys.readouterr().err
    assert "Invalid configuration" in stderr
    assert "NOTEDISCOVERY_URL" in stderr


# --- Startup and shutdown wiring ------------------------------------------


class RecordingRunner:
    """Stands in for the BotRunner: records the lifecycle without a network."""

    instances: ClassVar[list[RecordingRunner]] = []

    def __init__(self, application: object, settings: Settings) -> None:
        self.application = application
        self.settings = settings
        self.events: list[str] = []
        self.fail_with: BaseException | None = None
        self.is_running = False
        RecordingRunner.instances.append(self)

    async def start(self) -> None:
        if self.fail_with is not None:
            raise self.fail_with
        self.events.append("start")
        self.is_running = True

    async def stop(self) -> None:
        self.events.append("stop")
        self.is_running = False


class RecordingStore:
    def __init__(self) -> None:
        self.closed = False

    async def health(self) -> bool:
        return True

    async def get_config(self) -> InstanceConfig:
        return InstanceConfig(version="0.31.3")

    async def aclose(self) -> None:
        self.closed = True


class RecordingSessions:
    def __init__(self) -> None:
        self.closed = False

    async def ping(self) -> bool:
        return True

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace everything that would touch the network or bind a port."""
    RecordingRunner.instances.clear()
    notes = RecordingStore()
    sessions = RecordingSessions()
    health = SimpleNamespace(
        checks={},
        started=False,
        stopped=False,
    )

    class FakeHealthServer:
        def __init__(self, port: int, version: str) -> None:
            self.port = port

        def register_check(self, name: str, check: Any) -> None:
            health.checks[name] = check

        async def start(self) -> None:
            health.started = True

        async def stop(self) -> None:
            health.stopped = True

    monkeypatch.setattr(main_module, "build_note_store", lambda _s: notes)
    monkeypatch.setattr(main_module, "build_session_store", lambda _s: sessions)
    monkeypatch.setattr(main_module, "HealthServer", FakeHealthServer)
    monkeypatch.setattr(main_module, "build_application", lambda _deps: object())
    monkeypatch.setattr(main_module, "BotRunner", RecordingRunner)
    # Stop immediately: run() otherwise waits for a signal forever.
    monkeypatch.setattr(main_module, "_install_signal_handlers", lambda stop: stop.set())
    return {"notes": notes, "sessions": sessions, "health": health}


async def test_startup_brings_up_health_before_probing_the_instance(
    env: None, wired: dict[str, Any]
) -> None:
    """An orchestrator polling /readyz during a slow start deserves a 503, not a refusal."""
    await main_module.run()

    assert wired["health"].started is True
    assert set(wired["health"].checks) == {"notediscovery", "sessions", "telegram"}


async def test_shutdown_releases_everything_it_acquired(env: None, wired: dict[str, Any]) -> None:
    await main_module.run()

    assert RecordingRunner.instances[0].events == ["start", "stop"]
    assert wired["health"].stopped is True
    assert wired["sessions"].closed is True
    assert wired["notes"].closed is True


async def test_a_rejected_token_exits_2_rather_than_restarting_forever(
    env: None, wired: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """No number of restarts fixes a wrong token, so it is a configuration error."""
    original = RecordingRunner.__init__

    def failing_init(self: RecordingRunner, application: object, settings: Settings) -> None:
        original(self, application, settings)
        self.fail_with = InvalidToken("Not Found")

    monkeypatch.setattr(RecordingRunner, "__init__", failing_init)

    with pytest.raises(SystemExit) as exit_info:
        await main_module.run()

    assert exit_info.value.code == 2


async def test_a_transient_telegram_failure_exits_1_so_a_restart_can_help(
    env: None, wired: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    original = RecordingRunner.__init__

    def failing_init(self: RecordingRunner, application: object, settings: Settings) -> None:
        original(self, application, settings)
        self.fail_with = TelegramError("Bad Gateway")

    monkeypatch.setattr(RecordingRunner, "__init__", failing_init)

    with pytest.raises(SystemExit) as exit_info:
        await main_module.run()

    assert exit_info.value.code == 1


async def test_a_failed_start_still_tears_down_what_came_up(
    env: None, wired: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    original = RecordingRunner.__init__

    def failing_init(self: RecordingRunner, application: object, settings: Settings) -> None:
        original(self, application, settings)
        self.fail_with = TelegramError("Bad Gateway")

    monkeypatch.setattr(RecordingRunner, "__init__", failing_init)

    with pytest.raises(SystemExit):
        await main_module.run()

    assert wired["health"].stopped is True
    assert wired["sessions"].closed is True
    assert wired["notes"].closed is True
