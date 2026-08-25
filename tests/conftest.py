"""Shared test fixtures."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

# The minimum environment a valid Settings object needs.
BASE_ENV = {
    "TELEGRAM_BOT_TOKEN": "123:test-token",
    "TELEGRAM_ALLOWED_USER_IDS": "111",
    "NOTEDISCOVERY_URL": "http://notediscovery.test:8000",
}


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Isolate settings from the developer's real .env and shell environment."""
    for key in [
        *BASE_ENV,
        "NOTEDISCOVERY_API_KEY",
        "NOTEDISCOVERY_TRANSPORT",
        "TELEGRAM_MODE",
        "TELEGRAM_WEBHOOK_URL",
        "SESSION_BACKEND",
        "REDIS_URL",
        "MCP_ENABLED",
        "LLM_CHAIN_CHAT",
        "TELEGRAM_ALLOWED_CHAT_IDS",
    ]:
        monkeypatch.delenv(key, raising=False)
    for key, value in BASE_ENV.items():
        monkeypatch.setenv(key, value)
    # Never read the developer's .env during tests.
    monkeypatch.setattr(
        "discoverygram.config.settings.Settings.model_config",
        {"env_file": None, "extra": "ignore", "case_sensitive": False},
        raising=False,
    )
    yield
