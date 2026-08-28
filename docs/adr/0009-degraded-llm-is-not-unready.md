# 0009 — A degraded AI ladder is reported, not a readiness failure

**Status.** Accepted
**Date.** 2026-08-28

## Context

`/readyz` exists so an orchestrator can take an unhealthy instance out of service. The LLM providers
are a dependency, so the reflexive implementation fails readiness when they are all down.

But search, browse, read, `/new`, `/quick` and every navigation flow need no provider at all. Only
generation does. Failing readiness because a third-party model vendor is having a bad afternoon
takes away the features that still work, on purpose, and turns a partial outage into a total one.

## Decision

The readiness registry distinguishes **required** from **reported** checks. NoteDiscovery, the
session backend and the Telegram updater are required. The LLM ladder is registered with
`required=False`: it appears in the body as `"llm": "degraded"` while the overall verdict stays
`ready` and the endpoint returns `200`.

The degradation is still visible in three places — `/readyz`, `/status`, and the
`discoverygram_llm_circuit_open` gauge — so it is observable without being actionable by a scheduler
that cannot fix it.

## Consequences

- A model outage degrades the bot instead of removing it.
- Alerting on AI availability has to be built on the metrics, not on the readiness probe. That is
  the right place for it: it is a business signal, not a scheduling one.
- Readiness checks run concurrently, each bounded by a five-second timeout, with verdicts cached for
  two seconds — a hanging dependency yields an honest `503` rather than a probe that itself times
  out.
