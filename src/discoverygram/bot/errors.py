"""The global error handler.

One rule: **the user gets a sentence, the log gets everything**. A stack trace
in a chat is useless to the person reading it and tells anyone who provoked it
more about the deployment than they should know.

Errors the adapters already classified — `NoteStoreError` and `LlmError` and
their subclasses — map to a specific, actionable message. Anything else is a
bug, and says so.
"""

from __future__ import annotations

from telegram import Update
from telegram.error import BadRequest, TelegramError
from telegram.error import Forbidden as TelegramForbidden

from discoverygram.bot.deps import deps_of
from discoverygram.ports.errors import (
    Forbidden,
    InvalidRequest,
    NoteStoreError,
    NotFound,
    RateLimited,
    Unauthorized,
    Unavailable,
    Unsupported,
)
from discoverygram.ports.llm_errors import (
    LlmError,
    LlmNoProvider,
    LlmQuotaExceeded,
)
from discoverygram.util.logging import get_logger
from discoverygram.util.paths import InvalidPath

log = get_logger(__name__)

GENERIC = "Something went wrong on my side. It has been logged — please try again."

UNAVAILABLE = "I cannot reach your notes right now. Try again in a moment."
UNAUTHORIZED = (
    "Your notes instance rejected my credentials. The operator needs to check the API key."
)


def user_message(error: BaseException) -> str:
    """The one sentence the user sees for a given failure."""
    if isinstance(error, InvalidPath):
        return f"That path is not usable: {error}"

    if isinstance(error, RateLimited):
        if error.retry_after:
            wait = int(error.retry_after)
            return f"Your notes instance is rate-limiting me. Try again in {wait}s."
        return "Your notes instance is rate-limiting me. Give it a moment and try again."

    if isinstance(error, NotFound):
        return f"I could not find that: {error}"
    if isinstance(error, Unauthorized):
        return UNAUTHORIZED
    if isinstance(error, Forbidden):
        return f"Your notes instance refused that: {error}"
    if isinstance(error, Unsupported):
        return str(error)
    if isinstance(error, InvalidRequest):
        return f"That request was rejected: {error}"
    if isinstance(error, Unavailable):
        return UNAVAILABLE
    if isinstance(error, NoteStoreError):
        return f"Your notes instance had a problem: {error}"

    # The daily cap is the user's own budget, so it is stated plainly rather
    # than dressed up as a failure — nothing is broken.
    if isinstance(error, LlmQuotaExceeded):
        return str(error)
    # A missing chain is a configuration gap, and the message already names the
    # variable to set; repeating it as "something went wrong" would hide that.
    if isinstance(error, LlmNoProvider):
        return str(error)
    if isinstance(error, LlmError):
        return (
            "The AI providers could not answer that. Check /status to see which ones are degraded."
        )

    return GENERIC


def is_expected(error: BaseException) -> bool:
    """Whether the failure is a handled condition rather than a defect.

    Expected failures are logged at warning with no traceback; everything else
    is a bug and gets the full exception.
    """
    return isinstance(error, NoteStoreError | InvalidPath | LlmError)


async def handle_error(update: object, context: object) -> None:
    """Application-wide error callback.

    Signature is loose because python-telegram-bot passes the raw update, which
    may not be an `Update` at all when the failure happened outside a handler.
    """
    error = getattr(context, "error", None)
    if error is None:
        return

    chat = update.effective_chat if isinstance(update, Update) else None
    user = update.effective_user if isinstance(update, Update) else None
    chat_id = chat.id if chat else None
    user_id = user.id if user else None

    if is_expected(error):
        log.warning(
            "handler_failed",
            error=str(error),
            error_type=type(error).__name__,
            user_id=user_id,
            chat_id=chat_id,
        )
    else:
        log.error(
            "handler_crashed",
            error=str(error),
            error_type=type(error).__name__,
            user_id=user_id,
            chat_id=chat_id,
            exc_info=error,
        )

    try:
        deps = deps_of(context)
    except RuntimeError:
        deps = None
    if deps is not None:
        deps.count("errors")

    await _notify(update, context, user_message(error))


async def _notify(update: object, context: object, text: str) -> None:
    """Tell the user, without letting the notification itself become an error.

    A blocked bot or a deleted chat raises from `send_message`; that must not
    re-enter the error handler and loop.
    """
    if not isinstance(update, Update) or update.effective_chat is None:
        return
    bot = getattr(context, "bot", None)
    if bot is None:
        return

    try:
        await bot.send_message(chat_id=update.effective_chat.id, text=text)
    except (BadRequest, TelegramForbidden) as exc:
        log.warning("error_notice_undeliverable", error=str(exc))
    except TelegramError as exc:
        log.warning("error_notice_failed", error=str(exc))


__all__ = ["GENERIC", "handle_error", "is_expected", "user_message"]
