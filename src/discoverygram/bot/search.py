"""Search commands and their pagination.

`/search` `/find` `/tag` `/recent`, plus a plain message treated as a search.

Pagination stores the **whole result set once**, under a single callback token,
and every page button carries its number in the callback data (`page:<tok>:<n>`).
Turning twenty pages therefore creates one session entry, not forty, and a page
turn costs no vault read — which also means page 2 cannot disagree with page 1
because a note changed in between.

The token's lifetime is refreshed on every turn, so a long browse does not
expire because the first page was issued an hour ago.
"""

from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from discoverygram.app.search import ResultSet, SearchMode, SearchOutcome, SearchService
from discoverygram.bot.deps import BotDeps, deps_of
from discoverygram.bot.render import escape_markdown_v2 as esc
from discoverygram.bot.render import keyboard
from discoverygram.bot.results import hit_buttons, page_keyboard, render_page, render_tag_list
from discoverygram.util.logging import get_logger

log = get_logger(__name__)

PAGE_ACTION = "page"

USAGE = {
    SearchMode.FULL_TEXT: "Usage: `/search <words>`",
    SearchMode.LITERAL: "Usage: `/find <exact text>`",
    SearchMode.TAG: "Usage: `/tag <name>`, or `/tag` to list them",
    SearchMode.RECENT: "Usage: `/recent`, or `/recent 30` for a longer window",
}

STALE_RESULTS = "Those results have expired. Run the search again."


def _service(deps: BotDeps) -> SearchService:
    return SearchService(deps.notes, deps.settings, deps.instance)


def _argument(context: ContextTypes.DEFAULT_TYPE) -> str:
    """Everything after the command, as one string."""
    return " ".join(context.args or []).strip()


async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _run_text_mode(update, context, SearchMode.FULL_TEXT)


async def find_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _run_text_mode(update, context, SearchMode.LITERAL)


async def _run_text_mode(
    update: Update, context: ContextTypes.DEFAULT_TYPE, mode: SearchMode
) -> None:
    query = _argument(context)
    if not query:
        await _reply(update, esc_usage(mode))
        return

    deps = deps_of(context)
    service = _service(deps)
    outcome = (
        await service.literal(query)
        if mode is SearchMode.LITERAL
        else await service.full_text(query)
    )
    await _present(update, context, outcome)


async def tag_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """With an argument, the notes carrying that tag; without, the tag list.

    Neither needs `/api/search`, so `/tag` keeps working on an instance where
    search is disabled — worth knowing when it is the only mode left.
    """
    deps = deps_of(context)
    service = _service(deps)
    tag = _argument(context)

    if not tag:
        await _reply(update, render_tag_list(await service.tags()))
        return

    await _present(update, context, await service.by_tag(tag))


async def recent_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    deps = deps_of(context)
    raw = _argument(context)

    days: int | None = None
    if raw:
        if not raw.isdigit() or int(raw) <= 0:
            await _reply(update, esc_usage(SearchMode.RECENT))
            return
        days = int(raw)

    await _present(update, context, await _service(deps).recent(days=days))


async def text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A plain message that is not a command.

    Only reached when `DEFAULT_TEXT_ACTION=search`: with `quick`, the capture
    handler runs first and stops the chain, so a message the user meant to keep
    never also becomes a query. The check stays here as well, because handler
    order is easy to change by accident and searching someone's private thought
    is not a failure that announces itself.
    """
    message = update.effective_message
    if message is None or not message.text:
        return

    deps = deps_of(context)
    if deps.settings.default_text_action != "search":
        return

    await _present(update, context, await _service(deps).full_text(message.text))


async def page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Turn a page of an existing result set."""
    query = update.callback_query
    if query is None or not query.data:
        return

    deps = deps_of(context)
    _, token, args = deps.tokens.split(query.data)
    payload = await deps.tokens.resolve(query.data)

    if payload is None:
        # The token outlived its TTL. Say what expired, not "something expired".
        await query.answer(STALE_RESULTS, show_alert=True)
        return

    results = ResultSet.from_payload(payload)

    # An `h<n>` argument means "open hit n" rather than "turn to page n".
    if args and args[0].startswith("h"):
        await _open_hit(update, context, results, args[0][1:])
        return

    page = results.clamp(_page_number(args))

    # Still browsing, so keep the results alive.
    await deps.tokens.extend(query.data)
    await query.answer()

    base = f"{PAGE_ACTION}:{token}"
    rows = hit_buttons(results, page, base) + page_keyboard(results, page, base)
    await query.edit_message_text(
        text=render_page(results, page),
        parse_mode=deps.settings.telegram_parse_mode,
        # An empty InlineKeyboardMarkup is still a keyboard to Telegram, so the
        # absence of pages has to be expressed as no markup at all.
        reply_markup=keyboard(rows) if rows else None,
    )


async def _open_hit(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    results: ResultSet,
    raw_index: str,
) -> None:
    """Open the nth hit of a stored result set, by index into the payload."""
    from discoverygram.bot.browse import send_note

    query = update.callback_query
    assert query is not None

    if not raw_index.isdigit():
        await query.answer()
        return

    index = int(raw_index)
    if not 1 <= index <= len(results.hits):
        await query.answer("That result is no longer on this page.", show_alert=True)
        return

    await query.answer()
    deps = deps_of(context)
    note = await deps.notes.get_note(results.hits[index - 1].ref.path)
    await send_note(update, context, note, reply_to_query=True)


def _page_number(args: list[str]) -> int:
    """A page number from callback data, defaulting to the first page.

    Callback data is whatever arrives; a non-numeric page is a malformed button,
    not a reason to fail.
    """
    if not args or not args[0].lstrip("-").isdigit():
        return 1
    return int(args[0])


async def _present(
    update: Update, context: ContextTypes.DEFAULT_TYPE, outcome: SearchOutcome
) -> None:
    """Send the first page, with page buttons only when there is more than one."""
    deps = deps_of(context)

    if not outcome.ran:
        await _reply(update, esc(outcome.notice))
        return

    results = ResultSet.of(outcome, page_size=deps.settings.results_page_size)
    deps.count("searches")

    log.info(
        "search_completed",
        mode=outcome.mode.value,
        results=len(outcome.hits),
        pages=results.pages,
        truncated=outcome.truncated,
    )

    if results.is_empty:
        await _reply(update, render_page(results, 1))
        return

    data = await deps.tokens.issue(PAGE_ACTION, results.to_payload())
    rows = hit_buttons(results, 1, data) + page_keyboard(results, 1, data)
    await _reply(update, render_page(results, 1), markup=keyboard(rows) if rows else None)


def esc_usage(mode: SearchMode) -> str:
    """Usage text, escaped but keeping its intentional code span."""
    text = USAGE[mode]
    parts = text.split("`")
    return "`".join(esc(part) if index % 2 == 0 else part for index, part in enumerate(parts))


async def _reply(update: Update, text: str, markup: object = None) -> None:
    message = update.effective_message
    if message is None:
        return
    await message.reply_text(text, reply_markup=markup)  # type: ignore[arg-type]


COMMANDS = {
    "search": search_command,
    "find": find_command,
    "tag": tag_command,
    "recent": recent_command,
}

__all__ = ["COMMANDS", "PAGE_ACTION", "page_callback", "text_message"]
