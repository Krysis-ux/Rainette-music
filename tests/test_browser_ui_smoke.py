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


def emit(page, payload):
    page.evaluate(
        "payload => document.dispatchEvent(new CustomEvent('rainette:helper-message', { detail: payload }))",
        payload,
    )


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


def sample_tracks(count=14):
    tracks = []
    for index in range(count):
        tracks.append(
            {
                "id": f"track-{index}",
                "source": "youtube",
                "source_id": f"source-{index}",
                "title": f"Song {index:02d}",
                "artist": f"Artist {index % 4}",
                "duration_s": 180 + index,
                "thumbnail_url": "",
                "played_at": f"2026-07-{12 - min(index, 9):02d}T12:00:00-04:00",
                "metadata": {
                    "artist_id": f"artist-{index % 4}",
                    "album_id": f"album-{index % 3}",
                    "album_name": f"Album {index % 3}",
                },
            }
        )
    return tracks


def test_core_release_browser_flow():
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
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1060, "height": 730})
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
            except Exception as error:
                raise AssertionError(
                    f"browser smoke startup failed at {page.url}; diagnostics={diagnostics[-10:]}"
                ) from error

            assert page.locator("#rwMusicTabs button").all_inner_texts()[:7] == [
                "Home",
                "Search",
                "Songs",
                "Following",
                "Recents",
                "Playlists",
                "Insights",
            ]

            tracks = sample_tracks()
            emit(page, {"type": "music_recent_result", "ok": True, "tracks": tracks})

            body = page.locator("#rwMusicBody")
            body_metrics = body.evaluate(
                "el => ({ clientHeight: el.clientHeight, scrollHeight: el.scrollHeight, "
                "bodyRect: el.getBoundingClientRect().height, pageBody: el.parentElement.getBoundingClientRect().height, "
                "shell: el.closest('.rw-music-shell').getBoundingClientRect().height, cards: el.querySelectorAll('.rw-track-card').length })"
            )
            assert body_metrics["scrollHeight"] > body_metrics["clientHeight"], body_metrics
            assert page.locator("#rwMusicBody .rw-track-list").first.evaluate(
                "el => getComputedStyle(el).overflowY === 'visible'"
            )
            body.evaluate("el => { el.scrollTop = el.scrollHeight; }")
            assert body.evaluate("el => el.scrollTop > 0")

            page.locator('#rwMusicTabs button[data-tab="search"]').click()
            page.get_by_text("Recent artists", exact=True).wait_for()
            assert page.get_by_text("Artist 0", exact=True).count() >= 1

            page.locator('#rwMusicBody input[type="search"]').fill("rain")
            emit(
                page,
                {
                    "type": "music_catalog_search_result",
                    "ok": True,
                    "songs": [tracks[0]],
                    "artists": [{"id": "artist-result", "name": "Rain Artist"}],
                    "albums": [{"id": "album-result", "title": "Rain Album", "artist": "Rain Artist"}],
                },
            )
            assert page.locator("#rwMusicResults .rw-section-title h3").all_inner_texts() == [
                "SONGS",
                "ARTISTS",
                "ALBUMS",
            ]
            page.locator(".rw-search-filters").get_by_text("Artists", exact=True).click()
            assert page.locator("#rwMusicResults .rw-section-title h3").all_inner_texts() == ["ARTISTS"]

            emit(
                page,
                {
                    "type": "music_library_index_result",
                    "ok": True,
                    "tracks": tracks[:4],
                    "artists": [],
                    "albums": [],
                    "followed_artists": [
                        {
                            "artist_key": "id:artist-followed",
                            "artist_id": "artist-followed",
                            "name": "Followed Artist",
                            "followed_at": "2026-07-12T12:00:00-04:00",
                        }
                    ],
                },
            )
            page.locator('#rwMusicTabs button[data-tab="following"]').click()
            page.get_by_text("Followed Artist", exact=True).wait_for()

            page.locator('#rwMusicTabs button[data-tab="recent"]').click()
            page.locator("#rwMusicBody .rw-segment").get_by_text("Artists", exact=True).click()
            assert page.get_by_text("Artist 0", exact=True).count() >= 1
            page.locator("#rwMusicBody .rw-segment").get_by_text("Albums", exact=True).click()
            assert page.get_by_text("Album 0", exact=True).count() >= 1

            page.locator('#rwMusicTabs button[data-tab="home"]').click()
            page.locator('.rw-icon-btn[aria-label="More actions"]').first.click()
            page.get_by_text("Add to playlist", exact=True).click()
            page.get_by_text("Create new playlist", exact=True).wait_for()
            page.get_by_text("Cancel", exact=True).click()

            emit(
                page,
                {
                    "type": "music_playlist_list_result",
                    "ok": True,
                    "folders": [],
                    "playlists": [
                        {
                            "id": "playlist-art",
                            "name": "Artwork Playlist",
                            "kind": "manual",
                            "track_count": 1,
                            "artwork_key": "playlist-art_0123456789abcdef0123456789abcdef.png",
                        }
                    ],
                },
            )
            page.locator('#rwMusicTabs button[data-tab="playlists"]').click()
            art = page.locator('.rw-playlist-card img[src*="playlist-artwork"]').first
            assert art.get_attribute("src").endswith("playlist-art_0123456789abcdef0123456789abcdef.png")

            emit(
                page,
                {
                    "type": "music_now_playing",
                    "state": "playing",
                    "playing": True,
                    "queue": tracks[:2],
                    "index": 0,
                    "current_time": 12,
                    "duration": 180,
                },
            )
            volume = page.locator("#rwDockedBar .rw-now-volume")
            volume.wait_for(state="visible")
            volume.evaluate(
                "el => { el.value = '37'; el.dispatchEvent(new Event('input', { bubbles: true })); }"
            )
            assert page.evaluate("() => localStorage.getItem('rw.mp.volume')") == "0.37"

            command_button = page.locator("#rwCommandOpen")
            command_box = command_button.bounding_box()
            dock_box = page.locator("#rwDockedBar").bounding_box()
            assert command_box and dock_box and command_box["y"] + command_box["height"] <= dock_box["y"], (
                command_box,
                dock_box,
            )
            command_button.click()
            page.locator("#rwCommandPalette").wait_for(state="visible")
            page.keyboard.press("Escape")

            page.locator('#rwMusicTabs button[data-tab="settings"]').click()
            default_tab = page.locator('[aria-label="Default tab on launch"] .rh-selectx-button')
            default_tab.click()
            menu = page.locator(".rh-selectx-list.open")
            menu.wait_for(state="visible")
            menu_box = menu.bounding_box()
            dock_box = page.locator("#rwDockedBar").bounding_box()
            assert menu_box and dock_box and menu_box["y"] + menu_box["height"] <= dock_box["y"], (menu_box, dock_box)
            assert menu.get_by_role("option").count() >= 7
            default_tab.press("Escape")

            page.locator('#rwMusicTabs button[data-tab="insights"]').click()
            emit(
                page,
                {
                    "type": "music_insights_result",
                    "ok": True,
                    "total_plays": "3",
                    "total_minutes": "10",
                    "unique_tracks": "2",
                    "unique_artists": "1",
                    "daily": [
                        {"date": "2026-07-11", "count": "0"},
                        {"date": "2026-07-12", "count": "3"},
                    ],
                    "top_tracks": [],
                    "top_artists": [],
                },
            )
            assert "3 plays" in page.locator(".rw-insights-strip").inner_text()
            assert page.locator(".rw-insights-bar.zero").get_attribute("style").find("height: 0%") >= 0
            assert page.locator(".rw-insights-bar-label.peak").inner_text() == "3"

            emit(
                page,
                {
                    "type": "music_open_artist",
                    "artist_id": "artist-from-player",
                    "name": "Player Artist",
                },
            )
            page.get_by_text("Player Artist", exact=True).wait_for()

            page.set_viewport_size({"width": 390, "height": 844})
            page.locator('#rwMusicTabs button[data-tab="mobile"]').click()
            steps = page.locator("#rwMusicBody .rw-mobile-step-title").all_inner_texts()
            assert steps == ["1. Download", "2. Install", "3. Pair"]
            download = page.locator('#rwMusicBody a[download="rainette-music-android.apk"]')
            assert download.inner_text() == "Download APK"
            assert download.get_attribute("href") == (
                "https://github.com/Krysis-ux/Rainette-music/releases/latest/download/"
                "rainette-music-android.apk"
            )
            assert page.get_by_text(
                "Pairing requires the installed Rainette desktop app.", exact=True
            ).is_visible()
            assert page.get_by_text(
                "Release status unavailable here. Use the official GitHub link to check for the Android app.",
                exact=True,
            ).is_visible()
            assert page.locator(".rw-mobile-grid").evaluate(
                "el => getComputedStyle(el).gridTemplateColumns.trim().split(/\\s+/).length === 1"
            )

            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
