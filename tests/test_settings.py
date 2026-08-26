"""Settings loading, parsing and cross-field validation."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from discoverygram.config import SessionBackend, Settings, TelegramMode, Transport
from discoverygram.llm import TaskProfile


def test_loads_from_environment(env: None) -> None:
    settings = Settings()  # type: ignore[call-arg]

    assert settings.telegram_allowed_user_ids == [111]
    assert str(settings.notediscovery_url).startswith("http://notediscovery.test:8000")
    assert settings.notediscovery_transport is Transport.REST
    assert settings.telegram_mode is TelegramMode.POLLING
    assert settings.session_backend is SessionBackend.MEMORY


def test_parses_comma_separated_ids(env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", " 111, 222 ,333 ")
    settings = Settings()  # type: ignore[call-arg]

    assert settings.telegram_allowed_user_ids == [111, 222, 333]


def test_parses_provider_chains(env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_CHAIN_CHAT", "Groq, Cerebras ,ollama")
    settings = Settings()  # type: ignore[call-arg]

    assert settings.llm_chain_chat == ["groq", "cerebras", "ollama"]


def test_empty_allow_list_is_rejected(env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty allow-list would expose the bot to everyone."""
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "")

    with pytest.raises(ValidationError, match="at least one Telegram user id"):
        Settings()  # type: ignore[call-arg]


def test_webhook_mode_requires_url(env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_MODE", "webhook")

    with pytest.raises(ValidationError, match="TELEGRAM_WEBHOOK_URL is required"):
        Settings()  # type: ignore[call-arg]


def test_redis_backend_requires_url(env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SESSION_BACKEND", "redis")

    with pytest.raises(ValidationError, match="REDIS_URL is required"):
        Settings()  # type: ignore[call-arg]


def test_mcp_transport_requires_mcp_enabled(env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOTEDISCOVERY_TRANSPORT", "mcp")

    with pytest.raises(ValidationError, match="requires MCP_ENABLED=true"):
        Settings()  # type: ignore[call-arg]


def test_api_key_is_optional(env: None) -> None:
    """NoteDiscovery may run unauthenticated, so no key must still work."""
    settings = Settings()  # type: ignore[call-arg]

    assert "X-API-Key" not in settings.notediscovery_headers


def test_api_key_is_sent_when_present(env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOTEDISCOVERY_API_KEY", "s3cret")
    settings = Settings()  # type: ignore[call-arg]

    assert settings.notediscovery_headers["X-API-Key"] == "s3cret"


def test_allow_list_checks(env: None) -> None:
    settings = Settings()  # type: ignore[call-arg]

    assert settings.is_user_allowed(111)
    assert not settings.is_user_allowed(999)
    assert not settings.is_user_allowed(None)
    # No chat allow-list configured means any chat is acceptable.
    assert settings.is_chat_allowed(-100)


def test_chat_allow_list_restricts(env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "-100200300")
    settings = Settings()  # type: ignore[call-arg]

    assert settings.is_chat_allowed(-100200300)
    assert not settings.is_chat_allowed(-1)


def test_max_upload_bytes(env: None) -> None:
    settings = Settings()  # type: ignore[call-arg]

    assert settings.max_upload_bytes == 20 * 1024 * 1024


def test_attempt_ladder_from_environment(env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Settings expands the configured chain into (provider, model) attempts."""
    monkeypatch.setenv("LLM_CHAIN_CHAT", "nvidia,ollama")
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-x")
    monkeypatch.setenv("NVIDIA_MODELS", "model-a,model-b")
    monkeypatch.setenv("OLLAMA_MODELS", "local-a")
    settings = Settings()  # type: ignore[call-arg]

    ladder, skipped = settings.attempt_ladder(TaskProfile.CHAT)

    assert [str(attempt) for attempt in ladder] == [
        "nvidia/model-a",
        "nvidia/model-b",
        "ollama/local-a",
    ]
    assert skipped == []


def test_retries_per_model_default(env: None) -> None:
    settings = Settings()  # type: ignore[call-arg]

    assert settings.llm_retries_per_model == 3


# --- The .env.example contract --------------------------------------------


def test_every_setting_the_code_reads_appears_in_env_example() -> None:
    """A variable the code reads and the example omits is invisible to an operator.

    Asserted mechanically because it is exactly the kind of thing that drifts:
    a field added in a hurry works perfectly for whoever added it and cannot be
    discovered by anyone else.
    """
    present = _documented_variables()
    declared = {name.upper() for name in Settings.model_fields}

    assert declared - present == set()


def test_env_example_documents_nothing_the_code_does_not_read() -> None:
    """The other direction: a stale variable is a promise the code does not keep."""
    from discoverygram.llm.plan import KNOWN_PROVIDERS

    provider_variables = {
        f"{provider.upper()}_{suffix}"
        for provider in KNOWN_PROVIDERS
        for suffix in ("API_KEY", "BASE_URL", "ACCOUNT_ID", "MODELS", "VISION_MODELS")
    }
    declared = {name.upper() for name in Settings.model_fields}

    unexplained = _documented_variables() - declared - provider_variables - {"VERSION"}

    assert unexplained == set()


def _documented_variables() -> set[str]:
    """Every `NAME=` in .env.example, including the commented-out optional ones."""
    example = Path(__file__).resolve().parent.parent / ".env.example"
    return set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]+)=", example.read_text(), re.MULTILINE))
