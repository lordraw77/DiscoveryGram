"""Application settings.

Every value comes from the environment (or a `.env` file). Nothing is hardcoded.
Invalid or missing required values fail fast at startup with an explicit message.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, HttpUrl, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


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
    llm_max_retries: int = Field(default=3, ge=0)
    llm_backoff_base_s: float = Field(default=1.0, gt=0)
    llm_request_timeout_s: float = Field(default=60.0, gt=0)
    llm_circuit_failure_threshold: int = Field(default=5, gt=0)
    llm_circuit_reset_s: float = Field(default=120.0, gt=0)
    llm_daily_call_limit_per_user: int = Field(default=100, ge=0)

    # --- Sessions, cache, limits -----------------------------------------
    session_backend: SessionBackend = SessionBackend.MEMORY
    redis_url: str = ""
    session_ttl_s: int = Field(default=3600, gt=0)
    results_page_size: int = Field(default=5, gt=0, le=20)
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

        if self.telegram_mode is TelegramMode.WEBHOOK and not self.telegram_webhook_url:
            raise ValueError("TELEGRAM_WEBHOOK_URL is required when TELEGRAM_MODE=webhook")

        if self.session_backend is SessionBackend.REDIS and not self.redis_url:
            raise ValueError("REDIS_URL is required when SESSION_BACKEND=redis")

        if self.notediscovery_transport is Transport.MCP and not self.mcp_enabled:
            raise ValueError(
                "NOTEDISCOVERY_TRANSPORT=mcp requires MCP_ENABLED=true. "
                "Note that MCP exposes a strict subset of the REST API"
            )

        return self

    # --- Derived helpers --------------------------------------------------

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
