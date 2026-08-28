"""Health and metrics endpoints for container orchestration.

`/healthz` is liveness: the process is up and the event loop responds.
`/readyz` is readiness: every registered *required* dependency check passes.
`/metrics` is the Prometheus exposition, served only when `METRICS_ENABLED`.

Checks are registered by the components that own them, so this module stays
dependency-free. Three properties are worth stating, because each one is a
production failure that has been designed out:

* **Not every dependency is a readiness dependency.** A degraded LLM ladder is
  reported in the body but does not fail readiness: an orchestrator that pulls
  the bot out of service because a third-party model provider is having a bad
  afternoon has turned a partial outage into a total one. Search, browse and
  `/new` do not need a provider at all.
* **Readiness results are cached for a beat.** The NoteDiscovery check is a
  real HTTP call, and a liveness probe every second — or three replicas of a
  probe — must not become load on the very instance whose health is in
  question.
* **Checks run concurrently and can never hang the endpoint.** Each one is
  bounded by a timeout, because a check that blocks forever produces a probe
  that times out instead of a probe that answers 503, and those two look very
  different to an operator.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from aiohttp import web

from discoverygram.util import metrics
from discoverygram.util.logging import get_logger

log = get_logger(__name__)

ReadinessCheck = Callable[[], Awaitable[bool]]

# A dependency check that has not answered in this long is a failed check. It
# is deliberately shorter than a typical probe timeout so the endpoint answers
# rather than being cut off.
CHECK_TIMEOUT_S = 5.0
# How long a readiness verdict is reused. Long enough to absorb an aggressive
# probe, short enough that a recovery is visible within one probe interval.
CACHE_TTL_S = 2.0


@dataclass(frozen=True, slots=True)
class _Registered:
    check: ReadinessCheck
    required: bool


class HealthServer:
    """Small aiohttp server exposing liveness, readiness and metrics."""

    def __init__(
        self,
        port: int,
        version: str,
        *,
        metrics_enabled: bool = False,
        cache_ttl_s: float = CACHE_TTL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._port = port
        self._version = version
        self._metrics_enabled = metrics_enabled
        self._cache_ttl_s = cache_ttl_s
        self._clock = clock
        self._checks: dict[str, _Registered] = {}
        self._runner: web.AppRunner | None = None
        self._cached: tuple[float, dict[str, str], bool] | None = None

    def register_check(self, name: str, check: ReadinessCheck, *, required: bool = True) -> None:
        """Register a readiness check. Later registrations replace earlier ones.

        `required=False` reports the dependency without letting it fail
        readiness — the shape a degraded-but-usable subsystem needs.
        """
        self._checks[name] = _Registered(check=check, required=required)

    async def _handle_healthz(self, _request: web.Request) -> web.Response:
        return web.json_response({"status": "ok", "version": self._version})

    async def _handle_readyz(self, _request: web.Request) -> web.Response:
        results, ready = await self._evaluate()
        return web.json_response(
            {
                "status": "ready" if ready else "not_ready",
                "version": self._version,
                "checks": results,
            },
            status=200 if ready else 503,
        )

    async def _handle_metrics(self, _request: web.Request) -> web.Response:
        if not self._metrics_enabled:
            return web.Response(status=404, text="metrics are disabled\n")
        return web.Response(text=metrics.render(), content_type="text/plain", charset="utf-8")

    async def _evaluate(self) -> tuple[dict[str, str], bool]:
        cached = self._cached
        if cached is not None and (self._clock() - cached[0]) < self._cache_ttl_s:
            return dict(cached[1]), cached[2]

        names = list(self._checks)
        outcomes = await asyncio.gather(
            *(self._run(name, self._checks[name].check) for name in names)
        )

        results: dict[str, Any] = {}
        ready = True
        for name, passed in zip(names, outcomes, strict=True):
            registered = self._checks[name]
            if passed:
                results[name] = "ok"
                continue
            results[name] = "failed" if registered.required else "degraded"
            ready = ready and not registered.required

        self._cached = (self._clock(), dict(results), ready)
        return results, ready

    async def _run(self, name: str, check: ReadinessCheck) -> bool:
        """A check that raises, or never answers, is a check that failed."""
        try:
            async with asyncio.timeout(CHECK_TIMEOUT_S):
                return await check()
        except TimeoutError:
            log.warning("readiness_check_timeout", check=name, timeout_s=CHECK_TIMEOUT_S)
            return False
        except Exception as exc:
            log.warning("readiness_check_failed", check=name, error=str(exc))
            return False

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/healthz", self._handle_healthz)
        app.router.add_get("/readyz", self._handle_readyz)
        app.router.add_get("/metrics", self._handle_metrics)

        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, host="0.0.0.0", port=self._port)  # noqa: S104
        await site.start()
        log.info("health_server_started", port=self._port, metrics=self._metrics_enabled)

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
            log.info("health_server_stopped")
