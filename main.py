"""Rainette Music — app entry point.

Starts the local server on a background thread, then opens the UI as a single
native desktop window (pywebview / WebView2), falling back to an Edge --app
window or the default browser. The player is the original in-page mini-player
bubble, which stays hidden until a track is played.

Any startup crash is written to `rainette-music.log` next to this file.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import traceback
import webbrowser
import ctypes
from datetime import datetime
from pathlib import Path

import server

WINDOW_TITLE = "Rainette Music"
WINDOW_SIZE = (1060, 730)
MIN_SIZE = (780, 560)
PLAYER_SIZE = (300, 68)
PLAYER_EXPANDED_HEIGHT = 184
PLAYER_EQ_HEIGHT = 340

LOG_PATH = Path(__file__).resolve().parent / "rainette-music.log"


def log(msg: str) -> None:
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


class WindowApi:
    def __init__(self) -> None:
        self._player_window = None
        self._player_on_top = False
        self._player_can_show = False
        self._player_collapsed = True
        self._player_eq_expanded = False

    def bind_player(self, player_window) -> None:
        self._player_window = player_window

    def player_allow_show(self):
        self._player_can_show = True

    def show_player(self):
        if self._player_window and self._player_can_show:
            self._shape_player()
            self._player_window.show()
            self._player_window.restore()

    def player_hide(self):
        if self._player_window:
            self._player_window.hide()

    def player_minimize(self):
        if self._player_window:
            self._player_window.minimize()

    def player_toggle_pin(self):
        self._player_on_top = not self._player_on_top
        if self._player_window:
            self._player_window.on_top = self._player_on_top
        return self._player_on_top

    def player_resize(self, collapsed: bool, eq_expanded: bool):
        self._player_collapsed = bool(collapsed)
        self._player_eq_expanded = bool(eq_expanded)
        if self._player_window:
            height = PLAYER_SIZE[1] if collapsed else (PLAYER_EQ_HEIGHT if eq_expanded else PLAYER_EXPANDED_HEIGHT)
            self._player_window.resize(PLAYER_SIZE[0], height)
            self._shape_player()

    def player_resize_eq(self, expanded: bool):
        self._player_collapsed = False
        self._player_eq_expanded = bool(expanded)
        if self._player_window:
            self._player_window.resize(PLAYER_SIZE[0], PLAYER_EQ_HEIGHT if expanded else PLAYER_EXPANDED_HEIGHT)
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
        main_window = webview.create_window(
            WINDOW_TITLE, f"{url}?remote=1", js_api=api,
            width=WINDOW_SIZE[0], height=WINDOW_SIZE[1], min_size=MIN_SIZE,
        )
        player_window = webview.create_window(
            "Player", f"{url}miniplayer.html", js_api=api,
            width=PLAYER_SIZE[0], height=PLAYER_SIZE[1], min_size=PLAYER_SIZE,
            hidden=True, frameless=True, easy_drag=False, resizable=False,
            shadow=False, background_color="#FFFFFF",
        )
        api.bind_player(player_window)
        main_window.events.closed += api.close_player

        def on_started():
            api.player_hide()
            api._shape_player()

        webview.start(on_started)   # blocks until the window is closed
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
    if not _try_edge_app(url):
        webbrowser.open(url)
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
