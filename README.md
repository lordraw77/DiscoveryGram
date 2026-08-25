# DiscoveryGram

Make a [NoteDiscovery](https://github.com/gamosoft/NoteDiscovery) vault fully usable from
**Telegram**: search it, navigate it, read it, and create notes in it — including LLM-assisted
creation from images.

> **Status: phase 0.** The foundations are in place (configuration, logging, health, container,
> CI) and NoteDiscovery's contract has been verified against version 0.31.3. The Telegram bot
> itself lands in phase 2. See [docs/ROADMAP.md](docs/ROADMAP.md).

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

## Docker

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
