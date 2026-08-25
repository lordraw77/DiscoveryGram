"""Health server behaviour."""

from __future__ import annotations

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from discoverygram.health import HealthServer


def _build_app(server: HealthServer) -> web.Application:
    app = web.Application()
    app.router.add_get("/healthz", server._handle_healthz)
    app.router.add_get("/readyz", server._handle_readyz)
    return app


async def _client(server: HealthServer) -> TestClient[web.Request, web.Application]:
    client: TestClient[web.Request, web.Application] = TestClient(TestServer(_build_app(server)))
    await client.start_server()
    return client


async def test_healthz_is_always_ok() -> None:
    client = await _client(HealthServer(port=0, version="0.1.0"))
    try:
        response = await client.get("/healthz")

        assert response.status == 200
        assert (await response.json())["status"] == "ok"
    finally:
        await client.close()


async def test_readyz_passes_when_all_checks_pass() -> None:
    server = HealthServer(port=0, version="0.1.0")

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
    server = HealthServer(port=0, version="0.1.0")

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
    server = HealthServer(port=0, version="0.1.0")

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
    server = HealthServer(port=0, version="0.1.0")

    await server.start()
    try:
        assert server._runner is not None
    finally:
        await server.stop()

    # Stopping twice must be safe: shutdown paths can run more than once.
    await server.stop()
