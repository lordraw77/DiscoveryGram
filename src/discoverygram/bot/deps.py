"""What every handler needs, assembled once at startup.

python-telegram-bot hands handlers a `ContextTypes.DEFAULT_TYPE` and nothing
else, so the application's collaborators have to travel with it. They live in
`application.bot_data` under one key and come back out through `deps_of`,
typed — rather than each handler reaching into a dict and hoping.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from discoverygram.app.probe import InstanceState
from discoverygram.bot.tokens import CallbackTokens
from discoverygram.config import Settings
from discoverygram.llm.router import LlmRouter
from discoverygram.ports.note_store import NoteStore
from discoverygram.ports.session_store import SessionStore

DEPS_KEY = "discoverygram_deps"


@dataclass(slots=True)
class BotDeps:
    """The collaborators a handler is allowed to reach for."""

    settings: Settings
    notes: NoteStore
    sessions: SessionStore
    tokens: CallbackTokens
    # Mutable on purpose: a re-probe after the instance comes back replaces it,
    # and handlers must see the new state without being rebuilt.
    instance: InstanceState
    # Optional on purpose: a bot with no provider credentials is a supported
    # configuration — it is milestone M1 — and the LLM-backed commands refuse
    # with a reason rather than the bot refusing to start.
    llm: LlmRouter | None = None
    started_at: float = 0.0
    counters: dict[str, int] = field(default_factory=dict)

    def count(self, name: str) -> None:
        self.counters[name] = self.counters.get(name, 0) + 1


def deps_of(context: Any) -> BotDeps:
    """The dependencies bound to this application.

    Raises rather than returning `None`: a handler running without them is a
    wiring bug, and failing loudly at the first call beats a `None` propagating
    into a message the user sees.
    """
    deps = context.bot_data.get(DEPS_KEY)
    if not isinstance(deps, BotDeps):
        raise RuntimeError("BotDeps are not attached to this application")
    return deps


__all__ = ["DEPS_KEY", "BotDeps", "deps_of"]
