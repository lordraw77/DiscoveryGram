# Changelog

All notable changes to DiscoveryGram.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions are the git
tags themselves — there is no version literal in the source, see
[adr/0007](docs/adr/0007-version-from-git-tag.md).

Entries are grouped by the delivery phase they completed; the phases and their definitions of done
are in [docs/ROADMAP.md](docs/ROADMAP.md).

---

## [Unreleased]

Phase 7 (hardening) and phase 8 (packaging, documentation and release) — milestone **M4**.

### Added

- **Caching** — one TTL cache type behind both hot reads, single-flight, invalidated on write
  (`adapters/cache.py`, `adapters/tree.py`). The tag index is cached on both transports.
- **Per-user burst limit** — `LLM_USER_RATE_PER_MINUTE`, a rolling window alongside the existing
  daily cap. The two answer different questions: the cap bounds spend, the window bounds rate.
- **Back-pressure** — a fully short-circuited provider ladder is refused with its cool-down before
  anything is spent, rather than queuing users behind a dead vendor.
- **Process-wide concurrency bound** on provider calls in flight (`LLM_MAX_CONCURRENT_REQUESTS`).
- **Prometheus metrics** — counters, gauges and histograms with no Prometheus dependency, served at
  `/metrics` on the health port and gated by `METRICS_ENABLED`. Instruments always record; only the
  endpoint is gated. See [adr/0008](docs/adr/0008-metrics-without-prometheus-client.md).
- **Readiness with required and reported checks**, run concurrently and timeout-bounded. A degraded
  AI ladder is reported without failing readiness — see
  [adr/0009](docs/adr/0009-degraded-llm-is-not-unready.md).
- **Upload validation from the bytes**, and filenames reduced to one filesystem-safe segment
  (`util/media.py`).
- `LlmThrottled` and `LlmDegraded`, each mapped to one actionable sentence.
- `make audit` against the advisory database.
- **Multi-arch release images** (`linux/amd64` + `linux/arm64`), with `make docker/buildx`,
  `make docker/push` and `make docker/pins`.
- **Documentation set**: `docs/OPERATIONS.md`, `docs/DEVELOPMENT.md`, `docs/WALKTHROUGH.md`,
  `docs/adr/` (ten records) and this changelog.
- `docker-compose.override.example.yml` — published image, resource limits, log rotation, webhook
  behind a proxy, metrics scraping.

### Changed

- Base images are **pinned by digest** rather than by tag, so a release rebuilds identically. Both
  pins are multi-arch indexes, so this costs nothing on arm64.
- Shutdown runs every teardown step whatever any of them does, so one collaborator that fails to
  close cannot leave the next holding a socket.

### Fixed

- **The back-pressure check was stealing the circuit breaker's half-open probe.** Asking `allows()`
  whether a provider is down *is* taking its single probe permit. The breaker grew `blocks()`, a
  genuinely read-only predicate; the rule is now explicit and asserted: a look must never be a call.
- **An append invalidated too much.** Appending to a note cannot move it, so dropping the tree cache
  cost a full vault listing for nothing — while `#tags` in the appended text mean the tag index
  really is stale. The two caches are now invalidated separately, and the asymmetry is asserted.
- **A failed load could poison a cache.** The value is assigned only on success and the exception
  propagates, so an outage is not cached and the vault coming back needs no restart.
- **`make audit` never worked.** pip-audit cannot resolve the project itself on PyPI and `--strict`
  turned the editable skip into an error. It now audits the exported lockfile: clean across 70
  packages.
- **CI could publish a failing build.** The image job ran in parallel with the quality job, so a
  failing test still pushed `latest`. It now declares `needs: quality`.
- **The image was single-arch despite the multi-arch setup.** QEMU and Buildx were configured but
  `platforms:` was never passed, so only `linux/amd64` was ever produced. `load: true` — which
  cannot hold a manifest list — was removed and the verification step now inspects the published
  manifest for both architectures.

---

## [2.0.0] — phases 5 and 6: the LLM router and note authoring

Milestones **M2** (resilient LLM layer) and **M3** (full note authoring).

### Added

- **LLM router** — nine providers behind one `LlmClient` port: one shared adapter for the six
  OpenAI-compatible ones, dedicated adapters for `gemini`, `cloudflare` and `puter`. A request walks
  a (provider, model) ladder with retry, exponential backoff and `Retry-After` awareness; a
  per-provider circuit breaker skips a provider's remaining models in one step rather than burning
  retries on a rejected key. See [adr/0003](docs/adr/0003-proprietary-llm-router.md).
- **Usage accounting** per provider, reported in `/status`, with a per-user daily cap counted per UTC
  day — a failover costs one call, not one per rung.
- **Image-to-note capture** — send a photo, the bot uploads it, reads it, writes it up and shows a
  **preview card**. Nothing reaches the vault until `Save` is tapped
  ([adr/0004](docs/adr/0004-preview-before-write.md)). Several photos sent together become one note.
- **Deterministic caption parsing** — paths, flags and actions come from rules, never from a model,
  so a photo cannot choose where a note is written
  ([adr/0005](docs/adr/0005-deterministic-intent-parsing.md)).
- `/new`, `/quick`, `/template`, `/summarize` and `/ask`; `/ask` grounds its answer in retrieved
  notes and names its sources.
- The version now comes from the git tag ([adr/0007](docs/adr/0007-version-from-git-tag.md)).

### Fixed

- Attachment size is checked from the update itself, before any download, against `MAX_UPLOAD_MB` —
  Telegram's Bot API caps downloads at 20 MB and the user is told the limit rather than hitting it.

---

## [1.0.0] — phase 4: navigation

Milestone **M1** — the read-only bot, complete and carrying no LLM dependency.

### Added

- Tree browsing with breadcrumbs, derived client-side from the flat listing because NoteDiscovery
  has no tree endpoint ([adr/0002](docs/adr/0002-client-side-compensation.md)).
- Note rendering with `paged` and `split` modes for long notes, and wiki-link buttons; links to
  notes that do not exist are listed as unresolved rather than dropped.
- `/backlinks` and `/related`.
- The per-note action bar: Edit, Append, Tag, Backlinks, Related, Path, Raw, Share, Delete.
- Folder operations via `/folder` and `/move`.

---

## [0.0.3] — phase 3: search

### Added

- All four search modes: full text, literal (`/find`), by tag (`/tag`) and recent (`/recent`).
- Client-side ranking — title match, match count, recency — because the API returns no relevance
  score; term highlighting in snippets.
- Pagination that costs no vault read and leaves one session entry however long the browse.

### Fixed

- `/api/search` has no default limit. An explicit `limit` is enforced in the adapter, and a full page
  is reported as truncated so the user knows results were cut.

---

## [0.0.2] — phase 2: the Telegram bot core

### Added

- The allow-list, enforced in handler group −1 before anything else runs; an empty list is rejected
  at startup ([adr/0010](docs/adr/0010-allow-list-as-access-model.md)).
- Command router: `/start`, `/help`, `/whoami`, `/cancel`, `/status`.
- The session store (memory and Redis) and the callback-token mechanism, which is what makes a
  64-byte `callback_data` limit irrelevant ([adr/0006](docs/adr/0006-opaque-callback-tokens.md)).
- The centralised message renderer and the global error handler.

### Fixed

- **python-telegram-bot logs the Bot API URL, and the token is in it.** Literal secret values are
  scrubbed from both the application and third-party logging pipelines, verified at
  `LOG_LEVEL=DEBUG`.

---

## [0.0.1] — phases 0 and 1: foundations and the NoteDiscovery integration layer

### Added

- Package skeleton, `pyproject.toml` on uv, ruff + mypy strict, pytest with coverage.
- Settings from the environment only, with fail-fast validation and cross-field rules; `.env.example`
  covering every variable the code reads.
- Structured logging with correlation ids and secret redaction.
- `/healthz` and `/readyz` with a pluggable readiness-check registry.
- Multi-stage Dockerfile (non-root uid 1001, `HEALTHCHECK`), `docker-compose.yml` with an optional
  Redis profile, and the `Makefile`.
- The `NoteStore` port and domain model; `RestNoteStore`; the flag-gated `McpNoteStore`
  ([adr/0001](docs/adr/0001-rest-as-primary-transport.md)); the client-side compensation layer; and
  the startup probe.
- `docs/notediscovery-contract.md` — the full REST surface, all 18 MCP tools, authentication, search
  semantics, rate limits, gaps and workarounds of NoteDiscovery 0.31.3.

### Notes

Contract discovery reshaped the plan. MCP turned out to be a strict subset of REST and stdio-only,
and several assumed capabilities — exact search, note update, a tree endpoint, relevance scores — do
not exist at all.

---

[Unreleased]: https://github.com/lordraw77/DiscoveryGram/compare/v2.0.0...HEAD
[2.0.0]: https://github.com/lordraw77/DiscoveryGram/compare/v1.0.0...v2.0.0
[1.0.0]: https://github.com/lordraw77/DiscoveryGram/compare/v0.0.3...v1.0.0
[0.0.3]: https://github.com/lordraw77/DiscoveryGram/compare/v0.0.2...v0.0.3
[0.0.2]: https://github.com/lordraw77/DiscoveryGram/compare/v0.0.1...v0.0.2
[0.0.1]: https://github.com/lordraw77/DiscoveryGram/releases/tag/v0.0.1
