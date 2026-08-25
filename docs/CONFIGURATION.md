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
| `TELEGRAM_WEBHOOK_URL` | empty | Public HTTPS URL, required when mode is `webhook` |
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
| `TREE_CACHE_TTL_S` | `300` | Lifetime of the client-side folder tree derived from `/api/notes` |
| `INBOX_PATH` | `Inbox` | Target of `/quick` |
| `AUTO_CREATE_PARENTS` | `true` | Call `POST /api/folders` for missing parents when writing a note |

### MCP subprocess (only when `NOTEDISCOVERY_TRANSPORT=mcp`)

| Variable | Default | Description |
|---|---|---|
| `MCP_ENABLED` | `false` | Master switch for the MCP adapter |
| `MCP_LAUNCH_MODE` | `docker` | `docker` (spawn `docker run`, needs the Docker socket mounted) or `local` (spawn `python -m mcp_server` from a vendored module) |
| `MCP_DOCKER_IMAGE` | `ghcr.io/gamosoft/notediscovery:latest` | Image used in `docker` mode |
| `MCP_STARTUP_TIMEOUT_S` | `30` | Time allowed for the subprocess to complete the MCP handshake |

## LLM router

| Variable | Default | Description |
|---|---|---|
| `LLM_CHAIN_CHAT` | — | Ordered failover chain, e.g. `groq,cerebras,openrouter,ollama` |
| `LLM_CHAIN_VISION` | — | Ordered chain for image tasks, e.g. `gemini,openrouter,ollama` |
| `LLM_MAX_RETRIES` | `3` | Retry attempts per provider before failover |
| `LLM_BACKOFF_BASE_S` | `1.0` | Exponential backoff base (jitter applied) |
| `LLM_REQUEST_TIMEOUT_S` | `60` | Per-request timeout |
| `LLM_CIRCUIT_FAILURE_THRESHOLD` | `5` | Failures before a provider's circuit opens |
| `LLM_CIRCUIT_RESET_S` | `120` | Cool-down before a half-open retry |
| `LLM_DAILY_CALL_LIMIT_PER_USER` | `100` | Cost guard, `0` disables |

Per provider — replace `<P>` with `NVIDIA`, `OPENROUTER`, `GROQ`, `GEMINI`, `CLOUDFLARE`,
`CEREBRAS`, `MISTRAL`, `PUTER`, `OLLAMA`:

| Variable | Description |
|---|---|
| `<P>_API_KEY` | Credential; a provider without one is skipped and logged at startup |
| `<P>_BASE_URL` | Override for self-hosted or regional endpoints |
| `<P>_MODEL` | Default chat model |
| `<P>_VISION_MODEL` | Model used for vision tasks, when supported |
| `<P>_ENABLED` | Explicit on/off switch, defaults to on when a key is present |

`CLOUDFLARE_ACCOUNT_ID` is additionally required for Cloudflare Workers AI.
`OLLAMA_BASE_URL` defaults to `http://localhost:11434` and needs no key.

## Sessions, cache and limits

| Variable | Default | Description |
|---|---|---|
| `SESSION_BACKEND` | `memory` | `memory` or `redis` |
| `REDIS_URL` | empty | Required when the backend is `redis` |
| `SESSION_TTL_S` | `3600` | Lifetime of pagination and draft state |
| `RESULTS_PAGE_SIZE` | `5` | Search hits per page |
| `TREE_PAGE_SIZE` | `10` | Tree entries per page |
| `LONG_NOTE_MODE` | `paged` | `paged` or `split` |
| `MAX_UPLOAD_MB` | `20` | Upper bound for downloaded Telegram files |
| `DEFAULT_TEXT_ACTION` | `search` | What a plain non-command message does: `search` or `quick` |

## Observability

| Variable | Default | Description |
|---|---|---|
| `LOG_LEVEL` | `INFO` | Standard Python levels |
| `LOG_FORMAT` | `json` | `json` or `console` |
| `HEALTH_PORT` | `8080` | Port for `/healthz` and `/readyz` |
| `METRICS_ENABLED` | `false` | Expose Prometheus metrics on the health port |
