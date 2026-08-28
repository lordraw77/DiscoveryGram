# 0004 — Nothing generated reaches the vault without an explicit tap

**Status.** Accepted
**Date.** 2026-08-28

## Context

The headline flow is: send a photo, the bot reads it, writes it up, and it becomes a note. The
tempting version writes the note and shows the user what was written. It is one fewer tap, and the
user can always delete it.

But a model writing directly into a personal knowledge vault means every hallucination, every
misread caption and every wrong path resolution is a durable artefact the user has to find and clean
up. The vault is the one piece of state that matters.

## Decision

Generated content goes to a **preview card**. Title, destination path, tags and body are all shown,
and all editable. Nothing is written until `Save` is tapped.

The order is the safety property, and it is asserted: the upload happens, the model is called, the
draft is rendered — and only a tap reaches the vault.

## Consequences

- The worst outcome of a bad generation is a discarded draft.
- Drafts are session state, so they are lost on restart. That is the correct trade: a lost draft
  costs a retake, a wrongly written note costs a cleanup.
- Every creation flow — photo, album, forward, quick capture, template — funnels through one draft
  mechanism sharing one pending key, which is what makes `/cancel` able to clear all of them.
- One extra tap on every capture. Deliberate.
