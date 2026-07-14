"""Rainette Music — app entry point.

Starts the local server on a background thread, then opens the UI as a single
native desktop window (pywebview / WebView2), falling back to an Edge --app
window or the default browser. The player is the original in-page mini-player
bubble, which stays hidden until a track is played.

Any startup crash is written to `rainette-music.log` in the user's local
Rainette Music application-data directory.
"""

from __future__ import annotations

import base64
import io
import os
import shutil
import subprocess
import sys
import threading
import traceback
import urllib.parse
import urllib.request
import urllib.error
import webbrowser
import ctypes
from datetime import datetime
from pathlib import Path

import qrcode

import server

WINDOW_TITLE = "Rainette Music"
WINDOW_SIZE = (1060, 730)
MIN_SIZE = (780, 560)
PLAYER_SIZE = (300, 60)
PLAYER_EXPANDED_HEIGHT = 184

APP_DATA_DIR = Path(os.environ.get("LOCALAPPDATA") or Path(__file__).resolve().parent) / "Rainette Music"
LOG_PATH = APP_DATA_DIR / "rainette-music.log"
# PyInstaller exposes bundled data under _MEIPASS.  Source runs continue to use
# the repository root, while a self-contained --onedir build uses its internal
# resource directory instead of relying on Python being installed on PATH.
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
ICON_PATH = RESOURCE_DIR / "web" / "assets" / "rainette-icon.ico"
ANDROID_APK_URL = (
    "https://github.com/Krysis-ux/Rainette-music/releases/latest/download/"
    "rainette-music-android.apk"
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


def _android_release_status(url: str) -> str:
    """Distinguish an absent release from a failed availability check."""
    try:
        request = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(request, timeout=3) as response:
            return "published" if 200 <= int(response.status) < 400 else "unavailable"
    except urllib.error.HTTPError as exc:
        return "unavailable" if exc.code == 404 else "check_failed"
    except Exception:
        return "check_failed"


def _is_url_published(url: str) -> bool:
    """Backward-compatible boolean publication helper."""
    return _android_release_status(url) == "published"


class WindowApi:
    def __init__(self) -> None:
        self._main_window = None
        self._player_window = None
        self._player_on_top = False
        self._player_can_show = False
        self._player_collapsed = True

    def bind_player(self, player_window) -> None:
        self._player_window = player_window

    def bind_main(self, main_window) -> None:
        self._main_window = main_window

    def companion_create_invitation(self):
        """Expose explicit desktop-controlled pairing to the settings UI."""
        try:
            invitation = server.create_companion_invitation()
            pairing_uri = "rainette://pair?" + urllib.parse.urlencode({
                "endpoint": invitation["endpoint"],
                "certificate_sha256": invitation["certificate_sha256"],
                "invitation": invitation["invitation"],
            })
            return {
                "ok": True,
                "pairing_uri": pairing_uri,
                "pairing_qr_data_url": _qr_data_url(pairing_uri),
                "expires_at": invitation["expires_at"],
            }
        except Exception as exc:
            log(f"companion invitation failed: {exc}")
            return {"ok": False, "msg": str(exc)}

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

    def android_download_info(self):
        status = _android_release_status(ANDROID_APK_URL)
        return {
            "url": ANDROID_APK_URL,
            "install_qr_data_url": _qr_data_url(ANDROID_APK_URL),
            "status": status,
            "published": status == "published",
        }

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
        if self._player_window and self._player_can_show:
            self._shape_player()
            self._player_window.show()
            self._player_window.restore()

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
        if self._player_window:
            self._player_window.hide()

    def player_minimize(self):
        if self._player_window:
            self._player_window.minimize()

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

    def _shape_player(self):
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
                # Best-effort fix for the reported "cutout corners" glitch: when
                # the main window toggles fullscreen, Windows changes DWM
                # composition mode for the whole desktop, and a regioned
                # borderless window doesn't always get repainted against the
                # new composition automatically. Forcing an immediate repaint
                # here re-applies the mask instead of leaving stale corners
                # until the next unrelated paint event.
                try:
                    form.Invalidate()
                except Exception:
                    pass

            if form.InvokeRequired:
                form.Invoke(Func[Type](apply_region))
            else:
                apply_region()
        except Exception as exc:
            log(f"player shape failed: {exc}")


def _try_pywebview(url: str) -> bool:
    try:
        import webview  # type: ignore
    except Exception as exc:
        log(f"pywebview import failed: {exc}")
        return False
    try:
        _enable_high_dpi()
        os.environ.setdefault("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", "--autoplay-policy=no-user-gesture-required")
        api = WindowApi()
        token = urllib.parse.quote(server.APP_TOKEN, safe="")
        main_window = webview.create_window(
            WINDOW_TITLE, f"{url}?remote=1&token={token}", js_api=api,
            width=WINDOW_SIZE[0], height=WINDOW_SIZE[1], min_size=MIN_SIZE,
        )
        player_window = webview.create_window(
            "Player", f"{url}miniplayer.html?token={token}", js_api=api,
            width=PLAYER_SIZE[0], height=PLAYER_SIZE[1], min_size=PLAYER_SIZE,
            hidden=True, frameless=True, easy_drag=False, resizable=False,
            shadow=False, background_color="#FFFFFF",
        )
        api.bind_main(main_window)
        api.bind_player(player_window)
        main_window.events.closed += api.close_player
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

        # icon= is honored by the winforms (Windows) backend too, despite the
        # docstring saying GTK/QT only - it sets each form's .Icon from
        # _state['icon'] (see webview/platforms/winforms.py).
        start_kwargs = {}
        if ICON_PATH.is_file():
            start_kwargs["icon"] = str(ICON_PATH)
        webview.start(on_started, **start_kwargs)   # blocks until the window is closed
        return True
    except Exception:
        log("pywebview window crashed:\n" + traceback.format_exc())
        return False


def _find_edge() -> str | None:
    found = shutil.which("msedge")
    if found:
        return found
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
    profile = os.path.join(os.environ.get("LocalAppData", os.getcwd()), "RainetteMusic", "edge-profile")
    try:
        subprocess.Popen([edge, f"--app={url}", f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check"])
        return True
    except Exception as exc:
        log(f"edge --app failed: {exc}")
        return False


def main() -> int:
    try:
        port = server.start()
    except Exception:
        log("server failed to start:\n" + traceback.format_exc())
        print("Failed to start the Rainette Music server (see rainette-music.log).", file=sys.stderr)
        return 1

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
