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

The port surface is wider than the API, so the REST adapter compensates:

- **Folder tree** — no tree endpoint exists; derived from `GET /api/notes` paths, cached and
  invalidated on write.
- **Exact search** — no literal mode exists; `/find` filters `/api/search` results client-side.
- **Ranking** — no scores are returned; hits are ordered client-side (title match before body
  match, then term frequency).
- **Edit** — `PATCH` only appends; a full edit is read-modify-write over `POST`.
- **Limits** — `GET /api/search` has no default cap, so an explicit `limit` is always sent.

## 4. Domain model

Normalised in DiscoveryGram, mapped by each adapter:

- `Note` — id, path, title, body, tags, timestamps, parent, attachment refs.
- `NoteRef` — lightweight id + path + title, used in listings to avoid fetching bodies.
- `SearchHit` — `NoteRef` + score + highlighted snippet.
- `TreeNode` — path segment, child count, whether it holds a note.

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
