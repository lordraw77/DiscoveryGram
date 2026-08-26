# DiscoveryGram Documentation

DiscoveryGram makes a **NoteDiscovery** instance fully usable from **Telegram**: search, browse,
read and create notes — including LLM-assisted creation from images and unstructured input.

| Document | Purpose |
|---|---|
| [ROADMAP.md](ROADMAP.md) | Delivery plan: phases, milestones, deliverables, risks |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Components, layering, data flow, key design decisions |
| [FEATURES.md](FEATURES.md) | Functional catalogue: commands, flows, UX behaviour |
| [CONFIGURATION.md](CONFIGURATION.md) | Full `.env` reference |
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
however long the browse. `make check` is green at 93% coverage across 384 tests, with an opt-in
live suite (`make test-live`) waiting on credentials. Phase 4 (navigation) is next.

The version now comes from the git tag rather than a literal — see
[CONFIGURATION.md](CONFIGURATION.md#versioning).

## Decisions already taken

| Topic | Decision |
|---|---|
| NoteDiscovery access | Live instance at `NOTEDISCOVERY_URL` (base URL **with port**); API key optional |
| Transport | **REST is primary.** NoteDiscovery's MCP server is a stdio subprocess exposing a strict subset of the REST API, so it is supported but flag-gated and off by default |
| Access model | Multiple Telegram IDs (allow-list) sharing **one** NoteDiscovery instance/credential |
| LLM layer | **Proprietary router** with our own provider adapters, retry and failover |
| Bot UX | Inline keyboards with pagination — no Telegram Mini App |
| MCP execution | Adapter built but flag-gated and **off by default**; no Docker socket exposure |
