# DiscoveryGram Documentation

DiscoveryGram makes a **NoteDiscovery** instance fully usable from **Telegram**: search, browse,
read and create notes — including LLM-assisted creation from images and unstructured input.

| Document | Purpose |
|---|---|
| [ROADMAP.md](ROADMAP.md) | Delivery plan: phases, milestones, deliverables, risks |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Components, layering, data flow, key design decisions |
| [FEATURES.md](FEATURES.md) | Functional catalogue: commands, flows, UX behaviour |
| [CONFIGURATION.md](CONFIGURATION.md) | Full `.env` reference |
| [LLM_PROVIDERS.md](LLM_PROVIDERS.md) | Per-provider setup, dialect quirks, chain design, failure triage |
| [notediscovery-contract.md](notediscovery-contract.md) | Verified REST + MCP contract of NoteDiscovery 0.31.3 |
| [dockerhub-overview.md](dockerhub-overview.md) | Text published as the Docker Hub repository overview |

## Project constraints (non-negotiable)

- Implementation language: **Python** (3.12+), fully async.
- **All** configuration via `.env` — no hardcoded values, no config files with secrets.
- **All** code, comments, identifiers, commit messages and documentation in **English**.
- Shipped as a **Docker** image with `docker-compose.yml` and a `Makefile`.
- Documentation is updated in the same change as the code it describes.

## Current status

**Phases 0 and 1 complete.** The contract is documented against NoteDiscovery 0.31.3
([notediscovery-contract.md](notediscovery-contract.md)) and re-verified in phase 1 against the
image's handler source, which settled both open behaviours and corrected four assumptions.

On top of the phase 0 foundations, the NoteDiscovery integration layer is built and tested: the
`NoteStore` port and domain model, `RestNoteStore`, the flag-gated `McpNoteStore`, the client-side
compensation layer (derived folder tree, literal search, ranking, read-modify-write editing,
rate-limit pacing) and the startup probe.

**Phase 2 complete.** The bot core runs: the allow-list, the command router (`/start`, `/help`,
`/whoami`, `/cancel`, `/status`), the session store, the callback-token mechanism, the message
renderer and the global error handler.

**Phase 3 complete.** All four search modes — full text, literal, tag and recent — with client-side
ranking, term highlighting and pagination that costs no vault read and leaves one session entry
however long the browse.

**Phase 4 complete, and with it milestone M1 — the read-only bot.** Tree browsing with breadcrumbs,
note rendering with `paged` and `split` modes, wiki-link buttons, backlinks, graph-related notes,
the per-note action bar, and folder operations.

**Phase 5 complete — the LLM router.** Nine providers behind one `LlmClient` port: one adapter for
the six OpenAI-compatible ones, dedicated adapters for `gemini`, `cloudflare` and `puter`. A
request walks the (provider, model) ladder with retry, exponential backoff and `Retry-After`
awareness; a per-provider circuit breaker skips a provider's remaining models in one step rather
than burning retries on a rejected key. Usage is accounted per provider and reported in `/status`,
and a per-user daily cap bounds cost. See [LLM_PROVIDERS.md](LLM_PROVIDERS.md).

**Phase 6 complete, and with it milestone M3 — full note authoring.** `/new`, `/quick`, templates,
`/summarize` and `/ask`; and the headline flow: send a photo with a caption, the bot uploads it,
reads it, writes it up, and shows a **preview card** — nothing reaches the vault until `Save` is
tapped. Captions are parsed by rules rather than by a model, so a photo cannot choose where a note
is written. `make check` is green at 93% coverage across 936 tests, with 31 live tests
(`make test-live`) waiting on credentials.

The version now comes from the git tag rather than a literal — see
[CONFIGURATION.md](CONFIGURATION.md#versioning).

## Decisions already taken

| Topic | Decision |
|---|---|
| NoteDiscovery access | Live instance at `NOTEDISCOVERY_URL` (base URL **with port**); API key optional |
| Transport | **REST is primary.** NoteDiscovery's MCP server is a stdio subprocess exposing a strict subset of the REST API, so it is supported but flag-gated and off by default |
| Access model | Multiple Telegram IDs (allow-list) sharing **one** NoteDiscovery instance/credential |
| LLM layer | **Proprietary router** with our own provider adapters, retry, failover and a per-provider circuit breaker |
| Bot UX | Inline keyboards with pagination — no Telegram Mini App |
| Generated notes | **Preview before write.** Nothing generated is saved without an explicit tap |
| Prompt injection | The LLM is asked for content, never for control flow: paths and flags come from rules over the caption, never from a model |
| MCP execution | Adapter built but flag-gated and **off by default**; no Docker socket exposure |
