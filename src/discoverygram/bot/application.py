"""Building and running the Telegram application.

DiscoveryGram already owns its event loop — the health server runs in it and
SIGTERM is handled there — so `Application.run_polling()`, which takes over the
loop and installs its own signal handlers, is the wrong entry point. The
lifecycle is driven manually instead:

    initialize -> start -> updater.start_polling/start_webhook
                        ...
    updater.stop -> stop -> shutdown

Handler ordering matters and is deliberate:

    group -1   the allow-list, which stops non-listed updates dead
    group  0   commands, callbacks, plain text
"""

from __future__ import annotations

import time

from telegram import Update
from telegram.ext import (
    AIORateLimiter,
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

from discoverygram.app.probe import InstanceState
from discoverygram.bot import browse as browse_handlers
from discoverygram.bot import create as create_handlers
from discoverygram.bot import drafts
from discoverygram.bot import search as search_handlers
from discoverygram.bot.commands import COMMAND_MENU, COMMANDS, unknown_command
from discoverygram.bot.deps import DEPS_KEY, BotDeps
from discoverygram.bot.errors import handle_error
from discoverygram.bot.guard import enforce_allow_list
from discoverygram.bot.tokens import CallbackTokens
from discoverygram.config import Settings, TelegramMode
from discoverygram.llm.router import LlmRouter
from discoverygram.ports.note_store import NoteStore
from discoverygram.ports.session_store import SessionStore
from discoverygram.util.logging import get_logger

log = get_logger(__name__)

# Update types we ask Telegram for. Narrower than ALL_TYPES on purpose: the bot
# has no use for polls or chat-member changes, and not receiving them is cheaper
# and quieter than discarding them.
ALLOWED_UPDATES = [
    Update.MESSAGE,
    Update.EDITED_MESSAGE,
    Update.CALLBACK_QUERY,
]

# Sized against concurrent_updates so a burst of handlers cannot starve the
# connection pool and start timing out on its own requests.
CONCURRENT_UPDATES = 16
CONNECTION_POOL_SIZE = 32


def build_deps(
    settings: Settings,
    notes: NoteStore,
    sessions: SessionStore,
    instance: InstanceState,
    llm: LlmRouter | None = None,
) -> BotDeps:
    return BotDeps(
        settings=settings,
        notes=notes,
        sessions=sessions,
        tokens=CallbackTokens(sessions, ttl_s=settings.session_ttl_s),
        instance=instance,
        llm=llm,
        started_at=time.monotonic(),
    )


async def _noop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Swallow the inert page-counter button.

    Telegram requires callback data on every button and shows a spinner until
    the query is answered, so the counter in a pagination row needs a handler
    even though it does nothing.
    """
    del context
    if update.callback_query:
        await update.callback_query.answer()


async def _post_init(application: Application) -> None:  # type: ignore[type-arg]
    """Publish the command menu once the bot is initialised."""
    await application.bot.set_my_commands(COMMAND_MENU)
    log.info("command_menu_published", commands=len(COMMAND_MENU))


def build_application(deps: BotDeps) -> Application:  # type: ignore[type-arg]
    """Assemble the application, its handlers and its dependencies."""
    settings = deps.settings

    builder: ApplicationBuilder = (  # type: ignore[type-arg]
        ApplicationBuilder()
        .token(settings.telegram_bot_token)
        # Respects Telegram's flood limits for us, including the per-chat ones.
        .rate_limiter(AIORateLimiter())
        .concurrent_updates(CONCURRENT_UPDATES)
        .connection_pool_size(CONNECTION_POOL_SIZE)
        .pool_timeout(30.0)
        .post_init(_post_init)
    )
    application = builder.build()
    application.bot_data[DEPS_KEY] = deps

    # Group -1: nothing else runs until the caller is known to be allowed.
    application.add_handler(TypeHandler(Update, enforce_allow_list), group=-1)

    for name, handler in {
        **COMMANDS,
        **search_handlers.COMMANDS,
        **browse_handlers.COMMANDS,
        **create_handlers.COMMANDS,
    }.items():
        application.add_handler(CommandHandler(name, handler))

    for action, callback in (
        (search_handlers.PAGE_ACTION, search_handlers.page_callback),
        (browse_handlers.NAV_ACTION, browse_handlers.nav_callback),
        (browse_handlers.NOTE_ACTION, browse_handlers.note_callback),
        (browse_handlers.ACT_ACTION, browse_handlers.act_callback),
        (drafts.DRAFT_ACTION, create_handlers.draft_callback),
        (create_handlers.TEMPLATE_ACTION, create_handlers.template_callback),
    ):
        application.add_handler(CallbackQueryHandler(callback, pattern=rf"^{action}:"))
    application.add_handler(CallbackQueryHandler(_noop_callback, pattern=r"^noop:"))

    # Photos and image documents enter the capture pipeline. Registered before
    # the text handlers because a captioned photo is a photo, not a message.
    application.add_handler(
        MessageHandler(filters.PHOTO | filters.Document.IMAGE, create_handlers.attachment_message)
    )

    # Order matters, three times over. The command filter has to be offered the
    # update before any text handler, or every `/whatever` would become a
    # search. A pending draft or edit has to claim the message before anything
    # else, or text meant as a title would also be run as a query. And quick
    # capture has to precede search, because with DEFAULT_TEXT_ACTION=quick a
    # message the user meant to keep must not quietly become a query.
    application.add_handler(MessageHandler(filters.COMMAND, unknown_command))
    # A forward is someone else's words: it goes to a preview, because the title
    # and the path are exactly what the user has not decided yet.
    application.add_handler(
        MessageHandler(
            filters.FORWARDED & filters.TEXT & ~filters.COMMAND, create_handlers.forwarded_message
        )
    )
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, create_handlers.pending_input)
    )
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, browse_handlers.pending_input)
    )
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, create_handlers.default_text_capture)
    )
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, search_handlers.text_message)
    )

    application.add_error_handler(handle_error)
    return application


class BotRunner:
    """Starts and stops the Telegram application inside our own event loop."""

    def __init__(self, application: Application, settings: Settings) -> None:  # type: ignore[type-arg]
        self._application = application
        self._settings = settings
        self._running = False

    @property
    def application(self) -> Application:  # type: ignore[type-arg]
        return self._application

    @property
    def is_running(self) -> bool:
        return self._running

    async def start(self) -> None:
        await self._application.initialize()
        await self._application.start()

        updater = self._application.updater
        if updater is None:  # pragma: no cover - only with a custom builder
            raise RuntimeError("The application was built without an updater")

        if self._settings.telegram_mode is TelegramMode.WEBHOOK:
            await self._start_webhook(updater)
        else:
            await updater.start_polling(
                allowed_updates=ALLOWED_UPDATES,
                # Updates queued while the bot was down are stale by the time it
                # returns, and replaying them would act on a user's intent from
                # an hour ago without them asking again.
                drop_pending_updates=True,
            )
            log.info("telegram_polling_started")

        self._running = True

    async def _start_webhook(self, updater: object) -> None:
        settings = self._settings
        url = settings.telegram_webhook_url.rstrip("/")
        path = settings.telegram_webhook_path.strip("/")

        await updater.start_webhook(  # type: ignore[attr-defined]
            listen=settings.telegram_webhook_listen,
            port=settings.telegram_webhook_port,
            url_path=path,
            webhook_url=f"{url}/{path}" if path else url,
            # Telegram signs every delivery with this, so a request that reaches
            # the port from somewhere else is rejected before it is parsed.
            secret_token=settings.telegram_webhook_secret or None,
            allowed_updates=ALLOWED_UPDATES,
            drop_pending_updates=True,
        )
        log.info(
            "telegram_webhook_started",
            port=settings.telegram_webhook_port,
            path=path,
            secret_configured=bool(settings.telegram_webhook_secret),
        )

    async def stop(self) -> None:
        """Reverse of `start`, tolerant of a partially started application."""
        if not self._running:
            return
        self._running = False

        updater = self._application.updater
        if updater is not None and updater.running:
            await updater.stop()
        if self._application.running:
            await self._application.stop()
        await self._application.shutdown()
        log.info("telegram_stopped")


__all__ = [
    "ALLOWED_UPDATES",
    "BotRunner",
    "build_application",
    "build_deps",
]
