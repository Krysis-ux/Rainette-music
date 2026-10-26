"""Keep the test suite out of the real application's data directory.

Several tests import `main`, whose `log()` writes to
`APP_DATA_DIR / "rainette-music.log"` — the *user's* log. Running the suite
therefore filled a real install's log with hundreds of lines like
"player shape failed: No module named 'winreg'" and "update install failed",
interleaved with genuine entries.

That is not merely untidy. This app's two invariants are "the music plays" and
"the tunnel connects" (see CLAUDE.md), and the log is the only evidence either
leaves behind. Debugging a real tunnel failure meant reading around pages of
test noise to find the one traceback that mattered, which is exactly the cost
this fixture removes.

What is replaced is the *write*, not the path. `main.LOG_PATH` is an invariant
two tests deliberately assert on (the database and log must live in per-user app
data), so redirecting it would break the very guarantee those tests exist to
protect. Swapping `main.log` for one that writes elsewhere stops the side effect
and leaves the property intact.
"""

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
    """Refuse to execute Tailscale from a test.

    On macOS the `tailscale` CLI is the Tailscale *app* binary, so running it
    opens the app. Every suite run used to launch Tailscale on the developer's
    machine: tests that mocked preflight/launch but not `stop` ran
    `tailscale serve --https=443 off` for real, and a login test whose fake
    status read as "daemon asleep" made `_wake_daemon` run
    `open -a Tailscale`. Tests that want a command's answer patch
    `subprocess.run` themselves, which overrides this for that test.
    """
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
