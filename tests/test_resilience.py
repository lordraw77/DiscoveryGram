"""Fault injection: what the bot does when its dependencies misbehave.

Phase 7's Definition of Done is that the service survives every scenario below
without crashing or losing user state, and reports the degradation honestly.
Each test is one fault, injected at the seam where it would really arrive.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
import respx
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from discoverygram.adapters.rest import RestNoteStore
from discoverygram.adapters.session import RedisSessionStore
from discoverygram.adapters.throttle import RateLimiter
from discoverygram.bot.errors import UNAVAILABLE, handle_error, user_message
from discoverygram.config import Settings
from discoverygram.health import HealthServer
from discoverygram.llm.breaker import CircuitBreaker
from discoverygram.llm.plan import TaskProfile
from discoverygram.ports.errors import RateLimited, Unavailable
from discoverygram.ports.llm_errors import LlmDegraded, LlmUnavailable
from discoverygram.util import metrics
from tests.fixtures import notediscovery as fx
from tests.test_llm_router import PROMPT, FakeClient, build_router

BASE = "http://notediscovery.test:8000"


@dataclass
class Clock:
    now: float = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def no_throttle() -> RateLimiter:
    """The client-side limiter is not the subject of these tests."""
    return RateLimiter(limits={})


@pytest.fixture
def store(settings: Settings, no_throttle: RateLimiter) -> RestNoteStore:
    return RestNoteStore(
        settings.model_copy(update={"notediscovery_max_retries": 0}), limiter=no_throttle
    )


# --- NoteDiscovery is down ----------------------------------------------


@respx.mock
async def test_health_reports_false_rather_than_raising(store: RestNoteStore) -> None:
    """`/readyz` is polled by an orchestrator; a raising check is a probe timeout."""
    respx.get(f"{BASE}/health").mock(side_effect=httpx.ConnectError("refused"))

    assert await store.health() is False
    await store.aclose()


@respx.mock
async def test_a_read_against_a_dead_instance_becomes_one_sentence(
    store: RestNoteStore,
) -> None:
    respx.get(f"{BASE}/api/notes").mock(side_effect=httpx.ConnectError("refused"))

    with pytest.raises(Unavailable) as caught:
        await store.list_notes()

    assert user_message(caught.value) == UNAVAILABLE
    await store.aclose()


@respx.mock
async def test_an_outage_is_never_cached(store: RestNoteStore) -> None:
    """A vault that comes back must be usable without restarting the bot."""
    route = respx.get(f"{BASE}/api/notes").mock(
        side_effect=[
            httpx.ConnectError("refused"),
            httpx.Response(200, json=fx.NOTES_LISTING),
        ]
    )

    with pytest.raises(Unavailable):
        await store.get_tree()
    tree = await store.get_tree()

    assert route.call_count == 2
    assert tree.notes or tree.folders
    await store.aclose()


@respx.mock
async def test_a_flapping_instance_is_retried_and_served(
    settings: Settings, no_throttle: RateLimiter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("discoverygram.adapters.rest.random.uniform", lambda *_: 0.0)
    store = RestNoteStore(
        settings.model_copy(update={"notediscovery_max_retries": 3}), limiter=no_throttle
    )
    respx.get(f"{BASE}/api/tags").mock(
        side_effect=[
            httpx.Response(503),
            httpx.ReadTimeout("slow"),
            httpx.Response(200, json=fx.TAGS),
        ]
    )

    assert await store.list_tags() == {"planning": 2, "docker": 1}
    await store.aclose()


@respx.mock
async def test_being_rate_limited_is_reported_with_the_wait(store: RestNoteStore) -> None:
    respx.get(f"{BASE}/api/tags").mock(
        return_value=httpx.Response(
            429, headers={"Retry-After": "12"}, json={"detail": "slow down"}
        )
    )

    with pytest.raises(RateLimited) as caught:
        await store.list_tags()

    assert caught.value.retry_after == 12
    assert "12s" in user_message(caught.value)
    await store.aclose()


@respx.mock
async def test_a_write_that_fails_leaves_the_caches_alone(store: RestNoteStore) -> None:
    """An invalidation on a failed write would throw away a good tree for nothing."""
    respx.get(f"{BASE}/api/notes").mock(return_value=httpx.Response(200, json=fx.NOTES_LISTING))
    respx.post(f"{BASE}/api/notes/Projects/New.md").mock(return_value=httpx.Response(500))

    await store.get_tree()
    with pytest.raises(Unavailable):
        await store.create_note("Projects/New.md", "body")
    await store.get_tree()

    assert respx.calls.call_count == 2  # the listing, then the failed write
    await store.aclose()


# --- Sessions are down ---------------------------------------------------


async def test_a_dead_session_backend_reports_unready_rather_than_raising() -> None:
    """Redis down must degrade readiness, not kill the process."""

    class DeadRedis:
        async def ping(self) -> bool:
            raise ConnectionError("no route to host")

    store = RedisSessionStore("redis://nowhere:6379", default_ttl_s=60)
    store._client = DeadRedis()

    assert await store.ping() is False


# --- Every provider is failing ------------------------------------------


async def test_every_provider_failing_ends_in_one_error_not_a_crash(
    settings: Settings,
) -> None:
    groq, gemini = FakeClient("groq"), FakeClient("gemini")
    groq.program("a", LlmUnavailable("502"))
    gemini.program("b", LlmUnavailable("502"))
    router = build_router(
        settings.model_copy(update={"llm_retries_per_model": 1}),
        {"groq": groq, "gemini": gemini},
        [("groq", "a"), ("gemini", "b")],
    )

    with pytest.raises(Exception, match="Every configured chat model failed"):
        await router.complete(TaskProfile.CHAT, PROMPT, user_id=7)

    assert router.ledger.requests == 1
    assert router.ledger.successful_requests == 0


async def test_sustained_failure_becomes_back_pressure_then_recovers(
    settings: Settings,
) -> None:
    """The whole arc: fail, trip, refuse cheaply, cool down, serve again."""
    clock = Clock()
    breaker = CircuitBreaker(failure_threshold=1, reset_s=60.0, clock=clock)
    groq = FakeClient("groq")
    groq.program("a", LlmUnavailable("502"), "back")
    router = build_router(
        settings.model_copy(update={"llm_retries_per_model": 0}),
        {"groq": groq},
        [("groq", "a")],
        breaker=breaker,
    )

    with pytest.raises(Exception, match="Every configured chat model failed"):
        await router.complete(TaskProfile.CHAT, PROMPT, user_id=7)

    calls_after_failure = len(groq.calls)
    with pytest.raises(LlmDegraded):
        await router.complete(TaskProfile.CHAT, PROMPT, user_id=7)
    assert len(groq.calls) == calls_after_failure  # refused without calling anything

    clock.now = 61.0
    completion = await router.complete(TaskProfile.CHAT, PROMPT, user_id=7)

    assert completion.text == "back"


async def test_status_names_the_degraded_provider_and_the_wait(settings: Settings) -> None:
    """Degradation the operator can see, which is the other half of the DoD."""
    clock = Clock()
    breaker = CircuitBreaker(failure_threshold=1, reset_s=90.0, clock=clock)
    breaker.record_failure("groq", reason="LlmAuthError", immediate=True)
    router = build_router(settings, {"groq": FakeClient("groq")}, [("groq", "a")], breaker=breaker)

    status = router.status()

    assert status.any_open is True
    degraded = [circuit for circuit in status.circuits if not circuit.healthy]
    assert [circuit.provider for circuit in degraded] == ["groq"]
    assert degraded[0].opens_remaining_s == pytest.approx(90.0)
    assert metrics.LLM_CIRCUIT_STATE.value(provider="groq", state="open") == 1.0


# --- Telegram is throttling us ------------------------------------------


class ThrottlingBot:
    """A Bot API that answers every send with 429."""

    def __init__(self) -> None:
        self.attempts = 0

    async def send_message(self, **kwargs: Any) -> None:
        from telegram.error import RetryAfter

        self.attempts += 1
        raise RetryAfter(30)


async def test_telegram_throttling_does_not_re_enter_the_error_handler() -> None:
    """The notification failing must not become a second failure, and a loop."""
    bot = ThrottlingBot()
    context = type("Ctx", (), {"error": Unavailable("vault down"), "bot": bot, "bot_data": {}})()

    await handle_error(object(), context)

    assert bot.attempts == 0  # no Update, so nothing to answer


async def test_a_throttled_notification_is_logged_and_swallowed() -> None:
    from telegram import Chat, Message, Update, User

    bot = ThrottlingBot()
    message = Message(
        message_id=1,
        date=None,  # type: ignore[arg-type]
        chat=Chat(id=99, type="private"),
        from_user=User(id=7, first_name="A", is_bot=False),
    )
    update = Update(update_id=1, message=message)
    context = type("Ctx", (), {"error": Unavailable("vault down"), "bot": bot, "bot_data": {}})()

    await handle_error(update, context)

    assert bot.attempts == 1  # tried once, raised, and did not propagate


# --- The health endpoint under fault -------------------------------------


def _app(server: HealthServer) -> web.Application:
    app = web.Application()
    app.router.add_get("/readyz", server._handle_readyz)
    return app


async def _client(server: HealthServer) -> TestClient[web.Request, web.Application]:
    client: TestClient[web.Request, web.Application] = TestClient(TestServer(_app(server)))
    await client.start_server()
    return client


async def test_a_check_that_hangs_answers_503_rather_than_timing_out() -> None:
    """A probe that never gets an answer looks like a dead process, not a sick one."""
    server = HealthServer(port=0, version="0.1.0", cache_ttl_s=0.0)

    async def hangs() -> bool:
        await asyncio.sleep(3600)
        return True

    server.register_check("notediscovery", hangs)
    import discoverygram.health as health_module

    original = health_module.CHECK_TIMEOUT_S
    health_module.CHECK_TIMEOUT_S = 0.05
    client = await _client(server)
    try:
        response = await client.get("/readyz")

        assert response.status == 503
        assert (await response.json())["checks"]["notediscovery"] == "failed"
    finally:
        health_module.CHECK_TIMEOUT_S = original
        await client.close()


async def test_checks_run_concurrently_so_one_slow_dependency_does_not_add_up() -> None:
    server = HealthServer(port=0, version="0.1.0", cache_ttl_s=0.0)

    async def slow() -> bool:
        await asyncio.sleep(0.05)
        return True

    for name in ("notediscovery", "sessions", "telegram"):
        server.register_check(name, slow)

    started = asyncio.get_running_loop().time()
    results, ready = await server._evaluate()
    elapsed = asyncio.get_running_loop().time() - started

    assert ready is True
    assert len(results) == 3
    assert elapsed < 0.15
