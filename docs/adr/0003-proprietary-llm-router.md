# 0003 — Build the LLM router rather than adopt a framework

**Status.** Accepted
**Date.** 2026-08-28

## Context

The bot needs chat and vision across nine providers, with retry, failover, per-provider circuit
breaking, cost accounting and per-user limits. LangChain, LiteLLM and similar libraries offer parts
of this.

## Decision

Build the router. One `LlmClient` port; one shared adapter for the six OpenAI-compatible providers;
dedicated adapters for `gemini`, `cloudflare` and `puter`, which do not speak that dialect.

A request walks a configured (provider, model) ladder with retry, exponential backoff and
`Retry-After` awareness. A per-provider circuit breaker skips a provider's **remaining models in one
step** rather than burning retries on a key that is already known to be rejected.

## Consequences

- The behaviours that actually matter here are ours to specify: what a failover costs the user's
  quota (one call, not one per rung), when a circuit opens, what happens when the whole ladder is
  short-circuited (refuse immediately with the cool-down, spending nothing).
- Adding an OpenAI-compatible provider is configuration, not code.
- We own the vendor dialect quirks, and they are real — three providers needed their own adapter.
  [LLM_PROVIDERS.md](../LLM_PROVIDERS.md) is the tax this decision charges.
- No dependency on a fast-moving framework's release cadence, and no framework abstraction to fight
  when a provider behaves unusually.
