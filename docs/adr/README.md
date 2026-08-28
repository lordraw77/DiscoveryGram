# Architecture Decision Records

One file per decision that was **contested, expensive, or would look arbitrary to someone reading
the code later**. Not every choice needs a record; these are the ones where knowing the alternative
that was rejected, and why, is what stops the decision being quietly undone.

Each record is immutable once accepted. A decision that changes gets a **new** record that
supersedes the old one, and the old one stays — a record you can no longer read is a record that
cannot tell you why the current design is what it is.

| # | Decision | Status |
|---|---|---|
| [0001](0001-rest-as-primary-transport.md) | REST is the primary NoteDiscovery transport; MCP is flag-gated and off | Accepted |
| [0002](0002-client-side-compensation.md) | Compensate for missing NoteDiscovery capabilities client-side | Accepted |
| [0003](0003-proprietary-llm-router.md) | Build the LLM router rather than adopt a framework | Accepted |
| [0004](0004-preview-before-write.md) | Nothing generated reaches the vault without an explicit tap | Accepted |
| [0005](0005-deterministic-intent-parsing.md) | The model is asked for content, never for control flow | Accepted |
| [0006](0006-opaque-callback-tokens.md) | Callback data is an opaque token over a server-side session | Accepted |
| [0007](0007-version-from-git-tag.md) | The version comes from the git tag, not from a literal | Accepted |
| [0008](0008-metrics-without-prometheus-client.md) | Emit Prometheus exposition without the Prometheus client library | Accepted |
| [0009](0009-degraded-llm-is-not-unready.md) | A degraded AI ladder is reported, not a readiness failure | Accepted |
| [0010](0010-allow-list-as-access-model.md) | An allow-list over one shared vault credential is the whole access model | Accepted |

## Format

```markdown
# NNNN — Title

**Status.** Accepted | Superseded by NNNN
**Date.** YYYY-MM-DD

## Context
What forced a decision. The constraint, not the preference.

## Decision
What was chosen, stated so that a reader can tell whether the code still obeys it.

## Consequences
What this costs, what it rules out, and what it makes true.
```
