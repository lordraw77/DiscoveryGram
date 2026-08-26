"""Entry point.

Configuration, logging, the health server and the NoteDiscovery adapter are
wired here, and the instance is probed once at startup. The Telegram application
itself arrives in phase 2.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import sys
from typing import NoReturn

from pydantic import ValidationError

from discoverygram import __version__
from discoverygram.adapters import build_note_store
from discoverygram.app import probe_instance
from discoverygram.config import Settings
from discoverygram.health import HealthServer
from discoverygram.ports import NoteStore
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


async def run() -> None:
    settings = load_settings()
    configure_logging(level=settings.log_level, log_format=settings.log_format)

    log.info(
        "starting",
        version=__version__,
        transport=settings.notediscovery_transport.value,
        telegram_mode=settings.telegram_mode.value,
        session_backend=settings.session_backend.value,
        allowed_users=len(settings.telegram_allowed_user_ids),
    )

    store: NoteStore = build_note_store(settings)

    health = HealthServer(port=settings.health_port, version=__version__)
    health.register_check("notediscovery", store.health)
    await health.start()

    # Not fatal when it fails: the health endpoint reports the degradation
    # honestly and the instance may come back without a restart.
    state = await probe_instance(store)
    if not state.healthy:
        log.warning("notediscovery_not_reachable_at_startup", url=str(settings.notediscovery_url))

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    log.info("ready")
    await stop.wait()

    log.info("shutting_down")
    await health.stop()
    await store.aclose()


def main() -> NoReturn:
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run())
    raise SystemExit(0)


if __name__ == "__main__":
    main()
