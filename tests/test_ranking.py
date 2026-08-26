"""Client-side ranking, snippet cleaning and literal filtering."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from discoverygram.adapters.ranking import (
    clean_snippet,
    filter_literal,
    parse_matches,
    rank,
    score_hit,
)
from discoverygram.ports.model import NoteRef, SearchHit, SearchMatch

NOW = datetime(2026, 8, 26, tzinfo=UTC)


def hit(
    path: str,
    *,
    title: str | None = None,
    matches: tuple[SearchMatch, ...] = (),
    modified: datetime | None = None,
) -> SearchHit:
    ref = NoteRef(
        path=path,
        title=title if title is not None else path.rpartition("/")[2].removesuffix(".md"),
        modified=modified,
    )
    return SearchHit(ref=ref, matches=matches)


def test_clean_snippet_strips_highlight_markup_and_unescapes() -> None:
    raw = '...notes on <mark class="search-highlight">docker</mark> &amp; k8s...'

    text, matched = clean_snippet(raw)

    assert text == "...notes on docker & k8s..."
    assert matched == "docker"


def test_clean_snippet_without_markup_is_left_alone() -> None:
    assert clean_snippet("plain context") == ("plain context", "")


def test_parse_matches_normalises_and_skips_junk() -> None:
    matches = parse_matches(
        [
            {"line_number": 3, "context": '<mark class="search-highlight">x</mark>'},
            "not a record",
            {"line_number": "7", "context": "plain"},
        ]
    )

    assert [(m.line_number, m.snippet) for m in matches] == [(3, "x"), (7, "plain")]


def test_parse_matches_leaves_backlink_context_unescaped() -> None:
    """Backlink references are plain text already; escaping them twice mangles them."""
    (match,) = parse_matches([{"line_number": 1, "context": "see [[A & B]]"}], html_escaped=False)

    assert match.snippet == "see [[A & B]]"


def test_title_hit_outranks_body_hit() -> None:
    """The strongest relevance signal NoteDiscovery does not give us."""
    body_only = hit("Notes/Misc.md", matches=(SearchMatch(1, "mentions docker once"),))
    in_title = hit("Notes/docker-setup.md", matches=(SearchMatch(400, "unrelated line"),))

    ordered = rank([body_only, in_title], "docker", now=NOW)

    assert [h.path for h in ordered] == ["Notes/docker-setup.md", "Notes/Misc.md"]
    assert ordered[0].title_match is True
    assert ordered[1].title_match is False


def test_exact_title_outranks_partial_title() -> None:
    partial = hit("a/docker-setup.md")
    exact = hit("b/docker.md")

    ordered = rank([partial, exact], "docker", now=NOW)

    assert [h.path for h in ordered] == ["b/docker.md", "a/docker-setup.md"]


def test_more_occurrences_rank_higher_among_body_hits() -> None:
    few = hit("a/one.md", matches=(SearchMatch(50, "docker"),))
    many = hit(
        "b/two.md",
        matches=(SearchMatch(50, "docker docker"), SearchMatch(60, "docker again")),
    )

    ordered = rank([few, many], "docker", now=NOW)

    assert [h.path for h in ordered] == ["b/two.md", "a/one.md"]


def test_recency_breaks_ties_between_equal_notes() -> None:
    stale = hit(
        "a/one.md",
        title="one",
        matches=(SearchMatch(10, "docker"),),
        modified=NOW - timedelta(days=365),
    )
    fresh = hit("b/two.md", title="two", matches=(SearchMatch(10, "docker"),), modified=NOW)

    ordered = rank([stale, fresh], "docker", now=NOW)

    assert [h.path for h in ordered] == ["b/two.md", "a/one.md"]


def test_ranking_is_stable_for_identical_scores() -> None:
    """Pagination turns pages against a re-run query; equal scores must not shuffle."""
    a = hit("z/alpha.md", title="alpha")
    b = hit("a/beta.md", title="beta")

    first = [h.path for h in rank([a, b], "nothing", now=NOW)]
    second = [h.path for h in rank([b, a], "nothing", now=NOW)]

    assert first == second == ["a/beta.md", "z/alpha.md"]


def test_score_is_never_negative() -> None:
    scored = score_hit(hit("a/x.md"), "absent", now=NOW)

    assert scored.score >= 0.0


def test_filter_literal_is_case_sensitive() -> None:
    """NoteDiscovery's only search mode is case-insensitive; `/find` narrows it."""
    upper = hit("a/Docker.md", title="Docker")
    lower = hit("b/notes.md", matches=(SearchMatch(1, "using docker here"),))

    kept = filter_literal([upper, lower], "docker")

    assert [h.path for h in kept] == ["b/notes.md"]


def test_filter_literal_passes_everything_through_for_an_empty_query() -> None:
    hits = [hit("a/x.md"), hit("b/y.md")]

    assert filter_literal(hits, "   ") == hits
