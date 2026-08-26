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
    """What a request needs from a model.

    Four tasks, two capabilities. `TITLE` and `SUMMARISE` are ordinary chat
    calls with different sampling settings, so they draw on the chat chain and
    the chat model lists — an operator configures two chains, not four.
    """

    CHAT = "chat"
    VISION = "vision"
    TITLE = "title"
    SUMMARISE = "summarise"

    @property
    def requires_vision(self) -> bool:
        """Whether serving this task means sending an image to the provider."""
        return self is TaskProfile.VISION


@dataclass(frozen=True, slots=True)
class Generation:
    """Sampling settings for a task.

    A title wants to be short and nearly deterministic; a chat reply wants
    room and some warmth. Keeping these next to the profile means a caller
    asks for a *task* and never has to remember the numbers.
    """

    max_tokens: int
    temperature: float


# Defaults per task. Callers may override both at the call site.
TASK_DEFAULTS: dict[TaskProfile, Generation] = {
    TaskProfile.CHAT: Generation(max_tokens=1024, temperature=0.7),
    TaskProfile.VISION: Generation(max_tokens=1536, temperature=0.2),
    TaskProfile.TITLE: Generation(max_tokens=48, temperature=0.1),
    TaskProfile.SUMMARISE: Generation(max_tokens=512, temperature=0.3),
}


@dataclass(frozen=True)
class ProviderConfig:
    """One provider's credentials and its ordered model preferences."""

    name: str
    api_key: str = ""
    base_url: str = ""
    # Cloudflare Workers AI puts the account id in the URL path. Read
    # generically as `<P>_ACCOUNT_ID` rather than special-cased, so the loader
    # stays one loop over the known providers.
    account_id: str = ""
    models: tuple[str, ...] = field(default_factory=tuple)
    vision_models: tuple[str, ...] = field(default_factory=tuple)

    def models_for(self, task: TaskProfile) -> tuple[str, ...]:
        return self.vision_models if task.requires_vision else self.models

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
            account_id=source.get(f"{prefix}_ACCOUNT_ID", "").strip(),
            models=_split_csv(source.get(f"{prefix}_MODELS")),
            vision_models=_split_csv(source.get(f"{prefix}_VISION_MODELS")),
        )
    return configs


def build_attempt_ladder(
    chain: list[str],
    configs: Mapping[str, ProviderConfig],
    task: TaskProfile,
    *,
    capabilities: Mapping[str, bool] | None = None,
) -> tuple[list[Attempt], list[str]]:
    """Expand a provider chain into the ordered list of (provider, model) attempts.

    Returns the ladder and a list of human-readable reasons for every provider
    that was skipped, so startup can log exactly why a chain is shorter than
    the operator expected.

    `capabilities` maps provider name to "this adapter can send images". It is
    consulted only for vision tasks, and only when supplied: the ladder is
    also built before any client exists (`make check-env`), where the honest
    answer is that capability is unknown rather than false.
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

        if (
            task.requires_vision
            and capabilities is not None
            and not capabilities.get(provider_name, False)
        ):
            skipped.append(f"{provider_name}: this provider cannot accept images")
            continue

        models = config.models_for(task)
        if not models:
            suffix = "VISION_MODELS" if task.requires_vision else "MODELS"
            variable = f"{provider_name.upper()}_{suffix}"
            skipped.append(f"{provider_name}: no {task.value} model listed in {variable}")
            continue

        ladder.extend(Attempt(provider=provider_name, model=model) for model in models)

    return ladder, skipped
