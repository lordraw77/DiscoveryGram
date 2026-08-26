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

## 5. The Telegram layer

### Handler ordering

```
group -1   allow-list        TypeHandler(Update) -> ApplicationHandlerStop
group  0   commands, callback queries, plain text
error      one handler for everything that escapes
```

The allow-list is a handler, not a check inside each command, because a check that has to be
remembered in twenty places is a check that will be forgotten in one. It raises
`ApplicationHandlerStop`, which is what actually prevents the update reaching group 0.

A rejected caller is told they are not authorised and **given their own Telegram id** — the number
an operator needs for `TELEGRAM_ALLOWED_USER_IDS`, and something the user already knows about
themselves. Nothing about the instance, its URL or the other allow-listed ids is revealed, and the
refusal is sent once per session TTL so an unknown account cannot keep the bot replying to it.

### Running inside our own event loop

DiscoveryGram owns its loop: the health server runs in it and SIGTERM is handled there. So
`Application.run_polling()`, which takes over the loop and installs its own signal handlers, is the
wrong entry point. `BotRunner` drives the lifecycle manually —
`initialize → start → updater.start_polling/start_webhook`, and the reverse on shutdown.

Startup order is deliberate: the health server binds **before** the instance is probed, so an
orchestrator polling `/readyz` during a slow start gets an honest 503 rather than a refused
connection. Shutdown runs in reverse from a `finally`, so a failure halfway through startup still
releases what did come up.

Exit codes carry intent: `InvalidToken` exits **2**, the same code an invalid `.env` produces,
because no number of restarts fixes a wrong token. Any other Telegram failure exits **1**, where a
restart plausibly helps.

### Callback tokens

Telegram caps `callback_data` at 64 bytes. A note path alone can exceed it. Buttons therefore carry
`action:token`, and the payload lives in the session store:

```
callback_data = "open:9f3a1c2b7d4e"
                 ^^^^ ^^^^^^^^^^^^
                 action  token -> session store -> {"path": "Projects/…"}
```

The action stays in the clear so a handler can route without a store round trip, and so an expired
token can say *which* button went stale rather than "something expired". Tokens are random rather
than derived from the payload: two users opening the same note get different tokens, so one user's
callback data reveals nothing about another's or about the vault.

`revoke` makes a one-shot button inert — a double tap on `Delete` must not delete twice — and
`extend` refreshes a token still in use, so paging does not expire mid-flow because page one was
issued an hour ago.

### Search and pagination

The four search modes live in `app/search.py` and know nothing about Telegram. Only one of them is
a NoteDiscovery feature: full text. Literal search filters the same call case-sensitively, tag
search uses `/api/tags/{tag}`, and recent is derived from the listing's timestamps over REST.

A mode that cannot run returns an outcome carrying a **notice** rather than raising — "search is
disabled on this instance" is an answer, not an error. The startup probe answers that cheaply, but
a `403` at call time is believed over it, because an instance can be reconfigured while we run.

Pagination stores the **whole result set once**, under a single callback token, and each page
button carries its number in the callback data:

```
page:9f3a1c2b7d4e:3
^^^^ ^^^^^^^^^^^^ ^
action  token   page
```

Twenty page turns therefore create one session entry rather than forty, and a page turn costs no
vault read — which also means page 2 cannot disagree with page 1 because a note changed in between.
The token's lifetime is refreshed on each turn, so a long browse does not expire.

Snippets come from the API, HTML-escaped and wrapped in `<mark>`; phase 1 strips that markup, and
`bot/results.py` re-applies highlighting in Telegram's syntax. The order matters: the snippet is
escaped **first**, the term is escaped the same way, and occurrences are then found in the escaped
text. Marking up first would let the escaper mangle its own markers. The term is regex-escaped too,
because it comes from the vault — `a.b` must not match `axb`.

### Navigation and the callback vocabulary

Three actions carry the whole navigation surface, and all three follow one shape:

```
nav:<token>:<arg>    a folder listing   arg = page number | e<index> | up | root
note:<token>:<arg>   a note body        arg = page number
act:<token>:<verb>   an action on a note
page:<token>:<arg>   search results     arg = page number | h<index>
```

The rule is **one token per view, not per button**. The token's payload holds what the whole view
needs — a folder's entries, a note's path, a search's hits — and every button on it carries an
argument instead of a token of its own. A nine-button action bar therefore costs one session entry,
and paging a sixty-item folder costs none. Only *entering* a different view issues a new token,
because that is genuinely new state rather than the same state at a different offset.

A listing is rebuilt from its token rather than re-derived from the tree: a page turn should not
depend on the vault being reachable, and page 2 must not show a different set of children than the
page the reader came from.

### Writing to a note

`PATCH` appends only, so replacing a body is a read-modify-write over `POST`, which is an upsert.
The read is not incidental — it makes editing a note that was deleted meanwhile fail as `NotFound`
rather than silently re-creating it from the editor's buffer.

Tags are body text in NoteDiscovery, not a field, so `Add tag` is an edit. It is idempotent, and
tag detection ignores fenced code so a `#` in a shell snippet is not mistaken for one.

Multi-step actions (`Edit`, `Append`, `Add tag`) park a pending intent in `user_data` and claim the
next message through a handler registered **ahead of** the plain-text search handler, which raises
`ApplicationHandlerStop` when it acts. Without that ordering, text meant as a note body would also
be run as a search. `/cancel` clears `user_data`, so it cancels every flow present and future
without needing a branch per flow.

Deleting asks first and revokes its own confirm token on success, so a double tap cannot act twice.

### Rendering

Two limits shape `bot/render.py`. MarkdownV2 reserves eighteen characters and rejects the **whole
message** on one unescaped occurrence, so escaping is total rather than clever — including literals
in our own strings, which is where the first bug of this kind was found. Messages cap at 4096
characters, so long text is split on paragraph boundaries, then line boundaries, and only mid-line
when a single line is itself too long.

### Secret scrubbing

Key-based redaction protects only what we log. Third-party libraries log whatever they like —
python-telegram-bot logs the Bot API URL, which contains the token, on every request it builds. So
the literal token and API key are scrubbed by substring from **both** pipelines, structlog and
stdlib, and the libraries that narrate requests are quieted to WARNING.

## 6. LLM router (proprietary)

```
LlmRouter                             llm/router.py
  ├── task profile        (chat | vision | title | summarise)   llm/plan.py
  ├── attempt ladder      ordered (provider, model) pairs, expanded from .env
  ├── retry policy        exponential backoff + jitter, Retry-After aware
  ├── circuit breaker     per provider, opens on repeated failures  llm/breaker.py
  ├── usage ledger        provider, model, latency, tokens, outcome  llm/usage.py
  ├── daily cap           LLM_DAILY_CALL_LIMIT_PER_USER, per UTC day
  └── provider adapters
        OpenAiCompatibleClient  nvidia, openrouter, groq, cerebras,
                                mistral, ollama          llm/base.py
        GeminiClient            llm/gemini.py
        CloudflareClient        llm/cloudflare.py
        PuterClient             llm/puter.py
```

The port is deliberately narrow — `LlmClient.complete`, one call — because everything interesting
lives above it. **Adapters never retry, never fail over and never sleep**: one request in, one
typed result out. An adapter that also retried would silently multiply every configured retry
count.

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

### Task profiles

Four tasks, two capabilities: `chat`, `title` and `summarise` are chat-capability tasks and share
`LLM_CHAIN_CHAT` and `<P>_MODELS`; only `vision` uses the vision chain. They differ in sampling
defaults, which live next to the profile — so a caller asks for a *task* and never carries the
numbers. An operator configures two chains, not four.

### Routing rules

The router routes on two questions, and adapters answer them by choosing an error type:

- **`retryable`** — is another attempt at this *same* rung worth making? (429, 5xx, timeout,
  malformed body.)
- **`provider_level`** — is the provider itself broken, rather than this one model? (Rejected
  credentials.)

From those:

- **Retry** covers retryable failures with exponential backoff and full jitter, honouring
  `Retry-After` when the provider sends one. A `Retry-After` longer than 30 s abandons the rung
  instead: a Telegram user is waiting, and the next rung is the faster answer.
- **A model-level failure is never retried.** A 400, an unknown model or a refused prompt will
  answer the same way next time; the rung is abandoned with no delay at all.
- **A provider-level failure opens that provider's circuit immediately** and skips all its
  remaining rungs at once. One rejected key costs one call, not `models x retries` of them.
- **Capability is checked when the ladder is built**, not on the first photo: a text-only provider
  (`cerebras`) and an unbuildable one (`cloudflare` with no account id) are dropped at startup with
  a reason an operator can read.
- Every *attempt* is logged and recorded with provider, model, latency, token usage and outcome;
  `/status` reports the aggregate, the current first rung per task, and any open circuit with its
  remaining cool-down.

### The circuit breaker

Three states, per provider. **Closed**: calls pass, failures are counted. **Open**: every call is
refused for `LLM_CIRCUIT_RESET_S`. **Half-open**: exactly *one* probe is admitted — without that
rule, every request queued during an outage becomes a probe the moment the cool-down expires and
hammers a provider that is still down. A successful probe closes the circuit and forgets the
failure count; a failed one re-opens it for a full fresh cool-down.

The unit is the provider rather than the (provider, model) pair, because the failures worth
short-circuiting are provider-wide: a revoked key, an unreachable host, a sustained outage.

### Cost accounting

Usage is recorded per attempt; the **daily cap counts requests**. One `/summarize` that fails over
across four rungs is one call against the user's budget — charging per attempt would punish a user
for a provider outage they did not cause. Both the ledger and the cap are in-process and reset on
restart, which is the honest scope of a cost *guard* rather than a billing system.

## 7. Session and callback state

The session store is **on the request path, not a cache**: losing an entry means a button already
sitting in a user's chat history stops working. Entries are therefore TTL-bounded rather than
size-bounded.

`SESSION_BACKEND=memory` (default) is a dict with expiry and a periodic sweep, correct for a single
replica, and loses everything on restart. `redis` survives restarts and is shared across replicas,
which is what keeps old keyboards working across a deploy. Values are JSON-serialisable mappings,
because the Redis backend has to round-trip them through a string.

## 8. Runtime and deployment

- `python-telegram-bot` v22.x, async `Application`, **long polling** by default
  (`TELEGRAM_MODE=polling`); `webhook` mode optional for production behind a TLS-terminating
  reverse proxy, with `TELEGRAM_WEBHOOK_SECRET` so a request arriving at the port from anywhere
  else is rejected before it is parsed.
- `AIORateLimiter` to stay inside Telegram flood limits, including the per-chat ones.
- `allowed_updates` is narrowed to messages, edited messages and callback queries: not receiving
  polls and chat-member churn is cheaper and quieter than discarding them.
- Pending updates are dropped on start. An update queued while the bot was down is stale by the
  time it returns, and replaying it acts on a user's intent from an hour ago without them asking.

### Versioning

The version has exactly one source: **the git tag**. `hatch-vcs` derives it at build time and bakes
it into the distribution metadata, which `discoverygram.__version__` reads back — so there is no
version literal anywhere in the source tree to drift.

`.git` is deliberately not in the Docker build context, so `make docker/build` derives the version
with `scripts/version.py` and passes it in as a build argument. That script calls the same
`setuptools_scm` entry point the build backend uses, so the tag stamped on an image cannot disagree
with the version inside it. A plain `docker build` with no argument yields `0.0.0+unknown` rather
than a plausible-looking lie, and CI asserts that the built image reports the version CI computed.

A tagged commit produces that tag (`0.2.0`); anything else produces a PEP 440 development version
pointing at it (`0.2.1.dev4+g1a2b3c4`), so an image built from an untagged commit can never be
mistaken for a release. `make release` refuses to stay quiet about that.
- Small `aiohttp` health endpoint exposing `/healthz` (liveness) and `/readyz`
  (NoteDiscovery reachable + at least one LLM provider healthy).
- Structured JSON logging (`structlog`), correlation id per update.
- Docker image: multi-stage, non-root user, `uv` for dependency install.
- `docker-compose.yml`: the bot plus optional Redis; NoteDiscovery is external.

## 9. Security

- Hard allow-list: every update is rejected unless the Telegram user id is in
  `TELEGRAM_ALLOWED_USER_IDS`. Rejections are logged, not answered with details.
- Secrets only from environment; `.env` is git-ignored, `.env.example` is committed.
- Per-user rate limiting on LLM-backed commands to bound cost.
- Uploaded files are size- and MIME-checked before being sent to a provider.
- Note content is sent to third-party LLM providers only for commands that require it; the
  provider chain is configurable so an `ollama`-only chain keeps data local.
