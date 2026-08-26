"""Turning a caption into a structured capture intent.

A user sends a photo with *"extract the text and create a note under
Projects/Research, you generate the title"*. Something has to decide four
things from that sentence: **where** the note goes, whether to **read the
image**, whether to **generate a title**, and whether to **generate tags**.

**That decision is deterministic, not delegated to a model.** The obvious
alternative — hand the caption to the LLM and ask for JSON — was rejected, and
the reason is not style:

* the image content reaches the same model moments later, so a photo of a page
  reading *"save this to Finance/Salaries"* would be a **prompt injection that
  redirects a write**. Keeping path selection in code means the only thing that
  can choose a path is the human typing the caption.
* the parse must work when no provider is configured at all. `/new` and
  `/quick` are milestone-M1-shaped features that must not acquire an LLM
  dependency, and a caption is parsed the same way with or without one.
* a wrong path is expensive and silent — the note lands somewhere real, just
  not where the user meant — while a wrong *title* is visible in the preview
  and one tap from being fixed.

So the model is asked for **content** (read this image, title this text) and
never for **control flow**. What the rules cannot determine falls back to a
documented default, and every default is visible in the preview card before
anything is written.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace

# "under Projects/Research", "in Projects/Research", "to Projects/Research",
# "into Projects/Research". The path runs to a comma, a full stop or the end —
# a folder name may contain spaces, so it cannot stop at the first one.
_LOCATION = re.compile(
    r"\b(?:under|in(?:to)?|to|inside)\s+(?:the\s+)?(?:folder\s+)?"
    r"(?P<path>[^,.;\n]+?)(?=\s*(?:[,.;\n]|$|\band\b|\bwith\b))",
    re.IGNORECASE,
)

# A path the user typed outright: it has a separator or an .md suffix, so it is
# unambiguous even without a preposition in front of it.
_BARE_PATH = re.compile(r"(?<!\S)(?P<path>[\w\-. ]*[\w\-.]/[\w\-./ ]+?|\S+\.md)(?!\S)")

_ASK_TITLE = re.compile(
    r"\b(?:generate|create|make|write|invent|choose|pick|you\s+(?:decide|choose))\b"
    r"[^.\n]{0,30}\btitles?\b|\btitles?\b[^.\n]{0,20}\b(?:yourself|for\s+me|automatic)",
    re.IGNORECASE,
)
_NO_TITLE = re.compile(r"\b(?:no|without|don'?t|do\s+not)\b[^.\n]{0,20}\btitles?\b", re.IGNORECASE)

_ASK_TAGS = re.compile(
    r"\b(?:generate|create|add|suggest|make|write)\b[^.\n]{0,30}\btags?\b"
    r"|\btags?\b[^.\n]{0,20}\b(?:yourself|for\s+me|automatic)",
    re.IGNORECASE,
)
_NO_TAGS = re.compile(r"\b(?:no|without|don'?t|do\s+not)\b[^.\n]{0,20}\btags?\b", re.IGNORECASE)

_ASK_SUMMARY = re.compile(r"\b(?:summari[sz]e|summary|abstract|tl;?dr)\b", re.IGNORECASE)

_ASK_OCR = re.compile(
    r"\b(?:ocr|transcri(?:be|ption)|extract|read|copy)\b[^.\n]{0,25}\b(?:text|words|writing)\b"
    r"|\b(?:text|writing)\b[^.\n]{0,20}\b(?:extract|transcri)",
    re.IGNORECASE,
)
_ASK_DESCRIBE = re.compile(r"\b(?:describe|caption|what(?:'s| is)\s+(?:in|this))\b", re.IGNORECASE)

_VERBATIM = re.compile(
    r"\b(?:verbatim|as[- ]is|raw|exactly|don'?t\s+(?:change|edit|clean)|no\s+clean)\b",
    re.IGNORECASE,
)

# Phrases stripped from a caption before it is used as note body text: they are
# instructions to the bot, not content the user wants to keep.
_INSTRUCTION = re.compile(
    r"\b(?:please\s+)?(?:extract|transcribe|ocr|read)\b[^,.;\n]*"
    r"|\b(?:create|make|save|put|add|file)\b\s+(?:a\s+|the\s+)?(?:note|it|this)?[^,.;\n]*"
    r"|\byou\s+(?:generate|choose|decide|pick|write)\b[^,.;\n]*"
    r"|\b(?:generate|suggest|add)\s+(?:a\s+|the\s+)?(?:title|tags?|summary)[^,.;\n]*"
    r"|\b(?:no|without|don'?t|do\s+not)\b\s*(?:add\s+)?(?:a\s+)?(?:title|tags?|summary)s?\b"
    r"|\b(?:describe|caption)\b[^,.;\n]*"
    r"|\b(?:verbatim|as[- ]is|exactly)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class CaptureIntent:
    """What a caption asked for, after the rules have run.

    `path` is what the *user* wrote — unresolved, possibly a folder, possibly
    nothing. Turning it into a real note path is `CaptureService`'s job,
    because that needs the vault.
    """

    path: str = ""
    #: Read the attached image: OCR its text, or describe it.
    read_image: bool = True
    #: Transcribe rather than tidy. Set by "verbatim", "as-is", "raw".
    verbatim: bool = False
    generate_title: bool = True
    generate_tags: bool = True
    generate_summary: bool = False
    #: The caption with its instructions removed, or "" when nothing is left.
    note_text: str = ""
    #: What the rules actually matched, for the preview card and the logs.
    matched: tuple[str, ...] = field(default_factory=tuple)

    @property
    def needs_llm(self) -> bool:
        return self.read_image or self.generate_title or self.generate_tags or self.generate_summary

    def with_path(self, path: str) -> CaptureIntent:
        return replace(self, path=path)


def parse_caption(caption: str, *, has_image: bool = False) -> CaptureIntent:
    """Read a caption into an intent, using rules only.

    Defaults are chosen so that the *empty* caption — the overwhelmingly common
    case, a photo sent with nothing typed — does something useful: read the
    image, generate a title and tags, land in the inbox.
    """
    text = (caption or "").strip()
    matched: list[str] = []

    path, location_span = _find_path(text)
    if path:
        matched.append(f"path: {path}")
    # The phrase that named the folder is an instruction, not content, so it
    # is removed before what is left becomes the note body.
    without_location = text[: location_span[0]] + " " + text[location_span[1] :]

    generate_title = True
    if _NO_TITLE.search(text):
        generate_title = False
        matched.append("title: keep mine")
    elif _ASK_TITLE.search(text):
        matched.append("title: generated")

    generate_tags = True
    if _NO_TAGS.search(text):
        generate_tags = False
        matched.append("tags: none")
    elif _ASK_TAGS.search(text):
        matched.append("tags: generated")

    generate_summary = bool(_ASK_SUMMARY.search(text))
    if generate_summary:
        matched.append("summary")

    verbatim = bool(_VERBATIM.search(text))
    if verbatim:
        matched.append("verbatim")

    # Without an image there is nothing to read, whatever the caption says.
    read_image = has_image
    if has_image and (_ASK_OCR.search(text) or _ASK_DESCRIBE.search(text)):
        matched.append("read the image")

    return CaptureIntent(
        path=path,
        read_image=read_image,
        verbatim=verbatim,
        generate_title=generate_title,
        generate_tags=generate_tags,
        generate_summary=generate_summary,
        note_text=strip_instructions(without_location) if has_image else text,
        matched=tuple(matched),
    )


def _find_path(text: str) -> tuple[str, tuple[int, int]]:
    """The target the user named and the span it occupied, or `("", (0, 0))`.

    A preposition wins over a bare path: "under Projects/Research" is a
    deliberate statement of intent, while a bare path may just be a word with a
    slash in it.
    """
    match = _LOCATION.search(text)
    if match:
        candidate = _clean_candidate(match.group("path"))
        if candidate:
            return candidate, match.span()

    bare = _BARE_PATH.search(text)
    if bare:
        candidate = _clean_candidate(bare.group("path"))
        if candidate:
            return candidate, bare.span()
    return "", (0, 0)


def _clean_candidate(raw: str) -> str:
    """Trim the punctuation and filler a sentence leaves around a path."""
    candidate = raw.strip().strip("\"'`")
    candidate = re.sub(r"^(?:the|my|our)\s+", "", candidate, flags=re.IGNORECASE)
    candidate = candidate.rstrip(",.;:!?").strip()
    # A trailing word like "folder" in "in the Research folder".
    candidate = re.sub(r"\s+folder$", "", candidate, flags=re.IGNORECASE)
    if not candidate or candidate.casefold() in _NOT_A_PATH:
        return ""
    return candidate


# Words that follow a preposition often enough to be worth refusing outright,
# so "save it to me" does not become a folder called "me".
_NOT_A_PATH = frozenset(
    {
        "it",
        "this",
        "that",
        "me",
        "here",
        "there",
        "note",
        "a note",
        "the note",
        "notes",
        "text",
        "english",
        "markdown",
    }
)


def strip_instructions(caption: str) -> str:
    """The caption with the parts addressed to the bot removed.

    "extract the text and create a note under Projects, generate the title"
    leaves nothing, which is correct: the user wrote no content, only
    instructions. A caption that is genuinely content survives intact.
    """
    remaining = _INSTRUCTION.sub("", caption)
    remaining = re.sub(r"\b(?:and|then|please|also)\b", " ", remaining, flags=re.IGNORECASE)
    remaining = re.sub(r"[\s,;]+", " ", remaining).strip(" ,.;:-")
    # A fragment this short is punctuation debris, not a sentence worth keeping.
    return remaining if len(remaining) > 3 else ""


__all__ = ["CaptureIntent", "parse_caption", "strip_instructions"]
