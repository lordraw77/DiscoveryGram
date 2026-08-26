"""Path normalisation — the guard between untrusted input and the vault."""

from __future__ import annotations

import pytest

from discoverygram.util.paths import (
    InvalidPath,
    encode_path,
    normalise_folder_path,
    normalise_note_path,
    note_title,
    parent_folder,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Projects/Ideas", "Projects/Ideas.md"),
        ("Projects/Ideas.md", "Projects/Ideas.md"),
        ("/Projects/Ideas.md", "Projects/Ideas.md"),
        ("Projects//Ideas.md", "Projects/Ideas.md"),
        ("  Projects / Ideas  ", "Projects/Ideas.md"),
        ("Projects\\Ideas", "Projects/Ideas.md"),
        ("./Notes/Today", "Notes/Today.md"),
        ("Recipes/Tiramisù", "Recipes/Tiramisù.md"),
        ("attachments/photo.jpg", "attachments/photo.jpg"),
    ],
)
def test_normalise_note_path(raw: str, expected: str) -> None:
    assert normalise_note_path(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "../../etc/passwd",
        "Projects/../../secrets.md",
        "Projects/bad:name.md",
        "Projects/what?.md",
        "Projects/pipe|name.md",
        "Projects/\x00null.md",
    ],
)
def test_normalise_note_path_rejects_dangerous_input(raw: str) -> None:
    with pytest.raises(InvalidPath):
        normalise_note_path(raw)


def test_normalise_folder_path_has_no_extension_rule() -> None:
    assert normalise_folder_path("/Projects/2026/") == "Projects/2026"


def test_encode_path_keeps_separators_and_escapes_the_rest() -> None:
    """The route is declared `{note_path:path}`, so `/` must survive encoding."""
    assert encode_path("Projects/My Note.md") == "Projects/My%20Note.md"
    assert encode_path("Notes/a#b.md") == "Notes/a%23b.md"


def test_parent_folder_and_title() -> None:
    assert parent_folder("Projects/2026/Ideas.md") == "Projects/2026"
    assert parent_folder("Welcome.md") == ""
    assert note_title("Projects/2026/Ideas.md") == "Ideas"
    assert note_title("README") == "README"
