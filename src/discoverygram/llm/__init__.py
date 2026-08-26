"""LLM router: task profiles, provider chains, retry, failover, circuit breaker.

Only `plan` is re-exported here. `factory` and `router` import `Settings`, and
`Settings` imports `plan` to build its ladders — so re-exporting them from the
package would close an import cycle through `discoverygram.config`. Callers
that need the router import `discoverygram.llm.factory` or
`discoverygram.llm.router` directly, which is also where the module docstrings
explaining them live.
"""

from discoverygram.llm.plan import (
    KNOWN_PROVIDERS,
    TASK_DEFAULTS,
    Attempt,
    Generation,
    ProviderConfig,
    TaskProfile,
    build_attempt_ladder,
    load_provider_configs,
)

__all__ = [
    "KNOWN_PROVIDERS",
    "TASK_DEFAULTS",
    "Attempt",
    "Generation",
    "ProviderConfig",
    "TaskProfile",
    "build_attempt_ladder",
    "load_provider_configs",
]
