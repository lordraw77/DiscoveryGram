"""LLM operations over notes that already exist: `/summarize` and `/ask`.

`/ask` is retrieval-augmented, and the retrieval is the honest part of it.
Three rules shape it:

* **Only the vault answers.** The prompt says so explicitly, and the model is
  told to reply `NOT_IN_NOTES` when the context does not contain the answer.
  A note-taking bot that confidently answers from its training data is worse
  than one that says it does not know — the user cannot tell the two apart.
* **Every answer carries its sources.** The notes that were read are returned
  alongside the text and rendered as buttons, so a claim can be checked in one
  tap rather than trusted.
* **The context is bounded** by `ASK_CONTEXT_NOTES` and by an excerpt limit
  per note. Feeding a whole vault to the first rung would exceed its context
  window, and the router would then read the resulting 400 as a dead rung and
  walk the entire ladder for a request that was never going to fit.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from discoverygram.app.search import SearchService
from discoverygram.config import Settings
from discoverygram.llm.plan import TaskProfile
from discoverygram.llm.router import LlmRouter
from discoverygram.ports.errors import InvalidRequest, NoteStoreError
from discoverygram.ports.llm import Completion, Message
from discoverygram.ports.llm_errors import LlmError, LlmNoProvider
from discoverygram.ports.model import NoteRef, SearchHit
from discoverygram.ports.note_store import NoteStore
from discoverygram.util.logging import get_logger
from discoverygram.util.paths import normalise_note_path

log = get_logger(__name__)

SUMMARISE_SYSTEM = (
    "You summarise a personal note. "
    "Return a concise summary in Markdown: a short paragraph, then bullet "
    "points for any concrete items, decisions or dates. Keep every fact and "
    "invent nothing. Return only the summary."
)

ASK_SYSTEM = (
    "You answer questions using only the notes provided below. "
    "Do not use any other knowledge. Cite the notes you used by their path, "
    "in brackets, immediately after the claim they support. "
    "If the notes do not contain the answer, reply with exactly: NOT_IN_NOTES"
)

# The model's own way of saying the vault does not know.
NOT_IN_NOTES = "NOT_IN_NOTES"

# Characters of each note that reach the prompt. Enough for a whole short note
# and the opening of a long one, which is where a note usually says what it is.
CONTEXT_EXCERPT_CHARS = 2000


@dataclass(frozen=True, slots=True)
class Answer:
    """A generated answer and the notes it was allowed to use."""

    text: str
    sources: tuple[NoteRef, ...] = field(default_factory=tuple)
    grounded: bool = True
    provider: str = ""
    model: str = ""

    @property
    def found_nothing(self) -> bool:
        """The model said the vault does not contain the answer."""
        return not self.grounded


class AssistService:
    """Summarising a note, and answering a question from the vault."""

    def __init__(
        self,
        notes: NoteStore,
        search: SearchService,
        router: LlmRouter | None,
        settings: Settings,
    ) -> None:
        self._notes = notes
        self._search = search
        self._llm = router
        self._settings = settings

    @property
    def available(self) -> bool:
        return self._llm is not None and self._llm.available(TaskProfile.CHAT)

    def unavailable_reason(self) -> str:
        if self._llm is None or not self._llm.available(TaskProfile.CHAT):
            return (
                "No AI provider is configured for text. "
                "Set LLM_CHAIN_CHAT and a provider key to use this command."
            )
        return ""

    async def summarise(self, path: str, *, user_id: int | None = None) -> Answer:
        """Summarise one note. The note is read first, so a bad path fails fast."""
        note_path = normalise_note_path(path)
        note = await self._notes.get_note(note_path, include_backlinks=False)

        if not note.content.strip():
            raise InvalidRequest(f"{note_path} is empty, so there is nothing to summarise.")

        completion = await self._complete(
            TaskProfile.SUMMARISE,
            SUMMARISE_SYSTEM,
            _excerpt(note.content, limit=12_000),
            user_id=user_id,
        )
        log.info("note_summarised", path=note_path, provider=completion.provider)
        return Answer(
            text=completion.text,
            sources=(note.ref,),
            provider=completion.provider,
            model=completion.model,
        )

    async def ask(self, question: str, *, user_id: int | None = None) -> Answer:
        """Answer from the vault, citing what was read.

        A question that finds no notes never reaches a provider: there would be
        nothing to ground an answer in, and asking anyway is exactly the
        failure mode the grounding rule exists to prevent.
        """
        query = question.strip()
        if len(query) < self._settings.search_min_query_length:
            raise InvalidRequest(
                f"Ask something a little longer — at least "
                f"{self._settings.search_min_query_length} characters."
            )

        outcome = await self._search.full_text(query)
        if not outcome.ran:
            raise InvalidRequest(outcome.notice)
        if outcome.is_empty:
            return Answer(text="", sources=(), grounded=False)

        hits = outcome.hits[: self._settings.ask_context_notes]
        context, sources = await self._gather(hits)
        if not context:
            return Answer(text="", sources=tuple(sources), grounded=False)

        completion = await self._complete(
            TaskProfile.CHAT,
            ASK_SYSTEM,
            f"Notes:\n\n{context}\n\nQuestion: {query}",
            user_id=user_id,
        )

        answer = completion.text.strip()
        grounded = NOT_IN_NOTES not in answer
        log.info(
            "vault_question_answered",
            notes_read=len(sources),
            grounded=grounded,
            provider=completion.provider,
        )
        return Answer(
            text="" if not grounded else answer,
            sources=tuple(sources),
            grounded=grounded,
            provider=completion.provider,
            model=completion.model,
        )

    # --- Internals -------------------------------------------------------

    async def _gather(self, hits: tuple[SearchHit, ...]) -> tuple[str, list[NoteRef]]:
        """Read the hit notes into one prompt block, skipping what will not load.

        One unreadable note must not sink the whole question: the others still
        answer it, and the sources list shows exactly which notes were used.
        """
        blocks: list[str] = []
        sources: list[NoteRef] = []

        for hit in hits:
            try:
                note = await self._notes.get_note(hit.ref.path, include_backlinks=False)
            except NoteStoreError as exc:
                log.info("context_note_skipped", path=hit.ref.path, error=str(exc))
                continue
            if not note.content.strip():
                continue
            blocks.append(f"--- {note.ref.path} ---\n{_excerpt(note.content)}")
            sources.append(note.ref)

        return "\n\n".join(blocks), sources

    async def _complete(
        self,
        task: TaskProfile,
        system: str,
        user_text: str,
        *,
        user_id: int | None,
    ) -> Completion:
        if self._llm is None:
            raise LlmNoProvider(self.unavailable_reason())
        try:
            return await self._llm.complete(
                task,
                [Message(role="system", text=system), Message(role="user", text=user_text)],
                user_id=user_id,
            )
        except LlmError:
            # Deliberately not swallowed. Unlike the draft pipeline, there is
            # no partial result worth keeping here: a failed summary is just a
            # failure, and the error handler already turns it into one sentence.
            raise


def _excerpt(text: str, limit: int = CONTEXT_EXCERPT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n[…]"


__all__ = [
    "ASK_SYSTEM",
    "CONTEXT_EXCERPT_CHARS",
    "NOT_IN_NOTES",
    "SUMMARISE_SYSTEM",
    "Answer",
    "AssistService",
]
