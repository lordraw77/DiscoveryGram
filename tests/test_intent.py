"""Reading a caption into a capture intent.

The parser is deterministic on purpose — the module docstring explains why —
so it can be pinned down exactly, phrasing by phrasing. The headline case is
the caption from the phase 6 Definition of Done.
"""

from __future__ import annotations

import pytest

from discoverygram.app.intent import CaptureIntent, parse_caption, strip_instructions

HEADLINE = "extract the text and create a note under Projects/Research, you generate the title"


# --- The Definition of Done caption --------------------------------------


def test_the_headline_caption_is_read_completely() -> None:
    intent = parse_caption(HEADLINE, has_image=True)

    assert intent.path == "Projects/Research"
    assert intent.read_image is True
    assert intent.generate_title is True
    assert intent.generate_tags is True
    assert intent.verbatim is False
    # Every word of it was an instruction, so no body text survives.
    assert intent.note_text == ""


# --- Defaults ------------------------------------------------------------


def test_a_bare_photo_still_does_something_useful() -> None:
    """The overwhelmingly common case: a photo, no caption at all."""
    intent = parse_caption("", has_image=True)

    assert intent.read_image is True
    assert intent.generate_title is True
    assert intent.generate_tags is True
    assert intent.path == ""
    assert intent.needs_llm is True


def test_without_an_image_there_is_nothing_to_read() -> None:
    intent = parse_caption("summarise this", has_image=False)

    assert intent.read_image is False


def test_a_caption_with_no_image_is_kept_whole_as_the_body() -> None:
    """Instruction-stripping is for captions *about* an image, not for text."""
    intent = parse_caption("Create a note about the budget", has_image=False)

    assert intent.note_text == "Create a note about the budget"


# --- Paths ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("caption", "expected"),
    [
        ("save under Finance", "Finance"),
        ("put it in Projects/Research", "Projects/Research"),
        ("file this into Archive/2026", "Archive/2026"),
        ("save it inside the Research folder", "Research"),
        ("in the Meeting Notes folder", "Meeting Notes"),
        ("save under Finance, generate a title", "Finance"),
        ("Projects/Research.md", "Projects/Research.md"),
    ],
)
def test_a_named_location_is_found(caption: str, expected: str) -> None:
    assert parse_caption(caption, has_image=True).path == expected


@pytest.mark.parametrize(
    "caption",
    [
        "save it to me",
        "turn this into a note",
        "extract the text",
        "translate it to english",
        "",
    ],
)
def test_a_phrase_that_is_not_a_path_is_not_read_as_one(caption: str) -> None:
    """ "save it to me" must not create a folder called "me"."""
    assert parse_caption(caption, has_image=True).path == ""


def test_a_preposition_wins_over_a_bare_path() -> None:
    """A stated intent beats a word that merely contains a slash."""
    intent = parse_caption("about the a/b test, save under Research", has_image=True)

    assert intent.path == "Research"


def test_the_phrase_that_named_the_folder_does_not_become_body_text() -> None:
    intent = parse_caption("Shopping list for Saturday, save under Home/Lists", has_image=True)

    assert intent.path == "Home/Lists"
    assert intent.note_text == "Shopping list for Saturday"


# --- Flags ---------------------------------------------------------------


@pytest.mark.parametrize(
    "caption",
    [
        "you generate the title",
        "generate a title for it",
        "make up a title",
        "choose the title yourself",
    ],
)
def test_asking_for_a_title_is_recognised(caption: str) -> None:
    assert parse_caption(caption, has_image=True).generate_title is True


@pytest.mark.parametrize(
    "caption",
    ["no title", "without a title", "don't add a title", "do not generate a title"],
)
def test_refusing_a_title_is_recognised(caption: str) -> None:
    assert parse_caption(caption, has_image=True).generate_title is False


@pytest.mark.parametrize("caption", ["no tags", "without tags", "don't add tags"])
def test_refusing_tags_is_recognised(caption: str) -> None:
    assert parse_caption(caption, has_image=True).generate_tags is False


def test_a_refusal_beats_a_request_in_the_same_sentence() -> None:
    """ "generate a title but no tags" must not generate tags."""
    intent = parse_caption("generate a title but no tags", has_image=True)

    assert intent.generate_title is True
    assert intent.generate_tags is False


@pytest.mark.parametrize("caption", ["summarise it", "summarize this", "give me a tldr"])
def test_a_summary_is_opt_in(caption: str) -> None:
    assert parse_caption(caption, has_image=True).generate_summary is True


def test_no_summary_unless_asked() -> None:
    assert parse_caption("save under Notes", has_image=True).generate_summary is False


@pytest.mark.parametrize(
    "caption",
    ["transcribe verbatim", "copy it as-is", "extract the text exactly", "raw text please"],
)
def test_verbatim_switches_off_the_tidy_step(caption: str) -> None:
    assert parse_caption(caption, has_image=True).verbatim is True


# --- Instruction stripping ------------------------------------------------


@pytest.mark.parametrize(
    "caption",
    [
        "extract the text",
        "create a note",
        "you generate the title",
        "describe this",
        "generate a title and tags",
        "please transcribe it",
    ],
)
def test_a_pure_instruction_leaves_no_body(caption: str) -> None:
    assert strip_instructions(caption) == ""


def test_real_content_survives_stripping() -> None:
    assert strip_instructions("Meeting notes from today") == "Meeting notes from today"


def test_content_mixed_with_an_instruction_keeps_only_the_content() -> None:
    kept = strip_instructions("Receipt from the hardware shop, extract the text")

    assert "Receipt from the hardware shop" in kept
    assert "extract" not in kept


# --- The dataclass --------------------------------------------------------


def test_an_intent_that_asks_for_nothing_needs_no_provider() -> None:
    """`/new` must never acquire an LLM dependency."""
    intent = CaptureIntent(
        read_image=False, generate_title=False, generate_tags=False, generate_summary=False
    )

    assert intent.needs_llm is False


def test_with_path_replaces_only_the_path() -> None:
    intent = parse_caption("no tags", has_image=True).with_path("Inbox")

    assert intent.path == "Inbox"
    assert intent.generate_tags is False
