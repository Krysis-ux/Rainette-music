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
