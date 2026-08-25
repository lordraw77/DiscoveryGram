"""The attempt ladder: (provider, model) failover ordering."""

from __future__ import annotations

from discoverygram.llm.plan import (
    ProviderConfig,
    TaskProfile,
    build_attempt_ladder,
    load_provider_configs,
)


def _configs(**overrides: ProviderConfig) -> dict[str, ProviderConfig]:
    return dict(overrides)


def test_models_are_expanded_in_order() -> None:
    """Every model of a provider is tried before the next provider."""
    configs = _configs(
        nvidia=ProviderConfig(
            name="nvidia", api_key="nvapi-x", models=("model-a", "model-b", "model-c")
        ),
        ollama=ProviderConfig(name="ollama", models=("model-d",)),
    )

    ladder, skipped = build_attempt_ladder(["nvidia", "ollama"], configs, TaskProfile.CHAT)

    assert [str(attempt) for attempt in ladder] == [
        "nvidia/model-a",
        "nvidia/model-b",
        "nvidia/model-c",
        "ollama/model-d",
    ]
    assert skipped == []


def test_vision_task_uses_vision_models() -> None:
    configs = _configs(
        nvidia=ProviderConfig(
            name="nvidia",
            api_key="nvapi-x",
            models=("text-only",),
            vision_models=("sees-images", "sees-images-too"),
        )
    )

    ladder, _ = build_attempt_ladder(["nvidia"], configs, TaskProfile.VISION)

    assert [attempt.model for attempt in ladder] == ["sees-images", "sees-images-too"]


def test_provider_without_key_is_skipped_with_a_reason() -> None:
    configs = _configs(
        nvidia=ProviderConfig(name="nvidia", api_key="", models=("model-a",)),
        ollama=ProviderConfig(name="ollama", models=("model-d",)),
    )

    ladder, skipped = build_attempt_ladder(["nvidia", "ollama"], configs, TaskProfile.CHAT)

    assert [str(attempt) for attempt in ladder] == ["ollama/model-d"]
    assert skipped == ["nvidia: no API key configured"]


def test_keyless_provider_needs_no_credentials() -> None:
    """Ollama is reachable without an API key."""
    configs = _configs(ollama=ProviderConfig(name="ollama", models=("model-d",)))

    ladder, skipped = build_attempt_ladder(["ollama"], configs, TaskProfile.CHAT)

    assert len(ladder) == 1
    assert skipped == []


def test_provider_without_models_for_the_task_is_skipped() -> None:
    """A provider with no vision model must not be tried for a vision task."""
    configs = _configs(
        nvidia=ProviderConfig(name="nvidia", api_key="nvapi-x", models=("text-only",)),
    )

    ladder, skipped = build_attempt_ladder(["nvidia"], configs, TaskProfile.VISION)

    assert ladder == []
    assert skipped == ["nvidia: no vision model listed in NVIDIA_VISION_MODELS"]


def test_unknown_provider_is_reported_not_ignored() -> None:
    """A typo in the chain must be visible, not silently dropped."""
    ladder, skipped = build_attempt_ladder(["nvidai"], {}, TaskProfile.CHAT)

    assert ladder == []
    assert skipped == ["nvidai: unknown provider"]


def test_empty_chain_yields_empty_ladder() -> None:
    assert build_attempt_ladder([], {}, TaskProfile.CHAT) == ([], [])


def test_load_provider_configs_parses_model_lists() -> None:
    configs = load_provider_configs(
        {
            "NVIDIA_API_KEY": "nvapi-x",
            "NVIDIA_BASE_URL": "https://integrate.api.nvidia.com/v1",
            "NVIDIA_MODELS": " model-a , model-b ,, model-c ",
            "NVIDIA_VISION_MODELS": "vision-a",
        }
    )

    nvidia = configs["nvidia"]
    assert nvidia.api_key == "nvapi-x"
    assert nvidia.models == ("model-a", "model-b", "model-c")
    assert nvidia.vision_models == ("vision-a",)
    # Providers with nothing configured still exist, simply empty.
    assert configs["groq"].models == ()
    assert not configs["groq"].has_credentials


def test_load_provider_configs_covers_every_known_provider() -> None:
    configs = load_provider_configs({})

    assert set(configs) == {
        "nvidia",
        "openrouter",
        "groq",
        "gemini",
        "cloudflare",
        "cerebras",
        "mistral",
        "puter",
        "ollama",
    }
