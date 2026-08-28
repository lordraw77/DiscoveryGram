"""Shared test fixtures."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from discoverygram.adapters.rest import RestNoteStore
from discoverygram.config import Settings
from discoverygram.llm.plan import KNOWN_PROVIDERS

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
        "SEARCH_MIN_QUERY_LENGTH",
        "SEARCH_DEFAULT_LIMIT",
        "TREE_CACHE_TTL_S",
        "NOTEDISCOVERY_MAX_RETRIES",
        "MAX_UPLOAD_MB",
        "TELEGRAM_WEBHOOK_SECRET",
        "TELEGRAM_WEBHOOK_PORT",
        "TELEGRAM_WEBHOOK_PATH",
        "TELEGRAM_PARSE_MODE",
        "SESSION_TTL_S",
        "HEALTH_PORT",
        "LLM_CHAIN_VISION",
        "LLM_RETRIES_PER_MODEL",
        "LLM_BACKOFF_BASE_S",
        "LLM_REQUEST_TIMEOUT_S",
        "LLM_CIRCUIT_FAILURE_THRESHOLD",
        "LLM_CIRCUIT_RESET_S",
        "LLM_DAILY_CALL_LIMIT_PER_USER",
        "LLM_USER_RATE_PER_MINUTE",
        "LLM_MAX_CONCURRENT_REQUESTS",
        "METRICS_ENABLED",
        # Every `<PROVIDER>_*` variable, so a developer's real keys and model
        # lists can never reach a test's ladder.
        *(
            f"{provider.upper()}_{suffix}"
            for provider in KNOWN_PROVIDERS
            for suffix in ("API_KEY", "BASE_URL", "ACCOUNT_ID", "MODELS", "VISION_MODELS")
        ),
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


@pytest.fixture
def settings(env: None) -> Settings:
    """A Settings object built from the isolated test environment."""
    return Settings()  # type: ignore[call-arg]


@pytest.fixture
def store(settings: Settings) -> Iterator[RestNoteStore]:
    """A REST adapter with retries off, so failure tests do not sleep."""
    settings = settings.model_copy(update={"notediscovery_max_retries": 0})
    adapter = RestNoteStore(settings)
    yield adapter
