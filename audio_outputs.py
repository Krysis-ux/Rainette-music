"""Names of the audio outputs this computer can actually play through.

Rainette's "Play on" picker used to offer exactly two entries — this desktop and
one paired phone — which is wrong the moment somebody connects headphones: the
audio *is* going somewhere else, and the picker still says "This desktop".  This
module supplies the missing half, so a Bluetooth speaker appears under the name
its owner gave it rather than as an anonymous default.

Enumeration only.  Nothing here changes the system output device:

* macOS exposes no supported way to set the default output without shipping a
  helper binary, and silently re-routing every other app's audio is not a
  reasonable thing for a music player to do behind the user's back.
* The web layer can route Rainette's own audio with ``HTMLMediaElement.setSinkId``
  where the engine supports it (Chromium/WebView2 does, WKWebView does not), and
  it matches its own device list against these names.  See
  ``web/audio_outputs.js``.

So the contract is deliberately narrow: report what exists, say which one the
system is using, and let the caller decide what it can honour.  Every probe is
best-effort and returns an empty list rather than raising — a missing speaker
name must never break playback.
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any

# Probing shells out, so it is capped hard: this runs when a user opens a picker
# and is not allowed to make that picker feel slow.
_PROBE_TIMEOUT_S = 4.0

# Transport strings CoreAudio reports, mapped to the vocabulary the UI uses for
# icons.  Anything unrecognised falls back to "speaker", which is always a safe
# thing to call an audio output.
_MACOS_TRANSPORTS = {
    "coreaudio_device_type_builtin": "builtin",
    "coreaudio_device_type_bluetooth": "bluetooth",
    "coreaudio_device_type_bluetooth_le": "bluetooth",
    "coreaudio_device_type_usb": "usb",
    "coreaudio_device_type_hdmi": "hdmi",
    "coreaudio_device_type_displayport": "hdmi",
    "coreaudio_device_type_airplay": "airplay",
    "coreaudio_device_type_virtual": "virtual",
    "coreaudio_device_type_aggregate": "virtual",
}

# Windows friendly names carry their own hints. AudioEndpoint gives no transport
# field, so the name is the only signal available for an icon.
_WINDOWS_NAME_HINTS = (
    ("bluetooth", "bluetooth"),
    ("hands-free", "bluetooth"),
    ("stereo", "bluetooth"),
    ("airpods", "bluetooth"),
    ("headphone", "headphones"),
    ("headset", "headphones"),
    ("hdmi", "hdmi"),
    ("displayport", "hdmi"),
    ("usb", "usb"),
)


def _run(command: list[str]) -> str:
    """Best-effort capture of a probe command's stdout, or "" on any failure."""
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_S,
            # A GUI build has no console; without this Windows flashes one.
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return completed.stdout if completed.returncode == 0 else ""


def _device(name: str, *, kind: str, is_default: bool) -> dict[str, Any]:
    return {
        # The name doubles as the match key against the browser's own device
        # labels, because the two enumerations share no stable identifier.
        "id": f"system:{name}",
        "name": name,
        "kind": kind,
        "is_default": bool(is_default),
    }


def _macos_outputs() -> list[dict[str, Any]]:
    """CoreAudio outputs via ``system_profiler``.

    Chosen over pyobjc's CoreAudio bindings because it needs no extra dependency
    in the frozen app and returns the same names the Sound menu shows, including
    the user-assigned name of a paired Bluetooth device.
    """
    raw = _run(["system_profiler", "SPAudioDataType", "-json"])
    if not raw:
        return []
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return []

    outputs: list[dict[str, Any]] = []
    for section in payload.get("SPAudioDataType") or []:
        for item in (section or {}).get("_items") or []:
            if not isinstance(item, dict):
                continue
            # An input-only device (a microphone) has no output channel count.
            if not item.get("coreaudio_device_output"):
                continue
            name = str(item.get("_name") or "").strip()
            if not name:
                continue
            outputs.append(_device(
                name,
                kind=_MACOS_TRANSPORTS.get(str(item.get("coreaudio_device_transport") or ""), "speaker"),
                is_default=item.get("coreaudio_default_audio_output_device") == "spaudio_yes",
            ))
    return outputs


# Render endpoints only, so microphones never reach a "play on" list. Status OK
# excludes endpoints that exist in the registry but are currently unplugged.
_WINDOWS_PROBE = (
    "Get-CimInstance -ClassName Win32_PnPEntity "
    "-Filter \"PNPClass='AudioEndpoint' AND Status='OK'\" "
    "| Select-Object -ExpandProperty Name"
)


def _windows_outputs() -> list[dict[str, Any]]:
    """Audio render endpoints via PowerShell.

    Windows reports no default-device flag through this interface, so every
    entry comes back with ``is_default`` false and the caller falls back to the
    browser's own ``default`` sink for that distinction.
    """
    raw = _run([
        "powershell", "-NoProfile", "-NonInteractive",
        "-ExecutionPolicy", "Bypass", "-Command", _WINDOWS_PROBE,
    ])
    if not raw:
        return []

    outputs: list[dict[str, Any]] = []
    for line in raw.splitlines():
        name = line.strip()
        if not name:
            continue
        lowered = name.lower()
        # AudioEndpoint covers capture endpoints too; drop the obvious ones.
        if "microphone" in lowered or "line in" in lowered:
            continue
        kind = next((value for hint, value in _WINDOWS_NAME_HINTS if hint in lowered), "speaker")
        outputs.append(_device(name, kind=kind, is_default=False))
    return outputs


def list_outputs() -> list[dict[str, Any]]:
    """Every audio output this computer can play through, best-effort.

    Returns an empty list on an unsupported platform or a failed probe.  Callers
    treat that as "offer the system default only" rather than as an error, which
    is exactly the behaviour Rainette had before this module existed.
    """
    try:
        if sys.platform == "darwin":
            return _macos_outputs()
        if sys.platform.startswith("win"):
            return _windows_outputs()
    except Exception:
        # Deliberately broad: a speaker list is a convenience, and no probe
        # failure is worth propagating into the playback path.
        return []
    return []


def default_output_name() -> str:
    """The system's current output device name, or "" when it is unknown."""
    return next((device["name"] for device in list_outputs() if device["is_default"]), "")


def open_sound_settings() -> bool:
    """Open the OS sound panel, where output really can be switched.

    The honest fallback for a device Rainette cannot route to itself.  A picker
    entry that quietly does nothing is worse than one that hands the user the
    control that works, so this is what those entries do.
    """
    try:
        if sys.platform == "darwin":
            return subprocess.run(
                ["open", "x-apple.systempreferences:com.apple.Sound-Settings.extension"],
                capture_output=True, timeout=_PROBE_TIMEOUT_S,
            ).returncode == 0
        if sys.platform.startswith("win"):
            return subprocess.run(
                ["cmd", "/c", "start", "", "ms-settings:sound"],
                capture_output=True, timeout=_PROBE_TIMEOUT_S,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False
    return False
