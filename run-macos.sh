#!/usr/bin/env bash
#
# Run Rainette Music from source on macOS.
#
# Creates a virtual environment on first run, installs dependencies into it, and
# starts the app. Safe to re-run: it reuses the environment once it exists.
#
# The Windows README says `pythonw main.py`; there is no pythonw on macOS, and a
# plain `python main.py` is the equivalent -- pywebview's Cocoa backend opens the
# window, and this script keeps the terminal attached so failures are visible
# rather than silent.
#
#   ./run-macos.sh               start the app
#   ./run-macos.sh --doctor      check the setup and report, without starting
#   ./run-macos.sh --test-audio  play a test tone and prove where the sound goes
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$ROOT/.venv-mac"
LOG="$HOME/Library/Application Support/Rainette Music/rainette-music.log"
cd "$ROOT"

DOCTOR=0
TEST_AUDIO=0
[ "${1:-}" = "--doctor" ] && DOCTOR=1
[ "${1:-}" = "--test-audio" ] && TEST_AUDIO=1

say()  { printf '\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
bad()  { printf '  \033[31m✗\033[0m %s\n' "$*"; }
note() { printf '    %s\n' "$*"; }

say "Rainette Music — macOS launcher"

# ── macOS version ───────────────────────────────────────────────────────────
OS_VER="$(sw_vers -productVersion)"
OS_MAJOR="${OS_VER%%.*}"
if [ "$OS_MAJOR" -ge 13 ]; then
    ok "macOS $OS_VER"
else
    bad "macOS $OS_VER — Rainette's stylesheet needs macOS 13 or newer (it uses color-mix)"
fi

# ── Python ──────────────────────────────────────────────────────────────────
PYTHON_BIN="${PYTHON_BIN:-}"
if [ -z "$PYTHON_BIN" ]; then
    for candidate in python3.12 python3.13 python3.11 python3; do
        if command -v "$candidate" >/dev/null 2>&1; then PYTHON_BIN="$(command -v "$candidate")"; break; fi
    done
fi
if [ -z "$PYTHON_BIN" ]; then
    bad "No python3 found"
    note "Install it with:  brew install python@3.12"
    note "(no Homebrew? get it at https://brew.sh)"
    exit 1
fi
ok "Python $("$PYTHON_BIN" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])') at $PYTHON_BIN"

# ── Stale instances ─────────────────────────────────────────────────────────
# A leftover copy keeps the database and the port, so a fresh launch either
# binds a different port or appears to do nothing at all.
STALE="$(pgrep -f "$ROOT/main.py" 2>/dev/null || true)"
if [ -n "$STALE" ]; then
    if [ "$DOCTOR" -eq 1 ]; then
        bad "Rainette is already running (pid $(echo "$STALE" | tr '\n' ' '))"
    else
        say "Closing a Rainette instance that was still running…"
        # shellcheck disable=SC2086
        kill $STALE 2>/dev/null || true
        sleep 2
        ok "closed"
    fi
else
    ok "No stale instance running"
fi

# ── Virtual environment ─────────────────────────────────────────────────────
if [ ! -x "$VENV/bin/python" ]; then
    if [ "$DOCTOR" -eq 1 ]; then
        bad "No virtual environment yet (it is created on first run)"
    else
        say "Creating the virtual environment (first run only)…"
        "$PYTHON_BIN" -m venv "$VENV"
        "$VENV/bin/python" -m pip install --upgrade pip --quiet
        ok "created"
    fi
fi

# pywebview declares the pyobjc frameworks it needs on darwin, so requirements.txt
# alone brings in the whole Cocoa/WebKit stack -- there is no macOS-only file.
DEPS_OK=0
if [ -x "$VENV/bin/python" ] && "$VENV/bin/python" - <<'PY' >/dev/null 2>&1
import webview, aiohttp, yt_dlp, qrcode, cryptography, AppKit, WebKit
PY
then DEPS_OK=1; fi

if [ "$DEPS_OK" -eq 1 ]; then
    ok "Dependencies installed (pywebview, aiohttp, yt-dlp, Cocoa/WebKit)"
elif [ "$DOCTOR" -eq 1 ]; then
    bad "Dependencies missing or incomplete"
    note "They install automatically on the next normal run."
else
    say "Installing dependencies (a few minutes the first time)…"
    "$VENV/bin/python" -m pip install -r requirements.txt
    ok "installed"
fi

if [ "$TEST_AUDIO" -eq 1 ]; then
    echo
    exec "$VENV/bin/python" audio_selftest.py
fi

if [ "$DOCTOR" -eq 1 ]; then
    echo
    say "Log file"
    if [ -f "$LOG" ]; then
        note "$LOG"
        tail -15 "$LOG" | sed 's/^/    /'
    else
        note "none yet — the app writes one on first start"
    fi
    echo
    say "Doctor finished. Start the app with:  ./run-macos.sh"
    exit 0
fi

# ── Launch ──────────────────────────────────────────────────────────────────
echo
say "Starting Rainette Music…"
note "The window opens in a second or two. Keep this terminal open;"
note "closing it stops the app. Errors appear here and in:"
note "$LOG"
echo

set +e
"$VENV/bin/python" main.py "$@"
STATUS=$?
set -e

if [ "$STATUS" -ne 0 ]; then
    echo
    bad "Rainette exited with status $STATUS"
    if [ -f "$LOG" ]; then
        say "Last lines of the log:"
        tail -20 "$LOG" | sed 's/^/    /'
    fi
    note "Run './run-macos.sh --doctor' for a setup check."
    # Keep the Terminal window readable when launched by double-click.
    [ -t 0 ] && { echo; read -r -p "Press Return to close…" _; }
fi
exit "$STATUS"
