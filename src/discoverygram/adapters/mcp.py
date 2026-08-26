"""`McpNoteStore` — the optional, flag-gated stdio adapter.

NoteDiscovery's MCP server is **not** a network service: it is launched as a
subprocess and spoken to over stdio. It exposes 18 tools that call the very same
`/api/...` endpoints, so it adds no capability REST lacks and is missing several
the bot needs. It exists here for interface completeness and agentic use, and
defaults to disabled.

Everything outside the 18 tools raises `Unsupported`, deliberately and loudly,
rather than degrading into a half-working flow:

    media upload · export · sharing · stats · folder move/rename/delete

The compensation layer is shared with REST: the tree comes from `list_notes`,
literal search filters `search_notes`, ranking is client-side, and an edit is a
`get_note` + `create_note` pair, because `append_to_note` only appends.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Mapping, Sequence
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any

from discoverygram.adapters.parsing import (
    parse_backlink,
    parse_config,
    parse_graph,
    parse_note,
    parse_note_listing,
    parse_search_results,
    parse_tag_notes,
    parse_tags,
    parse_template,
    parse_templates,
)
from discoverygram.adapters.ranking import filter_literal, rank
from discoverygram.adapters.tree import TreeCache
from discoverygram.config import McpLaunchMode, Settings
from discoverygram.ports.errors import (
    InvalidRequest,
    NoteStoreError,
    NotFound,
    Unavailable,
    Unsupported,
)
from discoverygram.ports.model import (
    Backlink,
    Graph,
    InstanceConfig,
    MediaUpload,
    Note,
    NoteListing,
    NoteRef,
    SearchHit,
    ShareLink,
    Template,
    TemplateRef,
    TreeNode,
    VaultStats,
)
from discoverygram.ports.note_store import NoteStore
from discoverygram.util.logging import get_logger
from discoverygram.util.paths import normalise_folder_path, normalise_note_path

log = get_logger(__name__)

# The complete tool set of NoteDiscovery 0.31.3's MCP server. Used to fail fast
# at startup when the connected server is not the one this adapter was written
# against, instead of discovering the gap mid-conversation.
SUPPORTED_TOOLS = frozenset(
    {
        "search_notes",
        "list_notes",
        "get_note",
        "list_tags",
        "get_notes_by_tag",
        "get_graph",
        "get_backlinks",
        "create_note",
        "delete_note",
        "create_folder",
        "append_to_note",
        "move_note",
        "get_recent_notes",
        "create_note_from_template",
        "list_templates",
        "get_template",
        "health_check",
        "get_config",
    }
)

_REST_ONLY = "This operation exists only over REST. Set NOTEDISCOVERY_TRANSPORT=rest to use it."


class McpNoteStore(NoteStore):
    """NoteDiscovery over its stdio MCP server.

    The `mcp` package is an optional dependency (`pip install discoverygram[mcp]`);
    it is imported lazily so a REST-only deployment never needs it installed.
    """

    def __init__(self, settings: Settings) -> None:
        if not settings.mcp_enabled:
            raise InvalidRequest("MCP_ENABLED must be true to use the MCP transport.")
        self._settings = settings
        self._session: Any = None
        self._exit_stack: Any = None
        self._tools: frozenset[str] = frozenset()
        self._tree = TreeCache(self._load_listing_for_tree, ttl_s=float(settings.tree_cache_ttl_s))

    # --- Subprocess lifecycle --------------------------------------------

    def _server_command(self) -> tuple[str, list[str], dict[str, str]]:
        """The command line and environment for the MCP subprocess."""
        env = {
            "NOTEDISCOVERY_URL": str(self._settings.notediscovery_url).rstrip("/"),
            "NOTEDISCOVERY_TIMEOUT": str(int(self._settings.notediscovery_timeout)),
            "NOTEDISCOVERY_MAX_RETRIES": str(self._settings.notediscovery_max_retries),
        }
        if self._settings.notediscovery_api_key:
            env["NOTEDISCOVERY_API_KEY"] = self._settings.notediscovery_api_key

        if self._settings.mcp_launch_mode is McpLaunchMode.LOCAL:
            # The module must be importable in this interpreter — the socket-free
            # alternative to the Docker launch, at the cost of vendoring it.
            return "python", ["-m", "mcp_server"], env

        docker = shutil.which("docker")
        if docker is None:
            raise Unavailable("MCP_LAUNCH_MODE=docker needs the docker CLI, which is not on PATH.")
        args = ["run", "--rm", "-i"]
        for key, value in env.items():
            args += ["-e", f"{key}={value}"]
        args += [self._settings.mcp_docker_image, "python", "-m", "mcp_server"]
        return docker, args, env

    async def connect(self) -> None:
        """Launch the subprocess, handshake, and verify the tool set."""
        if self._session is not None:
            return

        try:
            from contextlib import AsyncExitStack

            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError as exc:  # pragma: no cover - exercised only without the extra
            raise Unavailable(
                "The MCP transport needs the optional 'mcp' extra: uv sync --extra mcp"
            ) from exc

        command, args, env = self._server_command()
        stack = AsyncExitStack()
        try:
            read, write = await stack.enter_async_context(
                stdio_client(StdioServerParameters(command=command, args=args, env=env))
            )
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            listed = await session.list_tools()
        except Exception as exc:
            await stack.aclose()
            raise Unavailable(f"The MCP server failed to start: {exc}") from exc

        self._exit_stack = stack
        self._session = session
        self._tools = frozenset(tool.name for tool in listed.tools)

        missing = SUPPORTED_TOOLS - self._tools
        if missing:
            log.warning("mcp_tools_missing", missing=sorted(missing), found=len(self._tools))

    async def _call(self, tool: str, **arguments: Any) -> Any:
        """Invoke a tool and decode its result payload.

        A dropped subprocess is restarted once: an stdio server that exits takes
        the session with it, and a single reconnect turns a crash into a hiccup
        rather than a dead bot.
        """
        if tool not in SUPPORTED_TOOLS:
            raise Unsupported(f"{tool} is not one of the MCP server's tools. {_REST_ONLY}")

        await self.connect()
        try:
            result = await self._session.call_tool(tool, arguments)
        except Exception as exc:
            log.warning("mcp_call_failed", tool=tool, error=str(exc))
            await self._restart()
            try:
                result = await self._session.call_tool(tool, arguments)
            except Exception as retry_exc:
                raise Unavailable(f"MCP call {tool} failed: {retry_exc}") from retry_exc

        return self._decode(tool, result)

    @staticmethod
    def _decode(tool: str, result: Any) -> Any:
        """MCP returns content blocks; NoteDiscovery's tools put JSON in the text."""
        if getattr(result, "isError", False):
            message = McpNoteStore._text_of(result) or f"{tool} failed"
            if "not found" in message.lower():
                raise NotFound(message)
            raise NoteStoreError(message)

        structured = getattr(result, "structuredContent", None)
        if isinstance(structured, Mapping) and structured:
            return structured

        text = McpNoteStore._text_of(result)
        if not text:
            return {}
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"message": text}

    @staticmethod
    def _text_of(result: Any) -> str:
        content = getattr(result, "content", None)
        if not isinstance(content, Sequence):
            return ""
        parts = [str(block.text) for block in content if getattr(block, "text", None)]
        return "\n".join(parts).strip()

    async def _restart(self) -> None:
        log.info("mcp_restarting")
        await self.aclose()
        await self.connect()

    async def aclose(self) -> None:
        stack, self._exit_stack = self._exit_stack, None
        self._session = None
        self._tools = frozenset()
        if stack is not None:
            with suppress(Exception):
                await stack.aclose()

    @staticmethod
    def _mapping(payload: Any) -> Mapping[str, Any]:
        return payload if isinstance(payload, Mapping) else {}

    # --- System ----------------------------------------------------------

    async def health(self) -> bool:
        try:
            payload = self._mapping(await self._call("health_check"))
        except NoteStoreError:
            return False
        return payload.get("status") == "healthy"

    async def get_config(self) -> InstanceConfig:
        payload = self._mapping(await self._call("get_config"))
        return parse_config(payload, min_query_length=self._settings.search_min_query_length)

    async def get_stats(self) -> VaultStats:
        raise Unsupported(f"Vault statistics are not an MCP tool. {_REST_ONLY}")

    # --- Notes -----------------------------------------------------------

    async def list_notes(self, *, limit: int | None = None, offset: int = 0) -> NoteListing:
        arguments: dict[str, Any] = {"offset": offset}
        if limit is not None:
            arguments["limit"] = limit
        return parse_note_listing(self._mapping(await self._call("list_notes", **arguments)))

    async def get_note(self, path: str, *, include_backlinks: bool = True) -> Note:
        note_path = normalise_note_path(path)
        payload = self._mapping(await self._call("get_note", path=note_path))
        return parse_note(payload, path=note_path)

    async def create_note(self, path: str, content: str) -> NoteRef:
        note_path = normalise_note_path(path)
        await self._call("create_note", path=note_path, content=content)
        self.invalidate_tree()
        return NoteRef.from_path(note_path)

    async def append_note(self, path: str, content: str, *, add_timestamp: bool = False) -> None:
        if not content:
            raise InvalidRequest("There is nothing to append.")
        await self._call(
            "append_to_note",
            path=normalise_note_path(path),
            content=content,
            add_timestamp=add_timestamp,
        )

    async def delete_note(self, path: str) -> None:
        await self._call("delete_note", path=normalise_note_path(path))
        self.invalidate_tree()

    async def move_note(self, old_path: str, new_path: str) -> NoteRef:
        target = normalise_note_path(new_path)
        await self._call("move_note", old_path=normalise_note_path(old_path), new_path=target)
        self.invalidate_tree()
        return NoteRef.from_path(target)

    async def update_note(self, path: str, content: str) -> NoteRef:
        """Read-modify-write, exactly as over REST: `append_to_note` only appends."""
        note_path = normalise_note_path(path)
        await self.get_note(note_path)
        return await self.create_note(note_path, content)

    # --- Folders ---------------------------------------------------------

    async def create_folder(self, path: str) -> str:
        folder = normalise_folder_path(path)
        await self._call("create_folder", path=folder)
        self.invalidate_tree()
        return folder

    async def move_folder(self, old_path: str, new_path: str) -> str:
        raise Unsupported(f"Moving a folder is not an MCP tool. {_REST_ONLY}")

    async def rename_folder(self, old_path: str, new_path: str) -> str:
        raise Unsupported(f"Renaming a folder is not an MCP tool. {_REST_ONLY}")

    async def delete_folder(self, path: str) -> None:
        raise Unsupported(f"Deleting a folder is not an MCP tool. {_REST_ONLY}")

    # --- Search and tags -------------------------------------------------

    async def search(
        self, query: str, *, limit: int | None = None, offset: int = 0
    ) -> list[SearchHit]:
        text = query.strip()
        if not text or len(text) < self._settings.search_min_query_length:
            return []
        payload = self._mapping(
            await self._call(
                "search_notes",
                query=text,
                limit=limit if limit is not None else self._settings.search_default_limit,
                offset=offset,
            )
        )
        return rank(parse_search_results(payload), text)

    async def search_literal(self, query: str, *, limit: int | None = None) -> list[SearchHit]:
        text = query.strip()
        if not text:
            return []
        hits = await self.search(text, limit=self._settings.search_default_limit * 4)
        ordered = rank(filter_literal(hits, text), text)
        return ordered[:limit] if limit else ordered

    async def list_tags(self) -> dict[str, int]:
        return parse_tags(self._mapping(await self._call("list_tags")))

    async def get_notes_by_tag(
        self, tag: str, *, limit: int | None = None, offset: int = 0
    ) -> list[NoteRef]:
        arguments: dict[str, Any] = {"tag": tag, "offset": offset}
        if limit is not None:
            arguments["limit"] = limit
        return parse_tag_notes(self._mapping(await self._call("get_notes_by_tag", **arguments)))

    # --- Graph -----------------------------------------------------------

    async def get_graph(self) -> Graph:
        return parse_graph(self._mapping(await self._call("get_graph")))

    async def get_backlinks(self, path: str) -> list[Backlink]:
        payload = await self._call("get_backlinks", path=normalise_note_path(path))
        raw = payload.get("backlinks", payload) if isinstance(payload, Mapping) else payload
        if not isinstance(raw, list | tuple):
            return []
        return [parse_backlink(item) for item in raw if isinstance(item, Mapping)]

    # --- Templates -------------------------------------------------------

    async def list_templates(self) -> list[TemplateRef]:
        return parse_templates(self._mapping(await self._call("list_templates")))

    async def get_template(self, name: str) -> Template:
        return parse_template(self._mapping(await self._call("get_template", name=name)), name=name)

    async def create_note_from_template(self, template_name: str, note_path: str) -> NoteRef:
        target = normalise_note_path(note_path)
        await self._call("create_note_from_template", template_name=template_name, note_path=target)
        self.invalidate_tree()
        return NoteRef.from_path(target)

    # --- REST-only surface -----------------------------------------------

    async def upload_media(
        self,
        filename: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
        note_path: str = "",
    ) -> MediaUpload:
        raise Unsupported(
            f"The MCP server cannot upload media, so image-to-note needs REST. {_REST_ONLY}"
        )

    async def export_note(self, path: str) -> bytes:
        raise Unsupported(f"Exporting a note is not an MCP tool. {_REST_ONLY}")

    async def share_note(self, path: str, *, theme: str = "light") -> ShareLink:
        raise Unsupported(f"Sharing a note is not an MCP tool. {_REST_ONLY}")

    async def unshare_note(self, path: str) -> None:
        raise Unsupported(f"Un-sharing a note is not an MCP tool. {_REST_ONLY}")

    # --- Compensation ----------------------------------------------------

    async def _load_listing_for_tree(self) -> NoteListing:
        return await self.list_notes()

    async def get_tree(self, *, refresh: bool = False) -> TreeNode:
        return await self._tree.get(refresh=refresh)

    def invalidate_tree(self) -> None:
        self._tree.invalidate()

    async def recent_notes(self, *, days: int = 7, limit: int = 20) -> list[NoteRef]:
        """MCP has a real tool for this, unlike REST."""
        payload = self._mapping(await self._call("get_recent_notes", days=days, limit=limit))
        notes = parse_note_listing(payload).notes
        if notes:
            return list(notes[:limit])
        # Some builds answer with a bare `{"notes": [...]}`, others with a list;
        # fall back to the same client-side derivation the REST adapter uses.
        listing = await self.list_notes()
        cutoff = datetime.now(UTC) - timedelta(days=days)
        recent = [
            note for note in listing.notes if note.modified is not None and note.modified >= cutoff
        ]
        recent.sort(
            key=lambda note: note.modified or datetime.min.replace(tzinfo=UTC), reverse=True
        )
        return recent[:limit]
