# 0008 — Emit Prometheus exposition without the Prometheus client library

**Status.** Accepted
**Date.** 2026-08-28

## Context

The bot needs counters, gauges and histograms on a `/metrics` endpoint. `prometheus_client` is the
obvious dependency.

It would also bring a second global registry, a second concurrency model, and a WSGI server to keep
out of the event loop — to produce the same output. The actual requirement is three instrument types
with low-cardinality labels, and the exposition format is a few lines of text.

## Decision

Implement the instruments and the exposition format directly, served by the existing health server
on `HEALTH_PORT`, gated by `METRICS_ENABLED`.

**Instruments always record; only the endpoint is gated.** Enabling metrics on a running process
gives numbers from that process rather than from zero, and no counter site in the codebase has to
ask whether metrics are switched on.

## Consequences

- One fewer dependency, one HTTP server, one concurrency model.
- The exposition format is ours to get right, so it is asserted against the format itself:
  cumulative buckets, a trailing newline, and label values containing `"`, `\` and a newline that
  cannot break a scrape.
- No Prometheus client features we did not build: no multiprocess mode, no push gateway, no default
  process collectors. None are needed for a single-process bot.
- **No metric label may come from user input** — not a note path, not a query, not a user id. Series
  count is bounded by configuration, not by the vault. This constraint is enforced by the label sets
  the instruments are constructed with, and it very nearly went wrong: the note path was right there
  in the obvious version of the NoteDiscovery latency metric.
