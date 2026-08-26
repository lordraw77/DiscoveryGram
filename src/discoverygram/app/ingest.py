"""The LLM-assisted capture pipeline: attachment or text in, draft out.

The shape of the pipeline is the same whatever arrives:

    read (vision) -> tidy -> title -> tags -> Draft -> preview -> save

Every step is **optional and independently degradable**. A vision rung that
fails does not lose the note: the draft carries whatever text there is, plus a
warning saying what could not be done. That matters because the alternative —
raising — throws away a photo the user has already sent and cannot easily send
again from a phone.

Nothing here writes to the vault. The pipeline produces a `Draft`; only the
`Save` button in the preview turns one into a note. That separation is the
whole of "preview-before-write", and it is why generation failures are
survivable: a bad draft is discarded, not undone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace

from discoverygram.app.capture import Provenance
from discoverygram.app.intent import CaptureIntent
from discoverygram.app.notes import normalise_tag
from discoverygram.config import Settings
from discoverygram.llm.plan import TaskProfile
from discoverygram.llm.router import LlmRouter
from discoverygram.ports.errors import InvalidRequest
from discoverygram.ports.llm import Completion, ImagePart, Message
from discoverygram.ports.llm_errors import LlmError, LlmNoProvider, LlmQuotaExceeded
from discoverygram.util.logging import get_logger

log = get_logger(__name__)

# --- Prompts --------------------------------------------------------------
#
# Written as constants rather than f-strings scattered through the code, so a
# prompt change is a visible diff and the whole set can be read at once.

OCR_SYSTEM = (
    "You transcribe images for a personal note-taking system. "
    "Return only the text visible in the image, preserving its structure with "
    "Markdown: headings as headings, lists as lists, tables as tables. "
    "Do not add commentary, do not describe the image, and do not invent text "
    "that is not there. If the image contains no readable text, reply with "
    "exactly: NO_TEXT"
)

TIDY_SYSTEM = (
    "You clean up transcribed text for a personal note. "
    "Fix obvious transcription errors and formatting, keep every fact, and "
    "keep the author's own wording. Return only the cleaned text as Markdown."
)

DESCRIBE_SYSTEM = (
    "You describe images for a personal note-taking system. "
    "Give one clear paragraph describing what the image shows. "
    "Be concrete and factual; do not speculate about context you cannot see."
)

TITLE_SYSTEM = (
    "You write titles for personal notes. "
    "Return one short title — at most eight words — and nothing else. "
    "No quotation marks, no trailing full stop, no 'Title:' prefix."
)

TAGS_SYSTEM = (
    "You suggest tags for personal notes. "
    "Return between one and {maximum} lowercase tags, separated by spaces, and "
    "nothing else. Each tag is a single word or hyphenated-phrase, with no '#' "
    "and no punctuation. Prefer specific tags over generic ones."
)

SUMMARY_SYSTEM = (
    "You summarise personal notes. "
    "Return a short summary — at most four sentences — capturing the "
    "substance. Return only the summary."
)

# The model's own way of saying an image has nothing to read.
_NO_TEXT = "NO_TEXT"

_TAG_SPLIT = re.compile(r"[\s,;]+")
_QUOTED = re.compile(r"^[\"'`*_\s]+|[\"'`*_\s]+$")
# Around a *tag*, only list decoration is trimmed. A backtick or a bracket is
# left in place so the candidate is refused: silently repairing it would put a
# tag in the vault's index that the user never chose.
_TAG_DECORATION = re.compile(r"^[-*•\s\"']+|[\s\"']+$")


@dataclass(frozen=True, slots=True)
class Draft:
    """A note that does not exist yet.

    Carries everything the preview card shows and the save step needs, plus
    the `warnings` that say what the pipeline could not do — a degraded draft
    is still a draft, and hiding the degradation would be dishonest.
    """

    body: str = ""
    title: str = ""
    tags: tuple[str, ...] = field(default_factory=tuple)
    path: str = ""
    summary: str = ""
    provenance: Provenance | None = None
    warnings: tuple[str, ...] = field(default_factory=tuple)
    #: Vault paths of media uploaded for this draft, already referenced in the body.
    attachments: tuple[str, ...] = field(default_factory=tuple)
    #: The intent it came from, so `Regenerate` can repeat the same request.
    intent: CaptureIntent = field(default_factory=CaptureIntent)

    @property
    def is_empty(self) -> bool:
        """Nothing to save: no text, no title, no attachment."""
        return not (self.body.strip() or self.title.strip() or self.attachments)

    def with_title(self, title: str) -> Draft:
        return replace(self, title=title.strip())

    def with_path(self, path: str) -> Draft:
        return replace(self, path=path)

    def warn(self, message: str) -> Draft:
        if message in self.warnings:
            return self
        return replace(self, warnings=(*self.warnings, message))


class IngestService:
    """Builds drafts. Never writes."""

    def __init__(self, router: LlmRouter | None, settings: Settings) -> None:
        self._llm = router
        self._settings = settings

    @property
    def available(self) -> bool:
        """Whether any LLM work is possible at all."""
        return self._llm is not None and self._llm.available(TaskProfile.CHAT)

    @property
    def vision_available(self) -> bool:
        return self._llm is not None and self._llm.available(TaskProfile.VISION)

    def unavailable_reason(self) -> str:
        """Why an LLM step cannot run, in words fit for a chat message."""
        if self._llm is None:
            return "No AI provider is configured, so I cannot generate anything."
        if not self._llm.available(TaskProfile.CHAT):
            return (
                "No AI provider is configured for text, so I cannot generate "
                "titles, tags or summaries."
            )
        return ""

    # --- Entry points ----------------------------------------------------

    async def from_image(
        self,
        images: tuple[ImagePart, ...],
        intent: CaptureIntent,
        *,
        user_id: int | None = None,
        attachments: tuple[str, ...] = (),
    ) -> Draft:
        """A photo, or an album, plus whatever the caption asked for."""
        if not images:
            raise InvalidRequest("There is no image to read.")

        draft = Draft(
            body=intent.note_text,
            path=intent.path,
            intent=intent,
            attachments=attachments,
        )

        provider = ""
        model = ""

        if intent.read_image:
            read, completion, failure = await self._read_images(images, intent, user_id=user_id)
            if failure:
                draft = draft.warn(failure)
            elif completion is not None:
                provider, model = completion.provider, completion.model
            if read:
                draft = replace(draft, body=_join(draft.body, read))

        draft = await self._enrich(draft, intent, user_id=user_id, provider=provider, model=model)
        return self._reference_attachments(draft)

    async def from_text(
        self,
        text: str,
        intent: CaptureIntent,
        *,
        user_id: int | None = None,
    ) -> Draft:
        """A message, a forward or a caption with no image behind it."""
        body = text.strip()
        if not body:
            raise InvalidRequest("There is nothing to capture.")

        draft = Draft(body=body, path=intent.path, intent=intent)
        return await self._enrich(draft, intent, user_id=user_id)

    async def regenerate(self, draft: Draft, *, user_id: int | None = None) -> Draft:
        """Ask again for the generated parts, keeping the body and the path.

        The body is kept on purpose: it is the expensive part — it came from a
        vision call, or from the user — and `Regenerate` is almost always about
        being unhappy with the *title*.
        """
        cleared = replace(draft, title="", tags=(), summary="", warnings=())
        refreshed = await self._enrich(cleared, draft.intent, user_id=user_id)
        return refreshed.with_path(draft.path)

    # --- Steps -----------------------------------------------------------

    async def _read_images(
        self,
        images: tuple[ImagePart, ...],
        intent: CaptureIntent,
        *,
        user_id: int | None,
    ) -> tuple[str, Completion | None, str]:
        """Vision: transcribe or describe. Returns `(text, completion, failure)`."""
        if self._llm is None or not self._llm.available(TaskProfile.VISION):
            return "", None, "No vision provider is configured, so I could not read the image."

        instruction = (
            "Transcribe every piece of text in "
            + ("these images" if len(images) > 1 else "this image")
            + "."
        )

        try:
            completion = await self._llm.complete(
                TaskProfile.VISION,
                [
                    Message(role="system", text=OCR_SYSTEM),
                    Message(role="user", text=instruction, images=images),
                ],
                user_id=user_id,
            )
        except LlmQuotaExceeded:
            raise
        except LlmError as exc:
            log.warning("vision_step_failed", error=str(exc))
            return "", None, f"I could not read the image: {exc}"

        text = completion.text.strip()
        if text == _NO_TEXT or not text:
            # Nothing to transcribe is not a failure; it is an answer. Fall
            # back to a description so the note is still about something.
            described, failure = await self._describe(images, user_id=user_id)
            return described, completion, failure

        if not intent.verbatim:
            text = await self._tidy(text, user_id=user_id)

        return text, completion, ""

    async def _describe(
        self, images: tuple[ImagePart, ...], *, user_id: int | None
    ) -> tuple[str, str]:
        if self._llm is None:
            return "", ""
        try:
            completion = await self._llm.complete(
                TaskProfile.VISION,
                [
                    Message(role="system", text=DESCRIBE_SYSTEM),
                    Message(role="user", text="Describe this image.", images=images),
                ],
                user_id=user_id,
            )
        except LlmQuotaExceeded:
            raise
        except LlmError as exc:
            log.warning("describe_step_failed", error=str(exc))
            return "", "The image has no readable text, and I could not describe it either."
        return completion.text.strip(), ""

    async def _tidy(self, text: str, *, user_id: int | None) -> str:
        """Clean up a transcription — best effort, never destructive.

        On any failure the *raw* transcription is returned. Losing a page of
        recovered text because a tidy-up call timed out would be the worst
        possible trade.
        """
        if self._llm is None or not self._llm.available(TaskProfile.CHAT):
            return text
        try:
            completion = await self._llm.complete(
                TaskProfile.CHAT,
                [
                    Message(role="system", text=TIDY_SYSTEM),
                    Message(role="user", text=text),
                ],
                user_id=user_id,
            )
        except LlmError as exc:
            log.info("tidy_step_skipped", error=str(exc))
            return text
        return completion.text.strip() or text

    async def _enrich(
        self,
        draft: Draft,
        intent: CaptureIntent,
        *,
        user_id: int | None,
        provider: str = "",
        model: str = "",
    ) -> Draft:
        """Title, tags and summary — each optional, each independently skippable."""
        material = draft.body or draft.title
        if not material.strip():
            return self._stamp(draft, provider, model)

        if intent.generate_title and not draft.title:
            title, completion, failure = await self._generate(
                TITLE_SYSTEM, material, user_id=user_id, label="title"
            )
            if failure:
                draft = draft.warn(failure)
            elif completion is not None:
                provider, model = provider or completion.provider, model or completion.model
            if title:
                draft = draft.with_title(_clean_line(title))

        if intent.generate_tags and not draft.tags:
            raw, completion, failure = await self._generate(
                TAGS_SYSTEM.format(maximum=self._settings.generated_tags_max),
                material,
                user_id=user_id,
                label="tags",
            )
            if failure:
                draft = draft.warn(failure)
            elif completion is not None:
                provider, model = provider or completion.provider, model or completion.model
            if raw:
                draft = replace(draft, tags=parse_tags(raw, self._settings.generated_tags_max))

        if intent.generate_summary and not draft.summary:
            summary, completion, failure = await self._generate(
                SUMMARY_SYSTEM, material, user_id=user_id, label="summary"
            )
            if failure:
                draft = draft.warn(failure)
            if summary:
                draft = replace(draft, summary=summary.strip())

        return self._stamp(draft, provider, model)

    async def _generate(
        self,
        system: str,
        material: str,
        *,
        user_id: int | None,
        label: str,
    ) -> tuple[str, Completion | None, str]:
        """One chat call. Returns `(text, completion, failure)`; never raises `LlmError`."""
        if self._llm is None or not self._llm.available(TaskProfile.CHAT):
            return "", None, f"No AI provider is configured, so I could not generate a {label}."

        task = {
            "title": TaskProfile.TITLE,
            "summary": TaskProfile.SUMMARISE,
        }.get(label, TaskProfile.CHAT)

        try:
            completion = await self._llm.complete(
                task,
                [
                    Message(role="system", text=system),
                    Message(role="user", text=_excerpt(material)),
                ],
                user_id=user_id,
            )
        except LlmQuotaExceeded:
            # The cap is the user's own budget, not a degradation: it must
            # surface as a refusal rather than as a warning on a half-made draft.
            raise
        except LlmNoProvider as exc:
            return "", None, str(exc)
        except LlmError as exc:
            log.warning("generation_step_failed", step=label, error=str(exc))
            return "", None, f"I could not generate a {label}: {exc}"

        return completion.text.strip(), completion, ""

    def _stamp(self, draft: Draft, provider: str, model: str) -> Draft:
        if not self._settings.provenance_enabled or not (provider or model):
            return draft
        return replace(draft, provenance=Provenance(provider=provider, model=model))

    @staticmethod
    def _reference_attachments(draft: Draft) -> Draft:
        """Put every uploaded file into the body as a Markdown image.

        An attachment that is uploaded but never referenced is invisible: it
        exists in the vault's media folder and nothing points at it.
        """
        if not draft.attachments:
            return draft
        already = draft.body
        links = "\n".join(
            f"![{path.rpartition('/')[2]}]({path})"
            for path in draft.attachments
            if path not in already
        )
        if not links:
            return draft
        return replace(draft, body=_join(links, already))


# --- Helpers -------------------------------------------------------------


def parse_tags(raw: str, maximum: int) -> tuple[str, ...]:
    """Turn a model's answer into tags the vault will accept.

    Models return `#a, b and c`, `- a\\n- b`, or a sentence. Anything that is
    not a legal tag is dropped rather than repaired: a mangled tag is worse
    than a missing one, because it pollutes the vault's tag index permanently.
    """
    cleaned = raw.replace("\n", " ")
    tags: list[str] = []
    for candidate in _TAG_SPLIT.split(cleaned):
        piece = _TAG_DECORATION.sub("", candidate).strip().lstrip("#")
        if not piece or piece.casefold() in {"and", "or", "tags", "tag"}:
            continue
        try:
            name = normalise_tag(piece)
        except InvalidRequest:
            continue
        lowered = name.casefold()
        if lowered not in {tag.casefold() for tag in tags}:
            tags.append(lowered)
        if len(tags) >= maximum:
            break
    return tuple(tags)


def _clean_line(text: str) -> str:
    """A single line, without the decoration models like to add to titles."""
    first = text.strip().splitlines()[0] if text.strip() else ""
    first = re.sub(r"^\s*(?:title|titolo)\s*[:\-]\s*", "", first, flags=re.IGNORECASE)
    first = _QUOTED.sub("", first)
    return first.strip().rstrip(".").strip()[:120]


def _excerpt(text: str, limit: int = 6000) -> str:
    """Bound what is sent to a provider.

    A 200-page transcription would blow the context window of the first rung
    and get a 400 from it — which the router would then treat as a dead rung
    and walk the whole ladder for. Truncating is cheaper and honest.
    """
    if len(text) <= limit:
        return text
    return text[:limit] + "\n\n[…truncated for length…]"


def _join(first: str, second: str) -> str:
    parts = [part.strip() for part in (first, second) if part and part.strip()]
    return "\n\n".join(parts)


__all__ = [
    "DESCRIBE_SYSTEM",
    "OCR_SYSTEM",
    "SUMMARY_SYSTEM",
    "TAGS_SYSTEM",
    "TIDY_SYSTEM",
    "TITLE_SYSTEM",
    "Draft",
    "IngestService",
    "parse_tags",
]
