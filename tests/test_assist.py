"""`/summarize` and `/ask`.

`/ask` is retrieval-augmented, and the grounding rule is the point: a
note-taking bot that answers from its training data is worse than one that says
it does not know, because the user cannot tell the two apart.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from discoverygram.app.assist import NOT_IN_NOTES, AssistService
from discoverygram.app.probe import InstanceState
from discoverygram.app.search import SearchService
from discoverygram.config import Settings
from discoverygram.llm.plan import Attempt, TaskProfile
from discoverygram.llm.router import LlmRouter, TaskLadder
from discoverygram.ports.errors import InvalidRequest, NotFound, Unavailable
from discoverygram.ports.llm import Completion, LlmClient, Message, Usage
from discoverygram.ports.llm_errors import LlmUnavailable
from discoverygram.ports.model import (
    InstanceConfig,
    Note,
    NoteRef,
    SearchHit,
    SearchMatch,
)


class RecordingClient(LlmClient):
    def __init__(self, answer: str | Exception = "an answer") -> None:
        self.name = "fake"
        self.answer = answer
        self.prompts: list[str] = []

    async def complete(
        self,
        *,
        model: str,
        messages: Sequence[Message],
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> Completion:
        self.prompts.append("\n".join(message.text for message in messages))
        if isinstance(self.answer, Exception):
            raise self.answer
        return Completion(
            text=self.answer, provider="fake", model=model, usage=Usage(), latency_s=0.01
        )

    def supports_vision(self) -> bool:
        return False

    async def aclose(self) -> None:
        return None


class StubStore:
    def __init__(self, notes: dict[str, str], *, hits: list[SearchHit] | None = None) -> None:
        self.notes = notes
        self.hits = hits or []
        self.reads: list[str] = []

    async def get_note(self, path: str, *, include_backlinks: bool = True) -> Note:
        self.reads.append(path)
        if path not in self.notes:
            raise NotFound(f"no note at {path}")
        return Note(ref=NoteRef.from_path(path), content=self.notes[path], lines=1)

    async def search(
        self, query: str, *, limit: int | None = None, offset: int = 0
    ) -> list[SearchHit]:
        return list(self.hits)


def hit(path: str) -> SearchHit:
    return SearchHit(ref=NoteRef.from_path(path), matches=(SearchMatch(1, "line"),))


def make_service(
    settings: Settings,
    store: StubStore,
    *,
    client: RecordingClient | None = None,
    with_llm: bool = True,
) -> AssistService:
    instance = InstanceState(config=InstanceConfig(version="0.31.3"), healthy=True)
    search = SearchService(store, settings, instance)  # type: ignore[arg-type]

    router: LlmRouter | None = None
    if with_llm:
        rung = (Attempt(provider="fake", model="m"),)
        router = LlmRouter(
            settings,
            {"fake": client or RecordingClient()},
            {
                task: TaskLadder(task=task, attempts=rung)
                for task in (TaskProfile.CHAT, TaskProfile.SUMMARISE)
            },
        )
    return AssistService(store, search, router, settings)  # type: ignore[arg-type]


# --- Summarise ------------------------------------------------------------


async def test_a_note_is_summarised_and_cites_itself(settings: Settings) -> None:
    store = StubStore({"Projects/Plan.md": "a long plan about hiring"})
    service = make_service(settings, store, client=RecordingClient("Hiring plan for Q1."))

    answer = await service.summarise("Projects/Plan")

    assert answer.text == "Hiring plan for Q1."
    assert [ref.path for ref in answer.sources] == ["Projects/Plan.md"]
    assert answer.provider == "fake"


async def test_summarising_a_missing_note_fails_before_any_provider_call(
    settings: Settings,
) -> None:
    client = RecordingClient()
    service = make_service(settings, StubStore({}), client=client)

    with pytest.raises(NotFound):
        await service.summarise("Nope.md")

    assert client.prompts == []


async def test_an_empty_note_is_refused_rather_than_summarised(settings: Settings) -> None:
    service = make_service(settings, StubStore({"Empty.md": "   "}))

    with pytest.raises(InvalidRequest, match="nothing to summarise"):
        await service.summarise("Empty.md")


async def test_a_provider_failure_on_summarise_is_not_swallowed(settings: Settings) -> None:
    """Unlike a draft, a failed summary has no partial result worth keeping."""
    service = make_service(
        settings,
        StubStore({"A.md": "content"}),
        client=RecordingClient(LlmUnavailable("down")),
    )

    with pytest.raises(Exception, match="failed"):
        await service.summarise("A.md")


# --- Ask ------------------------------------------------------------------


async def test_an_answer_cites_the_notes_it_read(settings: Settings) -> None:
    store = StubStore(
        {"A.md": "the budget is 12k", "B.md": "the deadline is March"},
        hits=[hit("A.md"), hit("B.md")],
    )
    service = make_service(settings, store, client=RecordingClient("It is 12k [A.md]."))

    answer = await service.ask("what is the budget")

    assert answer.grounded is True
    assert [ref.path for ref in answer.sources] == ["A.md", "B.md"]


async def test_the_prompt_carries_the_note_bodies_and_the_question(
    settings: Settings,
) -> None:
    client = RecordingClient("answer")
    store = StubStore({"A.md": "the budget is 12k"}, hits=[hit("A.md")])

    await make_service(settings, store, client=client).ask("what is the budget")

    prompt = client.prompts[0]
    assert "the budget is 12k" in prompt
    assert "--- A.md ---" in prompt
    assert "what is the budget" in prompt


async def test_a_question_with_no_hits_never_reaches_a_provider(settings: Settings) -> None:
    """There would be nothing to ground the answer in."""
    client = RecordingClient()
    service = make_service(settings, StubStore({}, hits=[]), client=client)

    answer = await service.ask("what is the budget")

    assert answer.found_nothing is True
    assert client.prompts == []


async def test_an_ungrounded_answer_is_reported_as_not_found(settings: Settings) -> None:
    """The model saying "not in the notes" must not be shown as an answer."""
    store = StubStore({"A.md": "unrelated"}, hits=[hit("A.md")])
    service = make_service(settings, store, client=RecordingClient(NOT_IN_NOTES))

    answer = await service.ask("what is the budget")

    assert answer.found_nothing is True
    assert answer.text == ""
    # The sources are still reported: knowing *what was read* is the point.
    assert [ref.path for ref in answer.sources] == ["A.md"]


async def test_the_context_is_capped_by_ask_context_notes(settings: Settings) -> None:
    """Feeding a whole vault to the first rung would exceed its context window."""
    capped = settings.model_copy(update={"ask_context_notes": 2})
    store = StubStore(
        {f"n{index}.md": "text" for index in range(6)},
        hits=[hit(f"n{index}.md") for index in range(6)],
    )

    answer = await make_service(capped, store).ask("anything")

    assert len(answer.sources) == 2


async def test_one_unreadable_note_does_not_sink_the_question(settings: Settings) -> None:
    class Flaky(StubStore):
        async def get_note(self, path: str, *, include_backlinks: bool = True) -> Note:
            if path == "bad.md":
                raise Unavailable("cannot read")
            return await super().get_note(path, include_backlinks=include_backlinks)

    store = Flaky({"good.md": "the answer"}, hits=[hit("bad.md"), hit("good.md")])

    answer = await make_service(settings, store).ask("anything")

    assert [ref.path for ref in answer.sources] == ["good.md"]


async def test_an_empty_note_is_not_used_as_context(settings: Settings) -> None:
    store = StubStore({"empty.md": "  ", "real.md": "text"}, hits=[hit("empty.md"), hit("real.md")])

    answer = await make_service(settings, store).ask("anything")

    assert [ref.path for ref in answer.sources] == ["real.md"]


async def test_a_question_below_the_minimum_length_is_refused(settings: Settings) -> None:
    with pytest.raises(InvalidRequest):
        await make_service(settings, StubStore({})).ask("a")


# --- Availability ---------------------------------------------------------


def test_no_provider_reports_the_variable_to_set(settings: Settings) -> None:
    service = make_service(settings, StubStore({}), with_llm=False)

    assert service.available is False
    assert "LLM_CHAIN_CHAT" in service.unavailable_reason()
