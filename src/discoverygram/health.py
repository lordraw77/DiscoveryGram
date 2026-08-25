"""Health endpoints for container orchestration.

`/healthz` is liveness: the process is up and the event loop responds.
`/readyz` is readiness: every registered dependency check passes. Checks are
registered by the components that own them, so this module stays dependency-free.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiohttp import web

from discoverygram.util.logging import get_logger

log = get_logger(__name__)

ReadinessCheck = Callable[[], Awaitable[bool]]


class HealthServer:
    """Small aiohttp server exposing liveness and readiness."""

    def __init__(self, port: int, version: str) -> None:
        self._port = port
        self._version = version
        self._checks: dict[str, ReadinessCheck] = {}
        self._runner: web.AppRunner | None = None

    def register_check(self, name: str, check: ReadinessCheck) -> None:
        """Register a readiness check. Later registrations replace earlier ones."""
        self._checks[name] = check

    async def _handle_healthz(self, _request: web.Request) -> web.Response:
        return web.json_response({"status": "ok", "version": self._version})

    async def _handle_readyz(self, _request: web.Request) -> web.Response:
        results: dict[str, Any] = {}
        ready = True

        for name, check in self._checks.items():
            try:
                passed = await check()
            # A check that raises is a check that failed; readiness must not crash.
            except Exception as exc:
                log.warning("readiness_check_failed", check=name, error=str(exc))
                passed = False
            results[name] = "ok" if passed else "failed"
            ready = ready and passed

        return web.json_response(
            {"status": "ready" if ready else "not_ready", "checks": results},
            status=200 if ready else 503,
        )

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/healthz", self._handle_healthz)
        app.router.add_get("/readyz", self._handle_readyz)

        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, host="0.0.0.0", port=self._port)  # noqa: S104
        await site.start()
        log.info("health_server_started", port=self._port)

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
            log.info("health_server_stopped")
