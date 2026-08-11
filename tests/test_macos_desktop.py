"""The macOS desktop branch of main.py, and the Cocoa helpers behind it.

Split in two on purpose:

* The branching tests run **everywhere**, including the Windows CI box. They
  drive ``main``'s macOS path with a stand-in ``macos_support``, so a change that
  breaks the port is caught by the same suite that guards the Windows build
  rather than only when somebody happens to run it on a Mac.
* The Cocoa tests are skipped off macOS, because they need real AppKit.

The regression these exist for: the first port produced no sound at all. The
cause was not the audio pipeline but window creation -- ``PLAYER_PARK_POS``
(-32000, -32000) leaves a window on no screen, Cocoa returns nil for
``window.screen()``, and pywebview's ``windowDidMove_`` dereferences it. The
resulting exception dropped the app into browser fallback, where the mini-player
window architecture does not exist. Hence ``test_player_is_never_created_at_the_windows_park_position``.
"""

from __future__ import annotations

import sys
import types
import unittest
from unittest.mock import patch

import main
import server

IS_MACOS_HOST = sys.platform == "darwin"


def _fake_macos_support():
    """A recording stand-in for the real Cocoa module."""
    calls: list[tuple] = []
    module = types.SimpleNamespace(
        calls=calls,
        autoplay_needs_no_patch=lambda log: calls.append(("autoplay",)) or True,
        park_player=lambda window, log: calls.append(("park", window)),
        unpark_player=lambda window, log: calls.append(("unpark", window)),
        shape_player=lambda window, collapsed, log: calls.append(("shape", collapsed)),
        hide_player_from_window_list=lambda window, log: calls.append(("window_list",)),
        apply_player_on_top=lambda window, enabled, log: calls.append(("on_top", enabled)),
        activate_app=lambda log: calls.append(("activate",)),
    )
    return module


def _as_macos(test_case):
    """Run the body as though this were a macOS host, with a fake Cocoa layer."""
    fake = _fake_macos_support()
    for patcher in (
        patch.object(main, "IS_MACOS", True),
        patch.object(main, "IS_WINDOWS", False),
        patch.object(main, "macos_support", fake),
    ):
        patcher.start()
        test_case.addCleanup(patcher.stop)
    return fake


class _FakeNativeWindow:
    """Stands in for the AppKit NSWindow behind a pywebview window."""

    def __init__(self, visible=False):
        self.alpha = 1.0
        self.ignores_mouse = False
        self.ordered_front = False
        self._visible = visible

    def setAlphaValue_(self, value):
        self.alpha = float(value)

    def setIgnoresMouseEvents_(self, value):
        self.ignores_mouse = bool(value)

    def isVisible(self):
        return self._visible

    def orderFront_(self, _sender):
        self.ordered_front = True
        self._visible = True


class _RecordingWindow:
    def __init__(self, x=200, y=300):
        self.calls = []
        self.x, self.y = x, y
        self.on_top = False

    def show(self):
        self.calls.append("show")

    def hide(self):
        self.calls.append("hide")

    def restore(self):
        self.calls.append("restore")

    def move(self, x, y):
        self.calls.append(("move", x, y))
        self.x, self.y = x, y

    def resize(self, width, height):
        self.calls.append(("resize", width, height))


class PlayerPlacementTests(unittest.TestCase):
    """Where the player window is created -- the crash that silenced the port."""

    def test_player_is_never_created_at_the_windows_park_position(self):
        _as_macos(self)
        placement = main._player_placement_kwargs()
        self.assertNotEqual(
            (placement["x"], placement["y"]),
            main.PLAYER_PARK_POS,
            "the off-screen park coordinate leaves the window on no screen, and "
            "pywebview's Cocoa windowDidMove_ dereferences window.screen() -- "
            "creating the player there raises before it is ever shown",
        )

    def test_macos_player_is_created_hidden_at_an_onscreen_position(self):
        _as_macos(self)
        placement = main._player_placement_kwargs()
        self.assertTrue(placement["hidden"])
        self.assertEqual((placement["x"], placement["y"]), main.PLAYER_MACOS_INITIAL_POS)
        self.assertGreaterEqual(placement["x"], 0)
        self.assertGreaterEqual(placement["y"], 0)

    def test_windows_placement_is_unchanged(self):
        with patch.object(main, "IS_MACOS", False):
            placement = main._player_placement_kwargs()
        self.assertEqual((placement["x"], placement["y"]), main.PLAYER_PARK_POS)
        self.assertNotIn("hidden", placement)


class PlayerParkingTests(unittest.TestCase):
    """macOS hides the player instead of moving it off-screen."""

    def setUp(self):
        self.fake = _as_macos(self)
        self.api = main.WindowApi()
        self.player = _RecordingWindow()
        self.api.bind_player(self.player)

    def test_park_hides_rather_than_moving_offscreen(self):
        self.api.player_hide()
        self.assertIn(("park", self.player), self.fake.calls)
        self.assertNotIn(("move", *main.PLAYER_PARK_POS), self.player.calls)

    def test_minimize_also_parks(self):
        self.api.player_minimize()
        self.assertIn(("park", self.player), self.fake.calls)

    def test_park_remembers_where_the_user_left_the_player(self):
        self.player.x, self.player.y = 640, 480
        self.api.player_hide()
        self.assertEqual(self.api._player_onscreen_pos, (640, 480))

    def test_reveal_restores_position_then_unparks_then_shapes(self):
        self.api._player_onscreen_pos = (640, 480)
        self.api.reveal_player()
        self.assertEqual(self.player.calls[0], ("move", 640, 480))
        kinds = [call[0] for call in self.fake.calls]
        self.assertEqual(kinds.index("unpark") < kinds.index("shape"), True,
                         "the window must be on screen before it is shaped")

    def test_show_player_stays_a_noop_until_allowed(self):
        self.api.show_player()
        self.assertEqual(self.player.calls, [])
        self.assertEqual(self.fake.calls, [])


class PlayerNativeChromeTests(unittest.TestCase):
    def setUp(self):
        self.fake = _as_macos(self)
        self.api = main.WindowApi()
        self.player = _RecordingWindow()
        self.api.bind_player(self.player)

    def test_pin_delegates_to_the_cocoa_layer_and_reports_success(self):
        result = self.api.player_toggle_pin()
        self.assertEqual(result, {"enabled": True, "available": True})
        self.assertIn(("on_top", True), self.fake.calls)

    def test_shape_follows_the_collapsed_state(self):
        self.api.player_resize(collapsed=False)
        self.assertIn(("shape", False), self.fake.calls)
        self.api.player_resize(collapsed=True)
        self.assertIn(("shape", True), self.fake.calls)

    def test_window_list_suppression_uses_the_cocoa_layer(self):
        self.api._hide_player_from_taskbar()
        self.assertIn(("window_list",), self.fake.calls)


class GesturelessPlaybackTests(unittest.TestCase):
    """The player is driven over the socket, so play() never carries activation."""

    def test_macos_verifies_rather_than_patches(self):
        fake = _as_macos(self)
        self.assertTrue(main._enable_gestureless_playback())
        self.assertIn(("autoplay",), fake.calls)

    def test_windows_still_patches_webview2(self):
        with patch.object(main, "IS_MACOS", False), \
             patch.object(main, "_patch_webview2_autoplay", return_value=True) as patched:
            self.assertTrue(main._enable_gestureless_playback())
        patched.assert_called_once()


class AppDataLocationTests(unittest.TestCase):
    def test_main_and_server_agree_on_one_writable_directory(self):
        # These diverged off Windows: main.py fell back to LOCALAPPDATA (unset)
        # and logged beside the source tree while server.py used the real
        # platform directory -- two processes, two databases, one confusing bug.
        self.assertEqual(main.APP_DATA_DIR, server.APP_DATA_DIR)
        self.assertEqual(main.LOG_PATH.parent, server.APP_DATA_DIR)

    @unittest.skipUnless(IS_MACOS_HOST, "macOS layout")
    def test_macos_uses_application_support(self):
        self.assertEqual(
            main.APP_DATA_DIR,
            server.Path.home() / "Library" / "Application Support" / "Rainette Music",
        )


class WindowIconTests(unittest.TestCase):
    def test_macos_never_offers_appkit_a_windows_ico(self):
        # NSImage reads .icns and .png, not .ico; handing it one leaves the app
        # with the bare Python rocket in the Dock.
        with patch.object(main, "IS_MACOS", True):
            icon = main._window_icon_path()
        if icon is not None:
            self.assertNotEqual(icon.suffix.lower(), ".ico")
            self.assertIn(icon.suffix.lower(), {".icns", ".png"})

    def test_windows_still_uses_the_ico(self):
        with patch.object(main, "IS_MACOS", False):
            icon = main._window_icon_path()
        if icon is not None:
            self.assertEqual(icon.suffix.lower(), ".ico")


class UpdaterPlatformGateTests(unittest.TestCase):
    def test_non_windows_refuses_to_install_the_windows_installer(self):
        api = main.WindowApi()
        with patch.object(main.sys, "frozen", True, create=True), \
             patch.object(main, "IS_WINDOWS", False), \
             patch.object(main.urllib.request, "urlopen",
                          side_effect=AssertionError("must not download an .exe")):
            result = api.apply_update("anything")
        self.assertEqual(result["status"], "unsupported")
        self.assertEqual(result["msg"], main.UNSUPPORTED_PLATFORM_UPDATE_MSG)


class StreamFormatTests(unittest.TestCase):
    """WebKit cannot decode Opus-in-WebM, so an AAC/MP4 rung must come first."""

    def test_format_selector_prefers_aac_before_any_bare_bestaudio(self):
        import music_bridge

        selector = music_bridge._STREAM_OPTS["format"]
        rungs = selector.split("/")
        first_bare = next(i for i, rung in enumerate(rungs) if rung in ("bestaudio", "best"))
        aac_rungs = [i for i, rung in enumerate(rungs)
                     if "m4a" in rung or "mp4a" in rung or "mp4" in rung]
        self.assertTrue(aac_rungs, "no AAC/MP4 preference at all")
        self.assertLess(min(aac_rungs), first_bare)
        self.assertLess(max(aac_rungs), first_bare,
                        "every AAC/MP4 rung must be tried before a bare bestaudio")


@unittest.skipUnless(IS_MACOS_HOST, "needs real AppKit/WebKit")
class CocoaHelperTests(unittest.TestCase):
    """The real Cocoa layer, exercised on a macOS host."""

    def setUp(self):
        import macos_support

        self.macos_support = macos_support
        self.logged: list[str] = []

    def test_module_reports_itself_supported_on_a_mac(self):
        self.assertTrue(self.macos_support.is_supported())

    def test_wkwebview_allows_gestureless_playback(self):
        # The whole player design depends on this: audio starts from a socket
        # command, never from a click. If a future macOS flips the default this
        # fails loudly instead of the app going mysteriously silent.
        self.assertTrue(self.macos_support.autoplay_needs_no_patch(self.logged.append))
        self.assertEqual(self.logged, [])

    def test_helpers_are_inert_before_the_window_is_realised(self):
        # pywebview only assigns .native while constructing the browser view, so
        # every helper may be called with nothing behind it.
        unrealised = types.SimpleNamespace(native=None)
        self.macos_support.shape_player(unrealised, True, self.logged.append)
        self.macos_support.hide_player_from_window_list(unrealised, self.logged.append)
        self.assertEqual(self.logged, [])

    def test_pin_reports_a_missing_native_window_instead_of_failing_silently(self):
        with self.assertRaises(RuntimeError):
            self.macos_support.apply_player_on_top(
                types.SimpleNamespace(native=None), True, self.logged.append
            )

    def test_park_fades_the_window_instead_of_ordering_it_out(self):
        """Ordering the player out suspends requestAnimationFrame, and Rainette's
        pause is a rAF-driven volume ramp whose callback calls audio.pause() --
        so a hidden player plays on forever with a dead pause button. Parking
        must fade the window and keep it ordered in, never hide()/minimize()."""
        native = _FakeNativeWindow()
        window = _RecordingWindow()
        window.native = native

        self.macos_support.park_player(window, self.logged.append)
        self.assertEqual(native.alpha, 0.0, "parked player must be transparent")
        self.assertTrue(native.ignores_mouse, "an invisible window must not eat clicks")
        self.assertTrue(native.ordered_front, "it must stay ordered in, or rAF suspends")
        self.assertNotIn("hide", window.calls)
        self.assertNotIn("minimize", window.calls)

        self.macos_support.unpark_player(window, self.logged.append)
        self.assertEqual(native.alpha, 1.0)
        self.assertFalse(native.ignores_mouse)
        self.assertEqual(self.logged, [])

    def test_park_and_unpark_are_inert_without_a_native_window(self):
        unrealised = types.SimpleNamespace(native=None)
        self.macos_support.park_player(unrealised, self.logged.append)
        self.macos_support.unpark_player(unrealised, self.logged.append)
        self.assertEqual(self.logged, [])


if __name__ == "__main__":
    unittest.main()
