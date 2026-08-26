# Configuration Reference

Everything is configured through environment variables, loaded from `.env` via
`pydantic-settings`. `.env.example` is the committed template and is kept in sync with the code.
Invalid or missing required values fail fast at startup with an explicit message.

## Telegram

| Variable | Default | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | **Required.** Token from BotFather |
| `TELEGRAM_ALLOWED_USER_IDS` | — | **Required.** Comma-separated allow-list of Telegram user ids |
| `TELEGRAM_ALLOWED_CHAT_IDS` | empty | Optional allow-list for group chats |
| `TELEGRAM_MODE` | `polling` | `polling` or `webhook` |
| `TELEGRAM_WEBHOOK_URL` | empty | Public HTTPS URL of the reverse proxy, required when mode is `webhook` |
| `TELEGRAM_WEBHOOK_SECRET` | empty | Telegram signs every delivery with this, so a request reaching the port from anywhere else is rejected before it is parsed. Strongly recommended in webhook mode |
| `TELEGRAM_WEBHOOK_LISTEN` | `0.0.0.0` | Interface the webhook listener binds inside the container |
| `TELEGRAM_WEBHOOK_PORT` | `8081` | Local webhook port. Must differ from `HEALTH_PORT` — startup refuses a collision |
| `TELEGRAM_WEBHOOK_PATH` | `telegram` | Path Telegram posts to, appended to the public URL |
| `TELEGRAM_WEBHOOK_SECRET` | empty | Secret token validating incoming webhook calls |
| `TELEGRAM_PARSE_MODE` | `MarkdownV2` | Rendering mode for note bodies |

## NoteDiscovery

Variable names mirror upstream NoteDiscovery so the same values can be passed straight through to
the MCP subprocess. See [notediscovery-contract.md](notediscovery-contract.md).

| Variable | Default | Description |
|---|---|---|
| `NOTEDISCOVERY_URL` | — | **Required.** Base URL **with port**, e.g. `http://host.docker.internal:8000`. Serves both REST and the MCP server's backend calls |
| `NOTEDISCOVERY_API_KEY` | empty | Shared API key. **Optional** — an instance may run unauthenticated. Sent as `X-API-Key` |
| `NOTEDISCOVERY_TIMEOUT` | `30` | Per-request timeout in seconds |
| `NOTEDISCOVERY_MAX_RETRIES` | `3` | Retry attempts on transient failures |
| `NOTEDISCOVERY_VERIFY_TLS` | `true` | Set false only for internal self-signed instances |
| `NOTEDISCOVERY_TRANSPORT` | `rest` | `rest` or `mcp`. MCP is a strict subset — REST is the default for a reason |
| `SEARCH_DEFAULT_LIMIT` | `50` | Always sent to `/api/search`, which has **no server-side default cap** |
| `SEARCH_MIN_QUERY_LENGTH` | `2` | Shortest query NoteDiscovery acts on. A hard-coded server constant that no endpoint exposes, so the bot carries its own copy and refuses shorter queries locally. Raise it only to match a patched instance |
| `TREE_CACHE_TTL_S` | `300` | Lifetime of the client-side folder tree derived from `/api/notes`. Every write invalidates it regardless, so a low value is rarely needed; `0` disables caching |
| `INBOX_PATH` | `Inbox` | Where `/quick` captures land — **one note per day** under this folder, not one file per thought |
| `AUTO_CREATE_PARENTS` | `true` | Create missing parent folders when writing a note. With it **off**, a note aimed at a folder that does not exist is refused with the folder named, rather than silently inventing a tree |

### MCP subprocess (only when `NOTEDISCOVERY_TRANSPORT=mcp`)

| Variable | Default | Description |
|---|---|---|
| `MCP_ENABLED` | `false` | Master switch for the MCP adapter |
| `MCP_LAUNCH_MODE` | `docker` | `docker` (spawn `docker run`, needs the Docker socket mounted) or `local` (spawn `python -m mcp_server` from a vendored module) |
| `MCP_DOCKER_IMAGE` | `ghcr.io/gamosoft/notediscovery:latest` | Image used in `docker` mode |
| `MCP_STARTUP_TIMEOUT_S` | `30` | Time allowed for the subprocess to complete the MCP handshake |

## LLM router

Failover works on **(provider, model) pairs**, not on providers alone. A request retries the same
pair `LLM_RETRIES_PER_MODEL` times, then moves to the next model of the same provider, and only
when that provider's models are exhausted does the next provider take over.

```
LLM_CHAIN_CHAT=groq,ollama
GROQ_MODELS=fast-model,bigger-model
OLLAMA_MODELS=local-model

  groq/fast-model  (x3) -> groq/bigger-model (x3) -> ollama/local-model (x3) -> give up
```

`make check-env` prints the exact ladder a configuration produces, including the reason for every
provider that was skipped.

| Variable | Default | Description |
|---|---|---|
| `LLM_CHAIN_CHAT` | — | Ordered provider chain for text tasks, e.g. `groq,cerebras,ollama` |
| `LLM_CHAIN_VISION` | — | Ordered provider chain for image tasks, e.g. `gemini,openrouter,ollama` |
| `LLM_RETRIES_PER_MODEL` | `3` | Retries against the **same** (provider, model) pair before advancing to the next model |
| `LLM_BACKOFF_BASE_S` | `1.0` | Exponential backoff base (jitter applied) |
| `LLM_REQUEST_TIMEOUT_S` | `60` | Per-request timeout |
| `LLM_CIRCUIT_FAILURE_THRESHOLD` | `5` | Failures before a provider's circuit opens |
| `LLM_CIRCUIT_RESET_S` | `120` | Cool-down before a half-open retry |
| `LLM_DAILY_CALL_LIMIT_PER_USER` | `100` | Cost guard, `0` disables |

Per provider — replace `<P>` with `NVIDIA`, `OPENROUTER`, `GROQ`, `GEMINI`, `CLOUDFLARE`,
`CEREBRAS`, `MISTRAL`, `PUTER`, `OLLAMA`:

| Variable | Description |
|---|---|
| `<P>_API_KEY` | Credential. A provider without one is skipped and the reason logged — `ollama` is exempt |
| `<P>_BASE_URL` | Override for self-hosted, proxied or regional endpoints. Every provider has a built-in default |
| `<P>_ACCOUNT_ID` | Only Cloudflare uses it, and Cloudflare **requires** it: Workers AI puts the account id in the request URL |
| `<P>_MODELS` | **Ordered, comma-separated** list of chat models, tried left to right |
| `<P>_VISION_MODELS` | **Ordered, comma-separated** list of vision-capable models |

A provider listed in a chain but with no model for that task is skipped, not silently retried with
a default: leaving `<P>_VISION_MODELS` empty removes it from the vision ladder only.

Two provider-specific facts that change what a chain does:

- **`cerebras` cannot carry an image.** It is dropped from the vision ladder at startup whatever
  `CEREBRAS_VISION_MODELS` says, with the reason logged.
- **`cloudflare` without `CLOUDFLARE_ACCOUNT_ID` is dropped entirely**, when clients are built —
  not as a 404 on every attempt.

`OLLAMA_BASE_URL` defaults to `http://localhost:11434`, needs no key, and has `/v1` appended for
you if you leave it off. Inside a container, `localhost` means the container: use
`http://host.docker.internal:11434`.

Per-provider setup, dialect quirks and how to read a failure are in
[LLM_PROVIDERS.md](LLM_PROVIDERS.md).

### Generation behaviour

| Variable | Default | Description |
|---|---|---|
| `GENERATED_TAGS_MAX` | `5` | Most tags a generated note may carry. A model asked for "some tags" will happily return twenty, and the vault's tag index is shared across every note |
| `PROVENANCE_ENABLED` | `true` | Record the provider and model on generated notes, as an HTML comment — greppable, but never shown in a snippet or an export |
| `ASK_CONTEXT_NOTES` | `5` | How many notes `/ask` may read as context. Each one is a vault read and tokens in the prompt |

### Task profiles

Four tasks, two capabilities. `chat`, `title` and `summarise` are all chat-capability tasks and
draw on `LLM_CHAIN_CHAT` and `<P>_MODELS`; only `vision` uses `LLM_CHAIN_VISION` and
`<P>_VISION_MODELS`. The tasks differ in their sampling defaults — a title is short and nearly
deterministic, a chat reply has room and some warmth — so a caller asks for a task and never
configures the numbers. Two chains, not four.

## Sessions, cache and limits

| Variable | Default | Description |
|---|---|---|
| `SESSION_BACKEND` | `memory` | `memory` (single replica, lost on restart) or `redis` (survives restarts, shared across replicas — keeps existing keyboards working across a deploy). Redis needs the optional extra: `uv sync --extra redis` |
| `REDIS_URL` | empty | Required when the backend is `redis` |
| `SESSION_TTL_S` | `3600` | Lifetime of pagination state, drafts and **callback tokens**. A button older than this stops working, so lower it only deliberately |
| `RECENT_DEFAULT_DAYS` | `7` | Default window for `/recent`. Override per call with `/recent 30` |
| `RESULTS_PAGE_SIZE` | `5` | Search hits per page |
| `TREE_PAGE_SIZE` | `10` | Tree entries per page |
| `LONG_NOTE_MODE` | `paged` | `paged` or `split` |
| `MAX_UPLOAD_MB` | `20` | Upper bound for attachments. Checked against the size Telegram reports **before** downloading, so an oversized file costs no transfer. Telegram's own Bot API cap is 20 MB |
| `DEFAULT_TEXT_ACTION` | `search` | What a plain non-command message does: `search` runs a query, `quick` captures into today's inbox note. With `quick` a message is **never also searched** — a thought you meant to keep must not become a query |

## Observability

| Variable | Default | Description |
|---|---|---|
| `LOG_LEVEL` | `INFO` | Standard Python levels |
| `LOG_FORMAT` | `json` | `json` or `console` |
| `HEALTH_PORT` | `8080` | Port for `/healthz` and `/readyz` |
| `METRICS_ENABLED` | `false` | Expose Prometheus metrics on the health port |

## Versioning

There is no version variable to set. The version comes from the git tag:

```bash
make version          # what the build backend will produce
make docker/build     # builds and tags the image with it
make release          # check + build, and warns if the commit is not tagged
```

A tagged commit produces that tag (`0.2.0`); any other commit produces a PEP 440 development
version pointing at it (`0.2.1.dev4+g1a2b3c4`). The image reports its own version at
`GET /healthz` and in `/status`.

Because `.git` is not part of the Docker build context, a bare `docker build` cannot see the tag
and reports `0.0.0+unknown`. Pass it explicitly if you are not using the Makefile:

```bash
docker build --build-arg VERSION="$(make -s version)" -t discoverygram:local .
```
