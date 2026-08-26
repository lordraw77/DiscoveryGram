# NoteDiscovery Contract

Extracted from `ghcr.io/gamosoft/notediscovery:latest`, **version 0.31.3**
(`/app/backend/main.py`, `/app/mcp_server/`). Upstream: https://github.com/gamosoft/NoteDiscovery

Re-verify this document whenever the NoteDiscovery image is upgraded.

**Revision, phase 1.** The handler bodies were read directly from the image
(`backend/main.py`, `backend/utils.py`) rather than inferred from the route table. That settled
both open questions from phase 0 and corrected four assumptions; the affected rows below are
marked **(corrected in phase 1)**.

## 1. Connection and authentication

A single base URL with port serves both the REST API and the MCP server's own backend calls —
for example `http://host.docker.internal:8000`. The MCP server takes it as `NOTEDISCOVERY_URL`.

Authentication is one shared API key, accepted two ways:

- `X-API-Key: <key>` (used by the bundled MCP client)
- `Authorization: Bearer <key>`

Server-side the key comes from `AUTHENTICATION_API_KEY`. It is **optional** — an instance may run
unauthenticated, so DiscoveryGram must treat the key as optional and still send it when present.
A browser session login (`POST /login`) also exists; DiscoveryGram does not use it.

## 2. REST API surface

Base path `/api`. This is the **authoritative, complete** surface.

### Notes
| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/notes` | List notes; `limit`, `offset`. Returns `{notes, folders, pagination?}` — the **folder list comes with it** *(corrected in phase 1)*. Records whose `type` is not `note` are media |
| `GET` | `/api/notes/{note_path:path}` | Fetch one note: `{path, content, metadata, backlinks}`. **Backlinks are included** unless `include_backlinks=false` *(corrected in phase 1)* |
| `POST` | `/api/notes/{note_path:path}` | Create **or overwrite** — an upsert. Rate-limited **300/minute** *(corrected in phase 1: it is the editor's autosave endpoint, `create_or_update_note`)* |
| `PATCH` | `/api/notes/{note_path:path}` | **Append only** — body `{content, add_timestamp}`. Rate-limited **60/minute** |
| `DELETE` | `/api/notes/{note_path:path}` | Delete note. Rate-limited **30/minute** |
| `POST` | `/api/notes/move` | Move / rename a note. Body is camelCase: `{oldPath, newPath}`. **30/minute** |

### Folders
`POST /api/folders` · `POST /api/folders/move` · `POST /api/folders/rename` ·
`DELETE /api/folders/{folder_path:path}`

### Search and tags
| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/search?q=&limit=&offset=` | Content search. `limit` optional — **no default cap** |
| `GET` | `/api/tags` | All tags |
| `GET` | `/api/tags/{tag_name}` | Notes carrying a tag; `limit`, `offset` |

Search behaviour, confirmed in code:
- **One search mode only** — case-insensitive substring matching (`re.escape` + `re.IGNORECASE`).
  No exact/fuzzy switch, no field selector, no relevance score parameter.
- The **minimum query length is a hard-coded 2** (`SEARCH_MIN_QUERY_LENGTH`) and is **not exposed
  by any endpoint** *(corrected in phase 1)*. Below it the endpoint returns an empty result set.
- Results **do carry snippets**: up to 3 matched lines per note as
  `{line_number, context}`, HTML-escaped and wrapped in `<mark class="search-highlight">`
  *(corrected in phase 1 — the phase 0 assumption was that no snippets were returned)*. The markup
  must be stripped before rendering, or it collides with Telegram's own formatting.
- Search can be **disabled server-side** (`search.enabled: false`) and then returns **403**.
- An empty `q` returns `{"results": [], "message": "No search term provided"}` — not an error.
- A plugin hook may replace the result set entirely.

### Graph and links
`GET /api/graph` — full note graph · backlinks for a note (exposed as an MCP tool over the same data).

### Templates
`GET /api/templates` · `GET /api/templates/{template_name}` · `POST /api/templates/create-note`

### Media
`POST /api/upload-media` · `GET /api/media/{media_path:path}` · `PUT /api/media/{media_path:path}` ·
`POST /api/media/move`

### Rate limits *(added in phase 1)*

Declared with `slowapi` on the handlers. DiscoveryGram paces itself below each of these rather
than collecting 429s (`adapters/throttle.py`).

| Limit | Endpoints |
|---|---|
| 300/min | `POST /api/notes/{path}` |
| 120/min | `GET /api/templates`, `GET /api/templates/{name}`, `GET /api/share/{path}` |
| 60/min | `PATCH /api/notes/{path}`, `POST /api/templates/create-note` |
| 30/min | `DELETE /api/notes/{path}`, `POST /api/notes/move`, `POST /api/folders`, `POST /api/folders/rename`, `POST /api/share/{path}`, `GET /api/stats`, `GET /api/export/{path}` |
| 20/min | `POST /api/upload-media`, `POST /api/folders/move`, `DELETE /api/folders/{path}` |
| 10/min | `POST /api/plugins/{name}/toggle` |

Everything else — `GET /api/notes`, `GET /api/search`, `GET /api/tags`, `GET /api/graph` — is
unlimited server-side.

### Export, sharing, system
`GET /api/export/{note_path:path}` ·
`POST|GET|DELETE /api/share/{note_path:path}`, `GET /api/share-slug`, `GET /api/shared-notes` ·
`GET /api/config`, `GET /api/stats`, `GET /api/index/stats`, `GET /health` ·
`GET /api/plugins`, `POST /api/plugins/{plugin_name}/toggle`

## 3. MCP server

Launched over **stdio**, not over the network:

```json
{
  "mcpServers": {
    "notediscovery": {
      "command": "docker",
      "args": ["run", "--rm", "-i",
               "-e", "NOTEDISCOVERY_URL=http://host.docker.internal:8000",
               "ghcr.io/gamosoft/notediscovery:latest",
               "python", "-m", "mcp_server"]
    }
  }
}
```

Its own configuration (`/app/mcp_server/config.py`):
`NOTEDISCOVERY_URL` (default `http://localhost:8000`), `NOTEDISCOVERY_API_KEY`,
`NOTEDISCOVERY_TIMEOUT` (default 30), `NOTEDISCOVERY_MAX_RETRIES` (default 3).

### Tools (18)

| Tool | Required params | Optional |
|---|---|---|
| `search_notes` | `query` | `limit`, `offset` |
| `list_notes` | — | `limit`, `offset` |
| `get_note` | `path` | — |
| `list_tags` | — | — |
| `get_notes_by_tag` | `tag` | `limit`, `offset` |
| `get_graph` | — | — |
| `get_backlinks` | `path` | — |
| `create_note` | `path`, `content` | — |
| `delete_note` | `path` | — |
| `create_folder` | `path` | — |
| `append_to_note` | `path`, `content` | `add_timestamp` |
| `move_note` | `old_path`, `new_path` | — |
| `get_recent_notes` | — | `days`, `limit` |
| `create_note_from_template` | `template_name`, `note_path` | — |
| `list_templates` | — | — |
| `get_template` | `name` | — |
| `health_check` | — | — |
| `get_config` | — | — |

### Critical finding: MCP is a strict subset of REST

`mcp_server/client.py` calls exactly the same `/api/...` endpoints over HTTP. **MCP adds no
capability that REST lacks**, and it is *missing* capabilities DiscoveryGram needs:

- **no media upload** — the image-to-note flow cannot be served by MCP,
- no export, sharing, stats, folder move/rename/delete, plugin control.

**Consequence:** REST is the primary transport. MCP is supported for interface completeness and
agentic use, never as the sole backend. The "capability map with per-operation `auto` resolution"
originally planned is dropped — the capability relationship is statically known and one-directional.

## 4. Gaps DiscoveryGram must work around

| Gap | Workaround |
|---|---|
| No full **update** endpoint — `PATCH` appends only | Edit = `GET` note, apply change, `POST` back over the same path. **`POST` overwrite is confirmed in source**, so the delete-then-create fallback in the risk register is not needed. The `GET` is kept so editing a deleted note fails as `NotFound` instead of silently re-creating it |
| No exact/literal search mode | Run `/api/search`, then filter literally client-side. `/find` is a client-side refinement of `/search`, not a separate backend call |
| No relevance score returned | Rank client-side (title hit > body hit, term frequency); do not promise scores in the UI |
| Search may be disabled (403) | Detect at startup via `/api/config` (`searchEnabled`); disable `/search` with a clear message instead of failing per-request |
| Minimum query length is not discoverable | Carried in DiscoveryGram's own `SEARCH_MIN_QUERY_LENGTH` (default 2) and enforced client-side, so a short query never leaves the process |
| `GET /api/search` has **no default limit** | Always send an explicit `limit` — an unbounded vault query could return everything |
| `PATCH` rate-limited to 60/min | Client-side throttle on append flows; surface 429 as a friendly retry message |
| No tree endpoint | Derive the folder tree client-side. `GET /api/notes` returns the vault's **folder list** alongside the notes, so empty folders survive the derivation — a path-only derivation would lose them. Cached with a TTL, invalidated on every write |
| No `recent` endpoint over REST | Derived from the listing's `modified` timestamps. MCP does have `get_recent_notes` |

## 5. Capabilities worth exploiting

Discovered features not in the original plan, all cheap to add:

- **`get_backlinks` + `get_graph`** — "notes linking here" navigation and related-note suggestions.
- **Templates** — `/new` from a template, a natural fit for structured note creation.
- **Sharing** — generate a public share link for a note and send it in the chat.
- **`get_recent_notes(days, limit)`** — backs `/recent` directly.
- **`/api/stats`** — feeds `/status`.

## 6. Response shapes relied on by the adapter

Recorded in `tests/fixtures/notediscovery.py` and asserted by `tests/test_parsing.py`, so a
NoteDiscovery upgrade that changes a shape fails the suite rather than the bot.

| Endpoint | Shape |
|---|---|
| `GET /health` | `{status: "healthy", app, version}` |
| `GET /api/config` | **Flat and camelCase**: `{name, version, searchEnabled, demoMode, authentication: {enabled}, ...}` — *not* the nested `search.enabled` assumed in phase 0 |
| `GET /api/notes` | `{notes: [{name, path, folder, modified, size, type, tags}], folders: [str], pagination?}` |
| `GET /api/notes/{path}` | `{path, content, metadata: {created, modified, size, lines}, backlinks: [{path, name, references: [{line_number, context, type}]}]}` |
| `GET /api/search` | `{results: [{name, path, folder, matches: [{line_number, context}]}], query, pagination?}` |
| `GET /api/tags` | `{tags: {name: count}}` — a map, not a list |
| `GET /api/tags/{tag}` | `{tag, count, notes: [...], pagination?}` |
| `GET /api/graph` | `{nodes: [{id, label}], edges: [{source, target, type}]}` |
| `GET /api/stats` | `{notes_count, folders_count, tags_count, templates_count, media_count, total_size_bytes, last_modified, plugins_enabled, version}` |
| `POST /api/upload-media` | `{success, path, filename, type, message}` |
| `POST /api/share/{path}` | `{success, token, url, path, theme}` |

Errors are FastAPI's `{"detail": ...}`, where `detail` is a string except on a rejected share slug,
where it is `{reason, message}`. Authentication failure is **401**; search disabled is **403**.
