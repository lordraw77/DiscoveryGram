"""Entry point: settings loading.

The NoteDiscovery readiness probe moved into `RestNoteStore.health` in phase 1
and is covered by `tests/test_rest_note_store.py`.
"""

from __future__ import annotations

import pytest

from discoverygram.__main__ import load_settings
from discoverygram.config import Settings


def test_load_settings_returns_settings(env: None) -> None:
    assert isinstance(load_settings(), Settings)


def test_load_settings_exits_with_code_2_on_bad_config(
    env: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A misconfigured .env must fail fast with a readable message, not a traceback."""
    monkeypatch.delenv("NOTEDISCOVERY_URL", raising=False)

    with pytest.raises(SystemExit) as exit_info:
        load_settings()

    assert exit_info.value.code == 2
    stderr = capsys.readouterr().err
    assert "Invalid configuration" in stderr
    assert "NOTEDISCOVERY_URL" in stderr
