"""Smoke-test a release APK on a device, through its real UI.

Usage: python release_smoke.py <apk> <out-dir>

Release builds are not debuggable, so there is no DevTools: the UI is driven
through Android's accessibility tree (uiautomator), the way a finger would,
and playback is read from `dumpsys media_session` and `dumpsys audio`. Run it
on the exact APK CI built before publishing that APK.
"""
import re
import sys
import time
import xml.etree.ElementTree as ElementTree
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import emulator_check as device  # noqa: E402

PACKAGE = "com.krysisux.rainette"
ACTIVITY = f"{PACKAGE}/com.krysisux.rainettemusic.MainActivity"
device.PACKAGE = PACKAGE  # session() and audio_started() read this


def nodes() -> list[dict]:
    device.adb("shell", "uiautomator", "dump", "/sdcard/rainette-ui.xml", check=False)
    xml = device.adb("exec-out", "cat", "/sdcard/rainette-ui.xml", check=False)
    try:
        root = ElementTree.fromstring(xml[xml.find("<?xml"):] if "<?xml" in xml else xml)
    except ElementTree.ParseError:
        return []
    return [node.attrib for node in root.iter("node")]


def find(pattern: str, attribute: str | None = None) -> dict | None:
    """A node whose label matches. WebViews put an element's accessible name in
    `text` on some versions and `content-desc` on others, so both are checked."""
    regex = re.compile(pattern)
    attributes = (attribute,) if attribute else ("text", "content-desc")
    return next((n for n in nodes() if any(regex.search(n.get(a, "")) for a in attributes)), None)


def bounds(node: dict) -> tuple[int, int, int, int]:
    x1, y1, x2, y2 = map(int, re.findall(r"\d+", node["bounds"]))
    return x1, y1, x2, y2


def tap(node: dict, at_left: bool = False) -> None:
    """Tap a node's centre, or near its left edge: on a track row that is the
    artwork, which nothing else (the artist link) overlaps."""
    x1, y1, x2, y2 = bounds(node)
    x = x1 + min(60, (x2 - x1) // 4) if at_left else (x1 + x2) // 2
    device.adb("shell", "input", "tap", str(x), str((y1 + y2) // 2))


def visible_play_button() -> dict | None:
    """The first song's play button that is fully on screen: above the tab bar,
    not scrolled away (zero-size) or under the keyboard."""
    all_nodes = nodes()
    tab_bar = next((n for n in all_nodes if (n.get("text") or n.get("content-desc")) == "Home"), None)
    limit = bounds(tab_bar)[1] if tab_bar else 10_000
    for node in all_nodes:
        label = node.get("text", "") or node.get("content-desc", "")
        if re.match(r"^Play .+", label):
            _, y1, _, y2 = bounds(node)
            if 0 < y1 and y2 <= limit:
                return node
    return None


def main() -> int:
    apk, out = sys.argv[1], Path(sys.argv[2] if len(sys.argv) > 2 else "release-smoke")
    out.mkdir(parents=True, exist_ok=True)
    device.OUT = out
    print(device.adb("install", "-r", apk).strip().splitlines()[-1], flush=True)
    device.wait_for_unlock()
    device.adb("shell", "am", "force-stop", PACKAGE)
    device.adb("shell", "am", "start", "-W", "-n", ACTIVITY)

    search_tab = device.wait_for(lambda: find(r"^Search$"), 30)
    device.check(bool(search_tab), "the release app opens into music (Search tab present)")
    if not search_tab:
        return device.failures
    tap(search_tab)
    field = device.wait_for(lambda: find(r"EditText", "class"), 10)
    tap(field)
    device.adb("shell", "input", "text", "daft%spunk%sone%smore%stime")
    device.adb("shell", "input", "keyevent", "KEYCODE_ENTER")
    started = time.time()
    found = device.wait_for(lambda: find(r"^Play .+"), 40)
    # Enter puts the keyboard away now; Back with nothing to close would leave
    # the app. Older builds kept it up over the results, so Back only if shown.
    if "mInputShown=true" in device.adb("shell", "dumpsys", "input_method", check=False):
        device.adb("shell", "input", "keyevent", "KEYCODE_BACK")
    time.sleep(1)
    # Songs come after the artists; bring a row up above the tab bar.
    play_button = None
    for _ in range(4):
        play_button = visible_play_button() if found else None
        if play_button:
            break
        # A slow drag, so the list does not coast: a tap on a coasting list only
        # stops it, and never reaches the song.
        device.adb("shell", "input", "swipe", "540", "1500", "540", "900", "1500")
        time.sleep(2)
    device.check(bool(play_button), "on-device search returns songs", f"{time.time() - started:.1f}s")
    device.screenshot("01-search")
    if not play_button:
        return device.failures

    time.sleep(1)
    tap(play_button, at_left=True)
    started = time.time()
    playing = device.wait_for(lambda: device.session()["state"] == "PLAYING", 45)
    device.check(bool(playing) and device.audio_started(), "a song plays on the native player",
                 f"{time.time() - started:.1f}s, {device.session()}")
    device.screenshot("02-playing")
    if not playing:
        return device.failures  # leave the screen on, and the phone unlocked, to look

    first = device.session()
    device.adb("shell", "input", "keyevent", "KEYCODE_SLEEP")
    time.sleep(12)
    later = device.session()
    device.check(later["state"] == "PLAYING" and later["position_ms"] > first["position_ms"] and device.audio_started(),
                 "keeps playing with the screen off", f"{first['position_ms']} -> {later['position_ms']} ms")
    device.adb("shell", "input", "keyevent", "KEYCODE_MEDIA_PAUSE")
    print(f"{device.failures} failed check(s)", flush=True)
    return device.failures


if __name__ == "__main__":
    sys.exit(main())
