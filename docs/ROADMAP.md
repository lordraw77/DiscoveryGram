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

## Phase 1 — NoteDiscovery integration layer

Simplified by the phase 0 findings: no runtime capability map, no per-operation transport
resolution. REST is primary; MCP is an optional, flag-gated subset adapter.

**Work**
1. Define the `NoteStore` port and the normalised domain model (`Note`, `NoteRef`, `SearchHit`,
   `TreeNode`).
2. `RestNoteStore` on `httpx.AsyncClient` covering notes, folders, search, tags, templates, media,
   graph/backlinks, stats: connection pooling, timeouts, retry on 5xx/timeout, typed errors
   (`NotFound`, `Unauthorized`, `Forbidden`, `RateLimited`, `Unavailable`, `Unsupported`).
   Optional API key sent as `X-API-Key`; unauthenticated instances must work.
3. **Client-side compensation layer** — the part with real logic:
   - folder tree derived from `GET /api/notes` paths, cached with TTL, invalidated on write;
   - literal filtering for exact search;
   - client-side ranking (title hit before body hit, then term frequency);
   - read-modify-write edit over `POST`, since `PATCH` only appends;
   - explicit `limit` on every search call;
   - throttle for the 60/minute `PATCH` limit, with 429 surfaced as a friendly retry.
4. `McpNoteStore` over an **stdio subprocess**: launch, MCP handshake, tool call mapping,
   supervision and restart, `Unsupported` for anything outside the 18 tools. Defaults to disabled.
5. Startup probe of `GET /api/config` and `/health`: record whether search is enabled, its minimum
   query length, and the instance version; disable affected commands cleanly if search is off.
6. Contract tests against recorded fixtures, plus an opt-in live suite (`pytest -m live`).

**Definition of Done** — every `NoteStore` method works end-to-end against the live instance over
REST; the MCP adapter passes the same suite for its 18 supported operations and reports
`Unsupported` for the rest; a search-disabled instance degrades without errors.

---

## Phase 2 — Telegram bot core

**Work**
1. `python-telegram-bot` v22.x async `Application`; polling by default, webhook mode behind a flag.
2. Allow-list middleware rejecting non-listed user ids before any handler runs.
3. Command router, `/start`, `/help`, `/whoami`, `/cancel`, `/status`.
4. Session store abstraction (`memory` | `redis`) and the opaque callback-token mechanism that
   works around Telegram's 64-byte `callback_data` limit.
5. Global error handler: user-facing friendly message, full detail to logs.
6. `RateLimiter` wired into the application to respect Telegram flood limits.
7. Message rendering utilities: MarkdownV2 escaping, 4096-character chunking, keyboard builders.

**Definition of Done** — an allow-listed user gets a working `/help` and `/status`; a non-listed
user is refused; restarting the container loses no session state when Redis is enabled.

---

## Phase 3 — Search

Shaped by the contract: NoteDiscovery offers **one** search mode, no scores, an optional
server-side disable and a minimum query length. Modes beyond full-text are built client-side.

**Work**
1. Use cases: full-text search (`GET /api/search`), literal search (client-side filter over the
   same call), tag search (`GET /api/tags/{tag}`), tag listing, recent notes
   (`get_recent_notes(days, limit)`).
2. `/search`, `/find`, `/tag`, `/recent`, plus plain-text-message search.
3. Client-side ranking and snippet extraction with term highlighting, since the API returns
   neither scores nor snippets.
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
   requirements.
2. OpenAI-compatible base adapter covering **nvidia, openrouter, groq, cerebras, mistral, ollama**.
3. Dedicated adapters for **gemini**, **cloudflare** (Workers AI, needs account id) and **puter**.
4. Routing chain from `.env` per task profile; providers lacking a required capability are skipped.
5. Retry with exponential backoff + jitter, honouring `Retry-After`; failover to the next provider
   on exhaustion or non-transient error.
6. Per-provider circuit breaker with cool-down and half-open probing.
7. Usage accounting: provider, model, latency, tokens, outcome — logged and surfaced in `/status`.
8. Per-user daily call cap as a cost guard.

**Definition of Done** — with the first two providers in a chain forced to fail, a request still
succeeds through the third; `/status` reports circuit states accurately; a fault-injection test
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
| `POST /api/notes/{path}` may reject instead of overwrite | Edit flow breaks | Confirmed live in phase 0; fallback is delete-then-create wrapped in a single guarded operation |
| MCP subprocess needs the Docker socket | Root-equivalent host access | MCP defaults to disabled; REST loses no functionality. Vendoring the module is the socket-free alternative |
| NoteDiscovery upgrade changes the contract | Silent breakage | Contract doc is version-stamped (0.31.3); startup logs the instance version and warns on mismatch; contract tests run against fixtures |
| Search disabled or min-length floored server-side | `/search` silently useless | Probed at startup via `/api/config`; commands disabled with an explicit message |
| Unbounded `/api/search` with no default limit | Whole-vault response | Explicit `limit` on every call, enforced in the adapter |
| Telegram formatting and size limits | Broken rendering on real notes | Centralised renderer with escaping and chunking, tested against pathological note bodies |
| `callback_data` 64-byte limit | Navigation cannot carry state | Opaque token + server-side session store, designed in phase 2 |
| LLM cost drift | Unbounded spend | Per-user daily caps, usage accounting, local `ollama` as a chain terminator |
| Telegram bot API caps file downloads at 20 MB | Large attachments rejected | Validate early, tell the user the limit, document it |

## Open items

Phase 0's contract discovery is complete. What is still needed:

1. `NOTEDISCOVERY_URL` (with port) and the API key of the live instance — if the instance runs
   unauthenticated, say so, the adapter supports both.
2. BotFather token and the list of Telegram user ids to allow-list.
3. Which LLM providers already have credentials, so the phase 5 chains start from real keys.
4. Confirmation of the two live behaviours listed in phase 0 (`POST` overwrite semantics,
   `search.enabled` and its minimum query length) — these can be checked as the first task of
   phase 0 once credentials are available.
