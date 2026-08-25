"""Logging setup: correlation ids and secret redaction."""

from __future__ import annotations

from collections.abc import MutableMapping
from typing import Any

from discoverygram.util.correlation import (
    clear_correlation_id,
    get_correlation_id,
    set_correlation_id,
)
from discoverygram.util.logging import (
    _add_correlation_id,
    _redact_secrets,
    configure_logging,
    get_logger,
)


def _process(event: MutableMapping[str, Any]) -> dict[str, Any]:
    processed = _add_correlation_id(None, "info", event)
    return dict(_redact_secrets(None, "info", processed))


def test_correlation_id_round_trip() -> None:
    clear_correlation_id()
    assert get_correlation_id() is None

    value = set_correlation_id()
    assert get_correlation_id() == value
    assert len(value) == 12

    clear_correlation_id()
    assert get_correlation_id() is None


def test_correlation_id_is_attached_to_events() -> None:
    set_correlation_id("abc123abc123")
    try:
        assert _process({"event": "test"})["correlation_id"] == "abc123abc123"
    finally:
        clear_correlation_id()


def test_no_correlation_key_when_unset() -> None:
    clear_correlation_id()

    assert "correlation_id" not in _process({"event": "test"})


def test_secrets_are_redacted() -> None:
    event = _process(
        {
            "event": "call",
            "api_key": "sk-real-key",
            "token": "123:abc",
            "Authorization": "Bearer xyz",
            "url": "http://notediscovery:8000",
        }
    )

    assert event["api_key"] == "***redacted***"
    assert event["token"] == "***redacted***"
    assert event["Authorization"] == "***redacted***"
    # Non-secret fields are untouched.
    assert event["url"] == "http://notediscovery:8000"


def test_configure_logging_json_and_console() -> None:
    """Both renderers must configure without raising."""
    configure_logging(level="DEBUG", log_format="json")
    get_logger("test").info("configured", api_key="should-be-redacted")

    configure_logging(level="INFO", log_format="console")
    get_logger("test").info("configured")
