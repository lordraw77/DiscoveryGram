# Usage walkthrough

Each headline flow, end to end, with the bot's actual replies.

> **How these transcripts were produced.** The bot replies below are the output of the real
> renderers in `bot/results.py` and `bot/notes.py`, driven with sample vault data — not
> hand-written approximations. They are shown as Telegram displays them; on the wire each is
> MarkdownV2 with every reserved character escaped. Where a transcript could not be generated
> mechanically it is marked **(structure)** and describes the sections rather than quoting exact
> text.

---

## 0. First contact

```
You:  /start

Bot:  Hello Alex.

      I connect this chat to your NoteDiscovery vault at `vault.internal:8000`.

      Send /help to see what I can do.
```

If you are not on the allow-list, this is where it ends — but the refusal tells you your own
Telegram id, which is the id to add to `TELEGRAM_ALLOWED_USER_IDS`:

```
You:  /whoami

Bot:  Your Telegram id: `48219044`
      This chat id: `48219044`
      Username: @alex
```

---

## 1. Searching

Full-text search across the vault. Results are ranked client-side — NoteDiscovery returns no
relevance score — by title match, match count and recency.

```
You:  /search kafka

Bot:  🔍 *2 results* for *kafka*

      *1.* Kafka retention
         `Projects/Streaming`
         _retention.ms on the compacted topic is still the 7-day default_
      *2.* 2026-08-14 Platform sync
         `Meetings`
         _agreed to move the audit stream off *kafka* before the migration_

      [ 1. Kafka retention               ]
      [ 2. 2026-08-14 Platform sync      ]
```

The matched term is highlighted inside the snippet. Each result is a button; tapping one opens the
note. With more results than `RESULTS_PAGE_SIZE`, a pagination row appears — paging costs **no vault
read**, because the whole result set is held in one session entry.

Any plain message you send is treated as `/search`, unless the operator set
`DEFAULT_TEXT_ACTION=quick` — in which case it is a capture instead, so a thought you meant to keep
never quietly becomes a query.

### The other three search modes

```
You:  /find retention.ms
```
Literal, case-sensitive. The API has no exact search, so this is full-text search filtered
client-side.

```
You:  /tag

Bot:  🏷 *3* tags

      `infra` — 14
      `meeting` — 9
      `kafka` — 3

      Open one with /tag <name>.
```

```
You:  /recent 7
```
Notes changed in the last 7 days (default `RECENT_DEFAULT_DAYS`).

---

## 2. Browsing

NoteDiscovery has no folder-tree endpoint; the tree is derived from the flat listing, cached, and
invalidated when a write moves something.

```
You:  /browse Projects

Bot:  📁 *Projects*
      `🏠 / Projects`
      _2 items_

      📁 Streaming (6)
      📄 Roadmap

      [ 📁 Streaming ]
      [ 📄 Roadmap   ]
      [ ⬆️ Up        ]
```

Breadcrumbs are buttons too, so you can jump back up several levels in one tap rather than pressing
*Up* repeatedly.

---

## 3. Reading a note

```
You:  /open Projects/Streaming/Kafka retention.md

Bot:  *Kafka retention*
      `Projects/Streaming/Kafka retention.md`
      #infra #kafka
      _42 lines · modified 2026-08-21 09:14_

      ## Current state

      retention.ms on the compacted topic is still the 7-day default.

      See [[Projects/Streaming/Broker sizing]].

      [ ✏️ Edit ] [ ➕ Append ] [ 🏷 Tag  ]
      [ 🔗 Backlinks ] [ 🕸 Related      ]
      [ 📋 Path ] [ 📄 Raw ] [ 🔗 Share  ]
      [ 🗑 Delete                        ]
```

Wiki-links in the body become buttons — `[[Projects/Streaming/Broker sizing]]` is one tap away. A
link pointing at a note that does not exist is listed as unresolved rather than silently dropped.

Long notes are split according to `LONG_NOTE_MODE`: `paged` gives a page navigation row, `split`
sends consecutive messages. Either way the renderer splits at the 4096-character limit without
breaking a code fence or a table.

```
You:  /backlinks Projects/Streaming/Kafka retention.md

Bot:  🔗 *1* notes link to `Projects/Streaming/Kafka retention.md`

      📄 *2026-08-14 Platform sync*
         `Meetings/2026-08-14 Platform sync.md`
         _blocked on [[Kafka retention]]_
```

`/related` does the same over the graph — notes linked in either direction.

---

## 4. Capturing a photo into a note — the headline flow

Send a photo with a caption. **(structure)**

```
You:  [photo of a whiteboard]
      caption: extract the text, save under Projects/Research, you generate the title

Bot:  📸 Reading the image…

Bot:  📝 *Draft — not saved yet*

      *Title:* Retention strategy for compacted topics
      *Path:*  `Projects/Research/Retention strategy for compacted topics.md`
      *Tags:*  #research #kafka

      ── body ──
      ## Whiteboard, 21 August

      Compacted topics keep the 7-day default. Options discussed:
      …

      [ 💾 Save ] [ ✏️ Title ] [ 📁 Path ]
      [ 🏷 Tags ] [ ❌ Discard          ]
```

What happens, in this order:

1. The size is checked **from the update itself**, before any download, against `MAX_UPLOAD_MB`.
2. The file is downloaded and its type determined **from the bytes**, not from what the client
   claimed — a PDF announced as `image/png` is refused here, before it costs a vision call.
3. The image is uploaded to the vault.
4. The model is asked for content: a transcription, a title, tags.
5. A draft is rendered.

**Nothing is in the vault until you tap Save.** The destination path came from the *caption parser*,
not from the model — a photo cannot choose where it is written, however it is captioned and whatever
text the image itself contains.

Send several photos at once and they become **one** note: the album buffer groups them, and the
first update wins the group.

Tap `Save`:

```
Bot:  ✅ Saved to `Projects/Research/Retention strategy for compacted topics.md`

      [ 📄 Open ] [ ✏️ Edit ]
```

`/cancel` at any point clears the draft. Every multi-step flow shares one pending key, so `/cancel`
clears whichever one you are in.

---

## 5. Creating notes by hand

```
You:  /new Projects/Streaming/Broker sizing.md Three brokers, 8 vCPU each.

Bot:  ✅ Saved to `Projects/Streaming/Broker sizing.md`
```

```
You:  /quick remember to check the audit stream lag

Bot:  ✅ Appended to today's inbox note
      `Inbox/2026-08-28.md`
```

`/quick` appends to a dated note under `INBOX_PATH` — capture with no decisions to make. Note the
asymmetry this creates internally: an append invalidates the **tag** cache (the appended text may
carry `#tags`) but not the **tree** cache, because appending cannot move a note.

Templates:

```
You:  /template

Bot:  📄 Pick a template

      [ Meeting notes ] [ Daily      ]
      [ Book note     ] [ Project    ]
```

Or directly: `/new --template "Meeting notes" Meetings/2026-08-28 Standup.md`

**Forwarded messages** go to a draft rather than being saved, because the title and the path are
exactly what you have not decided yet.

---

## 6. Asking things

```
You:  /summarize Projects/Streaming/Kafka retention.md
```
The note, summarised. The note itself is untouched.

```
You:  /ask what did we decide about the audit stream?

Bot:  Based on your notes, the audit stream is to be moved off Kafka before the
      migration — agreed at the 14 August platform sync, and still listed as
      blocked on the retention question.

      *Sources*
      • Meetings/2026-08-14 Platform sync.md
      • Projects/Streaming/Kafka retention.md
```

`/ask` retrieves context from the vault (`ASK_CONTEXT_NOTES` notes), grounds the answer in it, and
**names its sources** — so an answer can be checked rather than trusted.

---

## 7. Changing things

```
You:  /move Inbox/2026-08-28.md Journal/2026-08-28.md
You:  /folder rename Projects/Streaming Projects/Events
```

Editing goes through the per-note action bar. There is no note *update* endpoint in NoteDiscovery,
so an edit is read-modify-write over an upsert `POST`.

---

## 8. Checking on the bot

**(structure)** `/status` reports, in sections:

- **Instance** — NoteDiscovery name and version, with a warning marker when the version differs from
  the documented contract; and the vault size in notes and tags.
- **Sessions** — the backend in use and whether it answers.
- **Updates** — accepted and rejected counts.
- **AI** — the configured ladder per task (`Chat`, `Vision`), shown as the first rung plus how many
  more; any provider whose circuit is open, with the seconds until it retries; and the request count
  with failures.

```
You:  /status

Bot:  *DiscoveryGram status*
      Version: `2.0.0`
      Instance: `NoteDiscovery 0.31.3`
        Vault: 1,204 notes, 87 tags
        Sessions: `memory` ✅
        Updates: 1,891 accepted, 3 rejected
      *AI*
        Chat: `groq:llama-3.3-70b` +4 more
        Vision: `gemini:gemini-2.0-flash` +2 more
        Providers: all healthy
```

When a provider is failing, that last line names it instead:

```
        ⚠️ groq: circuit open, retrying in 42s
```

That is informational. A degraded ladder does **not** make the bot unready — search, browse, read
and create keep working, and `/readyz` still returns `200`. See
[adr/0009](adr/0009-degraded-llm-is-not-unready.md).

---

## 9. When something is refused

Every refusal is one actionable sentence rather than a stack trace:

| Situation | What you see |
|---|---|
| Not on the allow-list | Refused, plus your own Telegram id so it can be added |
| Query shorter than `SEARCH_MIN_QUERY_LENGTH` | Asked for a longer query — the API does not expose its minimum, so this is enforced client-side |
| Upload over `MAX_UPLOAD_MB` | The limit, before anything is downloaded |
| File is not the image it claims | Refused after download, before any AI call |
| Daily AI allowance spent | The cap, and that it resets at UTC midnight |
| Too many AI requests in a minute | Try again shortly |
| Every AI provider down | Unavailable, with the seconds until retry |

The last three all refuse **before** spending anything — you are never charged for a request the bot
declined to attempt.

---

## Next

- Every command and flag: [FEATURES.md](FEATURES.md)
- Every setting: [CONFIGURATION.md](CONFIGURATION.md)
- Running it for real: [OPERATIONS.md](OPERATIONS.md)
