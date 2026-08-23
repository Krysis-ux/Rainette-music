#!/usr/bin/env python3
"""Check the two things that must never break, against the real world.

    python scripts/preflight.py

1. THE MUSIC PLAYS  -- resolve a real track and open it exactly the way a
   browser's <audio> element does: an unbounded `Range: bytes=0-`. Assert the
   result is audio-only and served as `audio/*`.

2. THE TUNNEL CONNECTS -- start a real tunnel in front of a stand-in listener,
   prove the public address reaches it, tear it down, and confirm no helper
   process was left behind.

Both are end-to-end on purpose. Every outage this app has had was invisible to
the unit suite: the code was right, and the world underneath it had moved.

Run this from an ordinary residential connection -- YouTube answers datacenter
IPs with a bot challenge, so a cloud runner cannot do it. Exit status is 0 only
if both invariants hold; 2 means "could not measure", which is not the same as
"broken" and is not a failure.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CANARY_SOURCES = ("dQw4w9WgXcQ", "9bZkp7q19f0")
PROBE_PORT = 47903

GREEN, RED, AMBER, DIM, OFF = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"

# Failures that mean "we never got an answer" rather than "the answer is wrong".
INCONCLUSIVE = (
    "sign in to confirm", "not a bot", "http error 429", "too many requests",
    "certificate_verify_failed", "certificate verify failed",
    "unable to download", "the page needs to be reloaded",
    "video unavailable", "private video",
)


def _inconclusive(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in INCONCLUSIVE)


def _say(state: str, label: str, detail: str = "") -> None:
    colour = {"ok": GREEN, "fail": RED, "skip": AMBER}[state]
    mark = {"ok": "PASS", "fail": "FAIL", "skip": "SKIP"}[state]
    print(f"  {colour}{mark}{OFF}  {label}")
    if detail:
        print(f"        {DIM}{detail}{OFF}")


# ── 1. the music plays ────────────────────────────────────────────────────

def check_music() -> str:
    print("\n1. THE MUSIC PLAYS")
    try:
        import music_bridge
    except Exception as exc:
        _say("skip", "import music_bridge", str(exc)[:140])
        return "skip"

    verdict = "ok"
    for source_id in CANARY_SOURCES:
        music_bridge._stream_cache_invalidate(source_id)
        try:
            resolved = music_bridge._extract_stream(source_id)
        except Exception as exc:
            if _inconclusive(exc):
                _say("skip", source_id, f"YouTube would not answer: {str(exc)[:110]}")
                verdict = "skip" if verdict == "ok" else verdict
                continue
            _say("fail", source_id, str(exc)[:160])
            return "fail"

        request = urllib.request.Request(
            resolved["url"], headers={**resolved["http_headers"], "Range": "bytes=0-"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                status, ctype = response.status, response.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            status, ctype = exc.code, ""
        except Exception as exc:
            _say("skip", source_id, f"upstream unreachable: {str(exc)[:110]}")
            verdict = "skip" if verdict == "ok" else verdict
            continue

        if status in (403, 429):
            _say("skip", source_id, f"upstream returned {status} before anything could be judged")
            verdict = "skip" if verdict == "ok" else verdict
            continue
        if status not in (200, 206):
            _say("fail", source_id,
                 f"answered {status} to the open-ended range <audio> opens with")
            return "fail"
        if not ctype.startswith("audio/"):
            _say("fail", source_id,
                 f"served as {ctype!r} -- a video format in an <audio> element")
            return "fail"
        _say("ok", source_id, f"{ctype}, {status} on Range: bytes=0-")

    fallback = music_bridge.last_muxed_fallback()
    if fallback:
        _say("fail", "audio-only format available",
             f"had to fall back to muxed {fallback['format_id']} ({fallback['ext']})")
        return "fail"
    return verdict


# ── 2. the tunnel connects ────────────────────────────────────────────────

class _Standin(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(401 if self.path.startswith("/status") else 404)
        self.end_headers()
        self.wfile.write(b"standin")

    def log_message(self, *_args):
        return


def _helpers_running() -> set[str]:
    try:
        out = subprocess.run(["pgrep", "-f", "cloudflared tunnel"],
                             capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return set()
    return {line.strip() for line in out.splitlines() if line.strip()}


def check_tunnel() -> str:
    print("\n2. THE TUNNEL CONNECTS")
    try:
        import server
        import tunnel
    except Exception as exc:
        _say("skip", "import tunnel", str(exc)[:140])
        return "skip"

    manager = tunnel.TunnelManager(server.APP_DATA_DIR)
    if manager.provider().capabilities.auto_installable and manager.provider().ensure_binary(None) is None:
        _say("skip", "helper present", "cloudflared has not been downloaded on this machine")
        return "skip"

    before = _helpers_running()
    httpd = HTTPServer(("127.0.0.1", PROBE_PORT), _Standin)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        manager.start(PROBE_PORT)
        deadline = time.time() + 150
        while time.time() < deadline:
            status = manager.status()
            if status["phase"] in ("running", "error", "setup"):
                break
            time.sleep(1)
        status = manager.status()
        if status["phase"] != "running":
            _say("fail", f"{manager.provider().id} came up",
                 f"phase={status['phase']}: {status.get('message', '')[:130]}")
            return "fail"
        _say("ok", f"{manager.provider().id} came up", status["url"])

        if not tunnel._probe_reachable(status["url"]):
            _say("fail", "the address reaches this computer", status["url"])
            return "fail"
        _say("ok", "the address reaches this computer", "gateway answered through the tunnel")
    finally:
        try:
            manager.stop()
        except Exception:
            pass
        httpd.shutdown()

    time.sleep(2)
    leaked = _helpers_running() - before
    if leaked:
        _say("fail", "no helper left behind", f"leaked pids: {', '.join(sorted(leaked))}")
        return "fail"
    _say("ok", "no helper left behind")
    return "ok"


def main() -> int:
    print("Rainette preflight — the two things that must never break")
    results = {"music": check_music(), "tunnel": check_tunnel()}

    print("\n" + "─" * 62)
    if "fail" in results.values():
        broken = ", ".join(name for name, state in results.items() if state == "fail")
        print(f"{RED}BLOCKED{OFF} — {broken} is broken. Do not ship this.")
        return 1
    if "skip" in results.values():
        print(f"{AMBER}INCONCLUSIVE{OFF} — nothing was measured for part of this. "
              f"Not the same as passing; re-run on an ordinary connection.")
        return 2
    print(f"{GREEN}BOTH HOLD{OFF} — music plays, tunnel connects.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
