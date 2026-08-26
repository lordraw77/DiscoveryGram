"""Note and folder path handling.

NoteDiscovery addresses everything by a vault-relative POSIX path (`Projects/Ideas.md`).
Paths arrive from Telegram users, from LLM output and from wiki-links, so they are
untrusted: every path crossing into an adapter goes through `normalise_note_path`
or `normalise_folder_path` first.
"""

from __future__ import annotations

from urllib.parse import quote

NOTE_SUFFIX = ".md"

# Characters NoteDiscovery's own `sanitize_filename` rejects, plus the separators
# we need to keep meaningful.
_FORBIDDEN = set('\\:*?"<>|')


class InvalidPath(ValueError):
    """A path that must never reach NoteDiscovery."""


def _clean_segments(path: str) -> list[str]:
    if not path or not path.strip():
        raise InvalidPath("path is empty")

    candidate = path.strip().replace("\\", "/")
    segments = [segment.strip() for segment in candidate.split("/")]
    segments = [segment for segment in segments if segment and segment != "."]

    if not segments:
        raise InvalidPath(f"path resolves to nothing: {path!r}")

    for segment in segments:
        if segment == "..":
            raise InvalidPath(f"path escapes the vault: {path!r}")
        if any(char in _FORBIDDEN for char in segment):
            raise InvalidPath(f"path contains forbidden characters: {path!r}")
        if any(ord(char) < 32 for char in segment):
            raise InvalidPath(f"path contains control characters: {path!r}")

    return segments


def normalise_note_path(path: str) -> str:
    """Vault-relative note path, with `.md` enforced.

    A user typing `Projects/Ideas` means `Projects/Ideas.md`; a path that already
    carries another extension keeps it, because media notes exist too.
    """
    segments = _clean_segments(path)
    joined = "/".join(segments)
    if "." not in segments[-1]:
        joined += NOTE_SUFFIX
    return joined


def normalise_folder_path(path: str) -> str:
    """Vault-relative folder path, without a trailing slash."""
    return "/".join(_clean_segments(path))


def encode_path(path: str) -> str:
    """Percent-encode a path for use in a URL path segment.

    `/` stays literal: NoteDiscovery's routes declare `{note_path:path}`, so the
    separators are part of the value the handler receives.
    """
    return quote(path, safe="/")


def parent_folder(path: str) -> str:
    """Folder containing `path`, or `""` for a vault-root item."""
    head, _, _ = path.rpartition("/")
    return head


def note_title(path: str) -> str:
    """Human-readable title: the file stem."""
    name = path.rpartition("/")[2]
    stem, dot, _ = name.rpartition(".")
    return stem if dot else name
