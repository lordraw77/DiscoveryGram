# DiscoveryGram Documentation

DiscoveryGram makes a **NoteDiscovery** instance fully usable from **Telegram**: search, browse,
read and create notes — including LLM-assisted creation from images and unstructured input.

| Document | Purpose |
|---|---|
| [WALKTHROUGH.md](WALKTHROUGH.md) | Every flow end to end, with the bot's actual replies |
| [FEATURES.md](FEATURES.md) | Functional catalogue: commands, flows, UX behaviour |
| [CONFIGURATION.md](CONFIGURATION.md) | Full `.env` reference |
| [OPERATIONS.md](OPERATIONS.md) | Deploy, upgrade, back up, observe, troubleshoot |
| [DEVELOPMENT.md](DEVELOPMENT.md) | Toolchain, layout, conventions, how to add things |
| [LLM_PROVIDERS.md](LLM_PROVIDERS.md) | Per-provider setup, dialect quirks, chain design, failure triage |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Components, layering, data flow, key design decisions |
| [adr/](adr/README.md) | Architecture decision records — the *why*, and what was rejected |
| [notediscovery-contract.md](notediscovery-contract.md) | Verified REST + MCP contract of NoteDiscovery 0.31.3 |
| [ROADMAP.md](ROADMAP.md) | Delivery plan: phases, milestones, deliverables, risks |
| [../CHANGELOG.md](../CHANGELOG.md) | What changed, per release |
| [dockerhub-overview.md](dockerhub-overview.md) | Text published as the Docker Hub repository overview |

## Where to start

- **Running it** → [../README.md](../README.md) quick start, then [OPERATIONS.md](OPERATIONS.md).
- **Using it** → [WALKTHROUGH.md](WALKTHROUGH.md).
- **Changing it** → [DEVELOPMENT.md](DEVELOPMENT.md), then [ARCHITECTURE.md](ARCHITECTURE.md).
- **Understanding a decision that looks odd** → [adr/](adr/README.md). It probably has a record, and
  the record names the alternative that was rejected.

## Project constraints (non-negotiable)

- Implementation language: **Python** (3.12+), fully async.
- **All** configuration via `.env` — no hardcoded values, no config files with secrets.
- **All** code, comments, identifiers, commit messages and documentation in **English**.
- Shipped as a **Docker** image with `docker-compose.yml` and a `Makefile`.
- Documentation is updated in the same change as the code it describes.

## Current status

**All eight phases are delivered.** The bot searches, browses, reads and authors notes, including
image-to-note capture across nine LLM providers, with the phase 7 hardening — caching, rate limits,
back-pressure, metrics, upload validation — on top, and the phase 8 packaging and documentation set
around it.

`make check` is green: ruff clean, mypy strict clean over 112 files, **1030 tests at 94% coverage**.
`make audit` reports no known vulnerabilities across the 70 locked packages. The release image
builds for `linux/amd64` and `linux/arm64` from base images pinned by digest.

**What has not happened is a run against live credentials.** There is no NoteDiscovery instance, Bot
API token or provider key in this environment. Every fault scenario is injected at the adapter seams
rather than by unplugging a real vault, and `/metrics` has been scraped by a test client rather than
by a Prometheus. The three commands that close that gap — `make check-env`, `make verify-contract`,
`make test-live` — are documented in [OPERATIONS.md](OPERATIONS.md) and wait only on credentials.

Per-phase detail is in [ROADMAP.md](ROADMAP.md); per-release detail in
[../CHANGELOG.md](../CHANGELOG.md).

## Decisions already taken

Each row links to the record that argues it, where one exists.

| Topic | Decision |
|---|---|
| NoteDiscovery access | Live instance at `NOTEDISCOVERY_URL` (base URL **with port**); API key optional |
| Transport | **REST is primary** — MCP is a stdio-only strict subset, so it is supported but flag-gated and off ([0001](adr/0001-rest-as-primary-transport.md)) |
| Missing API capabilities | Compensated client-side: derived tree, literal search, local ranking, read-modify-write edits ([0002](adr/0002-client-side-compensation.md)) |
| Access model | Multiple Telegram IDs (allow-list) sharing **one** NoteDiscovery credential; no per-user permissions ([0010](adr/0010-allow-list-as-access-model.md)) |
| LLM layer | **Proprietary router** with our own adapters, retry, failover and a per-provider circuit breaker ([0003](adr/0003-proprietary-llm-router.md)) |
| Bot UX | Inline keyboards with pagination — no Telegram Mini App |
| Callback state | Opaque token over a server-side session, because `callback_data` caps at 64 bytes ([0006](adr/0006-opaque-callback-tokens.md)) |
| Generated notes | **Preview before write.** Nothing generated is saved without an explicit tap ([0004](adr/0004-preview-before-write.md)) |
| Prompt injection | The model is asked for content, never for control flow: paths and flags come from rules ([0005](adr/0005-deterministic-intent-parsing.md)) |
| Versioning | Derived from the git tag; there is no version literal ([0007](adr/0007-version-from-git-tag.md)) |
| Metrics | Prometheus exposition without the client library; no label from user input ([0008](adr/0008-metrics-without-prometheus-client.md)) |
| Readiness | A degraded AI ladder is reported, not a readiness failure ([0009](adr/0009-degraded-llm-is-not-unready.md)) |
| MCP execution | Adapter built but flag-gated and **off by default**; no Docker socket exposure |
