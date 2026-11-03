"""Keep the test suite out of the real application's data directory."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _never_run_the_real_tailscale(monkeypatch):
    """Refuse to execute Tailscale from a test. On macOS the `tailscale` CLI is the Tailscale *app*
    binary, so running it opens the app."""
    real_run = subprocess.run

    def guarded_run(command, *args, **kwargs):
        argv = command if isinstance(command, (list, tuple)) else [command]
        if any(Path(str(arg)).stem.lower() == "tailscale" for arg in argv):
            raise OSError("tests never run the real Tailscale")
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", guarded_run)


@pytest.fixture(autouse=True, scope="session")
def _keep_the_real_log_clean(tmp_path_factory):
    """Send `main.log` output to a temp file, leaving `LOG_PATH` untouched."""
    try:
        import main
    except Exception:
        # A suite run without pywebview cannot import main; nothing to redirect.
        yield
        return

    scratch = tmp_path_factory.mktemp("rainette-log") / "rainette-music.log"
    original = main.log

    def log_to_scratch(message: str) -> None:
        try:
            with open(scratch, "a", encoding="utf-8") as handle:
                handle.write(f"{message}\n")
        except Exception:
            pass

    main.log = log_to_scratch
    try:
        yield
    finally:
        main.log = original
