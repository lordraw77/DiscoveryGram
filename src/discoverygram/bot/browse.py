"""Navigation handlers: `/browse`, `/open`, the action bar, and folder operations.

Callback data follows one shape throughout:

    <action>:<token>:<arg>

The token holds the path in the session store — a note path routinely exceeds
Telegram's 64-byte callback limit — and the argument distinguishes buttons that
share it. One token therefore serves a whole view: every button on a note reuses
the token issued when that note was opened.

Three actions carry the whole surface:

    nav:<tok>:<page>    a folder listing
    note:<tok>:<page>   a note body page
    act:<tok>:<verb>    an action on the note the token names
"""

from __future__ import annotations

from typing import Any

from telegram import Message, Update
from telegram.ext import ContextTypes

from discoverygram.app.navigation import (
    Entry,
    EntryKind,
    FolderView,
    NavigationService,
    WikiLink,
)
from discoverygram.app.notes import NoteService, normalise_tag
from discoverygram.bot.deps import BotDeps, deps_of
from discoverygram.bot.notes import (
    body_pages,
    entry_label,
    render_backlinks,
    render_folder,
    render_note,
    render_note_split,
    render_related,
    unresolved_links_note,
)
from discoverygram.bot.render import button_grid, keyboard, pagination_row
from discoverygram.bot.render import escape_markdown_v2 as esc
from discoverygram.bot.tokens import CallbackTokens
from discoverygram.ports.errors import NoteStoreError, Unsupported
from discoverygram.ports.model import Note
from discoverygram.util.logging import get_logger

log = get_logger(__name__)

NAV_ACTION = "nav"
NOTE_ACTION = "note"
ACT_ACTION = "act"

STALE = "That view has expired. Open it again."

# Pending multi-step inputs, kept in `user_data` so /cancel clears them all.
PENDING_KEY = "pending_action"

FOLDER_USAGE = (
    "Usage:\n"
    "`/folder new <path>`\n"
    "`/folder rename <old> <new>`\n"
    "`/folder move <old> <new>`\n"
    "`/folder delete <path>`"
)


def _navigation(deps: BotDeps) -> NavigationService:
    return NavigationService(deps.notes, deps.settings)


def _notes(deps: BotDeps) -> NoteService:
    return NoteService(deps.notes)


def _argument(context: ContextTypes.DEFAULT_TYPE) -> str:
    return " ".join(context.args or []).strip()


# --- Browsing -------------------------------------------------------------


async def browse_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Enter the tree, at the root or at a folder the user named."""
    deps = deps_of(context)
    view = await _navigation(deps).folder(_argument(context))
    text, markup = await _folder_message(deps, view, 1)
    await _reply(update, text, markup)


async def nav_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Every folder button: turn a page, step into a child, go up, go home.

    All four reuse the token issued when the folder was rendered, because its
    payload already holds the listing. Only *entering* a different folder needs
    a new token, since that is genuinely a new view rather than the same one
    seen from a different offset.
    """
    query = update.callback_query
    if query is None or not query.data:
        return

    deps = deps_of(context)
    payload = await deps.tokens.resolve(query.data)
    if payload is None:
        await query.answer(STALE, show_alert=True)
        return

    _, token, args = CallbackTokens.split(query.data)
    await deps.tokens.extend(query.data)

    # A token with no stored listing is a *pointer* to a folder — the "⬆ Folder"
    # button on a note, for instance. Load it and render it fresh, which issues
    # the listing token the rest of this handler expects.
    if "entries" not in payload:
        await query.answer()
        destination = await _navigation(deps).folder(str(payload.get("path", "")))
        text, markup = await _folder_message(deps, destination, 1)
        await query.edit_message_text(
            text=text, parse_mode=deps.settings.telegram_parse_mode, reply_markup=markup
        )
        return

    view = _view_from_payload(payload, deps)
    argument = args[0] if args else "1"

    if argument.startswith("e"):
        await _enter(update, context, view, argument[1:])
        return

    if argument in ("up", "root"):
        await query.answer()
        target = "" if argument == "root" else view.parent
        destination = await _navigation(deps).folder(target)
        text, markup = await _folder_message(deps, destination, 1)
        await query.edit_message_text(
            text=text, parse_mode=deps.settings.telegram_parse_mode, reply_markup=markup
        )
        return

    await query.answer()
    text, markup = await _folder_message(deps, view, _number([argument]), token=token)
    await query.edit_message_text(
        text=text, parse_mode=deps.settings.telegram_parse_mode, reply_markup=markup
    )


async def _enter(
    update: Update, context: ContextTypes.DEFAULT_TYPE, view: FolderView, raw_index: str
) -> None:
    """Open the child at `raw_index` — a subfolder listing, or a note."""
    query = update.callback_query
    assert query is not None
    deps = deps_of(context)

    if not raw_index.isdigit() or not 0 <= int(raw_index) < len(view.entries):
        await query.answer("That item is no longer here.", show_alert=True)
        return

    entry = view.entries[int(raw_index)]
    await query.answer()

    if entry.is_folder:
        destination = await _navigation(deps).folder(entry.path)
        text, markup = await _folder_message(deps, destination, 1)
        await query.edit_message_text(
            text=text, parse_mode=deps.settings.telegram_parse_mode, reply_markup=markup
        )
        return

    note = await _navigation(deps).open_note(entry.path)
    await send_note(update, context, note, reply_to_query=True)


def _folder_payload(view: FolderView) -> dict[str, Any]:
    return {
        "path": view.path,
        "page_size": view.page_size,
        "entries": [
            {"k": entry.kind.value, "p": entry.path, "t": entry.title, "c": entry.children}
            for entry in view.entries
        ],
    }


def _view_from_payload(payload: dict[str, Any], deps: BotDeps) -> FolderView:
    """Rebuild a listing from its token, rather than re-deriving the tree.

    A page turn should not depend on the vault being reachable, and it must not
    show a different set of children than the page the user came from.
    """
    raw = payload.get("entries", [])
    entries = tuple(
        Entry(
            kind=EntryKind(str(item.get("k", EntryKind.NOTE.value))),
            path=str(item.get("p", "")),
            title=str(item.get("t", "")),
            children=int(item.get("c", 0) or 0),
        )
        for item in raw
        if isinstance(item, dict)
    )
    return FolderView(
        path=str(payload.get("path", "")),
        entries=entries,
        page_size=int(payload.get("page_size") or deps.settings.tree_page_size),
    )


async def _folder_message(
    deps: BotDeps, view: FolderView, page: int, *, token: str | None = None
) -> tuple[str, Any]:
    """The listing text and its keyboard.

    One token per folder *view*; every button on it carries an argument instead
    of a token of its own, so turning pages costs no session state.
    """
    page = view.clamp(page)
    if token is None:
        _, token = CallbackTokens.parse(await deps.tokens.issue(NAV_ACTION, _folder_payload(view)))
    base = f"{NAV_ACTION}:{token}"

    offset = (page - 1) * view.page_size
    rows: list[list[tuple[str, str]]] = [
        [(entry_label(entry), CallbackTokens.with_args(base, f"e{offset + index}"))]
        for index, entry in enumerate(view.page(page))
    ]

    if view.pages > 1:
        rows.append(
            pagination_row(
                previous_data=CallbackTokens.with_args(base, page - 1) if page > 1 else None,
                next_data=(CallbackTokens.with_args(base, page + 1) if page < view.pages else None),
                page=page,
                pages=view.pages,
            )
        )

    controls: list[tuple[str, str]] = []
    if not view.is_root:
        controls.append(("⬆ Up", CallbackTokens.with_args(base, "up")))
    controls.append(("🏠 Root", CallbackTokens.with_args(base, "root")))
    rows.append(controls)

    return render_folder(view, page), keyboard(rows)


# --- Opening a note -------------------------------------------------------


async def open_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    deps = deps_of(context)
    path = _argument(context)
    if not path:
        await _reply(update, _escaped_usage("Usage: `/open <path>`"))
        return
    await send_note(update, context, await _navigation(deps).open_note(path))


async def note_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Open a note, or turn a page of one already open."""
    query = update.callback_query
    if query is None or not query.data:
        return

    deps = deps_of(context)
    payload = await deps.tokens.resolve(query.data)
    if payload is None:
        await query.answer(STALE, show_alert=True)
        return

    _, token, args = CallbackTokens.split(query.data)
    path = str(payload.get("path", ""))
    await deps.tokens.extend(query.data)
    await query.answer()

    note = await _navigation(deps).open_note(path)
    pages = body_pages(note)
    page = max(1, min(_number(args), len(pages)))

    text, markup = await _note_message(deps, note, page, pages=pages, token=token)
    await query.edit_message_text(
        text=text, parse_mode=deps.settings.telegram_parse_mode, reply_markup=markup
    )


async def send_note(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    note: Note,
    *,
    reply_to_query: bool = False,
) -> None:
    """Send a note as a new message, honouring `LONG_NOTE_MODE`.

    `split` sends the note as consecutive messages and puts the action bar on
    the last one; `paged` sends a single message the reader turns pages in.
    """
    deps = deps_of(context)
    deps.count("notes_opened")
    send = _reply_to_query if reply_to_query else _reply
    _, token = CallbackTokens.parse(await deps.tokens.issue(NOTE_ACTION, {"path": note.path}))

    if deps.settings.long_note_mode == "split":
        messages = render_note_split(note)
        for body in messages[:-1]:
            await send(update, body, None)
        await send(update, messages[-1], await _action_keyboard(deps, note, token))
        return

    text, markup = await _note_message(deps, note, 1, pages=body_pages(note), token=token)
    await send(update, text, markup)


async def _note_message(
    deps: BotDeps, note: Note, page: int, *, pages: list[str], token: str
) -> tuple[str, Any]:
    text = render_note(note, page, pages=pages)
    unresolved = unresolved_links_note(await _navigation(deps).wiki_links(note))
    if unresolved:
        text = f"{text}\n\n{unresolved}"

    rows: list[list[tuple[str, str]]] = []
    if len(pages) > 1:
        base = f"{NOTE_ACTION}:{token}"
        rows.append(
            pagination_row(
                previous_data=CallbackTokens.with_args(base, page - 1) if page > 1 else None,
                next_data=(CallbackTokens.with_args(base, page + 1) if page < len(pages) else None),
                page=page,
                pages=len(pages),
            )
        )

    rows.extend(await _wiki_link_rows(deps, note))
    rows.extend(_action_rows(token))
    rows.append(await _note_navigation(deps, note))
    return text, keyboard(rows)


async def _wiki_link_rows(deps: BotDeps, note: Note) -> list[list[tuple[str, str]]]:
    links: list[WikiLink] = await _navigation(deps).wiki_links(note)
    buttons = []
    for link in links:
        if not link.resolved:
            continue
        data = await deps.tokens.issue(NOTE_ACTION, {"path": link.path})
        buttons.append((f"🔗 {link.label[:24]}", data))
    return button_grid(buttons, columns=2)


def _action_rows(token: str) -> list[list[tuple[str, str]]]:
    """The per-note action bar. One token, one verb per button."""
    base = f"{ACT_ACTION}:{token}"
    return [
        [
            ("✏️ Edit", f"{base}:edit"),
            ("➕ Append", f"{base}:append"),
            ("🏷 Tag", f"{base}:tag"),
        ],
        [
            ("🔗 Backlinks", f"{base}:back"),
            ("🕸 Related", f"{base}:rel"),
            ("📋 Path", f"{base}:path"),
        ],
        [
            ("📄 Raw", f"{base}:raw"),
            ("🌐 Share", f"{base}:share"),
            ("🗑 Delete", f"{base}:del"),
        ],
    ]


async def _action_keyboard(deps: BotDeps, note: Note, token: str) -> Any:
    rows = _action_rows(token)
    rows.append(await _note_navigation(deps, note))
    return keyboard(rows)


async def _note_navigation(deps: BotDeps, note: Note) -> list[tuple[str, str]]:
    """The step back out of a note: its folder, then the root."""
    from discoverygram.util.paths import parent_folder

    folder = parent_folder(note.path)
    return [
        ("⬆ Folder", await deps.tokens.issue(NAV_ACTION, {"path": folder})),
        ("🏠 Root", await deps.tokens.issue(NAV_ACTION, {"path": ""})),
    ]


# --- The action bar -------------------------------------------------------


async def act_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Dispatch one verb of the action bar."""
    query = update.callback_query
    if query is None or not query.data:
        return

    deps = deps_of(context)
    payload = await deps.tokens.resolve(query.data)
    if payload is None:
        await query.answer(STALE, show_alert=True)
        return

    _, token, args = CallbackTokens.split(query.data)
    verb = args[0] if args else ""
    path = str(payload.get("path", ""))
    await deps.tokens.extend(query.data)

    handler = _VERBS.get(verb)
    if handler is None:
        await query.answer("That button is no longer supported.", show_alert=True)
        return

    await handler(update, context, deps, path, token)


async def _verb_backlinks(
    update: Update, context: ContextTypes.DEFAULT_TYPE, deps: BotDeps, path: str, token: str
) -> None:
    query = update.callback_query
    assert query is not None
    await query.answer()
    backlinks = await _navigation(deps).backlinks(path)

    rows = []
    for link in backlinks[: deps.settings.results_page_size]:
        data = await deps.tokens.issue(NOTE_ACTION, {"path": link.path})
        rows.append([(f"📄 {link.title[:28]}", data)])

    await _reply_to_query(
        update, render_backlinks(path, backlinks), keyboard(rows) if rows else None
    )


async def _verb_related(
    update: Update, context: ContextTypes.DEFAULT_TYPE, deps: BotDeps, path: str, token: str
) -> None:
    query = update.callback_query
    assert query is not None
    await query.answer()
    refs = await _navigation(deps).related(path)

    rows = []
    for ref in refs[: deps.settings.results_page_size]:
        data = await deps.tokens.issue(NOTE_ACTION, {"path": ref.path})
        rows.append([(f"📄 {ref.title[:28]}", data)])

    await _reply_to_query(update, render_related(path, refs), keyboard(rows) if rows else None)


async def _verb_path(
    update: Update, context: ContextTypes.DEFAULT_TYPE, deps: BotDeps, path: str, token: str
) -> None:
    """Telegram has no clipboard, so a tappable code span is the copy affordance."""
    query = update.callback_query
    assert query is not None
    await query.answer()
    await _reply_to_query(update, f"`{esc(path)}`", None)


async def _verb_raw(
    update: Update, context: ContextTypes.DEFAULT_TYPE, deps: BotDeps, path: str, token: str
) -> None:
    """The unrendered source, in a fenced block."""
    from discoverygram.bot.render import chunk_text, code_block

    query = update.callback_query
    assert query is not None
    await query.answer()

    note = await _notes(deps).read(path)
    for chunk in chunk_text(note.content or "(empty)", limit=3800):
        await _reply_to_query(update, code_block(chunk), None)


async def _verb_share(
    update: Update, context: ContextTypes.DEFAULT_TYPE, deps: BotDeps, path: str, token: str
) -> None:
    query = update.callback_query
    assert query is not None
    try:
        link = await _notes(deps).share(path)
    except Unsupported as exc:
        await query.answer(str(exc)[:190], show_alert=True)
        return
    await query.answer()
    await _reply_to_query(update, f"🌐 {esc(link.url)}", None)


async def _verb_delete(
    update: Update, context: ContextTypes.DEFAULT_TYPE, deps: BotDeps, path: str, token: str
) -> None:
    """Step one of two. Deleting is the one action with no undo."""
    query = update.callback_query
    assert query is not None
    await query.answer()

    base = f"{ACT_ACTION}:{token}"
    await _reply_to_query(
        update,
        f"{esc('Delete')} `{esc(path)}`{esc('? This cannot be undone.')}",
        keyboard([[("🗑 Yes, delete", f"{base}:delok"), ("Cancel", f"{base}:nodel")]]),
    )


async def _verb_delete_confirmed(
    update: Update, context: ContextTypes.DEFAULT_TYPE, deps: BotDeps, path: str, token: str
) -> None:
    query = update.callback_query
    assert query is not None
    result = await _notes(deps).delete(path)
    # The note is gone; the token that names it must not act again.
    await deps.tokens.revoke(query.data or "")
    await query.answer("Deleted")
    await query.edit_message_text(text=esc(result.summary))


async def _verb_delete_cancelled(
    update: Update, context: ContextTypes.DEFAULT_TYPE, deps: BotDeps, path: str, token: str
) -> None:
    query = update.callback_query
    assert query is not None
    await query.answer("Kept")
    await query.edit_message_text(text=esc("Not deleted."))


async def _verb_edit(
    update: Update, context: ContextTypes.DEFAULT_TYPE, deps: BotDeps, path: str, token: str
) -> None:
    await _ask_for_input(
        update,
        context,
        path,
        kind="edit",
        prompt=(
            f"{esc('Send the new body for')} `{esc(path)}`{esc('.')}\n"
            f"{esc('It replaces the note entirely. /cancel to stop.')}"
        ),
    )


async def _verb_append(
    update: Update, context: ContextTypes.DEFAULT_TYPE, deps: BotDeps, path: str, token: str
) -> None:
    await _ask_for_input(
        update,
        context,
        path,
        kind="append",
        prompt=(
            f"{esc('Send the text to append to')} `{esc(path)}`{esc('.')}\n"
            f"{esc('/cancel to stop.')}"
        ),
    )


async def _verb_tag(
    update: Update, context: ContextTypes.DEFAULT_TYPE, deps: BotDeps, path: str, token: str
) -> None:
    await _ask_for_input(
        update,
        context,
        path,
        kind="tag",
        prompt=f"{esc('Send the tag to add to')} `{esc(path)}`{esc('. /cancel to stop.')}",
    )


async def _ask_for_input(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    path: str,
    *,
    kind: str,
    prompt: str,
) -> None:
    """Park a pending action in `user_data` and wait for the next message."""
    query = update.callback_query
    assert query is not None
    await query.answer()

    if context.user_data is not None:
        context.user_data[PENDING_KEY] = {"kind": kind, "path": path}
    await _reply_to_query(update, prompt, None)


_VERBS = {
    "edit": _verb_edit,
    "append": _verb_append,
    "tag": _verb_tag,
    "back": _verb_backlinks,
    "rel": _verb_related,
    "path": _verb_path,
    "raw": _verb_raw,
    "share": _verb_share,
    "del": _verb_delete,
    "delok": _verb_delete_confirmed,
    "nodel": _verb_delete_cancelled,
}


# --- Pending input --------------------------------------------------------


async def pending_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Consume the next message when an action is waiting for one.

    Registered ahead of the plain-text search handler and raising
    `ApplicationHandlerStop` when it acts, so text meant as a note body is never
    also run as a search.
    """
    from telegram.ext import ApplicationHandlerStop

    message = update.effective_message
    if message is None or not message.text or context.user_data is None:
        return

    pending = context.user_data.get(PENDING_KEY)
    if not isinstance(pending, dict):
        return

    context.user_data.pop(PENDING_KEY, None)
    deps = deps_of(context)
    service = _notes(deps)
    path = str(pending.get("path", ""))
    kind = str(pending.get("kind", ""))

    if kind == "edit":
        result = await service.replace(path, message.text)
    elif kind == "append":
        result = await service.append(path, message.text, timestamp=True)
    elif kind == "tag":
        result = await service.add_tag(path, normalise_tag(message.text))
    else:  # pragma: no cover - the dict is written only by this module
        return

    await _reply(update, esc(result.summary))
    raise ApplicationHandlerStop


# --- Backlinks and related as commands ------------------------------------


async def backlinks_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    deps = deps_of(context)
    path = _argument(context)
    if not path:
        await _reply(update, _escaped_usage("Usage: `/backlinks <path>`"))
        return
    await _reply(update, render_backlinks(path, await _navigation(deps).backlinks(path)))


async def related_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    deps = deps_of(context)
    path = _argument(context)
    if not path:
        await _reply(update, _escaped_usage("Usage: `/related <path>`"))
        return
    await _reply(update, render_related(path, await _navigation(deps).related(path)))


async def move_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    deps = deps_of(context)
    parts = context.args or []
    if len(parts) != 2:
        await _reply(update, _escaped_usage("Usage: `/move <old path> <new path>`"))
        return
    result = await _notes(deps).move(parts[0], parts[1])
    await _reply(update, esc(result.summary))


# --- Folder operations ----------------------------------------------------


async def folder_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/folder new|rename|move|delete` — the REST-only folder surface.

    Deleting takes everything under the folder with it, so it asks first and
    says how much is at stake.
    """
    deps = deps_of(context)
    parts = context.args or []
    action = parts[0].lower() if parts else ""
    rest = parts[1:]

    navigation = _navigation(deps)
    try:
        if action == "new" and len(rest) == 1:
            await _reply(update, esc(f"Created {await navigation.create_folder(rest[0])}."))
            return
        if action == "rename" and len(rest) == 2:
            await _reply(update, esc(f"Renamed to {await navigation.rename_folder(*rest)}."))
            return
        if action == "move" and len(rest) == 2:
            await _reply(update, esc(f"Moved to {await navigation.move_folder(*rest)}."))
            return
        if action == "delete" and len(rest) == 1:
            await _confirm_folder_delete(update, context, rest[0])
            return
    except Unsupported as exc:
        await _reply(update, esc(str(exc)))
        return

    await _reply(update, _escaped_usage(FOLDER_USAGE))


async def _confirm_folder_delete(
    update: Update, context: ContextTypes.DEFAULT_TYPE, path: str
) -> None:
    deps = deps_of(context)
    size = await _navigation(deps).folder_size(path)
    token = await deps.tokens.issue(ACT_ACTION, {"path": path, "folder": True})

    await _reply(
        update,
        f"{esc('Delete the folder')} `{esc(path)}` "
        f"{esc(f'and everything in it ({size} items)? This cannot be undone.')}",
        keyboard(
            [
                [
                    ("🗑 Yes, delete", CallbackTokens.with_args(token, "rmdirok")),
                    ("Cancel", CallbackTokens.with_args(token, "nodel")),
                ]
            ]
        ),
    )


async def _verb_delete_folder_confirmed(
    update: Update, context: ContextTypes.DEFAULT_TYPE, deps: BotDeps, path: str, token: str
) -> None:
    query = update.callback_query
    assert query is not None
    try:
        await _navigation(deps).delete_folder(path)
    except NoteStoreError as exc:
        await query.answer()
        await query.edit_message_text(text=esc(str(exc)))
        return
    await deps.tokens.revoke(query.data or "")
    await query.answer("Deleted")
    await query.edit_message_text(text=esc(f"Deleted {path} and its contents."))


_VERBS["rmdirok"] = _verb_delete_folder_confirmed


# --- Helpers --------------------------------------------------------------


def _number(args: list[str]) -> int:
    if not args or not args[0].lstrip("-").isdigit():
        return 1
    return int(args[0])


def _escaped_usage(text: str) -> str:
    parts = text.split("`")
    return "`".join(esc(part) if index % 2 == 0 else part for index, part in enumerate(parts))


async def _reply(update: Update, text: str, markup: Any = None) -> None:
    message = update.effective_message
    if message is None:
        return
    await message.reply_text(text, reply_markup=markup)


async def _reply_to_query(update: Update, text: str, markup: Any) -> None:
    """Answer a button with a new message, leaving the original view intact.

    A callback's message can be *inaccessible* — too old for the bot to read
    back — in which case there is nothing to reply to and the chat id is used
    directly instead.
    """
    query = update.callback_query
    if query is None:
        return
    if isinstance(query.message, Message):
        await query.message.reply_text(text, reply_markup=markup)
        return
    chat = update.effective_chat
    if chat is not None:
        await query.get_bot().send_message(chat_id=chat.id, text=text, reply_markup=markup)


COMMANDS = {
    "browse": browse_command,
    "open": open_command,
    "backlinks": backlinks_command,
    "related": related_command,
    "move": move_command,
    "folder": folder_command,
}

__all__ = [
    "ACT_ACTION",
    "COMMANDS",
    "NAV_ACTION",
    "NOTE_ACTION",
    "PENDING_KEY",
    "act_callback",
    "nav_callback",
    "note_callback",
    "pending_input",
    "send_note",
]
