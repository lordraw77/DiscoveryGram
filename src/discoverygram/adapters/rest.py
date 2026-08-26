"""`RestNoteStore` — the primary NoteDiscovery adapter.

REST is the complete surface: MCP is a strict subset of it. Everything the bot
needs (media upload above all) lives here.

Responsibilities beyond plain HTTP:

* one pooled `httpx.AsyncClient` for the process, with a shared timeout;
* retries on timeouts, connection errors and 5xx, with exponential backoff
  and jitter — never on 4xx, which will not get better;
* client-side throttling ahead of the server's per-endpoint rate limits;
* the compensation layer: derived tree, literal search, client-side ranking,
  read-modify-write editing and client-side `recent`.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from discoverygram.adapters.parsing import (
    parse_config,
    parse_graph,
    parse_media_upload,
    parse_note,
    parse_note_listing,
    parse_note_ref,
    parse_search_results,
    parse_share,
    parse_stats,
    parse_tag_notes,
    parse_tags,
    parse_template,
    parse_templates,
)
from discoverygram.adapters.ranking import filter_literal, rank
from discoverygram.adapters.throttle import RateLimiter
from discoverygram.adapters.tree import TreeCache
from discoverygram.config import Settings
from discoverygram.ports.errors import (
    Conflict,
    Forbidden,
    InvalidRequest,
    NoteStoreError,
    NotFound,
    RateLimited,
    Unauthorized,
    Unavailable,
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
from discoverygram.util.correlation import get_correlation_id
from discoverygram.util.logging import get_logger
from discoverygram.util.paths import encode_path, normalise_folder_path, normalise_note_path

log = get_logger(__name__)

# How many candidate note bodies a literal search may read before giving up on
# precision. Snippets carry only ±15 characters of context, so a literal match
# can sit just outside them; reading bodies fixes that at a bounded cost.
LITERAL_BODY_FETCH_LIMIT = 25

_RETRYABLE_STATUS = frozenset({502, 503, 504})


class RestNoteStore(NoteStore):
    """NoteDiscovery over its REST API."""

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
        limiter: RateLimiter | None = None,
    ) -> None:
        self._settings = settings
        self._base_url = str(settings.notediscovery_url).rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(settings.notediscovery_timeout),
            verify=settings.notediscovery_verify_tls,
            headers=settings.notediscovery_headers,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            follow_redirects=True,
        )
        self._limiter = limiter or RateLimiter()
        self._tree = TreeCache(
            self._load_listing_for_tree,
            ttl_s=float(settings.tree_cache_ttl_s),
        )

    # --- Transport -------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        bucket: str | None = None,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        files: Any = None,
        data: Mapping[str, Any] | None = None,
        expect_json: bool = True,
        retries: int | None = None,
    ) -> Any:
        """Send one request, retrying transient failures, and return its body."""
        if bucket:
            waited = await self._limiter.acquire(bucket)
            if waited:
                log.debug("throttled", bucket=bucket, waited_s=round(waited, 2))

        headers = {}
        correlation_id = get_correlation_id()
        if correlation_id:
            headers["X-Correlation-Id"] = correlation_id

        max_retries = (
            self._settings.notediscovery_max_retries if retries is None else max(retries, 0)
        )
        attempts = max_retries + 1
        last_error: Exception | None = None

        for attempt in range(1, attempts + 1):
            try:
                response = await self._client.request(
                    method,
                    path,
                    params=dict(params) if params else None,
                    json=json,
                    files=files,
                    data=dict(data) if data else None,
                    headers=headers or None,
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                if attempt >= attempts:
                    raise Unavailable(
                        f"NoteDiscovery is unreachable at {self._base_url}: {exc}"
                    ) from exc
                await self._backoff(attempt, method=method, path=path, reason=type(exc).__name__)
                continue

            if response.status_code in _RETRYABLE_STATUS and attempt < attempts:
                await self._backoff(
                    attempt, method=method, path=path, reason=str(response.status_code)
                )
                continue

            if response.status_code >= 400:
                raise self._to_error(response)

            if not expect_json:
                return response.content
            if not response.content:
                return {}
            try:
                return response.json()
            except ValueError as exc:
                raise NoteStoreError(
                    f"NoteDiscovery returned a non-JSON body for {method} {path}"
                ) from exc

        # Only reachable when the last loop pass was a retryable 5xx.
        raise Unavailable(
            f"NoteDiscovery failed after {attempts} attempts for {method} {path}"
            + (f": {last_error}" if last_error else "")
        )

    async def _backoff(self, attempt: int, *, method: str, path: str, reason: str) -> None:
        # Full jitter: spreads a thundering herd of retries after an outage.
        ceiling = min(2.0 ** (attempt - 1), 30.0)
        delay = random.uniform(0.0, ceiling)  # noqa: S311 — jitter, not cryptography
        log.warning(
            "notediscovery_retry",
            method=method,
            path=path,
            attempt=attempt,
            reason=reason,
            delay_s=round(delay, 2),
        )
        await asyncio.sleep(delay)

    def _to_error(self, response: httpx.Response) -> NoteStoreError:
        detail = self._detail(response)
        status = response.status_code

        if status == 401:
            return Unauthorized(
                "NoteDiscovery rejected the API key. Check NOTEDISCOVERY_API_KEY.", status=status
            )
        if status == 403:
            return Forbidden(detail or "NoteDiscovery refused the operation.", status=status)
        if status == 404:
            return NotFound(detail or "Not found in the vault.", status=status)
        if status == 409:
            return Conflict(detail or "That name is already taken.", status=status)
        if status == 429:
            retry_after = response.headers.get("Retry-After")
            return RateLimited(
                detail or "NoteDiscovery is rate-limiting this operation.",
                retry_after=float(retry_after) if retry_after and retry_after.isdigit() else None,
            )
        if 400 <= status < 500:
            return InvalidRequest(
                detail or f"NoteDiscovery rejected the request ({status}).", status=status
            )
        return Unavailable(detail or f"NoteDiscovery returned HTTP {status}.", status=status)

    @staticmethod
    def _detail(response: httpx.Response) -> str:
        """FastAPI puts the message in `detail`, sometimes as a nested object."""
        try:
            payload = response.json()
        except ValueError:
            return response.text.strip()[:200]
        if isinstance(payload, Mapping):
            detail = payload.get("detail")
            if isinstance(detail, Mapping):
                return str(detail.get("message") or detail.get("reason") or detail)
            if detail is not None:
                return str(detail)
        return ""

    @staticmethod
    def _expect_mapping(payload: Any, what: str) -> Mapping[str, Any]:
        if not isinstance(payload, Mapping):
            raise NoteStoreError(f"NoteDiscovery returned an unexpected shape for {what}")
        return payload

    # --- System ----------------------------------------------------------

    async def health(self) -> bool:
        """Never raises, never retries.

        This backs `/readyz`, which is polled by the orchestrator: burning the
        full retry ladder on every poll would turn a brief outage into a probe
        that times out instead of answering 503.
        """
        try:
            payload = await self._request("GET", "/health", retries=0)
        except NoteStoreError:
            return False
        if isinstance(payload, Mapping) and payload.get("status"):
            return str(payload["status"]).lower() == "healthy"
        # A 2xx with no recognisable body still means something answered.
        return True

    async def get_config(self) -> InstanceConfig:
        payload = self._expect_mapping(await self._request("GET", "/api/config"), "/api/config")
        return parse_config(payload, min_query_length=self._settings.search_min_query_length)

    async def get_stats(self) -> VaultStats:
        payload = self._expect_mapping(
            await self._request("GET", "/api/stats", bucket="stats"), "/api/stats"
        )
        return parse_stats(payload)

    # --- Notes -----------------------------------------------------------

    async def list_notes(self, *, limit: int | None = None, offset: int = 0) -> NoteListing:
        params: dict[str, Any] = {"offset": offset}
        if limit is not None:
            params["limit"] = limit
        payload = self._expect_mapping(
            await self._request("GET", "/api/notes", params=params), "/api/notes"
        )
        return parse_note_listing(payload)

    async def get_note(self, path: str, *, include_backlinks: bool = True) -> Note:
        note_path = normalise_note_path(path)
        payload = self._expect_mapping(
            await self._request(
                "GET",
                f"/api/notes/{encode_path(note_path)}",
                params={"include_backlinks": include_backlinks},
            ),
            "a note",
        )
        return parse_note(payload, path=note_path)

    async def create_note(self, path: str, content: str) -> NoteRef:
        note_path = normalise_note_path(path)
        await self._request(
            "POST",
            f"/api/notes/{encode_path(note_path)}",
            bucket="note_write",
            json={"content": content},
        )
        self.invalidate_tree()
        return NoteRef.from_path(note_path)

    async def append_note(self, path: str, content: str, *, add_timestamp: bool = False) -> None:
        if not content:
            raise InvalidRequest("There is nothing to append.")
        note_path = normalise_note_path(path)
        await self._request(
            "PATCH",
            f"/api/notes/{encode_path(note_path)}",
            bucket="note_append",
            json={"content": content, "add_timestamp": add_timestamp},
        )

    async def delete_note(self, path: str) -> None:
        note_path = normalise_note_path(path)
        await self._request("DELETE", f"/api/notes/{encode_path(note_path)}", bucket="note_delete")
        self.invalidate_tree()

    async def move_note(self, old_path: str, new_path: str) -> NoteRef:
        source = normalise_note_path(old_path)
        target = normalise_note_path(new_path)
        await self._request(
            "POST",
            "/api/notes/move",
            bucket="note_move",
            json={"oldPath": source, "newPath": target},
        )
        self.invalidate_tree()
        return NoteRef.from_path(target)

    async def update_note(self, path: str, content: str) -> NoteRef:
        """Replace a note's body.

        `PATCH` only appends, so a full update is a `POST` over the same path —
        which NoteDiscovery treats as an upsert. The note is read first so an
        edit of something that no longer exists fails as `NotFound` instead of
        silently re-creating it.
        """
        note_path = normalise_note_path(path)
        await self.get_note(note_path, include_backlinks=False)
        return await self.create_note(note_path, content)

    # --- Folders ---------------------------------------------------------

    async def create_folder(self, path: str) -> str:
        folder = normalise_folder_path(path)
        await self._request("POST", "/api/folders", bucket="folder_create", json={"path": folder})
        self.invalidate_tree()
        return folder

    async def move_folder(self, old_path: str, new_path: str) -> str:
        source = normalise_folder_path(old_path)
        target = normalise_folder_path(new_path)
        await self._request(
            "POST",
            "/api/folders/move",
            bucket="folder_move",
            json={"oldPath": source, "newPath": target},
        )
        self.invalidate_tree()
        return target

    async def rename_folder(self, old_path: str, new_path: str) -> str:
        source = normalise_folder_path(old_path)
        target = normalise_folder_path(new_path)
        await self._request(
            "POST",
            "/api/folders/rename",
            bucket="folder_rename",
            json={"oldPath": source, "newPath": target},
        )
        self.invalidate_tree()
        return target

    async def delete_folder(self, path: str) -> None:
        folder = normalise_folder_path(path)
        await self._request("DELETE", f"/api/folders/{encode_path(folder)}", bucket="folder_delete")
        self.invalidate_tree()

    # --- Search and tags -------------------------------------------------

    async def search(
        self, query: str, *, limit: int | None = None, offset: int = 0
    ) -> list[SearchHit]:
        text = query.strip()
        if not text:
            return []
        # Below the server's floor the endpoint returns an empty set anyway; not
        # calling it saves a round trip and keeps the "too short" message local.
        if len(text) < self._settings.search_min_query_length:
            return []

        effective_limit = limit if limit is not None else self._settings.search_default_limit
        payload = self._expect_mapping(
            await self._request(
                "GET",
                "/api/search",
                params={"q": text, "limit": effective_limit, "offset": offset},
            ),
            "/api/search",
        )
        return rank(parse_search_results(payload), text)

    async def search_literal(self, query: str, *, limit: int | None = None) -> list[SearchHit]:
        text = query.strip()
        if not text:
            return []

        # Over-fetch: the case-sensitive filter below only ever removes results,
        # so asking for exactly `limit` would usually under-deliver.
        fetch = (limit or self._settings.search_default_limit) * 4
        hits = await self.search(text, limit=min(fetch, self._settings.search_default_limit * 10))

        kept = filter_literal(hits, text)
        matched_paths = {hit.path for hit in kept}

        # Snippets are only ±15 characters wide, so a literal occurrence can sit
        # outside every snippet. Confirm the near-misses against the real body.
        undecided = [hit for hit in hits if hit.path not in matched_paths]
        for hit in undecided[:LITERAL_BODY_FETCH_LIMIT]:
            try:
                note = await self.get_note(hit.path, include_backlinks=False)
            except NoteStoreError:
                continue
            if text in note.content:
                kept.append(hit)

        ordered = rank(kept, text)
        return ordered[:limit] if limit else ordered

    async def list_tags(self) -> dict[str, int]:
        payload = self._expect_mapping(await self._request("GET", "/api/tags"), "/api/tags")
        return parse_tags(payload)

    async def get_notes_by_tag(
        self, tag: str, *, limit: int | None = None, offset: int = 0
    ) -> list[NoteRef]:
        params: dict[str, Any] = {"offset": offset}
        if limit is not None:
            params["limit"] = limit
        payload = self._expect_mapping(
            await self._request("GET", f"/api/tags/{encode_path(tag)}", params=params),
            "a tag listing",
        )
        return parse_tag_notes(payload)

    # --- Graph -----------------------------------------------------------

    async def get_graph(self) -> Graph:
        payload = self._expect_mapping(await self._request("GET", "/api/graph"), "/api/graph")
        return parse_graph(payload)

    async def get_backlinks(self, path: str) -> list[Backlink]:
        """Free with the note payload — no separate call exists over REST."""
        note = await self.get_note(path, include_backlinks=True)
        return list(note.backlinks)

    # --- Templates -------------------------------------------------------

    async def list_templates(self) -> list[TemplateRef]:
        payload = self._expect_mapping(
            await self._request("GET", "/api/templates", bucket="template_read"), "/api/templates"
        )
        return parse_templates(payload)

    async def get_template(self, name: str) -> Template:
        payload = self._expect_mapping(
            await self._request(
                "GET", f"/api/templates/{encode_path(name)}", bucket="template_read"
            ),
            "a template",
        )
        return parse_template(payload, name=name)

    async def create_note_from_template(self, template_name: str, note_path: str) -> NoteRef:
        target = normalise_note_path(note_path)
        payload = await self._request(
            "POST",
            "/api/templates/create-note",
            bucket="template_create",
            json={"templateName": template_name, "notePath": target},
        )
        self.invalidate_tree()
        if isinstance(payload, Mapping) and payload.get("path"):
            return parse_note_ref({"path": str(payload["path"])})
        return NoteRef.from_path(target)

    # --- Media, export, sharing ------------------------------------------

    async def upload_media(
        self,
        filename: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
        note_path: str = "",
    ) -> MediaUpload:
        if len(data) > self._settings.max_upload_bytes:
            raise InvalidRequest(
                f"{filename} is larger than the {self._settings.max_upload_mb} MB limit."
            )
        payload = self._expect_mapping(
            await self._request(
                "POST",
                "/api/upload-media",
                bucket="media_upload",
                files={"file": (filename, data, content_type)},
                data={"note_path": note_path},
            ),
            "/api/upload-media",
        )
        return parse_media_upload(payload)

    async def export_note(self, path: str) -> bytes:
        note_path = normalise_note_path(path)
        content = await self._request(
            "GET",
            f"/api/export/{encode_path(note_path)}",
            bucket="export",
            params={"download": True},
            expect_json=False,
        )
        return bytes(content)

    async def share_note(self, path: str, *, theme: str = "light") -> ShareLink:
        note_path = normalise_note_path(path)
        payload = self._expect_mapping(
            await self._request(
                "POST",
                f"/api/share/{encode_path(note_path)}",
                bucket="share_write",
                json={"theme": theme},
            ),
            "a share link",
        )
        return parse_share(payload)

    async def unshare_note(self, path: str) -> None:
        note_path = normalise_note_path(path)
        await self._request("DELETE", f"/api/share/{encode_path(note_path)}", bucket="share_write")

    # --- Compensation ----------------------------------------------------

    async def _load_listing_for_tree(self) -> NoteListing:
        return await self.list_notes()

    async def get_tree(self, *, refresh: bool = False) -> TreeNode:
        return await self._tree.get(refresh=refresh)

    def invalidate_tree(self) -> None:
        self._tree.invalidate()

    async def recent_notes(self, *, days: int = 7, limit: int = 20) -> list[NoteRef]:
        """No `/api/recent` exists; the note listing already carries `modified`."""
        listing = await self.list_notes()
        cutoff = datetime.now(UTC) - timedelta(days=days)
        recent = [
            note for note in listing.notes if note.modified is not None and note.modified >= cutoff
        ]
        recent.sort(
            key=lambda note: note.modified or datetime.min.replace(tzinfo=UTC), reverse=True
        )
        return recent[:limit]

    # --- Lifecycle -------------------------------------------------------

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
