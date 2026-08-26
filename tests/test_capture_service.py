"""Path resolution and note creation.

The rule that matters most here is the collision rule: `POST /api/notes/{path}`
is an **upsert**, so a create that does not check first silently destroys
whatever was at that path. Several tests exist only to hold that line.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from discoverygram.app.capture import (
    MAX_COLLISION_ATTEMPTS,
    CaptureService,
    Provenance,
    compose,
    filename_for,
)
from discoverygram.config import Settings
from discoverygram.ports.errors import InvalidRequest, NotFound
from discoverygram.ports.model import NoteRef, Template, TemplateRef, TreeNode


class StubStore:
    """A vault as a set of note paths and a folder tree."""

    def __init__(self, *, notes: set[str] | None = None, folders: tuple[str, ...] = ()) -> None:
        self.notes = notes or set()
        self.folders = folders
        self.created: list[tuple[str, str]] = []
        self.appended: list[tuple[str, str]] = []
        self.created_folders: list[str] = []
        self.templated: list[tuple[str, str]] = []

    async def get_tree(self, *, refresh: bool = False) -> TreeNode:
        return _tree(self.folders)

    async def get_note(self, path: str, *, include_backlinks: bool = True):  # type: ignore[no-untyped-def]
        if path not in self.notes:
            raise NotFound(f"no note at {path}")
        return NoteRef.from_path(path)

    async def create_note(self, path: str, content: str) -> NoteRef:
        self.created.append((path, content))
        self.notes.add(path)
        return NoteRef.from_path(path)

    async def append_note(self, path: str, content: str, *, add_timestamp: bool = False) -> None:
        self.appended.append((path, content))

    async def create_folder(self, path: str) -> str:
        self.created_folders.append(path)
        return path

    async def list_templates(self) -> list[TemplateRef]:
        return [TemplateRef(name="Meeting")]

    async def get_template(self, name: str) -> Template:
        return Template(name=name, content="# {{title}}")

    async def create_note_from_template(self, template_name: str, note_path: str) -> NoteRef:
        self.templated.append((template_name, note_path))
        return NoteRef.from_path(note_path)


def _tree(folders: tuple[str, ...]) -> TreeNode:
    """Build a folder-only tree from a list of paths."""

    def node(prefix: str, name: str) -> TreeNode:
        path = f"{prefix}/{name}" if prefix else name
        children = tuple(
            node(path, child)
            for child in sorted(
                {
                    folder[len(path) + 1 :].split("/")[0]
                    for folder in folders
                    if folder.startswith(f"{path}/")
                }
            )
        )
        return TreeNode(path=path, name=name, folders=children)

    roots = sorted({folder.split("/")[0] for folder in folders})
    return TreeNode(path="", name="", folders=tuple(node("", root) for root in roots))


def service(settings: Settings, store: StubStore) -> CaptureService:
    return CaptureService(store, settings)  # type: ignore[arg-type]


# --- Resolution ----------------------------------------------------------


async def test_no_target_lands_in_the_inbox(settings: Settings) -> None:
    store = StubStore()

    resolution = await service(settings, store).resolve("", title="Budget notes")

    assert resolution.path == f"{settings.inbox_path}/Budget notes.md"


async def test_an_existing_folder_takes_a_note_inside_it(settings: Settings) -> None:
    store = StubStore(folders=("Projects/Research",))

    resolution = await service(settings, store).resolve("Projects/Research", title="Findings")

    assert resolution.path == "Projects/Research/Findings.md"


async def test_a_path_that_is_not_a_folder_becomes_the_note_itself(settings: Settings) -> None:
    store = StubStore(folders=("Projects",))

    resolution = await service(settings, store).resolve("Projects/Findings", title="ignored")

    assert resolution.path == "Projects/Findings.md"


async def test_an_explicit_md_suffix_is_taken_literally(settings: Settings) -> None:
    """Even when a folder of that name exists: `.md` is a statement."""
    store = StubStore(folders=("Research",))

    resolution = await service(settings, store).resolve("Research.md", title="Findings")

    assert resolution.path == "Research.md"


async def test_a_folder_name_is_matched_anywhere_in_the_tree(settings: Settings) -> None:
    """A user says "research" and means the folder they have, wherever it is."""
    store = StubStore(folders=("Projects/Research",))

    resolution = await service(settings, store).resolve("research", title="Findings")

    assert resolution.path == "Projects/Research/Findings.md"


async def test_two_folders_of_the_same_name_are_ambiguous(settings: Settings) -> None:
    store = StubStore(folders=("Projects/Research", "Archive/Research"))

    resolution = await service(settings, store).resolve("research", title="Findings")

    assert resolution.ambiguous is True
    assert resolution.candidates == ("Archive/Research", "Projects/Research")
    assert resolution.path == ""


async def test_one_match_is_not_ambiguous(settings: Settings) -> None:
    """A keyboard offering a single choice is friction, not safety."""
    store = StubStore(folders=("Projects/Research",))

    resolution = await service(settings, store).resolve("research")

    assert resolution.ambiguous is False


async def test_missing_parents_are_reported(settings: Settings) -> None:
    store = StubStore()

    resolution = await service(settings, store).resolve("A/B/C/note.md")

    assert resolution.missing_parents == ("A", "A/B", "A/B/C")


async def test_a_traversal_attempt_is_refused(settings: Settings) -> None:
    store = StubStore()

    with pytest.raises(InvalidRequest):
        await service(settings, store).resolve("../../etc/passwd.md")


# --- The collision rule ---------------------------------------------------


async def test_an_existing_path_is_suffixed_rather_than_overwritten(settings: Settings) -> None:
    """`create_note` is an upsert: resolving to a taken path destroys a note."""
    store = StubStore(notes={"Inbox/Budget notes.md"})

    resolution = await service(settings, store).resolve("", title="Budget notes")

    assert resolution.path == "Inbox/Budget notes-2.md"
    assert resolution.renamed_from == "Inbox/Budget notes.md"


async def test_the_suffix_keeps_counting_past_the_first_collision(settings: Settings) -> None:
    store = StubStore(notes={"Inbox/Note.md", "Inbox/Note-2.md", "Inbox/Note-3.md"})

    resolution = await service(settings, store).resolve("", title="Note")

    assert resolution.path == "Inbox/Note-4.md"


async def test_a_free_path_is_not_renamed(settings: Settings) -> None:
    store = StubStore()

    resolution = await service(settings, store).resolve("", title="Fresh")

    assert resolution.renamed_from == ""


async def test_an_absurd_number_of_collisions_is_refused_rather_than_looped(
    settings: Settings,
) -> None:
    taken = {"Inbox/Note.md"} | {
        f"Inbox/Note-{index}.md" for index in range(2, MAX_COLLISION_ATTEMPTS + 3)
    }
    store = StubStore(notes=taken)

    with pytest.raises(InvalidRequest, match="Choose a different name"):
        await service(settings, store).resolve("", title="Note")


# --- Writing --------------------------------------------------------------


async def test_create_writes_the_composed_note(settings: Settings) -> None:
    store = StubStore(folders=("Inbox",))

    await service(settings, store).create(
        "Inbox/Note.md", "body text", title="A title", tags=("one", "two")
    )

    path, content = store.created[0]
    assert path == "Inbox/Note.md"
    assert content.startswith("# A title")
    assert "body text" in content
    assert "#one #two" in content


async def test_create_makes_the_parent_folders_when_allowed(settings: Settings) -> None:
    store = StubStore()

    await service(settings, store).create("New/Deep/Note.md", "body")

    assert store.created_folders == ["New/Deep"]


async def test_create_refuses_a_missing_folder_when_auto_create_is_off(
    settings: Settings,
) -> None:
    """With the flag off, the bot must not quietly invent a folder tree."""
    strict = settings.model_copy(update={"auto_create_parents": False})
    store = StubStore()

    with pytest.raises(InvalidRequest, match="AUTO_CREATE_PARENTS"):
        await service(strict, store).create("New/Deep/Note.md", "body")

    assert store.created == []


async def test_create_does_not_re_resolve_the_path_it_was_given(settings: Settings) -> None:
    """The user confirmed a path in the preview; saving must use exactly that."""
    store = StubStore(notes={"Inbox/Note.md"}, folders=("Inbox",))

    ref = await service(settings, store).create("Inbox/Note.md", "replacement")

    assert ref.path == "Inbox/Note.md"


# --- Quick capture --------------------------------------------------------


async def test_quick_capture_creates_todays_note(settings: Settings) -> None:
    store = StubStore(folders=("Inbox",))

    ref = await service(settings, store).quick("buy milk")

    assert ref.path == f"{settings.inbox_path}/{datetime.now(UTC).strftime('%Y-%m-%d')}.md"
    assert "buy milk" in store.created[0][1]


async def test_a_second_quick_capture_appends_rather_than_creating(settings: Settings) -> None:
    """Twenty thoughts in an afternoon should be one page, not twenty files."""
    path = f"{settings.inbox_path}/{datetime.now(UTC).strftime('%Y-%m-%d')}.md"
    store = StubStore(notes={path}, folders=(settings.inbox_path,))

    await service(settings, store).quick("second thought")

    assert store.created == []
    assert "second thought" in store.appended[0][1]


async def test_quick_capture_refuses_an_empty_message(settings: Settings) -> None:
    with pytest.raises(InvalidRequest):
        await service(settings, StubStore()).quick("   ")


# --- Templates ------------------------------------------------------------


async def test_a_template_note_is_expanded_server_side(settings: Settings) -> None:
    """NoteDiscovery owns the placeholders; re-implementing them would drift."""
    store = StubStore(folders=("Meetings",))

    ref = await service(settings, store).from_template("Meeting", "Meetings/Monday")

    assert store.templated == [("Meeting", "Meetings/Monday.md")]
    assert ref.path == "Meetings/Monday.md"


async def test_a_template_note_also_gets_its_parent_folders(settings: Settings) -> None:
    store = StubStore()

    await service(settings, store).from_template("Meeting", "New/Monday")

    assert store.created_folders == ["New"]


# --- Composition ----------------------------------------------------------


def test_compose_orders_the_parts_predictably() -> None:
    content = compose(
        "the body",
        title="The title",
        tags=("a", "b"),
        provenance=Provenance(
            provider="groq", model="llama", captured_at=datetime(2026, 1, 2, tzinfo=UTC)
        ),
    )

    assert content.index("# The title") < content.index("the body")
    assert content.index("the body") < content.index("#a #b")
    assert content.index("#a #b") < content.index("generated-by")


def test_provenance_is_a_comment_so_it_never_shows_in_a_snippet() -> None:
    """A visible footer would end up in every search result and every export."""
    rendered = Provenance(provider="groq", model="llama-3.3").render()

    assert rendered.startswith("<!--")
    assert rendered.endswith("-->")
    assert "groq/llama-3.3" in rendered


def test_a_note_with_no_title_or_tags_is_just_its_body() -> None:
    assert compose("only this") == "only this\n"


def test_tags_are_written_with_exactly_one_hash() -> None:
    assert "#already" in compose("body", tags=("#already",))
    assert "##" not in compose("body", tags=("#already",))


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Q1 costs", "Q1 costs.md"),
        ("Q1: costs / draft", "Q1 costs draft.md"),
        ("  spaced  out  ", "spaced out.md"),
        ("a" * 200, "a" * 80 + ".md"),
    ],
)
def test_a_filename_is_derived_from_the_title(title: str, expected: str) -> None:
    assert filename_for(title) == expected


def test_a_title_of_only_punctuation_falls_back_to_a_timestamp() -> None:
    name = filename_for("///:::")

    assert name.startswith("Note ")
    assert name.endswith(".md")
