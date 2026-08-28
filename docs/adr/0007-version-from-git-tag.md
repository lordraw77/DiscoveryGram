# 0007 — The version comes from the git tag, not from a literal

**Status.** Accepted
**Date.** 2026-08-28

## Context

A version literal in `pyproject.toml` is a second place to remember, and the failure mode is silent:
an image tagged `1.2.0` that reports `1.1.0` because someone forgot the bump. For a bot deployed as
a container, "what is actually running" has to be answerable from the running process.

## Decision

hatch-vcs derives the version from the git tag. A tagged commit produces that tag (`2.0.0`); any
other commit produces a PEP 440 development version pointing at it (`2.0.1.dev1+gad5a1eb`). The
process reports its own version at `/healthz`, in `/status` and in `discoverygram_build_info`.

`.git` is deliberately **not** in the Docker build context. The Makefile computes the version and
passes it as a build arg; a bare `docker build` with no arg yields `0.0.0+unknown` rather than a
plausible-looking lie.

## Consequences

- There is no version to bump and no way for the tag and the reported version to disagree.
- A development build says so, in a way that is visible in the deployed container. `make release`
  refuses to call it a release, and `make docker/push` refuses outright.
- CI verifies the published image reports the expected version, so the check is not merely a
  convention.
- Anyone building without the Makefile must pass `--build-arg VERSION=...` or accept
  `0.0.0+unknown`. Documented in [CONFIGURATION.md](../CONFIGURATION.md#versioning).
