import functools
import json
import mimetypes
import threading
import time
import urllib.request
from urllib.parse import urlparse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

# Playwright is an optional developer dependency, not part of requirements.txt.
# Importing it unguarded made the whole suite fail to *collect* on any machine
# without it -- including a fresh macOS checkout -- which hid every other test.
sync_playwright = pytest.importorskip(
    "playwright.sync_api",
    reason="playwright is not installed; run `pip install playwright && playwright install chromium`",
).sync_playwright

import main


ROOT = Path(__file__).resolve().parents[1]
WEB_DIR = ROOT / "web"
FAKE_WEBSOCKET_SCRIPT = """
(() => {
    window.__rainetteFakeWebSockets = [];
    window.__rainetteSentMessages = [];
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
        send(data) {
            try { window.__rainetteSentMessages.push(JSON.parse(String(data))); }
            catch (_error) { window.__rainetteSentMessages.push(String(data)); }
        }
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


FAKE_AUDIO_SCRIPT = """
(() => {
    window.__rainetteFakeAudioInstances = [];
    class FakeAudio extends EventTarget {
        constructor() {
            super();
            this._src = '';
            this.preload = '';
            this.volume = 1;
            this.currentTime = 0;
            this.duration = 120;
            this.readyState = 0;
            this.paused = true;
            this.error = null;
            this.playCalls = 0;
            this.pauseCalls = 0;
            this.loadCalls = 0;
            window.__rainetteFakeAudioInstances.push(this);
        }
        get src() { return this._src; }
        set src(value) {
            this._src = value ? new URL(String(value), document.baseURI).href : '';
            this.readyState = 0;
            queueMicrotask(() => this.dispatchEvent(new Event('loadstart')));
        }
        get currentSrc() { return this._src; }
        getAttribute(name) { return name === 'src' ? (this._src || null) : null; }
        removeAttribute(name) { if (name === 'src') this._src = ''; }
        play() {
            this.playCalls += 1;
            this.paused = false;
            this.readyState = 4;
            queueMicrotask(() => this.dispatchEvent(new Event('playing')));
            return Promise.resolve();
        }
        pause() {
            this.pauseCalls += 1;
            const changed = !this.paused;
            this.paused = true;
            if (changed) queueMicrotask(() => this.dispatchEvent(new Event('pause')));
        }
        load() {
            this.loadCalls += 1;
            if (this._src) {
                this.readyState = 1;
                queueMicrotask(() => this.dispatchEvent(new Event('loadedmetadata')));
            } else {
                this.readyState = 0;
                queueMicrotask(() => this.dispatchEvent(new Event('emptied')));
            }
        }
    }
    window.Audio = FakeAudio;
})();
"""


class QuietStaticHandler(SimpleHTTPRequestHandler):
    extensions_map = {
        **SimpleHTTPRequestHandler.extensions_map,
        ".js": "text/javascript",
        ".mjs": "text/javascript",
    }

    def log_message(self, _format, *_args):
        pass


class QuietThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True


def emit(page, payload):
    page.evaluate(
        "payload => document.dispatchEvent(new CustomEvent('rainette:helper-message', { detail: payload }))",
        payload,
    )


def emit_ws(page, payload):
    """Deliver a server message through music_shell's real WebSocket handler."""
    page.evaluate(
        """payload => {
            const socket = window.__rainetteFakeWebSockets.at(-1);
            if (!socket) throw new Error('fake WebSocket is not connected');
            socket.dispatchEvent(new MessageEvent('message', { data: JSON.stringify(payload) }));
        }""",
        payload,
    )


def fulfill_test_asset(route):
    parsed = urlparse(route.request.url)
    if parsed.hostname in {"127.0.0.1", "localhost"}:
        relative = parsed.path.lstrip("/") or "index.html"
        path = (WEB_DIR / relative).resolve()
        if WEB_DIR.resolve() in path.parents and path.is_file():
            content_type = (
                "text/javascript"
                if path.suffix.lower() in {".js", ".mjs"}
                else (mimetypes.guess_type(path.name)[0] or "application/octet-stream")
            )
            route.fulfill(status=200, path=str(path), content_type=content_type)
            return
    if parsed.hostname == "fonts.googleapis.com":
        route.fulfill(status=200, content_type="text/css", body="")
        return
    if parsed.hostname == "fonts.gstatic.com":
        route.abort()
        return
    route.continue_()


def open_fake_miniplayer(browser, base_url):
    """Open the real detached-player module with deterministic browser APIs."""
    page = browser.new_page(viewport={"width": 352, "height": 184})
    diagnostics = []
    page.set_default_timeout(5_000)
    page.add_init_script(FAKE_WEBSOCKET_SCRIPT)
    page.add_init_script(FAKE_AUDIO_SCRIPT)
    page.on("pageerror", lambda error: diagnostics.append(f"pageerror:{error}"))
    page.route("**/*", fulfill_test_asset)
    page.goto(base_url + "miniplayer.html", wait_until="domcontentloaded")
    page.locator("#mpPlayPill").wait_for(state="attached")
    page.wait_for_function(
        "() => (window.__rainetteSentMessages || []).some(message => message.type === 'music_request_state')"
    )
    return page, diagnostics


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
            page.wait_for_function(
                "() => window.__rainetteSentMessages.some(message => message.type === 'music_catalog_search')"
            )
            search_id = page.evaluate(
                "() => window.__rainetteSentMessages.filter(message => message.type === 'music_catalog_search').at(-1).id"
            )
            assert page.locator("#rwMusicSearchStatus .rw-kage-loader").is_visible()
            emit(
                page,
                {
                    "type": "music_catalog_search_result",
                    "id": search_id,
                    "ok": True,
                    "songs": [tracks[0]],
                    "artists": [{"id": "artist-result", "name": "Rain Artist"}],
                    "albums": [{"id": "album-result", "title": "Rain Album", "artist": "Rain Artist"}],
                },
            )
            assert page.locator("#rwMusicSearchStatus .rw-kage-loader").count() == 0
            assert page.locator("#rwMusicResults .rw-section-title h3").all_inner_texts() == [
                "SONGS",
                "ARTISTS",
                "ALBUMS",
            ]
            page.locator(".rw-search-filters").get_by_text("Artists", exact=True).click()
            assert page.locator("#rwMusicResults .rw-section-title h3").all_inner_texts() == ["ARTISTS"]

            page.evaluate("document.documentElement.classList.add('rw-reduced-motion')")
            page.evaluate(
                """() => {
                    localStorage.removeItem('rainette.miniplayerEnabled');
                    window.__miniPlayerRevealCalls = 0;
                    window.pywebview = {api: {reveal_player: () => { window.__miniPlayerRevealCalls += 1; }}};
                    window.RainetteMusic.playTrack({source_id: 'default-off', title: 'Default off'});
                }"""
            )
            assert page.evaluate("window.__miniPlayerRevealCalls") == 0
            page.evaluate(
                """() => {
                    localStorage.setItem('rainette.miniplayerEnabled', '1');
                    window.RainetteMusic.playTrack({source_id: 'opt-in', title: 'Opt in'});
                    localStorage.setItem('rainette.miniplayerEnabled', '0');
                    window.RainetteMusic.playTrack({source_id: 'live-off', title: 'Live off'});
                }"""
            )
            assert page.evaluate("window.__miniPlayerRevealCalls") == 1

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

            page.locator('#rwMusicTabs button[data-tab="playlists"]').click()
            # Match the production order: opening the tab requests the list,
            # then the backend response renders into the active view. Keeping
            # the response before the click made this smoke test depend on a
            # stale pre-navigation render during heavily loaded full-suite runs.
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

            # Three-state loop: one click loops the queue, a second repeats the one
            # song, a third turns it off. The repeat-one glyph is the loop arrows
            # plus a stroked "1" (see rainette_icons.js), so its path identifies it.
            def loop_state():
                button = page.locator("#rwDockedBar .rw-now-loop")
                return (
                    "on" in (button.get_attribute("class") or ""),
                    "M11 10h1v4" in (button.inner_html() or ""),
                )

            def now_playing(**overrides):
                emit(page, {
                    "type": "music_now_playing", "state": "playing", "playing": True,
                    "queue": tracks[:2], "index": 0, "current_time": 12, "duration": 180,
                    **overrides,
                })

            now_playing(repeat="all")
            assert loop_state() == (True, False), "queue-loop should light up without the repeat-one glyph"
            now_playing(repeat="one")
            assert loop_state() == (True, True), "repeat-one should light up and swap to the '1' glyph"
            now_playing(repeat="off")
            assert loop_state() == (False, False), "loop off should clear the button"

            # Regression: loop used to vanish as soon as the track changed. A
            # producer that says nothing about repeat (the phone has no repeat
            # control) must leave the current mode alone rather than read as "off".
            now_playing(repeat="all")
            now_playing(index=1)
            assert loop_state() == (True, False), "loop must survive a track change that omits repeat"
            now_playing(repeat="off")

            command_button = page.locator("#rwCommandOpen")
            command_box = command_button.bounding_box()
            dock_box = page.locator("#rwDockedBar").bounding_box()
            assert command_box and dock_box and command_box["y"] + command_box["height"] <= dock_box["y"], (
                command_box,
                dock_box,
            )
            hit_target = command_button.evaluate(
                """button => {
                    const rect = button.getBoundingClientRect();
                    return document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2)?.closest('#rwCommandOpen') === button;
                }"""
            )
            assert hit_target
            command_button.click()
            page.locator("#rwCommandPalette").wait_for(state="visible")
            page.keyboard.press("Escape")

            page.set_viewport_size({"width": 940, "height": 560})
            command_button.scroll_into_view_if_needed()
            command_box = command_button.bounding_box()
            dock_box = page.locator("#rwDockedBar").bounding_box()
            assert command_box and dock_box and command_box["y"] + command_box["height"] <= dock_box["y"], (
                command_box,
                dock_box,
            )
            command_button.click()
            page.locator("#rwCommandPalette").wait_for(state="visible")
            page.keyboard.press("Escape")
            page.set_viewport_size({"width": 1060, "height": 730})

            page.locator("#rwDockedBar .rw-now-popout").click()
            assert page.evaluate("window.__miniPlayerRevealCalls") == 2

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
                    "bucket_unit": "day",
                    "buckets": [
                        {"start": "2026-07-11", "end": "2026-07-11", "count": "0"},
                        {"start": "2026-07-12", "end": "2026-07-12", "count": "3"},
                    ],
                    "top_tracks": [],
                    "top_artists": [],
                },
            )
            assert "3 plays" in page.locator(".rw-insights-strip").inner_text()
            assert page.locator(".rw-insights-bar.zero").get_attribute("style").find("height: 0%") >= 0
            # Every bar carries its number directly now (no peak-only / hover reveal).
            labels = page.locator(".rw-insights-bar-label").all_inner_texts()
            assert labels == ["0", "3"], labels

            # Every value label has to clear the top of its own bar. The label used
            # to be pinned to the full-height column at a fixed `top: -4px`, so it
            # never tracked the bar - and the tallest bar (height:100%) grew
            # straight into its own label, printing the number on top of the accent
            # fill. Assert the geometry, not just the text.
            page.wait_for_timeout(600)  # let the bar grow animation settle
            peak_col = page.locator(".rw-insights-col").nth(1)
            assert peak_col.locator(".rw-insights-bar-label").inner_text() == "3"
            label_box = peak_col.locator(".rw-insights-bar-label").bounding_box()
            bar_box = peak_col.locator(".rw-insights-bar").bounding_box()
            assert label_box["y"] + label_box["height"] <= bar_box["y"] + 0.5, (
                f"insights label overlaps its bar: {label_box} vs {bar_box}"
            )
            # ...and must not be clipped out of the top of the card either.
            card_box = page.locator(".rw-insights-chart-card").bounding_box()
            assert label_box["y"] >= card_box["y"], (label_box, card_box)

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
            page.evaluate("delete window.pywebview")
            page.locator('#rwMusicTabs button[data-tab="mobile"]').click()
            steps = page.locator("#rwMusicBody .rw-mobile-step-title").all_inner_texts()
            assert steps == ["1. Open", "2. Make this computer reachable", "3. Pair"]
            pwa_link = page.locator("#rwPwaLink")
            assert pwa_link.inner_text() == "Open the Rainette PWA"
            assert pwa_link.get_attribute("href") == "https://music-pwa-web.vercel.app"
            assert page.get_by_text(
                "Pairing requires the installed Rainette desktop app.", exact=True
            ).is_visible()
            # Without the desktop bridge the panel must not pretend it can pair.
            assert page.locator("#rwNewPairingCode").is_disabled()
            assert page.locator("#rwPairingLink").is_hidden()
            assert page.locator(".rw-mobile-grid").evaluate(
                "el => getComputedStyle(el).gridTemplateColumns.trim().split(/\\s+/).length === 1"
            )

            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_track_row_reflects_now_playing_state_and_toggles_in_place():
    """Track rows never reflected playback state at all: the play button
    always showed the play icon and there was no "now playing" indicator,
    regardless of whether that exact track was the one currently playing.

    Also guards a stale-closure bug in the fix: the click handler must
    re-check live now-playing state at click time, not a snapshot captured
    when the row was first built - otherwise clicking the active row's own
    button restarts the queue instead of toggling play/pause.
    """
    handler = functools.partial(QuietStaticHandler, directory=str(WEB_DIR))
    server = QuietThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/?remote=1"
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1060, "height": 730})
            diagnostics = []
            page.add_init_script(FAKE_WEBSOCKET_SCRIPT)
            page.on("pageerror", lambda error: diagnostics.append(f"pageerror:{error}"))
            page.route("**/*", fulfill_test_asset)
            page.set_default_timeout(7_000)
            page.goto(url, wait_until="domcontentloaded")
            page.locator("#rwMusicTabs").wait_for(state="visible")
            page.locator('#rwMusicTabs button[data-tab="recent"]').click()

            tracks = sample_tracks()
            emit(page, {"type": "music_recent_result", "ok": True, "tracks": tracks})
            page.locator(".rw-track-card").first.wait_for(state="visible")

            def row(i):
                return page.locator(".rw-track-card").nth(i)

            def is_pause_icon(locator):
                # The pause glyph is two solid bars (see rainette_icons.js); the
                # play glyph is a single triangle - distinct path data either way.
                return "M7 5h4v14H7z" in locator.locator(".rw-play-action").inner_html()

            assert "now-playing" not in (row(0).get_attribute("class") or "")
            assert not is_pause_icon(row(0))

            # This row is built BEFORE anything is playing, then becomes the
            # active track - the scenario that exposed the stale-closure bug.
            emit(page, {
                "type": "music_now_playing", "state": "playing", "playing": True,
                "queue": tracks[:3], "index": 0, "current_time": 5, "duration": 180,
            })
            page.wait_for_timeout(150)
            assert "now-playing" in row(0).get_attribute("class")
            assert row(0).locator(".rw-track-now-badge").is_visible()
            assert is_pause_icon(row(0))
            assert "now-playing" not in (row(1).get_attribute("class") or "")

            row(0).locator(".rw-play-action").click()
            page.wait_for_timeout(100)
            sent = page.evaluate("() => window.__rainetteSentMessages || []")
            toggles = [m for m in sent if isinstance(m, dict)
                      and m.get("type") == "music_remote_control" and m.get("action") == "toggle"]
            assert len(toggles) == 1, (
                "clicking the active row's own button must toggle play/pause in place, "
                f"not restart the queue - messages sent: {sent[-3:]}"
            )

            # Pausing keeps this as the current track: its button becomes Play,
            # the badge says Paused, and clicking resumes with toggle instead
            # of sending a fresh music_remote_play that rewinds the track.
            emit(page, {
                "type": "music_now_playing", "state": "paused", "playing": False,
                "queue": tracks[:3], "index": 0, "current_time": 12, "duration": 180,
            })
            page.wait_for_timeout(100)
            assert "now-playing" in row(0).get_attribute("class")
            assert row(0).locator(".rw-track-now-badge").text_content() == "Paused"
            assert not is_pause_icon(row(0))
            row(0).locator(".rw-play-action").click()
            page.wait_for_timeout(100)
            sent = page.evaluate("() => window.__rainetteSentMessages || []")
            toggles = [m for m in sent if isinstance(m, dict)
                      and m.get("type") == "music_remote_control" and m.get("action") == "toggle"]
            assert len(toggles) == 2

            # Loading is current too. It must not lose row feedback or restart
            # the queue when the user presses its cancel/pause affordance.
            emit(page, {
                "type": "music_now_playing", "state": "loading", "playing": False,
                "queue": tracks[:3], "index": 0, "current_time": 0, "duration": 180,
            })
            page.wait_for_timeout(100)
            assert row(0).locator(".rw-track-now-badge").text_content() == "Loading"
            assert is_pause_icon(row(0))
            row(0).locator(".rw-play-action").click()
            page.wait_for_timeout(100)
            sent = page.evaluate("() => window.__rainetteSentMessages || []")
            toggles = [m for m in sent if isinstance(m, dict)
                      and m.get("type") == "music_remote_control" and m.get("action") == "toggle"]
            assert len(toggles) == 3

            # A progress tick can be the first post-reconnect proof that audio
            # is playing. Recover the queue/row presentation from it even when
            # the matching now_playing broadcast was missed.
            emit(page, {
                "type": "music_now_playing", "state": "paused", "playing": False,
                "queue": tracks[:3], "index": 0, "current_time": 12, "duration": 180,
            })
            emit(page, {
                "type": "music_progress", "playing": True, "source_id": tracks[0]["source_id"],
                "current_time": 13, "duration": 180,
            })
            page.wait_for_timeout(100)
            assert row(0).locator(".rw-track-now-badge").text_content() == "Now playing"
            assert is_pause_icon(row(0))

            # Switching the active track must revert the old row and activate the new one.
            emit(page, {
                "type": "music_now_playing", "state": "playing", "playing": True,
                "queue": tracks[:3], "index": 1, "current_time": 0, "duration": 180,
            })
            page.wait_for_timeout(150)
            assert "now-playing" not in (row(0).get_attribute("class") or "")
            assert not is_pause_icon(row(0))
            assert "now-playing" in row(1).get_attribute("class")
            assert is_pause_icon(row(1))

            # Heavy Rotation builds a custom track row; it must carry the same
            # status badge as every standard track card.
            page.locator('#rwMusicTabs button[data-tab="insights"]').click()
            emit(page, {
                "type": "music_insights_result", "ok": True,
                "total_plays": 3, "total_minutes": 10,
                "unique_tracks": 1, "unique_artists": 1,
                "bucket_unit": "day", "buckets": [], "top_tracks": [{**tracks[1], "play_count": 3}], "top_artists": [],
            })
            page.wait_for_timeout(100)
            insight_row = page.locator(".rw-insights-rank-row").first
            assert insight_row.locator(".rw-track-now-badge").text_content() == "Now playing"
            assert insight_row.locator(".rw-track-now-badge").is_visible()

            assert diagnostics == [], diagnostics
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_miniplayer_terminal_stream_failure_survives_state_request():
    """A failed replacement track must not fall back to stale playing state."""
    handler = functools.partial(QuietStaticHandler, directory=str(WEB_DIR))
    server = QuietThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}/"
    track_a = {
        "id": "track-a",
        "source": "youtube",
        "source_id": "source-a",
        "title": "Track A",
        "artist": "Artist A",
        "duration_s": 120,
    }
    track_b = {
        "id": "track-b",
        "source": "youtube",
        "source_id": "source-b",
        "title": "Track B",
        "artist": "Artist B",
        "duration_s": 150,
    }
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page, diagnostics = open_fake_miniplayer(browser, base)

            # Establish a genuinely playing predecessor first. This catches the
            # stale-state regression that only appears when the next load fails.
            emit_ws(page, {"type": "music_remote_play", "tracks": [track_a], "index": 0})
            page.wait_for_function(
                "sourceId => window.__rainetteSentMessages.some(message => "
                "message.type === 'music_stream_url' && message.source_id === sourceId && !message.prefetch)",
                arg=track_a["source_id"],
            )
            request_a = page.evaluate(
                "sourceId => window.__rainetteSentMessages.filter(message => "
                "message.type === 'music_stream_url' && message.source_id === sourceId && !message.prefetch).at(-1)",
                track_a["source_id"],
            )
            emit_ws(page, {
                "type": "music_stream_url_result",
                "id": request_a["id"],
                "ok": True,
                "source_id": track_a["source_id"],
                "url": "https://audio.invalid/a.mp3",
            })
            page.wait_for_function(
                "sourceId => window.__rainetteSentMessages.some(message => "
                "message.type === 'music_now_playing_set' && message.track?.source_id === sourceId "
                "&& message.state === 'playing' && message.playing === true)",
                arg=track_a["source_id"],
            )

            emit_ws(page, {"type": "music_remote_play", "tracks": [track_a, track_b], "index": 1})
            page.wait_for_function(
                "sourceId => window.__rainetteSentMessages.filter(message => "
                "message.type === 'music_stream_url' && message.source_id === sourceId && !message.prefetch).length === 1",
                arg=track_b["source_id"],
            )
            first_b_request = page.evaluate(
                "sourceId => window.__rainetteSentMessages.filter(message => "
                "message.type === 'music_stream_url' && message.source_id === sourceId && !message.prefetch).at(-1)",
                track_b["source_id"],
            )
            assert first_b_request["force_refresh"] is False
            emit_ws(page, {
                "type": "music_stream_url_result",
                "id": first_b_request["id"],
                "ok": False,
                "msg": "initial resolve failed",
            })

            page.wait_for_function(
                "sourceId => window.__rainetteSentMessages.filter(message => "
                "message.type === 'music_stream_url' && message.source_id === sourceId && !message.prefetch).length === 2",
                arg=track_b["source_id"],
            )
            retry_b_request = page.evaluate(
                "sourceId => window.__rainetteSentMessages.filter(message => "
                "message.type === 'music_stream_url' && message.source_id === sourceId && !message.prefetch).at(-1)",
                track_b["source_id"],
            )
            assert retry_b_request["id"] != first_b_request["id"]
            assert retry_b_request["force_refresh"] is True
            emit_ws(page, {
                "type": "music_stream_url_result",
                "id": retry_b_request["id"],
                "ok": False,
                "msg": "retry failed",
            })
            page.wait_for_function(
                "sourceId => window.__rainetteSentMessages.some(message => "
                "message.type === 'music_now_playing_set' && message.track?.source_id === sourceId "
                "&& message.state === 'error' && message.playing === false)",
                arg=track_b["source_id"],
            )

            before_request = page.evaluate(
                "() => window.__rainetteSentMessages.filter(message => "
                "message.type === 'music_now_playing_set').length"
            )
            emit_ws(page, {"type": "music_remote_control", "action": "queue_request_state"})
            page.wait_for_function(
                "count => window.__rainetteSentMessages.filter(message => "
                "message.type === 'music_now_playing_set').length > count",
                arg=before_request,
            )
            reported = page.evaluate(
                "() => window.__rainetteSentMessages.filter(message => "
                "message.type === 'music_now_playing_set').at(-1)"
            )
            assert reported["track"]["source_id"] == track_b["source_id"]
            assert reported["state"] == "error"
            assert reported["playing"] is False

            for selector in (".mp-play", "#mpPlayPill"):
                paths = page.locator(f"{selector} path")
                assert paths.count() == 1, f"{selector} must render the single-path Play glyph"
                assert paths.first.get_attribute("d") == "M8 5v14l11-7-11-7z"
            assert page.locator(".mp-artist").text_content() == "Playback failed"
            assert diagnostics == [], diagnostics
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_miniplayer_toggle_during_resolution_keeps_successful_load_paused():
    """Canceling autoplay while resolving must survive the late URL response."""
    handler = functools.partial(QuietStaticHandler, directory=str(WEB_DIR))
    server = QuietThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}/"
    track_b = {
        "id": "track-b",
        "source": "youtube",
        "source_id": "source-b",
        "title": "Track B",
        "artist": "Artist B",
        "duration_s": 150,
    }
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page, diagnostics = open_fake_miniplayer(browser, base)

            emit_ws(page, {"type": "music_remote_play", "tracks": [track_b], "index": 0})
            page.wait_for_function(
                "sourceId => window.__rainetteSentMessages.some(message => "
                "message.type === 'music_stream_url' && message.source_id === sourceId && !message.prefetch)",
                arg=track_b["source_id"],
            )
            pending_request = page.evaluate(
                "sourceId => window.__rainetteSentMessages.filter(message => "
                "message.type === 'music_stream_url' && message.source_id === sourceId && !message.prefetch).at(-1)",
                track_b["source_id"],
            )

            emit_ws(page, {"type": "music_remote_control", "action": "toggle"})
            page.wait_for_function(
                "sourceId => window.__rainetteSentMessages.some(message => "
                "message.type === 'music_now_playing_set' && message.track?.source_id === sourceId "
                "&& message.state === 'paused' && message.playing === false)",
                arg=track_b["source_id"],
            )
            paused_before_resolution = page.evaluate(
                "sourceId => window.__rainetteSentMessages.filter(message => "
                "message.type === 'music_now_playing_set' && message.track?.source_id === sourceId "
                "&& message.state === 'paused' && message.playing === false).length",
                track_b["source_id"],
            )

            emit_ws(page, {
                "type": "music_stream_url_result",
                "id": pending_request["id"],
                "ok": True,
                "source_id": track_b["source_id"],
                "url": "https://audio.invalid/b.mp3",
            })
            page.wait_for_function(
                "args => window.__rainetteSentMessages.filter(message => "
                "message.type === 'music_now_playing_set' && message.track?.source_id === args.sourceId "
                "&& message.state === 'paused' && message.playing === false).length > args.count",
                arg={"sourceId": track_b["source_id"], "count": paused_before_resolution},
            )
            page.wait_for_function(
                "() => window.__rainetteFakeAudioInstances.at(-1)?.loadCalls >= 1"
            )
            media = page.evaluate(
                "() => { const audio = window.__rainetteFakeAudioInstances.at(-1); return { "
                "src: audio.src, paused: audio.paused, playCalls: audio.playCalls, loadCalls: audio.loadCalls }; }"
            )
            assert media == {
                "src": "https://audio.invalid/b.mp3",
                "paused": True,
                "playCalls": 0,
                "loadCalls": 1,
            }
            playing_broadcasts = page.evaluate(
                "sourceId => window.__rainetteSentMessages.filter(message => "
                "message.type === 'music_now_playing_set' && message.track?.source_id === sourceId "
                "&& message.state === 'playing' && message.playing === true).length",
                track_b["source_id"],
            )
            assert playing_broadcasts == 0

            before_request = page.evaluate(
                "() => window.__rainetteSentMessages.filter(message => "
                "message.type === 'music_now_playing_set').length"
            )
            emit_ws(page, {"type": "music_remote_control", "action": "queue_request_state"})
            page.wait_for_function(
                "count => window.__rainetteSentMessages.filter(message => "
                "message.type === 'music_now_playing_set').length > count",
                arg=before_request,
            )
            reported = page.evaluate(
                "() => window.__rainetteSentMessages.filter(message => "
                "message.type === 'music_now_playing_set').at(-1)"
            )
            assert reported["track"]["source_id"] == track_b["source_id"]
            assert reported["state"] == "paused"
            assert reported["playing"] is False
            assert diagnostics == [], diagnostics
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_both_playback_windows_resynchronize_after_websocket_reconnect():
    """A remote command broadcast while either WebView is disconnected is
    otherwise lost forever. Each side must request the authoritative state on
    every socket open, not only once during its initial module boot.
    """
    handler = functools.partial(QuietStaticHandler, directory=str(WEB_DIR))
    server = QuietThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}/"
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)

            player = browser.new_page()
            player.set_default_timeout(5_000)
            player.add_init_script(FAKE_WEBSOCKET_SCRIPT)
            player.goto(base + "miniplayer.html", wait_until="domcontentloaded")
            player.wait_for_function(
                "() => (window.__rainetteSentMessages || []).some(m => m.type === 'music_request_state')"
            )
            first_player_requests = player.evaluate(
                "() => window.__rainetteSentMessages.filter(m => m.type === 'music_request_state').length"
            )
            player.evaluate("() => window.__rainetteFakeWebSockets.at(-1).close()")
            player.wait_for_function("() => window.__rainetteFakeWebSockets.length >= 2", timeout=4_000)
            player.wait_for_function(
                "count => window.__rainetteSentMessages.filter(m => m.type === 'music_request_state').length > count",
                arg=first_player_requests,
            )

            main_page = browser.new_page()
            main_page.set_default_timeout(5_000)
            main_page.add_init_script(FAKE_WEBSOCKET_SCRIPT)
            main_page.route("**/*", fulfill_test_asset)
            main_page.goto(base + "?remote=1", wait_until="domcontentloaded")
            main_page.locator("#rwMusicTabs").wait_for(state="visible")
            main_page.wait_for_function(
                "() => (window.__rainetteSentMessages || []).some(m => m.type === 'music_remote_control' && m.action === 'queue_request_state')"
            )
            first_main_requests = main_page.evaluate(
                "() => window.__rainetteSentMessages.filter(m => m.type === 'music_remote_control' && m.action === 'queue_request_state').length"
            )
            main_page.evaluate("() => window.__rainetteFakeWebSockets.at(-1).close()")
            main_page.wait_for_function("() => window.__rainetteFakeWebSockets.length >= 2", timeout=4_000)
            main_page.wait_for_function(
                "count => window.__rainetteSentMessages.filter(m => m.type === 'music_remote_control' && m.action === 'queue_request_state').length > count",
                arg=first_main_requests,
            )

            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_state_handshake_restores_paused_transport_without_blocking_active_recovery():
    """Reconnect recovery must carry transport intent, not masquerade as a
    fresh user-initiated play. A paused queue stays paused after a cold player
    restore, while a queue that was still loading is allowed to finish and
    begin playback.
    """
    handler = functools.partial(QuietStaticHandler, directory=str(WEB_DIR))
    server = QuietThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}/"
    paused_track = {
        "id": "paused-track",
        "source": "youtube",
        "source_id": "paused-source",
        "title": "Paused Track",
        "artist": "Quiet Artist",
        "duration_s": 120,
    }
    loading_track = {
        "id": "loading-track",
        "source": "youtube",
        "source_id": "loading-source",
        "title": "Loading Track",
        "artist": "Active Artist",
        "duration_s": 150,
    }
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)

            main_page = browser.new_page(viewport={"width": 1060, "height": 730})
            main_page.set_default_timeout(5_000)
            main_page.add_init_script(FAKE_WEBSOCKET_SCRIPT)
            main_page.route("**/*", fulfill_test_asset)
            main_page.goto(base + "?remote=1", wait_until="domcontentloaded")
            main_page.locator("#rwMusicTabs").wait_for(state="visible")

            def request_restore(track, state, playing, *, cold_idle_first=False):
                emit(main_page, {
                    "type": "music_now_playing",
                    "state": state,
                    "playing": playing,
                    "track": track,
                    "queue": [track],
                    "index": 0,
                    "current_time": 37 if state == "paused" else 0,
                    "duration": track["duration_s"],
                })
                if cold_idle_first:
                    # Reproduces the real two-window race: the main window asks
                    # for player state on reconnect, the newly loaded player
                    # answers from its empty idle queue, then asks the main for
                    # the cached queue. That idle response must not turn a
                    # cached pause into a loading/autoplay restore.
                    emit(main_page, {
                        "type": "music_now_playing",
                        "state": "idle",
                        "playing": False,
                        "track": None,
                        "queue": [],
                        "index": -1,
                        "current_time": 0,
                        "duration": 0,
                    })
                before = main_page.evaluate(
                    "() => window.__rainetteSentMessages.filter(message => "
                    "message.type === 'music_remote_play').length"
                )
                emit(main_page, {"type": "music_request_state"})
                main_page.wait_for_function(
                    "count => window.__rainetteSentMessages.filter(message => "
                    "message.type === 'music_remote_play').length > count",
                    arg=before,
                )
                command = main_page.evaluate(
                    "() => window.__rainetteSentMessages.filter(message => "
                    "message.type === 'music_remote_play').at(-1)"
                )
                assert command["restore_state"] == state
                assert command["playing"] is playing
                return command

            paused_restore = request_restore(
                paused_track, "paused", False, cold_idle_first=True
            )
            player, diagnostics = open_fake_miniplayer(browser, base)
            emit_ws(player, paused_restore)
            player.wait_for_function(
                "sourceId => window.__rainetteSentMessages.some(message => "
                "message.type === 'music_stream_url' && message.source_id === sourceId && !message.prefetch)",
                arg=paused_track["source_id"],
            )
            paused_request = player.evaluate(
                "sourceId => window.__rainetteSentMessages.filter(message => "
                "message.type === 'music_stream_url' && message.source_id === sourceId && !message.prefetch).at(-1)",
                paused_track["source_id"],
            )
            emit_ws(player, {
                "type": "music_stream_url_result",
                "id": paused_request["id"],
                "ok": True,
                "source_id": paused_track["source_id"],
                "url": "https://audio.invalid/paused.mp3",
            })
            player.wait_for_function(
                "sourceId => window.__rainetteSentMessages.some(message => "
                "message.type === 'music_now_playing_set' && message.track?.source_id === sourceId "
                "&& message.state === 'paused' && message.playing === false)",
                arg=paused_track["source_id"],
            )
            paused_audio = player.evaluate(
                "() => { const audio = window.__rainetteFakeAudioInstances.at(-1); return { "
                "paused: audio.paused, playCalls: audio.playCalls, loadCalls: audio.loadCalls }; }"
            )
            assert paused_audio == {"paused": True, "playCalls": 0, "loadCalls": 1}

            # The handshake carries all three live transport states. Loading
            # remains distinct from paused even though both have playing=false.
            request_restore(loading_track, "playing", True)
            loading_restore = request_restore(loading_track, "loading", False)
            emit_ws(player, loading_restore)
            player.wait_for_function(
                "sourceId => window.__rainetteSentMessages.some(message => "
                "message.type === 'music_stream_url' && message.source_id === sourceId && !message.prefetch)",
                arg=loading_track["source_id"],
            )
            loading_request = player.evaluate(
                "sourceId => window.__rainetteSentMessages.filter(message => "
                "message.type === 'music_stream_url' && message.source_id === sourceId && !message.prefetch).at(-1)",
                loading_track["source_id"],
            )
            emit_ws(player, {
                "type": "music_stream_url_result",
                "id": loading_request["id"],
                "ok": True,
                "source_id": loading_track["source_id"],
                "url": "https://audio.invalid/loading.mp3",
            })
            player.wait_for_function(
                "sourceId => window.__rainetteSentMessages.some(message => "
                "message.type === 'music_now_playing_set' && message.track?.source_id === sourceId "
                "&& message.state === 'playing' && message.playing === true)",
                arg=loading_track["source_id"],
            )
            recovered_audio = player.evaluate(
                "() => { const audio = window.__rainetteFakeAudioInstances.at(-1); return { "
                "paused: audio.paused, playCalls: audio.playCalls }; }"
            )
            assert recovered_audio == {"paused": False, "playCalls": 1}
            assert diagnostics == [], diagnostics
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_miniplayer_loop_button_renders_a_glyph_in_every_repeat_state():
    """Regression: the detached player's ICON map lacked a `loopOne` entry, so
    cycling repeat to 'one' assigned `undefined` to the button's innerHTML and
    rendered the literal word "undefined" inside the loop control. Every repeat
    state must render an <svg> glyph and never that text.
    """
    handler = functools.partial(QuietStaticHandler, directory=str(WEB_DIR))
    server = QuietThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}/"
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page, diagnostics = open_fake_miniplayer(browser, base)
            loop = page.locator('[data-act="loop"]')

            for mode, expected_label in (
                ("all", "Looping the queue — click to repeat this song"),
                ("one", "Repeating this song — click to stop looping"),
                ("off", "Loop off — click to loop the queue"),
            ):
                emit_ws(page, {"type": "music_remote_control", "action": "set_repeat", "mode": mode})
                page.wait_for_function(
                    "label => document.querySelector('[data-act=\"loop\"]')"
                    "?.getAttribute('aria-label') === label",
                    arg=expected_label,
                )
                markup = loop.inner_html()
                assert "<svg" in markup, f"repeat '{mode}' must render an SVG glyph, got: {markup!r}"
                assert "undefined" not in markup, f"repeat '{mode}' rendered the literal 'undefined'"
                assert loop.locator("path").count() > 0, f"repeat '{mode}' glyph must have paths"

            assert diagnostics == [], diagnostics
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_update_badge_stays_hidden_and_binds_despite_late_pywebview_injection():
    """Two regressions in the update badge, both confirmed and fixed:

    1. `.rw-update-badge { display: inline-flex }` had equal CSS specificity to
       the browser's `[hidden] { display: none }` rule, so author CSS won the
       cascade tie and the badge showed "Update available" permanently
       regardless of the hidden attribute or any check result.
    2. bindUpdater() gated attaching the click listener on `window.pywebview`
       being truthy at mount time. pywebview injects window.pywebview
       asynchronously, so if it arrived even slightly after mount(), the click
       listener was never attached at all - "clicking Update does nothing"
       with no error, because nothing was listening.
    """
    handler = functools.partial(QuietStaticHandler, directory=str(WEB_DIR))
    server = QuietThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/?remote=1"
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1060, "height": 730})
            diagnostics = []
            page.add_init_script(FAKE_WEBSOCKET_SCRIPT)
            page.on("pageerror", lambda error: diagnostics.append(f"pageerror:{error}"))
            page.route("**/*", fulfill_test_asset)
            page.set_default_timeout(7_000)
            page.goto(url, wait_until="domcontentloaded")
            page.locator("#rwMusicTabs").wait_for(state="visible")

            badge = page.locator("#rwUpdateBadge")
            assert badge.evaluate("el => el.hidden") is True
            assert badge.evaluate("el => getComputedStyle(el).display") == "none"
            assert not badge.is_visible(), "badge must not render before any update check has run"

            # Inject pywebview.api LATE, after mount()/bindUpdater() already ran -
            # the exact race that used to leave the click listener unattached.
            page.wait_for_timeout(200)
            candidate_id = "a" * 64
            page.evaluate(
                "candidateId => {"
                "  window.__checkCalls = 0; window.__appliedCandidate = null;"
                "  window.__updatePoll = null; const realSetInterval = window.setInterval.bind(window);"
                "  window.setInterval = (fn, ms) => { window.__updatePoll = fn; return realSetInterval(() => {}, ms); };"
                "  window.pywebview = { api: {"
                "    check_for_updates: async () => { window.__checkCalls++; return {"
                "      status: 'update', current: '0.2.2', latest: '0.9.0', candidate_id: candidateId"
                "    }; },"
                "    apply_update: id => { window.__appliedCandidate = id; return new Promise(resolve => { window.__finishApply = resolve; }); },"
                "  } }; window.dispatchEvent(new Event('pywebviewready'));"
                "}",
                candidate_id,
            )
            badge.wait_for(state="visible")
            assert "0.9.0" in badge.inner_text()

            page.locator('#rwMusicTabs button[data-tab="settings"]').click()
            update_card = page.locator("#rwUpdateSettings")
            update_card.wait_for(state="visible")
            assert "0.9.0 is ready" in update_card.inner_text()
            update_card.get_by_role("button", name="Check again").click()
            page.wait_for_function("() => window.__checkCalls >= 2")

            badge.click()
            page.get_by_text("Update Rainette Music").wait_for(state="visible")
            page.locator(".rw-modal .rw-btn-primary").click()
            page.wait_for_function("() => window.__appliedCandidate !== null")
            assert page.evaluate("() => window.__appliedCandidate") == candidate_id, (
                "apply_update did not receive the exact candidate returned by the check"
            )

            checks_before_install_poll = page.evaluate("() => window.__checkCalls")
            page.evaluate("() => window.__updatePoll()")
            page.wait_for_timeout(100)
            assert page.evaluate("() => window.__checkCalls") == checks_before_install_poll, (
                "the automatic poll replaced updater state during an active installation"
            )
            assert update_card.get_by_role("button", name="Checking...").count() == 0
            assert update_card.get_by_role("button", name="Check again").is_disabled()
            page.evaluate("() => window.__finishApply({status: 'installing', version: '0.9.0'})")

            assert diagnostics == [], diagnostics
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_settings_manual_update_check_reports_current_without_showing_badge():
    handler = functools.partial(QuietStaticHandler, directory=str(WEB_DIR))
    server = QuietThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/?remote=1"
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1060, "height": 730})
            page.add_init_script(FAKE_WEBSOCKET_SCRIPT)
            page.add_init_script(
                """
                window.__checkCalls = 0;
                window.pywebview = {api: {
                    check_for_updates: async () => {
                        window.__checkCalls += 1;
                        return {status: 'current', current: '0.2.2'};
                    }
                }};
                """
            )
            page.route("**/*", fulfill_test_asset)
            page.set_default_timeout(7_000)
            page.goto(url, wait_until="domcontentloaded")
            page.locator("#rwMusicTabs").wait_for(state="visible")
            page.wait_for_function("() => window.__checkCalls >= 1")

            assert not page.locator("#rwUpdateBadge").is_visible()
            page.locator('#rwMusicTabs button[data-tab="settings"]').click()
            card = page.locator("#rwUpdateSettings")
            card.wait_for(state="visible")
            assert "Rainette is up to date" in card.inner_text()
            assert "0.2.2" in card.inner_text()
            assert "Krysis-ux/Rainette-music" in card.inner_text()

            before = page.evaluate("() => window.__checkCalls")
            card.get_by_role("button", name="Check again").click()
            page.wait_for_function("count => window.__checkCalls > count", arg=before)
            assert not page.locator("#rwUpdateBadge").is_visible()
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_source_run_points_at_the_repository_instead_of_installing():
    """A build that cannot install updates must route the user to the repo.

    check_for_updates() returns this payload verbatim for a source run (and,
    with different copy, for a build missing its release key), so the reason
    has to survive into the card and leave no install button behind - the
    release link is the only way forward.
    """
    handler = functools.partial(QuietStaticHandler, directory=str(WEB_DIR))
    server = QuietThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/?remote=1"
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1060, "height": 730})
            page.add_init_script(FAKE_WEBSOCKET_SCRIPT)
            page.add_init_script(
                """
                window.__checkCalls = 0;
                window.pywebview = {api: {
                    check_for_updates: async () => {
                        window.__checkCalls += 1;
                        return %s;
                    },
                    apply_update: () => { throw new Error('an unoffered update must never be applied'); },
                }};
                """
                % json.dumps({
                    "status": "unavailable",
                    "current": "0.2.2",
                    "msg": main.SOURCE_RUN_UPDATE_MSG,
                })
            )
            page.route("**/*", fulfill_test_asset)
            page.set_default_timeout(7_000)
            page.goto(url, wait_until="domcontentloaded")
            page.locator("#rwMusicTabs").wait_for(state="visible")
            page.wait_for_function("() => window.__checkCalls >= 1")

            assert not page.locator("#rwUpdateBadge").is_visible(), (
                "an update that cannot be installed must never advertise itself"
            )
            page.locator('#rwMusicTabs button[data-tab="settings"]').click()
            card = page.locator("#rwUpdateSettings")
            card.wait_for(state="visible")
            assert "Updates are unavailable here" in card.inner_text()
            assert main.SOURCE_RUN_UPDATE_MSG in card.inner_text(), (
                "the card dropped the reason and fell back to generic copy"
            )
            assert not card.get_by_role("button", name="Download and install").is_visible()
            link = card.get_by_role("link", name="Krysis-ux/Rainette-music")
            assert link.is_visible(), "no download route offered in place of the install button"
            assert link.get_attribute("href").startswith("https://github.com/Krysis-ux/Rainette-music")
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_update_install_shows_progress_then_failure_reaches_the_user():
    """The install flow's two halves, driven through the real page code:

    1. apply_update() returns "installing" optimistically; the UI must poll
       update_progress() and render a real percentage while downloading.
    2. A late verification failure is only observable via that poll, so it must
       still surface as user-visible error copy - not leave the badge spinning.
    """
    handler = functools.partial(QuietStaticHandler, directory=str(WEB_DIR))
    server = QuietThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/?remote=1"
    candidate_id = "c" * 64
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1060, "height": 730})
            page.add_init_script(FAKE_WEBSOCKET_SCRIPT)
            page.add_init_script(
                """
                window.__progressPhase = {phase: 'downloading', received: 0, total: 100, version: '9.9.9'};
                window.pywebview = {api: {
                    check_for_updates: async () => ({
                        status: 'update', current: '0.2.2', latest: '9.9.9',
                        candidate_id: '%s', release_id: 900,
                    }),
                    apply_update: async () => ({status: 'installing', version: '9.9.9'}),
                    update_progress: async () => window.__progressPhase,
                }};
                """ % candidate_id
            )
            page.route("**/*", fulfill_test_asset)
            page.set_default_timeout(7_000)
            page.goto(url, wait_until="domcontentloaded")
            page.locator("#rwMusicTabs").wait_for(state="visible")

            badge = page.locator("#rwUpdateBadge")
            badge.wait_for(state="visible")
            assert badge.inner_text().strip() == "Update to 9.9.9"

            badge.click()
            page.get_by_role("button", name="Update now").click()

            page.evaluate(
                "() => { window.__progressPhase = {phase: 'downloading', received: 42, total: 100, version: '9.9.9'}; }"
            )
            page.wait_for_function(
                "() => document.querySelector('#rwUpdateBadge')?.textContent.includes('Downloading 42%')"
            )
            fill = badge.evaluate("el => el.style.getPropertyValue('--rw-update-progress')")
            assert fill == "42%", f"badge progress fill should track bytes, got {fill!r}"

            page.evaluate(
                "() => { window.__progressPhase = {phase: 'failed', code: 'verification_or_launch_failed',"
                " message: 'Rainette could not verify or start the update. Please try again.'}; }"
            )
            page.get_by_text("Update failed", exact=True).wait_for()
            assert page.get_by_text(
                "Rainette could not verify or start the update. Please try again."
            ).first.is_visible(), "the late failure must reach the user, not just the console"
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
