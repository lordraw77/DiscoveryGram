"""Creating notes: path resolution, writing, templates and provenance.

Three problems live here, and they are all about *where* a note goes rather
than what is in it.

**Resolution.** A user says `Projects/Research`. Is that a folder to create a
note in, or a note called `Research`? The tree answers: an existing folder
takes a generated filename inside it, anything else becomes the note path
itself. Getting this backwards would scatter notes named after folders.

**Ambiguity.** `research` may match `Projects/Research` and `Archive/Research`.
Guessing is worse than asking, so candidates are returned and the bot offers a
keyboard — but only when there is genuine ambiguity, because a keyboard for one
candidate is friction, not safety.

**Collision.** `POST /api/notes/{path}` is an upsert, so writing to an existing
path *silently destroys* a note. Every create therefore checks first and
suffixes rather than overwrites, unless the caller explicitly asked to replace.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

from discoverygram.adapters.tree import find_node
from discoverygram.config import Settings
from discoverygram.ports.errors import InvalidRequest, NotFound
from discoverygram.ports.model import NoteRef, Template, TemplateRef, TreeNode
from discoverygram.ports.note_store import NoteStore
from discoverygram.util.logging import get_logger
from discoverygram.util.paths import InvalidPath, normalise_folder_path, normalise_note_path

log = get_logger(__name__)

# How many suffixed names to try before giving up on a colliding path. Ten is
# already absurd for one user in one session; the limit exists so a bug cannot
# turn into an unbounded loop against the vault.
MAX_COLLISION_ATTEMPTS = 10

_TITLE_UNSAFE = re.compile(r"[\\/:*?\"<>|#\[\]]+")
_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class Provenance:
    """Where a generated note came from.

    Written into the note so a body someone reads in six months can be traced
    back to the model that produced it. Off when `PROVENANCE_ENABLED=false`:
    some vaults want prose, not machine footers.
    """

    provider: str = ""
    model: str = ""
    source: str = ""
    captured_at: datetime | None = None

    def render(self) -> str:
        """An HTML comment, so it is invisible when the note is rendered.

        A visible footer would end up in search snippets and in every export.
        A comment is still plain text in the file — greppable, and readable by
        anyone who opens the raw note — without being *shown*.
        """
        parts = []
        if self.provider or self.model:
            parts.append(f"generated-by: {self.provider}/{self.model}".rstrip("/"))
        if self.source:
            parts.append(f"source: {self.source}")
        stamp = self.captured_at or datetime.now(UTC)
        parts.append(f"captured: {stamp.strftime('%Y-%m-%d %H:%M UTC')}")
        return "<!-- DiscoveryGram — " + "; ".join(parts) + " -->"


@dataclass(frozen=True, slots=True)
class Resolution:
    """The outcome of turning what a user typed into a real note path."""

    path: str = ""
    #: More than one existing folder matched; the bot must ask.
    candidates: tuple[str, ...] = field(default_factory=tuple)
    #: Folders that would have to be created first.
    missing_parents: tuple[str, ...] = field(default_factory=tuple)
    #: The path was already taken and was suffixed to avoid overwriting.
    renamed_from: str = ""

    @property
    def ambiguous(self) -> bool:
        return len(self.candidates) > 1


class CaptureService:
    """Everything that creates a note."""

    def __init__(self, notes: NoteStore, settings: Settings) -> None:
        self._notes = notes
        self._settings = settings

    # --- Resolution ------------------------------------------------------

    async def resolve(self, target: str, *, title: str = "") -> Resolution:
        """Where a note called `title` should go, given what the user typed.

        Empty target means the inbox. An existing folder means a note inside
        it. Anything else is taken literally as the note's own path.
        """
        tree = await self._notes.get_tree()
        raw = (target or "").strip()

        if not raw:
            folder = self._settings.inbox_path
            return await self._inside(tree, folder, title)

        # An explicit `.md` is a statement: this is the note, not a folder.
        if raw.lower().endswith(".md"):
            return await self._as_note(tree, raw)

        if find_node(tree, _folder_or_empty(raw)) is not None:
            return await self._inside(tree, raw, title)

        matches = _folders_named(tree, raw)
        if len(matches) > 1:
            return Resolution(candidates=tuple(matches))
        if len(matches) == 1:
            return await self._inside(tree, matches[0], title)

        return await self._as_note(tree, raw)

    async def _inside(self, tree: TreeNode, folder: str, title: str) -> Resolution:
        """A note inside a folder, named after its title."""
        try:
            normalised = normalise_folder_path(folder)
        except InvalidPath as exc:
            raise InvalidRequest(str(exc)) from exc

        filename = filename_for(title)
        path = f"{normalised}/{filename}" if normalised else filename
        return await self._settle(tree, path)

    async def _as_note(self, tree: TreeNode, raw: str) -> Resolution:
        try:
            path = normalise_note_path(raw)
        except InvalidPath as exc:
            raise InvalidRequest(str(exc)) from exc
        return await self._settle(tree, path)

    async def _settle(self, tree: TreeNode, path: str) -> Resolution:
        """Apply the collision and missing-parent rules to a candidate path."""
        missing = _missing_parents(tree, path)
        free = await self._free_path(path)
        return Resolution(
            path=free,
            missing_parents=missing,
            renamed_from=path if free != path else "",
        )

    async def _free_path(self, path: str) -> str:
        """`path`, or the first free `path-2`, `path-3`, … that is not taken.

        `create_note` is an upsert, so writing to a taken path destroys what
        was there. Suffixing is the only safe default: a duplicate note is
        recoverable, an overwritten one is not.
        """
        if not await self._exists(path):
            return path

        stem, _, suffix = path.rpartition(".")
        for attempt in range(2, MAX_COLLISION_ATTEMPTS + 2):
            candidate = f"{stem}-{attempt}.{suffix}"
            if not await self._exists(candidate):
                return candidate
        raise InvalidRequest(
            f"{path} and {MAX_COLLISION_ATTEMPTS} variations of it all exist. "
            f"Choose a different name."
        )

    async def _exists(self, path: str) -> bool:
        try:
            await self._notes.get_note(path, include_backlinks=False)
        except NotFound:
            return False
        return True

    # --- Writing ---------------------------------------------------------

    async def create(
        self,
        path: str,
        body: str,
        *,
        title: str = "",
        tags: tuple[str, ...] = (),
        provenance: Provenance | None = None,
    ) -> NoteRef:
        """Write a note, creating its parent folders when configured to.

        The path is used exactly as given: resolution already happened, and
        re-resolving here would move a note the user confirmed in the preview.
        """
        note_path = normalise_note_path(path)
        await self._ensure_parents(note_path)

        content = compose(body, title=title, tags=tags, provenance=provenance)
        ref = await self._notes.create_note(note_path, content)
        log.info(
            "note_created",
            path=note_path,
            size=len(content),
            tags=len(tags),
            generated=provenance is not None,
        )
        return ref

    async def quick(self, text: str, *, provenance: Provenance | None = None) -> NoteRef:
        """Capture into `INBOX_PATH` with no decisions to make.

        Appends to a single dated note rather than creating one per message:
        twenty thoughts in an afternoon should be one page to read back, not
        twenty files to tidy up.
        """
        body = text.strip()
        if not body:
            raise InvalidRequest("There is nothing to capture.")

        path = self.inbox_path_for(datetime.now(UTC))
        await self._ensure_parents(path)

        stamp = datetime.now(UTC).strftime("%H:%M")
        entry = f"- **{stamp}** — {body}"

        if await self._exists(path):
            await self._notes.append_note(path, entry)
            log.info("quick_capture_appended", path=path)
        else:
            heading = f"# {datetime.now(UTC).strftime('%Y-%m-%d')}\n\n"
            footer = f"\n\n{provenance.render()}" if provenance else ""
            await self._notes.create_note(path, f"{heading}{entry}\n{footer}")
            log.info("quick_capture_created", path=path)

        return NoteRef.from_path(path)

    def inbox_path_for(self, moment: datetime) -> str:
        """One inbox note per day, under `INBOX_PATH`."""
        folder = self._settings.inbox_path.strip("/")
        name = f"{moment.strftime('%Y-%m-%d')}.md"
        return normalise_note_path(f"{folder}/{name}" if folder else name)

    async def _ensure_parents(self, note_path: str) -> None:
        """Create the folders a note needs, if the operator allows it.

        NoteDiscovery creates intermediate folders itself on write, so this is
        belt and braces — but it is also the only place `AUTO_CREATE_PARENTS`
        can be *honoured*: with it off, a note aimed at a folder that does not
        exist is refused rather than quietly inventing a tree.
        """
        folder = note_path.rpartition("/")[0]
        if not folder:
            return

        tree = await self._notes.get_tree()
        if find_node(tree, folder) is not None:
            return

        if not self._settings.auto_create_parents:
            raise InvalidRequest(
                f"The folder {folder} does not exist, and AUTO_CREATE_PARENTS is off. "
                f"Create it with /folder new {folder} first."
            )

        await self._notes.create_folder(folder)
        log.info("folder_auto_created", path=folder)

    # --- Templates -------------------------------------------------------

    async def templates(self) -> list[TemplateRef]:
        return await self._notes.list_templates()

    async def template(self, name: str) -> Template:
        return await self._notes.get_template(name)

    async def from_template(self, template_name: str, path: str) -> NoteRef:
        """Create a note from a template, letting NoteDiscovery expand it.

        The expansion happens server-side (`create_note_from_template`), so the
        template's date and title placeholders behave exactly as they do in the
        NoteDiscovery editor rather than being re-implemented here and drifting.
        """
        note_path = normalise_note_path(path)
        await self._ensure_parents(note_path)
        ref = await self._notes.create_note_from_template(template_name, note_path)
        log.info("note_created_from_template", path=note_path, template=template_name)
        return ref


# --- Composition ---------------------------------------------------------


def compose(
    body: str,
    *,
    title: str = "",
    tags: tuple[str, ...] = (),
    provenance: Provenance | None = None,
) -> str:
    """Assemble a note body from its parts.

    Order is deliberate: `# Title`, body, tags, then the provenance comment
    last. Tags sit on their own trailing line so a later `Append` lands after
    them rather than between a tag and the text it belonged to.
    """
    sections: list[str] = []
    if title.strip():
        sections.append(f"# {title.strip()}")
    if body.strip():
        sections.append(body.strip())
    if tags:
        sections.append(" ".join(f"#{tag.lstrip('#')}" for tag in tags))
    if provenance is not None:
        sections.append(provenance.render())
    return "\n\n".join(sections) + "\n"


def filename_for(title: str) -> str:
    """A note filename from a title, or a timestamp when there is no title.

    Characters NoteDiscovery's own sanitiser rejects are removed rather than
    substituted: a title is a human artefact and `Q1: costs` reads better as
    `Q1 costs` than as `Q1_costs`.
    """
    cleaned = _TITLE_UNSAFE.sub(" ", title or "")
    cleaned = _WHITESPACE.sub(" ", cleaned).strip(" .-")
    if not cleaned:
        cleaned = f"Note {datetime.now(UTC).strftime('%Y-%m-%d %H%M%S')}"
    # Long enough for any real title, short enough to survive a filesystem.
    return f"{cleaned[:80].strip()}.md"


def _folder_or_empty(raw: str) -> str:
    try:
        return normalise_folder_path(raw)
    except InvalidPath:
        return "\x00"  # cannot match any folder, and never reaches an adapter


def _folders_named(root: TreeNode, name: str) -> list[str]:
    """Every folder whose own name matches, case-insensitively.

    Matching the *name* rather than the path is what lets a user say `research`
    and mean `Projects/Research` — and what makes two matches genuinely
    ambiguous rather than one being obviously right.
    """
    wanted = name.strip("/").casefold()
    found: list[str] = []

    def walk(node: TreeNode) -> None:
        for child in node.folders:
            if child.name.casefold() == wanted:
                found.append(child.path)
            walk(child)

    walk(root)
    return sorted(found)


def _missing_parents(root: TreeNode, note_path: str) -> tuple[str, ...]:
    """Folders on the way to `note_path` that do not exist yet."""
    folder = note_path.rpartition("/")[0]
    if not folder or find_node(root, folder) is not None:
        return ()

    missing: list[str] = []
    accumulated = ""
    for segment in folder.split("/"):
        accumulated = f"{accumulated}/{segment}" if accumulated else segment
        if find_node(root, accumulated) is None:
            missing.append(accumulated)
    return tuple(missing)


__all__ = [
    "MAX_COLLISION_ATTEMPTS",
    "CaptureService",
    "Provenance",
    "Resolution",
    "compose",
    "filename_for",
]
