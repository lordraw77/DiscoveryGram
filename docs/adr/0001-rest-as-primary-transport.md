# 0001 — REST is the primary NoteDiscovery transport; MCP is flag-gated and off

**Status.** Accepted
**Date.** 2026-08-28

## Context

NoteDiscovery exposes two interfaces: a REST API and an MCP server. MCP was the more interesting
option on paper — a typed tool surface, designed for exactly this kind of client.

Contract discovery against NoteDiscovery 0.31.3 settled it on facts rather than taste:

- MCP exposes **18 tools that are a strict subset of REST**. Media upload, export, sharing and
  folder move/rename/delete have no MCP equivalent at all. Note creation from a photo — the headline
  flow — is impossible over MCP.
- MCP is **stdio-only**. There is no network transport, so a client must spawn the server as a
  subprocess. From inside our container that means either mounting the Docker socket, which is
  root-equivalent access to the host, or vendoring NoteDiscovery's module into our image.

## Decision

REST is the transport. `NOTEDISCOVERY_TRANSPORT=rest` is the default and the supported path.

The MCP adapter is built, tested and behind a flag that defaults to off. It implements the same
`NoteStore` port, so it is a drop-in — it simply cannot serve the operations MCP does not have, and
raises `Unsupported` for them rather than pretending.

## Consequences

- No Docker socket is mounted in the default deployment, and the root-equivalent risk stays
  hypothetical.
- Choosing MCP is choosing a smaller feature set. That is the operator's call to make explicitly,
  which is what a flag defaulting to off means.
- The `NoteStore` port earns its existence: two real implementations, not one implementation and an
  interface written in hope.
- Both transports must invalidate their caches identically, and both are asserted to.
