"""Building the router from settings.

Two things are decided here and asserted here: which clients are built at all,
and which rungs survive the capability check. Both are startup-time decisions,
so getting them wrong shows up as a mysterious failure on the first photo
rather than as a message in the log.
"""

from __future__ import annotations

import pytest

from discoverygram.config import Settings
from discoverygram.llm.base import OpenAiCompatibleClient
from discoverygram.llm.cloudflare import CloudflareClient
from discoverygram.llm.factory import (
    build_client,
    build_clients,
    build_router,
    provider_supports_vision,
)
from discoverygram.llm.gemini import GeminiClient
from discoverygram.llm.plan import ProviderConfig, TaskProfile
from discoverygram.llm.puter import PuterClient


@pytest.fixture
def llm_env(monkeypatch: pytest.MonkeyPatch, env: None) -> None:
    """A configuration with two chat providers and one vision provider."""
    del env
    monkeypatch.setenv("LLM_CHAIN_CHAT", "groq,ollama")
    monkeypatch.setenv("LLM_CHAIN_VISION", "gemini,ollama")
    monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
    monkeypatch.setenv("GROQ_MODELS", "fast,slow")
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-test")
    monkeypatch.setenv("GEMINI_VISION_MODELS", "gemini-2.0-flash")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://ollama.test:11434")
    monkeypatch.setenv("OLLAMA_MODELS", "llama3")
    monkeypatch.setenv("OLLAMA_VISION_MODELS", "llava")


def _settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


# --- Adapter selection ---------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("gemini", GeminiClient),
        ("puter", PuterClient),
        ("groq", OpenAiCompatibleClient),
        ("nvidia", OpenAiCompatibleClient),
        ("openrouter", OpenAiCompatibleClient),
        ("cerebras", OpenAiCompatibleClient),
        ("mistral", OpenAiCompatibleClient),
        ("ollama", OpenAiCompatibleClient),
    ],
)
def test_each_provider_gets_its_adapter(name: str, expected: type) -> None:
    client = build_client(ProviderConfig(name=name, api_key="k"))
    assert isinstance(client, expected)
    assert client.name == name


def test_cloudflare_needs_its_account_id() -> None:
    client = build_client(ProviderConfig(name="cloudflare", api_key="k", account_id="acc"))
    assert isinstance(client, CloudflareClient)

    with pytest.raises(ValueError, match="CLOUDFLARE_ACCOUNT_ID"):
        build_client(ProviderConfig(name="cloudflare", api_key="k"))


def test_an_unknown_provider_with_a_base_url_is_treated_as_openai_compatible() -> None:
    """The escape hatch for a gateway DiscoveryGram has never heard of."""
    client = build_client(
        ProviderConfig(name="somegateway", api_key="k", base_url="https://gw.test/v1")
    )
    assert isinstance(client, OpenAiCompatibleClient)


def test_an_unknown_provider_with_no_base_url_is_refused() -> None:
    with pytest.raises(ValueError, match="SOMEGATEWAY_BASE_URL"):
        build_client(ProviderConfig(name="somegateway", api_key="k"))


# --- Which clients exist -------------------------------------------------


def test_only_providers_named_in_a_chain_are_built(llm_env: None) -> None:
    """Nine keys in .env must not open nine connection pools."""
    del llm_env
    clients, problems = build_clients(_settings())

    assert set(clients) == {"groq", "gemini", "ollama"}
    assert problems == []


def test_a_provider_without_credentials_is_not_built(
    monkeypatch: pytest.MonkeyPatch, llm_env: None
) -> None:
    del llm_env
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setenv("GROQ_API_KEY", "")

    clients, _ = build_clients(_settings())

    assert "groq" not in clients


def test_an_unbuildable_provider_degrades_the_ladder_rather_than_startup(
    monkeypatch: pytest.MonkeyPatch, llm_env: None
) -> None:
    del llm_env
    monkeypatch.setenv("LLM_CHAIN_CHAT", "cloudflare,ollama")
    monkeypatch.setenv("CLOUDFLARE_API_KEY", "cf-test")
    monkeypatch.setenv("CLOUDFLARE_MODELS", "@cf/meta/llama-3-8b")

    clients, problems = build_clients(_settings())

    assert "cloudflare" not in clients
    assert "ollama" in clients
    assert any("CLOUDFLARE_ACCOUNT_ID" in problem for problem in problems)


# --- Which rungs survive -------------------------------------------------


def test_the_chat_ladder_expands_every_model_in_order(llm_env: None) -> None:
    del llm_env
    router = build_router(_settings())

    assert [str(a) for a in router.ladder(TaskProfile.CHAT).attempts] == [
        "groq/fast",
        "groq/slow",
        "ollama/llama3",
    ]


def test_title_and_summarise_share_the_chat_ladder(llm_env: None) -> None:
    """Two chains to configure, not four."""
    del llm_env
    router = build_router(_settings())

    chat = router.ladder(TaskProfile.CHAT).attempts
    assert router.ladder(TaskProfile.TITLE).attempts == chat
    assert router.ladder(TaskProfile.SUMMARISE).attempts == chat


def test_a_text_only_provider_is_dropped_from_the_vision_ladder_with_a_reason(
    monkeypatch: pytest.MonkeyPatch, llm_env: None
) -> None:
    """Cerebras cannot carry an image; the operator learns that at startup."""
    del llm_env
    monkeypatch.setenv("LLM_CHAIN_VISION", "cerebras,gemini")
    monkeypatch.setenv("CEREBRAS_API_KEY", "csk-test")
    monkeypatch.setenv("CEREBRAS_MODELS", "llama-3.3-70b")

    router = build_router(_settings())
    ladder = router.ladder(TaskProfile.VISION)

    assert [attempt.provider for attempt in ladder.attempts] == ["gemini"]
    assert any("cannot accept images" in reason for reason in ladder.skipped)


def test_a_rung_whose_client_was_not_built_is_dropped_from_the_ladder(
    monkeypatch: pytest.MonkeyPatch, llm_env: None
) -> None:
    """`/status` must report the ladder that will actually be walked."""
    del llm_env
    monkeypatch.setenv("LLM_CHAIN_CHAT", "cloudflare,ollama")
    monkeypatch.setenv("CLOUDFLARE_API_KEY", "cf-test")
    monkeypatch.setenv("CLOUDFLARE_MODELS", "@cf/meta/llama-3-8b")

    ladder = build_router(_settings()).ladder(TaskProfile.CHAT)

    assert [attempt.provider for attempt in ladder.attempts] == ["ollama"]
    assert any("CLOUDFLARE_ACCOUNT_ID" in reason for reason in ladder.skipped)


def test_no_credentials_at_all_is_a_supported_configuration(env: None) -> None:
    """Milestone M1 is a bot with no LLM at all. It must still start."""
    del env
    router = build_router(_settings())

    assert router.available(TaskProfile.CHAT) is False
    assert router.available(TaskProfile.VISION) is False
    assert router.status().circuits == ()


def test_a_provider_with_a_key_but_no_models_is_skipped_with_the_variable_named(
    monkeypatch: pytest.MonkeyPatch, env: None
) -> None:
    del env
    monkeypatch.setenv("LLM_CHAIN_CHAT", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "gsk-test")

    ladder = build_router(_settings()).ladder(TaskProfile.CHAT)

    assert ladder.usable is False
    assert any("GROQ_MODELS" in reason for reason in ladder.skipped)


async def test_the_router_closes_the_clients_it_built(llm_env: None) -> None:
    del llm_env
    router = build_router(_settings())
    await router.aclose()


# --- Capability without a client ----------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("cerebras", False),
        ("groq", True),
        ("ollama", True),
        ("gemini", True),
        ("cloudflare", True),
        ("puter", True),
        ("somegateway", True),
    ],
)
def test_capability_is_knowable_without_building_a_client(name: str, expected: bool) -> None:
    """`make check-env` prints the real vision ladder with no credentials."""
    assert provider_supports_vision(name) is expected


def test_the_static_capability_table_agrees_with_the_clients_own_answer() -> None:
    """Two sources of truth would drift, and the drift would be silent."""
    for name in ("groq", "cerebras", "ollama", "gemini", "puter"):
        client = build_client(ProviderConfig(name=name, api_key="k"))
        assert client.supports_vision() is provider_supports_vision(name), name
