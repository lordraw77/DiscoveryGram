# DiscoveryGram

**A Telegram front-end for [NoteDiscovery](https://github.com/gamosoft/NoteDiscovery): search, browse, read and create notes in your vault from a chat.**

> ### Feature-complete
>
> Search, navigation, note authoring, image-to-note capture across nine LLM providers, and the
> hardening around all of it — caching, per-user rate limits, circuit breaking, Prometheus metrics,
> upload validation. 1030 tests at 94% coverage.
>
> One honest caveat: the project was built and tested **without live credentials**. Fault scenarios
> are injected at the adapter seams rather than by unplugging a real vault. Your first run is the
> first run against a real NoteDiscovery — `make check-env` validates your configuration before you
> start, and `make verify-contract` probes your instance for the two behaviours that shape editing
> and search.

---

## What it does

| Capability | Detail |
|---|---|
| **Search** | Full-text, literal and tag search over your vault, with paginated results |
| **Navigate** | Browse the note tree with inline keyboards, breadcrumbs, backlinks and wiki-link jumps |
| **Read** | Notes rendered for Telegram, with long bodies paged instead of truncated |
| **Create** | Plain notes, template-based notes, and LLM-assisted creation — send a photo with a caption like *"extract the text and file this under Projects/Research, generate the title"* and confirm a preview before anything is written |
| **Stay up** | Nine LLM providers behind one router, with retry and (provider, model) failover |

## Supported tags

| Tag | Contents |
|---|---|
| `latest` | The most recent release |
| `X.Y.Z` | That release, immutably. Pin this in anything unattended |

**Architectures:** `linux/amd64` and `linux/arm64` — the same tag serves both, so a Raspberry Pi
pulls the right one with no extra flags.

Base image: `python:3.12-slim-bookworm`, **pinned by digest** so a given tag rebuilds identically.
Runs as a non-root user (uid 1001) with a built-in `HEALTHCHECK`. The image carries no build
toolchain: dependencies are installed in a separate stage and only the virtual environment is
copied forward.

## Quick start

You need a reachable NoteDiscovery instance and a bot token from
[@BotFather](https://t.me/BotFather).

```bash
docker run -d --name discoverygram \
  -p 8080:8080 \
  -e TELEGRAM_BOT_TOKEN='123456:AA...' \
  -e TELEGRAM_ALLOWED_USER_IDS='123456789' \
  -e NOTEDISCOVERY_URL='http://192.168.1.50:8000' \
  --restart unless-stopped \
  lordraw/discoverygram:latest
```

Check it came up:

```bash
curl localhost:8080/healthz   # {"status": "ok", "version": "2.0.0"}
curl localhost:8080/readyz    # 200 when NoteDiscovery is reachable, 503 otherwise
```

### docker compose

```yaml
services:
  discoverygram:
    image: lordraw/discoverygram:latest
    container_name: discoverygram
    restart: unless-stopped
    env_file: [.env]
    ports:
      - "8080:8080"
    extra_hosts:
      - "host.docker.internal:host-gateway"
```

## Required configuration

Everything is configured through environment variables. Nothing is hardcoded, and invalid
configuration fails fast with exit code 2 and a message naming the offending variable.

| Variable | Description |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Token from [@BotFather](https://t.me/BotFather) |
| `TELEGRAM_ALLOWED_USER_IDS` | Comma-separated allow-list of Telegram user ids. **At least one is required** — an empty list would expose your vault to anyone who finds the bot. Get your id from [@userinfobot](https://t.me/userinfobot) |
| `NOTEDISCOVERY_URL` | Base URL **including the port**, e.g. `http://192.168.1.50:8000`. Do not use `localhost`: inside the container that means the container itself. For an instance on the Docker host use `http://host.docker.internal:8000` with `extra_hosts` as shown above |

`NOTEDISCOVERY_API_KEY` is **optional** — NoteDiscovery may run unauthenticated. When set, it is
sent as `X-API-Key`.

## Frequently useful settings

| Variable | Default | Description |
|---|---|---|
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` |
| `LOG_FORMAT` | `json` | `json` for log shipping, `console` for human reading |
| `HEALTH_PORT` | `8080` | Port serving `/healthz`, `/readyz` and `/metrics` |
| `METRICS_ENABLED` | `false` | Serve Prometheus metrics at `/metrics` |
| `NOTEDISCOVERY_TIMEOUT` | `30` | Per-request timeout in seconds |
| `NOTEDISCOVERY_VERIFY_TLS` | `true` | Set `false` only for a self-signed internal certificate |
| `SESSION_BACKEND` | `memory` | `redis` to survive restarts and run more than one replica |
| `REDIS_URL` | — | Required when `SESSION_BACKEND=redis` |
| `INBOX_PATH` | `Inbox` | Where quick captures land |
| `MAX_UPLOAD_MB` | `20` | Telegram's Bot API caps file downloads at 20 MB |

The full reference, including the nine LLM providers, is in
[CONFIGURATION.md](https://github.com/lordraw77/DiscoveryGram/blob/main/docs/CONFIGURATION.md).

## LLM providers

Supported: **NVIDIA, OpenRouter, Groq, Gemini, Cloudflare Workers AI, Cerebras, Mistral, Puter and
Ollama.** **None is required.** Search, browsing, reading and manual note creation work with no
provider configured at all; only generation needs one — and when every provider is down, the bot
says so and keeps serving everything else.

Failover works on **(provider, model) pairs**, not on providers alone:

```
LLM_CHAIN_CHAT=nvidia,ollama
NVIDIA_MODELS=llama-3.3-70b,qwen2.5-72b
OLLAMA_MODELS=llama3.2

  nvidia/llama-3.3-70b (x3) -> nvidia/qwen2.5-72b (x3) -> ollama/llama3.2 (x3)
```

A model-level failure advances one rung and keeps the warm connection; a provider-level failure
opens that provider's circuit and skips all of its remaining rungs at once. Listing `ollama` last
keeps things working offline and keeps note content on your own hardware.

## Health and operations

- `GET /healthz` — liveness. `200` whenever the process is up.
- `GET /readyz` — readiness. `200` when every dependency check passes, `503` otherwise, with a
  per-check breakdown in the body.
- Docker `HEALTHCHECK` is built in; `docker inspect` reports `healthy` once ready.
- `SIGTERM` shuts down gracefully with exit code 0.
- Logs are structured JSON by default, with a correlation id per action and credentials redacted.

## Security notes

- **The allow-list is the only thing between your notes and a stranger.** The bot refuses to start
  without one, and rejects every non-listed user before any handler runs.
- The container runs as a non-root user (uid 1001).
- Never bake secrets into an image layer — pass them as environment variables or an `env_file`.
- Note content reaches third-party LLM providers only for commands that need it. Configuring an
  `ollama`-only chain keeps everything local.
- NoteDiscovery's MCP server is supported but **disabled by default**: it exposes a strict subset of
  the REST API, and enabling it in `docker` launch mode requires mounting the Docker socket, which
  grants root-equivalent access to the host.

## Documentation

- [Source and README](https://github.com/lordraw77/DiscoveryGram)
- [Walkthrough](https://github.com/lordraw77/DiscoveryGram/blob/main/docs/WALKTHROUGH.md) — every flow, with the bot's actual replies
- [Features](https://github.com/lordraw77/DiscoveryGram/blob/main/docs/FEATURES.md)
- [Configuration reference](https://github.com/lordraw77/DiscoveryGram/blob/main/docs/CONFIGURATION.md)
- [Operations](https://github.com/lordraw77/DiscoveryGram/blob/main/docs/OPERATIONS.md) — deploy, upgrade, back up, troubleshoot
- [Architecture](https://github.com/lordraw77/DiscoveryGram/blob/main/docs/ARCHITECTURE.md) and [decision records](https://github.com/lordraw77/DiscoveryGram/blob/main/docs/adr/README.md)
- [Changelog](https://github.com/lordraw77/DiscoveryGram/blob/main/CHANGELOG.md)
- [NoteDiscovery contract](https://github.com/lordraw77/DiscoveryGram/blob/main/docs/notediscovery-contract.md)

## License

MIT. DiscoveryGram is an independent project and is not affiliated with NoteDiscovery or Telegram.
