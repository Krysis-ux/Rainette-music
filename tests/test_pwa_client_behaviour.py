"""The phone client, driven in a real browser against a fake computer.

The fake answers a command over HTTP *and* echoes the same result back on the
event stream, which is what the desktop genuinely does — every catalog result
fans out to all paired devices, including the one that asked. Reproducing that
here is the point: the client used to answer each echo with a fresh request and
repaint the list forever.
"""

import json

import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import pytest

sync_playwright = pytest.importorskip(
    "playwright.sync_api",
    reason="playwright is not installed; run `pip install playwright && playwright install chromium`",
).sync_playwright

ROOT = Path(__file__).resolve().parents[1]
PWA_DIR = ROOT / "pwa"
ENDPOINT = "https://companion.test"

TRACKS = [
    {"source_id": f"t{n}", "title": f"Track {n}", "artist": "Tester", "duration_s": 120 + n}
    for n in range(1, 9)
]

SYNCED_LRC = "[00:00.50]First line\n[00:05.00]Second line\n[00:10.00]Third line\n"


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


class FakeComputer:
    """Just enough of the companion API, with the desktop's echo behaviour."""

    def __init__(self):
        self.commands = []
        self.events = []
        self.revision = 0
        self.lock = threading.Lock()

    def publish(self, message):
        with self.lock:
            self.revision += 1
            self.events.append({"revision": self.revision, "message": message})

    def handle_command(self, payload):
        kind = payload.get("type")
        with self.lock:
            self.commands.append(kind)
        request_id = payload.get("id")

        if kind == "music_library_index":
            result = {"ok": True, "id": request_id, "tracks": TRACKS}
            # The desktop fans the same result out to every paired device.
            self.publish({"type": "music_library_index_result", **result})
            return result
        if kind == "music_recent":
            return {"ok": True, "id": request_id, "tracks": TRACKS[:3]}
        if kind == "music_playlist_list":
            return {"ok": True, "id": request_id, "playlists": [
                {"id": "p1", "name": "Late night", "track_count": 4},
            ]}
        if kind == "music_playlist_tracks":
            return {"ok": True, "id": request_id, "tracks": TRACKS[4:]}
        if kind == "music_lyrics":
            return {"ok": True, "id": request_id, "plain": "First line\nSecond line\nThird line",
                    "synced": SYNCED_LRC, "instrumental": False}
        if kind == "music_stream_url":
            # One grant per track, as the real relay hands out.
            return {"ok": True, "id": request_id,
                    "url": f"/audio/{payload.get('source_id')}", "expires_hint_s": 3600}
        if kind == "music_output_devices":
            return {"ok": True, "id": request_id, "devices": [
                {"id": "spk", "name": "Kitchen speaker", "kind": "bluetooth", "is_default": False},
            ]}
        return {"ok": True, "id": request_id}

    def read_events(self, after):
        deadline = time.monotonic() + 0.4
        while time.monotonic() < deadline:
            with self.lock:
                pending = [e for e in self.events if e["revision"] > after]
                if pending:
                    return {"revision": self.revision, "reset_required": False, "events": pending}
            time.sleep(0.02)
        with self.lock:
            return {"revision": self.revision, "reset_required": False, "events": []}


FAKE_AUDIO_SCRIPT = """
(() => {
    class FakeAudio extends EventTarget {
        constructor() {
            super();
            this.paused = true; this.currentTime = 0; this.duration = 180; this.volume = 1; this._src = '';
            // player.js builds exactly one of these; the test drives it directly.
            window.__rainetteAudio = this;
        }
        get src() { return this._src; }
        set src(value) { this._src = String(value); }
        removeAttribute() { this._src = ''; }
        get readyState() { return this._src ? 4 : 0; }
        play() {
            this.paused = false;
            queueMicrotask(() => this.dispatchEvent(new Event('play')));
            return Promise.resolve();
        }
        pause() {
            const changed = !this.paused;
            this.paused = true;
            if (changed) queueMicrotask(() => this.dispatchEvent(new Event('pause')));
        }
        load() { queueMicrotask(() => this.dispatchEvent(new Event('loadedmetadata'))); }
        /* Drives the lyric follow the way timeupdate does in a real element. */
        __seek(seconds) {
            this.currentTime = seconds;
            this.dispatchEvent(new Event('timeupdate'));
        }
    }
    window.Audio = FakeAudio;
})();
"""


@pytest.fixture(scope="module")
def static_server():
    def build(*args, **kwargs):
        return QuietStaticHandler(*args, directory=str(PWA_DIR), **kwargs)

    server = QuietThreadingHTTPServer(("127.0.0.1", 0), build)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/"
    server.shutdown()
    server.server_close()


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as play:
        instance = play.chromium.launch()
        yield instance
        instance.close()


def open_phone(browser, base_url, computer):
    page = browser.new_page(viewport={"width": 390, "height": 844})
    page.set_default_timeout(8_000)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.add_init_script(FAKE_AUDIO_SCRIPT)
    # Start already paired: pairing has its own coverage over the real HTTP API.
    page.add_init_script(
        "localStorage.setItem('rainette.pwa.endpoint', %s);"
        "localStorage.setItem('rainette.pwa.token', 'test-token');"
        "localStorage.setItem('rainette.pwa.device_id', 'phone-1');"
        % json.dumps(ENDPOINT)
    )

    def companion(route):
        parsed = urlparse(route.request.url)
        if parsed.path == "/status":
            body = {"ok": True, "name": "Studio Mac", "device_id": "phone-1"}
        elif parsed.path == "/command":
            body = computer.handle_command(json.loads(route.request.post_data or "{}"))
        elif parsed.path == "/events":
            after = int(parse_qs(parsed.query).get("after", ["0"])[0])
            body = computer.read_events(after)
        else:
            body = {"ok": True}
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    page.route(f"{ENDPOINT}/**", companion)
    page.goto(base_url + "index.html", wait_until="domcontentloaded")
    return page, errors


class TestLibraryDoesNotThrash:
    def test_an_echoed_result_does_not_start_a_refresh_loop(self, browser, static_server):
        computer = FakeComputer()
        page, errors = open_phone(browser, static_server, computer)
        page.locator("#appView").wait_for(state="visible")

        page.locator("[data-tab='library']").click()
        page.locator("#libraryList .track-shell").first.wait_for(state="visible")

        # Long enough for several echo round trips through the event stream.
        page.wait_for_timeout(2_500)

        asked = computer.commands.count("music_library_index")
        assert asked == 1, f"library was re-requested {asked} times; the echo loop is back"
        assert page.locator("#libraryList .track-shell").count() == len(TRACKS)
        assert "Syncing your library" not in page.locator("#libraryList").inner_text()
        assert not errors, errors
        page.close()

    def test_an_open_playlist_survives_a_pushed_library_result(self, browser, static_server):
        computer = FakeComputer()
        page, errors = open_phone(browser, static_server, computer)
        page.locator("#appView").wait_for(state="visible")

        page.locator("[data-tab='library']").click()
        page.locator("#libraryModePlaylists").click()
        page.locator(".collection-row").first.click()
        page.locator("#libraryList .collection-title").wait_for(state="visible")

        # The computer re-indexes on its own; the phone is reading a playlist.
        computer.publish({"type": "music_library_index_result", "ok": True, "tracks": TRACKS})
        page.wait_for_timeout(1_200)

        assert page.locator("#libraryList .collection-title").count() == 1, \
            "a pushed library result painted over the open playlist"
        assert not errors, errors
        page.close()


class TestTransport:
    def test_play_pause_answers_the_first_tap(self, browser, static_server):
        computer = FakeComputer()
        page, errors = open_phone(browser, static_server, computer)
        page.locator("#appView").wait_for(state="visible")

        page.locator("#recentList .track").first.click()
        page.locator("#player").wait_for(state="visible")
        page.wait_for_function("() => document.querySelector('#playPauseButton').dataset.state === 'playing'")

        page.locator("#playPauseButton").click()
        page.wait_for_function("() => document.querySelector('#playPauseButton').dataset.state === 'paused'")
        assert page.evaluate("() => window.__rainetteAudio.paused") is True

        page.locator("#playPauseButton").click()
        page.wait_for_function("() => document.querySelector('#playPauseButton').dataset.state === 'playing'")
        assert not errors, errors
        page.close()

    def test_play_on_hands_the_queue_to_the_computer(self, browser, static_server):
        computer = FakeComputer()
        page, errors = open_phone(browser, static_server, computer)
        page.locator("#appView").wait_for(state="visible")

        page.locator("#recentList .track").first.click()
        page.locator("#player").wait_for(state="visible")

        page.locator("[data-tab='settings']").click()
        page.locator("#outputButton").click()
        page.locator(".sheet-actions .action-item").first.wait_for(state="visible")

        # "This phone" is the current output and is marked as such.
        assert page.locator(".sheet-actions .action-item.active .action-label").inner_text() == "This phone"

        computer_row = page.locator(".sheet-actions .action-item").nth(1)
        assert computer_row.locator(".action-label").inner_text() == "Studio Mac"
        computer_row.click()
        page.wait_for_function(
            "() => document.querySelector('#toast')?.textContent.includes('Playing on Studio Mac')"
        )
        assert "music_output_transfer" in computer.commands, computer.commands
        assert page.evaluate("() => window.__rainetteAudio.paused") is True
        assert not errors, errors
        page.close()


class TestBackgroundPlayback:
    def test_the_next_track_starts_without_asking_the_computer(self, browser, static_server):
        """A backgrounded page may only start audio as a continuation of the
        track that just ended, so advancing must not await the network."""
        computer = FakeComputer()
        page, errors = open_phone(browser, static_server, computer)
        page.locator("#appView").wait_for(state="visible")

        page.locator("#recentList .track").first.click()
        page.locator("#player").wait_for(state="visible")

        # The rest of the queue is resolved while the first track plays: three
        # recents means one URL for the track playing and two prefetched.
        deadline = time.monotonic() + 5
        while computer.commands.count("music_stream_url") < 3 and time.monotonic() < deadline:
            page.wait_for_timeout(100)
        resolved = computer.commands.count("music_stream_url")
        assert resolved == 3, f"upcoming tracks were not prefetched: {computer.commands}"

        # `ended` must reach play() in the same task, with no request between.
        outcome = page.evaluate(
            """() => {
                const audio = window.__rainetteAudio;
                const before = audio.src;
                audio.paused = true;
                audio.dispatchEvent(new Event('ended'));
                return { started: audio.paused === false, changed: audio.src !== before };
            }"""
        )
        assert outcome["changed"], "the next track's source was not set synchronously"
        assert outcome["started"], "playback did not start in the same task as `ended`"
        assert computer.commands.count("music_stream_url") == resolved, \
            "advancing asked the computer for a URL instead of using the prefetched one"
        assert not errors, errors
        page.close()

    def test_the_lock_screen_is_told_what_is_playing(self, browser, static_server):
        computer = FakeComputer()
        page, errors = open_phone(browser, static_server, computer)
        page.locator("#appView").wait_for(state="visible")

        page.locator("#recentList .track").first.click()
        page.locator("#player").wait_for(state="visible")
        page.wait_for_function("() => navigator.mediaSession?.metadata?.title")

        assert page.evaluate("() => navigator.mediaSession.metadata.title") == "Track 1"
        assert page.evaluate("() => navigator.mediaSession.playbackState") == "playing"
        assert not errors, errors
        page.close()


class TestScanner:
    def test_the_setup_screen_offers_a_scanner(self, browser, static_server):
        computer = FakeComputer()
        page = browser.new_page(viewport={"width": 390, "height": 844})
        page.goto(static_server + "index.html", wait_until="domcontentloaded")
        page.locator("#setupView").wait_for(state="visible")
        # Nothing is paired, so the setup screen is what a new phone sees.
        page.locator("#scanButton").wait_for(state="visible")
        assert "Scan" in page.locator("#scanButton").inner_text()
        page.close()
        del computer

    def test_the_bundled_reader_decodes_a_real_pairing_code(self, browser, static_server):
        """Safari has no BarcodeDetector, so this path is the one iPhones use."""
        qrcode = pytest.importorskip("qrcode")
        from PIL import Image  # noqa: F401  (qrcode[pil] brings this in)

        link = ("https://music-pwa-web.vercel.app/#endpoint=https%3A%2F%2Fquiet-river-1182"
                ".trycloudflare.com&invitation=" + "a3f9" * 16)
        code = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=6, border=4)
        code.add_data(link)
        code.make(fit=True)
        image = code.make_image(fill_color="black", back_color="white").convert("RGBA")

        page = browser.new_page()
        page.goto(static_server + "index.html", wait_until="domcontentloaded")
        decoded = page.evaluate(
            """async ({ bytes, width, height }) => {
                const { decodeImage } = await import('./src/qr.js');
                return decodeImage({ width, height, data: new Uint8ClampedArray(bytes) });
            }""",
            {"bytes": list(image.tobytes()), "width": image.width, "height": image.height},
        )
        assert decoded == link
        page.close()


class TestSyncedLyrics:
    def test_the_current_line_follows_the_song_and_seeks_on_tap(self, browser, static_server):
        computer = FakeComputer()
        page, errors = open_phone(browser, static_server, computer)
        page.locator("#appView").wait_for(state="visible")

        page.locator("#recentList .track").first.click()
        page.locator("#player").wait_for(state="visible")
        page.locator("#playerOpen").click()
        page.locator(".sheet-now").wait_for(state="visible")
        page.get_by_role("button", name="Lyrics").click()
        page.locator(".sheet-lyrics .lyrics-line").first.wait_for(state="visible")

        lines = page.locator(".sheet-lyrics .lyrics-line")
        assert lines.count() == 3

        page.evaluate("() => window.__rainetteAudio.__seek(6)")
        page.wait_for_function(
            "() => document.querySelector('.sheet-lyrics .lyrics-line.is-current')?.textContent === 'Second line'"
        )

        # Tapping a line moves the song to it.
        lines.nth(2).click()
        assert page.evaluate("() => Math.round(window.__rainetteAudio.currentTime)") == 10
        assert not errors, errors
        page.close()
