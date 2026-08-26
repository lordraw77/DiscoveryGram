"""Logging setup: correlation ids and secret redaction."""

from __future__ import annotations

import logging
from collections.abc import Iterator, MutableMapping
from typing import Any

import pytest

import discoverygram.util.logging as logging_module
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


# --- Scrubbing literal secret values --------------------------------------


class Record(logging.LogRecord):
    """A stdlib record built without going through a logger."""

    def __init__(self, msg: object, args: object = ()) -> None:
        super().__init__("test", logging.INFO, __file__, 1, msg, args, None)  # type: ignore[arg-type]


@pytest.fixture
def scrubbing(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Install two secrets and remove them again, so tests stay isolated."""
    monkeypatch.setattr(
        logging_module, "_SECRET_VALUES", ["1234567890:AAFAKEtokenVALUE", "apikeyvalue12345"]
    )
    yield


def test_a_token_embedded_in_a_third_party_message_is_scrubbed(scrubbing: None) -> None:
    """python-telegram-bot logs the Bot API URL, and the token is part of it."""
    record = Record("Set Bot API URL: https://api.telegram.org/bot1234567890:AAFAKEtokenVALUE")

    logging_module.SecretScrubber().filter(record)

    assert "AAFAKEtokenVALUE" not in str(record.msg)
    assert "***redacted***" in str(record.msg)


def test_secrets_in_positional_and_keyword_args_are_scrubbed(scrubbing: None) -> None:
    positional = Record("calling %s", ("https://x/bot1234567890:AAFAKEtokenVALUE",))
    mapping = Record("calling %(url)s")
    # Assigned after construction: LogRecord unwraps a single mapping argument.
    mapping.args = {"url": "key=apikeyvalue12345"}

    logging_module.SecretScrubber().filter(positional)
    logging_module.SecretScrubber().filter(mapping)

    assert "AAFAKEtokenVALUE" not in str(positional.args)
    assert "apikeyvalue12345" not in str(mapping.args)


def test_ordinary_records_are_left_alone(scrubbing: None) -> None:
    record = Record("nothing sensitive here")

    logging_module.SecretScrubber().filter(record)

    assert record.msg == "nothing sensitive here"


def test_our_own_events_are_scrubbed_by_value_too(scrubbing: None) -> None:
    """Key-based redaction cannot catch a token quoted inside an error message."""
    event = logging_module._scrub_secret_values(
        None,
        "error",
        {
            "event": "telegram_token_rejected",
            "error": "The token `1234567890:AAFAKEtokenVALUE` was rejected",
        },
    )

    assert "AAFAKEtokenVALUE" not in event["error"]


def test_short_values_are_not_treated_as_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """A two-character "secret" would redact half of every message."""
    logging_module.configure_logging(secrets=("ab", "a-long-enough-secret"))

    assert logging_module._SECRET_VALUES == ["a-long-enough-secret"]


def test_configure_logging_quiets_the_libraries_that_narrate_requests() -> None:
    logging_module.configure_logging()

    for noisy in ("httpx", "telegram", "telegram.ext"):
        assert logging.getLogger(noisy).level == logging.WARNING
