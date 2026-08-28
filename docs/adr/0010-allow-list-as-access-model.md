# 0010 — An allow-list over one shared vault credential is the whole access model

**Status.** Accepted
**Date.** 2026-08-28

## Context

The bot fronts one NoteDiscovery instance with one credential. Several people may use it. The
alternative is per-user credentials and per-user scoping, which NoteDiscovery does not offer — it
would have to be invented in the bot, over an API with no concept of it.

## Decision

`TELEGRAM_ALLOWED_USER_IDS` (and optionally `TELEGRAM_ALLOWED_CHAT_IDS`) is an allow-list enforced
in handler group −1, before any other handler runs. Everyone on it shares one credential and
therefore has identical access to the whole vault.

An **empty allow-list is rejected at startup**. There is no configuration that means "everyone".

`/whoami` tells a user their own id, and a refusal tells an unlisted user theirs — because
populating the allow-list otherwise means finding a Telegram user id, and an access model nobody can
configure gets turned off.

## Consequences

- The security boundary is a single list, checked once, before anything else. It is easy to audit
  precisely because it is not per-feature.
- There are **no per-user permissions**. Someone on the list can read and delete any note. This is a
  shared-vault tool, not a multi-tenant one, and [OPERATIONS.md](../OPERATIONS.md) says so where an
  operator will see it.
- Per-user limits that do exist (the LLM daily cap, the per-minute burst) are about **cost**, not
  authorisation, and must not be mistaken for a permission boundary.
- Rejections are counted (`discoverygram_updates_total{outcome="rejected"}`) so a bot being probed
  is visible.
