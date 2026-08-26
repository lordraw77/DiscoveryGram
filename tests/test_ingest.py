"""The LLM-assisted draft pipeline.

Two properties are asserted repeatedly, because they are what makes the flow
survivable on a phone: **a failed step never loses the user's material**, and
**nothing is written** — the pipeline produces a `Draft` and stops.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from discoverygram.app.ingest import Draft, IngestService, parse_tags
from discoverygram.app.intent import parse_caption
from discoverygram.config import Settings
from discoverygram.llm.breaker import CircuitBreaker
from discoverygram.llm.plan import Attempt, TaskProfile
from discoverygram.llm.router import LlmRouter, TaskLadder
from discoverygram.llm.usage import DailyCallCap, UsageLedger
from discoverygram.ports.errors import InvalidRequest
from discoverygram.ports.llm import Completion, ImagePart, LlmClient, Message, Usage
from discoverygram.ports.llm_errors import LlmError, LlmQuotaExceeded, LlmUnavailable

IMAGE = (ImagePart(data=b"\xff\xd8\xff", mime_type="image/jpeg"),)


class ScriptedClient(LlmClient):
    """Answers by system prompt, so each pipeline step can be scripted apart."""

    def __init__(self, answers: dict[str, object]) -> None:
        self.name = "fake"
        self.answers = answers
        self.calls: list[str] = []

    async def complete(
        self,
        *,
        model: str,
        messages: Sequence[Message],
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> Completion:
        system = messages[0].text if messages and messages[0].role == "system" else ""
        step = _step_of(system)
        self.calls.append(step)
        answer = self.answers.get(step, "")
        if isinstance(answer, LlmError):
            raise answer
        return Completion(
            text=str(answer),
            provider="fake",
            model=model,
            usage=Usage(prompt_tokens=1, completion_tokens=1),
            latency_s=0.01,
        )

    def supports_vision(self) -> bool:
        return True

    async def aclose(self) -> None:
        return None


def _step_of(system: str) -> str:
    if "transcribe images" in system:
        return "ocr"
    if "describe images" in system:
        return "describe"
    if "clean up transcribed" in system:
        return "tidy"
    if "write titles" in system:
        return "title"
    if "suggest tags" in system:
        return "tags"
    if "summarise personal notes" in system:
        return "summary"
    return "other"


def make_router(
    settings: Settings,
    answers: dict[str, object],
    *,
    vision: bool = True,
    chat: bool = True,
    cap: DailyCallCap | None = None,
) -> tuple[LlmRouter, ScriptedClient]:
    client = ScriptedClient(answers)
    rung = (Attempt(provider="fake", model="m"),)
    ladders = {
        task: TaskLadder(task=task, attempts=rung if chat else ())
        for task in (TaskProfile.CHAT, TaskProfile.TITLE, TaskProfile.SUMMARISE)
    }
    ladders[TaskProfile.VISION] = TaskLadder(
        task=TaskProfile.VISION, attempts=rung if vision else ()
    )
    router = LlmRouter(
        settings.model_copy(update={"llm_retries_per_model": 0}),
        {"fake": client},
        ladders,
        breaker=CircuitBreaker(),
        ledger=UsageLedger(),
        cap=cap,
    )
    return router, client


def ingest(
    settings: Settings,
    answers: dict[str, object],
    *,
    vision: bool = True,
    chat: bool = True,
) -> IngestService:
    router, _ = make_router(settings, answers, vision=vision, chat=chat)
    return IngestService(router, settings)


# --- The headline flow ----------------------------------------------------


async def test_a_photo_becomes_a_titled_tagged_draft(settings: Settings) -> None:
    service = ingest(
        settings,
        {
            "ocr": "Q1 planning\n- hire two engineers",
            "tidy": "# Q1 planning\n\n- hire two engineers",
            "title": "Q1 planning",
            "tags": "planning hiring q1",
        },
    )
    intent = parse_caption(
        "extract the text and create a note under Projects/Research, you generate the title",
        has_image=True,
    )

    draft = await service.from_image(IMAGE, intent)

    assert "hire two engineers" in draft.body
    assert draft.title == "Q1 planning"
    assert draft.tags == ("planning", "hiring", "q1")
    assert draft.warnings == ()
    assert draft.provenance is not None


async def test_the_pipeline_runs_every_step_it_was_asked_for(settings: Settings) -> None:
    router, client = make_router(
        settings,
        {"ocr": "text", "tidy": "text", "title": "T", "tags": "a", "summary": "S"},
    )
    intent = parse_caption("summarise it", has_image=True)

    await IngestService(router, settings).from_image(IMAGE, intent)

    assert client.calls == ["ocr", "tidy", "title", "tags", "summary"]


async def test_verbatim_skips_the_tidy_step(settings: Settings) -> None:
    router, client = make_router(settings, {"ocr": "RAW TEXT", "title": "T", "tags": "a"})
    intent = parse_caption("transcribe verbatim", has_image=True)

    draft = await IngestService(router, settings).from_image(IMAGE, intent)

    assert "tidy" not in client.calls
    assert draft.body == "RAW TEXT"


async def test_a_refused_title_is_not_generated(settings: Settings) -> None:
    router, client = make_router(settings, {"ocr": "text", "tidy": "text", "tags": "a"})
    intent = parse_caption("no title", has_image=True)

    draft = await IngestService(router, settings).from_image(IMAGE, intent)

    assert "title" not in client.calls
    assert draft.title == ""


# --- Degradation ----------------------------------------------------------


async def test_a_failed_vision_call_keeps_the_caption_and_says_what_broke(
    settings: Settings,
) -> None:
    """A photo already sent from a phone must not be thrown away."""
    service = ingest(
        settings,
        {"ocr": LlmUnavailable("provider down"), "title": "T", "tags": "a"},
    )
    intent = parse_caption("Receipt from the shop", has_image=True)

    draft = await service.from_image(IMAGE, intent)

    assert "Receipt from the shop" in draft.body
    assert any("could not read the image" in warning for warning in draft.warnings)


async def test_no_vision_provider_degrades_to_the_caption(settings: Settings) -> None:
    service = ingest(settings, {"title": "T", "tags": "a"}, vision=False)
    intent = parse_caption("Receipt from the shop", has_image=True)

    draft = await service.from_image(IMAGE, intent)

    assert draft.body == "Receipt from the shop"
    assert any("No vision provider" in warning for warning in draft.warnings)


async def test_a_failed_tidy_step_keeps_the_raw_transcription(settings: Settings) -> None:
    """Losing a recovered page because a clean-up call timed out is the worst trade."""
    service = ingest(
        settings,
        {
            "ocr": "messy but real text",
            "tidy": LlmUnavailable("timeout"),
            "title": "T",
            "tags": "a",
        },
    )

    draft = await service.from_image(IMAGE, parse_caption("", has_image=True))

    assert draft.body == "messy but real text"


async def test_a_failed_title_step_still_produces_a_draft(settings: Settings) -> None:
    service = ingest(
        settings,
        {"ocr": "text", "tidy": "text", "title": LlmUnavailable("nope"), "tags": "a"},
    )

    draft = await service.from_image(IMAGE, parse_caption("", has_image=True))

    assert draft.body == "text"
    assert draft.title == ""
    assert any("could not generate a title" in warning for warning in draft.warnings)


async def test_an_image_with_no_text_falls_back_to_a_description(settings: Settings) -> None:
    """Nothing to transcribe is an answer, not a failure."""
    service = ingest(
        settings,
        {"ocr": "NO_TEXT", "describe": "A photo of a bicycle.", "title": "T", "tags": "a"},
    )

    draft = await service.from_image(IMAGE, parse_caption("", has_image=True))

    assert draft.body == "A photo of a bicycle."


async def test_the_daily_cap_surfaces_as_a_refusal_not_a_warning(settings: Settings) -> None:
    """A spent budget is the user's own limit, not a degraded provider.

    Every other failure becomes a warning on a draft. This one must not: a
    half-made note with "you are out of budget" in the corner would look like
    something worth saving.
    """
    cap = DailyCallCap(1)
    cap.consume(111)
    router, _ = make_router(settings, {"ocr": "text"}, cap=cap)

    with pytest.raises(LlmQuotaExceeded):
        await IngestService(router, settings).from_image(
            IMAGE, parse_caption("", has_image=True), user_id=111
        )


# --- Attachments ----------------------------------------------------------


async def test_an_uploaded_file_is_referenced_in_the_body(settings: Settings) -> None:
    """An attachment nothing points at is invisible in the vault."""
    service = ingest(settings, {"ocr": "text", "tidy": "text", "title": "T", "tags": "a"})

    draft = await service.from_image(
        IMAGE, parse_caption("", has_image=True), attachments=("media/photo.jpg",)
    )

    assert "![photo.jpg](media/photo.jpg)" in draft.body
    assert draft.attachments == ("media/photo.jpg",)


async def test_an_attachment_already_in_the_body_is_not_added_twice(
    settings: Settings,
) -> None:
    service = ingest(settings, {"ocr": "see media/photo.jpg", "tidy": "see media/photo.jpg"})

    draft = await service.from_image(
        IMAGE,
        parse_caption("no title, no tags", has_image=True),
        attachments=("media/photo.jpg",),
    )

    assert draft.body.count("media/photo.jpg") == 1


# --- Text ingestion -------------------------------------------------------


async def test_text_is_titled_and_tagged_without_a_vision_call(settings: Settings) -> None:
    router, client = make_router(settings, {"title": "A title", "tags": "one two"})

    draft = await IngestService(router, settings).from_text(
        "some forwarded content", parse_caption("", has_image=False)
    )

    assert client.calls == ["title", "tags"]
    assert draft.title == "A title"


async def test_empty_text_is_refused(settings: Settings) -> None:
    with pytest.raises(InvalidRequest):
        await ingest(settings, {}).from_text("  ", parse_caption("", has_image=False))


async def test_no_images_is_refused(settings: Settings) -> None:
    with pytest.raises(InvalidRequest):
        await ingest(settings, {}).from_image((), parse_caption("", has_image=True))


# --- Regenerate -----------------------------------------------------------


async def test_regenerate_keeps_the_body_and_asks_again_for_the_rest(
    settings: Settings,
) -> None:
    """The body is the expensive part; Regenerate is about the title."""
    router, client = make_router(settings, {"title": "Second try", "tags": "new"})
    draft = Draft(
        body="expensive transcription",
        title="First try",
        tags=("old",),
        intent=parse_caption("", has_image=True),
    )

    refreshed = await IngestService(router, settings).regenerate(draft)

    assert refreshed.body == "expensive transcription"
    assert refreshed.title == "Second try"
    assert refreshed.tags == ("new",)
    assert "ocr" not in client.calls


async def test_regenerate_keeps_the_path_the_user_settled_on(settings: Settings) -> None:
    router, _ = make_router(settings, {"title": "New", "tags": "t"})
    draft = Draft(body="text", title="Old", path="Chosen/Place.md")

    refreshed = await IngestService(router, settings).regenerate(draft)

    assert refreshed.path == "Chosen/Place.md"


# --- Availability ---------------------------------------------------------


def test_a_bot_with_no_router_reports_why(settings: Settings) -> None:
    service = IngestService(None, settings)

    assert service.available is False
    assert service.vision_available is False
    assert "No AI provider is configured" in service.unavailable_reason()


def test_a_router_with_no_chat_rung_reports_why(settings: Settings) -> None:
    router, _ = make_router(settings, {}, chat=False)

    assert IngestService(router, settings).unavailable_reason().startswith("No AI provider")


# --- Tag parsing ----------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("planning hiring", ("planning", "hiring")),
        ("#planning, #hiring", ("planning", "hiring")),
        ("planning and hiring", ("planning", "hiring")),
        ("- planning\n- hiring", ("planning", "hiring")),
        ("Planning PLANNING planning", ("planning",)),
        ("", ()),
    ],
)
def test_a_models_answer_becomes_clean_tags(raw: str, expected: tuple[str, ...]) -> None:
    assert parse_tags(raw, 5) == expected


def test_a_tag_the_vault_would_reject_is_dropped_not_repaired() -> None:
    """A mangled tag pollutes the vault's tag index permanently."""
    assert parse_tags("good [bad] worse` fine", 5) == ("good", "fine")


def test_the_tag_count_is_capped() -> None:
    """A model asked for "some tags" will happily return twenty."""
    assert len(parse_tags(" ".join(f"t{n}" for n in range(30)), 5)) == 5


# --- The draft dataclass --------------------------------------------------


def test_an_empty_draft_is_recognised() -> None:
    assert Draft().is_empty is True
    assert Draft(body="x").is_empty is False
    assert Draft(attachments=("a",)).is_empty is False


def test_a_warning_is_not_repeated() -> None:
    assert Draft().warn("same").warn("same").warnings == ("same",)
