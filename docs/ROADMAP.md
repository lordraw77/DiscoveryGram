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

## Phase 3 — Search — **COMPLETE**

Shaped by the contract: NoteDiscovery offers **one** search mode, no scores, an optional
server-side disable and a minimum query length. Modes beyond full-text are built client-side.

**Delivered**

| Item | Where |
|---|---|
| `SearchService` — full text, literal, tag, recent, tag listing. Degraded modes return a `notice`, not an exception | `app/search.py` |
| `ResultSet` — page arithmetic, clamping, and JSON round-tripping through the session store | `app/search.py` |
| Result rendering: numbered hits, folder, snippets, term highlighting, per-mode empty and header text | `bot/results.py` |
| `/search` `/find` `/tag` `/recent`, plain-message search, and the pagination callback | `bot/search.py` |
| Callback data carrying arguments (`page:<token>:<n>`), so one stored result set serves every page | `bot/tokens.py` |
| Shared `assert_markdown_v2_safe` helper, applied to every rendered reply | `tests/fixtures/telegram.py` |

**Design decisions worth stating**

- **The whole result set is stored once, and page numbers ride in the callback data.** The obvious
  alternative — a token per page button — would create forty session entries over twenty page
  turns. The alternative of re-running the query per page would be worse still: page 2 could then
  disagree with page 1 because a note changed in between. One entry, no vault reads, stable paging.
  The token's TTL is refreshed on every turn, so a long browse does not expire because page one was
  issued an hour ago.
- **Highlighting happens after escaping, not before.** The term is escaped the same way the snippet
  was, so the two still line up, and the occurrences found in the *escaped* text are wrapped in
  bold. Marking up first would let the escaper mangle the markers it had just inserted. The term
  also comes from the vault, so it is regex-escaped: `a.b` must not match `axb`.
- **A 403 at call time beats the startup probe.** An instance can be reconfigured while the bot
  runs, so `Forbidden` from `/api/search` is believed over the cached probe result.
- **Tag and recent do not use `/api/search`,** so they keep working on a search-disabled instance —
  worth knowing when they are the only modes left.
- **`DEFAULT_TEXT_ACTION=quick` is refused with an explanation** rather than silently searching.
  A message the user meant to capture must not quietly become a query; quick capture lands in
  phase 6.

**Verified, not assumed**

- `make check` green: ruff clean, mypy strict clean on 70 files, **384 tests passing at 93%
  coverage**, plus 16 live tests behind `-m live`.
- **Twenty-five page turns are asserted to leave exactly one session entry** — the Definition of
  Done, checked against the store rather than assumed.
- Page slices are proven to cover the result set with no gaps and no overlap, and an out-of-range
  or non-numeric page from a stale button is proven to clamp rather than crash.
- Every page button is proven to stay within Telegram's 64-byte limit at high page numbers.
- Every rendered reply is asserted MarkdownV2-safe, including balanced `*` and `_`, against titles
  like `Q1 2026 — costs (draft) [v2]!` and 400-character snippets.
- Five hits with long titles and long snippets are proven to fit one 4096-character message.
- A too-short query is proven never to reach the vault, and every search is proven to carry an
  explicit `limit`.

**Not delivered here, deliberately** — results carry no per-hit *open* button. Opening a note needs
the note renderer, which is phase 4's first item; adding it now would be doing phase 4. Pagination
is what "navigable" means in this phase.

---

## Phase 4 — Navigation — **COMPLETE**

**Delivered**

| Item | Where |
|---|---|
| `NavigationService` — folder views with paging, wiki-link resolution, backlinks, graph-related notes, folder operations | `app/navigation.py` |
| `NoteService` — append, replace, add tag, move, delete, share | `app/notes.py` |
| Note rendering: header, tags, timestamps, escaped body, `paged` and `split` long-note modes | `bot/notes.py` |
| Folder, backlink and related rendering | `bot/notes.py` |
| `/browse` `/open` `/backlinks` `/related` `/move` `/folder`, the action bar, and the multi-step input flow | `bot/browse.py` |
| Per-hit open buttons on search results — the item phase 3 deferred | `bot/results.py`, `bot/search.py` |
| `update_note` promoted onto the `NoteStore` port | `ports/note_store.py` |

**Design decisions worth stating**

- **One token per *view*, not per button.** Every button on a listing or a note carries an argument
  against the token that view already holds: `nav:<tok>:e3` enters child 3, `nav:<tok>:2` turns the
  page, `act:<tok>:del` acts on the note. The nine-button action bar therefore costs one session
  entry, and paging a sixty-item folder costs none at all. Only *entering* a different view issues
  a new token, because that is genuinely new state.
- **The same fix was applied retroactively to search.** Phase 3's open buttons were first written
  to issue a token per hit; a test showed that turning twenty pages then left 100+ session entries.
  They now carry an index into the stored result set instead.
- **A listing is rebuilt from its token, not re-derived from the tree.** A page turn should not
  depend on the vault being reachable, and page 2 must not show a different set of children than
  the page the reader came from.
- **Adding a tag is idempotent**, because tags live in the body text rather than a field. Tapping
  twice must not leave the note tagged twice — NoteDiscovery's index would show both. Detection
  ignores fenced code, so a `# comment` in a shell snippet is not mistaken for a tag.
- **Delete asks first, and the confirm button disarms itself.** Its token is revoked as soon as the
  delete succeeds, so a double tap cannot try again against a note that is already gone.
- **A broken `[[link]]` is named rather than silently dropped.** A dangling link is a fact about
  the vault worth surfacing.

**Two bugs the tests caught**

- **The note → folder step was broken** by the token rework: the "⬆ Folder" button pointed at a
  folder *path*, while the listing callback had come to expect a stored listing. Such a token is
  now recognised as a pointer and the folder is loaded fresh.
- **The MarkdownV2 safety assertion was wrong**, not the renderer. It stripped code spans with a
  regex, so an *escaped* backtick in a note body looked like a code-span opener and a valid message
  was reported as broken. It is now a single escape-aware pass, which also checks that backticks
  and markers balance — a stronger check than the one it replaced.

**Verified, not assumed**

- `make check` green: ruff clean, mypy strict clean on 78 files, **517 tests passing at 93%
  coverage**, plus 21 live tests behind `-m live`.
- **Every note is proven reachable from the root** by walking its path segment by segment — the
  Definition of Done, asserted for every note in the fixture vault and, in the live suite, for real
  ones.
- **Every reserved MarkdownV2 character is tested individually** inside a note body, so a failure
  names the character that broke it. A body containing a markdown table, a code fence, links, an
  image, emoji and accents renders safely.
- Long notes are proven to page within 4096 characters on **every** page, and escaping is proven to
  happen before chunking — a boundary between a backslash and its character would break the message.
- Paging a sixty-item folder eleven times is proven to leave the session store at exactly one entry.
- Delete is proven not to act before confirmation, and proven not to act twice on a double tap.
- Folder rename over MCP is proven to explain the gap rather than fail obscurely.

**Not verified** — no live vault or Bot API token was available. The action round-trips
(`edit → append → tag → share → delete`) are written as live tests and run on `make test-live`.

---

## Phase 5 — LLM router — **COMPLETE**

*Deliberately after navigation:* read-only value ships before any provider dependency exists.

**Delivered**

| Item | Where |
|---|---|
| `LlmClient` port — one call, `complete`; message/image/usage model | `ports/llm.py` |
| Typed errors carrying two routing answers: `retryable`, `provider_level` | `ports/llm_errors.py` |
| Task profiles `chat` `vision` `title` `summarise`, with per-task sampling defaults | `llm/plan.py` |
| OpenAI-compatible adapter — **nvidia, openrouter, groq, cerebras, mistral, ollama** | `llm/base.py` |
| `GeminiClient` — header auth, model in the path, no system role, `inline_data` images | `llm/gemini.py` |
| `CloudflareClient` — account id in the URL, `200`-with-`success:false` failures | `llm/cloudflare.py` |
| `PuterClient` — the `drivers/call` RPC dialect, several back-end response shapes | `llm/puter.py` |
| The ladder walker: retry, backoff with full jitter, `Retry-After`, rung and provider advance | `llm/router.py` |
| Per-provider circuit breaker: closed / open / half-open with a single probe | `llm/breaker.py` |
| Usage ledger (provider, model, latency, tokens, outcome) and the per-user daily cap | `llm/usage.py` |
| Build-time assembly: which clients exist, which rungs survive the capability check | `llm/factory.py` |
| `/status` AI section: first rung per task, open circuits with cool-down, usage, quota | `bot/commands.py` |
| Per-provider setup, quirks, chain design and failure triage | [LLM_PROVIDERS.md](LLM_PROVIDERS.md) |
| Opt-in live provider suite, skipping itself when no chain is configured | `tests/test_live_llm.py` |

**Design decisions worth stating**

- **Adapters never retry, never fail over, never sleep.** One request in, one typed result out.
  The router owns the ladder, and an adapter that also retried would silently multiply every
  configured retry count — `LLM_RETRIES_PER_MODEL=3` would mean nine calls, not four.
- **The router routes on two questions, not on status codes.** Adapters answer *is another attempt
  at this rung worth making?* and *is the provider itself broken, rather than this model?* by
  choosing an error type. Getting that split right is what stops a bad API key from burning nine
  retries across three models that were never going to answer: a 401 costs **one** call.
- **A 429 is retryable but not provider-level.** Quotas are usually per model, so the next model of
  the same provider often still has budget — treating a rate limit as a provider outage would
  throw away the rest of a working provider.
- **A malformed 200 never trips the breaker.** Sampling can produce an empty completion, so it is
  retryable; but the provider is *answering*, and counting it against the circuit would
  short-circuit a provider that is healthy.
- **The half-open state admits exactly one probe.** Without that rule, every request queued during
  an outage becomes a probe the instant the cool-down expires, and they all hit a provider that is
  still down. A failed probe re-opens for a full fresh cool-down.
- **A long `Retry-After` abandons the rung rather than sleeping through it.** Beyond 30 seconds,
  the next rung is the faster answer, and a Telegram user is waiting.
- **Capability is checked when the ladder is built, not on the first photo.** `cerebras` is
  text-only and `cloudflare` needs an account id its key does not carry; both are dropped at
  startup with a reason in the log and in `/status`, rather than failing at request time.
- **The daily cap counts requests, not attempts.** One `/summarize` that fails over across four
  rungs is one call against the user's budget — charging per attempt would punish a user for a
  provider outage they did not cause. A request that never reaches a provider costs nothing.
- **`title` and `summarise` share the chat chain.** Four tasks, two capabilities: an operator
  configures two chains, and the sampling differences live next to the task profile so no caller
  carries the numbers.

**Two problems the work surfaced**

- **`discoverygram.llm` could not re-export the router.** `Settings` imports `llm.plan` to build
  its ladders, and the router imports `Settings` — so re-exporting `router` and `factory` from the
  package `__init__` closed an import cycle that broke every test at collection time. The package
  now re-exports only `plan`; the router is imported from its module, which is also where the
  docstring explaining it lives.
- **The test suite was reading the developer's real provider keys.** `conftest`'s isolation
  fixture cleared a hand-listed set of variables, and the nine providers x five variables were not
  in it — so a machine with `GROQ_API_KEY` set would build a different ladder than CI. The fixture
  now clears every `<PROVIDER>_*` variable, generated from `KNOWN_PROVIDERS`.

**Verified, not assumed**

- `make check` green: ruff clean, mypy strict clean on 95 files, **706 tests passing at 94%
  coverage**, plus 26 live tests behind `-m live` — `router.py` 99%, `usage.py` and `plan.py`
  100%, `breaker.py` 97%, `base.py` 94%, `factory.py` 97%.
- **The Definition of Done is asserted twice**: with the first two rungs forced to fail, the third
  serves — once where the third rung is a different provider, and once where it is another model
  of the same provider.
- **A rejected API key is proven to cost one call**, not one per model: the remaining rungs of that
  provider are skipped in a single step and the next provider answers.
- An open circuit is proven to skip its provider **without calling it at all**, and a successful
  call is proven to close a circuit that had been accumulating failures.
- Backoff is proven to grow `1, 2, 4, 8, 16` and to cap; `Retry-After` is proven to win over it,
  and a one-hour `Retry-After` is proven to advance the rung with no sleep.
- A model-level failure is proven **never** to be retried, however high `LLM_RETRIES_PER_MODEL` is.
- **Every adapter is fault-injected** across 401, 403, 404, 429, 400, 5xx, timeout, connection
  error and six shapes of malformed 200 — including the `200`-with-`success:false` that Cloudflare
  and Puter use to report failure, which no status-code check would catch.
- Gemini is proven to send its key in a header and never in a query string, to lift the system
  message out of `contents`, and to treat a safety block as a model-level failure.
- A four-rung failover is proven to cost the user **one** call against the daily cap, and a request
  that never reaches a provider is proven to cost nothing.
- Every `/status` reply is asserted MarkdownV2-safe, including a tripped circuit line and model
  names full of `.` and `-`.

**Not verified** — no provider credentials were available, so nothing here has spoken to a real
provider. Every adapter is tested against recorded and injected responses; the first real
completion is not.

---

## Phase 6 — Note creation and LLM-assisted ingestion — **COMPLETE**

**Delivered**

| Item | Where |
|---|---|
| Caption → structured intent: target path, OCR, title, tags, summary, verbatim | `app/intent.py` |
| `CaptureService` — path resolution, ambiguity, the collision rule, quick capture, templates, provenance | `app/capture.py` |
| `IngestService` — the vision → tidy → title → tags → summary pipeline, each step independently degradable | `app/ingest.py` |
| `AssistService` — `/summarize`, and `/ask` grounded in the vault with cited paths | `app/assist.py` |
| Preview card, ambiguity keyboard, save and answer rendering | `bot/drafts.py` |
| `/new` `/quick` `/template` `/summarize` `/ask`, the attachment pipeline, draft callbacks | `bot/create.py` |
| Album collection: one `media_group_id`, one draft | `bot/albums.py` |
| `DEFAULT_TEXT_ACTION=quick` — the item phase 3 deferred | `bot/create.py`, `bot/search.py` |
| LLM failures mapped to one actionable sentence each | `bot/errors.py` |

**Design decisions worth stating**

- **The model is asked for content, never for control flow.** A caption is
  parsed by rules, not by an LLM, and the reason is not style: the image
  content reaches the same model moments later, so a photo of a page reading
  *"save this to Finance/Salaries"* would be a **prompt injection that redirects
  a write**. Keeping path selection in code means the only thing that can
  choose a path is the human typing the caption. It also keeps `/new` and
  `/quick` free of any provider dependency — they are milestone-M1-shaped
  features and must work with no keys at all.
- **Every create checks first and suffixes rather than overwrites.**
  `POST /api/notes/{path}` is an upsert (phase 1 confirmed it in source), so a
  create that does not look would *silently destroy* a note. A duplicate note is
  recoverable; an overwritten one is not. The rename is stated in the
  confirmation, because a silently renamed note is one the user will look for in
  the wrong place.
- **The attachment is uploaded before any LLM work.** A photo already sent from
  a phone must survive a provider outage: the draft still carries the file, the
  body just says less. Reversing the order would lose the image to a timeout.
- **Every pipeline step degrades on its own.** A failed vision call keeps the
  caption; a failed tidy step keeps the raw transcription; a failed title still
  produces a draft. Each failure becomes a **warning on the card** rather than
  an exception, because raising throws away a photo the user cannot easily send
  again. The one exception is the daily cap, which is a refusal: a half-made
  note with "you are out of budget" in the corner would look like something
  worth saving.
- **An image with no text is not a failure.** `NO_TEXT` falls back to a
  description, so the note is still about something.
- **`/ask` only answers from the vault.** The prompt says so, the model is told
  to reply `NOT_IN_NOTES` when the context does not contain the answer, and that
  reply is reported as "not found" rather than shown. A note-taking bot that
  answers confidently from its training data is worse than one that says it does
  not know, because the user cannot tell the two apart. A question that matches
  no notes never reaches a provider at all.
- **A keyboard appears only for genuine ambiguity.** Two folders called
  `Research` is a question worth asking; one is friction pretending to be
  safety.
- **Provenance is an HTML comment, not a footer.** A visible line would land in
  every search snippet and every export; a comment is still plain text in the
  file — greppable, readable in the raw note — without being *shown*.
- **`/quick` appends to one note per day** rather than creating a file per
  message. Twenty thoughts in an afternoon should be one page to read back.
- **One token per draft**, following the rule phase 4 established for the action
  bar: six buttons, one session entry.

**Two problems the work surfaced**

- **Two modules wanted the same pending-input key.** The draft flows and the
  browse flows both consume "the next message you send", and browse's handler
  popped the key before checking whose it was — so a pending draft title would
  have been swallowed and dropped. The capture handler is now registered first
  and returns *without popping* for a kind it does not own. They deliberately
  still share `PENDING_KEY`, because that is what makes `/cancel` clear every
  flow in one place rather than growing a branch per flow.
- **The album contract had a race in it.** Telegram sends each photo of an album
  as a separate update, so "first update takes the group" only works if the
  group entry is created **before** the first `await`. Written the obvious way —
  check, await, insert — two photos arriving together would both have believed
  they were first and produced two notes.

**Verified, not assumed**

- `make check` green: ruff clean, mypy strict clean on 109 files, **936 tests
  passing at 93% coverage**, plus 31 live tests behind `-m live` — `capture.py`
  97%, `intent.py` 97%, `drafts.py` and `albums.py` 100%, `create.py` 90%.
- **The Definition of Done is asserted end to end**: a photo captioned *"extract
  the text and create a note under Projects/Research, you generate the title"*
  produces a preview showing the path, the generated title and generated tags —
  with **nothing written** — and tapping `Save` creates
  `Projects/Research/Q1 planning.md` with the title, the body and the tags.
- **Nothing reaches the vault before `Save`** is asserted on the cancel path, on
  the ambiguity path and on the headline path, against the store rather than
  assumed.
- A double tap on `Save` is proven to create the note **once**: the token is
  revoked the moment the write succeeds.
- `/new` is proven not to overwrite an existing note, and proven to work with no
  LLM configured at all.
- An oversized file is proven to be refused **without being downloaded** —
  Telegram reports the size, so the limit costs no transfer.
- A transport that cannot upload media is proven to still produce a note, naming
  `NOTEDISCOVERY_TRANSPORT=rest` in the warning.
- Three photos sent as an album are proven to become **one** draft and two
  uploads.
- Every reserved MarkdownV2 character is tested individually inside a draft
  title, and a card carrying a table, a fence, an image and `Q1 2026 — costs
  (draft) [v2]!` is proven sendable.
- `.env.example` is asserted **mechanically** to document every variable the
  code reads and nothing more — in both directions.

**Not verified** — no live vault, Bot API token or provider key was available.
The vault half of capture is written as live tests (`make test-live`): creating,
the collision rule against a real upsert, quick-capture appending, and the
media-upload round trip that image-to-note depends on.

---

## Phase 7 — Hardening and production readiness — **COMPLETE**

**Delivered**

| Item | Where |
|---|---|
| One TTL cache type behind both hot reads, single-flight and write-invalidated | `adapters/cache.py`, `adapters/tree.py` |
| Tag index cached on both transports; every write drops tree **and** tags, an append drops tags only | `adapters/rest.py`, `adapters/mcp.py` |
| Per-user burst limit (`LLM_USER_RATE_PER_MINUTE`), rolling window | `llm/usage.py` |
| Back-pressure: a fully short-circuited ladder is refused with its cool-down, before anything is spent | `llm/router.py`, `llm/breaker.py` |
| Process-wide bound on provider calls in flight (`LLM_MAX_CONCURRENT_REQUESTS`) | `llm/router.py` |
| Prometheus exposition with no Prometheus dependency: counters, gauges, histograms | `util/metrics.py` |
| `/metrics` on the health port, gated by `METRICS_ENABLED`; instruments always live | `health.py` |
| `/readyz` with required and *reported* checks, concurrent, timeout-bounded, briefly cached | `health.py` |
| Shutdown that runs every teardown step whatever any of them does | `__main__.py` |
| Upload validation from the bytes, and a filename reduced to one safe segment | `util/media.py`, `bot/create.py`, `adapters/rest.py` |
| `LlmThrottled` and `LlmDegraded`, each mapped to one actionable sentence | `ports/llm_errors.py`, `bot/errors.py` |
| `make audit` against the advisory database | `Makefile` |

**Design decisions worth stating**

- **A degraded AI ladder is not a readiness failure.** `/readyz` reports it and
  stays `ready`. An orchestrator that pulls the bot out of service because a
  third-party model provider is having a bad afternoon has turned a partial
  outage into a total one — search, browse, read and `/new` need no provider at
  all. That is what the `required=False` check exists for.
- **Instruments are always recording; only the endpoint is gated.** Turning
  `METRICS_ENABLED` on gives numbers from a running process rather than from
  zero, and no counter site in the codebase has to ask whether it is switched
  on.
- **No metric label is anything a user chooses.** Not a note path, not a query,
  not a user id — only provider names, HTTP methods and outcomes from fixed
  sets. A label taken from user input is how a metrics endpoint becomes an
  out-of-memory, and the note path was *right there* in the obvious version of
  the NoteDiscovery latency metric.
- **Two limits, because they answer different questions.** The daily cap bounds
  spend; a rolling per-minute limit bounds rate. A daily cap alone lets one user
  empty their allowance in ten seconds and hold the shared provider connection
  pool while doing it. The burst default is deliberately loose (20), because one
  photo capture is *five* calls and a limit that refuses halfway through leaves
  a half-written draft the user cannot finish.
- **Refusing costs nothing.** Throttling, the daily cap and back-pressure all
  check *before* consuming, and consume only once a provider is actually going
  to be called. Charging a user for a request the bot declined to attempt would
  be indefensible.
- **Prometheus without `prometheus_client`.** The exposition format is a few
  lines of text and the whole need here is three instrument types with
  low-cardinality labels. The dependency would have brought a second global
  registry, a second concurrency model and a WSGI server to keep out of the
  event loop, to produce the same forty lines.
- **The bytes decide what a file is.** `mime_type` and `file_name` are both
  claims made by the sending client. A declared `image/png` that begins `%PDF`
  is refused before it costs a vision call; a real PNG mislabelled as JPEG is
  corrected instead, because phones mislabel images constantly and refusing
  those would be a bug, not a control.

**Three problems the work surfaced**

- **The back-pressure check was stealing the half-open probe.** Asking the
  breaker `allows()` to find out whether a provider is down *is* taking its
  single probe permit — that is the method's documented side effect. The first
  version of the check therefore consumed the one attempt that decides whether a
  provider has recovered, and the request that followed found the circuit shut
  again. The breaker grew `blocks()`, a genuinely read-only predicate, and the
  rule is now explicit: a look must never be a call.
- **An append changes the tags but not the tree.** The obvious invalidation —
  one method, both caches, every write — is wrong in one direction and right in
  the other: appending to an existing note cannot move it, so dropping the tree
  would cost a full vault listing for nothing, while `#tags` in the appended
  text mean the tag index really is stale. They are invalidated separately, and
  the asymmetry is asserted.
- **A failed load must not poison the cache.** Assigning the loader's result
  before it succeeds — or caching the exception — would have kept the bot broken
  after the vault came back. The value is assigned only on success, the
  exception propagates, and the next caller tries again.

**Concurrency review** — no blocking call reaches the event loop: no
`time.sleep`, no synchronous file or socket I/O, no fire-and-forget
`create_task` anywhere in `src/`. The album buffer creates its group entry
**before** its first `await`, which is what makes "first update wins" true. The
caches serialise their loaders on a lock, the throttle limiter on a lock per
bucket, and the router now bounds provider calls process-wide. Shutdown runs
every step even when one raises, so a collaborator that fails to close cannot
leave the next one holding a socket.

**Verified, not assumed**

- `make check` green: ruff clean, mypy strict clean on 112 files, **1030 tests
  passing at 94% coverage** — comfortably past the 80% target, with
  `health.py`, `usage.py` and `media.py` at 100%, `router.py` at 99% and
  `metrics.py` at 99%.
- **Every fault-injection scenario is a test** (`tests/test_resilience.py`):
  NoteDiscovery refusing connections, flapping between 503 and 200, rate-limiting
  with a `Retry-After`, a Redis that raises on `ping`, every provider failing,
  and Telegram answering a notification with 429.
- The full degradation arc is asserted end to end: repeated failures trip the
  circuit, the next request is refused **without calling anything**, and after
  the cool-down the same provider serves again.
- A failed write is proven **not** to invalidate the caches, and an outage is
  proven not to be cached — the vault coming back needs no restart.
- A readiness check that hangs is proven to answer `503` within its timeout
  rather than becoming a probe that times out, and three slow checks are proven
  to run concurrently.
- `/metrics` is proven to be a `404` when disabled, and the exposition is
  asserted against the format itself: cumulative buckets, a trailing newline,
  and a label value containing `"`, `\` and a newline that cannot break the
  scrape.
- A filename of `../../../etc/cron.d/evil.png` is proven to reach NoteDiscovery
  as `evil.png`, asserted against the multipart body actually sent.
- A PDF announced as `image/png` is proven to be refused **after** download and
  **before** any upload or provider call.
- The half-open probe is proven to survive the back-pressure check — the
  regression that motivated `blocks()`.

**Not verified** — still no live instance, Bot API token or provider key. The
metrics endpoint has been scraped by a test client, never by a Prometheus; the
fault-injection scenarios are injected at the adapter seams rather than by
unplugging a real NoteDiscovery. `make audit` needs network access and has not
been run here.

---

## Phase 8 — Packaging, documentation and release — **COMPLETE**

**Delivered**

| Item | Where |
|---|---|
| Base images pinned by **digest**, both multi-arch indexes; non-root uid 1001, `HEALTHCHECK`, cached dependency layer | `Dockerfile` |
| Multi-arch release build (`linux/amd64` + `linux/arm64`), and a target that refuses to push a non-release version | `Makefile` (`docker/buildx`, `docker/push`) |
| A target that prints the current base digests, so refreshing a pin is deliberate rather than a rebuild side effect | `Makefile` (`docker/pins`) |
| Deployment override example: published image, resource limits, log rotation, webhook behind a TLS proxy, metrics scraping | `docker-compose.override.example.yml` |
| Operations manual: deploy, upgrade, rollback, backup, observability, a troubleshooting index keyed by symptom | `docs/OPERATIONS.md` |
| Contributor guide: toolchain, layout, layering rules, conventions, how to add a command / setting / provider / metric | `docs/DEVELOPMENT.md` |
| Ten architecture decision records, each naming the alternative that was rejected | `docs/adr/` |
| Changelog grouped by the phase each release completed | `CHANGELOG.md` |
| Usage walkthrough with the bot's actual replies | `docs/WALKTHROUGH.md` |
| README rewritten from "M1 complete" to feature-complete, with a capability table and an honest live-credentials caveat | `README.md` |

**Three defects the packaging work surfaced** — all three were dead code paths that
looked correct in review and only failed when run.

- **`make audit` had never worked.** It was listed as delivered in phase 7 and
  documented as "not run here". Run, it fails: pip-audit tries to resolve
  `discoverygram` itself on PyPI, and `--strict` promotes even the
  `--skip-editable` skip to an error. It now audits the **exported lockfile**
  (`uv export --no-emit-project`) and reports clean across 70 packages. A
  quality gate that has never been executed is not a quality gate.
- **CI could publish a failing build.** The `image` job carried no
  `needs: quality`, so the two jobs ran in parallel and a red test suite still
  pushed `latest` to Docker Hub. The gate existed and was wired to nothing.
- **The image was single-arch despite looking multi-arch.** The workflow set up
  QEMU *and* Buildx — the whole apparatus — but never passed `platforms:`, so
  every release since the beginning was `linux/amd64` only. Adding it also
  required removing `load: true`, which cannot hold a manifest list, and
  rewriting the version check to inspect the *published* manifest instead of a
  locally loaded image that no longer exists.

**Design decisions worth stating**

- **Digests, not tags, for base images.** A tag is a moving pointer; an image
  that rebuilds identically from an identical commit is what makes a release
  reproducible. Both pins are multi-arch indexes, so pinning costs nothing on
  arm64 — and `make docker/pins` makes refreshing them a deliberate commit
  rather than something that happens silently on a rebuild.
- **`docker/buildx` builds both architectures and keeps neither.** A
  multi-arch result cannot be `--load`ed into the local image store, so the
  target exists to prove the arm64 build compiles; `docker/push` is what
  produces a usable artefact. Naming that in the target's own comment is
  cheaper than the next person rediscovering it.
- **The override file ships commented out, in full.** Every block is a real
  deployment concern with the reasoning next to it, and none of it is active.
  An override that repeats a default is a second place to keep it correct.
- **The walkthrough's transcripts are generated, not written.** The replies in
  `docs/WALKTHROUGH.md` came out of the real renderers driven with sample vault
  data. Where a transcript could not be produced mechanically it is marked
  `(structure)` and describes the sections instead of quoting text that was
  never emitted. Invented transcripts are how documentation starts lying.

**Verified, not assumed**

- `make check` green: **1030 tests at 94% coverage**, ruff and mypy strict clean.
- `make audit`: **no known vulnerabilities** across 70 locked packages — the
  first time this target has ever produced a result.
- `make docker/buildx` **run to completion**: `linux/amd64` and `linux/arm64`
  both build from the pinned digests, exit 0.
- `make docker/pins` output **matches the digests in the `Dockerfile`** exactly.
- `docker compose config` valid for the base file and for base + override.
- `.env.example` **verified programmatically** against `Settings`: every field
  the code reads is present, and every extra name is a provider variable read
  dynamically by `load_provider_configs`. Nothing missing, nothing stale.
- The CI workflow parses, `image.needs == quality`, and the build step carries
  both platforms with no `load`.

**Not verified** — the CI workflow itself has not run: it triggers on tags, and
tagging is a release decision rather than something to do to test a pipeline.
The multi-arch build is proven locally; the *push* path and the manifest
verification step have not executed against Docker Hub. And the live-credential
gap from phase 7 is unchanged — no instance, no token, no provider key.

**Definition of Done** — a new machine can go from `git clone` to a working bot using only
`README.md` and `.env.example`. **Met**, with one caveat that belongs to the environment rather than
the documentation: the walkthrough from clone to *running* bot is written and complete, but has
never been executed by someone who was not the author, because doing so needs the three credentials
below.

---

## Milestones

| Milestone | Phases | Demonstrable outcome |
|---|---|---|
| **M1 — Read-only bot** ✅ | 0–4 | Search, browse and read the whole vault from Telegram |
| **M2 — Resilient LLM layer** ✅ | 5 | Multi-provider chat and vision with retry and failover |
| **M3 — Full note authoring** ✅ | 6 | Image-to-note with generated title, preview and save |
| **M4 — Production release** ✅ | 7–8 | Hardened, containerised, fully documented release |

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
| ~~Unbounded `/api/search` with no default limit~~ | — | **Closed in phase 3.** Explicit `limit` enforced in the adapter and asserted in the service; a full page is reported as truncated so the user knows results were cut |
| Telegram formatting and size limits | Broken rendering on real notes | Centralised renderer, every reply asserted MarkdownV2-safe against every reserved character individually, tables, fences and 4096-character boundaries. Has already caught an unsendable `/status` and a faulty assertion |
| ~~`callback_data` 64-byte limit~~ | — | **Closed in phase 2.** Opaque token plus server-side session store, proven against a path three times the limit |
| ~~LLM cost drift~~ | — | **Closed in phase 5, tightened in phase 7.** Per-user daily cap counted per UTC day (failover costs one call, not one per rung), plus a rolling per-minute burst limit and a process-wide bound on calls in flight. Usage is accounted per provider and reported in `/status`; local `ollama` is documented as a chain terminator |
| ~~A provider outage becomes a queue of waiting users~~ | — | **Closed in phase 7.** A fully short-circuited ladder is refused immediately with its cool-down, spending nothing, and the degradation is named in `/status`, in `/readyz` and in `discoverygram_llm_circuit_open` |
| ~~A metrics label taken from user input~~ | — | **Closed in phase 7.** No label carries a note path, a query or a user id — only provider names, methods and outcomes from fixed sets. Asserted by the label set each instrument is constructed with |
| ~~A client-supplied filename used as a path~~ | — | **Closed in phase 7.** Reduced to one filesystem-safe segment before it leaves the process, asserted against the multipart body actually sent |
| Third-party libraries logging our secrets | Token in the logs | Found in phase 2: python-telegram-bot logs the Bot API URL. Literal secret values are scrubbed from both logging pipelines, verified at `LOG_LEVEL=DEBUG` |
| Telegram bot API caps file downloads at 20 MB | Large attachments rejected | **Closed in phase 6.** Size is checked against `MAX_UPLOAD_MB` from the update itself, before any download, and the user is told the limit |

## Open items

**Every phase is complete**, and with phase 8 so is milestone **M4**. All eight
were built without live credentials, and that — not any remaining feature — is
the whole of what stands between a bot that is written and a bot that is
running:

1. `NOTEDISCOVERY_URL` (with port) and the API key of the live instance — if the
   instance runs unauthenticated, say so, the adapter supports both.
2. BotFather token and the list of Telegram user ids to allow-list. The bot is
   finished and tested, but has never spoken to the Bot API; this is what turns
   that into a running bot.
3. **At least one LLM provider key, and ideally two from different companies.**
   `make check-env` prints the exact ladder a set of keys produces. `ollama`
   needs no key at all and is the cheapest way to see image-to-note work end to
   end.
4. A run of `make test-live` and `make verify-contract` against the real vault.
   Both behaviours they probe are confirmed from source; the run is what turns
   "confirmed in code" into "confirmed in production".

Two smaller things are deliberately left open rather than forgotten:

- **The release pipeline has never run.** CI triggers on tags, and the multi-arch
  push plus its manifest verification will execute for the first time on the
  next `git push --tags`. The build itself is proven locally on both
  architectures; the publish path is not.
- **CI has no pull-request or branch trigger** (removed deliberately in
  `78ce839`). `make check` is therefore the only gate on a change until someone
  tags a release. That is a defensible choice for a single-maintainer project
  and a bad one the moment it stops being single-maintainer.

Beyond that: the roadmap is finished. Further work is new scope, and the place
to argue for it is a new phase or an ADR, not an open item on a closed plan.
