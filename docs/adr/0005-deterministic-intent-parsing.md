# 0005 — The model is asked for content, never for control flow

**Status.** Accepted
**Date.** 2026-08-28

## Context

A photo caption like *"extract the text, save under Projects/Research, you generate the title"*
carries both an instruction and a destination. The natural implementation asks the model to return
structured intent: the path, the flags, the action.

That makes the caption — and by extension anything visible in an uploaded image — able to choose
where the bot writes. It is prompt injection with filesystem consequences, and the attacker does not
even need to be the user: an image containing text is content the model reads.

## Decision

Captions are parsed by **deterministic rules**, not by a model. Paths, flags and actions come from
the parser. The model is asked only for content: a title, a summary, a transcription, a body.

A generated path is never used as a path. A model can influence what a note *says*, never where it
goes or whether it is written.

## Consequences

- A photo cannot choose its own destination, however it is captioned and whatever it contains.
- Caption syntax is a fixed vocabulary rather than free natural language. Less magical; the trade is
  deliberate and documented in [FEATURES.md](../FEATURES.md).
- The parser is pure and fully unit-tested, with no provider call in the loop.
- Path resolution stays in one place and is subject to the same normalisation as every other path.
