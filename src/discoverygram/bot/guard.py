"""The allow-list.

This runs **before every other handler**, in handler group -1, and stops the
update dead when the caller is not allow-listed. It is registered as a
`TypeHandler(Update, ...)` rather than checked inside each handler, because a
check that has to be remembered in twenty places is a check that will be
forgotten in one.

A rejected user is told they are not authorised **and given their own Telegram
id**, because that is the number an operator needs to add them to
`TELEGRAM_ALLOWED_USER_IDS` — and it is information the user already has about
themselves. Nothing about the vault, the instance or the allow-list is revealed.

The refusal is sent once per user per session TTL. Without that, an unknown
account could keep the bot replying to it indefinitely.
"""

from __future__ import annotations

from telegram import Update
from telegram.ext import ApplicationHandlerStop, ContextTypes

from discoverygram.bot.deps import deps_of
from discoverygram.util import metrics
from discoverygram.util.correlation import set_correlation_id
from discoverygram.util.logging import get_logger

log = get_logger(__name__)

REFUSAL = (
    "You are not authorised to use this bot.\n\n"
    "If you should be, ask the operator to add your Telegram id to the allow-list:\n"
    "{user_id}"
)

_DENIED_KEY = "denied:{user_id}"


async def enforce_allow_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reject non-allow-listed callers before any handler sees the update."""
    deps = deps_of(context)
    settings = deps.settings

    # One id per user action, carried through NoteDiscovery and LLM calls, so a
    # single user tap can be followed end to end in the logs.
    set_correlation_id()

    user = update.effective_user
    chat = update.effective_chat
    user_id = user.id if user else None
    chat_id = chat.id if chat else None

    if settings.is_user_allowed(user_id) and settings.is_chat_allowed(chat_id):
        deps.count("updates_accepted")
        metrics.UPDATES.inc(outcome="accepted")
        return

    deps.count("updates_rejected")
    metrics.UPDATES.inc(outcome="rejected")
    log.warning(
        "update_rejected",
        user_id=user_id,
        chat_id=chat_id,
        username=user.username if user else None,
        reason="user" if not settings.is_user_allowed(user_id) else "chat",
    )

    await _refuse_once(update, context, user_id)
    raise ApplicationHandlerStop


async def _refuse_once(
    update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int | None
) -> None:
    """Answer a stranger at most once per session TTL."""
    if update.effective_chat is None:
        return

    deps = deps_of(context)
    key = _DENIED_KEY.format(user_id=user_id)
    if await deps.sessions.get(key) is not None:
        return
    await deps.sessions.set(key, {"notified": True})

    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=REFUSAL.format(user_id=user_id if user_id is not None else "unknown"),
    )


__all__ = ["REFUSAL", "enforce_allow_list"]
