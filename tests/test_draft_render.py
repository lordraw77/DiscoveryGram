"""The preview card.

The card is the safety mechanism of the capture flow — nothing is written until
someone reads it — so it is asserted to show the things a person would regret
not seeing, and to survive the Bot API whatever a model called the note.
"""

from __future__ import annotations

import pytest
from telegram import InlineKeyboardMarkup

from discoverygram.app.capture import Provenance, Resolution
from discoverygram.app.ingest import Draft
from discoverygram.bot.drafts import (
    PREVIEW_CHARS,
    UNTITLED,
    ambiguity_keyboard,
    draft_keyboard,
    render_ambiguity,
    render_answer,
    render_draft,
    render_saved,
)
from discoverygram.bot.tokens import CallbackTokens, fits_in_callback_data
from tests.fixtures.telegram import assert_markdown_v2_safe

TOKEN = "dr:abcdef123456"


def _labels(markup: InlineKeyboardMarkup) -> list[str]:
    return [button.text for row in markup.inline_keyboard for button in row]


def _callbacks(markup: InlineKeyboardMarkup) -> list[str]:
    return [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if isinstance(button.callback_data, str)
    ]


# --- The card -------------------------------------------------------------


def test_the_card_names_the_path_above_everything_else() -> None:
    """Where it will be written is the thing worth reading before tapping Save."""
    text = render_draft(Draft(body="body", title="A title", path="Projects/A title.md"))

    assert "`Projects/A title.md`" in text
    assert text.index("Path:") < text.index("body")


def test_a_draft_with_no_title_says_so_rather_than_showing_a_blank() -> None:
    assert UNTITLED in render_draft(Draft(body="body", path="Inbox/x.md")).replace("\\", "")


def test_tags_and_attachments_are_shown_when_present() -> None:
    text = render_draft(
        Draft(body="b", path="p.md", tags=("one", "two"), attachments=("media/a.jpg",))
    )

    assert "#one #two" in text.replace("\\", "")
    assert "1 file" in text


def test_several_attachments_are_counted_in_the_plural() -> None:
    text = render_draft(Draft(body="b", path="p.md", attachments=("a.jpg", "b.jpg")))

    assert "2 files" in text


def test_a_draft_with_no_body_says_so() -> None:
    assert "no body text" in render_draft(Draft(title="T", path="p.md"))


def test_a_long_body_is_excerpted_so_the_card_stays_one_message() -> None:
    """A preview that had to be paged would stop being a preview."""
    text = render_draft(Draft(body="x" * 20_000, path="p.md"))

    assert len(text) < 2000
    assert "[…]" in text.replace("\\", "")


def test_a_short_body_is_not_truncated() -> None:
    body = "y" * (PREVIEW_CHARS - 1)

    assert "[…]" not in render_draft(Draft(body=body, path="p.md")).replace("\\", "")


def test_the_card_states_that_nothing_is_written_yet() -> None:
    assert "Nothing is written until you tap Save" in render_draft(
        Draft(body="b", path="p.md")
    ).replace("\\", "")


def test_warnings_are_shown_rather_than_hidden() -> None:
    """A degraded draft is still a draft; hiding the degradation is dishonest."""
    text = render_draft(Draft(body="b", path="p.md").warn("I could not read the image"))

    assert "could not read the image" in text


def test_provenance_is_shown_when_the_draft_was_generated() -> None:
    text = render_draft(
        Draft(body="b", path="p.md", provenance=Provenance(provider="groq", model="llama"))
    )

    assert "groq/llama" in text


def test_a_summary_is_shown_when_one_was_asked_for() -> None:
    assert "Summary:" in render_draft(Draft(body="b", path="p.md", summary="short"))


@pytest.mark.parametrize("char", list("_*[]()~`>#+-=|{}.!"))
def test_every_reserved_character_survives_in_a_title(char: str) -> None:
    """One unescaped character is a 400 for the whole message."""
    draft = Draft(body=f"body {char}", title=f"Title {char} here", path="p.md")

    assert_markdown_v2_safe(render_draft(draft))


def test_a_card_full_of_markdown_is_still_sendable() -> None:
    draft = Draft(
        body="| a | b |\n|---|---|\n```py\nprint('x')\n```\n![img](a.png) 100%",
        title="Q1 2026 — costs (draft) [v2]!",
        tags=("q1-2026",),
        path="Projects/Q1 2026 — costs (draft) [v2]!.md",
        summary="A summary. With punctuation!",
    ).warn("Something — went wrong.")

    assert_markdown_v2_safe(render_draft(draft))


# --- Keyboards ------------------------------------------------------------


def test_the_card_carries_the_five_buttons_the_roadmap_named() -> None:
    labels = _labels(draft_keyboard(TOKEN))

    assert labels == ["💾 Save", "✏️ Title", "📁 Path", "🔄 Regenerate", "✖ Cancel"]


def test_every_button_reuses_the_one_draft_token() -> None:
    tokens = {CallbackTokens.split(data)[1] for data in _callbacks(draft_keyboard(TOKEN))}

    assert tokens == {"abcdef123456"}


def test_every_button_fits_telegrams_callback_limit() -> None:
    for data in _callbacks(draft_keyboard(TOKEN)):
        assert fits_in_callback_data(data)


# --- Ambiguity ------------------------------------------------------------


def test_the_ambiguity_message_lists_every_candidate() -> None:
    resolution = Resolution(candidates=("Projects/Research", "Archive/Research"))

    text = render_ambiguity("Research", resolution)

    assert "Projects/Research" in text
    assert "Archive/Research" in text
    assert_markdown_v2_safe(text)


def test_each_candidate_becomes_a_button_plus_a_way_out() -> None:
    markup = ambiguity_keyboard(TOKEN, ("Projects/Research", "Archive/Research"))

    assert _labels(markup) == ["Projects/Research", "Archive/Research", "✖ Cancel"]


def test_a_very_long_candidate_is_shortened_at_the_front() -> None:
    """The end of a path is what identifies it."""
    deep = "A/very/deeply/nested/set/of/folders/that/goes/on/Research"
    labels = _labels(ambiguity_keyboard(TOKEN, (deep,)))

    assert labels[0].startswith("…")
    assert labels[0].endswith("Research")


def test_candidate_buttons_stay_inside_the_callback_limit() -> None:
    markup = ambiguity_keyboard(TOKEN, tuple(f"folder/{index}" for index in range(9)))

    for data in _callbacks(markup):
        assert fits_in_callback_data(data)


# --- Confirmations --------------------------------------------------------


def test_saving_names_the_path() -> None:
    assert "`Inbox/Note.md`" in render_saved("Inbox/Note.md")


def test_a_renamed_note_says_it_was_renamed() -> None:
    """A silently renamed note is one the user will look for in the wrong place."""
    text = render_saved("Inbox/Note-2.md", renamed_from="Inbox/Note.md")

    assert "already existed" in text.replace("\\", "")
    assert_markdown_v2_safe(text)


def test_an_answer_lists_its_sources() -> None:
    text = render_answer("The budget is 12k.", ("Finance/Budget.md", "Notes/Q1.md"))

    assert "Sources" in text
    assert "`Finance/Budget.md`" in text
    assert_markdown_v2_safe(text)


def test_an_answer_with_no_sources_shows_no_empty_heading() -> None:
    assert "Sources" not in render_answer("Just this.", ())
