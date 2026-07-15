import sys
import types
import unittest
import urllib.error
from urllib.parse import parse_qs, urlparse

import main
import server


class FakePlayerWindow:
    """Records calls instead of touching a real pywebview window."""

    def __init__(self):
        self.calls = []
        self._on_top = False
        self.raise_on_top = False

    def show(self):
        self.calls.append("show")

    def restore(self):
        self.calls.append("restore")

    def hide(self):
        self.calls.append("hide")

    def minimize(self):
        self.calls.append("minimize")

    def resize(self, width, height):
        self.calls.append(("resize", width, height))

    @property
    def on_top(self):
        return self._on_top

    @on_top.setter
    def on_top(self, value):
        if self.raise_on_top:
            raise RuntimeError("native TopMost failure")
        self._on_top = bool(value)
        self.calls.append(("on_top", bool(value)))


class FakeMainWindow:
    def __init__(self):
        self.calls = []

    def show(self):
        self.calls.append("show")

    def restore(self):
        self.calls.append("restore")


class MissingNativeFormPlayer(FakePlayerWindow):
    uid = "missing-native-form"


class WindowApiGatingTests(unittest.TestCase):
    """reveal_player() replaced a two-call player_allow_show()+show_player()
    handshake that was a guaranteed no-op on its only first-play call site
    (nothing ever called player_allow_show() there) and a race on its other
    call site (two independent, un-awaited bridge calls). These tests guard
    the fixed, single-call contract."""

    def setUp(self):
        self.api = main.WindowApi()
        self.player = FakePlayerWindow()
        self.api.bind_player(self.player)

    def test_show_player_is_noop_before_any_reveal(self):
        self.api.show_player()
        self.assertEqual(self.player.calls, [])

    def test_reveal_player_shows_and_restores(self):
        self.api.reveal_player()
        self.assertIn("show", self.player.calls)
        self.assertIn("restore", self.player.calls)

    def test_reveal_player_unlocks_can_show_flag(self):
        self.assertFalse(self.api._player_can_show)
        self.api.reveal_player()
        self.assertTrue(self.api._player_can_show)

    def test_reveal_player_is_idempotent(self):
        self.api.reveal_player()
        self.api.reveal_player()
        self.assertEqual(self.player.calls.count("show"), 2)
        # Second call is a harmless redundant show/restore, not an error or a
        # different code path - the flag stays True throughout.
        self.assertTrue(self.api._player_can_show)

    def test_player_allow_show_alone_still_unlocks_show_player(self):
        # player_allow_show() is kept for the reconnect-handshake path in
        # miniplayer.js, which now calls reveal_player() too - but the old
        # two-step sequence must still work if anything else calls it directly.
        self.api.player_allow_show()
        self.api.show_player()
        self.assertIn("show", self.player.calls)

    def test_player_hide_and_minimize_do_not_require_reveal(self):
        self.api.player_hide()
        self.api.player_minimize()
        self.assertEqual(self.player.calls, ["hide", "minimize"])

    def test_player_resize_tracks_collapsed_state(self):
        self.api.player_resize(False)
        self.assertFalse(self.api._player_collapsed)
        resize_calls = [c for c in self.player.calls if isinstance(c, tuple) and c[0] == "resize"]
        self.assertEqual(resize_calls[-1], ("resize", main.PLAYER_SIZE[0], main.PLAYER_EXPANDED_HEIGHT))

        self.api.player_resize(True)
        self.assertTrue(self.api._player_collapsed)
        resize_calls = [c for c in self.player.calls if isinstance(c, tuple) and c[0] == "resize"]
        self.assertEqual(resize_calls[-1], ("resize", main.PLAYER_SIZE[0], main.PLAYER_SIZE[1]))

    def test_no_player_window_bound_is_safe(self):
        api = main.WindowApi()
        # None of these should raise even though bind_player() was never called.
        api.show_player()
        api.reveal_player()
        api.player_hide()
        api.player_minimize()
        api.player_resize(False)

    def test_pin_returns_structured_success_and_updates_after_native_call(self):
        result = self.api.player_toggle_pin()
        self.assertEqual(result, {"enabled": True, "available": True})
        self.assertTrue(self.api._player_on_top)
        self.assertTrue(self.player.on_top)

    def test_pin_failure_does_not_crash_or_change_state(self):
        self.player.raise_on_top = True
        result = self.api.player_toggle_pin()
        self.assertFalse(result["available"])
        self.assertFalse(result["enabled"])
        self.assertIn("TopMost failure", result["error"])
        self.assertFalse(self.api._player_on_top)

    def test_main_reveal_focuses_main_window(self):
        main_window = FakeMainWindow()
        self.api.bind_main(main_window)
        self.assertTrue(self.api.main_reveal())
        self.assertEqual(main_window.calls, ["show", "restore"])

    def test_windows_pin_never_falls_back_off_ui_thread_when_native_form_missing(self):
        native = MissingNativeFormPlayer()
        self.api.bind_player(native)
        result = self.api.player_toggle_pin()
        self.assertFalse(result["available"])
        self.assertFalse(native.on_top)
        self.assertNotIn(("on_top", True), native.calls)


class PlayerShapeOrderingTests(unittest.TestCase):
    """_shape_player() derives the rounded-rect region from the form's *current*
    Width/Height, so it may only run once the window is back at its Normal-state
    size. Shaping first baked the minimized bounds into the region and clipped the
    restored WebView2 content - the "mini player only half loads when I re-open it"
    bug. player_resize() always had the right order; show_player() did not."""

    def setUp(self):
        self.api = main.WindowApi()
        self.player = FakePlayerWindow()
        self.api.bind_player(self.player)
        # Record shaping in the same call log as the window calls so ordering is
        # asserted directly rather than inferred.
        self.api._shape_player = lambda: self.player.calls.append("shape")

    def test_reveal_player_shapes_only_after_show_and_restore(self):
        self.api.reveal_player()
        self.assertEqual(self.player.calls, ["show", "restore", "shape"])

    def test_show_player_shapes_only_after_show_and_restore(self):
        self.api.player_allow_show()
        self.api.show_player()
        self.assertEqual(self.player.calls, ["show", "restore", "shape"])

    def test_show_player_still_shapes_nothing_before_reveal(self):
        self.api.show_player()
        self.assertEqual(self.player.calls, [])

    def test_repeated_reveals_reshape_every_time(self):
        # Re-opening after a minimize must recompute the region; a cached/skipped
        # shape would reintroduce the stale-mask clipping.
        self.api.reveal_player()
        self.api.player_minimize()
        self.api.reveal_player()
        self.assertEqual(self.player.calls.count("shape"), 2)
        self.assertEqual(self.player.calls[-3:], ["show", "restore", "shape"])

    def test_player_resize_keeps_sizing_before_shaping(self):
        self.api.player_resize(False)
        self.assertEqual(
            self.player.calls,
            [("resize", main.PLAYER_SIZE[0], main.PLAYER_EXPANDED_HEIGHT), "shape"],
        )


class FakeCreationProperties:
    def __init__(self):
        self.AdditionalBrowserArguments = ""


class FakeWebView2:
    def __init__(self):
        self.CreationProperties = FakeCreationProperties()


class FakeEdgeChrome:
    """Mirrors the part of pywebview 6.2.1's EdgeChrome.__init__ that matters: it
    hardcodes AdditionalBrowserArguments from a string literal, and the browser's
    handle (which consumes them) is created before __init__ returns."""

    def __init__(self, form, window, cache_dir):
        self.form = form
        self.webview = FakeWebView2()
        self.webview.CreationProperties.AdditionalBrowserArguments = "--disable-features=ElasticOverscroll"


class UnrecognisedEdgeChrome:
    """A future pywebview whose hardcoded arguments no longer match."""

    def __init__(self, form, window, cache_dir):
        self.webview = FakeWebView2()
        self.webview.CreationProperties.AdditionalBrowserArguments = "--something-else-entirely"


class AutoplayFlagPatchTests(unittest.TestCase):
    """The player window owns the only <audio> element and is driven over the socket,
    so its play() has no user activation and Chromium rejects it with NotAllowedError
    - nothing is audible until the window is revealed. (Verified: a *visible* window
    is refused identically, so activation is the gate, not visibility.)

    pywebview hardcodes AdditionalBrowserArguments, offers no setting for extra
    arguments, and WebView2 ignores WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS once that
    property is set - so the env var alone is dead code. These tests pin the patch
    that actually delivers the flag, and pin that it fails safe."""

    def setUp(self):
        self.saved = {k: v for k, v in sys.modules.items() if k == "webview" or k.startswith("webview.")}
        for name in list(self.saved):
            del sys.modules[name]
        self._install(FakeEdgeChrome)

    def _install(self, browser_cls):
        # The patch rewrites the class's code object in place, so snapshot it and
        # restore in tearDown or the mutation leaks into the next test.
        self.browser_cls = browser_cls
        self._orig_code = browser_cls.__init__.__code__
        self._clear_marker(browser_cls)
        root = types.ModuleType("webview")
        platforms = types.ModuleType("webview.platforms")
        edge = types.ModuleType("webview.platforms.edgechromium")
        edge.EdgeChrome = browser_cls
        platforms.edgechromium = edge
        root.platforms = platforms
        sys.modules["webview"] = root
        sys.modules["webview.platforms"] = platforms
        sys.modules["webview.platforms.edgechromium"] = edge
        self.edge = edge

    @staticmethod
    def _clear_marker(browser_cls):
        try:
            del browser_cls.__init__._rainette_autoplay_patched
        except AttributeError:
            pass

    def tearDown(self):
        self.browser_cls.__init__.__code__ = self._orig_code
        self._clear_marker(self.browser_cls)
        for name in ("webview", "webview.platforms", "webview.platforms.edgechromium"):
            sys.modules.pop(name, None)
        sys.modules.update(self.saved)

    def _args(self):
        browser = self.edge.EdgeChrome(object(), object(), "cache")
        return browser.webview.CreationProperties.AdditionalBrowserArguments

    def test_flag_reaches_the_arguments_pywebview_itself_assigns(self):
        self.assertTrue(main._patch_webview2_autoplay())
        args = self._args()
        self.assertIn(main.AUTOPLAY_FLAG, args)
        # pywebview's own flag must survive - we extend its literal, never replace it.
        self.assertIn(main.PYWEBVIEW_BROWSER_ARGS, args)

    def test_patch_is_idempotent_and_does_not_duplicate_the_flag(self):
        self.assertTrue(main._patch_webview2_autoplay())
        self.assertTrue(main._patch_webview2_autoplay())
        self.assertEqual(self._args().count(main.AUTOPLAY_FLAG), 1)

    def test_patch_disables_itself_if_pywebview_changes_its_arguments(self):
        self.tearDown()
        self.saved = {}
        self._install(UnrecognisedEdgeChrome)
        # Better to lose the autoplay flag than to corrupt an unknown argument list.
        self.assertFalse(main._patch_webview2_autoplay())
        self.assertNotIn(main.AUTOPLAY_FLAG, self._args())

    def test_patch_reports_failure_instead_of_raising_when_backend_missing(self):
        del sys.modules["webview.platforms.edgechromium"]
        sys.modules["webview.platforms"] = types.ModuleType("webview.platforms")
        # Non-Windows hosts have no edgechromium backend; the app must still start.
        self.assertFalse(main._patch_webview2_autoplay())


def test_companion_invitation_returns_local_qr_without_launch_token(monkeypatch):
    monkeypatch.setattr(server, "create_companion_invitation", lambda: {
        "version": 1,
        "endpoint": "https://192.168.1.5:9999",
        "certificate_sha256": "abc",
        "invitation": "invite",
        "expires_at": 1300,
    })

    result = main.WindowApi().companion_create_invitation()

    assert result["ok"] is True
    assert result["pairing_uri"].startswith("rainette://pair?")
    assert result["pairing_qr_data_url"].startswith("data:image/png;base64,")
    query = parse_qs(urlparse(result["pairing_uri"]).query)
    assert query == {
        "endpoint": ["https://192.168.1.5:9999"],
        "certificate_sha256": ["abc"],
        "invitation": ["invite"],
    }
    assert result["expires_at"] == 1300
    assert server.APP_TOKEN not in result["pairing_uri"]


def test_desktop_startup_restores_listener_for_existing_paired_devices(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "start", lambda: 8777)
    monkeypatch.setattr(
        server,
        "start_paired_companion",
        lambda: calls.append("companion") or {"port": 47878},
    )
    monkeypatch.setattr(main, "log", lambda _message: None)
    monkeypatch.setattr(main, "_try_pywebview", lambda _url: True)

    assert main.main() == 0
    assert calls == ["companion"]


def test_companion_management_methods_delegate_to_server(monkeypatch):
    calls = []
    state = {"pending": [{"request_id": "request-1"}], "devices": []}
    monkeypatch.setattr(server, "companion_management_state", lambda: state)
    monkeypatch.setattr(
        server,
        "approve_companion_request",
        lambda request_id: calls.append(("approve", request_id)) or {"device_id": "device-1"},
    )
    monkeypatch.setattr(
        server,
        "reject_companion_request",
        lambda request_id: calls.append(("reject", request_id)) or True,
    )
    monkeypatch.setattr(
        server,
        "revoke_companion_device",
        lambda device_id: calls.append(("revoke", device_id)) or True,
    )
    api = main.WindowApi()

    assert api.companion_management_state() == state
    assert api.companion_approve_request("request-1") == {"device_id": "device-1"}
    assert api.companion_reject_request("request-2") is True
    assert api.companion_revoke_device("device-1") is True
    assert calls == [
        ("approve", "request-1"),
        ("reject", "request-2"),
        ("revoke", "device-1"),
    ]


def test_android_download_info_uses_exact_release_url_and_local_qr(monkeypatch):
    checked = []
    monkeypatch.setattr(
        main,
        "_android_release_status",
        lambda url: checked.append(url) or "published",
        raising=False,
    )

    result = main.WindowApi().android_download_info()

    expected_url = (
        "https://github.com/Krysis-ux/Rainette-music/releases/latest/download/"
        "rainette-music-android.apk"
    )
    assert result == {
        "url": expected_url,
        "install_qr_data_url": result["install_qr_data_url"],
        "status": "published",
        "published": True,
    }
    assert result["install_qr_data_url"].startswith("data:image/png;base64,")
    assert checked == [expected_url]


def test_download_publication_check_uses_head_with_three_second_ceiling(monkeypatch):
    observed = {}

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def fake_urlopen(request, timeout):
        observed["method"] = request.get_method()
        observed["timeout"] = timeout
        return Response()

    monkeypatch.setattr(main.urllib.request, "urlopen", fake_urlopen)

    assert main._is_url_published("https://example.invalid/app.apk") is True
    assert observed == {"method": "HEAD", "timeout": 3}


def test_download_publication_network_failure_returns_false(monkeypatch):
    def fail_urlopen(_request, timeout):
        assert timeout == 3
        raise OSError("offline")

    monkeypatch.setattr(main.urllib.request, "urlopen", fail_urlopen)

    assert main._is_url_published("https://example.invalid/app.apk") is False


def test_android_download_info_distinguishes_published_unavailable_and_check_failed(monkeypatch):
    api = main.WindowApi()
    for check, expected in (("published", "published"), ("unavailable", "unavailable"), ("check_failed", "check_failed")):
        monkeypatch.setattr(main, "_android_release_status", lambda _url, value=check: value)
        result = api.android_download_info()
        assert result["status"] == expected
        assert result["published"] is (expected == "published")


def test_release_status_maps_404_separately_from_network_failure(monkeypatch):
    def missing(_request, timeout):
        raise urllib.error.HTTPError("url", 404, "missing", {}, None)
    monkeypatch.setattr(main.urllib.request, "urlopen", missing)
    assert main._android_release_status("https://example.invalid/app.apk") == "unavailable"

    monkeypatch.setattr(main.urllib.request, "urlopen", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("offline")))
    assert main._android_release_status("https://example.invalid/app.apk") == "check_failed"


if __name__ == "__main__":
    unittest.main()
