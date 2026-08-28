# Development

How to work on DiscoveryGram: the toolchain, the layout, the conventions the code already follows,
and how to add things without breaking the properties the tests protect.

For *running* a deployment see [OPERATIONS.md](OPERATIONS.md); for the design itself see
[ARCHITECTURE.md](ARCHITECTURE.md).

---

## 1. Setup

Python **3.12+** and [uv](https://docs.astral.sh/uv/). Nothing else — uv manages the interpreter,
the virtual environment and the lockfile.

```bash
git clone https://github.com/lordraw77/DiscoveryGram.git
cd DiscoveryGram
make install          # uv sync --all-extras
make check            # lint, type-check, test — should be green on a fresh clone
```

`make check` on a clean checkout is the baseline. If it is not green before you change anything,
fix that first rather than adding to it.

To run the bot locally you need a `.env`:

```bash
cp .env.example .env
$EDITOR .env
make check-env        # validates it and prints the LLM ladder — no network
make run
```

---

## 2. The targets

```bash
make help             # every target, with its one-line description
```

| Target | What it does |
|---|---|
| `make install` | Install everything, including dev extras |
| `make lock` | Refresh `uv.lock` |
| `make format` | Auto-fix formatting and whatever ruff can fix |
| `make lint` | `ruff check` + `ruff format --check` |
| `make typecheck` | mypy, **strict**, over `src`, `tests` and `scripts` |
| `make test` | pytest with coverage, live tests excluded |
| `make test-live` | The opt-in tests that need a real instance and real credentials |
| `make check` | `lint typecheck test` — exactly what CI runs |
| `make audit` | Locked dependencies against the advisory database (needs network) |
| `make version` | The version the build backend will produce, from the git tag |
| `make docker/build` | Single-arch image for the current machine |
| `make docker/buildx` | Multi-arch build (amd64 + arm64), cache only — proves both compile |
| `make docker/push` | Multi-arch build and push; refuses a non-release version |
| `make docker/pins` | Current digests of the base images, for refreshing the Dockerfile |
| `make release` | `check` + build, warning when the commit is not tagged |

---

## 3. Layout

```
src/discoverygram/
  __main__.py     entry point: settings, logging, health, probe, run, shutdown
  health.py       /healthz, /readyz, /metrics
  config/         settings, loaded from the environment only
  ports/          the interfaces the application talks to, and the domain model
  adapters/       implementations of those ports: REST, MCP, cache, sessions, throttle
  app/            services — search, navigation, notes, capture, ingest, intent, assist
  bot/            the Telegram layer: handlers, rendering, keyboards, tokens, errors
  llm/            the provider router: adapters, ladder, breaker, usage accounting
  util/           logging, correlation, metrics, media sniffing, paths
```

### Layering rules

Dependencies point **inwards**, and only inwards:

```
bot  →  app  →  ports  ←  adapters
                  ↑
                 llm
```

- `ports/` depends on nothing but the standard library. It defines the interfaces and the domain
  model, and is the only module every other layer is allowed to import.
- `app/` holds the services. It knows about `ports`, never about Telegram and never about HTTP.
- `adapters/` and `llm/` implement ports. They know about HTTP, subprocesses and vendor dialects.
- `bot/` is the only layer that imports `telegram`.

The practical test: if a service in `app/` imports anything from `bot/`, the layering has been
broken and the service has become untestable without a Telegram update object.

---

## 4. Conventions

- **English only.** Code, identifiers, comments, docstrings, commit messages and documentation.
  This is a project constraint, not a style preference.
- **Async throughout.** No blocking call may reach the event loop: no `time.sleep`, no synchronous
  file or socket I/O, no fire-and-forget `create_task`. This is asserted, not assumed — see the
  concurrency review in [ROADMAP.md](ROADMAP.md).
- **Configuration comes from the environment.** No literals, no config file with secrets. A new
  setting is a field on `Settings`, a row in `.env.example` and a row in
  [CONFIGURATION.md](CONFIGURATION.md) — all three, in the same change.
- **Comments explain why, not what.** The existing comments carry the reasoning behind non-obvious
  choices; match that density rather than narrating the code.
- **Documentation ships with the change that needs it.** A behaviour change with no doc change is
  an incomplete change.

### Tooling

`ruff` at line length 100 with a broad rule set including `ASYNC`, `S` (bandit) and `PTH`. `mypy` in
**strict** mode over `src`, `tests` *and* `scripts` — tests are type-checked too, which is what stops
a fixture from drifting away from the type it is standing in for.

Per-file ignores exist and are justified in `pyproject.toml`; add one only with the reason next to
it.

---

## 5. Testing

```bash
make test                                  # everything except live
uv run pytest tests/test_search_service.py # one file
uv run pytest -k "circuit" -v              # one behaviour
uv run pytest --cov --cov-report=html      # then open htmlcov/index.html
```

Live tests are marked and excluded by default:

```python
@pytest.mark.live
```

They need a real NoteDiscovery instance and real credentials, and run only via `make test-live`.

### What the suite is for

Coverage sits at **94%**, but the number is not the point — the properties are. The suite exists to
pin down behaviours that are easy to break and expensive to notice:

- **Rendering is asserted against the format itself.** Every reply is checked MarkdownV2-safe
  against every reserved character individually, plus tables, code fences and the 4096-character
  boundary. This has already caught an unsendable `/status`.
- **Fault injection is a test, not a thought experiment** (`tests/test_resilience.py`): NoteDiscovery
  refusing connections, flapping between 503 and 200, rate-limiting with `Retry-After`, a Redis that
  raises on `ping`, every provider failing, Telegram answering with 429.
- **Cache invalidation asymmetry is asserted.** A write drops the tree *and* the tags; an append
  drops the tags *only*, because appending cannot move a note. A failed write invalidates nothing,
  and an outage is never cached.
- **Security properties are asserted against the wire.** A filename of `../../../etc/cron.d/evil.png`
  is checked against the multipart body actually sent. A PDF announced as `image/png` is checked to
  be refused after download and before any upload or provider call.

When you fix a bug, the regression test should assert the property that was violated, not merely
re-run the reproduction. The `blocks()` predicate on the circuit breaker exists because a test
proved that *asking* whether a provider was down consumed its half-open probe — a look must never be
a call.

---

## 6. Adding things

### A new command

1. Write the handler in the relevant `bot/` module and add it to that module's `COMMANDS` mapping —
   `build_application` merges them, so there is no second registration to keep in sync.
2. Add a `BotCommand` to `COMMAND_MENU` and a line to `HELP_TEXT` in `bot/commands.py`.
3. Put the logic in `app/`, not in the handler. The handler parses the update, calls a service and
   renders; anything else belongs a layer in.
4. Document it in [FEATURES.md](FEATURES.md).

**Handler order matters and is not incidental.** Commands are offered the update before any text
handler, or every `/whatever` would become a search. A pending draft or edit claims the message
before anything else, or text meant as a title would also be run as a query. Quick capture precedes
search, because with `DEFAULT_TEXT_ACTION=quick` a message the user meant to keep must not quietly
become a query. If you add a text handler, work out where it belongs in that order and say why in a
comment.

### A new setting

A field on `Settings`, a row in `.env.example`, a row in [CONFIGURATION.md](CONFIGURATION.md), and a
test if it has a cross-field rule. `make check-env` will pick it up for free.

### A new LLM provider

If it speaks the OpenAI dialect, it is configuration rather than code: add it to the provider
configs and it gets the shared adapter, the retry ladder, the circuit breaker and the usage
accounting with no new code path. A genuinely different dialect needs its own adapter alongside
`gemini`, `cloudflare` and `puter` — see [LLM_PROVIDERS.md](LLM_PROVIDERS.md).

### A new metric

Low-cardinality labels only, drawn from fixed sets: provider names, HTTP methods, outcomes. **Never**
a note path, a query or a user id. A label taken from user input is how a metrics endpoint becomes
an out-of-memory, and the note path was right there in the obvious version of the NoteDiscovery
latency metric.

---

## 7. Versioning and release

The version comes from the **git tag** via hatch-vcs, never from a literal:

```bash
make version      # 2.0.0 on a tagged commit, 2.0.1.dev1+gad5a1eb otherwise
```

To cut a release:

```bash
make check && make audit
git tag -a v2.1.0 -m "2.1.0"
git push origin v2.1.0        # CI builds, verifies and publishes the multi-arch image
```

CI triggers on tags. The quality job runs lint, type-check and tests, and the image job **depends on
it** — a failing test cannot publish `latest`. The published manifest is then verified to carry both
`linux/amd64` and `linux/arm64`, and the image is run to confirm it reports the expected version.

`.git` is deliberately not in the Docker build context, so a bare `docker build` cannot see the tag
and honestly reports `0.0.0+unknown`. Pass it explicitly if you are not using the Makefile:

```bash
docker build --build-arg VERSION="$(make -s version)" -t discoverygram:local .
```

Base images are pinned by digest. `make docker/pins` prints the current ones; updating them is a
deliberate commit, not a side effect of a rebuild.

---

## 8. Before opening a change

```bash
make format
make check
make audit        # if you touched dependencies
```

And check the three things a green `make check` does not:

- Did the behaviour change? Then the documentation changed in the same commit.
- Did a setting appear? Then `.env.example` and `CONFIGURATION.md` have it.
- Did you add a design decision worth arguing about later? Then it belongs in
  [adr/](adr/README.md).
