"""Rendering a note, a folder listing and a backlink list for Telegram.

A note body is arbitrary vault content: tables, HTML, code fences, emoji, and
every one of MarkdownV2's eighteen reserved characters. One unescaped occurrence
makes the Bot API reject the whole message, so the body is escaped in full and
never interpreted.

Long notes exceed Telegram's 4096-character limit. `LONG_NOTE_MODE` chooses what
happens:

* `paged` — one message with `◀ n/m ▶`, edited in place as the reader turns pages;
* `split` — the note sent as consecutive messages, which is better for reading
  straight through and worse for a chat history.
"""

from __future__ import annotations

from datetime import datetime

from discoverygram.app.navigation import Entry, FolderView, WikiLink
from discoverygram.bot.render import MESSAGE_LIMIT, chunk_text, pagination_row, truncate
from discoverygram.bot.render import escape_markdown_v2 as esc
from discoverygram.ports.model import Backlink, Note, NoteRef

TITLE_LIMIT = 80
# Room for the header, the action bar hint and the page footer.
BODY_BUDGET = MESSAGE_LIMIT - 400
MAX_TAGS_SHOWN = 12
BACKLINK_CONTEXT_LIMIT = 120

FOLDER_ICON = "📁"
NOTE_ICON = "📄"


def format_timestamp(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M") if value else "unknown"


def note_header(note: Note) -> str:
    """Title, path, tags and timestamps — everything but the body."""
    lines = [f"*{esc(truncate(note.title or note.path, TITLE_LIMIT))}*", f"`{esc(note.path)}`"]

    if note.tags:
        shown = note.tags[:MAX_TAGS_SHOWN]
        tags = " ".join(f"\\#{esc(tag)}" for tag in shown)
        if len(note.tags) > len(shown):
            tags += esc(f" +{len(note.tags) - len(shown)}")
        lines.append(tags)

    modified = format_timestamp(note.modified)
    created = format_timestamp(note.created)
    detail = f"{note.lines} lines · modified {modified}"
    if created != modified:
        detail += f" · created {created}"
    lines.append(f"_{esc(detail)}_")
    return "\n".join(lines)


def body_pages(note: Note, *, budget: int = BODY_BUDGET) -> list[str]:
    """The escaped body, split into messages that fit.

    Escaping happens **before** chunking so a chunk boundary can never land
    between a backslash and the character it escapes.
    """
    body = note.content.strip()
    if not body:
        return [esc("(this note is empty)")]
    return chunk_text(esc(body), limit=budget)


def render_note(note: Note, page: int = 1, *, pages: list[str] | None = None) -> str:
    """One message: the header, then the requested page of the body."""
    parts = pages if pages is not None else body_pages(note)
    index = max(1, min(page, len(parts)))
    rendered = [note_header(note), "", parts[index - 1]]
    if len(parts) > 1:
        rendered += ["", esc(f"— page {index} of {len(parts)} —")]
    return "\n".join(rendered)


def render_note_split(note: Note) -> list[str]:
    """The whole note as consecutive messages, header on the first."""
    parts = body_pages(note)
    messages = [f"{note_header(note)}\n\n{parts[0]}"]
    messages.extend(parts[1:])
    return messages


def render_folder(view: FolderView, page: int) -> str:
    """A folder listing: breadcrumb, then its children as text.

    The entries are also buttons; they are listed in the body as well so the
    message still reads as something on a narrow screen or in a quoted reply.
    """
    page = view.clamp(page)
    crumb = esc(" / ".join(label for _, label in view.crumbs))
    header = f"{FOLDER_ICON} *{esc(view.name)}*\n`{crumb}`"

    if view.is_empty:
        return f"{header}\n\n{esc('This folder is empty.')}"

    lines = []
    for entry in view.page(page):
        if entry.is_folder:
            count = esc(f" ({entry.children})") if entry.children else ""
            lines.append(f"{FOLDER_ICON} {esc(entry.title)}{count}")
        else:
            lines.append(f"{NOTE_ICON} {esc(entry.title)}")

    total = esc(f"{len(view.entries)} items")
    return "\n".join([header, f"_{total}_", "", *lines])


def entry_label(entry: Entry) -> str:
    """A button label: an icon and a title that fits on one line."""
    icon = FOLDER_ICON if entry.is_folder else NOTE_ICON
    return f"{icon} {truncate(entry.title, 28)}"


def render_backlinks(path: str, backlinks: list[Backlink]) -> str:
    if not backlinks:
        return f"{esc('Nothing links to')} `{esc(path)}`{esc('.')}"

    lines = [f"🔗 *{len(backlinks)}* {esc('notes link to')} `{esc(path)}`", ""]
    for link in backlinks:
        lines.append(f"{NOTE_ICON} *{esc(truncate(link.title, TITLE_LIMIT))}*")
        lines.append(f"   `{esc(link.path)}`")
        for reference in link.references[:1]:
            snippet = truncate(reference.snippet.strip(), BACKLINK_CONTEXT_LIMIT)
            if snippet:
                lines.append(f"   _{esc(snippet)}_")
    return "\n".join(lines)


def render_related(path: str, refs: list[NoteRef]) -> str:
    if not refs:
        return f"{esc('No notes are linked to')} `{esc(path)}`{esc('.')}"

    lines = [f"🕸 *{len(refs)}* {esc('notes linked with')} `{esc(path)}`", ""]
    lines += [
        f"{NOTE_ICON} {esc(truncate(ref.title, TITLE_LIMIT))}  `{esc(ref.folder or '/')}`"
        for ref in refs
    ]
    return "\n".join(lines)


def unresolved_links_note(links: list[WikiLink]) -> str:
    """A line naming `[[links]]` that point nowhere, or `""`.

    A broken wiki-link is a fact about the vault worth surfacing, not something
    to hide by dropping the button silently.
    """
    broken = [link.target for link in links if not link.resolved]
    if not broken:
        return ""
    names = ", ".join(truncate(name, 24) for name in broken[:4])
    return esc(f"Unresolved links: {names}")


def note_page_row(
    page: int, pages: int, *, previous_data: str | None, next_data: str | None
) -> list[tuple[str, str]]:
    return pagination_row(previous_data=previous_data, next_data=next_data, page=page, pages=pages)


__all__ = [
    "BODY_BUDGET",
    "body_pages",
    "entry_label",
    "format_timestamp",
    "note_header",
    "note_page_row",
    "render_backlinks",
    "render_folder",
    "render_note",
    "render_note_split",
    "render_related",
    "unresolved_links_note",
]
