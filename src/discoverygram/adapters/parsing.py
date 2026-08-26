"""Turn NoteDiscovery's JSON into the domain model.

Kept separate from the transport so the same normalisation serves both the REST
adapter and the MCP adapter — MCP tools return the very same payloads, just
wrapped in a tool result.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from discoverygram.adapters.ranking import parse_matches
from discoverygram.ports.model import (
    Backlink,
    Graph,
    GraphEdge,
    GraphNode,
    InstanceConfig,
    MediaUpload,
    Note,
    NoteListing,
    NoteRef,
    SearchHit,
    ShareLink,
    Template,
    TemplateRef,
    VaultStats,
)
from discoverygram.util.paths import note_title, parent_folder

JsonDict = Mapping[str, Any]


def parse_timestamp(value: Any) -> datetime | None:
    """ISO-8601 from NoteDiscovery, or a POSIX timestamp from an older field."""
    if isinstance(value, int | float) and not isinstance(value, bool):
        return datetime.fromtimestamp(float(value), tz=UTC)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _as_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int | float):
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value)
    return default


def _as_tags(value: Any) -> tuple[str, ...]:
    if isinstance(value, Mapping):
        return tuple(str(tag) for tag in value)
    if isinstance(value, list | tuple):
        return tuple(str(tag) for tag in value if str(tag))
    return ()


def parse_note_ref(payload: JsonDict) -> NoteRef:
    """One record from `/api/notes`, `/api/tags/{tag}` or a search result row.

    `name` is the file **stem** in every producer we have seen, but a path is
    always present, so the title is derived from the path when `name` is absent.
    """
    path = str(payload.get("path", "")).strip()
    if not path:
        raise ValueError("note record without a path")

    raw_name = payload.get("name")
    title = str(raw_name) if raw_name else note_title(path)
    # A producer that hands back `Ideas.md` rather than `Ideas` should not leak
    # the extension into the UI.
    if title.endswith(".md"):
        title = title[: -len(".md")]

    folder = payload.get("folder")
    return NoteRef(
        path=path,
        title=title,
        folder=str(folder) if folder is not None else parent_folder(path),
        modified=parse_timestamp(payload.get("modified")),
        size=_as_int(payload.get("size")),
        tags=_as_tags(payload.get("tags")),
    )


def parse_backlink(payload: JsonDict) -> Backlink:
    """One entry of a note's `backlinks` array.

    Its `references` are `{line_number, context, type}` records whose context is
    already plain text — unlike search snippets, which arrive as HTML.
    """
    path = str(payload.get("path", ""))
    raw_refs = payload.get("references", ())
    references = (
        parse_matches(raw_refs, html_escaped=False) if isinstance(raw_refs, list | tuple) else ()
    )
    name = str(payload.get("name") or note_title(path))
    return Backlink(path=path, title=name.removesuffix(".md"), references=references)


def parse_note(payload: JsonDict, *, path: str | None = None) -> Note:
    """`GET /api/notes/{path}` — `{path, content, metadata, backlinks}`."""
    resolved = str(payload.get("path") or path or "")
    metadata = payload.get("metadata")
    meta: JsonDict = metadata if isinstance(metadata, Mapping) else {}

    ref = NoteRef(
        path=resolved,
        title=note_title(resolved),
        folder=parent_folder(resolved),
        modified=parse_timestamp(meta.get("modified")),
        size=_as_int(meta.get("size")),
        tags=_as_tags(payload.get("tags")),
    )
    raw_backlinks = payload.get("backlinks")
    backlinks = (
        tuple(parse_backlink(item) for item in raw_backlinks if isinstance(item, Mapping))
        if isinstance(raw_backlinks, list | tuple)
        else ()
    )
    return Note(
        ref=ref,
        content=str(payload.get("content", "")),
        created=parse_timestamp(meta.get("created")),
        modified=ref.modified,
        lines=_as_int(meta.get("lines")),
        backlinks=backlinks,
    )


def parse_note_listing(payload: JsonDict) -> NoteListing:
    """`GET /api/notes` — `{notes, folders, pagination?}`.

    Media files share the endpoint when `include_media` is on server-side, so
    records whose `type` is not `note` are dropped here.
    """
    raw_notes = payload.get("notes", [])
    notes: list[NoteRef] = []
    if isinstance(raw_notes, list | tuple):
        for item in raw_notes:
            if not isinstance(item, Mapping):
                continue
            if str(item.get("type", "note")) != "note":
                continue
            try:
                notes.append(parse_note_ref(item))
            except ValueError:
                continue

    raw_folders = payload.get("folders", [])
    folders = (
        tuple(str(folder) for folder in raw_folders if str(folder))
        if isinstance(raw_folders, list | tuple)
        else ()
    )

    pagination = payload.get("pagination")
    total = (
        _as_int(pagination.get("total"), len(notes)) if isinstance(pagination, Mapping) else None
    )
    return NoteListing(notes=tuple(notes), folders=folders, total=total)


def parse_search_results(payload: JsonDict) -> list[SearchHit]:
    """`GET /api/search` — `{results, query, pagination?}`, unranked."""
    raw_results = payload.get("results", [])
    if not isinstance(raw_results, list | tuple):
        return []

    hits: list[SearchHit] = []
    for item in raw_results:
        if not isinstance(item, Mapping):
            continue
        try:
            ref = parse_note_ref(item)
        except ValueError:
            continue
        raw_matches = item.get("matches", [])
        matches = parse_matches(raw_matches) if isinstance(raw_matches, list | tuple) else ()
        hits.append(SearchHit(ref=ref, matches=matches))
    return hits


def parse_tags(payload: JsonDict) -> dict[str, int]:
    """`GET /api/tags` — `{"tags": {name: count}}`."""
    raw = payload.get("tags", {})
    if isinstance(raw, Mapping):
        return {str(name): _as_int(count) for name, count in raw.items()}
    if isinstance(raw, list | tuple):
        # Defensive: an older shape returned a bare list of tag names.
        return {str(name): 0 for name in raw if str(name)}
    return {}


def parse_tag_notes(payload: JsonDict) -> list[NoteRef]:
    raw = payload.get("notes", [])
    if not isinstance(raw, list | tuple):
        return []
    refs: list[NoteRef] = []
    for item in raw:
        if isinstance(item, Mapping):
            try:
                refs.append(parse_note_ref(item))
            except ValueError:
                continue
    return refs


def parse_graph(payload: JsonDict) -> Graph:
    raw_nodes = payload.get("nodes", [])
    raw_edges = payload.get("edges", [])
    nodes = (
        tuple(
            GraphNode(id=str(node.get("id", "")), label=str(node.get("label", "")))
            for node in raw_nodes
            if isinstance(node, Mapping) and node.get("id")
        )
        if isinstance(raw_nodes, list | tuple)
        else ()
    )
    edges = (
        tuple(
            GraphEdge(
                source=str(edge.get("source", "")),
                target=str(edge.get("target", "")),
                type=str(edge.get("type", "")),
            )
            for edge in raw_edges
            if isinstance(edge, Mapping) and edge.get("source") and edge.get("target")
        )
        if isinstance(raw_edges, list | tuple)
        else ()
    )
    return Graph(nodes=nodes, edges=edges)


def parse_templates(payload: JsonDict) -> list[TemplateRef]:
    raw = payload.get("templates", [])
    if not isinstance(raw, list | tuple):
        return []
    return [
        TemplateRef(
            name=str(item.get("name", "")),
            path=str(item.get("path", "")),
            modified=parse_timestamp(item.get("modified")),
        )
        for item in raw
        if isinstance(item, Mapping) and item.get("name")
    ]


def parse_template(payload: JsonDict, *, name: str = "") -> Template:
    return Template(
        name=str(payload.get("name") or name),
        content=str(payload.get("content", "")),
    )


def parse_media_upload(payload: JsonDict) -> MediaUpload:
    return MediaUpload(
        path=str(payload.get("path", "")),
        filename=str(payload.get("filename", "")),
        media_type=str(payload.get("type", "")),
    )


def parse_share(payload: JsonDict) -> ShareLink:
    return ShareLink(
        url=str(payload.get("url", "")),
        token=str(payload.get("token", "")),
        path=str(payload.get("path", "")),
        theme=str(payload.get("theme", "light")),
    )


def parse_stats(payload: JsonDict) -> VaultStats:
    return VaultStats(
        notes_count=_as_int(payload.get("notes_count")),
        folders_count=_as_int(payload.get("folders_count")),
        tags_count=_as_int(payload.get("tags_count")),
        templates_count=_as_int(payload.get("templates_count")),
        media_count=_as_int(payload.get("media_count")),
        total_size_bytes=_as_int(payload.get("total_size_bytes")),
        last_modified=parse_timestamp(payload.get("last_modified")),
        plugins_enabled=_as_int(payload.get("plugins_enabled")),
        version=str(payload.get("version", "unknown")),
    )


def parse_config(payload: JsonDict, *, min_query_length: int) -> InstanceConfig:
    """`GET /api/config`.

    The keys are camelCase and flat — `searchEnabled`, not `search.enabled`. The
    minimum query length is **not** in the payload (it is the server constant
    `SEARCH_MIN_QUERY_LENGTH`, 2 in 0.31.3), so it is supplied by the caller.
    """
    authentication = payload.get("authentication")
    auth_enabled = (
        bool(authentication.get("enabled", False)) if isinstance(authentication, Mapping) else False
    )
    return InstanceConfig(
        name=str(payload.get("name", "NoteDiscovery")),
        version=str(payload.get("version", "unknown")),
        search_enabled=bool(payload.get("searchEnabled", True)),
        auth_enabled=auth_enabled,
        demo_mode=bool(payload.get("demoMode", False)),
        min_query_length=min_query_length,
        reachable=True,
        raw=dict(payload),
    )
