"""The startup probe: what the bot may offer, decided once at boot."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from discoverygram.adapters.rest import RestNoteStore
from discoverygram.app.probe import KNOWN_GOOD_VERSION, probe_instance
from discoverygram.config import Settings
from tests.fixtures import notediscovery as fx

BASE = "http://notediscovery.test:8000"


@pytest.fixture
def rest(settings: Settings) -> RestNoteStore:
    return RestNoteStore(settings.model_copy(update={"notediscovery_max_retries": 0}))


def mock_instance(config: dict[str, Any], *, healthy: bool = True) -> None:
    respx.get(f"{BASE}/health").mock(
        return_value=httpx.Response(200, json=fx.HEALTH)
        if healthy
        else httpx.Response(503, json={"detail": "down"})
    )
    respx.get(f"{BASE}/api/config").mock(return_value=httpx.Response(200, json=config))


@respx.mock
async def test_a_healthy_instance_enables_search(rest: RestNoteStore) -> None:
    mock_instance(fx.CONFIG)

    state = await probe_instance(rest)

    assert state.healthy is True
    assert state.search_available is True
    assert state.why_search_unavailable() == ""
    assert state.config.version == KNOWN_GOOD_VERSION


@respx.mock
async def test_search_disabled_server_side_is_detected_at_startup(
    rest: RestNoteStore,
) -> None:
    """`/api/search` answers 403 when disabled; the bot must not learn that per request."""
    mock_instance(fx.CONFIG_SEARCH_DISABLED)

    state = await probe_instance(rest)

    assert state.search_available is False
    assert "disabled" in state.why_search_unavailable()


@respx.mock
async def test_an_unreachable_instance_degrades_without_raising(rest: RestNoteStore) -> None:
    respx.get(f"{BASE}/health").mock(side_effect=httpx.ConnectError("refused"))
    respx.get(f"{BASE}/api/config").mock(side_effect=httpx.ConnectError("refused"))

    state = await probe_instance(rest)

    assert state.healthy is False
    assert state.search_available is False
    assert "not reachable" in state.why_search_unavailable()


@respx.mock
async def test_a_failed_config_read_does_not_disable_search(rest: RestNoteStore) -> None:
    """A probe failure must not silently switch off a feature that may be fine."""
    respx.get(f"{BASE}/health").mock(return_value=httpx.Response(200, json=fx.HEALTH))
    respx.get(f"{BASE}/api/config").mock(return_value=httpx.Response(500))

    state = await probe_instance(rest)

    assert state.healthy is True
    assert state.search_available is True


@respx.mock
async def test_a_version_mismatch_is_flagged_not_fatal(rest: RestNoteStore) -> None:
    """The contract doc is version-stamped; an upgrade must be visible in the logs."""
    mock_instance({**fx.CONFIG, "version": "0.99.0"})

    state = await probe_instance(rest)

    assert state.version_matches_contract is False
    assert state.search_available is True
