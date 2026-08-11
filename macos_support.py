"""macOS (Cocoa / WKWebView) desktop integration for Rainette Music.

Everything Rainette needs from AppKit lives here so ``main.py`` can describe its
windows once and let each platform supply the native behaviour.  The Windows
equivalents are the WinForms/ctypes calls inline in ``main.py``; this module is
their Cocoa counterpart and is imported only on ``sys.platform == "darwin"``.

Three Windows workarounds do **not** carry over, and knowing why is the whole
point of this file.  Each conclusion below was measured on this platform rather
than assumed, because guessing at them is what made the first port silent.

1. **Autoplay needs no patch.**  On Windows the player window's gesture-less
   ``audio.play()`` is unblocked by handing WebView2
   ``--autoplay-policy=no-user-gesture-required``.  WKWebView has no command
   line, and its equivalent switch —
   ``WKWebViewConfiguration.mediaTypesRequiringUserActionForPlayback`` — already
   defaults to ``WKAudiovisualMediaTypeNone`` on macOS (it is
   ``WKAudiovisualMediaTypeAll`` only on iOS).  A gesture-less ``play()`` of
   *audible* media in a pywebview window was measured to start and keep running,
   so there is nothing to patch.  See :func:`autoplay_needs_no_patch`.

2. **The off-screen park is actively harmful.**  Windows parks the player at
   ``(-32000, -32000)`` so it stays "shown" and Chromium does not throttle media
   loading for a ``document.hidden`` page.  On macOS that coordinate *crashes
   window creation*: Cocoa reports no containing screen, and pywebview's
   ``windowDidMove_`` dereferences ``window.screen()`` unconditionally
   (``webview/platforms/cocoa.py``), raising ``AttributeError`` before the window
   ever appears.

3. **Hiding the player is NOT safe** -- but for a different reason than on
   Windows.  Audio does keep decoding in an ordered-out window, which makes
   ``hide()`` look fine if you only watch ``currentTime``.  What actually breaks
   is everything driven by the clock: an ordered-out window sets
   ``document.hidden``, and WebKit then suspends ``requestAnimationFrame``
   completely and throttles timers to about 1 Hz.  Rainette's pause is a volume
   ramp whose completion callback calls ``audio.pause()``, so in a hidden window
   the pause button does nothing while the track plays on.  The player is
   therefore kept ordered in and faded to fully transparent instead.  See
   :func:`park_player` for the measurements.

Every helper is best-effort: it reports failure through the caller's logger
rather than raising, because a cosmetic native tweak must never stop the app
from opening.  AppKit mutation is marshalled to the main thread — pywebview runs
lifecycle callbacks on worker threads, and touching AppKit from one crashes the
process with SIGTRAP.
"""

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
    """Report whether WKWebView will start gesture-less audio unaided.

    The counterpart to ``main._patch_webview2_autoplay``.  Rather than patching
    anything, this confirms the assumption the player window depends on — that
    ``mediaTypesRequiringUserActionForPlayback`` is ``WKAudiovisualMediaTypeNone``
    (0) — and logs loudly if a future macOS or pyobjc build ever changes it.  A
    False return means audio may be blocked until the user clicks something, and
    is a signal to revisit this module rather than a reason to fail startup.
    """
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
    """The AppKit ``NSWindow`` behind a pywebview window, once it exists.

    pywebview assigns ``.native`` while constructing the browser view, so this
    returns None for any call that lands before the window is realised.
    """
    return getattr(window, "native", None)


def _on_main_thread(action: Callable[[], None]) -> None:
    """Run an AppKit call on the main thread.

    pywebview invokes lifecycle callbacks from worker threads, and mutating an
    NSWindow from one terminates the process with SIGTRAP -- measured, not
    theoretical.
    """
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
    """Give the frameless player window rounded corners.

    The Windows build masks the form with a GDI round-rect region; the Cocoa
    equivalent is a corner radius on the content view's backing layer, with the
    window made non-opaque so the area outside the radius is genuinely clear
    rather than painted black.
    """
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
            log(f"player shape failed: {exc}")

    _on_main_thread(apply_shape)


def hide_player_from_window_list(window: Any, log: Logger) -> None:
    """Keep the player out of the Window menu and window cycling.

    The macOS counterpart of suppressing the Windows taskbar button: the player
    is an accessory surface, not a document the user should be able to summon
    from the app's window list.
    """
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
            log(f"player pin failed: {exc}")

    _on_main_thread(set_level)


def park_player(window: Any, log: Logger) -> None:
    """Make the player invisible while keeping its page fully alive.

    **Do not replace this with ``hide()``.** An ordered-out window sets
    ``document.hidden``, and WebKit then suspends ``requestAnimationFrame``
    entirely and throttles timers to roughly 1 Hz.  Measured on this platform:

        state      document.hidden   rAF ticks/3s   setInterval ticks/3s
        visible    false             180            29
        hidden     true                0             3
        alpha 0    false             181            29

    That breaks far more than animation.  Rainette implements pause as a ~180 ms
    volume ramp whose *completion callback* calls ``audio.pause()``; without the
    EQ graph that ramp is driven by ``requestAnimationFrame``.  In a hidden
    window the ramp never advances, the callback never fires, and the pause
    button does nothing at all while the track keeps playing.

    Fading the window to fully transparent keeps it ordered in -- so
    ``document.hidden`` stays false and every timer keeps running -- while
    nothing is drawn.  Mouse events are disabled too, so the invisible window
    cannot swallow clicks meant for whatever is behind it.

    This is the same conclusion the Windows build reached for its own engine's
    version of the problem; only the mechanism differs (it parks off-screen,
    which Cocoa will not honour -- see the module docstring).
    """
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
    """Bring Rainette to the front and give it a real Dock presence.

    A Python process launched from a terminal starts as a background
    application, so without this the main window can open behind whatever the
    user was already looking at.
    """
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
