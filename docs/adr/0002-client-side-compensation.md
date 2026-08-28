# 0002 — Compensate for missing NoteDiscovery capabilities client-side

**Status.** Accepted
**Date.** 2026-08-28

## Context

Contract discovery found that several capabilities the plan assumed do not exist in NoteDiscovery
0.31.3: there is no exact/literal search, no note *update* distinct from create, no folder-tree
endpoint, and search returns no relevance score. The choices were to drop the features, to wait on
an upstream change we do not control, or to build them over the primitives that do exist.

## Decision

Build them client-side, in the adapter layer, over the endpoints that exist:

- **Folder tree** — derived from the flat note listing, cached with a TTL, invalidated on write.
- **Literal search** — the API's full-text search, then filtered client-side for a case-sensitive
  match.
- **Ranking** — computed locally from title matches, match count and recency, because the API
  returns no score.
- **Editing** — read-modify-write over `POST`, which is an upsert (it is the editor's autosave
  endpoint, confirmed in source).
- **Rate-limit pacing** — a client-side throttle, so the bot does not have to discover the server's
  limit by hitting it.

All of it lives behind the `NoteStore` port. `app/` and `bot/` cannot tell which operations are
native and which are compensated.

## Consequences

- Features ship without an upstream dependency.
- Compensation costs reads. The derived tree is a full vault listing, which is exactly why it is
  cached, single-flighted, and invalidated on write but **not** on append — appending cannot move a
  note.
- The compensations are the parts most likely to break on a NoteDiscovery upgrade. Hence the
  version-stamped contract document, the startup version check, and `make verify-contract`.
- Read-modify-write has a lost-update window. Acceptable for a single-vault personal tool; it would
  not be for concurrent editors.
