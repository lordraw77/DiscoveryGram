"""Provider configuration and the attempt ladder.

The unit of failover is a **(provider, model) pair**, not a provider. A request
retries the same pair up to `LLM_RETRIES_PER_MODEL` times, then moves to the
next model of the same provider, and only when that provider's models are
exhausted does it move to the next provider in the chain.

Example, with `LLM_CHAIN_CHAT=nvidia,ollama`, three NVIDIA models and one
Ollama model, retries set to 3:

    nvidia/model-a  (x3) -> nvidia/model-b  (x3) -> nvidia/model-c  (x3)
                         -> ollama/model-d  (x3) -> give up

This module is pure logic: it builds the ladder, it does not call anything.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

# Every provider DiscoveryGram knows how to talk to.
KNOWN_PROVIDERS = (
    "nvidia",
    "openrouter",
    "groq",
    "gemini",
    "cloudflare",
    "cerebras",
    "mistral",
    "puter",
    "ollama",
)

# Providers that need no API key: they are reachable without credentials.
KEYLESS_PROVIDERS = frozenset({"ollama"})


class TaskProfile(StrEnum):
    """What a request needs from a model."""

    CHAT = "chat"
    VISION = "vision"


@dataclass(frozen=True)
class ProviderConfig:
    """One provider's credentials and its ordered model preferences."""

    name: str
    api_key: str = ""
    base_url: str = ""
    models: tuple[str, ...] = field(default_factory=tuple)
    vision_models: tuple[str, ...] = field(default_factory=tuple)

    def models_for(self, task: TaskProfile) -> tuple[str, ...]:
        return self.vision_models if task is TaskProfile.VISION else self.models

    @property
    def has_credentials(self) -> bool:
        return bool(self.api_key) or self.name in KEYLESS_PROVIDERS


@dataclass(frozen=True)
class Attempt:
    """One rung of the ladder: a provider paired with a specific model."""

    provider: str
    model: str

    def __str__(self) -> str:
        return f"{self.provider}/{self.model}"


def _split_csv(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def load_provider_configs(env: Mapping[str, str] | None = None) -> dict[str, ProviderConfig]:
    """Read `<PROVIDER>_*` variables into one config per known provider.

    `<P>_MODELS` and `<P>_VISION_MODELS` are ordered, comma-separated lists:
    the order is the order they will be tried in.
    """
    source: Mapping[str, str] = env if env is not None else os.environ

    configs: dict[str, ProviderConfig] = {}
    for name in KNOWN_PROVIDERS:
        prefix = name.upper()
        configs[name] = ProviderConfig(
            name=name,
            api_key=source.get(f"{prefix}_API_KEY", "").strip(),
            base_url=source.get(f"{prefix}_BASE_URL", "").strip(),
            models=_split_csv(source.get(f"{prefix}_MODELS")),
            vision_models=_split_csv(source.get(f"{prefix}_VISION_MODELS")),
        )
    return configs


def build_attempt_ladder(
    chain: list[str],
    configs: Mapping[str, ProviderConfig],
    task: TaskProfile,
) -> tuple[list[Attempt], list[str]]:
    """Expand a provider chain into the ordered list of (provider, model) attempts.

    Returns the ladder and a list of human-readable reasons for every provider
    that was skipped, so startup can log exactly why a chain is shorter than
    the operator expected.
    """
    ladder: list[Attempt] = []
    skipped: list[str] = []

    for provider_name in chain:
        config = configs.get(provider_name)

        if config is None:
            skipped.append(f"{provider_name}: unknown provider")
            continue

        if not config.has_credentials:
            skipped.append(f"{provider_name}: no API key configured")
            continue

        models = config.models_for(task)
        if not models:
            suffix = "VISION_MODELS" if task is TaskProfile.VISION else "MODELS"
            variable = f"{provider_name.upper()}_{suffix}"
            skipped.append(f"{provider_name}: no {task.value} model listed in {variable}")
            continue

        ladder.extend(Attempt(provider=provider_name, model=model) for model in models)

    return ladder, skipped
