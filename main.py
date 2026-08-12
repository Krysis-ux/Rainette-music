"""Rainette Music — app entry point.

Starts the local server on a background thread, then opens the UI as a single
native desktop window (pywebview / WebView2), falling back to an Edge --app
window or the default browser. The player is the original in-page mini-player
bubble, which stays hidden until a track is played.

Any startup crash is written to `rainette-music.log` in the user's local
Rainette Music application-data directory.
"""

from __future__ import annotations

import atexit
import base64
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse
import urllib.request
import urllib.error
import webbrowser
import ctypes
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import qrcode
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

import audio_outputs
import server
import version

# Which desktop integration applies. Everything platform-specific in this file
# branches on exactly these two, so a third platform has one obvious seam to
# fill rather than a scattering of os.name checks.
IS_WINDOWS = os.name == "nt"
IS_MACOS = sys.platform == "darwin"

# ``ctypes.wintypes`` is only meaningful on Windows, and ``ctypes.windll`` does
# not exist elsewhere at all. Import it conditionally so the module stays
# importable everywhere; the Authenticode helpers that use it already refuse to
# run off Windows before they touch either name.
if IS_WINDOWS:
    from ctypes import wintypes
else:  # pragma: no cover - exercised by the macOS/Linux import path
    wintypes = None

if IS_MACOS:
    import macos_support
else:
    macos_support = None

WINDOW_TITLE = "Rainette Music"
PLAYER_WINDOW_TITLE = "Rainette Music Player"
# Windows groups taskbar buttons and picks the taskbar icon by AppUserModelID.
# Unset, a source run inherits pythonw.exe's identity and shows the Python icon.
# This must stay byte-identical to the installer's [Icons] AppUserModelID, or a
# pinned shortcut and the running window split into two taskbar buttons.
APP_USER_MODEL_ID = "Rainette.Music"
WINDOW_SIZE = (1060, 730)
MIN_SIZE = (780, 560)
PLAYER_SIZE = (300, 60)
PLAYER_EXPANDED_HEIGHT = 184
# The player window owns the only <audio> element and is driven over the socket, so
# its play() never carries user activation. Chromium blocks gesture-less playback
# without this flag. See _patch_webview2_autoplay() for how it reaches WebView2.
AUTOPLAY_FLAG = "--autoplay-policy=no-user-gesture-required"
# The exact arguments pywebview 6.2.1 hardcodes onto every WebView2 it creates.
PYWEBVIEW_BROWSER_ARGS = "--disable-features=ElasticOverscroll"
# Park the player window off-screen rather than hiding it. A hidden or
# minimized window reports document.hidden, and WebView2 then throttles media
# *loading* indefinitely -- play() is called but its promise never settles.
# Measured, and separate from the autoplay-gesture policy.
PLAYER_PARK_POS = (-32000, -32000)
# macOS refuses that coordinate: Cocoa reports no containing screen off-display
# and pywebview dereferences it unconditionally, so the player raises before it
# is shown. The parking is unnecessary here anyway — WKWebView does not throttle
# media for a hidden page — so it is created hidden and shown on demand.
PLAYER_MACOS_INITIAL_POS = (120, 120)

# One writable per-user location, shared with the server. Deriving it here
# instead of re-deriving from LOCALAPPDATA matters off Windows: that variable is
# unset, so the old expression put this log beside the source tree -- or inside
# a read-only .app bundle -- while server.py used the real platform directory.
APP_DATA_DIR = server.APP_DATA_DIR
LOG_PATH = APP_DATA_DIR / "rainette-music.log"
# PyInstaller exposes bundled data under _MEIPASS.  Source runs continue to use
# the repository root, while a self-contained --onedir build uses its internal
# resource directory instead of relying on Python being installed on PATH.
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
ICON_PATH = RESOURCE_DIR / "web" / "assets" / "rainette-icon.ico"
# AppKit loads a window/app icon through NSImage, which reads .icns and .png but
# not Windows .ico. Prefer a real .icns when the build ships one, then the PNG
# that already exists for the web UI, so a macOS run gets Rainette's icon in the
# Dock instead of the bare Python rocket.
MACOS_ICON_CANDIDATES = (
    RESOURCE_DIR / "web" / "assets" / "rainette-icon.icns",
    RESOURCE_DIR / "web" / "assets" / "rainette-icon-256.png",
)


def _window_icon_path() -> Path | None:
    """The icon file the running platform's toolkit can actually display."""
    if IS_MACOS:
        return next((path for path in MACOS_ICON_CANDIDATES if path.is_file()), None)
    return ICON_PATH if ICON_PATH.is_file() else None
GITHUB_REPO = "Krysis-ux/Rainette-music"
GITHUB_RELEASES_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases?per_page=20"
GITHUB_RELEASE_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases"
GITHUB_ASSET_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/assets"
RELEASE_DOWNLOAD_BASE = f"https://github.com/{GITHUB_REPO}/releases/latest/download"
UPDATE_USER_AGENT = "RainetteMusic (local desktop app)"
UPDATE_API_VERSION = "2022-11-28"
WINDOWS_INSTALLER_ASSET = "RainetteMusicSetup.exe"
WINDOWS_MANIFEST_ASSET = "latest.json"
WINDOWS_MANIFEST_SIGNATURE_ASSET = f"{WINDOWS_MANIFEST_ASSET}.sig"
MAX_INSTALLER_BYTES = 512 * 1024 * 1024
MAX_INTEGRITY_ASSET_BYTES = 64 * 1024
MAX_RELEASE_METADATA_BYTES = 2 * 1024 * 1024
_STABLE_TAG_RE = re.compile(r"^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
_CERT_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SOURCE_RUN_UPDATE_MSG = (
    "Updates apply to the installed Rainette Music app, not a source run. "
    "Install the latest release from the Rainette repository instead."
)
UNSUPPORTED_PLATFORM_UPDATE_MSG = (
    "Rainette's built-in updater installs the signed Windows release, so it "
    "cannot update this build. Reinstall from the Rainette repository instead."
)
UNCONFIGURED_KEY_UPDATE_MSG = (
    "This build has no pinned release signing key, so Rainette cannot verify an "
    "update it downloads. Install the latest release from the Rainette repository instead."
)


def log(msg: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n")
    except Exception:
        pass


def _enable_high_dpi() -> None:
    if os.name != "nt":
        return
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass


def _set_app_user_model_id() -> None:
    """Give the process an explicit taskbar identity so Windows shows Rainette
    instead of inheriting the host interpreter's (python/pythonw) icon and label.
    Must run before any window is created."""
    if os.name != "nt":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
    except Exception:
        pass


def _qr_data_url(value: str) -> str:
    """Render a QR locally so pairing secrets never reach a third party."""
    qr = qrcode.QRCode()
    qr.add_data(value)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white")
    output = io.BytesIO()
    image.save(output, format="PNG")
    encoded = base64.b64encode(output.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _default_player_pos() -> tuple[int, int]:
    """First-launch player position, before the user has ever dragged it:
    bottom-right corner of the primary screen, matching where such a widget
    conventionally sits."""
    try:
        import webview  # type: ignore

        screen = webview.screens[0]
        margin = 24
        return (
            int(screen.x + screen.width - PLAYER_SIZE[0] - margin),
            int(screen.y + screen.height - PLAYER_SIZE[1] - margin),
        )
    except Exception:
        return (100, 100)


# ── In-app updater ──────────────────────────────────────────────────────────
#
# A GitHub release may contain source archives or unrelated attachments. Only
# these three exact Windows asset names are ever eligible for the native
# updater, and their numeric GitHub asset IDs are pinned together.


@dataclass(frozen=True)
class _UpdateAsset:
    asset_id: int
    name: str
    size: int
    sha256: str
    content_type: str


@dataclass(frozen=True)
class _UpdateCandidate:
    release_id: int
    tag: str
    release_version: str
    version_parts: tuple[int, int, int]
    installer: _UpdateAsset
    manifest: _UpdateAsset
    manifest_signature: _UpdateAsset
    notes: str

    @property
    def candidate_id(self) -> str:
        identity = {
            "release_id": self.release_id,
            "tag": self.tag,
            "version": self.release_version,
            "assets": [
                [self.installer.asset_id, self.installer.name, self.installer.size, self.installer.sha256],
                [self.manifest.asset_id, self.manifest.name, self.manifest.size, self.manifest.sha256],
                [self.manifest_signature.asset_id, self.manifest_signature.name,
                 self.manifest_signature.size, self.manifest_signature.sha256],
            ],
        }
        encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @property
    def pinned_identity(self) -> tuple:
        return (
            self.release_id,
            self.tag,
            self.release_version,
            self.installer,
            self.manifest,
            self.manifest_signature,
        )


def _github_request(url: str, *, accept: str = "application/vnd.github+json") -> urllib.request.Request:
    return urllib.request.Request(
        url,
        headers={
            "User-Agent": UPDATE_USER_AGENT,
            "Accept": accept,
            "X-GitHub-Api-Version": UPDATE_API_VERSION,
        },
    )


def _read_limited(response, limit: int) -> bytes:
    data = response.read(limit + 1)
    if len(data) > limit:
        raise RuntimeError("GitHub response exceeded the allowed size")
    return data


def _fetch_json(url: str, *, timeout: int = 6):
    with urllib.request.urlopen(_github_request(url), timeout=timeout) as response:
        return json.loads(_read_limited(response, MAX_RELEASE_METADATA_BYTES).decode("utf-8"))


def _strict_release_version(tag: object) -> tuple[str, tuple[int, int, int]] | None:
    match = _STABLE_TAG_RE.fullmatch(str(tag or ""))
    if not match:
        return None
    parts = tuple(int(part) for part in match.groups())
    return ".".join(str(part) for part in parts), parts


def _asset_from_payload(payload: object, expected_name: str) -> _UpdateAsset | None:
    if not isinstance(payload, dict) or payload.get("name") != expected_name:
        return None
    asset_id = payload.get("id")
    size = payload.get("size")
    if isinstance(asset_id, bool) or not isinstance(asset_id, int) or asset_id <= 0:
        return None
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        return None
    if payload.get("state") != "uploaded":
        return None
    digest = str(payload.get("digest") or "")
    digest_match = re.fullmatch(r"sha256:([0-9a-fA-F]{64})", digest)
    if not digest_match:
        return None
    # The content-type check is upload hygiene, not a security boundary (the
    # digest + manifest signature + hash chain are); it accepts every MIME type
    # GitHub's upload path actually assigns to these extensions. The live
    # publisher (action-gh-release) tags .exe as x-msdos-program and .sig as
    # pgp-signature — verified against a real published release.
    if expected_name == WINDOWS_INSTALLER_ASSET:
        if size > MAX_INSTALLER_BYTES:
            return None
        allowed_types = {"application/x-msdownload", "application/x-msdos-program",
                         "application/vnd.microsoft.portable-executable", "application/octet-stream"}
    elif expected_name == WINDOWS_MANIFEST_SIGNATURE_ASSET:
        if size > MAX_INTEGRITY_ASSET_BYTES:
            return None
        allowed_types = {"text/plain", "application/pgp-signature", "application/octet-stream"}
    else:
        if size > MAX_INTEGRITY_ASSET_BYTES:
            return None
        allowed_types = {"application/json", "text/plain", "application/octet-stream"}
    content_type = str(payload.get("content_type") or "").lower()
    if content_type not in allowed_types:
        return None
    return _UpdateAsset(asset_id, expected_name, size, digest_match.group(1).lower(), content_type)


def _candidate_from_release(payload: object) -> _UpdateCandidate | None:
    if not isinstance(payload, dict):
        return None
    if payload.get("draft") is not False or payload.get("prerelease") is not False:
        return None
    if not payload.get("published_at"):
        return None
    release_id = payload.get("id")
    if isinstance(release_id, bool) or not isinstance(release_id, int) or release_id <= 0:
        return None
    parsed = _strict_release_version(payload.get("tag_name"))
    if parsed is None:
        return None
    release_version, version_parts = parsed
    assets = payload.get("assets")
    if not isinstance(assets, list):
        return None
    selected = {}
    for expected_name in (WINDOWS_INSTALLER_ASSET, WINDOWS_MANIFEST_ASSET, WINDOWS_MANIFEST_SIGNATURE_ASSET):
        matches = [asset for asset in assets if isinstance(asset, dict) and asset.get("name") == expected_name]
        if len(matches) != 1:
            return None
        selected[expected_name] = _asset_from_payload(matches[0], expected_name)
        if selected[expected_name] is None:
            return None
    selected_assets = list(selected.values())
    if len({asset.asset_id for asset in selected_assets}) != len(selected_assets):
        return None
    return _UpdateCandidate(
        release_id=release_id,
        tag=str(payload["tag_name"]),
        release_version=release_version,
        version_parts=version_parts,
        installer=selected[WINDOWS_INSTALLER_ASSET],
        manifest=selected[WINDOWS_MANIFEST_ASSET],
        manifest_signature=selected[WINDOWS_MANIFEST_SIGNATURE_ASSET],
        notes=str(payload.get("body") or "")[:2000],
    )


def _public_update_result(current: str, candidate: _UpdateCandidate) -> dict:
    return {
        "status": "update",
        "current": current,
        "latest": candidate.release_version,
        "tag": candidate.tag,
        "candidate_id": candidate.candidate_id,
        "release_id": candidate.release_id,
        "notes": candidate.notes,
        "release_url": (
            f"https://github.com/{GITHUB_REPO}/releases/tag/"
            f"{urllib.parse.quote(candidate.tag, safe='')}"
        ),
    }


def _check_for_updates(current: str = version.APP_VERSION) -> tuple[dict, _UpdateCandidate | None]:
    # Never offer an update this build could not install. A source run has no
    # installer to replace, and a build with no pinned signing key can only
    # refuse whatever it downloads, so an install button in either case would
    # lead every user to the same dead end. Checked before the request so these
    # builds cost no GitHub call and name no candidate.
    if not getattr(sys, "frozen", False):
        return {"status": "unavailable", "current": current, "msg": SOURCE_RUN_UPDATE_MSG}, None
    if not _update_signing_configured():
        return {"status": "unavailable", "current": current, "msg": UNCONFIGURED_KEY_UPDATE_MSG}, None
    try:
        payload = _fetch_json(GITHUB_RELEASES_API)
    except urllib.error.HTTPError as exc:
        status = "unavailable" if exc.code == 404 else "check_failed"
        return {"status": status, "current": current}, None
    except Exception:
        return {"status": "check_failed", "current": current}, None
    if not isinstance(payload, list):
        return {"status": "check_failed", "current": current}, None
    if not payload:
        return {"status": "unavailable", "current": current}, None
    current_parts = version.parse_version(current)
    candidates = [candidate for item in payload if (candidate := _candidate_from_release(item))]
    newer = [candidate for candidate in candidates if candidate.version_parts > current_parts]
    if not newer:
        return {"status": "current", "current": current}, None
    candidate = max(newer, key=lambda item: item.version_parts)
    return _public_update_result(current, candidate), candidate


def check_for_updates(current: str = version.APP_VERSION) -> dict:
    """Return the highest valid newer Windows release, never an arbitrary asset."""
    result, _candidate = _check_for_updates(current)
    return result


def _revalidate_candidate(candidate: _UpdateCandidate) -> _UpdateCandidate | None:
    """Re-fetch one pinned release and reject any changed identity or downgrade."""
    payload = _fetch_json(f"{GITHUB_RELEASE_API}/{candidate.release_id}", timeout=10)
    refreshed = _candidate_from_release(payload)
    if refreshed is None:
        return None
    if refreshed.version_parts <= version.parse_version(version.APP_VERSION):
        return None
    if refreshed.pinned_identity != candidate.pinned_identity:
        return None
    return refreshed


def _validate_asset_response_url(url: str, asset_id: int) -> None:
    parsed = urllib.parse.urlparse(str(url or ""))
    if parsed.scheme.lower() != "https":
        raise RuntimeError("GitHub returned an insecure update asset URL")
    host = (parsed.hostname or "").lower()
    if host == "api.github.com":
        expected_path = f"/repos/{GITHUB_REPO}/releases/assets/{asset_id}"
        if parsed.path != expected_path:
            raise RuntimeError("GitHub returned the wrong update asset")
        return
    if host not in {"objects.githubusercontent.com", "release-assets.githubusercontent.com"}:
        raise RuntimeError("GitHub redirected an update asset to an untrusted host")


def _read_asset_response(response, asset: _UpdateAsset, max_bytes: int, sink=None, progress=None) -> bytes:
    _validate_asset_response_url(response.geturl(), asset.asset_id)
    content_length = response.headers.get("Content-Length") if getattr(response, "headers", None) else None
    if content_length:
        try:
            declared = int(content_length)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("update asset had an invalid Content-Length") from exc
        if declared != asset.size or declared > max_bytes:
            raise RuntimeError("update asset size did not match its GitHub metadata")

    hasher = hashlib.sha256()
    collected = bytearray()
    total = 0
    while True:
        chunk = response.read(64 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes or total > asset.size:
            raise RuntimeError("update asset exceeded its allowed size")
        hasher.update(chunk)
        if sink is None:
            collected.extend(chunk)
        else:
            sink.write(chunk)
        if progress is not None:
            progress(total, asset.size)
    if total != asset.size:
        raise RuntimeError("update asset was truncated")
    if hasher.hexdigest().lower() != asset.sha256:
        raise RuntimeError("update asset failed its GitHub digest check")
    return bytes(collected)


def _fetch_asset_bytes(asset: _UpdateAsset, max_bytes: int, *, timeout: int = 30) -> bytes:
    if asset.size > max_bytes:
        raise RuntimeError("update integrity metadata exceeded its allowed size")
    url = f"{GITHUB_ASSET_API}/{asset.asset_id}"
    with urllib.request.urlopen(
        _github_request(url, accept="application/octet-stream"),
        timeout=timeout,
    ) as response:
        return _read_asset_response(response, asset, max_bytes)


def _stream_asset_to_path(asset: _UpdateAsset, path: Path, *, timeout: int = 180, progress=None) -> Path:
    if asset.size > MAX_INSTALLER_BYTES:
        raise RuntimeError("installer exceeded its allowed size")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    partial = path.with_name(path.name + ".part")
    if path.exists() or partial.exists():
        raise RuntimeError("update destination was not empty")
    url = f"{GITHUB_ASSET_API}/{asset.asset_id}"
    try:
        with urllib.request.urlopen(
            _github_request(url, accept="application/octet-stream"),
            timeout=timeout,
        ) as response, open(partial, "xb") as output:
            _read_asset_response(response, asset, MAX_INSTALLER_BYTES, sink=output, progress=progress)
            output.flush()
            os.fsync(output.fileno())
        os.replace(partial, path)
        return path
    except Exception:
        try:
            partial.unlink(missing_ok=True)
        except Exception:
            pass
        raise


def _validate_release_manifest(candidate: _UpdateCandidate, manifest_bytes: bytes) -> None:
    """Check the schema-2 manifest's claims. Only ever called on manifest bytes
    whose Ed25519 signature already verified — these fields are trusted because
    of that check, not because GitHub served them."""
    try:
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except Exception as exc:
        raise RuntimeError("release manifest is malformed") from exc
    if not isinstance(manifest, dict):
        raise RuntimeError("release manifest is malformed")
    if manifest.get("schema") != 2:
        raise RuntimeError("release manifest schema is not supported")
    if manifest.get("artifact") != WINDOWS_INSTALLER_ASSET:
        raise RuntimeError("release manifest named the wrong installer")
    if str(manifest.get("version") or "") != candidate.release_version:
        raise RuntimeError("release manifest version did not match its tag")
    # local-test / dev builds must stay non-installable even when validly signed.
    if manifest.get("channel") not in {"stable", "release"}:
        raise RuntimeError("release manifest was not a stable Windows release")
    # The signed hash is the installer's identity: it must match the digest
    # GitHub reports for the asset, and the streamed bytes are hashed against
    # that same digest, so a swapped installer can never reach the disk name.
    if str(manifest.get("sha256") or "").lower() != candidate.installer.sha256:
        raise RuntimeError("release manifest checksum did not match the installer")
    if _authenticode_pin_configured():
        authenticode = manifest.get("authenticode")
        if not isinstance(authenticode, dict) or authenticode.get("signed") is not True:
            raise RuntimeError("release manifest did not require an Authenticode-signed installer")


def _download_verified_installer(candidate: _UpdateCandidate, dest_dir: Path, progress=None) -> Path:
    """Download the pinned assets and stream the authenticated installer.

    The manifest signature is verified before a single field of the manifest is
    read — everything downstream trusts the manifest only because of that check.
    """
    manifest_bytes = _fetch_asset_bytes(candidate.manifest, MAX_INTEGRITY_ASSET_BYTES, timeout=15)
    signature_bytes = _fetch_asset_bytes(candidate.manifest_signature, MAX_INTEGRITY_ASSET_BYTES, timeout=15)
    _verify_manifest_signature(manifest_bytes, signature_bytes)
    _validate_release_manifest(candidate, manifest_bytes)
    return _stream_asset_to_path(candidate.installer, dest_dir / WINDOWS_INSTALLER_ASSET, progress=progress)


def _configured_update_public_keys() -> tuple[bytes, ...]:
    """Return the committed Ed25519 release keys (raw 32 bytes each), failing
    closed when absent or malformed. Multiple comma-separated keys support an
    intentional rotation: ship an update that trusts both, then switch CI."""
    configured = version.UPDATE_SIGNER_PUBLIC_KEY
    if not isinstance(configured, str):
        raise RuntimeError("Rainette update signing key is invalid")
    keys = []
    for value in configured.split(","):
        value = value.strip()
        if not value:
            continue
        try:
            raw = base64.b64decode(value, validate=True)
        except Exception as exc:
            raise RuntimeError("Rainette update signing key is invalid") from exc
        if len(raw) != 32:
            raise RuntimeError("Rainette update signing key is invalid")
        keys.append(raw)
    if not keys:
        raise RuntimeError("Rainette update signing key is not configured")
    return tuple(keys)


def _update_signing_configured() -> bool:
    """Report whether this build carries a release key it could verify against.

    This decides whether an update is *offered*; the install path still calls
    _configured_update_public_keys() and fails closed itself.
    """
    try:
        _configured_update_public_keys()
    except RuntimeError:
        return False
    return True


def _verify_manifest_signature(manifest_bytes: bytes, signature_bytes: bytes) -> None:
    """Require a valid Ed25519 signature over the manifest's raw bytes from one
    of the committed release keys. This is the updater's root of trust."""
    try:
        signature = base64.b64decode(signature_bytes.decode("ascii").strip(), validate=True)
    except Exception as exc:
        raise RuntimeError("release manifest signature is malformed") from exc
    if len(signature) != 64:
        raise RuntimeError("release manifest signature is malformed")
    for raw_key in _configured_update_public_keys():
        try:
            Ed25519PublicKey.from_public_bytes(raw_key).verify(signature, manifest_bytes)
            return
        except InvalidSignature:
            continue
    raise RuntimeError("release manifest signature is not from Rainette's release signing key")


def _authenticode_pin_configured() -> bool:
    """Whether the optional Authenticode layer is enabled for this build.

    Empty means Rainette ships without a code-signing certificate and the
    Ed25519 manifest signature is the sole (sufficient) root of trust. Any
    non-empty value — valid or not — turns enforcement on; an invalid value
    then fails closed inside _configured_update_signer_hashes()."""
    configured = version.UPDATE_SIGNER_CERT_SHA256
    return isinstance(configured, str) and bool(configured.strip())


def _configured_update_signer_hashes() -> frozenset[str]:
    """Return the embedded Authenticode signer allow-list, failing closed if absent."""
    configured = version.UPDATE_SIGNER_CERT_SHA256
    if not isinstance(configured, str):
        raise RuntimeError("Rainette update signer identity is invalid")
    values = [value.strip().lower() for value in configured.split(",") if value.strip()]
    if not values:
        raise RuntimeError("Rainette update signer identity is not configured")
    if any(not _CERT_SHA256_RE.fullmatch(value) for value in values):
        raise RuntimeError("Rainette update signer identity is invalid")
    return frozenset(values)


def _authenticode_signer_sha256(path: Path) -> str:
    """Read the SHA-256 fingerprint of the file's embedded signer certificate."""
    system_root = Path(os.environ.get("SystemRoot") or r"C:\Windows")
    powershell = system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if not powershell.is_file():
        raise RuntimeError("Windows signer inspection is unavailable")

    # The path is passed through a dedicated child-process environment variable,
    # never interpolated into the PowerShell program. SignatureType rejects a
    # catalog-only signature: Rainette installers must carry their signer.
    script = """
$ErrorActionPreference = 'Stop'
$signature = Get-AuthenticodeSignature -LiteralPath $env:RAINETTE_UPDATE_INSTALLER
if ($null -eq $signature.SignerCertificate) { exit 3 }
if ([string]$signature.SignatureType -ne 'Authenticode') { exit 4 }
$sha256 = [Security.Cryptography.SHA256]::Create()
try {
    $hash = $sha256.ComputeHash($signature.SignerCertificate.RawData)
} finally {
    $sha256.Dispose()
}
[BitConverter]::ToString($hash).Replace('-', '').ToLowerInvariant()
""".strip()
    environment = os.environ.copy()
    environment["RAINETTE_UPDATE_INSTALLER"] = str(path.resolve())
    try:
        completed = subprocess.run(
            [
                str(powershell),
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                script,
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=environment,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("Windows signer inspection failed") from exc
    signer_hash = completed.stdout.strip().lower()
    if completed.returncode != 0 or not _CERT_SHA256_RE.fullmatch(signer_hash):
        raise RuntimeError("installer signer certificate could not be verified")
    return signer_hash


def _verify_windows_authenticode_trust(path: Path) -> None:
    """Require Windows to trust the installer's embedded Authenticode signature."""
    if os.name != "nt":
        raise RuntimeError("Windows Authenticode verification is unavailable")
    if not path.is_file():
        raise RuntimeError("downloaded installer is missing")

    class _GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", wintypes.DWORD),
            ("Data2", wintypes.WORD),
            ("Data3", wintypes.WORD),
            ("Data4", wintypes.BYTE * 8),
        ]

    class _WINTRUST_FILE_INFO(ctypes.Structure):
        _fields_ = [
            ("cbStruct", wintypes.DWORD),
            ("pcwszFilePath", wintypes.LPCWSTR),
            ("hFile", wintypes.HANDLE),
            ("pgKnownSubject", ctypes.POINTER(_GUID)),
        ]

    class _WINTRUST_DATA(ctypes.Structure):
        _fields_ = [
            ("cbStruct", wintypes.DWORD),
            ("pPolicyCallbackData", wintypes.LPVOID),
            ("pSIPClientData", wintypes.LPVOID),
            ("dwUIChoice", wintypes.DWORD),
            ("fdwRevocationChecks", wintypes.DWORD),
            ("dwUnionChoice", wintypes.DWORD),
            ("pFile", ctypes.POINTER(_WINTRUST_FILE_INFO)),
            ("dwStateAction", wintypes.DWORD),
            ("hWVTStateData", wintypes.HANDLE),
            ("pwszURLReference", wintypes.LPCWSTR),
            ("dwProvFlags", wintypes.DWORD),
            ("dwUIContext", wintypes.DWORD),
        ]

    action = _GUID(
        0x00AAC56B,
        0xCD44,
        0x11D0,
        (wintypes.BYTE * 8)(0x8C, 0xC2, 0x00, 0xC0, 0x4F, 0xC2, 0x95, 0xEE),
    )
    file_info = _WINTRUST_FILE_INFO()
    file_info.cbStruct = ctypes.sizeof(_WINTRUST_FILE_INFO)
    file_info.pcwszFilePath = str(path.resolve())
    file_info.hFile = None
    file_info.pgKnownSubject = None

    trust_data = _WINTRUST_DATA()
    trust_data.cbStruct = ctypes.sizeof(_WINTRUST_DATA)
    trust_data.dwUIChoice = 2  # WTD_UI_NONE
    trust_data.fdwRevocationChecks = 1  # WTD_REVOKE_WHOLECHAIN
    trust_data.dwUnionChoice = 1  # WTD_CHOICE_FILE
    trust_data.pFile = ctypes.pointer(file_info)
    trust_data.dwStateAction = 1  # WTD_STATEACTION_VERIFY
    trust_data.dwProvFlags = 0x00000040  # WTD_REVOCATION_CHECK_CHAIN
    trust_data.dwUIContext = 0  # WTD_UICONTEXT_EXECUTE

    wintrust = ctypes.WinDLL("wintrust", use_last_error=True)
    verify = wintrust.WinVerifyTrust
    verify.argtypes = [wintypes.HWND, ctypes.POINTER(_GUID), ctypes.POINTER(_WINTRUST_DATA)]
    verify.restype = wintypes.LONG
    hwnd = wintypes.HWND(-1)
    result = int(verify(hwnd, ctypes.byref(action), ctypes.byref(trust_data)))
    try:
        if result != 0:
            raise RuntimeError(f"installer Authenticode verification failed (0x{result & 0xFFFFFFFF:08x})")
    finally:
        trust_data.dwStateAction = 2  # WTD_STATEACTION_CLOSE
        verify(hwnd, ctypes.byref(action), ctypes.byref(trust_data))


def _verify_authenticode(path: Path) -> None:
    """Require OS trust and an embedded signer explicitly pinned to Rainette."""
    if os.name != "nt":
        raise RuntimeError("Windows Authenticode verification is unavailable")
    if not path.is_file():
        raise RuntimeError("downloaded installer is missing")
    allowed_signers = _configured_update_signer_hashes()
    _verify_windows_authenticode_trust(path)
    actual_signer = _authenticode_signer_sha256(path)
    if actual_signer not in allowed_signers:
        raise RuntimeError("installer signer certificate is not Rainette's trusted release identity")


class WindowApi:
    def __init__(self) -> None:
        self._main_window = None
        self._player_window = None
        self._player_on_top = False
        self._player_can_show = False
        self._player_collapsed = True
        self._update_candidate = None
        self._update_candidate_lock = threading.Lock()
        self._update_apply_lock = threading.Lock()
        self._update_progress_lock = threading.Lock()
        self._update_progress = {"phase": "idle"}
        self._update_worker = None
        # Remembers where the user last dragged the player window so reveals
        # restore it there instead of resetting to a fixed spot each time.
        self._player_onscreen_pos = _default_player_pos()

    def bind_player(self, player_window) -> None:
        self._player_window = player_window

    def bind_main(self, main_window) -> None:
        self._main_window = main_window

    def companion_create_invitation(self):
        """Expose explicit desktop-controlled pairing to the settings UI.

        The QR encodes a link to the hosted PWA with the endpoint and a
        short-lived invitation in the URL *fragment*, so those values are
        processed by the phone's browser and never sent to the static host.
        """
        try:
            invitation = server.create_companion_invitation()
            return {
                "ok": True,
                "pairing_url": invitation["pairing_url"],
                "pairing_qr_data_url": _qr_data_url(invitation["pairing_url"]),
                "expires_at": invitation["expires_at"],
                "endpoint": invitation["endpoint"],
                "tunnel_configured": invitation["tunnel_configured"],
                "endpoint_is_local": invitation["endpoint_is_local"],
                "companion_port": invitation["companion_port"],
            }
        except Exception as exc:
            log(f"companion invitation failed: {exc}")
            return {"ok": False, "msg": str(exc)}

    def tunnel_status(self):
        """Report the managed HTTPS tunnel so the UI can poll it."""
        try:
            return {"ok": True, **server.tunnel_status()}
        except Exception as exc:
            return {"ok": False, "msg": str(exc)}

    def tunnel_helper_download(self):
        """Download cloudflared as its own explicit, one-time step."""
        try:
            return {"ok": True, **server.download_tunnel_helper()}
        except Exception as exc:
            log(f"tunnel helper download failed: {exc}")
            return {"ok": False, "msg": str(exc)}

    def tunnel_start(self):
        """Generate the HTTPS tunnel and publish its address.

        Returns as soon as the work is scheduled; the settings page polls
        ``tunnel_status`` for the address Cloudflare hands out.
        """
        try:
            return {"ok": True, **server.start_tunnel()}
        except Exception as exc:
            log(f"tunnel start failed: {exc}")
            return {"ok": False, "msg": str(exc)}

    def tunnel_stop(self):
        try:
            return {"ok": True, **server.stop_tunnel()}
        except Exception as exc:
            log(f"tunnel stop failed: {exc}")
            return {"ok": False, "msg": str(exc)}

    def pwa_config_get(self):
        """Return the PWA address and public tunnel address for the UI."""
        try:
            return {"ok": True, **server.read_pwa_config(), "default_pwa_url": server.DEFAULT_PWA_URL}
        except Exception as exc:
            return {"ok": False, "msg": str(exc)}

    def pwa_config_set(self, pwa_url: str, public_url: str):
        """Persist the two addresses every future pairing link is built from."""
        try:
            return {"ok": True, **server.write_pwa_config(str(pwa_url or ""), str(public_url or ""))}
        except ValueError as exc:
            return {"ok": False, "msg": str(exc)}
        except Exception as exc:
            log(f"pwa config update failed: {exc}")
            return {"ok": False, "msg": str(exc)}

    def open_sound_settings(self):
        """Hand the user the OS control that can actually move system audio.

        Deliberately a desktop-only bridge call rather than a companion command:
        a paired phone has no business opening panels on someone's computer.
        """
        return {"ok": audio_outputs.open_sound_settings()}

    def companion_management_state(self):
        return server.companion_management_state()

    def companion_approve_request(self, request_id: str):
        try:
            return server.approve_companion_request(str(request_id or ""))
        except Exception as exc:
            return {"ok": False, "msg": str(exc)}

    def companion_reject_request(self, request_id: str):
        return server.reject_companion_request(str(request_id or ""))

    def companion_revoke_device(self, device_id: str):
        return server.revoke_companion_device(str(device_id or ""))

    def app_version(self):
        return version.APP_VERSION

    def check_for_updates(self):
        """Report whether a newer GitHub release exists (see check_for_updates())."""
        result, candidate = _check_for_updates()
        with self._update_candidate_lock:
            # A failed refresh clears the old candidate. Installation must be
            # tied to the most recent successful check, never stale UI state.
            self._update_candidate = candidate
        return result

    def _set_update_progress(self, phase: str, **fields) -> None:
        with self._update_progress_lock:
            self._update_progress = {"phase": phase, **fields}

    def update_progress(self):
        """Snapshot of the in-flight install for the UI's progress poll.

        apply_update() returns "installing" as soon as the download worker
        starts, so a late verification failure is only observable here — the
        poll drives the error state, not just the percentage.
        """
        with self._update_progress_lock:
            return dict(self._update_progress)

    def apply_update(self, candidate_id: str = ""):
        """Start downloading, verifying, and launching the new installer, then
        quit so it can replace the running files. The installer relaunches the
        app when done. Every pre-flight guard stays synchronous; only the
        download/verify/launch tail runs on a worker so the UI can poll
        update_progress() for a real progress bar.

        Self-updating only makes sense for the packaged build: a source checkout
        has no installer to swap in, so say so instead of doing something surprising.
        """
        if not getattr(sys, "frozen", False):
            return {"status": "unsupported", "msg": SOURCE_RUN_UPDATE_MSG}
        # The only artifact Rainette knows how to install is the signed Windows
        # installer, driven with Inno Setup's silent switches and verified with
        # Authenticode. Refuse here with an explanation rather than downloading
        # an .exe no other platform can run.
        if not IS_WINDOWS:
            return {"status": "unsupported", "msg": UNSUPPORTED_PLATFORM_UPDATE_MSG}
        if not self._update_apply_lock.acquire(blocking=False):
            return {"status": "busy", "msg": "An update is already being installed."}
        worker_started = False
        try:
            with self._update_candidate_lock:
                candidate = self._update_candidate
            if candidate is None:
                return {"status": "no_update", "msg": "Check for updates before installing."}
            if not isinstance(candidate_id, str) or candidate_id != candidate.candidate_id:
                return {"status": "stale", "msg": "The selected update is no longer current. Check again."}
            try:
                candidate = _revalidate_candidate(candidate)
            except Exception as exc:
                log(f"update revalidation failed: {exc}")
                candidate = None
            if candidate is None:
                return {"status": "stale", "msg": "The selected release changed. Check for updates again."}
            self._set_update_progress("downloading", received=0, total=candidate.installer.size,
                                      version=candidate.release_version)
            worker = threading.Thread(target=self._apply_update_worker, args=(candidate,),
                                      name="rainette-update", daemon=True)
            self._update_worker = worker
            worker.start()
            worker_started = True
            return {"status": "installing", "version": candidate.release_version}
        finally:
            if not worker_started:
                self._update_apply_lock.release()

    def _apply_update_worker(self, candidate: _UpdateCandidate) -> None:
        """Owns _update_apply_lock (acquired by apply_update). On failure it
        cleans up and releases the lock; on success it keeps the lock held until
        this process exits — releasing it during the 0.6-second UI handoff
        window allowed a second bridge call to delete or launch the
        already-verified installer again."""
        update_dir = None
        install_started = False
        try:
            update_dir = Path(tempfile.mkdtemp(prefix="RainetteMusicUpdate-"))

            def on_progress(received: int, total: int) -> None:
                self._set_update_progress("downloading", received=received, total=total,
                                          version=candidate.release_version)

            installer = _download_verified_installer(candidate, update_dir, progress=on_progress)
            self._set_update_progress("verifying", version=candidate.release_version)
            if _authenticode_pin_configured():
                _verify_authenticode(installer)
            self._set_update_progress("launching", version=candidate.release_version)
            # /autorelaunch=1 is read by the installer's [Code] to relaunch the app
            # after a silent install; /VERYSILENT keeps the whole thing headless.
            subprocess.Popen(
                [str(installer), "/VERYSILENT", "/NORESTART", "/autorelaunch=1"],
                close_fds=True,
            )
            install_started = True
            self._set_update_progress("installing", version=candidate.release_version)
        except Exception as exc:
            if update_dir is not None:
                shutil.rmtree(update_dir, ignore_errors=True)
            log(f"update install failed: {exc}")
            self._set_update_progress(
                "failed",
                code="verification_or_launch_failed",
                message="Rainette could not verify or start the update. Please try again.",
            )
        finally:
            if not install_started:
                self._update_apply_lock.release()
        if install_started:
            # Give the progress poll a moment to observe "installing", then exit
            # so the running exe and _internal files unlock for the installer.
            threading.Timer(0.6, self._quit_for_update).start()

    def _quit_for_update(self) -> None:
        try:
            if self._player_window:
                self._player_window.destroy()
            if self._main_window:
                self._main_window.destroy()
        except Exception:
            pass
        # Belt and suspenders: if destroying the windows didn't end the process,
        # force it so the installer isn't left waiting on a locked file.
        threading.Timer(2.0, lambda: os._exit(0)).start()

    def main_reveal(self):
        if not self._main_window:
            return False
        try:
            self._main_window.show()
            self._main_window.restore()
            return True
        except Exception as exc:
            log(f"main window reveal failed: {exc}")
            return False

    def player_allow_show(self):
        self._player_can_show = True

    def show_player(self):
        if not (self._player_window and self._player_can_show):
            return
        if IS_MACOS:
            # Nothing to un-park positionally: the macOS player is faded rather
            # than moved off-screen, so it reappears exactly where it was left.
            self._player_window.move(*self._player_onscreen_pos)
            macos_support.unpark_player(self._player_window, log)
            self._shape_player()
            return
        self._player_window.move(*self._player_onscreen_pos)
        self._player_window.show()
        self._player_window.restore()
        # Shape only once the window is back at its Normal-state size.
        # _shape_player() derives the rounded-rect mask from form.Width/Height, so
        # running it while the window is still minimized bakes the minimized bounds
        # into the region and clips the restored WebView2 content - the "mini player
        # only half loads when I re-open it" bug. player_resize() already sizes
        # before shaping for exactly this reason.
        self._shape_player()

    def reveal_player(self):
        """Unlock and show the player window in one call.

        show_player() alone is a no-op until player_allow_show() has run, and
        the two were previously fired as separate, un-awaited JS->Python bridge
        calls (each dispatched on its own thread), which raced. Collapsing them
        into a single idempotent call removes that race entirely.
        """
        self._player_can_show = True
        self.show_player()

    def player_hide(self):
        self._park_player()

    def player_minimize(self):
        # Minimizing (like hiding) previously put the window into a state where
        # document.hidden becomes true, which would throttle an already-playing
        # stream's buffering to a halt the moment the user minimized it. Parking
        # off-screen keeps it "shown" so playback that's already underway keeps
        # running in the background.
        self._park_player()

    def _park_player(self) -> None:
        """Move the player window far off-screen instead of hiding/minimizing it.

        See PLAYER_PARK_POS for why: a .hide()'d or .minimize()'d window reports
        document.hidden === true, which makes WebView2/Chromium throttle media
        resource loading indefinitely - audio.play() gets called but its promise
        never settles. Staying "shown" but off-screen avoids that while remaining
        fully invisible to the user.
        """
        if not self._player_window:
            return
        try:
            x, y = int(self._player_window.x), int(self._player_window.y)
            if (x, y) != PLAYER_PARK_POS:
                self._player_onscreen_pos = (x, y)
        except Exception:
            pass
        if IS_MACOS:
            # Fades to transparent but stays ordered in. Hiding it would suspend
            # requestAnimationFrame, and Rainette's pause is a rAF-driven volume
            # ramp -- a hidden player plays on with a dead pause button.
            macos_support.park_player(self._player_window, log)
            return
        self._player_window.move(*PLAYER_PARK_POS)

    def player_toggle_pin(self):
        if not self._player_window:
            return {"enabled": self._player_on_top, "available": False, "error": "player window unavailable"}
        target = not self._player_on_top
        try:
            self._apply_player_on_top(target)
        except Exception as exc:
            log(f"player pin failed: {exc}")
            return {"enabled": self._player_on_top, "available": False, "error": str(exc)}
        self._player_on_top = target
        return {"enabled": target, "available": True}

    def _apply_player_on_top(self, enabled: bool) -> None:
        """Set WinForms TopMost on its UI thread; fall back for other backends."""
        if IS_MACOS and self._player_window:
            macos_support.apply_player_on_top(self._player_window, enabled, log)
            return
        if os.name == "nt" and self._player_window and hasattr(self._player_window, "uid"):
            try:
                from webview.platforms import winforms  # type: ignore
                from System import Action  # type: ignore
            except Exception as exc:
                raise RuntimeError(f"native always-on-top backend unavailable: {exc}") from exc
            form = winforms.BrowserView.instances.get(self._player_window.uid)
            if form is None:
                raise RuntimeError("native player form is unavailable")

            def apply():
                form.TopMost = bool(enabled)

            if form.InvokeRequired:
                form.Invoke(Action(apply))
            else:
                apply()
            return
        self._player_window.on_top = bool(enabled)

    def player_resize(self, collapsed: bool):
        self._player_collapsed = bool(collapsed)
        if self._player_window:
            height = PLAYER_SIZE[1] if collapsed else PLAYER_EXPANDED_HEIGHT
            self._player_window.resize(PLAYER_SIZE[0], height)
            self._shape_player()

    def close_player(self):
        if self._player_window:
            self._player_window.destroy()

    def _hide_player_from_taskbar(self) -> None:
        """Keep the player out of the OS window switcher.

        On Windows the player is created "shown" rather than hidden=True (see
        PLAYER_PARK_POS), so without this it would get its own taskbar button and
        Alt+Tab entry even while parked off-screen and invisible to the user.
        macOS has the same need for its Window menu and window cycling.
        """
        if IS_MACOS and self._player_window:
            macos_support.hide_player_from_window_list(self._player_window, log)
            return
        if os.name != "nt" or not self._player_window:
            return
        try:
            from webview.platforms import winforms  # type: ignore
            from System import Action  # type: ignore

            form = winforms.BrowserView.instances.get(self._player_window.uid)
            if form is not None:
                def suppress_taskbar_entry():
                    form.ShowInTaskbar = False

                # on_started() runs in pywebview's worker thread.  Accessing a
                # WinForms control directly from there can deadlock WebView2 at
                # startup, so marshal this exactly as the shape/pin helpers do.
                if form.InvokeRequired:
                    form.Invoke(Action(suppress_taskbar_entry))
                else:
                    suppress_taskbar_entry()
        except Exception as exc:
            log(f"player taskbar suppression failed: {exc}")

    def _shape_player(self):
        if IS_MACOS and self._player_window:
            macos_support.shape_player(self._player_window, self._player_collapsed, log)
            return
        if os.name != "nt" or not self._player_window:
            return
        try:
            from webview.platforms import winforms  # type: ignore
            from System import Func, IntPtr, Type  # type: ignore
            from System.Drawing import Region  # type: ignore

            form = winforms.BrowserView.instances.get(self._player_window.uid)
            if not form:
                return

            def apply_region():
                width = int(getattr(form, "Width", PLAYER_SIZE[0]))
                height = int(getattr(form, "Height", PLAYER_SIZE[1]))
                radius = height if self._player_collapsed else 44
                handle = ctypes.windll.gdi32.CreateRoundRectRgn(0, 0, width + 1, height + 1, radius, radius)
                form.Region = Region.FromHrgn(IntPtr.op_Explicit(handle))
                ctypes.windll.gdi32.DeleteObject(handle)
                # Toggling fullscreen changes DWM composition for the whole
                # desktop, and a regioned borderless window is not always
                # repainted against it. Forcing a repaint re-applies the mask
                # instead of leaving stale corners.
                try:
                    # invalidateChildren=True: the WebView2 browser is a child
                    # control with its own HWND, so invalidating just the form
                    # repaints the frame but leaves the page painted against the
                    # previous region. Update() flushes it now rather than at the
                    # next unrelated paint.
                    form.Invalidate(True)
                    form.Update()
                except Exception:
                    pass

            if form.InvokeRequired:
                form.Invoke(Func[Type](apply_region))
            else:
                apply_region()
        except Exception as exc:
            log(f"player shape failed: {exc}")


def _patch_webview2_autoplay() -> bool:
    """Deliver AUTOPLAY_FLAG to WebView2 so the player window can start audio itself.

    The player window owns the only <audio> element and receives play commands over
    the socket, so its play() never carries user activation and Chromium rejects it
    with NotAllowedError -- nothing is audible until the window is revealed. (The
    reveal is incidental: a *visible* window is refused just the same. Activation,
    not visibility, is the gate.)

    pywebview 6.2.1 hardcodes CoreWebView2CreationProperties.AdditionalBrowserArguments
    and exposes no setting for extra arguments, and WebView2 ignores
    WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS once that property is set -- so the env var
    exported below is dead on arrival. The properties are consumed when the browser's
    handle is created, which pywebview does inside EdgeChrome.__init__ itself, so
    there is no post-construction window in which to append the flag.

    Rewrite the string constant pywebview assigns, so its own assignment already
    carries the flag. Deliberately narrow: if pywebview ever changes that literal the
    constant is not found, the patch disables itself, and playback merely reverts to
    today's behaviour rather than breaking startup.
    """
    try:
        from webview.platforms import edgechromium  # type: ignore
    except Exception as exc:
        log(f"webview2 autoplay patch unavailable: {exc}")
        return False
    init = getattr(edgechromium.EdgeChrome, "__init__", None)
    code = getattr(init, "__code__", None)
    if code is None:
        log("webview2 autoplay patch skipped: unexpected EdgeChrome.__init__")
        return False
    if getattr(init, "_rainette_autoplay_patched", False):
        return True
    if not any(c == PYWEBVIEW_BROWSER_ARGS for c in code.co_consts):
        log("webview2 autoplay patch skipped: pywebview's browser arguments changed")
        return False
    init.__code__ = code.replace(co_consts=tuple(
        f"{c} {AUTOPLAY_FLAG}" if c == PYWEBVIEW_BROWSER_ARGS else c
        for c in code.co_consts
    ))
    init._rainette_autoplay_patched = True
    return True


def _enable_gestureless_playback() -> bool:
    """Make sure the player window may start audio without a user gesture.

    The player owns the only <audio> element and is driven over the socket, so
    its play() never carries user activation. Each engine gates that
    differently: WebView2 needs AUTOPLAY_FLAG injected (see
    :func:`_patch_webview2_autoplay`), while WKWebView already permits it and
    only needs verifying. Returns True when gesture-less playback is expected to
    work.
    """
    if IS_MACOS:
        return macos_support.autoplay_needs_no_patch(log)
    return _patch_webview2_autoplay()


def _player_placement_kwargs() -> dict:
    """Where and how the player window is first created.

    Windows parks it far off-screen while leaving it "shown", because a hidden
    or minimized WebView2 window reports document.hidden and Chromium then
    throttles media loading indefinitely (see PLAYER_PARK_POS).

    macOS must not use that coordinate at all: Cocoa finds no screen for it and
    pywebview crashes creating the window. It needs the same *effect* though --
    an ordered-out window there suspends requestAnimationFrame and throttles
    timers to ~1Hz, which breaks pause (see macos_support.park_player). So the
    player starts hidden purely to avoid a flash on launch, at an ordinary
    on-screen position, and is immediately faded to transparent-but-shown.
    """
    if IS_MACOS:
        return {
            "x": PLAYER_MACOS_INITIAL_POS[0],
            "y": PLAYER_MACOS_INITIAL_POS[1],
            "hidden": True,
        }
    return {"x": PLAYER_PARK_POS[0], "y": PLAYER_PARK_POS[1]}


def _try_pywebview(url: str) -> bool:
    try:
        import webview  # type: ignore
    except Exception as exc:
        log(f"pywebview import failed: {exc}")
        return False
    try:
        _enable_high_dpi()
        _set_app_user_model_id()
        # Kept for WebView2 hosts / future pywebview versions that honour it. The
        # bundled pywebview does not, so the patch below is what actually delivers
        # the flag today.
        os.environ.setdefault("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", AUTOPLAY_FLAG)
        _enable_gestureless_playback()
        api = WindowApi()
        token = urllib.parse.quote(server.APP_TOKEN, safe="")
        main_window = webview.create_window(
            WINDOW_TITLE, f"{url}?remote=1&token={token}", js_api=api,
            width=WINDOW_SIZE[0], height=WINDOW_SIZE[1], min_size=MIN_SIZE,
        )
        player_window = webview.create_window(
            PLAYER_WINDOW_TITLE, f"{url}miniplayer.html?token={token}", js_api=api,
            width=PLAYER_SIZE[0], height=PLAYER_SIZE[1], min_size=PLAYER_SIZE,
            frameless=True, easy_drag=False, resizable=False,
            shadow=False, background_color="#FFFFFF",
            **_player_placement_kwargs(),
        )
        api.bind_main(main_window)
        api.bind_player(player_window)
        # ShowInTaskbar recreates/adjusts the native form handle.  Doing that
        # from pywebview's global ``on_started`` callback is still too early for
        # the second WebView2 controller and can abort its initialization with
        # E_ABORT.  ``loaded`` is the first lifecycle point at which the player
        # controller is fully initialized; the helper itself then marshals the
        # WinForms property write onto the UI thread.
        player_window.events.loaded += lambda *_: api._hide_player_from_taskbar()
        # The parked player is still a real WinForms window. Tear it down while
        # the main form is closing, not from ``closed``: pywebview dispatches
        # ``closed`` handlers on a worker thread after WinForms has begun
        # disposing the main form, which can leave the off-screen player alive
        # with no visible application window and keep pythonw running forever.
        main_window.events.closing += api.close_player
        try:
            # Best-effort mitigation for the "cutout corners" glitch: re-apply
            # the player window's rounded-corner region whenever the main
            # window resizes (fullscreen/maximize toggles surface as a resize
            # here), since that's when the underlying DWM composition change
            # is most likely to leave the player's region mask stale.
            main_window.events.resized += lambda *_: api._shape_player()
        except Exception as exc:
            log(f"could not hook main window resize event: {exc}")

        def on_started():
            api.player_hide()
            api._shape_player()
            if IS_MACOS:
                # A python process launched from a terminal starts as a
                # background app, which would open the main window behind
                # whatever the user was already looking at.
                macos_support.activate_app(log)

        # icon= is honored by the winforms (Windows) backend too, despite the
        # docstring saying GTK/QT only - it sets each form's .Icon from
        # _state['icon'] (see webview/platforms/winforms.py).
        start_kwargs = {}
        icon = _window_icon_path()
        if icon is not None:
            start_kwargs["icon"] = str(icon)
        webview.start(on_started, **start_kwargs)   # blocks until the window is closed
        return True
    except Exception:
        log("pywebview window crashed:\n" + traceback.format_exc())
        return False


#: Chromium browsers that support --app, in preference order. Any of them gives
#: the fallback a real app window rather than a tab in the user's main browser.
_MACOS_CHROMIUM_APPS = (
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
)


def _find_edge() -> str | None:
    """A Chromium browser that can host the UI in --app mode, if one exists."""
    for command in ("msedge", "google-chrome", "chromium"):
        found = shutil.which(command)
        if found:
            return found
    if IS_MACOS:
        return next((path for path in _MACOS_CHROMIUM_APPS if os.path.isfile(path)), None)
    for path in (
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%LocalAppData%\Microsoft\Edge\Application\msedge.exe"),
    ):
        if path and os.path.isfile(path):
            return path
    return None


def _try_edge_app(url: str) -> bool:
    edge = _find_edge()
    if not edge:
        return False
    # Keep the throwaway profile beside the app's own data instead of guessing at
    # a Windows-shaped environment variable that is unset everywhere else.
    profile = str(APP_DATA_DIR / "edge-profile")
    try:
        subprocess.Popen([edge, f"--app={url}", f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check"])
        return True
    except Exception as exc:
        log(f"edge --app failed: {exc}")
        return False


def _restore_tunnel() -> None:
    """Reopen the HTTPS tunnel on launch when Rainette was the one running it.

    A Cloudflare Quick Tunnel address does not survive a restart, so a paired
    phone would otherwise point at a hostname that stopped existing when the
    computer was last shut down.  Bringing it back up here — and only when the
    stored address is one Rainette minted — means the operator's own named
    tunnel or reverse proxy is never touched.
    """
    if not server.is_managed_tunnel_url(server.read_pwa_config().get("public_url") or ""):
        return
    try:
        server.start_tunnel()
        log("reopening the managed HTTPS tunnel for paired phones")
    except Exception:
        log("managed tunnel failed to reopen:\n" + traceback.format_exc())


def _shutdown_tunnel() -> None:
    """Stop cloudflared so it does not outlive the app that started it."""
    try:
        server.tunnel_manager().stop(timeout_s=5.0)
    except Exception:
        pass


def main() -> int:
    try:
        port = server.start()
    except Exception:
        log("server failed to start:\n" + traceback.format_exc())
        print("Failed to start the Rainette Music server (see rainette-music.log).", file=sys.stderr)
        return 1

    # Paired phones store a certificate-pinned LAN endpoint.  Restore that
    # listener on every desktop launch instead of waiting for the user to open
    # Settings and generate another pairing code.  A companion-specific bind
    # failure must not prevent the desktop player itself from opening.
    try:
        companion_runtime = server.start_paired_companion()
        if companion_runtime:
            log(f"companion listener restored on port {companion_runtime['port']}")
            _restore_tunnel()
    except Exception:
        log("paired companion listener failed to restart:\n" + traceback.format_exc())

    atexit.register(_shutdown_tunnel)

    url = f"http://127.0.0.1:{port}/"
    log(f"server up at {url}")
    print(f"Rainette Music running at {url}")

    # 1. Native window (blocks until closed).
    if _try_pywebview(url):
        return 0

    # 2/3. Fallback keeps the (daemon) server alive until this process is killed.
    launch_url = f"{url}?token={urllib.parse.quote(server.APP_TOKEN, safe='')}"
    if not _try_edge_app(launch_url):
        webbrowser.open(launch_url)
    print("Music window opened in fallback mode. Close this window to stop Rainette Music.")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        log("fatal:\n" + traceback.format_exc())
        raise
