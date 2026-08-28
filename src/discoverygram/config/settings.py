"""Application settings.

Every value comes from the environment (or a `.env` file). Nothing is hardcoded.
Invalid or missing required values fail fast at startup with an explicit message.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

from dotenv import dotenv_values
from pydantic import Field, HttpUrl, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from discoverygram.llm.plan import (
    Attempt,
    ProviderConfig,
    TaskProfile,
    build_attempt_ladder,
    load_provider_configs,
)


class Transport(StrEnum):
    """NoteDiscovery transport.

    REST is primary: NoteDiscovery's MCP server is a strict subset of its REST API
    (no media upload, export, sharing or folder move/rename/delete).
    """

    REST = "rest"
    MCP = "mcp"


class McpLaunchMode(StrEnum):
    """How the MCP stdio subprocess is spawned."""

    DOCKER = "docker"
    LOCAL = "local"


class SessionBackend(StrEnum):
    MEMORY = "memory"
    REDIS = "redis"


class TelegramMode(StrEnum):
    POLLING = "polling"
    WEBHOOK = "webhook"


# `NoDecode` stops pydantic-settings from JSON-decoding these fields, so the
# `mode="before"` validators below receive the raw `1,2,3` string.
CsvInts = Annotated[list[int], NoDecode, Field(default_factory=list)]
CsvStrs = Annotated[list[str], NoDecode, Field(default_factory=list)]


class Settings(BaseSettings):
    """Root configuration object, loaded once at startup."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Telegram ---------------------------------------------------------
    telegram_bot_token: str = Field(min_length=1)
    telegram_allowed_user_ids: CsvInts
    telegram_allowed_chat_ids: CsvInts
    telegram_mode: TelegramMode = TelegramMode.POLLING
    telegram_webhook_url: str = ""
    telegram_webhook_secret: str = ""
    # Where the webhook listener binds inside the container, and the path
    # Telegram posts to. The public URL above is what Telegram is told; these
    # describe the local side of the reverse proxy.
    telegram_webhook_listen: str = "0.0.0.0"  # noqa: S104 - a container binds to all interfaces
    telegram_webhook_port: int = Field(default=8081, gt=0, lt=65536)
    telegram_webhook_path: str = "telegram"
    telegram_parse_mode: Literal["MarkdownV2", "HTML"] = "MarkdownV2"

    # --- NoteDiscovery ----------------------------------------------------
    # Names mirror upstream NoteDiscovery so they pass straight through to the
    # MCP subprocess. The URL must include the port, e.g. http://host:8000
    notediscovery_url: HttpUrl
    notediscovery_api_key: str = ""
    notediscovery_timeout: float = Field(default=30.0, gt=0)
    notediscovery_max_retries: int = Field(default=3, ge=0)
    notediscovery_verify_tls: bool = True
    notediscovery_transport: Transport = Transport.REST

    # `GET /api/search` has no server-side default cap, so we always send one.
    search_default_limit: int = Field(default=50, gt=0)
    # NoteDiscovery refuses to act on queries shorter than this. It is a server
    # constant (`SEARCH_MIN_QUERY_LENGTH`, 2 in 0.31.3) and is *not* exposed by
    # `/api/config`, so the bot carries its own copy and rejects short queries
    # locally instead of round-tripping for an empty result.
    search_min_query_length: int = Field(default=2, ge=1)
    tree_cache_ttl_s: int = Field(default=300, ge=0)
    inbox_path: str = "Inbox"
    auto_create_parents: bool = True

    # --- MCP subprocess ---------------------------------------------------
    mcp_enabled: bool = False
    mcp_launch_mode: McpLaunchMode = McpLaunchMode.DOCKER
    mcp_docker_image: str = "ghcr.io/gamosoft/notediscovery:latest"
    mcp_startup_timeout_s: float = Field(default=30.0, gt=0)

    # --- LLM router -------------------------------------------------------
    llm_chain_chat: CsvStrs
    llm_chain_vision: CsvStrs
    # Retries against the *same* (provider, model) pair before moving to the
    # next model. See discoverygram.llm.plan for the ladder semantics.
    llm_retries_per_model: int = Field(default=3, ge=0)
    llm_backoff_base_s: float = Field(default=1.0, gt=0)
    llm_request_timeout_s: float = Field(default=60.0, gt=0)
    llm_circuit_failure_threshold: int = Field(default=5, gt=0)
    llm_circuit_reset_s: float = Field(default=120.0, gt=0)
    llm_daily_call_limit_per_user: int = Field(default=100, ge=0)
    # Burst limit, per user, per rolling minute. The daily cap bounds spend;
    # this bounds rate. Loose by default: one photo capture is several calls.
    llm_user_rate_per_minute: int = Field(default=20, ge=0)
    # How many provider calls the whole process may have in flight. Beyond
    # this, requests queue rather than pile onto a provider that is already
    # the slow part. 0 disables the bound.
    llm_max_concurrent_requests: int = Field(default=8, ge=0)
    # How many tags a generation step may put on a note. A model asked for
    # "some tags" will happily return twenty, and a vault's tag index is
    # shared: over-tagging one note degrades browsing for every other.
    generated_tags_max: int = Field(default=5, gt=0, le=20)
    # Whether a generated note records the provider and model that made it.
    provenance_enabled: bool = True
    # How many notes `/ask` may read as context. Each one is a vault read and
    # tokens in the prompt, so this bounds both cost and latency.
    ask_context_notes: int = Field(default=5, gt=0, le=20)

    # --- Sessions, cache, limits -----------------------------------------
    session_backend: SessionBackend = SessionBackend.MEMORY
    redis_url: str = ""
    session_ttl_s: int = Field(default=3600, gt=0)
    results_page_size: int = Field(default=5, gt=0, le=20)
    # Default window for /recent. NoteDiscovery has no recent endpoint over REST,
    # so this bounds a client-side filter rather than a server query.
    recent_default_days: int = Field(default=7, gt=0)
    tree_page_size: int = Field(default=10, gt=0, le=50)
    long_note_mode: Literal["paged", "split"] = "paged"
    max_upload_mb: int = Field(default=20, gt=0, le=20)
    default_text_action: Literal["search", "quick"] = "search"

    # --- Observability ----------------------------------------------------
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_format: Literal["json", "console"] = "json"
    health_port: int = Field(default=8080, gt=0, lt=65536)
    metrics_enabled: bool = False

    # --- Parsing ----------------------------------------------------------

    @field_validator(
        "telegram_allowed_user_ids",
        "telegram_allowed_chat_ids",
        mode="before",
    )
    @classmethod
    def _parse_int_csv(cls, value: object) -> object:
        """Accept `1,2,3` from the environment as a list of ints."""
        if isinstance(value, str):
            return [int(part.strip()) for part in value.split(",") if part.strip()]
        return value

    @field_validator("llm_chain_chat", "llm_chain_vision", mode="before")
    @classmethod
    def _parse_str_csv(cls, value: object) -> object:
        """Accept `groq,gemini,ollama` from the environment as a list of names."""
        if isinstance(value, str):
            return [part.strip().lower() for part in value.split(",") if part.strip()]
        return value

    # --- Cross-field validation ------------------------------------------

    @model_validator(mode="after")
    def _check_consistency(self) -> Settings:
        if not self.telegram_allowed_user_ids:
            raise ValueError(
                "TELEGRAM_ALLOWED_USER_IDS must list at least one Telegram user id; "
                "an empty allow-list would expose the bot to everyone"
            )

        if self.telegram_mode is TelegramMode.WEBHOOK:
            if not self.telegram_webhook_url:
                raise ValueError("TELEGRAM_WEBHOOK_URL is required when TELEGRAM_MODE=webhook")
            if self.telegram_webhook_port == self.health_port:
                raise ValueError("TELEGRAM_WEBHOOK_PORT and HEALTH_PORT cannot be the same port")

        if self.session_backend is SessionBackend.REDIS and not self.redis_url:
            raise ValueError("REDIS_URL is required when SESSION_BACKEND=redis")

        if self.notediscovery_transport is Transport.MCP and not self.mcp_enabled:
            raise ValueError(
                "NOTEDISCOVERY_TRANSPORT=mcp requires MCP_ENABLED=true. "
                "Note that MCP exposes a strict subset of the REST API"
            )

        return self

    # --- Derived helpers --------------------------------------------------

    def provider_configs(self) -> dict[str, ProviderConfig]:
        """Read the per-provider `<P>_MODELS` / `<P>_API_KEY` variables.

        These are dynamic (nine providers x four variables), so they are read
        from the raw environment rather than declared as fields. The `.env`
        file is merged in explicitly because pydantic-settings loads it into
        the model without exporting it to `os.environ`; real environment
        variables win, matching pydantic-settings' own precedence.
        """
        merged: dict[str, str] = {}
        env_file = self.model_config.get("env_file")
        if env_file and Path(str(env_file)).is_file():
            merged.update(
                {key: value for key, value in dotenv_values(str(env_file)).items() if value}
            )
        merged.update(os.environ)
        return load_provider_configs(merged)

    def attempt_ladder(
        self,
        task: TaskProfile,
        *,
        capabilities: Mapping[str, bool] | None = None,
    ) -> tuple[list[Attempt], list[str]]:
        """Ordered (provider, model) attempts for a task, plus skip reasons.

        `TITLE` and `SUMMARISE` are chat-capability tasks and share the chat
        chain: the operator configures two chains, not one per task.
        """
        chain = self.llm_chain_vision if task.requires_vision else self.llm_chain_chat
        return build_attempt_ladder(chain, self.provider_configs(), task, capabilities=capabilities)

    @property
    def notediscovery_headers(self) -> dict[str, str]:
        """Headers for NoteDiscovery REST calls.

        The API key is optional: an instance may run unauthenticated.
        """
        headers = {"Accept": "application/json", "User-Agent": "DiscoveryGram/0.1"}
        if self.notediscovery_api_key:
            headers["X-API-Key"] = self.notediscovery_api_key
        return headers

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    def is_user_allowed(self, user_id: int | None) -> bool:
        return user_id is not None and user_id in self.telegram_allowed_user_ids

    def is_chat_allowed(self, chat_id: int | None) -> bool:
        """Group chats are only served when explicitly allow-listed."""
        if not self.telegram_allowed_chat_ids:
            return True
        return chat_id is not None and chat_id in self.telegram_allowed_chat_ids
