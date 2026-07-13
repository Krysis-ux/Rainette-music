import contextlib
import functools
import mimetypes
import threading
import time
import urllib.request
from urllib.parse import urlparse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
WEB_DIR = ROOT / "web"
INSTALL_QR = "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='2' height='2'%3E%3Cpath fill='green' d='M0 0h2v2H0z'/%3E%3C/svg%3E"
PAIR_QR = "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='2' height='2'%3E%3Cpath fill='blue' d='M0 0h2v2H0z'/%3E%3C/svg%3E"
FAKE_WEBSOCKET_SCRIPT = """
(() => {
    window.__rainetteFakeWebSockets = [];
    class FakeWebSocket extends EventTarget {
        constructor(url) {
            super();
            this.url = String(url);
            this.readyState = FakeWebSocket.OPEN;
            this.protocol = '';
            this.extensions = '';
            this.bufferedAmount = 0;
            this.binaryType = 'blob';
            this.openDispatched = false;
            window.__rainetteFakeWebSockets.push(this);
            queueMicrotask(() => {
                if (this.readyState !== FakeWebSocket.OPEN) return;
                this.openDispatched = true;
                this.dispatchEvent(new Event('open'));
            });
        }
        send(_data) {}
        close() {
            if (this.readyState === FakeWebSocket.CLOSED) return;
            this.readyState = FakeWebSocket.CLOSED;
            queueMicrotask(() => this.dispatchEvent(new CloseEvent('close')));
        }
    }
    FakeWebSocket.CONNECTING = 0;
    FakeWebSocket.OPEN = 1;
    FakeWebSocket.CLOSING = 2;
    FakeWebSocket.CLOSED = 3;
    window.WebSocket = FakeWebSocket;
})();
"""


class QuietStaticHandler(SimpleHTTPRequestHandler):
    def log_message(self, _format, *_args):
        pass


class QuietThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True


@contextlib.contextmanager
def running_web_server():
    handler = functools.partial(QuietStaticHandler, directory=str(WEB_DIR))
    server = QuietThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/?remote=1"
    deadline = time.monotonic() + 3
    while True:
        try:
            with urllib.request.urlopen(url, timeout=0.5) as response:
                assert response.status == 200
            break
        except OSError:
            if time.monotonic() >= deadline:
                raise AssertionError(f"local browser fixture did not start: {url}")
            time.sleep(0.025)
    try:
        yield url
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def fulfill_test_asset(route):
    parsed = urlparse(route.request.url)
    if parsed.hostname in {"127.0.0.1", "localhost"}:
        relative = parsed.path.lstrip("/") or "index.html"
        path = (WEB_DIR / relative).resolve()
        if WEB_DIR.resolve() in path.parents and path.is_file():
            content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            route.fulfill(status=200, path=str(path), content_type=content_type)
            return
    if parsed.hostname == "fonts.googleapis.com":
        route.fulfill(status=200, content_type="text/css", body="")
        return
    if parsed.hostname == "fonts.gstatic.com":
        route.abort()
        return
    route.continue_()


def open_mobile_page(page, url):
    diagnostics = []
    page.add_init_script(FAKE_WEBSOCKET_SCRIPT)
    page.on(
        "requestfailed",
        lambda request: diagnostics.append(f"requestfailed:{request.url}:{request.failure}")
        if "fonts.googleapis.com" not in request.url and "fonts.gstatic.com" not in request.url
        else None,
    )
    page.on("pageerror", lambda error: diagnostics.append(f"pageerror:{error}"))
    page.route("**/*", fulfill_test_asset)
    page.set_default_timeout(7_000)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=7_000)
        page.locator("#rwMusicTabs").wait_for(state="visible", timeout=7_000)
        page.locator('#rwMusicTabs button[data-tab="mobile"]').click()
        page.locator(".rw-mobile-grid").wait_for(state="visible")
    except Exception as error:
        raise AssertionError(
            f"mobile page startup failed at {page.url}; diagnostics={diagnostics[-10:]}"
        ) from error


def test_mobile_native_pairing_actions_and_detail_unmount_stop_polling():
    with running_web_server() as url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": 1060, "height": 730})
            page.add_init_script(
                """
                Date.now = () => 1800000000000;
                window.__actionCalls = [];
                window.__managementCalls = 0;
                window.pywebview = {api: {
                    android_download_info: async () => ({
                        url: 'https://github.com/Krysis-ux/Rainette-music/releases/latest/download/rainette-music-android.apk',
                        install_qr_data_url: %s,
                        published: true
                    }),
                    companion_create_invitation: async () => ({
                        ok: true,
                        pairing_qr_data_url: %s,
                        expires_at: 1800000300
                    }),
                    companion_management_state: async () => {
                        window.__managementCalls += 1;
                        return {
                            pending: [
                                {request_id: 'request-approve', device_name: 'Pixel'},
                                {request_id: 'request-reject', device_name: 'Galaxy'}
                            ],
                            devices: [{device_id: 'device-revoke', name: 'Tablet', revoked: false}]
                        };
                    },
                    companion_approve_request: async id => (window.__actionCalls.push(['approve', id]), {device_id: 'approved'}),
                    companion_reject_request: async id => (window.__actionCalls.push(['reject', id]), true),
                    companion_revoke_device: async id => (window.__actionCalls.push(['revoke', id]), true)
                }};
                """
                % (repr(INSTALL_QR), repr(PAIR_QR))
            )
            open_mobile_page(page, url)

            page.get_by_text("Pixel", exact=True).wait_for()
            install_src = page.locator("#rwInstallQr img").get_attribute("src")
            page.get_by_text("New pairing code", exact=True).click()
            page.locator("#rwPairingQr img").wait_for()
            pair_src = page.locator("#rwPairingQr img").get_attribute("src")
            assert install_src == INSTALL_QR
            assert pair_src == PAIR_QR
            assert install_src != pair_src
            assert page.locator("#rwPairingExpiry").inner_text() == "Expires in 5:00"
            assert page.locator("#rwPairingExpiry").get_attribute("data-remaining-seconds") == "300"

            page.locator(".rw-mobile-device", has_text="Pixel").get_by_text("Approve", exact=True).click()
            page.locator(".rw-mobile-device", has_text="Galaxy").get_by_text("Reject", exact=True).click()
            page.locator(".rw-mobile-device", has_text="Tablet").get_by_text("Revoke", exact=True).click()
            page.wait_for_function("() => window.__actionCalls.length === 3")
            assert page.evaluate("window.__actionCalls") == [
                ["approve", "request-approve"],
                ["reject", "request-reject"],
                ["revoke", "device-revoke"],
            ]

            page.evaluate(
                "() => document.dispatchEvent(new CustomEvent('rainette:helper-message', "
                "{detail: {type: 'music_open_artist', artist_id: 'artist-mobile', name: 'Mobile Artist'}}))"
            )
            page.get_by_text("Mobile Artist", exact=True).wait_for()
            calls_after_unmount = page.evaluate("window.__managementCalls")
            page.wait_for_timeout(2_250)
            assert page.evaluate("window.__managementCalls") == calls_after_unmount
        finally:
            browser.close()


def test_stale_mount_result_cannot_overwrite_or_duplicate_new_mount_polling():
    with running_web_server() as url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": 1060, "height": 730})
            page.add_init_script(
                """
                window.__managementCalls = 0;
                window.__resolveStaleManagement = null;
                window.pywebview = {api: {
                    android_download_info: async () => ({published: false, install_qr_data_url: ''}),
                    companion_management_state: async () => {
                        window.__managementCalls += 1;
                        if (window.__managementCalls === 1) {
                            return new Promise(resolve => { window.__resolveStaleManagement = resolve; });
                        }
                        return {pending: [{request_id: 'new', device_name: 'New Phone'}], devices: []};
                    }
                }};
                """
            )
            open_mobile_page(page, url)
            page.wait_for_function("() => typeof window.__resolveStaleManagement === 'function'")
            page.locator('#rwMusicTabs button[data-tab="home"]').click()
            page.locator('#rwMusicTabs button[data-tab="mobile"]').click()
            page.get_by_text("New Phone", exact=True).wait_for()
            page.evaluate(
                "() => window.__resolveStaleManagement({pending: "
                "[{request_id: 'old', device_name: 'Old Phone'}], devices: []})"
            )
            page.wait_for_timeout(100)
            assert page.get_by_text("New Phone", exact=True).is_visible()
            assert page.get_by_text("Old Phone", exact=True).count() == 0
            calls_before_poll = page.evaluate("window.__managementCalls")
            page.wait_for_timeout(2_150)
            assert page.evaluate("window.__managementCalls") == calls_before_poll + 1
        finally:
            browser.close()


def test_browser_fixture_fake_websocket_opens_without_network_across_repeated_pages():
    with running_web_server() as url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            for _ in range(5):
                page = browser.new_page(viewport={"width": 1060, "height": 730})
                websocket_requests = []
                page.on(
                    "request",
                    lambda request: websocket_requests.append(request.url)
                    if request.url.startswith(("ws://", "wss://"))
                    else None,
                )
                open_mobile_page(page, url)
                page.wait_for_function(
                    "() => window.__rainetteFakeWebSockets?.some(socket => "
                    "socket.readyState === WebSocket.OPEN && socket.openDispatched)"
                )
                assert page.evaluate("() => WebSocket.name") == "FakeWebSocket"
                assert websocket_requests == []
                page.close()
        finally:
            browser.close()
