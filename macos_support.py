"""macOS (Cocoa / WKWebView) desktop integration for Rainette Music."""

from __future__ import annotations

import sys
from typing import Any, Callable

Logger = Callable[[str], None]

# Corner radius of the expanded player, in points.  The collapsed player is a
# pill, so its radius is derived from its own height instead.
_EXPANDED_CORNER_RADIUS = 22.0


def is_supported() -> bool:
    """True when this module's AppKit/WebKit dependencies can actually be used."""
    if sys.platform != "darwin":
        return False
    try:
        import AppKit  # type: ignore  # noqa: F401
        import WebKit  # type: ignore  # noqa: F401
    except Exception:
        return False
    return True


def autoplay_needs_no_patch(log: Logger) -> bool:
    """Report whether WKWebView will start gesture-less audio unaided."""
    if not is_supported():
        return False
    try:
        import WebKit  # type: ignore

        config = WebKit.WKWebViewConfiguration.alloc().init()
        policy = int(config.mediaTypesRequiringUserActionForPlayback())
    except Exception as exc:
        log(f"could not read the WKWebView media playback policy: {exc}")
        return False
    if policy != 0:
        log(
            "WKWebView now requires a user gesture for media playback "
            f"(mediaTypesRequiringUserActionForPlayback={policy}); the player "
            "window may stay silent until it is clicked"
        )
        return False
    return True


# ── Native window helpers ───────────────────────────────────────────────────


def _ns_window(window: Any) -> Any | None:
    """The AppKit ``NSWindow`` behind a pywebview window, once it exists."""
    try:
        return getattr(window, "native", None)
    except Exception:
        return None


_warned: set[str] = set()


def _warn_once(log: Callable[[str], None], key: str, message: str) -> None:
    """Log a persistent platform limitation once, not once per window call."""
    if key in _warned:
        return
    _warned.add(key)
    log(message)


def _on_main_thread(action: Callable[[], None]) -> None:
    """Run an AppKit call on the main thread."""
    try:
        import Foundation  # type: ignore
        from PyObjCTools import AppHelper  # type: ignore
    except Exception:
        action()
        return
    try:
        if Foundation.NSThread.isMainThread():
            action()
            return
    except Exception:
        pass
    AppHelper.callAfter(action)


def shape_player(window: Any, collapsed: bool, log: Logger) -> None:
    """Give the frameless player window rounded corners."""
    native = _ns_window(window)
    if native is None:
        return

    def apply_shape() -> None:
        try:
            import AppKit  # type: ignore

            content = native.contentView()
            if content is None:
                return
            height = float(content.frame().size.height)
            radius = (height / 2.0) if collapsed else _EXPANDED_CORNER_RADIUS
            native.setOpaque_(False)
            native.setBackgroundColor_(AppKit.NSColor.clearColor())
            content.setWantsLayer_(True)
            layer = content.layer()
            if layer is not None:
                layer.setCornerRadius_(radius)
                layer.setMasksToBounds_(True)
        except Exception as exc:
            _warn_once(log, "shape", f"player shape failed: {exc}")

    _on_main_thread(apply_shape)


def hide_player_from_window_list(window: Any, log: Logger) -> None:
    """Keep the player out of the Window menu and window cycling."""
    native = _ns_window(window)
    if native is None:
        return

    def suppress() -> None:
        try:
            import AppKit  # type: ignore

            native.setExcludedFromWindowsMenu_(True)
            native.setCollectionBehavior_(
                AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
                | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary
                | AppKit.NSWindowCollectionBehaviorIgnoresCycle
            )
        except Exception as exc:
            log(f"player window-list suppression failed: {exc}")

    _on_main_thread(suppress)


def apply_player_on_top(window: Any, enabled: bool, log: Logger) -> None:
    """Pin or unpin the player above other applications' windows."""
    native = _ns_window(window)
    if native is None:
        raise RuntimeError("native player window is unavailable")

    def set_level() -> None:
        try:
            import AppKit  # type: ignore

            native.setLevel_(
                AppKit.NSStatusWindowLevel if enabled else AppKit.NSNormalWindowLevel
            )
        except Exception as exc:
            _warn_once(log, "pin", f"player pin failed: {exc}")

    _on_main_thread(set_level)


def park_player(window: Any, log: Logger) -> None:
    """Make the player invisible while keeping its page fully alive."""
    native = _ns_window(window)
    if native is None:
        return

    def fade_out() -> None:
        try:
            native.setAlphaValue_(0.0)
            native.setIgnoresMouseEvents_(True)
            # Ordered in, but invisible: this is what keeps the page unthrottled.
            # Setting alpha first means ordering it in causes no visible flash.
            if not native.isVisible():
                native.orderFront_(None)
        except Exception as exc:
            log(f"player park failed: {exc}")

    _on_main_thread(fade_out)


def unpark_player(window: Any, log: Logger) -> None:
    """Reverse :func:`park_player`: make the player visible and clickable."""
    native = _ns_window(window)
    if native is None:
        return

    def fade_in() -> None:
        try:
            native.setAlphaValue_(1.0)
            native.setIgnoresMouseEvents_(False)
            native.orderFront_(None)
        except Exception as exc:
            log(f"player reveal failed: {exc}")

    _on_main_thread(fade_in)


def activate_app(log: Logger) -> None:
    """Bring Rainette to the front and give it a real Dock presence."""
    if not is_supported():
        return

    def activate() -> None:
        try:
            import AppKit  # type: ignore

            app = AppKit.NSApplication.sharedApplication()
            app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyRegular)
            app.activateIgnoringOtherApps_(True)
        except Exception as exc:
            log(f"app activation failed: {exc}")

    _on_main_thread(activate)
