# DiscoveryGram

Make a [NoteDiscovery](https://github.com/gamosoft/NoteDiscovery) vault fully usable from
**Telegram**: search it, navigate it, read it, and create notes in it — including LLM-assisted
creation from images.

> **Status: milestone M1 complete** (phases 0–4). The bot searches, browses and reads the whole
> vault: `/search` `/find` `/tag` `/recent` `/browse` `/open` `/backlinks` `/related`, plus a
> per-note action bar that edits, appends, tags, shares and deletes. LLM-assisted note creation
> lands in phases 5–6. See [docs/ROADMAP.md](docs/ROADMAP.md).

## Quick start

```bash
cp .env.example .env      # then fill in the required values
make install
make check                # lint, type-check, test
make run
```

Required values in `.env`:

| Variable | What it is |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Token from [@BotFather](https://t.me/BotFather) |
| `TELEGRAM_ALLOWED_USER_IDS` | Comma-separated allow-list. At least one id — an empty list would expose the bot to everyone |
| `NOTEDISCOVERY_URL` | Base URL **with port**, e.g. `http://host.docker.internal:8000` |

`NOTEDISCOVERY_API_KEY` is optional: NoteDiscovery may run unauthenticated.

Send `/whoami` to the bot to discover the id you need for the allow-list — an unlisted user is
refused, but the refusal tells them their own id.

## Versioning

The version comes from the **git tag**, not from a literal in `pyproject.toml`:

```bash
make version         # 0.2.0 on a tagged commit, 0.2.1.dev4+g1a2b3c4 otherwise
make docker/build    # tags the image with it and bakes it into the metadata
make release         # check + build, warns if the commit is not tagged
```

`/healthz` and `/status` both report the running version, so what is deployed is always
identifiable. Details in [docs/CONFIGURATION.md](docs/CONFIGURATION.md#versioning).

## Docker

Published as [`lordraw/discoverygram`](https://hub.docker.com/r/lordraw/discoverygram).

```bash
make docker/build
make docker/run      # docker compose up -d --build
make docker/logs
make docker/stop
```

Redis is only needed when `SESSION_BACKEND=redis`:

```bash
docker compose --profile redis up -d
```

The container exposes `/healthz` (liveness) and `/readyz` (readiness — reports whether
NoteDiscovery is reachable) on `HEALTH_PORT`, and carries a Docker `HEALTHCHECK`.

## Verifying the NoteDiscovery contract

Two NoteDiscovery behaviours could not be settled from source and shape the edit and search flows.
Once `.env` points at a live instance:

```bash
make verify-contract
```

It probes `POST` overwrite semantics using a single scratch note (which it deletes) and reads the
search configuration, then tells you what to record in the contract document.

## Documentation

| Document | Purpose |
|---|---|
| [docs/ROADMAP.md](docs/ROADMAP.md) | Delivery plan: phases, milestones, risks |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Components, layering, key decisions |
| [docs/FEATURES.md](docs/FEATURES.md) | Functional catalogue: commands and flows |
| [docs/CONFIGURATION.md](docs/CONFIGURATION.md) | Full `.env` reference |
| [docs/notediscovery-contract.md](docs/notediscovery-contract.md) | Verified REST + MCP contract |

## Development

```bash
make help            # list every target
make format          # auto-fix formatting and lint
make test            # unit tests (live tests excluded)
make test-live       # opt-in tests against a real instance
```

All code and documentation are English-only. All configuration comes from the environment.

## License

MIT
