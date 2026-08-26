"""DiscoveryGram — a Telegram front-end for NoteDiscovery."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _package_version

try:
    # Baked in at build time by hatch-vcs, which reads the git tag. Reading it
    # back from the installed distribution keeps one source of truth: there is
    # no version literal anywhere in the source tree.
    __version__ = _package_version("discoverygram")
except PackageNotFoundError:  # pragma: no cover - only when running uninstalled
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
