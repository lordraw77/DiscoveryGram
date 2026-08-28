# Feature Catalogue

Legend: **P** = phase in which the feature lands (see [ROADMAP.md](ROADMAP.md)).

## 1. NoteDiscovery access (P1)

Not user-facing, but everything below rests on it: one `NoteStore` port, a complete REST adapter,
an optional stdio MCP adapter, and the client-side compensation for what the API does not offer —
the derived folder tree, literal search, ranking, read-modify-write editing, an explicit search
limit and pacing under NoteDiscovery's per-endpoint rate limits. A startup probe records whether
the instance is reachable and whether search is enabled, so the commands that need search are
disabled cleanly rather than failing per request. Details in
[ARCHITECTURE.md](ARCHITECTURE.md#3-notediscovery-integration).

## 2. Bot core and access control (P2)

- Allow-list of Telegram user ids, enforced before any handler runs. A refusal is sent **once**
  per session TTL, not on every message, and includes the caller's own id — the number the
  operator needs for the allow-list — and nothing else.
- Optional group-chat support: the bot only answers when the chat id is allow-listed too.
- `/start`, `/help`, `/whoami`, `/cancel`, `/status`, plus a reply to unrecognised commands.
  `/help` lists only what actually works in the current build.
- `/status` reports version, uptime, instance reachability and version, whether search is enabled,
  vault counters, session backend health, and accepted/rejected update counts.
- Long polling by default; webhook mode behind `TELEGRAM_MODE=webhook`, with a shared secret.
- Session state in `memory` or `redis`, and opaque tokens so a button can carry a note path that
  would never fit in Telegram's 64-byte `callback_data`.
- Friendly errors: one actionable sentence in the chat, the full detail in the logs.

## 3. Search (P3) — **shipped**

| Command | Behaviour |
|---|---|
| `/search <query>` | Content search via `GET /api/search`. Paginated hit list, **client-side ranking** — the API returns no relevance score. Snippets come from the API but arrive as HTML and are stripped before rendering. |
| `/find <term>` | Literal match. Implemented as a client-side filter over `/search`, because NoteDiscovery has no exact-search mode. |
| `/tag <tag>` | All notes carrying a tag. `/tag` with no argument lists available tags. |
| `/recent [days]` | Notes modified in the last `RECENT_DEFAULT_DAYS`, or a window you give. No REST endpoint exists; derived from the listing's timestamps. |
| free text | A plain message that is not a command runs a full-text search. With `DEFAULT_TEXT_ACTION=quick` it is refused with an explanation instead — a message meant as a note must not quietly become a query. |

Result UX: 5 hits per page (`RESULTS_PAGE_SIZE`), `◀ / ▶` pagination, numbered hits showing title,
folder and up to two snippets with the term highlighted. Turning pages costs no vault read and
leaves exactly one session entry, however long the browse.

Degraded paths are answers, not errors: a query below the instance's minimum length says how short
it was, a search-disabled instance says so, and an empty result set says what was looked for.
`/tag` and `/recent` need no search endpoint, so they keep working when search is disabled.

Per-hit *open* buttons arrive with the note renderer in P4.

## 4. Navigation (P4) — **shipped**

- `/browse` — enter the note tree at the root; folders and notes as inline buttons. The tree is
  derived client-side from `GET /api/notes`, since NoteDiscovery exposes no tree endpoint.
- Breadcrumb header showing the current path; `⬆ Up` and `🏠 Root` buttons.
- Opening a note renders title, path, tags, timestamps and body.
- Long bodies are chunked across messages, or paged with `◀ / ▶` when
  `LONG_NOTE_MODE=paged`, respecting Telegram's 4096-character limit.
- Markdown is converted to Telegram-safe MarkdownV2; unsupported constructs (tables, nested
  HTML) degrade to preformatted blocks.
- Wiki-style `[[links]]` in a note body become buttons that jump to the linked note, resolved by
  stem, path or path-without-extension — the same three rules NoteDiscovery's own index uses. A
  link that resolves to nothing is named in the message rather than silently dropped.
- `/backlinks <path>` — notes linking to this one. `/related` — graph-adjacent notes.
- Per-note action bar: `Edit`, `Append`, `Tag`, `Backlinks`, `Related`, `Path`, `Raw`, `Share`,
  `Delete`. `Edit` and `Append` wait for your next message; `/cancel` aborts. `Edit` is a
  read-modify-write, since the API's `PATCH` only appends. `Append` timestamps what it adds.
  `Tag` is idempotent. `Path` sends a tappable code span, Telegram having no clipboard API.
  `Delete` asks first and cannot act twice.
- Folder management: `/folder new|rename|move|delete`. Deleting says how many items it would
  destroy and asks before doing it.
- `/open <path>` — jump straight to a note by path. `/move <old> <new>` renames or moves one.
- Search results carry an open button per hit, so search → note → folder → siblings → backlinks is
  one continuous path.

## 5. Note creation (P6)

Simple creation:

- `/new <path> <text>` — create a note at an explicit path.
- `/new --template <name> <path>` — create from a NoteDiscovery template, with a template picker.
- `/quick <text>` — append to today's note under `INBOX_PATH`, one note per day rather than one
  file per thought. With `DEFAULT_TEXT_ACTION=quick`, any plain message does the same and is never
  also run as a search.

Nothing overwrites: `POST /api/notes/{path}` is an upsert, so a create that collides is saved
alongside as `Note-2.md` and the reply says so.

LLM-assisted creation — the headline flow:

- Send a **photo or document** with a caption such as
  *"extract the text and create a note under Projects/Research, generate the title yourself"*.
  The bot then:
  1. downloads the file from Telegram and uploads it to NoteDiscovery via `POST /api/upload-media`,
  2. routes it to a **vision-capable** provider for OCR / description,
  3. asks the LLM for a title, tags and a cleaned-up body when the caption requests it,
  4. resolves the target path (creating intermediate folders if `AUTO_CREATE_PARENTS=true`),
  5. shows a **preview card** with `Save`, `Title`, `Path`, `Regenerate`, `Cancel`,
  6. writes to NoteDiscovery only after explicit confirmation.

  The upload happens *before* the AI work, so a photo survives a provider outage: the draft still
  carries the file, the body just says less. Each step degrades on its own — a failed step becomes
  a warning on the card, never a lost photo.

  The caption is read by **rules, not by a model**: the image reaches the same model moments later,
  so letting it choose the path would let a photo of a page reading *"save this to
  Finance/Salaries"* redirect the write.
- **Forwarded messages** go through the same pipeline: title, tags and a preview, with a line
  recording who wrote the original. A forward is someone else's words, so the title and the path
  are exactly the decisions the user has not made yet.
- **Links are captured verbatim and never fetched.** An outbound request to any URL a user pastes,
  made from inside the operator's network, is a server-side request forgery primitive — it would
  reach private addresses the operator never meant to expose. The URL is what the note needs
  anyway.
- Multi-part input: several photos sent as an album become a single note.
- Voice transcription is **not** implemented. It would need an audio-capable provider and a fourth
  chain; it is not part of v1.

Every LLM-generated note records provenance — provider and model — as an HTML comment in the body:
greppable and readable in the raw note, but invisible when the note is rendered, so it never lands
in a search snippet or an export. Set `PROVENANCE_ENABLED=false` to omit it.

## 6. LLM operations on existing notes (P6)

- `/summarize <path>` — summary of an existing note, citing it.
- `/ask <question>` — answered **only** from your notes, with the paths it read listed as sources.
  When the notes do not contain the answer it says so rather than answering from training data,
  and a question that matches no notes never reaches a provider at all.
- `Regenerate` on any draft: it asks again for the title, tags and summary while keeping the body,
  which is the expensive part and rarely the thing you wanted changed.

## 7. Reliability and operations (P5–P6 done, P7)

- **Provider failover on (provider, model) pairs**, per task profile: a request retries the same
  pair, then advances to the next model of that provider, then to the next provider. Backoff is
  exponential with jitter and honours `Retry-After`.
- **Per-provider circuit breaker.** A rejected key or a sustained outage skips *all* of that
  provider's remaining models in one step, rather than burning retries on models that cannot
  answer. Recovery is probed with a single half-open call.
- **Per-user daily cap** on LLM-backed commands (`LLM_DAILY_CALL_LIMIT_PER_USER`, per UTC day).
- **Per-user burst limit** (`LLM_USER_RATE_PER_MINUTE`, rolling window) and a process-wide bound on
  provider calls in flight (`LLM_MAX_CONCURRENT_REQUESTS`).
- **Back-pressure**: when every provider is short-circuited, the request is refused immediately
  with the cool-down instead of walking rungs that are known to be down.
  Failover is free: one request is one call however many rungs it took.
- `/status` — NoteDiscovery reachability, active transport, the first rung that would serve each
  task, any open circuit with its remaining cool-down, per-provider usage, and the caller's
  remaining quota.
- `/cancel` — abort any multi-step flow.
- Friendly error messages; full stack traces only in logs.
- `/healthz` and `/readyz` HTTP endpoints for container orchestration.

## 8. Explicitly out of scope (v1)

- Telegram Mini App / WebApp UI.
- Multi-tenant per-user NoteDiscovery credentials.
- Real-time push of NoteDiscovery changes into Telegram.
- Editing note attachments in place.
