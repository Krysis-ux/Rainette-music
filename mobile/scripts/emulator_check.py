"""Drive the debug app on an emulator or device and record what really happened.

Usage: python emulator_check.py <out-dir>
Needs: adb on PATH, one device attached with the debug build installed, and
aiohttp. The WebView is driven over Chrome's DevTools protocol (debug builds
only; Runtime.evaluate on the page target). The media session's own report
from `dumpsys media_session` is the ground truth for playback, not anything
the page claims.

Prints PASS/FAIL per check and exits with the number of failures. A result on
an emulator is an emulator result: it says nothing about Bluetooth, real audio
hardware, OEM battery managers, or a real network.
"""
import asyncio
import json
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import aiohttp

PACKAGE = "com.krysisux.rainette.dev"
ACTIVITY = f"{PACKAGE}/com.krysisux.rainettemusic.MainActivity"
PORT = 9333
OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "emulator-out")
failures = 0


def adb(*args, check=True) -> str:
    return subprocess.run(["adb", *args], capture_output=True, text=True, check=check).stdout


def check(ok: bool, what: str, detail: str = "") -> None:
    global failures
    print(("PASS " if ok else "FAIL ") + what + (f"  [{detail}]" if detail else ""), flush=True)
    failures += 0 if ok else 1


def wait_for(predicate, timeout_s: float, step: float = 0.5):
    deadline = time.time() + timeout_s
    last = None
    while time.time() < deadline:
        try:
            last = predicate()
        except Exception:  # the page may be mid-reload
            last = None
        if last:
            return last
        time.sleep(step)
    return last


def session() -> dict:
    """The app's media session as Android reports it. Android stamps the position
    only on state changes, so it is carried forward with the reported speed."""
    dump = adb("shell", "dumpsys", "media_session")
    # Exact package: com.krysisux.rainette is a prefix of the old dev app's id.
    found = re.search(rf"package={re.escape(PACKAGE)}\s", dump)
    start = found.start() if found else -1
    block = dump[start:start + 4000] if start >= 0 else ""
    state = re.search(r"state=PlaybackState \{state=(?:\w+\()?(\d+)\)?, position=(\d+), buffered position=\d+, "
                      r"speed=([\d.]+), updated=(\d+)", block)
    title = re.search(r"description=([^,\n]+)", block)
    item = re.search(r"active item id=(-?\d+)", block)
    if not state:
        return {"state": "NONE", "position_ms": -1, "title": "", "item": -1}
    now_ms = int(float(adb("shell", "cat", "/proc/uptime").split()[0]) * 1000)
    position = int(state.group(2)) + int(max(0, now_ms - int(state.group(4))) * float(state.group(3)))
    # Titles repeat across recordings ("One More Time" x4), so queue position is
    # what proves a track changed.
    return {"state": STATES.get(int(state.group(1)), state.group(1)), "position_ms": position,
            "title": title.group(1).strip() if title else "", "item": int(item.group(1)) if item else -1}


STATES = {0: "NONE", 1: "STOPPED", 2: "PAUSED", 3: "PLAYING", 6: "BUFFERING", 7: "ERROR", 8: "CONNECTING"}


def keyguard_up() -> bool:
    """The lock screen is up. Android 9 reports it as `showing=true` inside the
    KeyguardServiceDelegate block; newer versions use the named flags."""
    policy = adb("shell", "dumpsys", "window", "policy", check=False)
    delegate = policy[policy.find("KeyguardServiceDelegate"):][:400]
    return bool(re.search(r"(mShowingLockscreen|isKeyguardShowing|mKeyguardShowing)=true", policy)
                or re.search(r"\bshowing=true", delegate))


def wait_for_unlock() -> None:
    if keyguard_up():
        print("  >>> unlock the phone to continue (waiting up to 2 minutes)", flush=True)
        wait_for(lambda: not keyguard_up(), 120, step=1)


def audio_started() -> bool:
    """Is an AudioTrack owned by this app actually started? Stronger than any UI state."""
    listed = adb("shell", "pm", "list", "packages", "-U", PACKAGE)
    uid = re.search(rf"^package:{re.escape(PACKAGE)} uid:(\d+)", listed, re.MULTILINE)
    if not uid:
        return False
    players = adb("shell", "dumpsys", "audio")
    return any(f"u/pid:{uid.group(1)}/" in line and "state:started" in line for line in players.splitlines())


def forward() -> None:
    pid = wait_for(lambda: adb("shell", "pidof", PACKAGE, check=False).strip(), 20)
    socket = f"webview_devtools_remote_{pid}"
    wait_for(lambda: socket in adb("shell", "cat", "/proc/net/unix"), 20)
    adb("forward", f"tcp:{PORT}", f"localabstract:{socket}")


def target_ws() -> str:
    targets = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json", timeout=5))
    return next(t["webSocketDebuggerUrl"] for t in targets if "rainette.localhost" in t.get("url", ""))


def js(expression: str):
    """Evaluate in the app's page and return the (JSON) value; promises are awaited."""
    async def run():
        async with aiohttp.ClientSession() as http:
            async with http.ws_connect(target_ws(), max_msg_size=0) as ws:
                await ws.send_json({"id": 1, "method": "Runtime.evaluate", "params": {
                    "expression": expression, "awaitPromise": True, "returnByValue": True}})
                async for message in ws:
                    data = message.json()
                    if data.get("id") == 1:
                        return data
    data = asyncio.run(run())
    result = data["result"]
    if "exceptionDetails" in result:
        raise RuntimeError(result["exceptionDetails"].get("exception", {}).get("description", "page error"))
    return result["result"].get("value")


def screenshot(name: str) -> Path:
    path = OUT / f"{name}.png"
    path.write_bytes(subprocess.run(["adb", "exec-out", "screencap", "-p"], capture_output=True).stdout)
    return path


def status_bar_contrast(path: Path) -> int:
    """Brightest minus darkest pixel in the status bar strip: near 0 means the
    clock and icons are drawn in the background's own colour, i.e. invisible."""
    from PIL import Image
    image = Image.open(path).convert("L")
    width, height = image.size
    strip = image.crop((0, int(height * 0.008), width, int(height * 0.03)))
    low, high = strip.getextrema()
    return high - low


PLAYER = "(await import('./src/player.js'))"


def main() -> int:
    wifi = adb("shell", "settings", "get", "global", "wifi_on").strip()
    data = adb("shell", "settings", "get", "global", "mobile_data").strip()
    try:
        return run()
    finally:
        # Whatever happened, leave the phone's network exactly as it was found.
        adb("shell", "svc", "wifi", "enable" if wifi == "1" else "disable", check=False)
        adb("shell", "svc", "data", "enable" if data == "1" else "disable", check=False)


def run() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    adb("shell", "am", "force-stop", PACKAGE)
    adb("shell", "am", "start", "-W", "-n", ACTIVITY)
    forward()
    booted = wait_for(lambda: js("!document.querySelector('#appView').hidden"), 30)
    check(bool(booted), "app boots in the WebView")
    time.sleep(1.5)
    contrast = status_bar_contrast(screenshot("01-first-launch"))
    check(contrast > 120, "status bar icons are visible on the dark theme", f"contrast {contrast}")
    js("(async () => (await import('./src/prefs.js')).applyTheme('light'))()")
    time.sleep(1.5)
    contrast = status_bar_contrast(screenshot("01b-light-theme"))
    check(contrast > 120, "status bar icons are visible on the light theme", f"contrast {contrast}")
    js("(async () => (await import('./src/prefs.js')).applyTheme('midnight'))()")
    time.sleep(0.5)
    check(js("location.origin") == "https://rainette.localhost", "WebView origin is fixed")
    check(js("document.querySelector('#setupView').hidden && !document.querySelector('#tabBar').hidden"),
          "first launch opens into music, not pairing")
    check(js("document.querySelector('#statusText').textContent") == "On this phone", "status reads 'On this phone'")

    # Search on the phone.
    js("document.querySelector('#tabBar [data-tab=\"search\"]').click()")
    js("(() => { const i = document.querySelector('#searchInput'); i.value = 'daft punk one more time';"
       " document.querySelector('#searchForm').requestSubmit(); })()")
    started = time.time()
    rows = wait_for(lambda: js("document.querySelectorAll('#searchResults .track').length"), 40)
    check(bool(rows), "on-device search returns songs", f"{rows} rows in {time.time() - started:.1f}s")
    print("  results:", json.dumps(js("[...document.querySelectorAll('#searchResults .track')].slice(0, 5)"
                                      ".map(r => r.innerText.split('\\n').slice(0, 2).join(' — '))")), flush=True)
    screenshot("02-search-results")

    # Play: the service, not the element, must own it.
    started = time.time()
    # The row's stretched play button is what a finger lands on.
    js("document.querySelector('#searchResults .track .track-play').click()")
    playing = wait_for(lambda: session()["state"] == "PLAYING", 45)
    check(bool(playing), "media session reaches PLAYING", f"{time.time() - started:.1f}s to start")
    first = session()
    print("  session:", first, flush=True)
    time.sleep(4)
    later = session()
    check(later["position_ms"] > first["position_ms"], "position advances while playing",
          f"{first['position_ms']} -> {later['position_ms']} ms")
    check(audio_started(), "Android reports this app's audio track as started")
    check(js("[...document.querySelectorAll('audio')].every(a => a.paused)"),
          "the <audio> element is silent while the service plays")
    check(js(f"(async () => {PLAYER}.nativeOwns())()"), "the page agrees the service owns the audio")

    # A play request answers only once its track has loaded: what a desktop
    # handoff waits on before it pauses the computer.
    ack = js(f"""(async () => {{
        const p = {PLAYER}; const {{ state }} = await import('./src/state.js');
        const list = state.searchResults; const t0 = Date.now();
        await p.playTrack(list[4], list, 4);
        const s = await Capacitor.nativePromise('RainettePlayer', 'getState');
        return {{ ms: Date.now() - t0, loading: s.loading, id: s.source_id, want: list[4].source_id }};
    }})()""")
    check(ack["id"] == ack["want"] and not ack["loading"], "a play request resolves only once its track has loaded", str(ack))
    time.sleep(1)
    screenshot("03-playing")

    # Absolute media keys: PLAY while playing must not pause, and vice versa.
    adb("shell", "input", "keyevent", "KEYCODE_MEDIA_PLAY")
    adb("shell", "input", "keyevent", "KEYCODE_MEDIA_PLAY")
    time.sleep(1.5)
    check(session()["state"] == "PLAYING", "repeated PLAY key never pauses")
    adb("shell", "input", "keyevent", "KEYCODE_MEDIA_PAUSE")
    adb("shell", "input", "keyevent", "KEYCODE_MEDIA_PAUSE")
    time.sleep(1.5)
    check(session()["state"] == "PAUSED", "repeated PAUSE key never resumes")
    adb("shell", "input", "keyevent", "KEYCODE_MEDIA_PLAY")
    wait_for(lambda: session()["state"] == "PLAYING", 10)

    # Near the end, then background the app and turn the screen off: the next
    # track must start with no page in the loop.
    first_item = session()["item"]
    js(f"(async () => {{ const p = {PLAYER}; p.seekTo(p.duration() - 6); }})()")
    time.sleep(1)
    adb("shell", "input", "keyevent", "KEYCODE_HOME")
    adb("shell", "input", "keyevent", "KEYCODE_SLEEP")
    advanced = wait_for(lambda: (s := session())["item"] != first_item and s["state"] == "PLAYING", 45)
    check(bool(advanced), "next track starts with the app backgrounded and the screen off",
          f"item {first_item} -> {session()['item']} ({session()['title']!r})")
    s1 = session()
    time.sleep(6)
    s2 = session()
    check(s2["position_ms"] > s1["position_ms"] and audio_started(), "still playing with the screen off",
          f"{s1['position_ms']} -> {s2['position_ms']} ms")
    check("rainettemusic" in adb("shell", "dumpsys", "notification", "--noredact"), "a media notification is posted")
    adb("shell", "input", "keyevent", "KEYCODE_WAKEUP")
    adb("shell", "wm", "dismiss-keyguard", check=False)
    time.sleep(1)
    wait_for_unlock()

    # Lose the network, skip into a track that has to be resolved, then get the
    # network back: playback must recover by itself, within the bounded backoff.
    adb("shell", "svc", "wifi", "disable", check=False)
    adb("shell", "svc", "data", "disable", check=False)
    before = session()["item"]
    adb("shell", "input", "keyevent", "KEYCODE_MEDIA_NEXT")
    time.sleep(8)
    print("  while offline:", session(), flush=True)
    adb("shell", "svc", "wifi", "enable", check=False)
    adb("shell", "svc", "data", "enable", check=False)
    started_offline = time.time()
    recovered = wait_for(lambda: (s := session())["state"] == "PLAYING" and s["item"] != before, 60)
    print(f"  recovered {time.time() - started_offline:.1f}s after the network returned", flush=True)
    check(bool(recovered), "playback recovers once the network returns", str(session()))

    # Back to the app: it must show what the service is playing. It has to be on
    # screen for that -- a hidden page paints nothing -- so the phone must be unlocked.
    wait_for_unlock()
    adb("shell", "am", "start", "-n", ACTIVITY)
    forward()
    wait_for(lambda: js("!document.hidden"), 15)
    time.sleep(2)
    # By queue position, not title: four recordings here are all "One More Time".
    playing = session()
    followed = wait_for(lambda: js("(async () => (await import('./src/state.js')).state.queueIndex)()") == playing["item"], 10)
    shown = js("document.querySelector('#playerTitle').textContent")
    check(bool(followed) and shown == playing["title"],
          "returning to the app shows what the service is playing",
          f"page {shown!r} vs service item {playing['item']} {playing['title']!r}")
    screenshot("04-returned")

    # Swiped away and reopened while music plays: the activity and page are new,
    # the media service is not. The new page must show what is playing, not
    # "Nothing playing", and must not restart it.
    # `am start -W` blocks for good behind a secure lock screen, so unlock first.
    wait_for_unlock()
    before = session()
    adb("shell", "am", "start", "-W", "--activity-clear-task", "-n", ACTIVITY)
    forward()
    wait_for(lambda: js("!document.querySelector('#appView').hidden && !document.hidden"), 20)
    shown = wait_for(lambda: (t := js("document.querySelector('#playerTitle').textContent")) != "Nothing playing" and t, 10) or ""
    after = session()
    check(shown == after["title"] and after["item"] == before["item"] and after["state"] == "PLAYING",
          "a reopened app shows the music that kept playing, without restarting it",
          f"page {shown!r}, service {before['item']}->{after['item']} {after['state']}")
    screenshot("05-reopened")

    print(f"{failures} failed check(s)", flush=True)
    return failures


if __name__ == "__main__":
    sys.exit(main())
