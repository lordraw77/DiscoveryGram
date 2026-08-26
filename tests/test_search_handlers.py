"""Search commands and their pagination.

The Definition of Done asks for pagination that survives 20+ page turns without
state leaks, so that is asserted directly against the session store.
"""

from __future__ import annotations

import pytest

from discoverygram.adapters.session import MemorySessionStore
from discoverygram.app.probe import InstanceState
from discoverygram.app.search import ResultSet
from discoverygram.bot.deps import DEPS_KEY, BotDeps
from discoverygram.bot.search import COMMANDS as SEARCH_COMMANDS
from discoverygram.bot.search import (
    PAGE_ACTION,
    page_callback,
    recent_command,
    search_command,
    tag_command,
    text_message,
)
from discoverygram.bot.tokens import CallbackTokens
from discoverygram.config import Settings
from discoverygram.ports.model import (
    InstanceConfig,
    Note,
    NoteRef,
    SearchHit,
    SearchMatch,
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


def hit(index: int) -> SearchHit:
    return SearchHit(
        ref=NoteRef(path=f"Projects/n{index}.md", title=f"Note {index}", folder="Projects"),
        matches=(SearchMatch(1, f"mentions docker in note {index}"),),
    )


class StubNoteStore:
    def __init__(
        self,
        *,
        hits: list[SearchHit] | None = None,
        refs: list[NoteRef] | None = None,
        tags: dict[str, int] | None = None,
    ) -> None:
        self.hits = hits or []
        self.refs = refs or []
        self.tags_map = tags or {}

    async def search(
        self, query: str, *, limit: int | None = None, offset: int = 0
    ) -> list[SearchHit]:
        return list(self.hits)

    async def search_literal(self, query: str, *, limit: int | None = None) -> list[SearchHit]:
        return list(self.hits)

    async def get_notes_by_tag(
        self, tag: str, *, limit: int | None = None, offset: int = 0
    ) -> list[NoteRef]:
        return list(self.refs)

    async def recent_notes(self, *, days: int = 7, limit: int = 20) -> list[NoteRef]:
        return list(self.refs)

    async def list_tags(self) -> dict[str, int]:
        return dict(self.tags_map)

    async def get_note(self, path: str, *, include_backlinks: bool = True) -> Note:
        return Note(ref=NoteRef.from_path(path), content="body", lines=1)

    async def get_tree(self, *, refresh: bool = False) -> TreeNode:
        return TreeNode(path="", name="")


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
    search_enabled: bool = True,
    args: list[str] | None = None,
) -> FakeContext:
    deps = BotDeps(
        settings=settings,
        notes=notes or StubNoteStore(),  # type: ignore[arg-type]
        sessions=sessions,
        tokens=CallbackTokens(sessions, ttl_s=settings.session_ttl_s),
        instance=InstanceState(
            config=InstanceConfig(version="0.31.3", search_enabled=search_enabled),
            healthy=True,
        ),
    )
    context = FakeContext(bot, {DEPS_KEY: deps})
    context.args = args or []
    return context


# --- Commands -------------------------------------------------------------


async def test_search_sends_the_first_page_with_buttons(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    notes = StubNoteStore(hits=[hit(index) for index in range(8)])
    context = make_context(settings, bot, sessions, notes=notes, args=["docker"])

    await search_command(make_update(bot), as_context(context))

    assert "8 results" in bot.last_text
    assert bot.sent[-1]["reply_markup"] is not None
    assert_markdown_v2_safe(bot.last_text)


async def test_a_single_page_of_results_gets_no_pagination_row(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    """One open button, no arrows — there is nowhere to page to."""
    notes = StubNoteStore(hits=[hit(0)])
    context = make_context(settings, bot, sessions, notes=notes, args=["docker"])

    await search_command(make_update(bot), as_context(context))

    markup = bot.sent[-1]["reply_markup"]
    labels = [button.text for row in markup.inline_keyboard for button in row]
    assert labels == ["1. Note 0"]


async def test_every_hit_gets_an_open_button(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    notes = StubNoteStore(hits=[hit(index) for index in range(3)])
    context = make_context(settings, bot, sessions, notes=notes, args=["docker"])

    await search_command(make_update(bot), as_context(context))

    markup = bot.sent[-1]["reply_markup"]
    labels = [button.text for row in markup.inline_keyboard for button in row]
    assert labels == ["1. Note 0", "2. Note 1", "3. Note 2"]


async def test_open_buttons_reuse_the_result_set_token(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    """A token per hit would mean five new session entries on every page turn."""
    notes = StubNoteStore(hits=[hit(index) for index in range(12)])
    context = make_context(settings, bot, sessions, notes=notes, args=["docker"])

    await search_command(make_update(bot), as_context(context))

    tokens = {
        CallbackTokens.split(button.callback_data)[1]
        for row in bot.sent[-1]["reply_markup"].inline_keyboard
        for button in row
        if button.callback_data and not button.callback_data.startswith("noop")
    }
    assert len(tokens) == 1
    assert len(sessions) == 1


async def test_tapping_a_hit_opens_that_note(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    notes = StubNoteStore(hits=[hit(index) for index in range(3)])
    context = make_context(settings, bot, sessions, notes=notes, args=["docker"])
    await search_command(make_update(bot), as_context(context))
    open_button = bot.sent[-1]["reply_markup"].inline_keyboard[1][0]

    await page_callback(
        make_callback_update(bot, data=open_button.callback_data), as_context(context)
    )

    assert r"Projects/n1\.md" in bot.last_text


async def test_search_without_a_query_shows_usage(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    context = make_context(settings, bot, sessions)

    await search_command(make_update(bot), as_context(context))

    assert "/search" in bot.last_text
    assert_markdown_v2_safe(bot.last_text)


async def test_a_multi_word_query_is_kept_whole(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    """`/search machine learning` is one query, not two."""
    captured: list[str] = []

    class Recording(StubNoteStore):
        async def search(
            self, query: str, *, limit: int | None = None, offset: int = 0
        ) -> list[SearchHit]:
            captured.append(query)
            return []

    context = make_context(settings, bot, sessions, notes=Recording(), args=["machine", "learning"])

    await search_command(make_update(bot), as_context(context))

    assert captured == ["machine learning"]


async def test_a_search_disabled_instance_explains_rather_than_erroring(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    context = make_context(settings, bot, sessions, search_enabled=False, args=["docker"])

    await search_command(make_update(bot), as_context(context))

    assert "disabled" in bot.last_text
    assert_markdown_v2_safe(bot.last_text)


async def test_a_too_short_query_says_how_short(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    context = make_context(settings, bot, sessions, args=["d"])

    await search_command(make_update(bot), as_context(context))

    assert "too short" in bot.last_text


async def test_tag_without_an_argument_lists_the_tags(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    notes = StubNoteStore(tags={"planning": 9, "docker": 2})
    context = make_context(settings, bot, sessions, notes=notes)

    await tag_command(make_update(bot), as_context(context))

    assert "planning" in bot.last_text
    assert "*2* tags" in bot.last_text


async def test_tag_with_an_argument_lists_its_notes(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    notes = StubNoteStore(refs=[NoteRef.from_path("Projects/Ideas.md")])
    context = make_context(settings, bot, sessions, notes=notes, args=["planning"])

    await tag_command(make_update(bot), as_context(context))

    assert "tagged" in bot.last_text
    assert "Ideas" in bot.last_text


async def test_recent_accepts_a_day_count(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    captured: list[int] = []

    class Recording(StubNoteStore):
        async def recent_notes(self, *, days: int = 7, limit: int = 20) -> list[NoteRef]:
            captured.append(days)
            return []

    context = make_context(settings, bot, sessions, notes=Recording(), args=["30"])

    await recent_command(make_update(bot), as_context(context))

    assert captured == [30]


async def test_recent_rejects_nonsense_instead_of_guessing(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    context = make_context(settings, bot, sessions, args=["last-week"])

    await recent_command(make_update(bot), as_context(context))

    assert "/recent" in bot.last_text


async def test_a_plain_message_runs_a_search(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    notes = StubNoteStore(hits=[hit(0)])
    context = make_context(settings, bot, sessions, notes=notes)

    await text_message(make_update(bot, text="docker"), as_context(context))

    assert "1 result" in bot.last_text


async def test_quick_capture_is_refused_rather_than_silently_searching(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    """A message meant as a note must not quietly become a search."""
    quick = settings.model_copy(update={"default_text_action": "quick"})
    context = make_context(quick, bot, sessions)

    await text_message(make_update(bot, text="buy milk"), as_context(context))

    assert "not available yet" in bot.last_text
    assert "1 result" not in bot.last_text


# --- Pagination -----------------------------------------------------------


async def issue_results(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore, count: int
) -> tuple[FakeContext, str]:
    notes = StubNoteStore(hits=[hit(index) for index in range(count)])
    context = make_context(settings, bot, sessions, notes=notes, args=["docker"])
    await search_command(make_update(bot), as_context(context))

    markup = bot.sent[-1]["reply_markup"]
    forward = next(
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.text == "▶"
    )
    return context, forward


async def test_turning_a_page_edits_the_message_in_place(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    context, forward = await issue_results(settings, bot, sessions, 12)

    update = make_callback_update(bot, data=forward)
    await page_callback(update, as_context(context))

    assert update.callback_query is not None
    edited = bot.edited[-1]
    assert "*6\\.*" in edited["text"]


async def test_twenty_page_turns_leak_no_session_state(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    """The Definition of Done, asserted against the store rather than assumed."""
    context, forward = await issue_results(settings, bot, sessions, 200)
    before = len(sessions)
    assert before == 1

    data = forward
    for _ in range(25):
        update = make_callback_update(bot, data=data)
        await page_callback(update, as_context(context))
        markup = bot.edited[-1]["reply_markup"]
        forward_buttons = [
            button.callback_data
            for row in markup.inline_keyboard
            for button in row
            if button.text == "▶"
        ]
        if not forward_buttons:
            break
        data = forward_buttons[0]

    assert len(sessions) == before == 1


async def test_a_page_turn_refreshes_the_result_set_lifetime(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    """Browsing must not expire because page one was issued an hour ago."""
    context, forward = await issue_results(settings, bot, sessions, 12)
    _, token, _ = CallbackTokens.split(forward)
    key = f"cb:{token}"
    stored_before = await sessions.get(key)

    await page_callback(make_callback_update(bot, data=forward), as_context(context))

    assert stored_before is not None
    assert await sessions.get(key) is not None


async def test_an_expired_result_set_says_what_expired(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    """ "That button expired" is useless; "those results expired" is not."""
    context, forward = await issue_results(settings, bot, sessions, 12)
    _, token, _ = CallbackTokens.split(forward)
    await sessions.delete(f"cb:{token}")

    await page_callback(make_callback_update(bot, data=forward), as_context(context))

    assert "results have expired" in bot.answered_with[-1]
    assert bot.edited == []


async def test_a_malformed_page_number_is_clamped_not_a_crash(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    """Callback data is whatever arrives at the endpoint."""
    context, forward = await issue_results(settings, bot, sessions, 12)
    base = forward.rsplit(":", 1)[0]

    for junk in (f"{base}:abc", f"{base}:-5", f"{base}:9999", base):
        await page_callback(make_callback_update(bot, data=junk), as_context(context))

    assert len(bot.edited) == 4


async def test_the_stored_payload_holds_the_whole_result_set(
    settings: Settings, bot: FakeBot, sessions: MemorySessionStore
) -> None:
    """Page turns must not re-run the query against a vault that may have changed."""
    _, forward = await issue_results(settings, bot, sessions, 12)
    _, token, _ = CallbackTokens.split(forward)

    payload = await sessions.get(f"cb:{token}")
    assert payload is not None
    assert len(ResultSet.from_payload(payload).hits) == 12


def test_the_page_action_is_short_enough_to_leave_room_for_a_token() -> None:
    assert len(f"{PAGE_ACTION}:0011223344ff:999".encode()) <= 64


def test_all_four_modes_are_registered_as_commands() -> None:
    assert set(SEARCH_COMMANDS) == {"search", "find", "tag", "recent"}
