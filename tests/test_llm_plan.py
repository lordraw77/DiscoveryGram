"""The attempt ladder: (provider, model) failover ordering."""

from __future__ import annotations

from discoverygram.llm.plan import (
    TASK_DEFAULTS,
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


# --- Task profiles -------------------------------------------------------


def test_title_and_summarise_are_chat_capability_tasks() -> None:
    """Four tasks, two capabilities — so an operator configures two chains."""
    assert TaskProfile.CHAT.requires_vision is False
    assert TaskProfile.TITLE.requires_vision is False
    assert TaskProfile.SUMMARISE.requires_vision is False
    assert TaskProfile.VISION.requires_vision is True


def test_a_title_draws_on_the_chat_model_list() -> None:
    config = ProviderConfig(
        name="groq", api_key="gsk-x", models=("chat-model",), vision_models=("sees-images",)
    )

    assert config.models_for(TaskProfile.TITLE) == ("chat-model",)
    assert config.models_for(TaskProfile.SUMMARISE) == ("chat-model",)


def test_every_task_has_sampling_defaults_and_a_title_is_the_tightest() -> None:
    assert set(TASK_DEFAULTS) == set(TaskProfile)
    assert TASK_DEFAULTS[TaskProfile.TITLE].max_tokens < TASK_DEFAULTS[TaskProfile.CHAT].max_tokens
    assert (
        TASK_DEFAULTS[TaskProfile.TITLE].temperature < TASK_DEFAULTS[TaskProfile.CHAT].temperature
    )


# --- Capability filtering ------------------------------------------------


def test_a_provider_that_cannot_carry_an_image_is_dropped_from_the_vision_ladder() -> None:
    configs = _configs(
        cerebras=ProviderConfig(name="cerebras", api_key="csk-x", vision_models=("anything",)),
        gemini=ProviderConfig(name="gemini", api_key="AIza-x", vision_models=("flash",)),
    )

    ladder, skipped = build_attempt_ladder(
        ["cerebras", "gemini"],
        configs,
        TaskProfile.VISION,
        capabilities={"cerebras": False, "gemini": True},
    )

    assert [str(attempt) for attempt in ladder] == ["gemini/flash"]
    assert skipped == ["cerebras: this provider cannot accept images"]


def test_capability_is_ignored_for_a_chat_ladder() -> None:
    """A text-only provider is a perfectly good chat rung."""
    configs = _configs(cerebras=ProviderConfig(name="cerebras", api_key="csk-x", models=("fast",)))

    ladder, _ = build_attempt_ladder(
        ["cerebras"], configs, TaskProfile.CHAT, capabilities={"cerebras": False}
    )

    assert [str(attempt) for attempt in ladder] == ["cerebras/fast"]


def test_unknown_capability_is_not_treated_as_false_when_none_is_supplied() -> None:
    """`make check-env` builds the ladder before any client exists."""
    configs = _configs(gemini=ProviderConfig(name="gemini", api_key="k", vision_models=("f",)))

    ladder, _ = build_attempt_ladder(["gemini"], configs, TaskProfile.VISION)

    assert len(ladder) == 1


def test_the_account_id_is_read_generically_rather_than_special_cased() -> None:
    configs = load_provider_configs({"CLOUDFLARE_ACCOUNT_ID": "acc-1", "CLOUDFLARE_API_KEY": "k"})

    assert configs["cloudflare"].account_id == "acc-1"
