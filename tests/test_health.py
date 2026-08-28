"""Health server behaviour."""

from __future__ import annotations

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from discoverygram.health import HealthServer


def _build_app(server: HealthServer) -> web.Application:
    app = web.Application()
    app.router.add_get("/healthz", server._handle_healthz)
    app.router.add_get("/readyz", server._handle_readyz)
    app.router.add_get("/metrics", server._handle_metrics)
    return app


async def _client(server: HealthServer) -> TestClient[web.Request, web.Application]:
    client: TestClient[web.Request, web.Application] = TestClient(TestServer(_build_app(server)))
    await client.start_server()
    return client


async def test_healthz_is_always_ok() -> None:
    client = await _client(HealthServer(port=0, version="0.1.0", cache_ttl_s=0.0))
    try:
        response = await client.get("/healthz")

        assert response.status == 200
        assert (await response.json())["status"] == "ok"
    finally:
        await client.close()


async def test_readyz_passes_when_all_checks_pass() -> None:
    server = HealthServer(port=0, version="0.1.0", cache_ttl_s=0.0)

    async def ok() -> bool:
        return True

    server.register_check("notediscovery", ok)
    client = await _client(server)
    try:
        response = await client.get("/readyz")
        payload = await response.json()

        assert response.status == 200
        assert payload["status"] == "ready"
        assert payload["checks"] == {"notediscovery": "ok"}
    finally:
        await client.close()


async def test_readyz_reports_503_on_failure() -> None:
    server = HealthServer(port=0, version="0.1.0", cache_ttl_s=0.0)

    async def ok() -> bool:
        return True

    async def down() -> bool:
        return False

    server.register_check("notediscovery", down)
    server.register_check("llm", ok)
    client = await _client(server)
    try:
        response = await client.get("/readyz")
        payload = await response.json()

        assert response.status == 503
        assert payload["status"] == "not_ready"
        assert payload["checks"]["notediscovery"] == "failed"
        assert payload["checks"]["llm"] == "ok"
    finally:
        await client.close()


async def test_a_raising_check_counts_as_failed() -> None:
    """A check that explodes must degrade readiness, not crash the endpoint."""
    server = HealthServer(port=0, version="0.1.0", cache_ttl_s=0.0)

    async def boom() -> bool:
        raise RuntimeError("connection reset")

    server.register_check("notediscovery", boom)
    client = await _client(server)
    try:
        response = await client.get("/readyz")

        assert response.status == 503
        assert (await response.json())["checks"]["notediscovery"] == "failed"
    finally:
        await client.close()


async def test_start_and_stop_bind_a_real_port() -> None:
    """The server must actually bind and release its socket."""
    server = HealthServer(port=0, version="0.1.0", cache_ttl_s=0.0)

    await server.start()
    try:
        assert server._runner is not None
    finally:
        await server.stop()

    # Stopping twice must be safe: shutdown paths can run more than once.
    await server.stop()


# --- Phase 7: degradation, caching and metrics ---------------------------


async def test_an_optional_check_is_reported_without_failing_readiness() -> None:
    """Every AI provider being down must not pull the bot out of service."""
    server = HealthServer(port=0, version="0.1.0", cache_ttl_s=0.0)

    async def ok() -> bool:
        return True

    async def down() -> bool:
        return False

    server.register_check("notediscovery", ok)
    server.register_check("llm", down, required=False)
    client = await _client(server)
    try:
        response = await client.get("/readyz")
        payload = await response.json()

        assert response.status == 200
        assert payload["status"] == "ready"
        assert payload["checks"]["llm"] == "degraded"
    finally:
        await client.close()


async def test_a_verdict_is_reused_for_a_beat() -> None:
    """A probe every second must not become load on the instance it is checking."""
    calls = 0
    clock = 0.0

    async def counting() -> bool:
        nonlocal calls
        calls += 1
        return True

    server = HealthServer(port=0, version="0.1.0", cache_ttl_s=2.0, clock=lambda: clock)
    server.register_check("notediscovery", counting)

    await server._evaluate()
    await server._evaluate()
    assert calls == 1

    clock = 3.0
    await server._evaluate()
    assert calls == 2


async def test_readyz_names_the_version_it_is_answering_for() -> None:
    server = HealthServer(port=0, version="9.9.9", cache_ttl_s=0.0)
    client = await _client(server)
    try:
        payload = await (await client.get("/readyz")).json()

        assert payload["version"] == "9.9.9"
    finally:
        await client.close()


async def test_metrics_are_404_when_disabled() -> None:
    """Off means off: an endpoint that answers is an endpoint that can be scraped."""
    client = await _client(HealthServer(port=0, version="0.1.0"))
    try:
        response = await client.get("/metrics")

        assert response.status == 404
    finally:
        await client.close()


async def test_metrics_render_when_enabled() -> None:
    from discoverygram.util import metrics

    metrics.UPDATES.inc(outcome="accepted")
    client = await _client(HealthServer(port=0, version="0.1.0", metrics_enabled=True))
    try:
        response = await client.get("/metrics")
        body = await response.text()

        assert response.status == 200
        assert response.content_type == "text/plain"
        assert "# TYPE discoverygram_updates_total counter" in body
        assert 'discoverygram_updates_total{outcome="accepted"}' in body
    finally:
        await client.close()
