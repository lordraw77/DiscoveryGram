"""Creation handlers: `/new`, `/quick`, attachments, drafts, `/summarize`, `/ask`.

Callback data follows the shape the rest of the bot uses:

    dr:<tok>:<verb>     act on the draft the token holds
    dr:<tok>:pick:<n>   choose one of the folders the token offered
    tpl:<tok>:<n>       choose a template

**One token per draft**, not per button: a draft is one piece of state, and
every button acts on the same one. Editing a title and then changing the path
costs one session entry, not three.

The order of operations for an attachment is the part worth stating, because
it is chosen so that a failure late in the chain never loses the user's file:

    download -> upload to the vault -> read (LLM) -> generate -> resolve -> preview

The upload happens *before* the LLM work, so a photo survives a provider
outage: the draft still carries the attachment, the body just says less. The
write happens only on `Save`.
"""

from __future__ import annotations

from typing import Any

from telegram import Message, Update
from telegram.ext import ApplicationHandlerStop, ContextTypes

from discoverygram.app.assist import AssistService
from discoverygram.app.capture import CaptureService, Provenance, Resolution
from discoverygram.app.ingest import Draft, IngestService
from discoverygram.app.intent import CaptureIntent, parse_caption
from discoverygram.app.search import SearchService
from discoverygram.bot.albums import AlbumBuffer
from discoverygram.bot.browse import PENDING_KEY, send_note
from discoverygram.bot.deps import BotDeps, deps_of
from discoverygram.bot.drafts import (
    DRAFT_ACTION,
    ambiguity_keyboard,
    draft_keyboard,
    render_ambiguity,
    render_answer,
    render_draft,
    render_saved,
)
from discoverygram.bot.render import button_grid, keyboard
from discoverygram.bot.render import escape_markdown_v2 as esc
from discoverygram.bot.tokens import CallbackTokens
from discoverygram.ports.errors import InvalidRequest, NoteStoreError, Unsupported
from discoverygram.ports.llm import SUPPORTED_IMAGE_TYPES, ImagePart
from discoverygram.util.logging import get_logger
from discoverygram.util.media import safe_filename, sniff_image

log = get_logger(__name__)

TEMPLATE_ACTION = "tpl"

STALE = "That draft has expired. Send it again."

# Pending kinds this module owns. They share `PENDING_KEY` with the browse
# flows so that `/cancel` clears every multi-step flow in one place.
DRAFT_TITLE = "draft_title"
DRAFT_PATH = "draft_path"
DRAFT_KINDS = frozenset({DRAFT_TITLE, DRAFT_PATH})

NEW_USAGE = "Usage:\n`/new <path> <text>`\n`/new --template <name> <path>`"
ASK_USAGE = "Usage: `/ask <question>`"

# One buffer for the process: albums are keyed by Telegram's own group id, and
# two users cannot share one.
_ALBUMS: AlbumBuffer[dict[str, Any]] = AlbumBuffer()


def _capture(deps: BotDeps) -> CaptureService:
    return CaptureService(deps.notes, deps.settings)


def _ingest(deps: BotDeps) -> IngestService:
    return IngestService(deps.llm, deps.settings)


def _assist(deps: BotDeps) -> AssistService:
    search = SearchService(deps.notes, deps.settings, deps.instance)
    return AssistService(deps.notes, search, deps.llm, deps.settings)


def _argument(context: ContextTypes.DEFAULT_TYPE) -> str:
    return " ".join(context.args or []).strip()


# --- Simple creation ------------------------------------------------------


async def new_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/new <path> <text>` — the no-LLM path, and `--template` alongside it."""
    deps = deps_of(context)
    argument = _argument(context)

    if not argument:
        await _reply(update, _escaped_usage(NEW_USAGE))
        return

    if argument.startswith("--template"):
        await _new_from_template(update, deps, argument)
        return

    path, _, body = argument.partition(" ")
    if not body.strip():
        await _reply(update, _escaped_usage(NEW_USAGE))
        return

    service = _capture(deps)
    # `/new` names a path outright, so it is taken literally — but it still
    # goes through resolution, because the collision rule must apply here too:
    # `create_note` is an upsert and would otherwise overwrite silently.
    resolution = await service.resolve(path)
    ref = await service.create(resolution.path, body.strip())
    deps.count("notes_created")

    await _reply(update, render_saved(ref.path, renamed_from=resolution.renamed_from))


async def _new_from_template(update: Update, deps: BotDeps, argument: str) -> None:
    parts = argument.split()
    if len(parts) < 3:
        await _reply(update, _escaped_usage("Usage: `/new --template <name> <path>`"))
        return

    name, path = parts[1], " ".join(parts[2:])
    try:
        ref = await _capture(deps).from_template(name, path)
    except Unsupported as exc:
        await _reply(update, esc(str(exc)))
        return
    deps.count("notes_created")
    await _reply(update, render_saved(ref.path))


async def quick_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/quick <text>` — capture into today's inbox note with no decisions."""
    deps = deps_of(context)
    text = _argument(context)
    if not text:
        await _reply(update, _escaped_usage("Usage: `/quick <text>`"))
        return
    await _quick_capture(update, deps, text)


async def _quick_capture(update: Update, deps: BotDeps, text: str) -> None:
    ref = await _capture(deps).quick(text)
    deps.count("notes_created")
    await _reply(update, f"{esc('Captured to')} `{esc(ref.path)}`{esc('.')}")


async def default_text_capture(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A plain message when `DEFAULT_TEXT_ACTION=quick`.

    Registered ahead of the search handler and stopping the chain, so a message
    the user meant to capture never also becomes a query. With the default
    `search` setting this returns immediately and the search handler runs.
    """
    message = update.effective_message
    if message is None or not message.text:
        return

    deps = deps_of(context)
    if deps.settings.default_text_action != "quick":
        return

    await _quick_capture(update, deps, message.text)
    raise ApplicationHandlerStop


# --- Templates ------------------------------------------------------------


async def template_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List the vault's templates as a picker, or explain that there are none."""
    deps = deps_of(context)
    try:
        templates = await _capture(deps).templates()
    except Unsupported as exc:
        await _reply(update, esc(str(exc)))
        return

    if not templates:
        await _reply(update, esc("This vault has no templates."))
        return

    names = [template.name for template in templates]
    token = await deps.tokens.issue(TEMPLATE_ACTION, {"names": names})
    _, bare = CallbackTokens.parse(token)
    del bare

    rows = button_grid(
        [(name, CallbackTokens.with_args(token, index)) for index, name in enumerate(names[:20])],
        columns=2,
    )
    await _reply(
        update,
        esc("Pick a template. I will ask where to put the note."),
        keyboard(rows),
    )


async def template_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A template was picked: ask for the path, then create it."""
    query = update.callback_query
    if query is None or not query.data:
        return

    deps = deps_of(context)
    payload = await deps.tokens.resolve(query.data)
    if payload is None:
        await query.answer(STALE, show_alert=True)
        return

    _, _, args = CallbackTokens.split(query.data)
    names = payload.get("names")
    index = int(args[0]) if args and args[0].isdigit() else -1
    if not isinstance(names, list) or not 0 <= index < len(names):
        await query.answer(STALE, show_alert=True)
        return

    await query.answer()
    if context.user_data is not None:
        context.user_data[PENDING_KEY] = {"kind": "template_path", "template": str(names[index])}
    await _reply_to_query(
        update,
        esc(f"Where should the “{names[index]}” note go? Send a path."),
        None,
    )


# --- Attachments ----------------------------------------------------------


async def attachment_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A photo or an image document, alone or as part of an album."""
    message = update.effective_message
    if message is None:
        return

    deps = deps_of(context)
    item = await _extract_attachment(message, deps)
    if item is None:
        return

    if message.media_group_id:
        group = await _ALBUMS.collect(message.media_group_id, item)
        if group is None:
            # Absorbed into the first update of this album.
            return
        items = group
    else:
        items = [item]

    await _build_draft_from_images(update, context, deps, items)


async def _extract_attachment(message: Message, deps: BotDeps) -> dict[str, Any] | None:
    """Validate and download one attachment, or explain why it cannot be used.

    Size is checked against `MAX_UPLOAD_MB` **before** downloading. Telegram
    reports the size in the update, so a file that is too large costs no
    transfer at all — and the user is told the limit rather than watching a
    download fail.
    """
    settings = deps.settings

    if message.photo:
        # The last entry is the largest rendition Telegram kept.
        photo = message.photo[-1]
        file_id, size, mime, name = photo.file_id, photo.file_size or 0, "image/jpeg", "photo.jpg"
    elif message.document is not None:
        document = message.document
        mime = document.mime_type or "application/octet-stream"
        if mime not in SUPPORTED_IMAGE_TYPES:
            await _reply(
                message,
                esc(
                    f"I can only read images for now ({', '.join(sorted(SUPPORTED_IMAGE_TYPES))}), "
                    f"and that file is {mime}."
                ),
                to_message=True,
            )
            return None
        file_id = document.file_id
        size = document.file_size or 0
        name = document.file_name or "attachment"
    else:
        return None

    if size > settings.max_upload_bytes:
        await _reply(
            message,
            esc(
                f"That file is {size // (1024 * 1024)} MB and the limit is "
                f"{settings.max_upload_mb} MB."
            ),
            to_message=True,
        )
        return None

    telegram_file = await message.get_bot().get_file(file_id)
    data = bytes(await telegram_file.download_as_bytearray())

    if len(data) > settings.max_upload_bytes:
        # Telegram's reported size can be absent; the real one always decides.
        await _reply(
            message,
            esc(f"That file is larger than the {settings.max_upload_mb} MB limit."),
            to_message=True,
        )
        return None

    # What Telegram *called* the file is a claim; what the bytes start with is
    # a fact. A mismatch is not necessarily an attack — phones mislabel HEIC as
    # JPEG all the time — but sending a non-image to a vision model wastes a
    # provider call and stores something in the vault under a name that lies.
    sniffed = sniff_image(data)
    if sniffed is None:
        await _reply(
            message,
            esc("That file does not look like an image I can read, whatever it is named."),
            to_message=True,
        )
        return None
    if sniffed != mime:
        log.info("attachment_type_corrected", declared=mime, actual=sniffed)
        mime = sniffed

    return {
        "data": data,
        "mime": mime,
        "name": safe_filename(name),
        "caption": message.caption or "",
    }


async def _build_draft_from_images(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    deps: BotDeps,
    items: list[dict[str, Any]],
) -> None:
    """Upload, read, generate, resolve, preview."""
    caption = next((item["caption"] for item in items if item["caption"]), "")
    intent = parse_caption(caption, has_image=True)

    uploaded, upload_warning = await _upload_all(deps, items)

    images = tuple(
        ImagePart(data=item["data"], mime_type=item["mime"])
        for item in items
        if item["mime"] in SUPPORTED_IMAGE_TYPES
    )

    notice = await _reply(update, esc("Reading…"))
    try:
        draft = await _ingest(deps).from_image(
            images, intent, user_id=_user_id(update), attachments=uploaded
        )
    finally:
        await _delete(notice)

    if upload_warning:
        draft = draft.warn(upload_warning)

    deps.count("drafts_built")
    await _present_draft(update, context, deps, draft, intent)


async def _upload_all(deps: BotDeps, items: list[dict[str, Any]]) -> tuple[tuple[str, ...], str]:
    """Put every attachment in the vault, before any LLM work happens.

    `POST /api/upload-media` is REST-only, so an MCP transport degrades to a
    note without its picture rather than failing the capture — the text is
    usually the point, and losing it because the transport cannot carry files
    would be the wrong trade.
    """
    paths: list[str] = []
    for item in items:
        try:
            upload = await deps.notes.upload_media(
                item["name"], item["data"], content_type=item["mime"]
            )
        except Unsupported:
            return (), (
                "This transport cannot upload files, so the image is not attached. "
                "Switch to NOTEDISCOVERY_TRANSPORT=rest to attach images."
            )
        except NoteStoreError as exc:
            log.warning("media_upload_failed", error=str(exc))
            return tuple(paths), f"I could not attach the file: {exc}"
        paths.append(upload.path)
    return tuple(paths), ""


async def forwarded_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A forwarded message becomes a draft, through the same pipeline as a photo.

    Forwarding is how people keep something someone *else* wrote, so it goes to
    a preview rather than straight into the inbox: the title and the path are
    exactly the decisions the user has not made yet.

    A **link** in the text is captured as a link. The bot deliberately does not
    fetch it: an outbound request to any URL a user pastes, made from inside the
    operator's network, is a server-side request forgery primitive — it would
    reach private addresses the operator never meant to expose. The URL is kept
    verbatim, which is what a note needs anyway.
    """
    message = update.effective_message
    if message is None or not message.text:
        return

    deps = deps_of(context)
    intent = parse_caption("", has_image=False)

    notice = await _reply(update, esc("Writing it up…"))
    try:
        draft = await _ingest(deps).from_text(
            _with_attribution(message), intent, user_id=_user_id(update)
        )
    finally:
        await _delete(notice)

    deps.count("drafts_built")
    await _present_draft(update, context, deps, draft, intent)
    raise ApplicationHandlerStop


def _with_attribution(message: Message) -> str:
    """The forwarded text, with a line saying where it came from.

    Provenance for human-written material: without it a forwarded paragraph
    reads, six months later, as something the reader wrote themselves.
    """
    origin = message.forward_origin
    name = (
        getattr(getattr(origin, "sender_user", None), "full_name", "")
        or getattr(origin, "sender_user_name", "")
        or getattr(getattr(origin, "chat", None), "title", "")
    )
    text = message.text or ""
    return f"{text}\n\n*Forwarded from {name}.*" if name else text


# --- Drafts ---------------------------------------------------------------


async def _present_draft(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    deps: BotDeps,
    draft: Draft,
    intent: CaptureIntent,
) -> None:
    """Resolve the path and show the preview — or ask which folder was meant."""
    if draft.is_empty:
        await _reply(update, esc("There was nothing I could turn into a note."))
        return

    resolution = await _capture(deps).resolve(intent.path, title=draft.title)

    if resolution.ambiguous:
        token = await deps.tokens.issue(
            DRAFT_ACTION, {"draft": _to_payload(draft), "candidates": list(resolution.candidates)}
        )
        await _reply(
            update,
            render_ambiguity(intent.path, resolution),
            ambiguity_keyboard(token, resolution.candidates),
        )
        return

    await _send_card(update, deps, draft.with_path(resolution.path), resolution)


async def _send_card(
    update: Update, deps: BotDeps, draft: Draft, resolution: Resolution | None = None
) -> None:
    payload: dict[str, Any] = {"draft": _to_payload(draft)}
    if resolution is not None and resolution.renamed_from:
        payload["renamed_from"] = resolution.renamed_from
    token = await deps.tokens.issue(DRAFT_ACTION, payload)
    await _reply(update, render_draft(draft), draft_keyboard(token))


async def draft_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Every button on a preview card."""
    query = update.callback_query
    if query is None or not query.data:
        return

    deps = deps_of(context)
    payload = await deps.tokens.resolve(query.data)
    if payload is None:
        await query.answer(STALE, show_alert=True)
        return

    _, _, args = CallbackTokens.split(query.data)
    verb = args[0] if args else ""
    await deps.tokens.extend(query.data)

    draft = _from_payload(payload.get("draft"))
    if draft is None:
        await query.answer(STALE, show_alert=True)
        return

    if verb == "cancel":
        await deps.tokens.revoke(query.data)
        await query.answer("Discarded")
        await query.edit_message_text(text=esc("Discarded. Nothing was written."))
        return

    if verb == "save":
        await _save_draft(update, context, deps, draft, str(payload.get("renamed_from", "")))
        return

    if verb == "pick":
        await _pick_folder(update, deps, payload, draft, args)
        return

    if verb == "regen":
        await _regenerate(update, deps, draft)
        return

    if verb in ("title", "path"):
        kind = DRAFT_TITLE if verb == "title" else DRAFT_PATH
        prompt = (
            "Send the title you want."
            if verb == "title"
            else "Send the path. A folder puts the note inside it."
        )
        await query.answer()
        if context.user_data is not None:
            context.user_data[PENDING_KEY] = {"kind": kind, "draft": _to_payload(draft)}
        await _reply_to_query(update, esc(prompt), None)
        return

    await query.answer(STALE, show_alert=True)


async def _save_draft(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    deps: BotDeps,
    draft: Draft,
    renamed_from: str,
) -> None:
    """Write the note, then show it with its action bar.

    The token is revoked as soon as the write succeeds, so a double tap cannot
    create the note twice — the same rule the delete button follows.
    """
    query = update.callback_query
    assert query is not None

    provenance = draft.provenance
    if provenance is not None:
        provenance = Provenance(
            provider=provenance.provider,
            model=provenance.model,
            source="Telegram",
        )

    try:
        ref = await _capture(deps).create(
            draft.path,
            draft.body,
            title=draft.title,
            tags=draft.tags,
            provenance=provenance,
        )
    except (NoteStoreError, InvalidRequest) as exc:
        await query.answer()
        await query.edit_message_text(text=esc(f"I could not save it: {exc}"))
        return

    await deps.tokens.revoke(query.data or "")
    deps.count("notes_created")
    await query.answer("Saved")
    await query.edit_message_text(text=render_saved(ref.path, renamed_from=renamed_from))

    try:
        note = await deps.notes.get_note(ref.path, include_backlinks=False)
    except NoteStoreError:
        return
    await send_note(update, context, note, reply_to_query=True)


async def _pick_folder(
    update: Update,
    deps: BotDeps,
    payload: Any,
    draft: Draft,
    args: list[str],
) -> None:
    query = update.callback_query
    assert query is not None

    candidates = payload.get("candidates")
    index = int(args[1]) if len(args) > 1 and args[1].isdigit() else -1
    if not isinstance(candidates, list) or not 0 <= index < len(candidates):
        await query.answer(STALE, show_alert=True)
        return

    resolution = await _capture(deps).resolve(str(candidates[index]), title=draft.title)
    await query.answer()
    await query.edit_message_text(text=esc(f"Filing under {candidates[index]}."))
    await _send_card_to_query(update, deps, draft.with_path(resolution.path), resolution)


async def _regenerate(update: Update, deps: BotDeps, draft: Draft) -> None:
    query = update.callback_query
    assert query is not None

    await query.answer("Regenerating…")
    refreshed = await _ingest(deps).regenerate(draft, user_id=_user_id(update))

    # The path was derived from the old title, so a new title earns a new path
    # — unless the user set the path themselves, which the draft records by
    # already having been resolved against an explicit target.
    if refreshed.title != draft.title and not draft.intent.path:
        resolution = await _capture(deps).resolve("", title=refreshed.title)
        refreshed = refreshed.with_path(resolution.path)

    await query.edit_message_text(text=render_draft(refreshed))
    await _send_card_to_query(update, deps, refreshed)


async def _send_card_to_query(
    update: Update, deps: BotDeps, draft: Draft, resolution: Resolution | None = None
) -> None:
    payload: dict[str, Any] = {"draft": _to_payload(draft)}
    if resolution is not None and resolution.renamed_from:
        payload["renamed_from"] = resolution.renamed_from
    token = await deps.tokens.issue(DRAFT_ACTION, payload)
    await _reply_to_query(update, render_draft(draft), draft_keyboard(token))


# --- Pending input --------------------------------------------------------


async def pending_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Consume the next message when a draft is waiting for one.

    Registered ahead of the browse flows and the search handler. A kind this
    module does not own is left alone — deliberately without popping it — so
    the browse handler still sees its own pending edits.
    """
    message = update.effective_message
    if message is None or not message.text or context.user_data is None:
        return

    pending = context.user_data.get(PENDING_KEY)
    if not isinstance(pending, dict):
        return

    kind = str(pending.get("kind", ""))
    if kind == "template_path":
        context.user_data.pop(PENDING_KEY, None)
        await _create_from_template(
            update, deps_of(context), str(pending["template"]), message.text
        )
        raise ApplicationHandlerStop

    if kind not in DRAFT_KINDS:
        return

    context.user_data.pop(PENDING_KEY, None)
    deps = deps_of(context)
    draft = _from_payload(pending.get("draft"))
    if draft is None:
        await _reply(update, esc(STALE))
        raise ApplicationHandlerStop

    if kind == DRAFT_TITLE:
        title = message.text.strip()
        updated = draft.with_title(title)
        # A retitled note that was going to be named after its title should
        # follow the new one, or the preview would name a file the user has
        # just renamed away from.
        resolution = await _capture(deps).resolve(draft.intent.path, title=title)
        updated = updated.with_path(resolution.path)
    else:
        resolution = await _capture(deps).resolve(message.text.strip(), title=draft.title)
        if resolution.ambiguous:
            token = await deps.tokens.issue(
                DRAFT_ACTION,
                {"draft": _to_payload(draft), "candidates": list(resolution.candidates)},
            )
            await _reply(
                update,
                render_ambiguity(message.text.strip(), resolution),
                ambiguity_keyboard(token, resolution.candidates),
            )
            raise ApplicationHandlerStop
        updated = draft.with_path(resolution.path)

    await _send_card(update, deps, updated, resolution)
    raise ApplicationHandlerStop


async def _create_from_template(update: Update, deps: BotDeps, template: str, path: str) -> None:
    try:
        ref = await _capture(deps).from_template(template, path.strip())
    except (NoteStoreError, InvalidRequest) as exc:
        await _reply(update, esc(str(exc)))
        return
    deps.count("notes_created")
    await _reply(update, render_saved(ref.path))


# --- Assist ---------------------------------------------------------------


async def summarize_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    deps = deps_of(context)
    path = _argument(context)
    if not path:
        await _reply(update, _escaped_usage("Usage: `/summarize <path>`"))
        return

    service = _assist(deps)
    if not service.available:
        await _reply(update, esc(service.unavailable_reason()))
        return

    answer = await service.summarise(path, user_id=_user_id(update))
    deps.count("summaries")
    await _reply(update, render_answer(answer.text, tuple(ref.path for ref in answer.sources)))


async def ask_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    deps = deps_of(context)
    question = _argument(context)
    if not question:
        await _reply(update, _escaped_usage(ASK_USAGE))
        return

    service = _assist(deps)
    if not service.available:
        await _reply(update, esc(service.unavailable_reason()))
        return

    notice = await _reply(update, esc("Reading your notes…"))
    try:
        answer = await service.ask(question, user_id=_user_id(update))
    finally:
        await _delete(notice)

    deps.count("questions")

    if answer.found_nothing:
        await _reply(
            update,
            esc("I could not find an answer in your notes.")
            + (
                "\n" + esc(f"I read {len(answer.sources)} note(s) and none of them said.")
                if answer.sources
                else ""
            ),
        )
        return

    await _reply(update, render_answer(answer.text, tuple(ref.path for ref in answer.sources)))


# --- Draft serialisation --------------------------------------------------
#
# A draft crosses the session store, which is JSON. `Draft` is a frozen
# dataclass with a nested intent, so it is written out explicitly rather than
# reflected over: a field added later must be handled here on purpose, not
# silently dropped by a generic encoder.


def _to_payload(draft: Draft) -> dict[str, Any]:
    return {
        "body": draft.body,
        "title": draft.title,
        "tags": list(draft.tags),
        "path": draft.path,
        "summary": draft.summary,
        "warnings": list(draft.warnings),
        "attachments": list(draft.attachments),
        "provider": draft.provenance.provider if draft.provenance else "",
        "model": draft.provenance.model if draft.provenance else "",
        "intent": {
            "path": draft.intent.path,
            "read_image": draft.intent.read_image,
            "verbatim": draft.intent.verbatim,
            "generate_title": draft.intent.generate_title,
            "generate_tags": draft.intent.generate_tags,
            "generate_summary": draft.intent.generate_summary,
            "note_text": draft.intent.note_text,
        },
    }


def _from_payload(raw: Any) -> Draft | None:
    if not isinstance(raw, dict):
        return None
    nested = raw.get("intent")
    intent_raw: dict[str, Any] = nested if isinstance(nested, dict) else {}
    provider = str(raw.get("provider", ""))
    model = str(raw.get("model", ""))
    return Draft(
        body=str(raw.get("body", "")),
        title=str(raw.get("title", "")),
        tags=tuple(str(tag) for tag in raw.get("tags", [])),
        path=str(raw.get("path", "")),
        summary=str(raw.get("summary", "")),
        warnings=tuple(str(warning) for warning in raw.get("warnings", [])),
        attachments=tuple(str(path) for path in raw.get("attachments", [])),
        provenance=Provenance(provider=provider, model=model) if provider or model else None,
        intent=CaptureIntent(
            path=str(intent_raw.get("path", "")),
            read_image=bool(intent_raw.get("read_image", True)),
            verbatim=bool(intent_raw.get("verbatim", False)),
            generate_title=bool(intent_raw.get("generate_title", True)),
            generate_tags=bool(intent_raw.get("generate_tags", True)),
            generate_summary=bool(intent_raw.get("generate_summary", False)),
            note_text=str(intent_raw.get("note_text", "")),
        ),
    )


# --- Helpers --------------------------------------------------------------


def _user_id(update: Update) -> int | None:
    return update.effective_user.id if update.effective_user else None


def _escaped_usage(text: str) -> str:
    parts = text.split("`")
    return "`".join(esc(part) if index % 2 == 0 else part for index, part in enumerate(parts))


async def _reply(target: Any, text: str, markup: Any = None, *, to_message: bool = False) -> Any:
    """Reply to an update, or directly to a message when that is all we have."""
    message = target if to_message else getattr(target, "effective_message", None)
    if message is None:
        return None
    return await message.reply_text(text, reply_markup=markup)


async def _reply_to_query(update: Update, text: str, markup: Any) -> None:
    query = update.callback_query
    if query is None:
        return
    if isinstance(query.message, Message):
        await query.message.reply_text(text, reply_markup=markup)
        return
    chat = update.effective_chat
    if chat is not None:
        await query.get_bot().send_message(chat_id=chat.id, text=text, reply_markup=markup)


async def _delete(message: Any) -> None:
    """Remove a transient "working…" message, tolerating a bot that cannot.

    Deleting is best effort by design: in a group the bot may lack the right,
    and a leftover notice is a far smaller problem than a failed capture.
    """
    if message is None:
        return
    try:
        await message.delete()
    except Exception as exc:  # the notice is cosmetic; the capture is not
        log.debug("notice_delete_failed", error=str(exc))


COMMANDS = {
    "new": new_command,
    "quick": quick_command,
    "template": template_command,
    "summarize": summarize_command,
    "ask": ask_command,
}

__all__ = [
    "COMMANDS",
    "DRAFT_KINDS",
    "DRAFT_PATH",
    "DRAFT_TITLE",
    "TEMPLATE_ACTION",
    "ask_command",
    "attachment_message",
    "default_text_capture",
    "draft_callback",
    "forwarded_message",
    "pending_input",
    "summarize_command",
    "template_callback",
]
