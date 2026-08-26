# DiscoveryGram

**A Telegram front-end for [NoteDiscovery](https://github.com/gamosoft/NoteDiscovery): search, browse, read and create notes in your vault from a chat.**

> ### ⚠️ Early access — the bot is not implemented yet
>
> This image currently ships the **foundations only**: configuration, structured logging, health
> endpoints and the NoteDiscovery reachability probe. It starts, validates your configuration and
> answers `/healthz`, but **it does not yet connect to Telegram or serve any command**.
>
> Telegram handlers arrive in phase 2, search in phase 3, navigation in phase 4. Pull this image now
> if you want to prepare configuration or follow development — not if you expect a working bot today.
> Progress is tracked in [ROADMAP.md](https://github.com/lordraw77/DiscoveryGram/blob/main/docs/ROADMAP.md).

---

## What it will do

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
| `latest` | Latest build from `main` |
| `0.1.0` | Phase 0 — foundations, no Telegram bot yet |

Base image: `python:3.12-slim-bookworm`. Runs as a non-root user (uid 1001) with a built-in
`HEALTHCHECK`.

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
curl localhost:8080/healthz   # {"status": "ok", "version": "0.1.0"}
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
| `HEALTH_PORT` | `8080` | Port serving `/healthz` and `/readyz` |
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
Ollama.** None is required until the LLM features land in phase 5.

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
- [Roadmap](https://github.com/lordraw77/DiscoveryGram/blob/main/docs/ROADMAP.md)
- [Architecture](https://github.com/lordraw77/DiscoveryGram/blob/main/docs/ARCHITECTURE.md)
- [Features](https://github.com/lordraw77/DiscoveryGram/blob/main/docs/FEATURES.md)
- [Configuration reference](https://github.com/lordraw77/DiscoveryGram/blob/main/docs/CONFIGURATION.md)
- [NoteDiscovery contract](https://github.com/lordraw77/DiscoveryGram/blob/main/docs/notediscovery-contract.md)

## License

MIT. DiscoveryGram is an independent project and is not affiliated with NoteDiscovery or Telegram.
