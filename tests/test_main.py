"""Entry point: settings loading and the NoteDiscovery readiness probe."""

from __future__ import annotations

import httpx
import pytest
import respx

from discoverygram.__main__ import _check_notediscovery, load_settings
from discoverygram.config import Settings

HEALTH_URL = "http://notediscovery.test:8000/health"


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


@respx.mock
async def test_check_notediscovery_ok(env: None) -> None:
    respx.get(HEALTH_URL).mock(return_value=httpx.Response(200, json={"status": "ok"}))

    assert await _check_notediscovery(Settings())  # type: ignore[call-arg]


@respx.mock
async def test_check_notediscovery_sends_api_key(
    env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOTEDISCOVERY_API_KEY", "s3cret")
    route = respx.get(HEALTH_URL).mock(return_value=httpx.Response(200))

    await _check_notediscovery(Settings())  # type: ignore[call-arg]

    assert route.calls.last.request.headers["X-API-Key"] == "s3cret"


@respx.mock
async def test_check_notediscovery_false_on_error_status(env: None) -> None:
    respx.get(HEALTH_URL).mock(return_value=httpx.Response(503))

    assert not await _check_notediscovery(Settings())  # type: ignore[call-arg]


@respx.mock
async def test_check_notediscovery_false_when_unreachable(env: None) -> None:
    """A down instance degrades readiness; it must not raise."""
    respx.get(HEALTH_URL).mock(side_effect=httpx.ConnectError("refused"))

    assert not await _check_notediscovery(Settings())  # type: ignore[call-arg]
