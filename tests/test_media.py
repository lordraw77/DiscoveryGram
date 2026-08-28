"""Upload validation: what the bytes are, and what the name is allowed to be.

Both inputs are client-supplied. A Telegram client can send any `mime_type` and
any `file_name` it likes, and both travel onward — the type to a vision model,
the name to NoteDiscovery as a multipart filename.
"""

from __future__ import annotations

import pytest

from discoverygram.util.media import MAX_FILENAME_LENGTH, safe_filename, sniff_image

JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF"
PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
GIF = b"GIF89a\x01\x00\x01\x00"
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 "


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (JPEG, "image/jpeg"),
        (PNG, "image/png"),
        (GIF, "image/gif"),
        (b"GIF87a...", "image/gif"),
        (WEBP, "image/webp"),
    ],
)
def test_every_accepted_format_is_recognised(data: bytes, expected: str) -> None:
    assert sniff_image(data) == expected


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"PK\x03\x04",  # a ZIP named .png
        b"<!DOCTYPE html>",  # an error page saved as .jpg
        b"%PDF-1.7",
        b"RIFF\x24\x00\x00\x00WAVE",  # a RIFF container that is not WEBP
        b"\x89PNG",  # a truncated signature
    ],
)
def test_anything_else_is_not_an_image(data: bytes) -> None:
    assert sniff_image(data) is None


def test_a_plain_name_is_left_alone() -> None:
    assert safe_filename("scan-2026-01-04.png") == "scan-2026-01-04.png"


@pytest.mark.parametrize(
    "name",
    [
        "../../../etc/passwd.png",
        "..\\..\\windows\\system32\\config.png",
        "/etc/passwd.png",
        "notes/2026/photo.png",
    ],
)
def test_a_filename_is_never_a_path(name: str) -> None:
    """A media upload must not be able to choose where on the far side it lands."""
    result = safe_filename(name)

    assert "/" not in result
    assert "\\" not in result
    assert ".." not in result


@pytest.mark.parametrize("name", ["", "   ", ".", "..", "...", "/", "///"])
def test_a_name_that_reduces_to_nothing_becomes_the_default(name: str) -> None:
    assert safe_filename(name) == "attachment"


def test_the_default_is_overridable() -> None:
    assert safe_filename("", default="photo.jpg") == "photo.jpg"


def test_control_characters_and_shell_metacharacters_are_neutralised() -> None:
    result = safe_filename('re"port\x00|rm -rf *?.png')

    assert '"' not in result
    assert "\x00" not in result
    assert "|" not in result
    assert result.endswith(".png")


def test_a_very_long_name_is_truncated_but_keeps_its_extension() -> None:
    result = safe_filename("a" * 500 + ".png")

    assert len(result) <= MAX_FILENAME_LENGTH
    assert result.endswith(".png")


def test_a_long_name_with_no_real_extension_is_simply_cut() -> None:
    result = safe_filename("b" * 500)

    assert result == "b" * MAX_FILENAME_LENGTH


def test_a_leading_dot_does_not_survive() -> None:
    """A dotfile is not what anyone meant by sending a photo."""
    assert not safe_filename(".bashrc").startswith(".")
