"""Navigation handlers: browsing, opening, the action bar and folder operations."""

from __future__ import annotations

import pytest

from discoverygram.adapters.session import MemorySessionStore
from discoverygram.adapters.tree import build_tree
from discoverygram.app.probe import InstanceState
from discoverygram.bot.browse import (
    ACT_ACTION,
    NAV_ACTION,
    NOTE_ACTION,
    PENDING_KEY,
    act_callback,
    backlinks_command,
    browse_command,
    folder_command,
    nav_callback,
    note_callback,
    open_command,
    pending_input,
    related_command,
)
from discoverygram.bot.deps import DEPS_KEY, BotDeps
from discoverygram.bot.tokens import CallbackTokens
from discoverygram.config import Settings
from discoverygram.ports.errors import Unsupported
from discoverygram.ports.model import (
    Backlink,
    Graph,
    GraphEdge,
    InstanceConfig,
    Note,
    NoteRef,
    ShareLink,
    TreeNode,
)
from tests.fixtures.telegram import (
    FakeBot,
    FakeContext,
    as_context,
    assert_markdown_v2_safe,
    make_callback_update,
    make_update,
)

PATHS = ["Welcome.md", "Projects/Roadmap.md", "Projects/Ideas.md", "Projects/2026/Q1.md"]
FOLDERS = ["Projects", "Projects/2026", "Archive"]


class StubNoteStore:
    def __init__(
        self,
        *,
        paths: list[str] | None = None,
        content: str = "note body",
        backlinks: list[Backlink] | None = None,
        graph: Graph | None = None,
        raises: Exception | None = None,
    ) -> None:
        self._paths = paths or PATHS
        self.content = content
        self._backlinks = backlinks or []
        self._graph = graph or Graph()
        self._raises = raises
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    async def get_tree(self, *, refresh: bool = False) -> TreeNode:
        return build_tree([NoteRef.from_path(path) for path in self._paths], FOLDERS)

    async def get_note(self, path: str, *, include_backlinks: bool = True) -> Note:
        self.calls.append(("get_note", (path,)))
        return Note(ref=NoteRef.from_path(path), content=self.content, lines=2)

    async def get_backlinks(self, path: str) -> list[Backlink]:
        return list(self._backlinks)

    async def get_graph(self) -> Graph:
        return self._graph

    async def append_note(self, path: str, content: str, *, add_timestamp: bool = False) -> None:
        self.calls.append(("append_note", (path, content, add_timestamp)))

    async def update_note(self, path: str, content: str) -> NoteRef:
        self.calls.append(("update_note", (path, content)))
        return NoteRef.from_path(path)

    async def create_note(self, path: str, content: str) -> NoteRef:
        self.calls.append(("create_note", (path, content)))
        return NoteRef.from_path(path)

    async def delete_note(self, path: str) -> None:
        self.calls.append(("delete_note", (path,)))

    async def share_note(self, path: str, *, theme: str = "light") -> ShareLink:
        if self._raises:
            raise self._raises
        return ShareLink(url="http://x/share/abc", token="abc", path=path)

    async def create_folder(self, path: str) -> str:
        self.calls.append(("create_folder", (path,)))
        return path

    async def rename_folder(self, old: str, new: str) -> str:
        self.calls.append(("rename_folder", (old, new)))
        if self._raises:
            raise self._raises
        return new

    async def move_folder(self, old: str, new: str) -> str:
        self.calls.append(("move_folder", (old, new)))
        return new

    async def delete_folder(self, path: str) -> None:
        self.calls.append(("delete_folder", (path,)))


@pytest.fixture
def bot() -> FakeBot:
    return FakeBot()


@pytest.fixture
def sessions() -> MemorySessionStore:
    return MemorySessionStore(default_ttl_s=3600)


def make_context(
    settings: Settings,
    bot: FakeBot,
    sessions: MemorySessionStore,
    *,
    notes: StubNoteStore | None = None,
    args: list[str] | None = None,
) -> FakeContext:
    deps = BotDeps(
        settings=settings,
        notes=notes or StubNoteStore(),  # type: ignore[arg-type]
        sessions=sessions,
        tokens=CallbackTokens(sessions, ttl_s=settings.session_ttl_s),
        instance=InstanceState(config=InstanceConfig(version="0.31.3"), healthy=True),
    )
    context = FakeContext(bot, {DEPS_KEY: deps})
    context.args = args or []
    return context


def buttons(markup: object) -> list[tuple[str, str]]:
    return [
        (button.text, button.callback_data or "")
        for row in markup.inline_keyboard  # type: ignore[attr-defined]
        for button in row
    ]


# --- Browsing -------------------------------------------------------------


async def test_browse_opens_at_the_root(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    context = make_context(settings, bot, sessions)

    await browse_command(make_update(bot), as_context(context))

    labels = [label for label, _ in buttons(bot.sent[-1]["reply_markup"])]
    assert any("Projects" in label for label in labels)
    assert any("Welcome" in label for label in labels)
    assert_markdown_v2_safe(bot.last_text)


async def test_the_root_listing_offers_no_up_button(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    context = make_context(settings, bot, sessions)

    await browse_command(make_update(bot), as_context(context))

    assert not any("Up" in label for label, _ in buttons(bot.sent[-1]["reply_markup"]))


async def test_stepping_into_a_folder_lists_its_children(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    context = make_context(settings, bot, sessions)
    await browse_command(make_update(bot), as_context(context))
    projects = next(
        data for label, data in buttons(bot.sent[-1]["reply_markup"]) if "Projects" in label
    )

    await nav_callback(make_callback_update(bot, data=projects), as_context(context))

    assert "Roadmap" in str(buttons(bot.edited[-1]["reply_markup"]))
    assert any("Up" in label for label, _ in buttons(bot.edited[-1]["reply_markup"]))


async def test_up_and_root_return_to_the_parent_and_the_top(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    context = make_context(settings, bot, sessions, args=["Projects/2026"])
    await browse_command(make_update(bot), as_context(context))

    up = next(data for label, data in buttons(bot.sent[-1]["reply_markup"]) if "Up" in label)
    await nav_callback(make_callback_update(bot, data=up), as_context(context))
    assert "Projects" in bot.edited[-1]["text"]

    root = next(data for label, data in buttons(bot.edited[-1]["reply_markup"]) if "Root" in label)
    await nav_callback(make_callback_update(bot, data=root), as_context(context))
    assert "Vault root" in bot.edited[-1]["text"]


async def test_paging_a_folder_leaks_no_session_state(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    """Every button on a listing rides the token that listing already has."""
    paths = [f"Big/n{index}.md" for index in range(60)]
    small = settings.model_copy(update={"tree_page_size": 5})
    context = make_context(small, bot, sessions, notes=StubNoteStore(paths=paths), args=["Big"])
    await browse_command(make_update(bot), as_context(context))
    before = len(sessions)

    data = next(
        label_data for label, label_data in buttons(bot.sent[-1]["reply_markup"]) if label == "▶"
    )
    for _ in range(11):
        await nav_callback(make_callback_update(bot, data=data), as_context(context))
        forward = [d for label, d in buttons(bot.edited[-1]["reply_markup"]) if label == "▶"]
        if not forward:
            break
        data = forward[0]

    assert before == 1
    assert len(sessions) == 1


async def test_an_expired_listing_says_so(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    context = make_context(settings, bot, sessions)
    await browse_command(make_update(bot), as_context(context))
    data = buttons(bot.sent[-1]["reply_markup"])[0][1]
    _, token, _ = CallbackTokens.split(data)
    await sessions.delete(f"cb:{token}")

    await nav_callback(make_callback_update(bot, data=data), as_context(context))

    assert "expired" in bot.answered_with[-1]


async def test_a_stale_entry_index_is_refused_not_a_crash(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    context = make_context(settings, bot, sessions)
    await browse_command(make_update(bot), as_context(context))
    data = buttons(bot.sent[-1]["reply_markup"])[0][1]
    base = data.rsplit(":", 1)[0]

    await nav_callback(make_callback_update(bot, data=f"{base}:e999"), as_context(context))

    assert "no longer here" in bot.answered_with[-1]


# --- Opening notes --------------------------------------------------------


async def test_open_renders_a_note_with_its_action_bar(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    context = make_context(settings, bot, sessions, args=["Projects/Roadmap"])

    await open_command(make_update(bot), as_context(context))

    labels = [label for label, _ in buttons(bot.sent[-1]["reply_markup"])]
    for expected in (
        "Edit",
        "Append",
        "Tag",
        "Backlinks",
        "Related",
        "Path",
        "Raw",
        "Share",
        "Delete",
    ):
        assert any(expected in label for label in labels), expected
    assert_markdown_v2_safe(bot.last_text)


async def test_open_without_a_path_shows_usage(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    context = make_context(settings, bot, sessions)

    await open_command(make_update(bot), as_context(context))

    assert "/open" in bot.last_text


async def test_a_note_offers_a_way_back_to_its_folder(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    """Search → note → parent folder → siblings, the phase's last work item."""
    context = make_context(settings, bot, sessions, args=["Projects/Roadmap"])
    await open_command(make_update(bot), as_context(context))

    folder = next(
        data for label, data in buttons(bot.sent[-1]["reply_markup"]) if "Folder" in label
    )
    await nav_callback(make_callback_update(bot, data=folder), as_context(context))

    assert "Ideas" in str(buttons(bot.edited[-1]["reply_markup"]))


async def test_a_wiki_link_becomes_a_button(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    notes = StubNoteStore(content="see [[Ideas]] for more")
    context = make_context(settings, bot, sessions, notes=notes, args=["Projects/Roadmap"])

    await open_command(make_update(bot), as_context(context))

    assert any("Ideas" in label for label, _ in buttons(bot.sent[-1]["reply_markup"]))


async def test_a_broken_wiki_link_is_reported_in_the_body(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    notes = StubNoteStore(content="see [[Nowhere]]")
    context = make_context(settings, bot, sessions, notes=notes, args=["Projects/Roadmap"])

    await open_command(make_update(bot), as_context(context))

    assert "Unresolved" in bot.last_text
    assert_markdown_v2_safe(bot.last_text)


async def test_a_long_note_pages_in_place(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    body = "\n\n".join("x" * 1000 for _ in range(20))
    notes = StubNoteStore(content=body)
    context = make_context(settings, bot, sessions, notes=notes, args=["Projects/Roadmap"])
    await open_command(make_update(bot), as_context(context))

    forward = next(data for label, data in buttons(bot.sent[-1]["reply_markup"]) if label == "▶")
    await note_callback(make_callback_update(bot, data=forward), as_context(context))

    assert "page 2 of" in bot.edited[-1]["text"]


async def test_split_mode_sends_consecutive_messages(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    body = "\n\n".join("x" * 1000 for _ in range(20))
    split = settings.model_copy(update={"long_note_mode": "split"})
    context = make_context(
        split, bot, sessions, notes=StubNoteStore(content=body), args=["Projects/Roadmap"]
    )

    await open_command(make_update(bot), as_context(context))

    assert len(bot.sent) > 1
    assert bot.sent[-1]["reply_markup"] is not None


# --- The action bar -------------------------------------------------------


async def open_note(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore, **kwargs: object
) -> tuple[FakeContext, dict[str, str]]:
    context = make_context(settings, bot, sessions, args=["Projects/Roadmap"], **kwargs)  # type: ignore[arg-type]
    await open_command(make_update(bot), as_context(context))
    return context, dict(buttons(bot.sent[-1]["reply_markup"]))


async def test_the_action_bar_shares_one_token(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    """Nine buttons, one session entry — not nine."""
    _, bar = await open_note(settings, bot, sessions)

    tokens = {
        CallbackTokens.split(data)[1] for label, data in bar.items() if data.startswith(ACT_ACTION)
    }
    assert len(tokens) == 1


async def test_copy_path_sends_a_tappable_code_span(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    """Telegram has no clipboard API; a code span is the copy affordance."""
    context, bar = await open_note(settings, bot, sessions)
    data = next(value for label, value in bar.items() if "Path" in label)

    await act_callback(make_callback_update(bot, data=data), as_context(context))

    assert bot.last_text == "`Projects/Roadmap\\.md`"


async def test_share_returns_the_public_link(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    context, bar = await open_note(settings, bot, sessions)
    data = next(value for label, value in bar.items() if "Share" in label)

    await act_callback(make_callback_update(bot, data=data), as_context(context))

    assert "share/abc" in bot.last_text


async def test_share_over_mcp_explains_the_gap(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    notes = StubNoteStore(raises=Unsupported("Sharing is not an MCP tool. Use REST."))
    context, bar = await open_note(settings, bot, sessions, notes=notes)
    data = next(value for label, value in bar.items() if "Share" in label)

    await act_callback(make_callback_update(bot, data=data), as_context(context))

    assert "MCP" in bot.answered_with[-1]


async def test_raw_shows_the_unrendered_source(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    notes = StubNoteStore(content="# Heading\n\n*not bold*")
    context, bar = await open_note(settings, bot, sessions, notes=notes)
    data = next(value for label, value in bar.items() if "Raw" in label)

    await act_callback(make_callback_update(bot, data=data), as_context(context))

    assert "# Heading" in bot.last_text
    assert bot.last_text.startswith("```")


async def test_backlinks_offer_a_button_per_linking_note(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    notes = StubNoteStore(backlinks=[Backlink(path="Projects/Ideas.md", title="Ideas")])
    context, bar = await open_note(settings, bot, sessions, notes=notes)
    data = next(value for label, value in bar.items() if "Backlinks" in label)

    await act_callback(make_callback_update(bot, data=data), as_context(context))

    assert "Ideas" in bot.last_text
    assert bot.sent[-1]["reply_markup"] is not None


async def test_related_uses_the_graph(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    graph = Graph(edges=(GraphEdge(source="Projects/Roadmap.md", target="Welcome.md"),))
    context, bar = await open_note(settings, bot, sessions, notes=StubNoteStore(graph=graph))
    data = next(value for label, value in bar.items() if "Related" in label)

    await act_callback(make_callback_update(bot, data=data), as_context(context))

    assert "Welcome" in bot.last_text


# --- Delete, with confirmation --------------------------------------------


async def test_delete_asks_before_it_acts(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    notes = StubNoteStore()
    context, bar = await open_note(settings, bot, sessions, notes=notes)
    data = next(value for label, value in bar.items() if "Delete" in label)

    await act_callback(make_callback_update(bot, data=data), as_context(context))

    assert "cannot be undone" in bot.last_text
    assert ("delete_note", ("Projects/Roadmap.md",)) not in notes.calls
    assert_markdown_v2_safe(bot.last_text)


async def test_confirming_deletes_and_disarms_the_button(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    """A double tap must not try to delete twice."""
    notes = StubNoteStore()
    context, bar = await open_note(settings, bot, sessions, notes=notes)
    await act_callback(
        make_callback_update(bot, data=next(v for k, v in bar.items() if "Delete" in k)),
        as_context(context),
    )
    confirm = next(data for label, data in buttons(bot.sent[-1]["reply_markup"]) if "Yes" in label)

    await act_callback(make_callback_update(bot, data=confirm), as_context(context))
    await act_callback(make_callback_update(bot, data=confirm), as_context(context))

    assert notes.calls.count(("delete_note", ("Projects/Roadmap.md",))) == 1
    assert "expired" in bot.answered_with[-1]


async def test_cancelling_a_delete_keeps_the_note(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    notes = StubNoteStore()
    context, bar = await open_note(settings, bot, sessions, notes=notes)
    await act_callback(
        make_callback_update(bot, data=next(v for k, v in bar.items() if "Delete" in k)),
        as_context(context),
    )
    cancel = next(
        data for label, data in buttons(bot.sent[-1]["reply_markup"]) if "Cancel" in label
    )

    await act_callback(make_callback_update(bot, data=cancel), as_context(context))

    assert "Not deleted" in bot.edited[-1]["text"]
    assert not any(name == "delete_note" for name, _ in notes.calls)


# --- Multi-step input -----------------------------------------------------


async def test_edit_waits_for_the_next_message(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    notes = StubNoteStore()
    context, bar = await open_note(settings, bot, sessions, notes=notes)
    data = next(value for label, value in bar.items() if "Edit" in label)

    await act_callback(make_callback_update(bot, data=data), as_context(context))

    assert context.user_data[PENDING_KEY]["kind"] == "edit"
    assert "new body" in bot.last_text


async def test_the_next_message_becomes_the_new_body(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    from telegram.ext import ApplicationHandlerStop

    notes = StubNoteStore()
    context, bar = await open_note(settings, bot, sessions, notes=notes)
    await act_callback(
        make_callback_update(bot, data=next(v for k, v in bar.items() if "Edit" in k)),
        as_context(context),
    )

    with pytest.raises(ApplicationHandlerStop):
        await pending_input(make_update(bot, text="rewritten"), as_context(context))

    assert ("update_note", ("Projects/Roadmap.md", "rewritten")) in notes.calls
    assert PENDING_KEY not in context.user_data


async def test_append_timestamps_what_it_adds(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    from telegram.ext import ApplicationHandlerStop

    notes = StubNoteStore()
    context, bar = await open_note(settings, bot, sessions, notes=notes)
    await act_callback(
        make_callback_update(bot, data=next(v for k, v in bar.items() if "Append" in k)),
        as_context(context),
    )

    with pytest.raises(ApplicationHandlerStop):
        await pending_input(make_update(bot, text="a thought"), as_context(context))

    assert ("append_note", ("Projects/Roadmap.md", "a thought", True)) in notes.calls


async def test_a_message_with_nothing_pending_is_left_alone(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    """It must fall through to the search handler, not be swallowed here."""
    context = make_context(settings, bot, sessions)

    await pending_input(make_update(bot, text="docker"), as_context(context))

    assert bot.sent == []


async def test_cancel_clears_a_pending_edit(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    from discoverygram.bot.commands import cancel

    context, bar = await open_note(settings, bot, sessions)
    await act_callback(
        make_callback_update(bot, data=next(v for k, v in bar.items() if "Edit" in k)),
        as_context(context),
    )

    await cancel(make_update(bot), as_context(context))

    assert PENDING_KEY not in context.user_data


# --- Commands -------------------------------------------------------------


async def test_backlinks_command_needs_a_path(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    await backlinks_command(make_update(bot), as_context(make_context(settings, bot, sessions)))

    assert "/backlinks" in bot.last_text


async def test_related_command_needs_a_path(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    await related_command(make_update(bot), as_context(make_context(settings, bot, sessions)))

    assert "/related" in bot.last_text


# --- Folder operations ----------------------------------------------------


async def test_folder_new_creates_it(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    notes = StubNoteStore()
    context = make_context(settings, bot, sessions, notes=notes, args=["new", "Ideas/2026"])

    await folder_command(make_update(bot), as_context(context))

    assert ("create_folder", ("Ideas/2026",)) in notes.calls


async def test_folder_rename_and_move(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    notes = StubNoteStore()

    for action in ("rename", "move"):
        context = make_context(settings, bot, sessions, notes=notes, args=[action, "A", "B"])
        await folder_command(make_update(bot), as_context(context))

    assert ("rename_folder", ("A", "B")) in notes.calls
    assert ("move_folder", ("A", "B")) in notes.calls


async def test_folder_delete_asks_first_and_says_how_much_is_at_stake(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    notes = StubNoteStore()
    context = make_context(settings, bot, sessions, notes=notes, args=["delete", "Projects"])

    await folder_command(make_update(bot), as_context(context))

    assert "3 items" in bot.last_text
    assert "cannot be undone" in bot.last_text
    assert not any(name == "delete_folder" for name, _ in notes.calls)
    assert_markdown_v2_safe(bot.last_text)


async def test_confirming_a_folder_delete_removes_it(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    notes = StubNoteStore()
    context = make_context(settings, bot, sessions, notes=notes, args=["delete", "Projects"])
    await folder_command(make_update(bot), as_context(context))
    confirm = next(data for label, data in buttons(bot.sent[-1]["reply_markup"]) if "Yes" in label)

    await act_callback(make_callback_update(bot, data=confirm), as_context(context))

    assert ("delete_folder", ("Projects",)) in notes.calls


async def test_an_unsupported_folder_operation_explains_itself(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    notes = StubNoteStore(raises=Unsupported("Renaming a folder is not an MCP tool."))
    context = make_context(settings, bot, sessions, notes=notes, args=["rename", "A", "B"])

    await folder_command(make_update(bot), as_context(context))

    assert "MCP" in bot.last_text


async def test_folder_without_a_valid_subcommand_shows_usage(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    context = make_context(settings, bot, sessions, args=["wat"])

    await folder_command(make_update(bot), as_context(context))

    assert "/folder new" in bot.last_text
    assert_markdown_v2_safe(bot.last_text)


def test_the_three_callback_actions_are_distinct() -> None:
    assert len({NAV_ACTION, NOTE_ACTION, ACT_ACTION}) == 3
