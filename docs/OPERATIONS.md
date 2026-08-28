# Operations

Running DiscoveryGram in production: deploy, upgrade, observe, back up, and work out what is wrong
when something is. Configuration values themselves are in
[CONFIGURATION.md](CONFIGURATION.md); this document is about the running process.

---

## 1. What the process actually is

One container, one Python process, no database of its own. It holds:

- an outbound HTTP connection to **NoteDiscovery** (the vault is the only durable state),
- an outbound connection to the **Telegram Bot API** (polling), or an inbound listener (webhook),
- outbound connections to whichever **LLM providers** are configured,
- **session state**: in memory by default, in Redis when `SESSION_BACKEND=redis`.

Everything the bot knows how to lose, it can lose. Sessions are callback state — which note a button
belongs to, which page a result set is on, a draft awaiting `Save`. Losing them costs an open note's
buttons and any unsaved draft, never a note. That is why the default backend is memory and why the
bundled Redis is configured for cheap persistence rather than durability.

---

## 2. Deploying

### First deploy

```bash
git clone https://github.com/lordraw77/DiscoveryGram.git
cd DiscoveryGram
cp .env.example .env
$EDITOR .env              # at minimum the three required values
make check-env            # validates .env and prints a redacted summary — no network
make docker/run           # docker compose up -d --build
make docker/logs
```

The three values that must be set are `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USER_IDS` and
`NOTEDISCOVERY_URL`. Everything else has a working default.

`make check-env` is worth running before every deploy: it parses the same settings object the
process does, applies the same cross-field rules, and prints the LLM ladder the current keys
produce — without contacting anything.

### Running a published image instead of building

Copy `docker-compose.override.example.yml` to `docker-compose.override.yml` and uncomment the
`image:` / `build: !reset null` block, pinning a version rather than `latest`. Compose merges the
override automatically.

```bash
docker compose pull
docker compose up -d
```

### Polling or webhook

Polling (the default) needs no inbound connectivity and is the right choice for anything behind
NAT. Webhook mode needs a public HTTPS URL — Telegram will not post to plain HTTP — so it needs a
TLS-terminating reverse proxy in front, and `TELEGRAM_WEBHOOK_SECRET` set to a long random string so
the listener can reject forged posts. Bind the webhook port to `127.0.0.1` and let the proxy reach
it there; the override example shows the mapping.

### Startup sequence, and what is fatal

| Step | On failure |
|---|---|
| Load and validate settings | **Fatal, exit 2.** Every invalid field is printed by name |
| Configure logging, register secrets to scrub | — |
| Build the note store, session store and LLM router | Fatal on a malformed provider chain, exit 2 |
| Start the health server | Fatal — the port is how an orchestrator sees the process |
| Probe NoteDiscovery | **Not fatal.** Logs `notediscovery_not_reachable_at_startup`, `/readyz` reports it, and the instance may come back with no restart |
| Start Telegram | A **rejected token** is fatal, exit 2 — a restart cannot fix a wrong answer. Any other failure exits 1, which a restart policy should retry |

Exit codes are the contract: **2 means stop and fix the configuration**, **1 means try again**,
**0 is a clean stop**. A `restart: unless-stopped` policy plus exit 2 is the combination that stops
a misconfigured bot from restart-looping forever while still recovering from a transient outage.

### Shutdown

`SIGTERM` and `SIGINT` both set the stop event. Teardown then runs every step — Telegram, health,
sessions, the LLM router, NoteDiscovery — **in order, whatever any of them does**. One collaborator
that fails to close cannot leave the next holding a socket. Look for `shutdown_complete` in the log;
if you see `shutting_down` without it, the process was killed before teardown finished and the
`stop_grace_period` is too short.

---

## 3. Upgrading

```bash
git fetch --tags
git checkout v2.1.0          # or whatever the target tag is
make check                   # lint, type-check, test
make audit                   # locked dependencies against the advisory database
make docker/run              # rebuilds and replaces the container
```

Then confirm what is actually running:

```bash
curl -s localhost:8080/healthz     # reports the version
```

The version comes from the git tag, so a deployed image is always identifiable. A checkout that is
not exactly on a tag produces a development version (`2.0.1.dev1+gad5a1eb`) — which is the honest
answer, and `make release` warns rather than letting you publish it as a release.

**Base images are pinned by digest.** Refresh them deliberately:

```bash
make docker/pins             # prints current digests; paste them into the Dockerfile
```

### Upgrading NoteDiscovery underneath the bot

The contract is documented against a specific version (see
[notediscovery-contract.md](notediscovery-contract.md)). The bot logs the instance version at
startup and `/status` shows it with a warning marker when it differs from the documented one. A
mismatch is not an error — it is a prompt to re-run `make verify-contract` against the upgraded
instance before trusting the edit and search flows.

### Rollback

Deploys are a container swap and the vault is untouched by them, so rollback is checking out the
previous tag and running `make docker/run` again. Nothing in the bot needs migrating; sessions are
disposable and are meant to be lost across a restart.

---

## 4. Backup

**The bot holds nothing that needs backing up.** Back up the things it talks to:

| What | Why | How |
|---|---|---|
| The NoteDiscovery vault | The only durable state. Every note the bot creates lives here | Whatever you already use for the vault directory |
| `.env` | Secrets, and the only record of the deployment's configuration | Store it in a secret manager, not in git — it is gitignored for a reason |
| Redis (`redis-data` volume) | Only sessions. Losing it drops open drafts and button state | Optional. Not worth a backup schedule |

If you back up exactly one thing, back up the vault.

---

## 5. Observability

### Endpoints, on `HEALTH_PORT` (default 8080)

| Path | Use it for |
|---|---|
| `/healthz` | Liveness. Always `200` while the process is alive; reports the version |
| `/readyz` | Readiness. `200` ready, `503` not ready |
| `/metrics` | Prometheus exposition, `404` unless `METRICS_ENABLED=true` |

**A degraded AI ladder is deliberately not a readiness failure.** `/readyz` reports it as
`"llm": "degraded"` while the overall verdict stays `ready`. Search, browse, read and `/new` need no
provider at all; taking the bot out of service because a model vendor is having a bad afternoon
turns a partial outage into a total one.

Verdicts are cached for two seconds and every check is bounded by a five-second timeout, so an
aggressive probe cannot become load on the instance whose health it is asking about, and a
dependency that hangs yields an honest `503` rather than a probe that times out.

### Metrics worth alerting on

The full list is in [CONFIGURATION.md](CONFIGURATION.md#the-metrics). These are the ones that mean
something is wrong rather than merely busy:

| Signal | Reading |
|---|---|
| `discoverygram_handler_errors_total{kind="bug"}` | An unexpected exception reached the error handler. Any sustained rate is a defect, not load |
| `discoverygram_llm_circuit_open{state="open"}` | A provider is being skipped. One is failover working; all of them is a degraded ladder |
| `discoverygram_notediscovery_requests_total{outcome="unreachable"}` | The vault is not answering. Everything else will follow |
| `discoverygram_llm_throttled_total{limit="daily"}` | Users are hitting the cost cap. Either raise it deliberately or find out who |
| `discoverygram_updates_total{outcome="rejected"}` | Allow-list refusals. A steady trickle is someone finding the bot |

Instruments record whether or not `/metrics` is exposed, so enabling it gives numbers from a running
process rather than from zero. No label carries a note path, a query or a user id — series count is
bounded by configuration, not by the vault.

### Logs

Structured, JSON by default (`LOG_FORMAT=console` for a terminal). Every secret the process knows —
the bot token, the NoteDiscovery key, every provider key — is scrubbed from **both** the application
and the third-party logging pipelines, because python-telegram-bot logs the Bot API URL and the
token is in it. That scrubbing is verified at `LOG_LEVEL=DEBUG`.

Each update carries a correlation id, so one user's failing request can be followed end to end:

```bash
docker compose logs discoverygram | grep '"correlation_id": "abc123"'
```

Log lines worth knowing by name: `starting`, `ready`, `notediscovery_not_reachable_at_startup`,
`telegram_token_rejected`, `shutting_down`, `shutdown_complete`.

---

## 6. Troubleshooting

### The bot does not answer at all

1. `curl -s localhost:8080/healthz` — no answer means the process is not up. Read the logs: an
   `exit 2` printed the invalid field by name.
2. Answers, but silent in Telegram? Check the allow-list. Send `/whoami` — an unlisted user is
   refused, but the refusal tells them their own id, which is exactly the id to add to
   `TELEGRAM_ALLOWED_USER_IDS`.
3. Still silent, in webhook mode? Telegram is not reaching you. Confirm the public HTTPS URL
   terminates TLS and forwards to the webhook port, and that `TELEGRAM_WEBHOOK_SECRET` matches.
   Polling mode removes this entire class of problem — switch to it to bisect.

### `telegram_token_rejected`, exit 2

The token is wrong or revoked. A restart cannot fix a wrong answer, which is why this one is fatal
rather than retried. Get a fresh token from @BotFather.

### `/readyz` returns 503

The body names the failing check.

- `notediscovery` — the vault is unreachable. From inside the container:
  `docker compose exec discoverygram python -c "import urllib.request;
  print(urllib.request.urlopen('$NOTEDISCOVERY_URL/api/config', timeout=5).status)"`.
  On the host rather than in a container, `NOTEDISCOVERY_URL` must be
  `http://host.docker.internal:PORT`, not `localhost` — `localhost` inside a container is the
  container.
- `sessions` — with `SESSION_BACKEND=redis`, Redis is not answering. It only starts under its
  profile: `docker compose --profile redis up -d`.
- `telegram` — the updater stopped. The logs will say why.

`"llm": "degraded"` in a `200` body is not a fault to chase; see above.

### AI features fail while search and browse work

That is the design: the vault and the model providers are independent. `/status` names the state of
each provider, including which circuits are open and how long until they retry. Common causes, in
order of likelihood: no provider key configured at all (`make check-env` prints the ladder — an
empty ladder is the answer), a rejected key (the circuit opens after the threshold rather than
retrying it forever), or every provider genuinely failing, in which case the ladder is refused
immediately with its cool-down instead of queuing users behind a dead vendor.

`ollama` needs no key and terminates a chain — it is the cheapest way to confirm the pipeline works
end to end.

### A user hits a limit

Three different limits produce three different sentences:

| Message | Limit | Variable |
|---|---|---|
| Daily allowance spent | Per-user calls per UTC day | `LLM_DAILY_CALL_LIMIT_PER_USER` |
| Too many requests, try again shortly | Rolling per-minute burst | `LLM_USER_RATE_PER_MINUTE` |
| AI is unavailable, retrying in Ns | Whole ladder short-circuited | `LLM_CIRCUIT_RESET_S` |

All three refuse **before** spending anything. Note that one photo capture is roughly five calls, so
a per-minute limit set too tight fails halfway through a capture and leaves a draft the user cannot
finish. The default of 20 is deliberately loose for that reason.

### An upload is refused

Size is checked from the update itself, before any download, against `MAX_UPLOAD_MB` — and
Telegram's own Bot API cap of 20 MB is the ceiling regardless. Type is then checked **from the
bytes**, not from what the sending client claimed: a PDF announced as `image/png` is refused after
download and before any provider call. A real PNG mislabelled as JPEG is corrected rather than
refused, because phones mislabel images constantly.

### Something rendered wrong or a message did not send

The renderer escapes MarkdownV2 and splits at the 4096-character limit. If a note still fails to
send, capture the note that did it and the correlation id from the logs — that pairing is what makes
it reproducible as a test.

---

## 7. Security notes for operators

- **The allow-list is the entire access model.** Every user on it shares one NoteDiscovery
  credential and therefore has the same access to the whole vault. There are no per-user
  permissions. An empty `TELEGRAM_ALLOWED_USER_IDS` is rejected at startup rather than defaulting to
  "everyone".
- **`/metrics` is not authenticated.** Expose it to a scrape network, never to the internet.
- **`.env` is the secret store.** It is gitignored; keep it out of images and off shared hosts.
- **MCP transport is off by default and should stay off** unless you have a reason: it spawns a
  stdio subprocess, and in Docker launch mode that means access to the Docker socket, which is
  root-equivalent on the host. REST loses no functionality — MCP is a strict subset of it.
- Secrets are scrubbed from logs, but a log collector that ingests raw stdout from *other*
  containers is outside what this process can protect.

---

## 8. Runbook summary

```bash
make check-env              # validate configuration, no network
make docker/run             # start / rebuild
make docker/logs            # follow
make docker/stop            # stop and remove
make docker/shell           # shell inside the container
curl localhost:8080/healthz # liveness + version
curl localhost:8080/readyz  # readiness, with per-check verdicts
make verify-contract        # probe a live NoteDiscovery for the two edge behaviours
make audit                  # locked dependencies vs the advisory database
```
