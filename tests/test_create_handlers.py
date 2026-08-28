"""Creation handlers, and the phase 6 Definition of Done end to end.

The headline test is `test_the_headline_scenario_works_end_to_end`: a photo
with the caption from the roadmap, a preview, a tap on Save, and a note at the
right path with a sensible title and tags.

Everything else here defends the property that makes the flow safe to use —
**nothing reaches the vault until Save is tapped** — and the property that
makes it safe to use *twice*: a double tap cannot create the note twice.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest
from telegram.ext import ApplicationHandlerStop

from discoverygram.adapters.session import MemorySessionStore
from discoverygram.app.probe import InstanceState
from discoverygram.bot.browse import PENDING_KEY
from discoverygram.bot.create import (
    ask_command,
    attachment_message,
    default_text_capture,
    draft_callback,
    forwarded_message,
    new_command,
    pending_input,
    quick_command,
    summarize_command,
    template_callback,
    template_command,
)
from discoverygram.bot.deps import DEPS_KEY, BotDeps
from discoverygram.bot.tokens import CallbackTokens
from discoverygram.config import Settings
from discoverygram.llm.plan import Attempt, TaskProfile
from discoverygram.llm.router import LlmRouter, TaskLadder
from discoverygram.ports.errors import NotFound, Unavailable, Unsupported
from discoverygram.ports.llm import Completion, LlmClient, Message, Usage
from discoverygram.ports.model import (
    InstanceConfig,
    MediaUpload,
    Note,
    NoteRef,
    SearchHit,
    SearchMatch,
    Template,
    TemplateRef,
    TreeNode,
)
from tests.fixtures.telegram import (
    FakeBot,
    FakeContext,
    as_context,
    assert_markdown_v2_safe,
    make_callback_update,
    make_document_update,
    make_forward_update,
    make_photo_update,
    make_update,
)


class ScriptedClient(LlmClient):
    """One answer per pipeline step, chosen by the system prompt."""

    def __init__(self, answers: dict[str, str]) -> None:
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
        system = messages[0].text if messages else ""
        step = (
            "ocr"
            if "transcribe images" in system
            else "tidy"
            if "clean up" in system
            else "title"
            if "write titles" in system
            else "describe"
            if "describe images" in system
            else "tags"
            if "suggest tags" in system
            else "summary"
            if "summarise" in system
            else "ask"
        )
        self.calls.append(step)
        return Completion(
            text=self.answers.get(step, "text"),
            provider="fake",
            model="m",
            usage=Usage(),
            latency_s=0.01,
        )

    def supports_vision(self) -> bool:
        return True

    async def aclose(self) -> None:
        return None


class StubStore:
    """Enough of a vault to exercise resolution, writing and uploading."""

    def __init__(
        self,
        *,
        folders: tuple[str, ...] = (),
        notes: dict[str, str] | None = None,
        upload: MediaUpload | Exception | None = None,
        templates: list[TemplateRef] | None = None,
        hits: list[SearchHit] | None = None,
    ) -> None:
        self.folders = folders
        self.notes = notes or {}
        self.upload = upload
        self.templates_list = templates if templates is not None else [TemplateRef(name="Meeting")]
        self.hits = hits or []
        self.created: list[tuple[str, str]] = []
        self.appended: list[tuple[str, str]] = []
        self.uploads: list[str] = []
        self.templated: list[tuple[str, str]] = []

    async def get_tree(self, *, refresh: bool = False) -> TreeNode:
        def node(prefix: str, name: str) -> TreeNode:
            path = f"{prefix}/{name}" if prefix else name
            children = tuple(
                node(path, child)
                for child in sorted(
                    {
                        folder[len(path) + 1 :].split("/")[0]
                        for folder in self.folders
                        if folder.startswith(f"{path}/")
                    }
                )
            )
            return TreeNode(path=path, name=name, folders=children)

        roots = sorted({folder.split("/")[0] for folder in self.folders})
        return TreeNode(path="", name="", folders=tuple(node("", root) for root in roots))

    async def get_note(self, path: str, *, include_backlinks: bool = True) -> Note:
        if path not in self.notes:
            raise NotFound(f"no note at {path}")
        return Note(ref=NoteRef.from_path(path), content=self.notes[path], lines=1)

    async def create_note(self, path: str, content: str) -> NoteRef:
        self.created.append((path, content))
        self.notes[path] = content
        return NoteRef.from_path(path)

    async def append_note(self, path: str, content: str, *, add_timestamp: bool = False) -> None:
        self.appended.append((path, content))

    async def create_folder(self, path: str) -> str:
        return path

    async def upload_media(
        self,
        filename: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
        note_path: str = "",
    ) -> MediaUpload:
        if isinstance(self.upload, Exception):
            raise self.upload
        self.uploads.append(filename)
        return self.upload or MediaUpload(
            path=f"media/{filename}", filename=filename, media_type=content_type
        )

    async def list_templates(self) -> list[TemplateRef]:
        return list(self.templates_list)

    async def get_template(self, name: str) -> Template:
        return Template(name=name, content="# {{title}}")

    async def create_note_from_template(self, template_name: str, note_path: str) -> NoteRef:
        self.templated.append((template_name, note_path))
        return NoteRef.from_path(note_path)

    async def search(
        self, query: str, *, limit: int | None = None, offset: int = 0
    ) -> list[SearchHit]:
        return list(self.hits)


@pytest.fixture
def bot() -> FakeBot:
    return FakeBot()


@pytest.fixture
def sessions() -> MemorySessionStore:
    return MemorySessionStore(default_ttl_s=3600)


DEFAULT_ANSWERS = {
    "ocr": "Q1 planning\n- hire two engineers",
    "tidy": "Q1 planning\n\n- hire two engineers",
    "title": "Q1 planning",
    "tags": "planning hiring",
    "summary": "A short summary.",
    "ask": "The budget is 12k [A.md].",
}


def make_context(
    settings: Settings,
    bot: FakeBot,
    sessions: MemorySessionStore,
    *,
    store: StubStore | None = None,
    answers: dict[str, str] | None = None,
    with_llm: bool = True,
    args: list[str] | None = None,
) -> FakeContext:
    router: LlmRouter | None = None
    if with_llm:
        rung = (Attempt(provider="fake", model="m"),)
        router = LlmRouter(
            settings,
            {"fake": ScriptedClient(answers or DEFAULT_ANSWERS)},
            {task: TaskLadder(task=task, attempts=rung) for task in TaskProfile},
        )

    deps = BotDeps(
        settings=settings,
        notes=store or StubStore(),  # type: ignore[arg-type]
        sessions=sessions,
        tokens=CallbackTokens(sessions, ttl_s=settings.session_ttl_s),
        instance=InstanceState(config=InstanceConfig(version="0.31.3"), healthy=True),
        llm=router,
    )
    context = FakeContext(bot, {DEPS_KEY: deps})
    context.args = args or []
    return context


def buttons(markup: Any) -> dict[str, str]:
    return {button.text: button.callback_data for row in markup.inline_keyboard for button in row}


# --- The Definition of Done ----------------------------------------------


async def test_the_headline_scenario_works_end_to_end(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    """Photo + caption -> preview -> Save -> the note exists at the right path."""
    store = StubStore(folders=("Projects/Research",))
    context = make_context(settings, bot, sessions, store=store)
    caption = "extract the text and create a note under Projects/Research, you generate the title"

    await attachment_message(make_photo_update(bot, caption=caption), as_context(context))

    card = bot.sent[-1]
    assert "Q1 planning" in card["text"]
    assert "`Projects/Research/Q1 planning.md`" in card["text"]
    assert "#planning" in card["text"]
    assert store.created == [], "nothing may be written before Save"
    assert_markdown_v2_safe(card["text"])

    save = buttons(card["reply_markup"])["💾 Save"]
    await draft_callback(make_callback_update(bot, data=save), as_context(context))

    path, content = store.created[0]
    assert path == "Projects/Research/Q1 planning.md"
    assert content.startswith("# Q1 planning")
    assert "hire two engineers" in content
    assert "#planning #hiring" in content


# --- Preview before write -------------------------------------------------


async def test_cancel_writes_nothing(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    store = StubStore()
    context = make_context(settings, bot, sessions, store=store)
    await attachment_message(make_photo_update(bot), as_context(context))

    cancel = buttons(bot.sent[-1]["reply_markup"])["✖ Cancel"]
    await draft_callback(make_callback_update(bot, data=cancel), as_context(context))

    assert store.created == []
    assert "Nothing was written" in bot.edited[-1]["text"]


async def test_a_double_tap_on_save_cannot_create_the_note_twice(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    """The token is revoked the moment the write succeeds."""
    store = StubStore()
    context = make_context(settings, bot, sessions, store=store)
    await attachment_message(make_photo_update(bot), as_context(context))
    save = buttons(bot.sent[-1]["reply_markup"])["💾 Save"]

    await draft_callback(make_callback_update(bot, data=save), as_context(context))
    await draft_callback(make_callback_update(bot, data=save), as_context(context))

    assert len(store.created) == 1
    assert bot.answered_with[-1] != "Saved"


async def test_an_expired_draft_says_so(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    context = make_context(settings, bot, sessions)

    await draft_callback(make_callback_update(bot, data="dr:deadbeef:save"), as_context(context))

    assert "expired" in bot.answered_with[-1]


async def test_the_whole_draft_costs_one_session_entry(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    """Six buttons on one card, one token: the rule the action bar established."""
    context = make_context(settings, bot, sessions)

    await attachment_message(make_photo_update(bot), as_context(context))

    tokens = {
        CallbackTokens.split(data)[1] for data in buttons(bot.sent[-1]["reply_markup"]).values()
    }
    assert len(tokens) == 1
    assert len(sessions) == 1


# --- Editing a draft ------------------------------------------------------


async def test_the_title_button_asks_and_then_retitles(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    context = make_context(settings, bot, sessions)
    await attachment_message(make_photo_update(bot), as_context(context))
    title_button = buttons(bot.sent[-1]["reply_markup"])["✏️ Title"]

    await draft_callback(make_callback_update(bot, data=title_button), as_context(context))
    assert "Send the title" in bot.last_text

    with pytest.raises(ApplicationHandlerStop):
        await pending_input(make_update(bot, text="My own title"), as_context(context))

    assert "My own title" in bot.last_text


async def test_a_retitled_note_follows_its_new_name(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    """Otherwise the card names a file the user has just renamed away from."""
    context = make_context(settings, bot, sessions)
    await attachment_message(make_photo_update(bot), as_context(context))
    title_button = buttons(bot.sent[-1]["reply_markup"])["✏️ Title"]
    await draft_callback(make_callback_update(bot, data=title_button), as_context(context))

    with pytest.raises(ApplicationHandlerStop):
        await pending_input(make_update(bot, text="Renamed"), as_context(context))

    assert "`Inbox/Renamed.md`" in bot.last_text


async def test_the_path_button_moves_the_draft(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    store = StubStore(folders=("Archive",))
    context = make_context(settings, bot, sessions, store=store)
    await attachment_message(make_photo_update(bot), as_context(context))
    path_button = buttons(bot.sent[-1]["reply_markup"])["📁 Path"]

    await draft_callback(make_callback_update(bot, data=path_button), as_context(context))
    with pytest.raises(ApplicationHandlerStop):
        await pending_input(make_update(bot, text="Archive"), as_context(context))

    assert "Archive/Q1 planning" in bot.last_text.replace("\\", "")


async def test_regenerate_asks_again_without_re_reading_the_image(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    context = make_context(settings, bot, sessions)
    await attachment_message(make_photo_update(bot), as_context(context))
    client = _client_of(context)
    client.calls.clear()

    regen = buttons(bot.sent[-1]["reply_markup"])["🔄 Regenerate"]
    await draft_callback(make_callback_update(bot, data=regen), as_context(context))

    assert "ocr" not in client.calls
    assert "title" in client.calls


def _client_of(context: FakeContext) -> ScriptedClient:
    router = context.bot_data[DEPS_KEY].llm
    # Reaching into the router is deliberate: the double is what the test
    # scripted, and there is no public accessor for it.
    client = router._clients["fake"]
    assert isinstance(client, ScriptedClient)
    return client


# --- Ambiguity ------------------------------------------------------------


async def test_two_matching_folders_are_offered_as_a_keyboard(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    store = StubStore(folders=("Projects/Research", "Archive/Research"))
    context = make_context(settings, bot, sessions, store=store)

    await attachment_message(
        make_photo_update(bot, caption="save under Research"), as_context(context)
    )

    labels = list(buttons(bot.sent[-1]["reply_markup"]))
    assert "Projects/Research" in labels
    assert "Archive/Research" in labels
    assert store.created == []


async def test_picking_a_folder_produces_the_card(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    store = StubStore(folders=("Projects/Research", "Archive/Research"))
    context = make_context(settings, bot, sessions, store=store)
    await attachment_message(
        make_photo_update(bot, caption="save under Research"), as_context(context)
    )
    pick = buttons(bot.sent[-1]["reply_markup"])["Projects/Research"]

    await draft_callback(make_callback_update(bot, data=pick), as_context(context))

    assert "Projects/Research/Q1 planning" in bot.last_text.replace("\\", "")


# --- Attachments ----------------------------------------------------------


async def test_the_file_is_uploaded_before_any_llm_work(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    """So a photo survives a provider outage: the draft still carries it."""
    store = StubStore()
    context = make_context(settings, bot, sessions, store=store)

    await attachment_message(make_photo_update(bot), as_context(context))

    assert store.uploads == ["photo.jpg"]
    assert "media/photo.jpg" in bot.sent[-1]["text"].replace("\\", "")


async def test_a_transport_that_cannot_upload_still_produces_a_note(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    """Losing the text because the transport cannot carry files is the wrong trade."""
    store = StubStore(upload=Unsupported("media upload is REST only"))
    context = make_context(settings, bot, sessions, store=store)

    await attachment_message(make_photo_update(bot), as_context(context))

    card = bot.sent[-1]["text"]
    assert "Q1 planning" in card
    assert "NOTEDISCOVERY_TRANSPORT=rest" in card.replace("\\", "")


async def test_a_file_over_the_limit_is_refused_before_it_is_downloaded(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    """Telegram reports the size, so an oversized file costs no transfer at all."""
    context = make_context(settings, bot, sessions)

    await attachment_message(
        make_photo_update(bot, file_size=settings.max_upload_bytes + 1), as_context(context)
    )

    assert "limit" in bot.last_text
    assert bot.downloaded == []


async def test_a_non_image_document_is_refused_with_the_types_it_accepts(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    context = make_context(settings, bot, sessions)

    await attachment_message(
        make_document_update(bot, mime_type="application/pdf"), as_context(context)
    )

    assert "image" in bot.last_text
    assert bot.downloaded == []


async def test_a_file_that_is_not_really_an_image_is_refused(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    """`mime_type` is what the sending client claimed; the bytes are the fact."""
    store = StubStore()
    context = make_context(settings, bot, sessions, store=store)
    bot.file_bytes = b"%PDF-1.7 not an image at all"

    await attachment_message(make_document_update(bot, mime_type="image/png"), as_context(context))

    assert "does not look like an image" in bot.last_text
    assert store.uploads == []


async def test_a_mislabelled_image_is_corrected_rather_than_refused(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    """Phones mislabel images routinely; a real PNG called a JPEG is still readable."""
    store = StubStore()
    context = make_context(settings, bot, sessions, store=store)
    bot.file_bytes = b"\x89PNG\r\n\x1a\n" + b"body"

    await attachment_message(make_document_update(bot, mime_type="image/jpeg"), as_context(context))

    assert store.uploads == ["scan.png"]


async def test_an_upload_filename_from_a_client_is_reduced_to_one_segment(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    store = StubStore()
    context = make_context(settings, bot, sessions, store=store)

    await attachment_message(
        make_document_update(bot, file_name="../../../etc/cron.d/evil.png"),
        as_context(context),
    )

    assert store.uploads == ["evil.png"]


async def test_an_image_document_goes_through_the_same_pipeline(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    store = StubStore()
    context = make_context(settings, bot, sessions, store=store)

    await attachment_message(make_document_update(bot), as_context(context))

    assert store.uploads == ["scan.png"]
    assert "Q1 planning" in bot.sent[-1]["text"]


async def test_an_album_becomes_one_draft(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    """Three photos sent together must not become three notes."""
    import asyncio

    from discoverygram.bot import create as create_module
    from discoverygram.bot.albums import AlbumBuffer

    store = StubStore()
    context = make_context(settings, bot, sessions, store=store)

    async def immediately(_delay: float) -> None:
        await asyncio.sleep(0)

    create_module._ALBUMS = AlbumBuffer(sleep=immediately)

    first = asyncio.create_task(
        attachment_message(
            make_photo_update(bot, media_group_id="g1", caption="save under Inbox", file_id="p1"),
            as_context(context),
        )
    )
    await asyncio.sleep(0)
    await attachment_message(
        make_photo_update(bot, media_group_id="g1", file_id="p2"), as_context(context)
    )
    await first

    cards = [message for message in bot.sent if "Draft" in message["text"]]
    assert len(cards) == 1
    assert len(store.uploads) == 2


# --- Simple creation ------------------------------------------------------


async def test_new_creates_a_note_at_the_path(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    store = StubStore()
    context = make_context(
        settings, bot, sessions, store=store, args=["Projects/Idea", "the", "body"]
    )

    await new_command(make_update(bot), as_context(context))

    assert store.created[0][0] == "Projects/Idea.md"
    assert "the body" in store.created[0][1]


async def test_new_never_overwrites_an_existing_note(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    """`create_note` is an upsert; without this rule `/new` destroys notes."""
    store = StubStore(notes={"Idea.md": "the original"})
    context = make_context(settings, bot, sessions, store=store, args=["Idea", "replacement"])

    await new_command(make_update(bot), as_context(context))

    assert store.created[0][0] == "Idea-2.md"
    assert store.notes["Idea.md"] == "the original"
    assert "already existed" in bot.last_text.replace("\\", "")


async def test_new_without_a_body_shows_usage(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    context = make_context(settings, bot, sessions, args=["Projects/Idea"])

    await new_command(make_update(bot), as_context(context))

    assert "/new" in bot.last_text
    assert_markdown_v2_safe(bot.last_text)


async def test_new_works_without_any_llm_configured(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    """Milestone M1 features must not acquire a provider dependency."""
    store = StubStore()
    context = make_context(
        settings, bot, sessions, store=store, with_llm=False, args=["Idea", "body"]
    )

    await new_command(make_update(bot), as_context(context))

    assert store.created[0][0] == "Idea.md"


async def test_quick_captures_into_the_inbox(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    store = StubStore()
    context = make_context(settings, bot, sessions, store=store, args=["buy", "milk"])

    await quick_command(make_update(bot), as_context(context))

    assert store.created[0][0].startswith(settings.inbox_path)
    assert "buy milk" in store.created[0][1]


async def test_a_plain_message_becomes_a_capture_when_configured(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    quick = settings.model_copy(update={"default_text_action": "quick"})
    store = StubStore()
    context = make_context(quick, bot, sessions, store=store)

    with pytest.raises(ApplicationHandlerStop):
        await default_text_capture(make_update(bot, text="buy milk"), as_context(context))

    assert "buy milk" in store.created[0][1]


async def test_a_plain_message_is_left_to_search_by_default(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    store = StubStore()
    context = make_context(settings, bot, sessions, store=store)

    await default_text_capture(make_update(bot, text="docker"), as_context(context))

    assert store.created == []
    assert bot.sent == []


# --- Templates ------------------------------------------------------------


async def test_the_template_picker_lists_the_vaults_templates(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    context = make_context(settings, bot, sessions)

    await template_command(make_update(bot), as_context(context))

    assert "Meeting" in buttons(bot.sent[-1]["reply_markup"])


async def test_a_vault_with_no_templates_says_so(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    context = make_context(settings, bot, sessions, store=StubStore(templates=[]))

    await template_command(make_update(bot), as_context(context))

    assert "no templates" in bot.last_text


async def test_picking_a_template_asks_for_a_path_then_creates_it(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    store = StubStore()
    context = make_context(settings, bot, sessions, store=store)
    await template_command(make_update(bot), as_context(context))
    pick = buttons(bot.sent[-1]["reply_markup"])["Meeting"]

    await template_callback(make_callback_update(bot, data=pick), as_context(context))
    assert "Where should" in bot.last_text

    with pytest.raises(ApplicationHandlerStop):
        await pending_input(make_update(bot, text="Meetings/Monday"), as_context(context))

    assert store.templated == [("Meeting", "Meetings/Monday.md")]


async def test_new_with_a_template_flag_creates_it_directly(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    store = StubStore()
    context = make_context(
        settings, bot, sessions, store=store, args=["--template", "Meeting", "Meetings/Monday"]
    )

    await new_command(make_update(bot), as_context(context))

    assert store.templated == [("Meeting", "Meetings/Monday.md")]


# --- Assist ---------------------------------------------------------------


async def test_summarize_answers_with_the_note_cited(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    store = StubStore(notes={"A.md": "a long note"})
    context = make_context(settings, bot, sessions, store=store, args=["A.md"])

    await summarize_command(make_update(bot), as_context(context))

    assert "A short summary" in bot.last_text
    assert "Sources" in bot.last_text
    assert_markdown_v2_safe(bot.last_text)


async def test_summarize_without_a_provider_names_the_variable(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    context = make_context(settings, bot, sessions, with_llm=False, args=["A.md"])

    await summarize_command(make_update(bot), as_context(context))

    assert "LLM_CHAIN_CHAT" in bot.last_text.replace("\\", "")


async def test_ask_answers_with_its_sources(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    store = StubStore(
        notes={"A.md": "the budget is 12k"},
        hits=[SearchHit(ref=NoteRef.from_path("A.md"), matches=(SearchMatch(1, "budget"),))],
    )
    context = make_context(
        settings, bot, sessions, store=store, args=["what", "is", "the", "budget"]
    )

    await ask_command(make_update(bot), as_context(context))

    assert "12k" in bot.last_text
    assert "A\\.md" in bot.last_text
    assert_markdown_v2_safe(bot.last_text)


async def test_ask_with_no_matching_notes_says_it_does_not_know(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    """Better than an answer from training data the user cannot distinguish."""
    context = make_context(
        settings, bot, sessions, store=StubStore(), args=["anything", "at", "all"]
    )

    await ask_command(make_update(bot), as_context(context))

    assert "could not find an answer" in bot.last_text.replace("\\", "")


async def test_ask_without_a_question_shows_usage(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    context = make_context(settings, bot, sessions)

    await ask_command(make_update(bot), as_context(context))

    assert "/ask" in bot.last_text
    assert_markdown_v2_safe(bot.last_text)


# --- Rendering ------------------------------------------------------------


async def test_a_card_with_reserved_characters_everywhere_is_still_sendable(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    answers = {
        "ocr": "Q1 2026 — costs (draft) [v2]! 100% #done",
        "tidy": "Q1 2026 — costs (draft) [v2]! 100% #done",
        "title": "Q1 2026 — costs (draft) [v2]!",
        "tags": "q1-2026 costs",
    }
    context = make_context(settings, bot, sessions, answers=answers)

    await attachment_message(make_photo_update(bot), as_context(context))

    assert_markdown_v2_safe(bot.sent[-1]["text"])


async def test_the_pending_key_is_shared_so_cancel_clears_a_draft_flow(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    """`/cancel` clears `user_data`; a private key would survive it."""
    context = make_context(settings, bot, sessions)
    await attachment_message(make_photo_update(bot), as_context(context))
    title_button = buttons(bot.sent[-1]["reply_markup"])["✏️ Title"]

    await draft_callback(make_callback_update(bot, data=title_button), as_context(context))

    assert PENDING_KEY in context.user_data


async def test_a_pending_kind_this_module_does_not_own_is_left_alone(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    """The browse flows still see their own pending edits."""
    context = make_context(settings, bot, sessions)
    context.user_data[PENDING_KEY] = {"kind": "append", "path": "A.md"}

    await pending_input(make_update(bot, text="more text"), as_context(context))

    assert context.user_data[PENDING_KEY]["kind"] == "append"


# --- Failure paths --------------------------------------------------------


async def test_a_failed_save_says_so_and_leaves_the_card(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    class Refusing(StubStore):
        async def create_note(self, path: str, content: str) -> NoteRef:
            raise Unavailable("the vault is down")

    store = Refusing()
    context = make_context(settings, bot, sessions, store=store)
    await attachment_message(make_photo_update(bot), as_context(context))
    save = buttons(bot.sent[-1]["reply_markup"])["💾 Save"]

    await draft_callback(make_callback_update(bot, data=save), as_context(context))

    assert "could not save" in bot.edited[-1]["text"]


async def test_a_failed_upload_warns_but_still_produces_a_draft(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    store = StubStore(upload=Unavailable("media store is full"))
    context = make_context(settings, bot, sessions, store=store)

    await attachment_message(make_photo_update(bot), as_context(context))

    card = bot.sent[-1]["text"]
    assert "could not attach" in card
    assert "Q1 planning" in card


async def test_a_template_the_transport_cannot_reach_explains_itself(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    class NoTemplates(StubStore):
        async def list_templates(self) -> list[TemplateRef]:
            raise Unsupported("templates are REST only")

    context = make_context(settings, bot, sessions, store=NoTemplates())

    await template_command(make_update(bot), as_context(context))

    assert "REST only" in bot.last_text


async def test_an_expired_template_token_says_so(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    context = make_context(settings, bot, sessions)

    await template_callback(make_callback_update(bot, data="tpl:deadbeef:0"), as_context(context))

    assert "expired" in bot.answered_with[-1]


async def test_a_template_index_outside_the_list_is_refused(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    """Callback data is whatever arrives; a stale index must not crash."""
    context = make_context(settings, bot, sessions)
    await template_command(make_update(bot), as_context(context))
    pick = buttons(bot.sent[-1]["reply_markup"])["Meeting"]
    stale = f"{pick.rsplit(':', 1)[0]}:99"

    await template_callback(make_callback_update(bot, data=stale), as_context(context))

    assert "expired" in bot.answered_with[-1]


async def test_an_unknown_draft_verb_is_refused_rather_than_acted_on(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    context = make_context(settings, bot, sessions)
    await attachment_message(make_photo_update(bot), as_context(context))
    save = buttons(bot.sent[-1]["reply_markup"])["💾 Save"]
    forged = f"{save.rsplit(':', 1)[0]}:destroy"

    await draft_callback(make_callback_update(bot, data=forged), as_context(context))

    assert "expired" in bot.answered_with[-1]


async def test_a_photo_that_yields_nothing_is_not_turned_into_an_empty_note(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    class NoUpload(StubStore):
        async def upload_media(
            self,
            filename: str,
            data: bytes,
            *,
            content_type: str = "application/octet-stream",
            note_path: str = "",
        ) -> MediaUpload:
            raise Unsupported("no media over this transport")

    answers = {"ocr": "NO_TEXT", "describe": "", "title": "", "tags": ""}
    context = make_context(settings, bot, sessions, store=NoUpload(), answers=answers)

    await attachment_message(make_photo_update(bot, caption=""), as_context(context))

    assert "nothing I could turn into a note" in bot.last_text.replace("\\", "")


async def test_quick_without_text_shows_usage(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    context = make_context(settings, bot, sessions)

    await quick_command(make_update(bot), as_context(context))

    assert "/quick" in bot.last_text
    assert_markdown_v2_safe(bot.last_text)


async def test_a_template_flag_without_a_path_shows_usage(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    context = make_context(settings, bot, sessions, args=["--template", "Meeting"])

    await new_command(make_update(bot), as_context(context))

    assert "--template" in bot.last_text.replace("\\", "")


async def test_new_with_nothing_at_all_shows_usage(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    context = make_context(settings, bot, sessions)

    await new_command(make_update(bot), as_context(context))

    assert "/new" in bot.last_text


async def test_a_new_path_that_names_two_folders_is_disambiguated_from_a_message(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    """The path prompt has to handle ambiguity as well as the caption does."""
    store = StubStore(folders=("Projects/Research", "Archive/Research"))
    context = make_context(settings, bot, sessions, store=store)
    await attachment_message(make_photo_update(bot), as_context(context))
    path_button = buttons(bot.sent[-1]["reply_markup"])["📁 Path"]
    await draft_callback(make_callback_update(bot, data=path_button), as_context(context))

    with pytest.raises(ApplicationHandlerStop):
        await pending_input(make_update(bot, text="Research"), as_context(context))

    assert "Which one" in bot.last_text.replace("\\", "")


async def test_a_pending_draft_whose_token_expired_says_so(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    context = make_context(settings, bot, sessions)
    context.user_data[PENDING_KEY] = {"kind": "draft_title", "draft": "not a draft"}

    with pytest.raises(ApplicationHandlerStop):
        await pending_input(make_update(bot, text="a title"), as_context(context))

    assert "expired" in bot.last_text


async def test_pending_input_ignores_a_chat_with_no_pending_flow(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    context = make_context(settings, bot, sessions)

    await pending_input(make_update(bot, text="just a message"), as_context(context))

    assert bot.sent == []


async def test_a_template_path_that_is_refused_reports_the_reason(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    class Refusing(StubStore):
        async def create_note_from_template(self, template_name: str, note_path: str) -> NoteRef:
            raise Unavailable("the vault is down")

    context = make_context(settings, bot, sessions, store=Refusing())
    await template_command(make_update(bot), as_context(context))
    pick = buttons(bot.sent[-1]["reply_markup"])["Meeting"]
    await template_callback(make_callback_update(bot, data=pick), as_context(context))

    with pytest.raises(ApplicationHandlerStop):
        await pending_input(make_update(bot, text="Meetings/Monday"), as_context(context))

    assert "vault is down" in bot.last_text


async def test_the_working_notice_is_removed_when_the_draft_is_ready(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    """A "Reading…" message left behind makes the bot look stuck."""
    context = make_context(settings, bot, sessions)

    await attachment_message(make_photo_update(bot), as_context(context))

    assert bot.deleted, "the transient notice should have been deleted"


async def test_a_message_that_is_neither_photo_nor_document_is_ignored(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    context = make_context(settings, bot, sessions)

    await attachment_message(make_update(bot, text="not an attachment"), as_context(context))

    assert bot.sent == []


# --- Forwards -------------------------------------------------------------


async def test_a_forward_becomes_a_draft_rather_than_a_silent_capture(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    """The title and the path are exactly what the user has not decided yet."""
    store = StubStore()
    context = make_context(settings, bot, sessions, store=store)

    with pytest.raises(ApplicationHandlerStop):
        await forwarded_message(
            make_forward_update(bot, text="an interesting paragraph"), as_context(context)
        )

    card = bot.sent[-1]["text"]
    assert "Draft" in card
    assert "an interesting paragraph" in card
    assert store.created == []


async def test_a_forward_records_who_wrote_it(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    """Otherwise it reads, six months later, as something the reader wrote."""
    context = make_context(settings, bot, sessions)

    with pytest.raises(ApplicationHandlerStop):
        await forwarded_message(
            make_forward_update(bot, sender="Ada Lovelace"), as_context(context)
        )

    assert "Ada Lovelace" in bot.sent[-1]["text"]


async def test_a_forwarded_link_is_kept_verbatim_and_never_fetched(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    """Fetching any URL a user pastes is a server-side request forgery primitive."""
    context = make_context(settings, bot, sessions)
    url = "http://169.254.169.254/latest/meta-data/"

    with pytest.raises(ApplicationHandlerStop):
        await forwarded_message(make_forward_update(bot, text=url), as_context(context))

    assert url in bot.sent[-1]["text"].replace("\\", "")
