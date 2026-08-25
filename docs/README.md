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

## Project constraints (non-negotiable)

- Implementation language: **Python** (3.12+), fully async.
- **All** configuration via `.env` — no hardcoded values, no config files with secrets.
- **All** code, comments, identifiers, commit messages and documentation in **English**.
- Shipped as a **Docker** image with `docker-compose.yml` and a `Makefile`.
- Documentation is updated in the same change as the code it describes.

## Current status

**Phase 0 complete.** The contract is documented against NoteDiscovery 0.31.3
([notediscovery-contract.md](notediscovery-contract.md)), and the foundations are built and
verified: configuration, logging, health endpoints, container, CI, and a live contract probe.
`make check` is green at 91% coverage. Phase 1 (the NoteDiscovery integration layer) is next.

## Decisions already taken

| Topic | Decision |
|---|---|
| NoteDiscovery access | Live instance at `NOTEDISCOVERY_URL` (base URL **with port**); API key optional |
| Transport | **REST is primary.** NoteDiscovery's MCP server is a stdio subprocess exposing a strict subset of the REST API, so it is supported but flag-gated and off by default |
| Access model | Multiple Telegram IDs (allow-list) sharing **one** NoteDiscovery instance/credential |
| LLM layer | **Proprietary router** with our own provider adapters, retry and failover |
| Bot UX | Inline keyboards with pagination — no Telegram Mini App |
| MCP execution | Adapter built but flag-gated and **off by default**; no Docker socket exposure |
