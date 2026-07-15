import functools
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
WEB_DIR = ROOT / "web"


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, _format, *_args):
        pass


def test_native_mobile_loading_pairing_and_recovery_states_are_real_and_scoped():
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        functools.partial(QuietHandler, directory=str(WEB_DIR)),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 390, "height": 844})
            page.add_init_script(
                """
                window.__nativeListeners = {};
                window.__pendingNative = {};
                window.__paired = false;
                const listen = (name, callback) => {
                    window.__nativeListeners[name] = callback;
                    return Promise.resolve({remove() {}});
                };
                const companion = {
                    addListener: listen,
                    startSync: async () => ({ok: true}),
                    connectionStatus: async () => window.__paired
                        ? ({ok: true, paired: true, status: 'connected', device_id: 'device-12345678', endpoint_host: '192.168.5.31'})
                        : ({ok: true, paired: false, status: 'unpaired'}),
                    pair: async () => ({ok: true}),
                    request: ({payload}) => {
                        if (payload.type === 'music_library_index') {
                            return new Promise(resolve => { window.__pendingNative.library = value => resolve({id: payload.id, ...value}); });
                        }
                        if (payload.type === 'music_search') {
                            return new Promise(resolve => { window.__pendingNative.search = value => resolve({id: payload.id, ...value}); });
                        }
                        return Promise.resolve({id: payload.id, ok: true});
                    },
                };
                const player = {addListener: listen, command: async () => ({ok: true, playing: true})};
                window.Capacitor = {Plugins: {RainetteCompanion: companion, RainettePlayer: player}};
                """
            )
            page.goto(f"http://127.0.0.1:{server.server_port}/", wait_until="domcontentloaded")
            page.locator("#rwMobileApp .rw-mobile-tabs").wait_for(state="visible")

            page.get_by_text("Syncing your library", exact=True).wait_for()
            assert page.locator(".rw-flower-loader").count() == 0
            page.evaluate(
                """() => window.__pendingNative.library({
                    ok: true,
                    tracks: [{source_id: 'song-1', title: 'Quiet Bloom', artist: 'Rainette'}]
                })"""
            )
            page.get_by_text("Quiet Bloom", exact=True).wait_for()

            page.locator('#rwMobileApp [data-tab="more"]').click()
            page.get_by_text("Pair a desktop", exact=True).click()
            page.get_by_text("Pair this phone", exact=True).wait_for()
            page.evaluate(
                """() => window.__nativeListeners.rainetteCompanionMessage({
                    type: 'rainette_companion_pairing', status: 'pending_approval', ok: true
                })"""
            )
            page.get_by_role("heading", name="Waiting for approval", exact=True).wait_for()
            assert page.locator(".rw-mobile-pair-sheet .rw-kage-loader").is_visible()

            page.evaluate(
                """() => {
                    window.__paired = true;
                    window.__nativeListeners.rainetteCompanionMessage({
                        type: 'rainette_companion_pairing', status: 'approved', ok: true
                    });
                }"""
            )
            page.get_by_text("Phone paired", exact=True).wait_for()
            assert "192.168.5.31" in page.locator(".rw-mobile-pair-sheet").inner_text()
            page.get_by_text("Done", exact=True).click()

            page.locator('#rwMobileApp [data-tab="search"]').click()
            page.locator("#rwMobileQuery").fill("bloom")
            page.locator("#rwMobileSearch button").click()
            page.get_by_text("Searching Rainette", exact=True).wait_for()
            page.locator('#rwMobileApp [data-tab="library"]').click()
            assert page.get_by_text("Quiet Bloom", exact=True).is_visible()
            assert page.get_by_text("Searching Rainette", exact=True).count() == 0
            page.locator('#rwMobileApp [data-tab="search"]').click()
            page.evaluate(
                """() => window.__pendingNative.search({
                    ok: true,
                    items: [{source_id: 'song-2', title: 'After Rain', artist: 'Rainette'}]
                })"""
            )
            page.get_by_text("After Rain", exact=True).wait_for()

            page.evaluate(
                "() => window.__nativeListeners.rainetteCompanionSync({ok: false, status: 'reconnecting', msg: 'offline'})"
            )
            page.get_by_text("Reconnecting to desktop", exact=True).wait_for()
            page.evaluate(
                "() => window.__nativeListeners.rainetteCompanionSync({ok: true, revision: 4, events: []})"
            )
            assert page.get_by_text("Reconnecting to desktop", exact=True).count() == 0

            page.evaluate("document.documentElement.classList.add('rw-reduced-motion')")
            page.evaluate(
                """() => window.__nativeListeners.rainetteCompanionMessage({
                    type: 'rainette_companion_pairing', status: 'securing', ok: true
                })"""
            )
            page.get_by_role("heading", name="Securing the connection", exact=True).wait_for()
            assert page.locator(".rw-mobile-pair-sheet .rw-kage-loader-petal").first.evaluate(
                "element => getComputedStyle(element).animationName === 'none'"
            )
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
