# Architecture

## 1. Overview

DiscoveryGram is a single Python service that bridges the Telegram Bot API and a NoteDiscovery
instance, using LLM providers for content understanding and generation.

```
        Telegram users (allow-listed IDs)
                    |
            Telegram Bot API
                    |
   +----------------v-----------------------------------+
   |                DiscoveryGram                        |
   |                                                     |
   |  Presentation   handlers, keyboards, renderers,     |
   |                 pagination, session state           |
   |  ---------------------------------------------------|
   |  Application    search / browse / create use cases  |
   |  ---------------------------------------------------|
   |  Ports          NoteStore  |  LlmClient  |  Files    |
   |  ---------------------------------------------------|
   |  Adapters       REST  MCP  |  9 providers | storage  |
   +--------|-------------------|------------------------+
            |                   |
   NoteDiscovery (REST + MCP)   LLM providers
```

## 2. Layering rules

- **Presentation** never calls an adapter directly; it calls the application layer.
- **Application** depends only on **ports** (abstract interfaces), never on concrete adapters.
- **Adapters** are selected and wired at startup from `.env`.
- This keeps NoteDiscovery's transport (REST vs MCP) and the LLM provider set swappable
  without touching bot logic.

## 3. NoteDiscovery integration

The full contract is documented in [notediscovery-contract.md](notediscovery-contract.md)
(NoteDiscovery 0.31.3). Two facts drive the design.

**Fact 1 — MCP is a strict subset of REST.** NoteDiscovery's MCP server is a thin wrapper that
calls the same `/api/...` HTTP endpoints. Its 18 tools expose *less* than REST: no media upload,
no export, no sharing, no folder move/rename/delete. Media upload is required by the
image-to-note flow, so **MCP can never be the sole backend**.

**Fact 2 — MCP is stdio, not network.** It is launched as a child process
(`docker run --rm -i ... python -m mcp_server`), so it cannot be reached over a URL.

Therefore:

| Adapter | Transport | Role |
|---|---|---|
| `RestNoteStore` | HTTP, `httpx.AsyncClient` | **Primary.** Implements the whole `NoteStore` port |
| `McpNoteStore` | MCP over **stdio** subprocess | Optional, for interface completeness and agentic flows. Raises `Unsupported` for operations outside its 18 tools |

`NOTEDISCOVERY_TRANSPORT` selects `rest` (default) or `mcp`. The per-operation `auto` resolution
originally planned is dropped: the capability relationship is statically known and one-directional,
so a runtime capability map would be complexity with no payoff.

### Running the MCP subprocess from inside a container

DiscoveryGram itself ships as a container, so spawning `docker run` needs a decision:

| Option | Cost |
|---|---|
| **A — mount the Docker socket** | Matches the upstream config verbatim, but `/var/run/docker.sock` is root-equivalent access on the host |
| **B — vendor the module** | Install NoteDiscovery's `mcp_server` into the DiscoveryGram image and spawn `python -m mcp_server` directly. No socket exposure; couples us to the upstream module version |
| **C — REST only** | `MCP_ENABLED=false`. Loses nothing functionally, since MCP is a subset |

**Decided: option C is the shipping default.** The MCP adapter is built and tested, but
`NOTEDISCOVERY_TRANSPORT` defaults to `rest` and `MCP_ENABLED` to `false`. This costs no
functionality — MCP is a strict subset — and keeps the Docker socket off the container. Operators
who want MCP opt in via A or B, both supported through `MCP_LAUNCH_MODE`.

### Client-side compensation

The port surface is wider than the API. The gap is closed in `adapters/`, shared by both
transports rather than duplicated in each:

| Gap | Where | How |
|---|---|---|
| No tree endpoint | `adapters/tree.py` | Built from `GET /api/notes`, which returns the vault's **folder list** as well as its notes — so empty folders survive. Cached with `TREE_CACHE_TTL_S`, invalidated on every write, and guarded by a lock so ten concurrent `/browse` taps cause one vault scan |
| No literal search mode | `adapters/rest.py` + `ranking.py` | `/find` filters `/search` results case-sensitively. Snippets carry only ±15 characters of context, so near-misses are confirmed against the note body — bounded to 25 fetches |
| No relevance score | `adapters/ranking.py` | Ordered by title hit, then exact/prefix title, then term frequency, then how early the first match sits, with recency as a tie-breaker. Ties break on path, so pagination stays stable across page turns |
| HTML in snippets | `adapters/ranking.py` | `<mark class="search-highlight">` is stripped and the matched term kept separately, ready to be re-highlighted in Telegram's own syntax |
| `PATCH` appends only | `RestNoteStore.update_note` | Read-modify-write over `POST`, which is an upsert. The read is what makes editing a deleted note fail as `NotFound` instead of re-creating it |
| No `limit` default on `/api/search` | `RestNoteStore.search` | An explicit `limit` on every call, defaulting to `SEARCH_DEFAULT_LIMIT` |
| Minimum query length is undiscoverable | `RestNoteStore.search` | `SEARCH_MIN_QUERY_LENGTH` (2) enforced locally: a short query never leaves the process |
| Per-endpoint rate limits | `adapters/throttle.py` | A sliding-window limiter per endpoint bucket, set 10% under each server limit because the two windows are unaligned. A 429 that still gets through becomes `RateLimited` with its `Retry-After` |
| No `recent` endpoint over REST | `RestNoteStore.recent_notes` | Derived from the listing's `modified` timestamps. MCP has a real `get_recent_notes` tool and uses it |

### Failure handling

Adapters translate every transport failure into the port's own error type — `NotFound`,
`Unauthorized`, `Forbidden`, `Conflict`, `InvalidRequest`, `RateLimited`, `Unavailable`,
`Unsupported` — so no `httpx` or `mcp` exception ever reaches the application layer.

Timeouts, connection errors and 5xx are retried up to `NOTEDISCOVERY_MAX_RETRIES` with exponential
backoff and full jitter. 4xx is never retried: a rejected request will not improve, and retrying it
only spends rate limit. `health()` is the deliberate exception — it backs `/readyz`, which the
orchestrator polls, so it never retries and never raises.

### Startup probe

`app/probe.py` asks the instance who it is once at boot: reachable or not, `searchEnabled` or not,
and which version. A `search.enabled: false` instance answers **403** from every `/api/search`
call, so the search commands are disabled up front with a clear message rather than failing once
per user request. A version other than the contract's 0.31.3 is logged as a warning — the contract
doc is version-stamped, and drift should be visible before it is mysterious. The probe never
raises: an unreachable instance degrades the bot, `/readyz` reports the truth, and the instance may
come back without a restart.

## 4. Domain model

NoteDiscovery's JSON is inconsistent across endpoints — `name` is sometimes a stem and sometimes a
filename, config keys are camelCase while note keys are snake_case, tags arrive as a `{tag: count}`
map, snippets arrive as HTML. `ports/model.py` normalises all of it once, and every adapter maps
into it (`adapters/parsing.py`), so the application layer sees one shape.

- `NoteRef` — path, title, folder, `modified`, size, tags. Listings use it to avoid fetching bodies.
- `Note` — a `NoteRef` plus body, `created`, line count and backlinks.
- `SearchHit` — a `NoteRef` plus cleaned `SearchMatch` snippets, a **client-side** `score` and
  `title_match`. The score orders results; it is never shown as a percentage, because NoteDiscovery
  returns no relevance signal at all.
- `TreeNode` — a folder with its child folders and notes, from the client-derived tree.
- `Backlink`, `Graph`, `TemplateRef`/`Template`, `MediaUpload`, `ShareLink`, `VaultStats`,
  `InstanceConfig` — the remaining payloads, one dataclass each.

Everything is a frozen dataclass: these objects are cached, shared across concurrent handlers and
paginated over, and none of that is safe if a renderer can mutate them.

### Paths

Note paths arrive from Telegram users, from LLM output and from wiki-links, so they are untrusted.
`util/paths.py` is the single gate: it rejects traversal (`..`), control characters and the
characters NoteDiscovery itself refuses, normalises separators, and appends `.md` when a user types
`Projects/Ideas`. Nothing reaches an adapter without passing through it.

## 5. LLM router (proprietary)

```
LlmRouter
  ├── task profile        (chat | vision | title-generation | summarise)
  ├── attempt ladder      ordered (provider, model) pairs, expanded from .env
  ├── retry policy        exponential backoff + jitter, retry-after aware
  ├── circuit breaker     per provider, opens on repeated failures
  └── provider adapters   nvidia, openrouter, groq, gemini, cloudflare,
                          cerebras, mistral, puter, ollama
```

### The attempt ladder

The unit of failover is a **(provider, model) pair**, not a provider. A provider chain plus each
provider's ordered model list expands into a flat ladder, built by
`discoverygram.llm.plan.build_attempt_ladder`:

```
LLM_CHAIN_CHAT=nvidia,ollama
NVIDIA_MODELS=llama-3.3-70b,qwen2.5-72b
OLLAMA_MODELS=llama3.2

  1. nvidia/llama-3.3-70b   retried LLM_RETRIES_PER_MODEL times
  2. nvidia/qwen2.5-72b     "
  3. ollama/llama3.2        "
```

This separates two failure modes that a provider-only chain conflates:

- **Model-level failure** (model unavailable, context too long, content refused) advances one rung,
  keeping the same provider and its warm connection.
- **Provider-level failure** (auth rejected, host unreachable, sustained 5xx) opens that provider's
  circuit and skips *all* its remaining rungs at once, rather than burning retries on models that
  cannot possibly answer.

Ladder construction is pure logic with no I/O, so it is fully unit-tested and evaluated at startup:
providers that are skipped — no credentials, no model listed for the task, unknown name — are
reported with their reason instead of vanishing silently. `make check-env` prints the resulting
ladder, which is how an operator confirms that a chain does what they think it does.

### Routing rules

- Each adapter declares its **capabilities** (vision, streaming, max context, JSON mode); a rung
  whose model cannot satisfy the task profile is never attempted.
- **Retry** covers transient failures (429, 5xx, timeouts, connection errors) and honours
  `Retry-After`; exhausting retries advances to the next rung.
- A provider whose circuit is open is skipped until its cool-down elapses.
- Most providers are OpenAI-compatible and share a base adapter; `gemini`, `cloudflare` and
  `puter` need dedicated request/response mapping.
- Every call is logged with provider, model, latency, token usage and outcome.

## 6. Session and callback state

Telegram limits `callback_data` to 64 bytes, which cannot carry note paths or queries. Search
results, cursors and pending creation drafts are stored server-side in a **session store** keyed by
a short opaque token embedded in the callback data.

`SESSION_BACKEND=memory` (default, single replica) or `redis` (multi-replica, survives restarts).

## 7. Runtime and deployment

- `python-telegram-bot` v22.x, async `Application`, **long polling** by default
  (`TELEGRAM_MODE=polling`); `webhook` mode optional for production behind a reverse proxy.
- Built-in `RateLimiter` to stay inside Telegram flood limits.
- Small `aiohttp` health endpoint exposing `/healthz` (liveness) and `/readyz`
  (NoteDiscovery reachable + at least one LLM provider healthy).
- Structured JSON logging (`structlog`), correlation id per update.
- Docker image: multi-stage, non-root user, `uv` for dependency install.
- `docker-compose.yml`: the bot plus optional Redis; NoteDiscovery is external.

## 8. Security

- Hard allow-list: every update is rejected unless the Telegram user id is in
  `TELEGRAM_ALLOWED_USER_IDS`. Rejections are logged, not answered with details.
- Secrets only from environment; `.env` is git-ignored, `.env.example` is committed.
- Per-user rate limiting on LLM-backed commands to bound cost.
- Uploaded files are size- and MIME-checked before being sent to a provider.
- Note content is sent to third-party LLM providers only for commands that require it; the
  provider chain is configurable so an `ollama`-only chain keeps data local.
