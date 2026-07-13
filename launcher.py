"""Thin launcher compiled into ``Rainette Music.exe`` by PyInstaller.

Keeps the actual app as editable .py files using the system Python + installed
deps (so yt-dlp stays current via pip). The exe just locates a windowed Python
interpreter and starts ``main.py`` from its own folder, detached.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from datetime import datetime


def _app_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _find_pythonw() -> list[str] | None:
    # Prefer windowed interpreters (no console flash), then fall back.
    for name in ("pythonw", "pyw", "python"):
        found = shutil.which(name)
        if found:
            return [found]
    # Windows Python launcher, windowed variant.
    pyw = shutil.which("pyw")
    if pyw:
        return [pyw]
    py = shutil.which("py")
    if py:
        return [py, "-w"]
    return None


def main() -> int:
    here = _app_dir()
    _log(f"launcher start frozen={getattr(sys, 'frozen', False)} executable={sys.executable}")
    main_py = os.path.join(here, "main.py")
    if not os.path.isfile(main_py):
        _fail(f"main.py not found next to the launcher:\n{main_py}")
        return 1

    runner = _find_pythonw()
    _log(f"runner={runner!r} main={main_py}")
    if runner is None:
        _fail("Python was not found on PATH.\n\nInstall Python 3.10+ and run:\n  pip install -r requirements.txt")
        return 1

    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        child = subprocess.Popen(runner + [main_py], cwd=here, creationflags=creationflags)
        _log(f"spawned pid={child.pid}")
    except Exception as exc:
        _log(f"spawn failed: {exc!r}")
        _fail(f"Could not start Rainette Music:\n{exc}")
        return 1
    return 0


def _log(message: str) -> None:
    try:
        app_data = os.environ.get("LOCALAPPDATA") or _app_dir()
        log_dir = os.path.join(app_data, "Rainette Music")
        os.makedirs(log_dir, exist_ok=True)
        path = os.path.join(log_dir, "rainette-launcher.log")
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}\n")
    except Exception:
        pass


def _fail(message: str) -> None:
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, message, "Rainette Music", 0x10)
    except Exception:
        print(message, file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
