# DiscoveryGram — Roadmap

**Goal.** Make a NoteDiscovery instance fully usable from Telegram: search it, navigate it, read it,
and create notes in it — including LLM-assisted creation from images and unstructured input.

**Guiding constraints.** Python, async, `.env`-only configuration, English-only code and docs,
Docker-packaged, documentation updated with every change.

Phases are ordered by dependency, not by calendar. Each phase ends with a *Definition of Done* that
must hold before the next phase starts.

---

## Phase 0 — Foundations and contract discovery — **COMPLETE**

**Contract discovery.** NoteDiscovery **0.31.3** was extracted from
`ghcr.io/gamosoft/notediscovery:latest` and documented in
[notediscovery-contract.md](notediscovery-contract.md): full REST surface, all 18 MCP tools,
authentication, search semantics, rate limits, gaps and workarounds. Three findings reshaped the
plan — MCP is a strict subset of REST, MCP is stdio-only, and several assumed capabilities
(exact search, note update, a tree endpoint, relevance scores) do not exist.

**Delivered**

| Item | Where |
|---|---|
| Package skeleton (`bot`, `app`, `ports`, `adapters`, `llm`, `config`, `util`) | `src/discoverygram/` |
| `pyproject.toml` on uv, ruff + mypy **strict**, pytest + pytest-asyncio, coverage | repo root |
| Settings from environment only, fail-fast validation, CSV parsing, cross-field rules | `config/settings.py` |
| `.env.example` covering every variable the code reads | repo root |
| Structured logging (structlog), correlation ids, **secret redaction** | `util/logging.py`, `util/correlation.py` |
| `/healthz` + `/readyz` with a pluggable readiness-check registry | `health.py` |
| Entry point: config load, health server, NoteDiscovery probe, graceful SIGTERM | `__main__.py` |
| Multi-stage Dockerfile, non-root uid 1001, `HEALTHCHECK`, `.dockerignore` | repo root |
| `docker-compose.yml` with an optional Redis profile | repo root |
| `Makefile` (`help install lint format typecheck test check run verify-contract docker/* clean release`) | repo root |
| GitHub Actions CI: lint, type-check, test, image build, lockfile enforced with `--locked` | `.github/workflows/ci.yml` |
| MIT `LICENSE`, referenced from `pyproject.toml` | repo root |
| Live contract probe for the two unresolved behaviours | `scripts/verify_contract.py` |

**Verified, not assumed**

- `make check` green: ruff clean, mypy strict clean on 19 files, **28 tests passing at 91% coverage**.
- Image builds; container runs as uid 1001; `/healthz` returns 200 and `/readyz` correctly returns
  **503** when NoteDiscovery is unreachable; Docker `HEALTHCHECK` reaches `healthy`.
- SIGTERM shuts down gracefully with exit code 0; invalid configuration exits **2** with a readable
  message naming the offending variable.
- `docker compose config` validates both with and without a `.env` present; `make docker/run`
  brings the stack up and `make docker/stop` tears it down cleanly.
- Reproducible builds: `uv.lock` is committed, and both CI and the image use `uv sync --locked`,
  which **fails on a stale lock**. (`--frozen` was tried first and rejected: it consumes the lock
  without validating it against `pyproject.toml`, so it would not have caught drift.)

**Carried into phase 1** — the two live behaviours (`POST /api/notes/{path}` overwrite semantics,
and `search.enabled` with its minimum query length) still need a live instance. `make verify-contract`
probes both and is ready to run the moment credentials exist.

---

## Phase 1 — NoteDiscovery integration layer — **COMPLETE**

Simplified by the phase 0 findings: no runtime capability map, no per-operation transport
resolution. REST is primary; MCP is an optional, flag-gated subset adapter.

**Contract re-verification.** The handler bodies of NoteDiscovery 0.31.3 were read directly from
the image rather than inferred from its route table. That settled both open behaviours and
corrected four phase 0 assumptions, all folded back into
[notediscovery-contract.md](notediscovery-contract.md):

- **`POST /api/notes/{path}` overwrites.** It is the editor's autosave endpoint
  (`create_or_update_note`), rate-limited 300/minute, not 60. The delete-then-create fallback in
  the risk register is not needed.
- **`/api/config` is flat and camelCase** — `searchEnabled`, not `search.enabled` — and it does
  **not** expose the minimum query length. That is a hard-coded server constant of **2**, so the
  bot carries its own `SEARCH_MIN_QUERY_LENGTH` and refuses short queries locally.
- **Search does return snippets**: up to three matched lines per note, HTML-escaped and wrapped in
  `<mark class="search-highlight">`. The markup has to be stripped or it collides with Telegram's
  formatting.
- **`GET /api/notes` returns the folder list** alongside the notes, and `GET /api/notes/{path}`
  already includes backlinks. The derived tree keeps empty folders it would otherwise lose, and
  `/backlinks` costs no extra call over REST.

**Delivered**

| Item | Where |
|---|---|
| `NoteStore` port — 30 operations, grouped by capability | `ports/note_store.py` |
| Normalised domain model, frozen dataclasses | `ports/model.py` |
| Typed errors: `NotFound` `Unauthorized` `Forbidden` `Conflict` `InvalidRequest` `RateLimited` `Unavailable` `Unsupported` | `ports/errors.py` |
| Untrusted-path gate: traversal, control characters, forbidden characters, `.md` inference | `util/paths.py` |
| `RestNoteStore` — notes, folders, search, tags, templates, media, graph/backlinks, export, sharing, stats | `adapters/rest.py` |
| Pooled `httpx.AsyncClient`, retry on timeout/5xx with jitter, optional `X-API-Key`, correlation id propagation | `adapters/rest.py` |
| JSON → model normalisation shared by both transports | `adapters/parsing.py` |
| Client-derived folder tree, TTL cache, write invalidation, single-flight lock, breadcrumbs | `adapters/tree.py` |
| Client-side ranking, snippet cleaning, literal filtering | `adapters/ranking.py` |
| Sliding-window throttle for all 16 rate-limited endpoints | `adapters/throttle.py` |
| `McpNoteStore` over stdio: launch, handshake, tool mapping, restart-on-failure, `Unsupported` for the rest | `adapters/mcp.py` |
| Transport factory, the single place an implementation is chosen | `adapters/__init__.py` |
| Startup probe: reachability, `searchEnabled`, version-drift warning | `app/probe.py` |
| Recorded 0.31.3 payloads + contract tests | `tests/fixtures/`, `tests/test_parsing.py` |
| Opt-in live suite (`make test-live`), writing only under `_DiscoveryGram_live/` | `tests/test_live.py` |

**Verified, not assumed**

- `make check` green: ruff clean, mypy strict clean on 46 files, **189 tests passing at 92%
  coverage**, plus 13 live tests held back behind `-m live` — `rest.py` 93%, `mcp.py` 84%,
  `tree.py` 99%, `throttle.py`, `probe.py` and `note_store.py` 100%.
- Error mapping is asserted per status code, and a 4xx is proven **not** retried — retrying a
  rejected request only spends rate limit.
- `health()` is proven not to walk the retry ladder: it backs `/readyz`, which is polled.
- The edit flow is proven to be read-modify-write, and proven to refuse to resurrect a note that
  was deleted between read and write.
- Every search call is proven to carry an explicit `limit`, and a below-minimum query is proven
  never to leave the process.
- The tree is proven to survive concurrent callers with one load, to keep empty folders, and to be
  dropped by every write.
- All eight REST-only operations are proven to raise `Unsupported` over MCP, naming REST in the
  message.

**Carried into phase 2** — the live suite (`make test-live`) and `make verify-contract` are written
and ready; both need `NOTEDISCOVERY_URL` and, if the instance is authenticated, its API key. Until
then the contract is verified against source and recorded fixtures rather than against a running
vault.

---

## Phase 2 — Telegram bot core — **COMPLETE**

**Work**
1. `python-telegram-bot` v22.x async `Application`; polling by default, webhook mode behind a flag.
2. Allow-list middleware rejecting non-listed user ids before any handler runs.
3. Command router, `/start`, `/help`, `/whoami`, `/cancel`, `/status`.
4. Session store abstraction (`memory` | `redis`) and the opaque callback-token mechanism that
   works around Telegram's 64-byte `callback_data` limit.
5. Global error handler: user-facing friendly message, full detail to logs.
6. `RateLimiter` wired into the application to respect Telegram flood limits.
7. Message rendering utilities: MarkdownV2 escaping, 4096-character chunking, keyboard builders.

**Delivered**

| Item | Where |
|---|---|
| `SessionStore` port — TTL-bounded, JSON-valued, on the request path rather than a cache | `ports/session_store.py` |
| `MemorySessionStore` (expiry, periodic sweep, copy-in/copy-out) and `RedisSessionStore` (lazy import, key prefix, server-side expiry) | `adapters/session.py` |
| Opaque callback tokens: random per issue, `action:token` so a handler routes without a store round trip, plus `revoke` and `extend` | `bot/tokens.py` |
| MarkdownV2/HTML escaping, fenced blocks, paragraph-aware 4096-character chunking, keyboard and pagination builders | `bot/render.py` |
| Allow-list as a `TypeHandler` in group **-1**, with chat allow-listing and a once-per-session refusal | `bot/guard.py` |
| `/start` `/help` `/whoami` `/cancel` `/status`, unknown-command reply, published command menu | `bot/commands.py` |
| Global error handler mapping every port error to one actionable sentence | `bot/errors.py` |
| `BotDeps` container reachable from any handler, typed | `bot/deps.py` |
| Application builder (`AIORateLimiter`, bounded concurrency, narrowed `allowed_updates`) and `BotRunner` driving the manual lifecycle | `bot/application.py` |
| Startup/shutdown ordering, `InvalidToken` → exit 2, other Telegram failures → exit 1 | `__main__.py` |
| **Secret scrubbing by value** in both the structlog and stdlib pipelines | `util/logging.py` |

**Two problems the work surfaced**

- **python-telegram-bot logs the bot token.** It logs the Bot API URL — which contains the token —
  on every request it builds, and a smoke run put it straight into the console. Key-name redaction
  could never catch it, because the secret is embedded in a URL. Fixed by scrubbing the literal
  token and API key from every record in both pipelines, and by quieting the libraries that
  narrate requests. Verified: at `LOG_LEVEL=DEBUG`, neither value appears anywhere in the output.
- **`/status` was unsendable.** Its labels contained unescaped MarkdownV2 reserved characters
  (`Allow-listed`, a trailing full stop), which the Bot API rejects with a 400 — the whole message,
  not the offending character. Caught by a test that asserts every reply would survive the API,
  and fixed by escaping every literal rather than only the interpolated values.

**Verified, not assumed**

- `make check` green: ruff clean, mypy strict clean on 64 files, **305 tests passing at 93%
  coverage**, with 13 live tests still held behind `-m live`.
- The allow-list is proven to sit in group -1 and to raise `ApplicationHandlerStop`, so it cannot
  be bypassed by a handler that forgets to check.
- A stranger is proven to be answered **once**, not on every message, and the refusal is proven to
  leak nothing about the instance, the URL or the other allow-listed ids.
- Every reply is asserted to be MarkdownV2-safe, against titles containing `( ) [ ] ! . -`.
- A note path three times Telegram's callback limit is proven to round-trip through a button.
- Chunking is proven never to exceed 4096 characters and never to drop a character of the body.
- The error handler is proven never to put a traceback, a file path or a secret in the chat, and
  to survive a blocked bot without re-entering itself.
- Startup is proven to bring the health server up **before** probing, and shutdown to release the
  runner, health server, sessions and note store even when startup fails halfway.

**Not verified** — no live Telegram token was available, so nothing here has spoken to the Bot API.
The wiring, lifecycle and rendering are tested; the first real conversation is not.

---

## Phase 3 — Search

Shaped by the contract: NoteDiscovery offers **one** search mode, no scores, an optional
server-side disable and a minimum query length. Modes beyond full-text are built client-side.

**Work**
1. Use cases: full-text search (`GET /api/search`), literal search (client-side filter over the
   same call), tag search (`GET /api/tags/{tag}`), tag listing, recent notes
   (`get_recent_notes(days, limit)`).
2. `/search`, `/find`, `/tag`, `/recent`, plus plain-text-message search.
3. Client-side ranking, already built in phase 1. Snippets come from the API but arrive as HTML
   and are stripped in `adapters/ranking.py`; phase 3 re-highlights the term in Telegram's syntax.
4. Paginated rendering with `◀ / ▶`; cursor state in the session store, TTL-bounded. Always send
   an explicit `limit` — the endpoint has no server-side cap.
5. Handle the real edge cases: query below the minimum length, empty `q`, search disabled (403),
   plugin-replaced result sets.

**Definition of Done** — all four search modes return correct, paginated, navigable results against
the live instance; a query below the minimum length and a search-disabled instance both produce a
clear message rather than an error; pagination survives 20+ page turns without state leaks.

---

## Phase 4 — Navigation

**Work**
1. `/browse` tree navigation with breadcrumb, `⬆ Up`, `🏠 Root`, paginated children — over the
   **client-derived tree**, since NoteDiscovery exposes no tree endpoint.
2. Note rendering: title, path, tags, timestamps, body; `paged` and `split` long-note modes;
   MarkdownV2 escaping and 4096-character chunking.
3. `/open <path>` direct access; wiki-link `[[...]]` buttons that jump between notes.
4. **`/backlinks <path>`** — notes linking to the current one, via `get_backlinks`; surfaced as a
   button on every note. **`/related`** using `GET /api/graph` for graph-adjacent notes.
   Both are capabilities discovered in phase 0 that were not in the original plan.
5. Per-note action bar: `Edit`, `Append`, `Add tag`, `Copy path`, `Show raw`, `Backlinks`,
   `Share`, `Delete` (two-step confirmation).
   - `Edit` is read-modify-write over `POST`; `Append` uses `PATCH` with `add_timestamp`.
   - `Share` uses `POST /api/share/{path}` and returns the public link in the chat.
6. Folder operations: create, rename, move, delete (REST-only capabilities).
7. Seamless transition search → note → parent folder → sibling notes → backlinks.

**Definition of Done** — any note in the vault is reachable from `/browse` in a bounded number of
taps, renders without Telegram formatting errors, and its edit/append/share/delete actions
round-trip correctly against the live instance.

---

## Phase 5 — LLM router

*Deliberately after navigation:* read-only value ships before any provider dependency exists.

**Work**
1. `LlmClient` port; task profiles (`chat`, `vision`, `title`, `summarise`) with capability
   requirements. The **attempt ladder** — ordered (provider, model) pairs expanded from the
   chains and each provider's model list — is already built and unit-tested in
   `llm/plan.py` (delivered early in phase 0, since operators configure it in `.env`).
2. OpenAI-compatible base adapter covering **nvidia, openrouter, groq, cerebras, mistral, ollama**.
3. Dedicated adapters for **gemini**, **cloudflare** (Workers AI, needs account id) and **puter**.
4. Execute the ladder: providers lacking a required capability are skipped at build time.
5. Retry with exponential backoff + jitter, honouring `Retry-After`; on exhaustion advance one
   rung — the next model of the same provider, then the next provider.
6. Per-provider circuit breaker with cool-down and half-open probing. A provider-level failure
   (auth, unreachable, sustained 5xx) skips **all** that provider's remaining rungs at once,
   rather than burning retries on models that cannot answer.
7. Usage accounting: provider, model, latency, tokens, outcome — logged and surfaced in `/status`.
8. Per-user daily call cap as a cost guard.

**Definition of Done** — with the first two rungs of the ladder forced to fail, a request still
succeeds through the third, whether that rung is another model of the same provider or a
different provider; `/status` reports circuit states accurately; a fault-injection test
suite covers 429, 5xx, timeout and malformed-response paths for every adapter.

---

## Phase 6 — Note creation and LLM-assisted ingestion

**Work**
1. Simple creation: `/new <path> <text>`, `/quick <text>` into `INBOX_PATH`.
2. Attachment pipeline: download from Telegram (size and MIME validated against `MAX_UPLOAD_MB`),
   then upload to NoteDiscovery via `POST /api/upload-media` and reference the media path in the
   note body. **This endpoint exists only over REST**, so this flow forces the REST transport.
3. **Image → note**: photo/document plus a natural-language caption
   ("extract the text and create a note under X, generate the title") is parsed into a structured
   intent (target path, whether to OCR, whether to generate title/tags/summary).
4. Vision call for OCR/description; generation calls for title, tags and cleaned body.
5. Path resolution against the real tree, creating intermediate folders when
   `AUTO_CREATE_PARENTS=true`; ambiguous paths are disambiguated with a keyboard.
6. **Preview-before-write**: a draft card with `Save`, `Edit title`, `Change path`, `Regenerate`,
   `Cancel`. Nothing is written to NoteDiscovery without confirmation.
7. Album support (multiple photos → one note); forwarded messages and links through the same
   pipeline.
8. **Template-based creation** — `/new --template <name> <path>` via `create_note_from_template`,
   with `list_templates` / `get_template` backing a template picker keyboard.
9. Provenance metadata (provider, model, source message) stored on generated notes.
10. LLM operations on existing notes: `/summarize <path>`, `/ask <question>` with cited paths.

**Definition of Done** — the headline scenario works end-to-end: send a photo with the caption
*"extract the text and create a note under Projects/Research, you generate the title"*, review the
preview, tap Save, and the note exists at the right path with a sensible title and tags.

---

## Phase 7 — Hardening and production readiness

**Work**
1. Caching of hot reads (note tree, tag list) with explicit invalidation on write.
2. Per-user rate limiting on LLM-backed commands; back-pressure when providers are degraded.
3. Concurrency review: no blocking calls in the event loop, bounded task groups, clean shutdown.
4. Observability: optional Prometheus metrics (updates handled, NoteDiscovery latency, LLM latency
   and failover counts, error rates); `/readyz` reflecting real dependency health.
5. Security pass: secret redaction in logs, input sanitisation, upload validation, path traversal
   checks on note paths, dependency audit.
6. Resilience testing: NoteDiscovery down, MCP session dropped, all providers failing, Telegram API
   throttling.
7. Coverage target ≥ 80% on application and adapter layers.

**Definition of Done** — the service survives every fault-injection scenario without crashing or
losing user state, and reports its degradation honestly through `/status` and `/readyz`.

---

## Phase 8 — Packaging, documentation and release

**Work**
1. Final Docker image: pinned base, non-root, healthcheck, small layers, multi-arch build.
2. `docker-compose.yml` covering bot + optional Redis, with sane defaults and an override example.
3. `Makefile` targets verified end-to-end; release script producing tagged images.
4. Documentation set completed and cross-checked against the code:
   `README.md`, `docs/ARCHITECTURE.md`, `docs/FEATURES.md`, `docs/CONFIGURATION.md`,
   `docs/notediscovery-contract.md`, `docs/OPERATIONS.md` (deploy, upgrade, backup, troubleshoot),
   `docs/LLM_PROVIDERS.md` (per-provider setup and quirks), `docs/DEVELOPMENT.md`,
   `docs/adr/` (architecture decision records), `CHANGELOG.md`.
5. `.env.example` verified: every variable the code reads is present, and nothing extra.
6. Usage walkthrough with real command transcripts for each headline flow.

**Definition of Done** — a new machine can go from `git clone` to a working bot using only
`README.md` and `.env.example`.

---

## Milestones

| Milestone | Phases | Demonstrable outcome |
|---|---|---|
| **M1 — Read-only bot** | 0–4 | Search, browse and read the whole vault from Telegram |
| **M2 — Resilient LLM layer** | 5 | Multi-provider chat and vision with retry and failover |
| **M3 — Full note authoring** | 6 | Image-to-note with generated title, preview and save |
| **M4 — Production release** | 7–8 | Hardened, containerised, fully documented v1.0 |

M1 is the first genuinely useful release and carries no LLM dependency — it is the recommended
early cut-line if scope needs trimming.

## Risk register

| Risk | Impact | Mitigation |
|---|---|---|
| ~~`POST /api/notes/{path}` may reject instead of overwrite~~ | — | **Closed in phase 1.** Confirmed in source: it is an upsert (the editor's autosave endpoint). No fallback needed |
| MCP subprocess needs the Docker socket | Root-equivalent host access | MCP defaults to disabled; REST loses no functionality. Vendoring the module is the socket-free alternative |
| NoteDiscovery upgrade changes the contract | Silent breakage | Contract doc is version-stamped (0.31.3); startup logs the instance version and warns on mismatch; contract tests run against fixtures |
| Search disabled server-side | `/search` silently useless | Probed at startup via `/api/config` (`searchEnabled`); commands disabled with an explicit message |
| Minimum query length is not exposed by the API | Short queries look broken | Carried as `SEARCH_MIN_QUERY_LENGTH` (2) and enforced client-side |
| Unbounded `/api/search` with no default limit | Whole-vault response | Explicit `limit` on every call, enforced in the adapter |
| Telegram formatting and size limits | Broken rendering on real notes | Centralised renderer built in phase 2, tested against pathological bodies. Every reply asserted MarkdownV2-safe — this already caught an unsendable `/status` |
| ~~`callback_data` 64-byte limit~~ | — | **Closed in phase 2.** Opaque token plus server-side session store, proven against a path three times the limit |
| LLM cost drift | Unbounded spend | Per-user daily caps, usage accounting, local `ollama` as a chain terminator |
| Third-party libraries logging our secrets | Token in the logs | Found in phase 2: python-telegram-bot logs the Bot API URL. Literal secret values are scrubbed from both logging pipelines, verified at `LOG_LEVEL=DEBUG` |
| Telegram bot API caps file downloads at 20 MB | Large attachments rejected | Validate early, tell the user the limit, document it |

## Open items

Phases 0, 1 and 2 are complete and were built without live credentials. What is still needed:

1. `NOTEDISCOVERY_URL` (with port) and the API key of the live instance — if the instance runs
   unauthenticated, say so, the adapter supports both.
2. BotFather token and the list of Telegram user ids to allow-list. The bot core is finished and
   tested, but has never spoken to the Bot API; this is what turns that into a running bot.
3. Which LLM providers already have credentials, so the phase 5 chains start from real keys.
4. A run of `make test-live` and `make verify-contract` against the real vault. Both behaviours
   they probe are now confirmed from source; the run is what turns "confirmed in code" into
   "confirmed in production".
