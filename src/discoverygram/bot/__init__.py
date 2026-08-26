"""The Telegram presentation layer.

Handlers never call an adapter directly: they go through the application layer
and reach their collaborators via `BotDeps`.
"""

from discoverygram.bot.application import BotRunner, build_application, build_deps
from discoverygram.bot.deps import BotDeps, deps_of
from discoverygram.bot.tokens import CallbackTokens

__all__ = [
    "BotDeps",
    "BotRunner",
    "CallbackTokens",
    "build_application",
    "build_deps",
    "deps_of",
]
