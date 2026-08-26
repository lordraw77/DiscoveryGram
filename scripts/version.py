"""Print the version the build backend would produce.

The version has exactly one source: the git tag. `hatch-vcs` derives it at build
time, and this script derives it the same way — through `setuptools_scm` with the
options declared in `pyproject.toml` — so the tag the Makefile stamps on an image
can never disagree with the version inside it.

    $ uv run python scripts/version.py
    0.1.1.dev4+g1a2b3c4

Falls back to the installed distribution's metadata when git history is not
available (inside the container, where `.git` is not part of the build context),
and to `0.0.0+unknown` when neither is.

Run:  make version
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def from_git() -> str | None:
    try:
        from setuptools_scm import get_version
    except ImportError:
        return None
    try:
        return get_version(
            root=str(ROOT),
            version_scheme="guess-next-dev",
            local_scheme="node-and-date",
        )
    except Exception:
        # No repository, no tags, or a shallow clone: not an error here, just
        # a reason to fall through to the installed metadata.
        return None


def from_metadata() -> str | None:
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as package_version

    try:
        return package_version("discoverygram")
    except PackageNotFoundError:
        return None


def resolve() -> str:
    return from_git() or from_metadata() or "0.0.0+unknown"


if __name__ == "__main__":
    sys.stdout.write(resolve() + "\n")
