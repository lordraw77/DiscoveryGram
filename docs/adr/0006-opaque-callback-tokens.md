# 0006 — Callback data is an opaque token over a server-side session

**Status.** Accepted
**Date.** 2026-08-28

## Context

Telegram caps `callback_data` at **64 bytes**. A note path alone routinely exceeds that — the paths
used to develop this were three times the limit — so encoding state into the button is not a design
choice, it is impossible. Truncating or hashing paths into the callback would also put user-derived
data on a channel that comes back from the client and is trusted on the way in.

## Decision

Callback data is `action:token`. The token is an opaque, server-issued key into a session store; the
real state — path, page, result set, draft — lives server-side, keyed by it.

The session store is a port with two implementations, memory and Redis, chosen by
`SESSION_BACKEND`.

## Consequences

- Any state of any size can back a button, and the 64-byte limit stops being a design constraint.
- Nothing user-controlled arrives from the callback channel except a token that is either known or
  refused.
- Sessions expire (`SESSION_TTL_S`), so a very old message's buttons stop working. Correct: the note
  behind them may not exist any more.
- With `SESSION_BACKEND=memory` a restart invalidates every open keyboard. Acceptable — see
  [0004](0004-preview-before-write.md) on drafts being disposable — and Redis is there for
  deployments that would rather not.
- Pagination costs no vault read and leaves one session entry however long the browse.
