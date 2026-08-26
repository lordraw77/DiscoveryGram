"""Opaque callback tokens.

Telegram limits `callback_data` to **64 bytes**. A note path alone can exceed
that, and a search cursor certainly does. So the button carries an action name
and a short token; the real payload lives in the session store.

    callback_data = "open:9f3a1c2b7d4e"
                     ^^^^ ^^^^^^^^^^^^
                     action  token -> session store -> {"path": "Projects/…"}

The action stays in the callback data on purpose: a handler can route on it
without a store round trip, and an expired token still tells the user *which*
button went stale rather than "something expired".

Tokens are random, not derived from the payload. Two users opening the same note
get different tokens, so one user's callback data reveals nothing about another's
and nothing about the vault's contents.
"""

from __future__ import annotations

import secrets
from typing import Final

from discoverygram.ports.session_store import SessionStore, SessionValue

# Telegram's hard limit.
CALLBACK_DATA_MAX_BYTES: Final = 64
SEPARATOR: Final = ":"
TOKEN_BYTES: Final = 6  # 12 hex characters — 48 bits, ample against guessing
_KEY_PREFIX: Final = "cb:"

# Longest action name that still fits with a token and separator.
MAX_ACTION_LENGTH: Final = CALLBACK_DATA_MAX_BYTES - (TOKEN_BYTES * 2) - len(SEPARATOR)


class CallbackDataTooLongError(ValueError):
    """An action name that cannot fit in Telegram's callback data."""


class CallbackTokens:
    """Issues and resolves the tokens behind inline-keyboard buttons."""

    def __init__(self, store: SessionStore, *, ttl_s: int) -> None:
        self._store = store
        self._ttl_s = ttl_s

    @staticmethod
    def _key(token: str) -> str:
        return f"{_KEY_PREFIX}{token}"

    async def issue(self, action: str, payload: SessionValue | None = None) -> str:
        """Store `payload` and return callback data of the form `action:token`."""
        if not action or SEPARATOR in action:
            raise CallbackDataTooLongError(f"action must be non-empty and contain no {SEPARATOR!r}")
        if len(action.encode()) > MAX_ACTION_LENGTH:
            raise CallbackDataTooLongError(
                f"action {action!r} leaves no room for a token within "
                f"Telegram's {CALLBACK_DATA_MAX_BYTES}-byte callback data"
            )

        token = secrets.token_hex(TOKEN_BYTES)
        await self._store.set(self._key(token), payload or {}, ttl_s=self._ttl_s)
        return f"{action}{SEPARATOR}{token}"

    @staticmethod
    def split(callback_data: str) -> tuple[str, str, list[str]]:
        """Split callback data into `(action, token, args)`.

        Trailing arguments let one stored payload serve many buttons. Pagination
        relies on it: the result set is stored once and every page button reads
        `page:<token>:<n>`, so turning twenty pages creates one session entry
        rather than forty.
        """
        parts = callback_data.split(SEPARATOR)
        action = parts[0] if parts else ""
        token = parts[1] if len(parts) > 1 else ""
        return action, token, parts[2:]

    @classmethod
    def parse(cls, callback_data: str) -> tuple[str, str]:
        """Split callback data into `(action, token)`; the token may be empty."""
        action, token, _ = cls.split(callback_data)
        return action, token

    async def resolve(self, callback_data: str) -> SessionValue | None:
        """The payload behind a button, or `None` when the token has expired."""
        _, token = self.parse(callback_data)
        if not token:
            return None
        return await self._store.get(self._key(token))

    @staticmethod
    def with_args(callback_data: str, *args: object) -> str:
        """Append arguments to an issued button, e.g. a page number.

        Raises rather than producing data Telegram will reject, so the failure
        lands where the bug is instead of as a 400 in front of the user.
        """
        extended = SEPARATOR.join([callback_data, *(str(arg) for arg in args)])
        if not fits_in_callback_data(extended):
            raise CallbackDataTooLongError(
                f"{extended!r} exceeds Telegram's {CALLBACK_DATA_MAX_BYTES}-byte limit"
            )
        return extended

    async def revoke(self, callback_data: str) -> None:
        """Invalidate a one-shot button, so a double tap cannot act twice."""
        _, token = self.parse(callback_data)
        if token:
            await self._store.delete(self._key(token))

    async def extend(self, callback_data: str) -> bool:
        """Refresh a token's lifetime because the user is still using it.

        Paging through results should not expire mid-flow just because the first
        page was issued an hour ago.
        """
        _, token = self.parse(callback_data)
        if not token:
            return False
        payload = await self._store.get(self._key(token))
        if payload is None:
            return False
        await self._store.set(self._key(token), payload, ttl_s=self._ttl_s)
        return True


def fits_in_callback_data(value: str) -> bool:
    """Whether a string is legal as Telegram `callback_data`."""
    return 0 < len(value.encode()) <= CALLBACK_DATA_MAX_BYTES


__all__: list[str] = [
    "CALLBACK_DATA_MAX_BYTES",
    "MAX_ACTION_LENGTH",
    "CallbackDataTooLongError",
    "CallbackTokens",
    "fits_in_callback_data",
]
