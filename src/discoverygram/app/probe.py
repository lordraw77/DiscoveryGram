"""Startup probe.

Two facts decide which commands the bot may offer, and both are only knowable
from the live instance: whether it answers at all, and whether search is enabled.
An instance with `search.enabled: false` returns **403** from `/api/search`, so
the search commands are disabled cleanly at startup rather than failing once per
user request.

The probe never raises: an unreachable instance degrades the bot, it does not
stop it from starting. `/readyz` reports the truth either way.
"""

from __future__ import annotations

from dataclasses import dataclass

from discoverygram.ports.errors import NoteStoreError
from discoverygram.ports.model import InstanceConfig
from discoverygram.ports.note_store import NoteStore
from discoverygram.util.logging import get_logger

log = get_logger(__name__)

# The contract in docs/notediscovery-contract.md was extracted from this version.
KNOWN_GOOD_VERSION = "0.31.3"


@dataclass(frozen=True, slots=True)
class InstanceState:
    """What the bot knows about the instance it is attached to."""

    config: InstanceConfig
    healthy: bool

    @property
    def search_available(self) -> bool:
        return self.healthy and self.config.search_enabled

    @property
    def version_matches_contract(self) -> bool:
        return self.config.version == KNOWN_GOOD_VERSION

    def why_search_unavailable(self) -> str:
        """A message fit to send to a user, or `""` when search works."""
        if not self.healthy:
            return "The notes instance is not reachable right now."
        if not self.config.search_enabled:
            return "Search is disabled on this NoteDiscovery instance."
        return ""


async def probe_instance(store: NoteStore) -> InstanceState:
    """Ask the instance who it is and whether search works."""
    healthy = await store.health()

    try:
        config = await store.get_config()
    except NoteStoreError as exc:
        log.warning("instance_config_unavailable", error=str(exc))
        # Assume search works: a probe failure must not silently disable a
        # feature that may be perfectly fine.
        config = InstanceConfig(reachable=healthy)

    state = InstanceState(config=config, healthy=healthy)

    log.info(
        "instance_probed",
        healthy=healthy,
        name=config.name,
        version=config.version,
        search_enabled=config.search_enabled,
        auth_enabled=config.auth_enabled,
        min_query_length=config.min_query_length,
    )

    if healthy and not state.version_matches_contract:
        log.warning(
            "instance_version_mismatch",
            found=config.version,
            contract=KNOWN_GOOD_VERSION,
            hint="Re-verify docs/notediscovery-contract.md against this version.",
        )
    if healthy and not config.search_enabled:
        log.warning("search_disabled_server_side", hint="/search and /find will be refused.")

    return state
