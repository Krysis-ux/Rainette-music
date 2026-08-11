"""Audio self-test: is Rainette silent, or is the Mac silent?

"I press play and hear nothing" has two very different causes, and they need
different fixes:

  * the app never produces audio, or
  * the app produces audio that never reaches your speakers.

Telling them apart by ear is impossible, so this plays a tone through the exact
same path Rainette uses -- a pywebview/WKWebView window driving an <audio>
element with no user gesture -- while asking CoreAudio whether the output device
is genuinely being driven. It reports both what the browser engine thinks and
what the hardware is doing.

    python audio_selftest.py

Exit status is 0 when audio reached the device, 1 otherwise.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import functools
import http.server
import math
import os
import socketserver
import struct
import sys
import tempfile
import threading
import time
import wave

TONE_SECONDS = 6
TONE_HZ = 440
TONE_VOLUME = 0.25          # audible, but not startling
SAMPLE_INTERVAL_S = 0.4


# ── CoreAudio: is the output device actually running? ───────────────────────

def _fourcc(code: str) -> int:
    return struct.unpack(">I", code.encode("ascii"))[0]


class _PropertyAddress(ctypes.Structure):
    _fields_ = [("mSelector", ctypes.c_uint32),
                ("mScope", ctypes.c_uint32),
                ("mElement", ctypes.c_uint32)]


class OutputDevice:
    """The default output device, and whether anything is driving it."""

    _SYSTEM_OBJECT = 1

    def __init__(self) -> None:
        self._core_audio = ctypes.CDLL(ctypes.util.find_library("CoreAudio"))

    def _u32(self, obj_id: int, selector: int) -> int:
        address = _PropertyAddress(selector, _fourcc("glob"), 0)
        value = ctypes.c_uint32(0)
        size = ctypes.c_uint32(ctypes.sizeof(value))
        status = self._core_audio.AudioObjectGetPropertyData(
            ctypes.c_uint32(obj_id), ctypes.byref(address),
            ctypes.c_uint32(0), None, ctypes.byref(size), ctypes.byref(value))
        if status != 0:
            raise OSError(f"CoreAudio query failed ({status})")
        return value.value

    @property
    def device_id(self) -> int:
        return self._u32(self._SYSTEM_OBJECT, _fourcc("dOut"))

    def name(self) -> str:
        address = _PropertyAddress(_fourcc("lnam"), _fourcc("glob"), 0)
        ref = ctypes.c_void_p()
        size = ctypes.c_uint32(ctypes.sizeof(ref))
        status = self._core_audio.AudioObjectGetPropertyData(
            ctypes.c_uint32(self.device_id), ctypes.byref(address),
            ctypes.c_uint32(0), None, ctypes.byref(size), ctypes.byref(ref))
        if status != 0:
            return "(unknown)"
        core_foundation = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))
        core_foundation.CFStringGetCStringPtr.restype = ctypes.c_char_p
        pointer = core_foundation.CFStringGetCStringPtr(ref, 0x08000100)  # UTF-8
        return pointer.decode() if pointer else "(unreadable)"

    def is_running(self) -> bool:
        """True when some process is actively driving this device."""
        return bool(self._u32(self.device_id, _fourcc("gone")))


def _wait_until_quiet(device: OutputDevice, timeout_s: float = 20.0) -> bool:
    """Wait for a genuine silence baseline, so the measurement means something."""
    deadline, streak = time.time() + timeout_s, 0
    while time.time() < deadline:
        streak = streak + 1 if not device.is_running() else 0
        if streak >= 5:
            return True
        time.sleep(SAMPLE_INTERVAL_S)
    return False


def _sample(device: OutputDevice, seconds: float) -> tuple[int, int]:
    hits = total = 0
    end = time.time() + seconds
    while time.time() < end:
        total += 1
        hits += 1 if device.is_running() else 0
        time.sleep(SAMPLE_INTERVAL_S)
    return hits, total


# ── The tone, served the way Rainette serves its own pages ──────────────────

def _write_tone(path: str) -> None:
    frames = b"".join(
        struct.pack("<h", int(32767 * TONE_VOLUME * math.sin(2 * math.pi * TONE_HZ * t / 44100)))
        for t in range(44100 * TONE_SECONDS)
    )
    with wave.open(path, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(44100)
        handle.writeframes(frames)


_PAGE = """<!doctype html><meta charset="utf-8">
<title>Rainette audio test</title>
<body style="font:14px -apple-system;padding:20px;text-align:center">
<div style="font-size:34px">🔊</div>
<div id="out">getting ready…</div>
<script>
// Deliberately does NOT autoplay. The harness needs a genuine silence baseline
// from the audio device first, and a tone that starts on load would simply have
// finished before the measurement began -- which once made this very test
// report an app bug that did not exist.
const a = new Audio('tone.wav');
a.loop = true;               // outlast the measurement window
a.volume = 1.0;
const out = document.getElementById('out');
// No click anywhere in this path -- exactly like Rainette's socket-driven player.
window.__start = () => a.play().then(() => {
  out.textContent = 'playing — can you hear a tone?';
  window.__r = {ok: true};
  return 'ok';
}).catch(e => {
  out.textContent = 'blocked: ' + e.name;
  window.__r = {ok: false, error: e.name};
  return 'ERR:' + e.name;
});
window.__stop = () => { a.pause(); out.textContent = 'done'; };
window.__state = () => ({
  ok: (window.__r||{}).ok, error: (window.__r||{}).error || null,
  currentTime: +a.currentTime.toFixed(2), paused: a.paused,
  volume: a.volume, muted: a.muted, readyState: a.readyState,
  mediaError: a.error ? a.error.code : null,
});
</script></body>
"""


def main() -> int:
    if sys.platform != "darwin":
        print("This self-test is macOS-only.")
        return 2

    print("Rainette audio self-test")
    print("=" * 46)

    device = OutputDevice()
    print(f"  Output device : {device.name()}")

    workdir = tempfile.mkdtemp(prefix="rainette-audiotest-")
    _write_tone(os.path.join(workdir, "tone.wav"))
    with open(os.path.join(workdir, "index.html"), "w") as handle:
        handle.write(_PAGE)

    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=workdir)
    handler.log_message = lambda *a, **k: None
    httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]

    try:
        import webview
    except Exception as exc:
        print(f"  pywebview is unavailable: {exc}")
        return 1

    results: dict = {}
    window = webview.create_window(
        "Rainette audio test", f"http://127.0.0.1:{port}/index.html",
        width=340, height=150,
    )

    def work():
        time.sleep(2.5)
        print("\n  Listening to the hardware…")
        if not _wait_until_quiet(device):
            print("  ! Another app is already using the speakers; result may be unclear.")
        results["baseline"] = _sample(device, 2.0)
        print(f"  Baseline (silence)   : device busy {results['baseline'][0]}/{results['baseline'][1]} samples")

        # Only now start the tone, so 'during' really is during.
        print("\n  ♪ Playing a 440 Hz tone for a few seconds — listen now.")
        try:
            window.evaluate_js("window.__start()")
        except Exception as exc:
            print(f"  could not start the tone: {exc}")
        time.sleep(1.0)
        results["during"] = _sample(device, 5.0)
        print(f"  During the tone      : device busy {results['during'][0]}/{results['during'][1]} samples")

        try:
            results["state"] = window.evaluate_js("window.__state()")
            window.evaluate_js("window.__stop()")
        except Exception as exc:
            results["state"] = {"failed": str(exc)}
        time.sleep(0.3)
        try:
            window.destroy()
        except Exception:
            pass

    threading.Thread(target=work, daemon=True).start()
    webview.start()

    state = results.get("state") or {}
    baseline = results.get("baseline", (0, 1))[0]
    during = results.get("during", (0, 1))[0]

    print("\n  Browser engine reported:")
    print(f"    play() accepted : {state.get('ok')}"
          + (f"   (error: {state.get('error')})" if state.get("error") else ""))
    print(f"    position moved  : {state.get('currentTime')}s, paused={state.get('paused')}")
    print(f"    volume/muted    : {state.get('volume')} / {state.get('muted')}")

    print("\n" + "=" * 46)
    if state.get("ok") is False:
        print("  RESULT: WebKit refused to start playback.")
        print("  This is an app-side problem — please report the error above.")
        return 1
    if baseline > 0:
        print("  RESULT: inconclusive — the speakers were already busy.")
        print("  Quit other audio apps and run this again.")
        return 1
    if during > 0:
        print("  RESULT: audio IS reaching your output device.")
        print(f"  The tone was sent to: {device.name()}")
        print("\n  If you did not hear it, the audio is going somewhere you")
        print("  are not listening. Check, in order:")
        print("    1. System Settings ▸ Sound ▸ Output — is the right device selected?")
        print("    2. The volume slider there (and any Bluetooth headphones still paired)")
        print("    3. System Settings ▸ Sound ▸ 'Rainette Music' / 'Python' app volume")
        return 0
    print("  RESULT: the app decoded audio but never drove the output device.")
    print("  That is an app-side bug — please report this output.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
