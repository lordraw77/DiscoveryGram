"""Client-side ranking, snippet cleaning and literal filtering.

NoteDiscovery's search returns no relevance score. It *does* return up to three
matched lines per note, wrapped in HTML (`<mark class="search-highlight">`) and
HTML-escaped — that markup is stripped here, because it would collide with
Telegram's own formatting.

Ranking rules, in order of weight:

1. the query appears in the note **title** — the strongest signal a user gives;
2. the title starts with the query, or equals it — stronger still;
3. how many times the term occurs in the matched lines;
4. how early in the note the first match sits;
5. recency, as a small tie-breaker between otherwise equal notes.
"""

from __future__ import annotations

import html
import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime

from discoverygram.ports.model import SearchHit, SearchMatch

_MARK_OPEN = re.compile(r"<mark[^>]*>", re.IGNORECASE)
_MARK_CLOSE = re.compile(r"</mark\s*>", re.IGNORECASE)
_ANY_TAG = re.compile(r"<[^>]+>")

TITLE_HIT_WEIGHT = 100.0
TITLE_PREFIX_BONUS = 30.0
TITLE_EXACT_BONUS = 60.0
MATCH_WEIGHT = 5.0
EARLY_LINE_WEIGHT = 10.0
RECENCY_WEIGHT = 5.0


def clean_snippet(raw: str) -> tuple[str, str]:
    """Turn one HTML snippet into `(plain_text, matched_text)`.

    The matched term is recovered from inside the `<mark>` element before the
    tags are dropped, so the renderer can re-highlight it in Telegram's own
    syntax later without re-running the search.
    """
    matched = ""
    marked = re.search(r"<mark[^>]*>(.*?)</mark\s*>", raw, re.IGNORECASE | re.DOTALL)
    if marked:
        matched = html.unescape(_ANY_TAG.sub("", marked.group(1))).strip()

    text = _MARK_CLOSE.sub("", _MARK_OPEN.sub("", raw))
    text = _ANY_TAG.sub("", text)
    return html.unescape(text).strip(), matched


def parse_matches(
    raw_matches: Iterable[object], *, html_escaped: bool = True
) -> tuple[SearchMatch, ...]:
    """Normalise a `{line_number, context}` array.

    Search results arrive HTML-escaped and marked up; backlink references do not,
    so `html_escaped=False` leaves their context untouched.
    """
    parsed: list[SearchMatch] = []
    for item in raw_matches:
        if not isinstance(item, Mapping):
            continue
        context = item.get("context")
        if context is None:
            snippet, matched = "", ""
        elif html_escaped:
            snippet, matched = clean_snippet(str(context))
        else:
            snippet, matched = str(context).strip(), ""
        line_raw = item.get("line_number", 0)
        line = int(line_raw) if isinstance(line_raw, int | float | str) and line_raw != "" else 0
        parsed.append(SearchMatch(line_number=line, snippet=snippet, matched_text=matched))
    return tuple(parsed)


def _recency_factor(modified: datetime | None, *, now: datetime | None = None) -> float:
    """0.0 for something ancient, 1.0 for something touched right now."""
    if modified is None:
        return 0.0
    reference = now or datetime.now(UTC)
    if modified.tzinfo is None:
        modified = modified.replace(tzinfo=UTC)
    age_days = max((reference - modified).total_seconds() / 86400.0, 0.0)
    # Half-life of roughly a month; never negative, never above 1.
    return 1.0 / (1.0 + age_days / 30.0)


def score_hit(hit: SearchHit, query: str, *, now: datetime | None = None) -> SearchHit:
    """Return `hit` with `score` and `title_match` filled in."""
    needle = query.strip().casefold()
    title = hit.ref.title.casefold()

    score = 0.0
    title_match = bool(needle) and needle in title
    if title_match:
        score += TITLE_HIT_WEIGHT
        if title.startswith(needle):
            score += TITLE_PREFIX_BONUS
        if title == needle:
            score += TITLE_EXACT_BONUS

    occurrences = (
        sum(
            match.snippet.casefold().count(needle) or (1 if match.matched_text else 0)
            for match in hit.matches
        )
        if needle
        else 0
    )
    score += MATCH_WEIGHT * occurrences

    lines = [match.line_number for match in hit.matches if match.line_number > 0]
    if lines:
        # An early first hit beats a late one, with sharply diminishing returns.
        score += EARLY_LINE_WEIGHT / (1.0 + min(lines) / 10.0)

    score += RECENCY_WEIGHT * _recency_factor(hit.ref.modified, now=now)

    return SearchHit(ref=hit.ref, matches=hit.matches, score=score, title_match=title_match)


def rank(hits: Iterable[SearchHit], query: str, *, now: datetime | None = None) -> list[SearchHit]:
    """Score and order results. Ties break on path, so pagination is stable."""
    scored = [score_hit(hit, query, now=now) for hit in hits]
    scored.sort(key=lambda hit: (-hit.score, hit.ref.path.casefold()))
    return scored


def filter_literal(hits: Iterable[SearchHit], query: str) -> list[SearchHit]:
    """Keep only hits whose title or snippets contain `query` **case-sensitively**.

    NoteDiscovery has one search mode and it is case-insensitive substring
    matching, so `/find` is a refinement of `/search` rather than a second
    backend call. Snippets carry only ±15 characters of context, which is why
    `RestNoteStore.search_literal` can fall back to reading candidate bodies.
    """
    needle = query.strip()
    if not needle:
        return list(hits)

    kept: list[SearchHit] = []
    for hit in hits:
        if needle in hit.ref.title or any(needle in match.snippet for match in hit.matches):
            kept.append(hit)
    return kept
