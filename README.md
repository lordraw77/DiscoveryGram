# DiscoveryGram

Make a [NoteDiscovery](https://github.com/gamosoft/NoteDiscovery) vault fully usable from
**Telegram**: search it, navigate it, read it, and create notes in it — including LLM-assisted
creation from images.

> **Status: feature-complete.** All eight phases are delivered — search, navigation, the
> multi-provider LLM router, image-to-note authoring, and the phase 7 hardening on top. `make check`
> is green: ruff clean, mypy strict clean, **1030 tests at 94% coverage**. What has *not* happened is
> a run against live credentials — see [Verifying against a live instance](#verifying-against-a-live-instance).
> See [docs/ROADMAP.md](docs/ROADMAP.md) and [CHANGELOG.md](CHANGELOG.md).

## What it does

| | |
|---|---|
| **Find** | `/search` full text · `/find` literal · `/tag` · `/recent` — ranked client-side, with pagination that costs no vault read |
| **Read** | `/browse` the folder tree · `/open` · `/backlinks` · `/related` — wiki-links become buttons, long notes are paged or split |
| **Write** | `/new` · `/quick` capture · `/template` · `/move` · `/folder`, plus a per-note action bar |
| **Capture** | Send a photo — the bot reads it, writes it up and shows a **draft**. Nothing is saved until you tap *Save* |
| **Ask** | `/summarize` a note · `/ask` a question answered from your notes, with its sources named |

A guided tour with the bot's actual replies is in [docs/WALKTHROUGH.md](docs/WALKTHROUGH.md).

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
make docker/build    # single-arch, for this machine
make docker/run      # docker compose up -d --build
make docker/logs
make docker/stop
```

Release images are **multi-arch** (`linux/amd64` + `linux/arm64`) and build from base images pinned
by digest:

```bash
make docker/buildx   # build both architectures (cache only — proves they compile)
make docker/push     # build and push; refuses a version that is not a release
make docker/pins     # current base digests, for refreshing the Dockerfile
```

To run a published image instead of building from source, copy
`docker-compose.override.example.yml` to `docker-compose.override.yml` — Compose merges it
automatically, with no `-f` flag. It also covers resource limits, log rotation, webhook mode behind
a TLS proxy, and metrics scraping.

Redis is only needed when `SESSION_BACKEND=redis`:

```bash
docker compose --profile redis up -d
```

The container exposes `/healthz` (liveness), `/readyz` (readiness — NoteDiscovery, the session
backend and the Telegram updater; a degraded AI ladder is reported but does not fail readiness) and
`/metrics` (Prometheus, when `METRICS_ENABLED=true`) on `HEALTH_PORT`, and carries a Docker
`HEALTHCHECK`.

## Verifying against a live instance

The bot was built and tested without live credentials: every fault scenario is injected at the
adapter seams, and the `/metrics` endpoint has been scraped by a test client rather than by a real
Prometheus. Three things turn "confirmed in code" into "confirmed in production":

```bash
make check-env       # validate .env and print the LLM ladder the current keys produce — no network
make verify-contract # probe the live instance for the two behaviours that shape edit and search
make test-live       # the opt-in suite against a real NoteDiscovery
```

`make verify-contract` probes `POST` overwrite semantics using a single scratch note (which it
deletes) and reads the search configuration, then tells you what to record in the contract document.

`ollama` needs no API key and is the cheapest way to see image-to-note work end to end.

## Documentation

| Document | Purpose |
|---|---|
| [docs/WALKTHROUGH.md](docs/WALKTHROUGH.md) | Every flow end to end, with the bot's actual replies |
| [docs/FEATURES.md](docs/FEATURES.md) | Functional catalogue: commands and flows |
| [docs/CONFIGURATION.md](docs/CONFIGURATION.md) | Full `.env` reference |
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | Deploy, upgrade, back up, observe, troubleshoot |
| [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) | Toolchain, layout, conventions, how to add things |
| [docs/LLM_PROVIDERS.md](docs/LLM_PROVIDERS.md) | Per-provider setup, dialect quirks, chain design |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Components, layering, key decisions |
| [docs/adr/](docs/adr/README.md) | Architecture decision records — the *why*, and what was rejected |
| [docs/notediscovery-contract.md](docs/notediscovery-contract.md) | Verified REST + MCP contract |
| [docs/ROADMAP.md](docs/ROADMAP.md) | Delivery plan: phases, milestones, risks |
| [CHANGELOG.md](CHANGELOG.md) | What changed, per release |

## Development

```bash
make help            # list every target
make format          # auto-fix formatting and lint
make check           # lint, type-check, test — exactly what CI runs
make test-live       # opt-in tests against a real instance
make audit           # locked dependencies against the advisory database
```

All code and documentation are English-only. All configuration comes from the environment.
Full guide: [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md).

## License

MIT
