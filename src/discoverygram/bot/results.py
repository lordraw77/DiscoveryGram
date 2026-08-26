"""Rendering a page of search results.

Everything reaching Telegram is escaped first and marked up second — the note
titles, paths and snippets are arbitrary vault content, and one unescaped
MarkdownV2 character makes the Bot API reject the entire message.

Highlighting is applied *after* escaping. The matched term is escaped the same
way the snippet was, so the two still line up, and the occurrences found in the
escaped text are wrapped in bold. Doing it the other way round would let the
escaper mangle the markers it had just inserted.
"""

from __future__ import annotations

import re

from discoverygram.app.search import ResultSet, SearchMode
from discoverygram.bot.render import escape_markdown_v2 as esc
from discoverygram.bot.render import pagination_row, truncate
from discoverygram.bot.tokens import CallbackTokens
from discoverygram.ports.model import SearchHit

# Long enough to be useful, short enough that five results still fit one message.
SNIPPET_LIMIT = 160
TITLE_LIMIT = 64
# More than this per hit and the page becomes a wall of text.
MAX_SNIPPETS_PER_HIT = 2

_EMPTY = {
    SearchMode.FULL_TEXT: "No notes match *{query}*\\.",
    SearchMode.LITERAL: "No notes contain *{query}* exactly\\.",
    SearchMode.TAG: "No notes are tagged *{query}*\\.",
    SearchMode.RECENT: "Nothing has changed in the last {query} days\\.",
}

# Modes whose query is a term that actually appears in note bodies. A tag name
# or a day count does not, so highlighting them would mark up unrelated text.
_HIGHLIGHTED_MODES = frozenset({SearchMode.FULL_TEXT, SearchMode.LITERAL})

_HEADER = {
    SearchMode.FULL_TEXT: "🔍 *{count}* for *{query}*",
    SearchMode.LITERAL: "🔍 *{count}* containing *{query}* exactly",
    SearchMode.TAG: "🏷 *{count}* tagged *{query}*",
    SearchMode.RECENT: "🕒 *{count}* changed in the last {query} days",
}


def highlight(text: str, term: str) -> str:
    """Escape `text`, then bold every occurrence of `term` inside it."""
    escaped = esc(text)
    needle = esc(term.strip())
    if not needle:
        return escaped

    # The term came from the vault, so it can contain regex metacharacters.
    pattern = re.compile(re.escape(needle), re.IGNORECASE)
    return pattern.sub(lambda match: f"*{match.group(0)}*", escaped)


def _count_phrase(total: int, *, truncated: bool) -> str:
    noun = "result" if total == 1 else "results"
    return f"{total}\\+ {noun}" if truncated else f"{total} {noun}"


def render_hit(hit: SearchHit, term: str, *, index: int) -> str:
    """One result: a numbered title, its folder, and up to two snippets."""
    title = esc(truncate(hit.ref.title or hit.ref.path, TITLE_LIMIT))
    lines = [f"*{index}\\.* {title}"]

    location = hit.ref.folder or "/"
    lines.append(f"   `{location}`")

    for match in hit.matches[:MAX_SNIPPETS_PER_HIT]:
        snippet = match.snippet.strip()
        if not snippet:
            continue
        lines.append(f"   _{highlight(truncate(snippet, SNIPPET_LIMIT), term)}_")

    if not hit.matches and hit.ref.tags:
        tags = " ".join(f"\\#{esc(tag)}" for tag in hit.ref.tags[:5])
        lines.append(f"   {tags}")

    return "\n".join(lines)


def render_page(results: ResultSet, page: int) -> str:
    """The message body for one page of a result set."""
    page = results.clamp(page)
    query = esc(results.query)

    if results.is_empty:
        return _EMPTY[results.mode].format(query=query)

    header = _HEADER[results.mode].format(
        count=_count_phrase(len(results.hits), truncated=results.truncated),
        query=query,
    )

    # The term to highlight is the query itself, except for modes where the
    # query is a tag or a day count rather than something that appears in a body.
    term = results.query if results.mode in _HIGHLIGHTED_MODES else ""

    offset = (page - 1) * results.page_size
    body = [
        render_hit(hit, term, index=offset + position)
        for position, hit in enumerate(results.page(page), start=1)
    ]

    parts = [header, "", *body]
    if results.truncated and page == results.pages:
        parts += ["", esc("More results exist — narrow the query to see them.")]
    return "\n".join(parts)


def page_keyboard(results: ResultSet, page: int, base_callback: str) -> list[list[tuple[str, str]]]:
    """The `◀ n/m ▶` row, or nothing when everything fits on one page."""
    if results.pages <= 1:
        return []

    page = results.clamp(page)
    return [
        pagination_row(
            previous_data=(CallbackTokens.with_args(base_callback, page - 1) if page > 1 else None),
            next_data=(
                CallbackTokens.with_args(base_callback, page + 1) if page < results.pages else None
            ),
            page=page,
            pages=results.pages,
        )
    ]


def hit_buttons(results: ResultSet, page: int, base_callback: str) -> list[list[tuple[str, str]]]:
    """One `Open` button per hit on the current page.

    The buttons carry an **index into the stored result set**, not a path, so
    they reuse the token pagination already issued. A token per hit would mean
    five new session entries on every page turn — bounded by the TTL, but
    growing with nothing but browsing.
    """
    rows: list[list[tuple[str, str]]] = []
    offset = (results.clamp(page) - 1) * results.page_size
    for position, hit in enumerate(results.page(page), start=1):
        index = offset + position
        label = f"{index}. {truncate(hit.ref.title or hit.ref.path, 30)}"
        rows.append([(label, CallbackTokens.with_args(base_callback, f"h{index}"))])
    return rows


def render_tag_list(counts: dict[str, int], *, limit: int = 40) -> str:
    """`/tag` with no argument: what tags exist and how used they are."""
    if not counts:
        return esc("This vault has no tags yet.")

    shown = list(counts.items())[:limit]
    lines = [f"🏷 *{len(counts)}* tags", ""]
    lines += [f"`{esc(name)}` — {count}" for name, count in shown]
    if len(counts) > limit:
        lines += ["", esc(f"…and {len(counts) - limit} more.")]
    lines += ["", esc("Open one with /tag <name>.")]
    return "\n".join(lines)


__all__ = [
    "SNIPPET_LIMIT",
    "highlight",
    "page_keyboard",
    "render_hit",
    "render_page",
    "render_tag_list",
]
