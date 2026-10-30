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


def find(pattern: str, attribute: str = "text") -> dict | None:
    regex = re.compile(pattern)
    return next((n for n in nodes() if regex.search(n.get(attribute, ""))), None)


def tap(node: dict) -> None:
    x1, y1, x2, y2 = map(int, re.findall(r"\d+", node["bounds"]))
    device.adb("shell", "input", "tap", str((x1 + x2) // 2), str((y1 + y2) // 2))


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
    play_button = device.wait_for(lambda: find(r"^Play .+", "content-desc"), 40)
    device.check(bool(play_button), "on-device search returns songs", f"{time.time() - started:.1f}s")
    device.screenshot("01-search")
    if not play_button:
        return device.failures

    tap(play_button)
    started = time.time()
    playing = device.wait_for(lambda: device.session()["state"] == "PLAYING", 45)
    device.check(bool(playing) and device.audio_started(), "a song plays on the native player",
                 f"{time.time() - started:.1f}s, {device.session()}")
    device.screenshot("02-playing")

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
