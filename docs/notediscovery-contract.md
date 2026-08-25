# NoteDiscovery Contract

Extracted from `ghcr.io/gamosoft/notediscovery:latest`, **version 0.31.3**
(`/app/backend/main.py`, `/app/mcp_server/`). Upstream: https://github.com/gamosoft/NoteDiscovery

Re-verify this document whenever the NoteDiscovery image is upgraded.

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
| `GET` | `/api/notes` | List notes; `limit`, `offset` |
| `GET` | `/api/notes/{note_path:path}` | Fetch one note |
| `POST` | `/api/notes/{note_path:path}` | Create note (write/overwrite semantics **to verify**) |
| `PATCH` | `/api/notes/{note_path:path}` | **Append only** — body `{content, add_timestamp}`. Rate-limited **60/minute** |
| `DELETE` | `/api/notes/{note_path:path}` | Delete note |
| `POST` | `/api/notes/move` | Move / rename a note |

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
- **One search mode only.** No exact/fuzzy switch, no field selector, no relevance score parameter.
- A **minimum query length** floor (`SEARCH_MIN_QUERY_LENGTH`) returns an empty result set below it.
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
| No full **update** endpoint — `PATCH` appends only | Edit = `GET` note, apply change, `POST` back over the same path. Overwrite semantics of `POST` must be confirmed against the live instance before relying on it |
| No exact/literal search mode | Run `/api/search`, then filter literally client-side. `/find` is a client-side refinement of `/search`, not a separate backend call |
| No relevance score returned | Rank client-side (title hit > body hit, term frequency); do not promise scores in the UI |
| Search may be disabled (403) or floored by min length | Detect at startup via `/api/config`; disable `/search` with a clear message instead of failing per-request |
| `GET /api/search` has **no default limit** | Always send an explicit `limit` — an unbounded vault query could return everything |
| `PATCH` rate-limited to 60/min | Client-side throttle on append flows; surface 429 as a friendly retry message |
| No tree endpoint | Derive the folder tree client-side from `GET /api/notes` paths, cached and invalidated on write |

## 5. Capabilities worth exploiting

Discovered features not in the original plan, all cheap to add:

- **`get_backlinks` + `get_graph`** — "notes linking here" navigation and related-note suggestions.
- **Templates** — `/new` from a template, a natural fit for structured note creation.
- **Sharing** — generate a public share link for a note and send it in the chat.
- **`get_recent_notes(days, limit)`** — backs `/recent` directly.
- **`/api/stats`** — feeds `/status`.
