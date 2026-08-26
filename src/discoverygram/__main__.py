"""Entry point.

Owns the event loop, and therefore the startup and shutdown order:

    settings -> logging -> adapters -> health server -> instance probe
             -> Telegram application -> wait for a signal
             -> Telegram application -> health server -> adapters

The health server comes up **before** the probe, so an orchestrator polling
`/readyz` during a slow start sees an honest 503 rather than a refused
connection. Shutdown runs in reverse and is idempotent: a failure halfway
through startup still tears down what did come up.
"""

from __future__ import annotations

import asyncio
import signal
import sys
from typing import NoReturn

from pydantic import ValidationError
from telegram.error import InvalidToken, TelegramError

from discoverygram import __version__
from discoverygram.adapters import build_note_store
from discoverygram.adapters.session import build_session_store
from discoverygram.app import probe_instance
from discoverygram.bot.application import BotRunner, build_application, build_deps
from discoverygram.config import Settings
from discoverygram.health import HealthServer, ReadinessCheck
from discoverygram.llm.factory import build_router
from discoverygram.util.logging import configure_logging, get_logger

log = get_logger(__name__)


def load_settings() -> Settings:
    """Load settings, failing fast with a readable message."""
    try:
        return Settings()  # type: ignore[call-arg]  # values come from the environment
    except ValidationError as exc:
        print("Invalid configuration — check your .env file:\n", file=sys.stderr)
        for error in exc.errors():
            location = ".".join(str(part) for part in error["loc"]) or "(root)"
            print(f"  {location.upper()}: {error['msg']}", file=sys.stderr)
        raise SystemExit(2) from exc


def _install_signal_handlers(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)


async def run() -> None:
    settings = load_settings()
    configure_logging(
        level=settings.log_level,
        log_format=settings.log_format,
        # Scrubbed out of third-party records: python-telegram-bot logs the Bot
        # API URL, which carries the token, on every request it builds. Every
        # provider key is scrubbed for the same reason — httpx does not log
        # headers today, but nine third-party endpoints is nine chances for one
        # of them to end up in a traceback or a debug dump.
        secrets=(
            settings.telegram_bot_token,
            settings.notediscovery_api_key,
            *(config.api_key for config in settings.provider_configs().values()),
        ),
    )

    log.info(
        "starting",
        version=__version__,
        transport=settings.notediscovery_transport.value,
        telegram_mode=settings.telegram_mode.value,
        session_backend=settings.session_backend.value,
        allowed_users=len(settings.telegram_allowed_user_ids),
    )

    notes = build_note_store(settings)
    sessions = build_session_store(settings)
    # Built before the health server so its ladder is logged early: an operator
    # reading the startup log sees which (provider, model) rungs exist, and why
    # any configured provider was skipped, before anything else happens.
    llm = build_router(settings)

    health = HealthServer(port=settings.health_port, version=__version__)
    health.register_check("notediscovery", notes.health)
    health.register_check("sessions", sessions.ping)
    await health.start()

    # Not fatal when it fails: the health endpoint reports the degradation
    # honestly and the instance may come back without a restart.
    state = await probe_instance(notes)
    if not state.healthy:
        log.warning("notediscovery_not_reachable_at_startup", url=str(settings.notediscovery_url))

    deps = build_deps(settings, notes, sessions, state, llm)
    runner = BotRunner(build_application(deps), settings)

    stop = asyncio.Event()
    _install_signal_handlers(stop)

    try:
        await _start_telegram(runner)
        health.register_check("telegram", _telegram_check(runner))
        log.info("ready", mode=settings.telegram_mode.value)
        await stop.wait()
    finally:
        log.info("shutting_down")
        await runner.stop()
        await health.stop()
        await sessions.aclose()
        await llm.aclose()
        await notes.aclose()


async def _start_telegram(runner: BotRunner) -> None:
    """Start the bot, distinguishing a wrong token from a bad moment.

    An invalid token can never succeed, so restarting the container forever is
    the wrong answer — exit 2, the same code an invalid `.env` produces. Any
    other Telegram failure may well be transient, so exit 1 and let the restart
    policy do its job.
    """
    try:
        await runner.start()
    except InvalidToken as exc:
        log.error("telegram_token_rejected", error=str(exc))
        print(
            "Telegram rejected TELEGRAM_BOT_TOKEN. Check the value BotFather gave you.",
            file=sys.stderr,
        )
        raise SystemExit(2) from exc
    except TelegramError as exc:
        log.error("telegram_start_failed", error=str(exc))
        raise SystemExit(1) from exc


def _telegram_check(runner: BotRunner) -> ReadinessCheck:
    """Readiness check reporting whether the updater is still receiving."""

    async def check() -> bool:
        updater = runner.application.updater
        return runner.is_running and updater is not None and updater.running

    return check


def main() -> NoReturn:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    except SystemExit as exc:
        # asyncio.run re-raises whatever escaped the coroutine; the exit codes
        # chosen above have to survive that.
        raise SystemExit(exc.code) from None
    raise SystemExit(0)


if __name__ == "__main__":
    main()
