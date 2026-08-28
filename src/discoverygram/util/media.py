"""What an uploaded file actually is, as opposed to what it says it is.

A Telegram document arrives with a `mime_type` and a `file_name`, and both are
**client-supplied**: they describe what the sending app claimed, not what the
bytes contain. Two things follow, and this module is both of them.

* **The bytes decide the type.** A `.png` that is really a ZIP, or a `.jpg`
  that is really an HTML page, is refused here rather than sent to a vision
  model and then stored in the vault under a name that lies about it. The
  check is a signature match — the first few bytes of the four formats the
  vision adapters accept — because that is exactly the question being asked
  and it needs no dependency to answer.
* **A filename is not a path.** `../../../etc/passwd.png` is a legal Telegram
  filename and it travels to NoteDiscovery as the `filename` of a multipart
  part, which is a value a server will happily join onto a directory. It is
  reduced to a single, boring segment before it leaves this process.

Neither replaces NoteDiscovery's own validation. They are the half we control,
and the half a bug on the other side would otherwise expose.
"""

from __future__ import annotations

from pathlib import PurePosixPath

# Signature -> media type, for the formats the vision adapters accept.
# WEBP and GIF need a second look: both start with a container header whose
# meaning depends on bytes further in.
_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)

MAX_FILENAME_LENGTH = 100
DEFAULT_FILENAME = "attachment"

# Characters that make a filename mean something to a filesystem or a shell,
# plus the separators. Everything here becomes an underscore.
_UNSAFE = set('/\\:*?"<>|\0')


def sniff_image(data: bytes) -> str | None:
    """The media type of `data`, or `None` when it is not an image we accept."""
    for signature, media_type in _SIGNATURES:
        if data.startswith(signature):
            return media_type
    # RIFF....WEBP — the size field sits between the two markers.
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def safe_filename(name: str, *, default: str = DEFAULT_FILENAME) -> str:
    """One filesystem-safe path segment, never a path and never empty.

    Directory components are dropped rather than escaped: a caller asking to
    upload `notes/2026/photo.png` means the *file*, and inventing folders on
    someone else's disk from a filename is how a media upload becomes a write
    primitive.
    """
    candidate = name.replace("\\", "/").strip()
    segment = PurePosixPath(candidate).name if candidate else ""
    segment = "".join("_" if char in _UNSAFE or ord(char) < 32 else char for char in segment)
    segment = segment.strip(" .")

    if not segment or segment in {".", ".."}:
        return default

    if len(segment) > MAX_FILENAME_LENGTH:
        stem, dot, suffix = segment.rpartition(".")
        if dot and len(suffix) <= 8:
            keep = MAX_FILENAME_LENGTH - len(suffix) - 1
            segment = f"{stem[:keep]}.{suffix}"
        else:
            segment = segment[:MAX_FILENAME_LENGTH]

    return segment


__all__ = [
    "DEFAULT_FILENAME",
    "MAX_FILENAME_LENGTH",
    "safe_filename",
    "sniff_image",
]
