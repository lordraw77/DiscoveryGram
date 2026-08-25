"""Correlation ids.

Every Telegram update and every outbound call carries an id so a single user
action can be followed across the bot, NoteDiscovery and the LLM providers.
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar

_correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)


def new_correlation_id() -> str:
    return uuid.uuid4().hex[:12]


def set_correlation_id(value: str | None = None) -> str:
    """Bind a correlation id to the current context and return it."""
    resolved = value or new_correlation_id()
    _correlation_id.set(resolved)
    return resolved


def get_correlation_id() -> str | None:
    return _correlation_id.get()


def clear_correlation_id() -> None:
    _correlation_id.set(None)
