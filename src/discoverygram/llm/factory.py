"""Building the router from settings — the one place a provider is chosen.

Two decisions live here and nowhere else:

* **which clients exist.** Only providers that actually appear in a configured
  chain are built, so an operator with nine keys in `.env` and two providers in
  their chains opens two connection pools, not nine.
* **which rungs survive.** Capability is checked *at build time*, not on the
  first photo: a text-only provider is dropped from the vision ladder with a
  reason an operator can read in the startup log.

A provider whose client cannot be constructed at all — Cloudflare without an
account id — is skipped with its reason rather than raising. One
misconfigured provider degrades the ladder; it does not stop the bot.
"""

from __future__ import annotations

from discoverygram.config import Settings
from discoverygram.llm.base import OPENAI_COMPATIBLE, OpenAiCompatibleClient
from discoverygram.llm.breaker import CircuitBreaker
from discoverygram.llm.cloudflare import CloudflareClient
from discoverygram.llm.gemini import GeminiClient
from discoverygram.llm.plan import ProviderConfig, TaskProfile
from discoverygram.llm.puter import PuterClient
from discoverygram.llm.router import LlmRouter, TaskLadder
from discoverygram.llm.usage import DailyCallCap, UsageLedger
from discoverygram.ports.llm import LlmClient
from discoverygram.util.logging import get_logger

log = get_logger(__name__)

# The tasks whose ladders are built at startup. `TITLE` and `SUMMARISE` share
# the chat ladder, so building it once is building it for all three.
ROUTED_TASKS = (TaskProfile.CHAT, TaskProfile.VISION)


def provider_supports_vision(name: str) -> bool:
    """Whether this provider's adapter can carry an image, without building it.

    Capability is a fact about the adapter, not about a live connection, so
    `make check-env` can report the same vision ladder the router will walk —
    without credentials, a network, or a client to close afterwards.
    """
    profile = OPENAI_COMPATIBLE.get(name)
    if profile is not None:
        return profile.vision
    # The dialect adapters all carry images; an unknown provider is treated as
    # OpenAI-compatible, and those default to capable.
    return True


def build_client(config: ProviderConfig, *, timeout_s: float = 60.0) -> LlmClient:
    """One client for one provider. Raises `ValueError` on a configuration gap."""
    if config.name == "gemini":
        return GeminiClient(config, timeout_s=timeout_s)
    if config.name == "cloudflare":
        return CloudflareClient(config, timeout_s=timeout_s)
    if config.name == "puter":
        return PuterClient(config, timeout_s=timeout_s)
    if config.name in OPENAI_COMPATIBLE or config.base_url:
        return OpenAiCompatibleClient(config, timeout_s=timeout_s)
    raise ValueError(
        f"provider '{config.name}' has no adapter and no {config.name.upper()}_BASE_URL "
        f"to treat it as OpenAI-compatible"
    )


def build_clients(settings: Settings) -> tuple[dict[str, LlmClient], list[str]]:
    """Clients for every provider named in a chain, plus reasons for the rest."""
    configs = settings.provider_configs()
    wanted: list[str] = []
    for name in [*settings.llm_chain_chat, *settings.llm_chain_vision]:
        if name not in wanted:
            wanted.append(name)

    clients: dict[str, LlmClient] = {}
    problems: list[str] = []

    for name in wanted:
        config = configs.get(name)
        if config is None:
            problems.append(f"{name}: unknown provider")
            continue
        if not config.has_credentials:
            # Already reported by the ladder; not repeated as a problem here.
            continue
        try:
            clients[name] = build_client(config, timeout_s=settings.llm_request_timeout_s)
        except ValueError as exc:
            problems.append(f"{name}: {exc}")
            log.warning("llm_client_not_built", provider=name, reason=str(exc))

    return clients, problems


def build_router(settings: Settings) -> LlmRouter:
    """Assemble the router: clients, ladders, breaker, ledger and cap.

    Never raises on a bad provider. An empty ladder is a perfectly valid state
    — it is what a bot with no LLM credentials has — and the commands that need
    one refuse with the reason rather than the bot refusing to start.
    """
    clients, problems = build_clients(settings)
    capabilities = {name: client.supports_vision() for name, client in clients.items()}

    ladders: dict[TaskProfile, TaskLadder] = {}
    for task in ROUTED_TASKS:
        attempts, skipped = settings.attempt_ladder(task, capabilities=capabilities)
        # A rung whose client was never built is unreachable; drop it here so
        # the ladder length reported by `/status` is the length that will
        # actually be walked.
        usable = [attempt for attempt in attempts if attempt.provider in clients]
        dropped = {attempt.provider for attempt in attempts if attempt.provider not in clients}
        reasons = [*skipped, *(problem for problem in problems if _names(problem, dropped))]

        ladder = TaskLadder(task=task, attempts=tuple(usable), skipped=tuple(reasons))
        ladders[task] = ladder

        log.info(
            "llm_ladder_built",
            task=task.value,
            rungs=[str(attempt) for attempt in ladder.attempts],
            skipped=list(ladder.skipped),
        )
        if not ladder.usable:
            log.warning(
                "llm_ladder_empty",
                task=task.value,
                hint=f"{'LLM_CHAIN_VISION' if task.requires_vision else 'LLM_CHAIN_CHAT'} "
                f"produced no usable (provider, model) pair.",
            )

    # TITLE and SUMMARISE are chat-capability tasks: they walk the same rungs.
    chat_ladder = ladders[TaskProfile.CHAT]
    for task in (TaskProfile.TITLE, TaskProfile.SUMMARISE):
        ladders[task] = TaskLadder(
            task=task, attempts=chat_ladder.attempts, skipped=chat_ladder.skipped
        )

    return LlmRouter(
        settings,
        clients,
        ladders,
        breaker=CircuitBreaker(
            failure_threshold=settings.llm_circuit_failure_threshold,
            reset_s=settings.llm_circuit_reset_s,
        ),
        ledger=UsageLedger(),
        cap=DailyCallCap(settings.llm_daily_call_limit_per_user),
    )


def _names(problem: str, providers: set[str]) -> bool:
    return any(problem.startswith(f"{provider}:") for provider in providers)


__all__ = [
    "ROUTED_TASKS",
    "build_client",
    "build_clients",
    "build_router",
    "provider_supports_vision",
]
