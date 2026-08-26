"""Structured logging setup."""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterable, MutableMapping
from typing import Any

import structlog

from discoverygram.util.correlation import get_correlation_id

# Values that must never reach the logs, matched against the event dict keys.
_SECRET_KEYS = frozenset(
    {
        "token",
        "api_key",
        "apikey",
        "password",
        "secret",
        "authorization",
        "x-api-key",
    }
)
_REDACTED = "***redacted***"

# Literal secret values, scrubbed from *both* pipelines. Populated by
# `configure_logging`; module-level because structlog processors are plain
# functions with no place to hang configuration.
_SECRET_VALUES: list[str] = []
# Shorter values would match too much ordinary text to be worth scrubbing.
_MIN_SECRET_LENGTH = 8


def _scrub(text: str) -> str:
    for secret in _SECRET_VALUES:
        text = text.replace(secret, _REDACTED)
    return text


def _add_correlation_id(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    correlation_id = get_correlation_id()
    if correlation_id:
        event_dict["correlation_id"] = correlation_id
    return event_dict


def _scrub_secret_values(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Scrub secret values from anywhere in our own events.

    Key-based redaction cannot catch a token quoted inside someone else's error
    message — `InvalidToken` puts it in the text — so the values are scrubbed
    by substring as well.
    """
    if not _SECRET_VALUES:
        return event_dict
    for key, value in event_dict.items():
        if isinstance(value, str):
            event_dict[key] = _scrub(value)
    return event_dict


def _redact_secrets(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Redact anything whose key looks like a credential.

    Cheap insurance: a stray `log.info("call", api_key=...)` should not leak.
    """
    for key in list(event_dict):
        if key.lower() in _SECRET_KEYS:
            event_dict[key] = _REDACTED
    return event_dict


class SecretScrubber(logging.Filter):
    """Replace literal secret values anywhere in a stdlib log record.

    The key-based redaction above only protects what *we* log. Third-party
    libraries log whatever they like: python-telegram-bot, for one, logs the Bot
    API URL — which contains the bot token — every time it builds a request. A
    key-name check cannot catch a secret embedded in a URL, so the actual values
    are scrubbed by substring instead.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if not _SECRET_VALUES:
            return True
        record.msg = _scrub(str(record.msg))
        if isinstance(record.args, tuple):
            record.args = tuple(_scrub(str(arg)) for arg in record.args)
        elif isinstance(record.args, dict):
            record.args = {key: _scrub(str(value)) for key, value in record.args.items()}
        return True


def configure_logging(
    level: str = "INFO",
    log_format: str = "json",
    secrets: Iterable[str] = (),
) -> None:
    """Configure structlog and the stdlib root logger to match.

    `secrets` are literal values scrubbed from every stdlib record — the bot
    token and the NoteDiscovery API key, which third-party libraries embed in
    URLs we do not control.
    """
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, level.upper()),
        force=True,
    )

    _SECRET_VALUES[:] = sorted(
        {secret for secret in secrets if secret and len(secret) >= _MIN_SECRET_LENGTH},
        key=len,
        reverse=True,
    )
    scrubber = SecretScrubber()
    for handler in logging.getLogger().handlers:
        handler.addFilter(scrubber)

    # These libraries narrate every request. At our volume that is noise, and in
    # python-telegram-bot's case it is noise containing the token.
    for noisy in ("httpx", "httpcore", "telegram", "telegram.ext", "telegram.Bot", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer()
        if log_format == "json"
        else structlog.dev.ConsoleRenderer(colors=True)
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _add_correlation_id,
            _redact_secrets,
            _scrub_secret_values,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, level.upper())),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger
