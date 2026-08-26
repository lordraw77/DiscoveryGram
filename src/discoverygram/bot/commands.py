"""The core commands: `/start`, `/help`, `/whoami`, `/cancel`, `/status`.

Everything here is reachable in phase 2 with no NoteDiscovery call on the happy
path except `/status`, which is explicitly a diagnostic. Search, browse and
create arrive in phases 3, 4 and 6; `/help` lists only what actually works, so
it never promises a command that answers "unknown command".
"""

from __future__ import annotations

import time
from collections.abc import Callable, Coroutine
from typing import Any

from telegram import BotCommand, Update
from telegram.ext import ContextTypes

from discoverygram import __version__
from discoverygram.bot.deps import BotDeps, deps_of
from discoverygram.bot.render import escape_markdown_v2 as esc
from discoverygram.llm.plan import TaskProfile
from discoverygram.llm.router import LlmRouter
from discoverygram.ports.errors import NoteStoreError
from discoverygram.util.logging import get_logger

log = get_logger(__name__)

# What BotFather shows in the command menu. Extended as later phases land.
COMMAND_MENU = [
    BotCommand("start", "What this bot does"),
    BotCommand("help", "List the available commands"),
    BotCommand("search", "Full-text search across the vault"),
    BotCommand("find", "Literal, case-sensitive search"),
    BotCommand("tag", "Notes by tag, or list every tag"),
    BotCommand("recent", "Recently changed notes"),
    BotCommand("browse", "Walk the folder tree"),
    BotCommand("open", "Open a note by path"),
    BotCommand("backlinks", "Notes linking to a note"),
    BotCommand("related", "Notes linked with a note"),
    BotCommand("move", "Move or rename a note"),
    BotCommand("folder", "Create, rename, move or delete a folder"),
    BotCommand("new", "Create a note at a path, or from a template"),
    BotCommand("quick", "Capture a thought into today's inbox note"),
    BotCommand("template", "Create a note from a template"),
    BotCommand("summarize", "Summarise a note with AI"),
    BotCommand("ask", "Ask a question answered from your notes"),
    BotCommand("status", "Connection, instance and session health"),
    BotCommand("whoami", "Show your Telegram id"),
    BotCommand("cancel", "Abort the current multi-step flow"),
]

HELP_TEXT = """*DiscoveryGram*

Your NoteDiscovery vault, from Telegram.

*Finding notes*
/search <words> — full-text search
/find <text> — literal, case-sensitive match
/tag <name> — notes carrying a tag; /tag alone lists them
/recent [days] — recently changed notes

Any plain message you send is treated as a search — or as a quick capture,
when the operator set `DEFAULT_TEXT_ACTION=quick`.

*Reading and navigating*
/browse [path] — walk the folder tree
/open <path> — open a note
/backlinks <path> — notes linking to it
/related <path> — notes linked with it

Every note carries buttons: Edit, Append, Tag, Backlinks, Related, Path, \
Raw, Share, Delete.

*Creating notes*
/new <path> <text> — create a note
/new --template <name> <path> — create one from a template
/template — pick a template from a list
/quick <text> — capture into today's inbox note

Send a *photo* and I read it, write it up and show you a draft. Add a caption \
to say where it goes and what to generate, for example:
"extract the text, save under Projects/Research, you generate the title".
Several photos sent together become one note.

Nothing is written until you tap *Save* on the draft.

*Changing things*
/move <old> <new> — move or rename a note
/folder new|rename|move|delete — folder operations

*Asking things*
/summarize <path> — summarise a note
/ask <question> — answer from your notes, with the sources

*Everything else*
/help — this list
/status — connection, instance and session health
/whoami — your Telegram id and chat id
/cancel — abort whatever multi-step flow you are in
"""


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    deps = deps_of(context)
    name = update.effective_user.first_name if update.effective_user else "there"
    text = (
        f"Hello {esc(name)}\\.\n\n"
        f"I connect this chat to your NoteDiscovery vault at "
        f"`{esc(_host_of(deps))}`\\.\n\n"
        "Send /help to see what I can do\\."
    )
    await _reply(update, text)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    await _reply(update, _escape_help(HELP_TEXT))


async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """The command that makes populating the allow-list possible."""
    del context
    user = update.effective_user
    chat = update.effective_chat
    lines = [
        f"*Your Telegram id:* `{user.id if user else 'unknown'}`",
        f"*This chat id:* `{chat.id if chat else 'unknown'}`",
    ]
    if user and user.username:
        lines.append(f"*Username:* @{esc(user.username)}")
    await _reply(update, "\n".join(lines))


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Abort a multi-step flow.

    `user_data` is where conversations keep their partial state, so clearing it
    is the whole of "cancel" — and it stays correct as later phases add flows,
    rather than needing a new branch per flow.
    """
    had_state = bool(context.user_data)
    if context.user_data is not None:
        context.user_data.clear()
    message = "Cancelled\\." if had_state else "Nothing to cancel\\."
    await _reply(update, message)


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A diagnostic, and the one place degradation is reported honestly.

    Every literal here goes through the escaper too. `Allow-listed`, `1/3` and a
    trailing full stop all contain MarkdownV2 reserved characters, and one of
    them unescaped means the Bot API rejects the whole message — so no label is
    written straight into the output.
    """
    deps = deps_of(context)
    settings = deps.settings

    healthy = await deps.notes.health()
    sessions_ok = await deps.sessions.ping()
    instance = deps.instance

    lines = [
        _heading("DiscoveryGram status"),
        "",
        _field("Version", _code(__version__)),
        _field("Uptime", _text(_format_uptime(time.monotonic() - deps.started_at))),
        "",
        _heading("NoteDiscovery"),
        _field("Reachable", _tick(healthy), indent=True),
        _field("Transport", _code(settings.notediscovery_transport.value), indent=True),
        _field(
            "Instance",
            _code(f"{instance.config.name} {instance.config.version}"),
            indent=True,
        ),
        _field("Search", _tick(instance.search_available), indent=True),
    ]

    if not instance.version_matches_contract:
        lines.append("  ⚠️ " + _text("Version differs from the documented contract."))

    vault = await _vault_line(deps)
    if vault:
        lines.append(vault)

    lines += [
        "",
        _heading("Bot"),
        _field("Mode", _code(settings.telegram_mode.value), indent=True),
        _field(
            "Sessions", f"{_code(settings.session_backend.value)} {_tick(sessions_ok)}", indent=True
        ),
        _field(
            "Allow-listed users", _text(str(len(settings.telegram_allowed_user_ids))), indent=True
        ),
        _field(
            "Updates",
            _text(
                f"{deps.counters.get('updates_accepted', 0)} accepted, "
                f"{deps.counters.get('updates_rejected', 0)} rejected"
            ),
            indent=True,
        ),
    ]

    lines += _llm_lines(
        deps.llm, user_id=update.effective_user.id if update.effective_user else None
    )

    if not healthy:
        lines += ["", _text("The vault is unreachable, so note commands will fail.")]
    elif not instance.search_available:
        lines += ["", _text(instance.why_search_unavailable())]

    await _reply(update, "\n".join(lines))


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Answer an unrecognised `/command` instead of ignoring it."""
    del context
    await _reply(update, esc("I don't know that command. Try /help."))


# --- Helpers -------------------------------------------------------------


async def _vault_line(deps: BotDeps) -> str:
    """Vault counters, when the instance can supply them.

    `/status` must survive a NoteDiscovery that is down or on a transport that
    cannot answer — MCP has no stats tool at all — so a failure here degrades
    one line rather than the command.
    """
    try:
        stats = await deps.notes.get_stats()
    except NoteStoreError:
        return ""
    return _field(
        "Vault", _text(f"{stats.notes_count} notes, {stats.tags_count} tags"), indent=True
    )


def _llm_lines(router: LlmRouter | None, *, user_id: int | None) -> list[str]:
    """The LLM section of `/status`.

    Reports the two things an operator actually needs when a generation
    command misbehaves: **which rung would serve the next request**, and
    **which providers are currently short-circuited**. A tripped breaker is
    otherwise invisible — the bot just seems slow to fail over — so it is
    named here with its remaining cool-down.

    Every literal goes through the escaper: `half-open`, `3/5` and `(0 left)`
    all carry MarkdownV2 reserved characters, and one unescaped means the Bot
    API rejects the whole message rather than that character.
    """
    if router is None:
        return ["", _heading("AI"), "  " + _text("Not configured.")]

    status = router.status()
    lines = ["", _heading("AI")]

    for task in (TaskProfile.CHAT, TaskProfile.VISION):
        ladder = router.ladder(task)
        if not ladder.usable:
            value = _text("none configured")
        elif len(ladder.attempts) > 1:
            # The first rung is what serves the next request; the count is what
            # says whether there is anything behind it.
            value = _text(f"{ladder.attempts[0]} +{len(ladder.attempts) - 1} more")
        else:
            value = _text(str(ladder.attempts[0]))
        lines.append(_field(task.value.capitalize(), value, indent=True))

    degraded = [circuit for circuit in status.circuits if not circuit.healthy]
    if degraded:
        for circuit in degraded:
            lines.append(
                "  ⚠️ "
                + _text(
                    f"{circuit.provider}: circuit {circuit.state.value}, "
                    f"retrying in {int(circuit.opens_remaining_s)}s"
                )
            )
    elif status.circuits:
        lines.append(_field("Providers", _text("all healthy"), indent=True))

    if status.requests:
        failed = status.requests - status.successful_requests
        lines.append(
            _field(
                "Requests",
                _text(
                    f"{status.successful_requests} served, {failed} failed, "
                    f"{status.attempts} provider calls"
                ),
                indent=True,
            )
        )

    remaining = router.cap.remaining(user_id) if user_id is not None else None
    if remaining is not None:
        lines.append(
            _field(
                "Your daily quota", _text(f"{remaining} of {router.cap.limit} left"), indent=True
            )
        )

    return lines


def _heading(text: str) -> str:
    return f"*{esc(text)}*"


def _field(label: str, value: str, *, indent: bool = False) -> str:
    """`label` is escaped here; `value` must arrive already escaped."""
    prefix = "  " if indent else ""
    return f"{prefix}*{esc(label)}:* {value}"


def _text(value: str) -> str:
    return esc(value)


def _code(value: str) -> str:
    """A code span. Only a backtick needs escaping inside one."""
    return f"`{value.replace('`', '')}`"


def _host_of(deps: BotDeps) -> str:
    url = str(deps.settings.notediscovery_url)
    return url.rstrip("/")


def _tick(value: bool) -> str:
    return "✅" if value else "❌"


def _format_uptime(seconds: float) -> str:
    total = int(max(seconds, 0))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _escape_help(text: str) -> str:
    """Escape the help text while keeping its intentional `*bold*` markers.

    The text is a literal in this file, not user input, so the markers are known
    to be balanced; everything between them still goes through the escaper.
    """
    parts = text.split("*")
    return "*".join(esc(part) if index % 2 == 0 else part for index, part in enumerate(parts))


async def _reply(update: Update, text: str) -> None:
    message = update.effective_message
    if message is None:
        return
    await message.reply_text(text)


# python-telegram-bot's handler signature: a coroutine function, not merely
# something awaitable.
Handler = Callable[[Update, ContextTypes.DEFAULT_TYPE], Coroutine[Any, Any, None]]

COMMANDS: dict[str, Handler] = {
    "start": start,
    "help": help_command,
    "whoami": whoami,
    "cancel": cancel,
    "status": status,
}

__all__ = ["COMMANDS", "COMMAND_MENU", "unknown_command"]
