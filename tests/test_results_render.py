"""Rendering a page of results.

Note titles, folders and snippets are arbitrary vault content, so every test
here asserts the output would survive the Bot API as well as reading correctly.
"""

from __future__ import annotations

import pytest

from discoverygram.app.search import ResultSet, SearchMode
from discoverygram.bot.results import (
    SNIPPET_LIMIT,
    highlight,
    page_keyboard,
    render_hit,
    render_page,
    render_tag_list,
)
from discoverygram.ports.model import NoteRef, SearchHit, SearchMatch
from tests.fixtures.telegram import assert_markdown_v2_safe


def hit(
    path: str,
    *,
    title: str | None = None,
    snippets: tuple[str, ...] = (),
    tags: tuple[str, ...] = (),
) -> SearchHit:
    ref = NoteRef(
        path=path,
        title=title if title is not None else path.rpartition("/")[2].removesuffix(".md"),
        folder=path.rpartition("/")[0],
        tags=tags,
    )
    return SearchHit(
        ref=ref,
        matches=tuple(SearchMatch(index + 1, text) for index, text in enumerate(snippets)),
    )


def results(
    hits: list[SearchHit],
    *,
    mode: SearchMode = SearchMode.FULL_TEXT,
    query: str = "docker",
    page_size: int = 5,
    truncated: bool = False,
) -> ResultSet:
    return ResultSet(
        mode=mode, query=query, hits=tuple(hits), page_size=page_size, truncated=truncated
    )


# --- Highlighting ---------------------------------------------------------


def test_the_term_is_bolded_inside_the_snippet() -> None:
    assert highlight("a note about docker", "docker") == "a note about *docker*"


def test_highlighting_is_case_insensitive() -> None:
    """NoteDiscovery's only search mode is case-insensitive, so hits differ in case."""
    assert highlight("DOCKER and docker", "docker") == "*DOCKER* and *docker*"


def test_highlighting_escapes_before_it_marks_up() -> None:
    """Escaping afterwards would mangle the markers the highlighter just inserted."""
    result = highlight("costs (draft) for docker.", "docker")

    assert "\\(" in result and "\\)" in result
    assert "*docker*" in result
    assert result.endswith("\\.")
    assert_markdown_v2_safe(result)


def test_a_term_with_regex_metacharacters_is_not_a_pattern() -> None:
    """The term comes from the vault; `a.b` must not match `axb`."""
    assert "*" not in highlight("axb", "a.b")
    assert "*a\\.b*" in highlight("a.b here", "a.b")


def test_an_empty_term_leaves_the_snippet_escaped_but_unmarked() -> None:
    assert highlight("plain (text)", "  ") == "plain \\(text\\)"


# --- One hit --------------------------------------------------------------


def test_a_hit_shows_its_title_folder_and_snippets() -> None:
    rendered = render_hit(
        hit("Projects/Ideas.md", snippets=("uses docker daily",)), "docker", index=1
    )

    assert "Ideas" in rendered
    assert "Projects" in rendered
    assert "*docker*" in rendered
    assert_markdown_v2_safe(rendered)


def test_a_pathological_title_is_still_sendable() -> None:
    rendered = render_hit(
        hit("a/x.md", title="Q1 2026 — costs (draft) [v2]!", snippets=("x",)), "x", index=1
    )

    assert_markdown_v2_safe(rendered)


def test_a_root_level_note_shows_a_root_marker() -> None:
    assert "`/`" in render_hit(hit("Welcome.md"), "", index=1)


def test_only_the_first_snippets_are_shown() -> None:
    """NoteDiscovery returns up to three; three per hit is a wall of text."""
    rendered = render_hit(hit("a.md", snippets=("one", "two", "three")), "", index=1)

    assert "three" not in rendered


def test_a_long_snippet_is_truncated() -> None:
    rendered = render_hit(hit("a.md", snippets=("x" * 500,)), "", index=1)

    assert len(rendered) < 500 + SNIPPET_LIMIT


def test_a_hit_without_snippets_falls_back_to_its_tags() -> None:
    """Tag and recent results carry no matches, so the line would be bare."""
    rendered = render_hit(hit("a.md", tags=("planning", "docker")), "", index=1)

    assert "planning" in rendered
    assert_markdown_v2_safe(rendered)


# --- A page ---------------------------------------------------------------


def test_a_page_numbers_results_by_their_position_in_the_whole_set() -> None:
    """Page two starts at 6, not at 1 — otherwise the count means nothing."""
    page = render_page(results([hit(f"n{index}.md") for index in range(8)]), 2)

    assert "*6\\.*" in page
    assert "*1\\.*" not in page


def test_the_header_states_how_many_results_there_are() -> None:
    assert "3 results" in render_page(results([hit(f"n{i}.md") for i in range(3)]), 1)


def test_one_result_is_not_called_results() -> None:
    assert "1 result" in render_page(results([hit("a.md")]), 1)


def test_a_truncated_set_says_so_rather_than_implying_completeness() -> None:
    page = render_page(results([hit("a.md")], truncated=True), 1)

    assert "\\+" in page
    assert "narrow the query" in page


@pytest.mark.parametrize(
    ("mode", "query", "fragment"),
    [
        (SearchMode.FULL_TEXT, "docker", "No notes match"),
        (SearchMode.LITERAL, "Docker", "exactly"),
        (SearchMode.TAG, "planning", "tagged"),
        (SearchMode.RECENT, "7", "changed"),
    ],
)
def test_each_mode_has_its_own_empty_message(mode: SearchMode, query: str, fragment: str) -> None:
    """ "No results" should say what was actually looked for."""
    page = render_page(results([], mode=mode, query=query), 1)

    assert fragment in page
    assert_markdown_v2_safe(page)


def test_a_tag_query_is_not_highlighted_in_bodies() -> None:
    """A tag name does not appear in the snippet text, so marking it up is noise."""
    page = render_page(
        results(
            [hit("a.md", snippets=("mentions planning",))], mode=SearchMode.TAG, query="planning"
        ),
        1,
    )

    assert "*planning*" not in page.split("\n", 1)[1]


def test_every_mode_renders_within_telegram_s_message_limit() -> None:
    """Five hits with long titles and snippets must still fit one message."""
    hits = [
        hit(f"Projects/Very Long Folder Name/{'title' * 12}-{index}.md", snippets=("x" * 400,))
        for index in range(5)
    ]

    page = render_page(results(hits), 1)

    assert len(page) <= 4096


# --- Pagination keyboard --------------------------------------------------


def test_a_single_page_gets_no_buttons() -> None:
    assert page_keyboard(results([hit("a.md")]), 1, "page:tok") == []


def test_the_first_page_offers_only_forward() -> None:
    rows = page_keyboard(results([hit(f"n{i}.md") for i in range(8)]), 1, "page:tok")

    assert [label for label, _ in rows[0]] == ["1/2", "▶"]


def test_the_last_page_offers_only_back() -> None:
    rows = page_keyboard(results([hit(f"n{i}.md") for i in range(8)]), 2, "page:tok")

    assert [label for label, _ in rows[0]] == ["◀", "2/2"]


def test_page_buttons_carry_their_number_in_the_callback_data() -> None:
    """One stored result set serves every page, so turning pages leaks no state."""
    rows = page_keyboard(results([hit(f"n{i}.md") for i in range(20)]), 2, "page:abc123")

    data = dict(rows[0])
    assert data["◀"] == "page:abc123:1"
    assert data["▶"] == "page:abc123:3"


def test_page_buttons_stay_within_the_callback_limit_at_high_page_numbers() -> None:
    """Twenty page turns must not produce data Telegram rejects."""
    many = results([hit(f"n{i}.md") for i in range(200)])

    for page in range(1, many.pages + 1):
        for _, data in page_keyboard(many, page, "page:0011223344ff")[0]:
            assert len(data.encode()) <= 64


# --- Tag listing ----------------------------------------------------------


def test_the_tag_list_shows_counts_and_how_to_open_one() -> None:
    rendered = render_tag_list({"planning": 9, "docker": 2})

    assert "planning" in rendered
    assert "/tag" in rendered
    assert_markdown_v2_safe(rendered)


def test_an_empty_vault_says_so() -> None:
    assert "no tags" in render_tag_list({})


def test_a_huge_tag_list_is_capped() -> None:
    rendered = render_tag_list({f"tag{index}": 1 for index in range(200)}, limit=10)

    assert "190 more" in rendered
    assert len(rendered) <= 4096
