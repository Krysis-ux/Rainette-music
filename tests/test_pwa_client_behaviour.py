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
        if kind == "music_artist_catalog":
            return {"ok": True, "id": request_id, "artist": {"name": payload.get("name") or "Artist"},
                    "songs": TRACKS[:2], "albums": [], "singles": [], "videos": []}
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
            queueMicrotask(() => {
                this.dispatchEvent(new Event('play'));
                // A real element fires both: `play` when play() is called, and
                // `playing` once audio is actually flowing. Code that waits for
                // audio to genuinely start listens for the second one.
                this.dispatchEvent(new Event('playing'));
            });
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
    /* WebKit's Audio Session API, which Chromium does not implement. Stubbed
     * because the property it exposes is the difference between a Home Screen
     * app that keeps playing when you switch away and one that goes silent,
     * and there is otherwise nowhere to observe it outside a real iPhone.
     *
     * It starts at 'auto' because that is what Safari starts at -- and 'auto'
     * resolves to *ambient*, the category iOS silences on backgrounding. */
    Object.defineProperty(navigator, 'audioSession', {
        configurable: true,
        writable: true,
        value: { type: 'auto' },
    });
    /* Capture the Media Session action handlers so a test can invoke them the
     * way CarPlay does. There is no way to fire a real media action from page
     * script, and CarPlay's behaviour -- sending the absolute verb, and
     * re-sending it whenever its view disagrees with the phone's -- is exactly
     * what this needs to reproduce. */
    window.__mediaHandlers = {};
    if (navigator.mediaSession) {
        const real = navigator.mediaSession.setActionHandler.bind(navigator.mediaSession);
        navigator.mediaSession.setActionHandler = (action, handler) => {
            window.__mediaHandlers[action] = handler;
            try { real(action, handler); } catch { /* unsupported action */ }
        };
    }
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


# Launched from the Home Screen rather than in a tab. iOS reports this two
# ways and the client checks both, so the stub sets both.
STANDALONE_SCRIPT = """
(() => {
    Object.defineProperty(navigator, 'standalone', { configurable: true, value: true });
    const real = window.matchMedia.bind(window);
    window.matchMedia = query =>
        query.includes('display-mode: standalone')
            ? { matches: true, media: query, addEventListener() {}, removeEventListener() {} }
            : real(query);
})();
"""


def open_phone(browser, base_url, computer, *, standalone=False):
    page = browser.new_page(viewport={"width": 390, "height": 844})
    page.set_default_timeout(8_000)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.add_init_script(FAKE_AUDIO_SCRIPT)
    if standalone:
        page.add_init_script(STANDALONE_SCRIPT)
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


class TestArtistLinks:
    """The artist name is a control everywhere it appears, not just on the card.

    Two things have to be true at once, and they pull against each other: the
    whole row plays the track, and the name inside it goes to the artist. Get
    the stacking wrong and either the link is unreachable or it swallows the
    tap that should have started the song.
    """

    def test_the_row_plays_and_the_name_inside_it_does_not(self, browser, static_server):
        computer = FakeComputer()
        page, errors = open_phone(browser, static_server, computer)
        page.locator("#appView").wait_for(state="visible")

        # A tap anywhere on the row that is not the name starts the track. The
        # middle of the row is the natural place to press, and is exactly where
        # a full-width artist link would have intercepted it.
        page.locator("#recentList .track").first.click()
        page.locator("#player").wait_for(state="visible")

        assert not errors, errors
        page.close()

    def test_the_artist_name_opens_the_artist_without_playing(self, browser, static_server):
        computer = FakeComputer()
        page, errors = open_phone(browser, static_server, computer)
        page.locator("#appView").wait_for(state="visible")

        link = page.locator("#recentList .track .track-artist").first
        link.wait_for(state="visible")
        # A real control, so it carries a name and can be reached by keyboard.
        assert link.evaluate("node => node.tagName") == "BUTTON"
        assert "Go to" in (link.get_attribute("aria-label") or "")

        link.click()
        page.locator(".sheet-catalog").wait_for(state="visible")
        # The row underneath must not also have fired: opening someone's page
        # and starting their song are different requests.
        assert page.locator("#player").is_hidden()

        assert not errors, errors
        page.close()

    def test_every_track_row_offers_the_artist(self, browser, static_server):
        computer = FakeComputer()
        page, errors = open_phone(browser, static_server, computer)
        page.locator("#appView").wait_for(state="visible")

        # Wait for the list rather than counting whatever has rendered so far:
        # under load the recent list arrives after #appView does.
        page.locator("#recentList .track").first.wait_for(state="visible")
        rows = page.locator("#recentList .track")
        for index in range(rows.count()):
            row = rows.nth(index)
            assert row.locator(".track-artist").count() == 1, (
                "every row shows an artist line, and it is the same control on each"
            )

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

    def test_changing_track_quickly_does_not_raise_a_playback_error(self, browser, static_server):
        """Tapping a second song before the first starts is not a failure.

        `audio.play()` rejects with AbortError whenever a new load supersedes a
        pending play, which is exactly what a second tap does. Reporting it put
        a red toast on screen for a song that then played perfectly well.
        """
        computer = FakeComputer()
        page, errors = open_phone(browser, static_server, computer)
        page.locator("#appView").wait_for(state="visible")

        row = page.locator("#recentList .track").first
        row.wait_for(state="visible")

        # The race needs a play() that is still pending when the next load
        # lands, and against a fake computer that answers instantly it never is.
        # So the rejection itself is staged: this is precisely what the browser
        # hands back when a second tap supersedes the first.
        page.evaluate(
            """() => {
                const audio = window.__rainetteAudio;
                audio.play = () => Promise.reject(
                    new DOMException('The play() request was interrupted by a new load request.', 'AbortError')
                );
            }"""
        )

        row.click()
        page.wait_for_timeout(800)

        # The toast element is always in the DOM; only text in it is a message.
        toasts = page.evaluate(
            "() => [...document.querySelectorAll('.toast')]"
            ".map(node => node.textContent.trim()).filter(Boolean)"
        )
        assert not toasts, toasts
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


class TestAudioSessionCategory:
    """The Home Screen app has to declare itself a music player, not a page.

    Installed to the Home Screen, the same client that plays perfectly in a
    Safari tab stopped the instant the phone was locked or switched away from.
    The cause is not the code that plays -- it is what the app declared its
    audio to *be*: `navigator.audioSession.type` defaults to `auto`, `auto`
    resolves to ambient, and iOS silences ambient audio the moment the app is
    no longer frontmost. A Safari tab is exempt only because Safari itself owns
    a real playback session the page borrows.
    """

    def test_nothing_is_declared_before_any_audio_plays(self, browser, static_server):
        """Playback must never be gated on an iOS call we cannot test.

        Declaring the session up front would mean a session iOS refuses takes
        the music with it. Declared after `playing`, the worst case is that
        background playback is no better than before -- a bug, not a silence.
        """
        computer = FakeComputer()
        page, errors = open_phone(browser, static_server, computer, standalone=True)
        page.locator("#appView").wait_for(state="visible")

        assert page.evaluate("() => navigator.audioSession.type") == "auto", (
            "the session was declared before anything played, which puts the "
            "one thing that must always work behind an untestable iOS call"
        )
        assert not errors, errors
        page.close()

    def test_the_session_becomes_playback_once_audio_starts(self, browser, static_server):
        computer = FakeComputer()
        page, errors = open_phone(browser, static_server, computer, standalone=True)
        page.locator("#appView").wait_for(state="visible")

        page.locator("#recentList .track").first.click()
        page.wait_for_function("() => navigator.audioSession.type === 'playback'")

        assert page.evaluate("() => navigator.audioSession.type") == "playback", (
            "the session stayed at 'auto', which resolves to ambient -- iOS "
            "stops ambient audio when a Home Screen app is backgrounded"
        )
        assert not errors, errors
        page.close()

    def test_the_session_is_written_once_and_not_per_track(self, browser, static_server):
        """Once, not before every play.

        An earlier version re-asserted the category immediately before every
        `audio.play()`. Mutating an audio session at the exact moment playback
        starts is a way to interrupt it, not a way to be safe. A sentinel set
        after the first track proves the second does not write again.
        """
        computer = FakeComputer()
        page, errors = open_phone(browser, static_server, computer, standalone=True)
        page.locator("#appView").wait_for(state="visible")

        page.locator("#recentList .track").first.click()
        page.wait_for_function("() => navigator.audioSession.type === 'playback'")
        page.evaluate("() => { navigator.audioSession.type = 'sentinel'; }")

        # Every later track raises `playing` again. The listener is registered
        # `{ once: true }`, so none of them may write to the session.
        page.evaluate(
            "() => { for (let i = 0; i < 3; i += 1) "
            "window.__rainetteAudio.dispatchEvent(new Event('playing')); }"
        )
        page.wait_for_timeout(300)

        assert page.evaluate("() => navigator.audioSession.type") == "sentinel", (
            "the session was written again on a later `playing` event"
        )
        assert not errors, errors
        page.close()

    def test_a_tab_gets_the_session_too(self, browser, static_server):
        """Not only the Home Screen app.

        This was briefly scoped to standalone, as the smallest fix for the
        background-playback bug. It cannot stay that way: the same session is
        what keeps a Web Audio graph alive in the background, and that graph is
        the phone's volume control. A tab without it would mean a slider that
        silently costs background playback -- the exact bug this replaced.
        """
        computer = FakeComputer()
        page, errors = open_phone(browser, static_server, computer)
        page.locator("#appView").wait_for(state="visible")

        page.locator("#recentList .track").first.click()
        page.wait_for_function("() => navigator.audioSession.type === 'playback'")

        assert not errors, errors
        page.close()

    def test_the_volume_slider_returns_where_the_session_keeps_a_graph_alive(
            self, browser, static_server):
        """The iPhone volume slider, and why it is a capability check.

        It was removed because a Web Audio graph -- the only volume iOS honours
        -- was suspended whenever the page was hidden, so the control that
        carried volume also stopped the music on lock. WebKit fixed that (bug
        261554, iOS 17.5): a graph survives backgrounding under a declared
        playback session.

        So the gate is "can this engine hold a playback session", not "is this
        iOS". Hardcoding the platform was true when written, stopped being
        true, and nothing would have noticed -- the same shape as pinning a
        yt-dlp player client. This test pins the *capability*, on a page
        pretending to be an iPhone.
        """
        computer = FakeComputer()
        page = browser.new_page(viewport={"width": 390, "height": 844})
        page.set_default_timeout(8_000)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.add_init_script(FAKE_AUDIO_SCRIPT)
        page.add_init_script(
            "Object.defineProperty(navigator, 'platform', "
            "{ configurable: true, value: 'iPhone' });"
        )
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
        page.goto(static_server + "index.html", wait_until="domcontentloaded")
        page.locator("#appView").wait_for(state="visible")

        adjustable = page.evaluate(
            "async () => (await import('./src/audio.js')).volumeIsAdjustable()")
        assert adjustable is True, (
            "an iPhone that can declare a playback session has a working "
            "volume slider again; the gate must ask what the engine can do, "
            "not what platform it is"
        )
        assert not errors, errors
        page.close()

    def test_no_slider_and_no_essay_where_a_graph_would_cost_playback(
            self, browser, static_server):
        """Remove it completely rather than explain it.

        Where no slider can work, the row is hidden. A paragraph about audio
        graphs in the middle of a now-playing card is worse than the space it
        fills, and the hardware buttons need no instructions.
        """
        computer = FakeComputer()
        page = browser.new_page(viewport={"width": 390, "height": 844})
        page.set_default_timeout(8_000)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.add_init_script(FAKE_AUDIO_SCRIPT)
        # An iPhone old enough to have no Audio Session API at all.
        page.add_init_script(
            "Object.defineProperty(navigator, 'platform', "
            "{ configurable: true, value: 'iPhone' });"
            "delete navigator.audioSession;"
        )
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
        page.goto(static_server + "index.html", wait_until="domcontentloaded")
        page.locator("#appView").wait_for(state="visible")
        page.locator("#recentList .track").first.click()
        page.locator("#player").wait_for(state="visible")

        card = page.locator("#player")
        assert card.locator(".now-volume-note").count() == 0, "the essay is back"
        assert "audio graph" not in card.inner_text().lower()
        assert not errors, errors
        page.close()

    def test_a_browser_without_the_api_still_plays(self, browser, static_server):
        """The API is WebKit-only; asking for it must never break anyone else."""
        computer = FakeComputer()
        page = browser.new_page(viewport={"width": 390, "height": 844})
        page.set_default_timeout(8_000)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.add_init_script(FAKE_AUDIO_SCRIPT)
        page.add_init_script("delete navigator.audioSession;")
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
        page.goto(static_server + "index.html", wait_until="domcontentloaded")
        page.locator("#appView").wait_for(state="visible")

        page.locator("#recentList .track").first.click()
        page.locator("#player").wait_for(state="visible")
        page.wait_for_function("() => navigator.mediaSession?.metadata?.title")

        assert page.evaluate("() => 'audioSession' in navigator") is False
        assert not errors, errors
        page.close()


class TestCarTransportCommands:
    """A car states an intent; answering it with a flip is an oscillator.

    CarPlay sends the absolute verb -- `play`, `pause` -- and re-sends it
    whenever its own view of the phone disagrees, which is often. Both action
    handlers used to call `toggle()`. So `play` arriving while playing paused
    the music, the car saw paused and sent `play` again, and the result was a
    second of music and a second of silence for the whole song, on CarPlay
    only. Bluetooth never drives the session this way; it just carries audio,
    which is why it was fine.
    """

    def _play_first_track(self, page):
        page.locator("#appView").wait_for(state="visible")
        page.locator("#recentList .track").first.click()
        page.locator("#player").wait_for(state="visible")
        page.wait_for_function("() => window.__rainetteAudio?.paused === false")

    def test_a_play_command_while_playing_does_not_pause(self, browser, static_server):
        """The stutter, reproduced: this used to leave the track paused."""
        computer = FakeComputer()
        page, errors = open_phone(browser, static_server, computer)
        self._play_first_track(page)

        page.evaluate("() => window.__mediaHandlers.play()")
        page.wait_for_timeout(150)

        assert page.evaluate("() => window.__rainetteAudio.paused") is False, (
            "a `play` command while already playing paused the track -- the car "
            "then re-sends `play`, and that oscillation is the CarPlay stutter"
        )
        assert not errors, errors
        page.close()

    def test_repeated_play_commands_never_flip_the_state(self, browser, static_server):
        """The car may send it many times; every one must mean the same thing."""
        computer = FakeComputer()
        page, errors = open_phone(browser, static_server, computer)
        self._play_first_track(page)

        for _ in range(6):
            page.evaluate("() => window.__mediaHandlers.play()")
            page.wait_for_timeout(40)

        assert page.evaluate("() => window.__rainetteAudio.paused") is False
        assert not errors, errors
        page.close()

    def test_a_pause_command_while_paused_does_not_resume(self, browser, static_server):
        computer = FakeComputer()
        page, errors = open_phone(browser, static_server, computer)
        self._play_first_track(page)

        page.evaluate("() => window.__mediaHandlers.pause()")
        page.wait_for_function("() => window.__rainetteAudio.paused === true")
        page.evaluate("() => window.__mediaHandlers.pause()")
        page.wait_for_timeout(150)

        assert page.evaluate("() => window.__rainetteAudio.paused") is True, (
            "a second `pause` resumed playback"
        )
        assert not errors, errors
        page.close()

    def test_the_absolute_verbs_still_start_and_stop(self, browser, static_server):
        """Idempotence must not cost the commands their actual job."""
        computer = FakeComputer()
        page, errors = open_phone(browser, static_server, computer)
        self._play_first_track(page)

        page.evaluate("() => window.__mediaHandlers.pause()")
        page.wait_for_function("() => window.__rainetteAudio.paused === true")
        page.evaluate("() => window.__mediaHandlers.play()")
        page.wait_for_function("() => window.__rainetteAudio.paused === false")

        assert not errors, errors
        page.close()

    def test_the_in_app_button_still_flips(self, browser, static_server):
        """A tap does mean "the other one" -- toggle keeps that meaning."""
        computer = FakeComputer()
        page, errors = open_phone(browser, static_server, computer)
        self._play_first_track(page)

        page.locator("#playPauseButton").click()
        page.wait_for_function("() => window.__rainetteAudio.paused === true")
        page.locator("#playPauseButton").click()
        page.wait_for_function("() => window.__rainetteAudio.paused === false")

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


class TestTransportStaysWhereItIsPut:
    """One tap on pause means paused, and nothing restarts playback after."""

    def _count_transport(self, page):
        page.evaluate(
            """() => {
                const audio = window.__rainetteAudio;
                window.__calls = { play: 0, pause: 0 };
                const play = audio.play.bind(audio);
                const pause = audio.pause.bind(audio);
                audio.play = () => { window.__calls.play += 1; return play(); };
                audio.pause = () => { window.__calls.pause += 1; return pause(); };
            }"""
        )

    def test_one_pause_tap_does_not_start_a_play_pause_loop(self, browser, static_server):
        computer = FakeComputer()
        page, errors = open_phone(browser, static_server, computer)
        page.locator("#appView").wait_for(state="visible")
        page.locator("#recentList .track").first.click()
        page.locator("#player").wait_for(state="visible")
        page.wait_for_function(
            "() => document.querySelector('#playPauseButton').dataset.state === 'playing'"
        )

        self._count_transport(page)
        page.locator("#playPauseButton").click()
        page.wait_for_function(
            "() => document.querySelector('#playPauseButton').dataset.state === 'paused'"
        )
        # Long enough for a status tick, an event-loop round trip and any
        # re-render to have happened.
        page.wait_for_timeout(2000)

        calls = page.evaluate("() => window.__calls")
        assert page.evaluate("() => window.__rainetteAudio.paused") is True, (
            "playback restarted itself after a single pause"
        )
        assert calls["play"] == 0, f"pause was undone by {calls['play']} play call(s)"
        assert calls["pause"] <= 1, f"pause fired {calls['pause']} times for one tap"
        assert not errors, errors
        page.close()

    def test_a_paused_track_stays_at_its_position(self, browser, static_server):
        """The loop's other half: the position kept jumping backwards."""
        computer = FakeComputer()
        page, errors = open_phone(browser, static_server, computer)
        page.locator("#appView").wait_for(state="visible")
        page.locator("#recentList .track").first.click()
        page.locator("#player").wait_for(state="visible")
        page.wait_for_function(
            "() => document.querySelector('#playPauseButton').dataset.state === 'playing'"
        )

        page.evaluate("() => window.__rainetteAudio.__seek(42)")
        page.locator("#playPauseButton").click()
        page.wait_for_function(
            "() => document.querySelector('#playPauseButton').dataset.state === 'paused'"
        )
        page.wait_for_timeout(1500)

        assert page.evaluate("() => window.__rainetteAudio.currentTime") == 42, (
            "the position moved while paused"
        )
        assert not errors, errors
        page.close()


class TestPlayingFromAnArtistPage:
    """The card opens over the profile, and closing it returns there."""

    @staticmethod
    def _player_on_top(page):
        """Is the mini bar the thing under your thumb, or is a sheet over it?

        `is_visible` only reports layout, and the bar stays laid out behind a
        sheet — so it answers True for a bar nobody can see or tap.
        """
        return page.evaluate(
            """() => {
                const bar = document.querySelector('#player');
                if (!bar || bar.hidden) return false;
                const box = bar.getBoundingClientRect();
                if (!box.width) return false;
                const hit = document.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2);
                return !!(hit && bar.contains(hit));
            }"""
        )

    def test_playing_from_an_artist_opens_the_card_over_the_profile(self, browser, static_server):
        computer = FakeComputer()
        page, errors = open_phone(browser, static_server, computer)
        page.locator("#appView").wait_for(state="visible")

        page.locator("#recentList .track .link-inline").first.click()
        page.locator(".sheet-catalog").wait_for(state="visible")
        # The mini bar stays behind the sheet; the card is what reports playback.
        assert not self._player_on_top(page)

        page.locator(".sheet-catalog .track").first.click()
        page.locator(".sheet-now").wait_for(state="visible")

        # The profile is still underneath, so closing the card returns to it
        # rather than dropping the user back on the page they started from.
        assert page.locator(".sheet-catalog").count() == 1
        page.locator(".sheet-now .now-top button").first.click()
        page.locator(".sheet-now").wait_for(state="detached")
        assert page.locator(".sheet-catalog").is_visible()
        assert not errors, errors
        page.close()

    def test_the_mini_bar_returns_once_the_profile_is_closed(self, browser, static_server):
        computer = FakeComputer()
        page, errors = open_phone(browser, static_server, computer)
        page.locator("#appView").wait_for(state="visible")

        page.locator("#recentList .track").first.click()
        page.locator("#player").wait_for(state="visible")
        page.locator("#recentList .track .link-inline").first.click()
        page.locator(".sheet-catalog").wait_for(state="visible")
        assert not self._player_on_top(page), "the mini bar sat on top of the profile"

        page.keyboard.press("Escape")
        page.locator(".sheet-catalog").wait_for(state="detached")
        page.wait_for_function(
            """() => {
                const bar = document.querySelector('#player');
                const box = bar.getBoundingClientRect();
                const hit = document.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2);
                return !!(hit && bar.contains(hit));
            }"""
        )
        assert not errors, errors
        page.close()


class TestSheetsCanBePulledDown:
    """A sheet must close by dragging it, not only by its button."""

    @staticmethod
    def _drag_down(page, selector, distance=320):
        box = page.locator(selector).bounding_box()
        start_x = box["x"] + box["width"] / 2
        start_y = box["y"] + 12
        page.mouse.move(start_x, start_y)
        page.mouse.down()
        # Several steps: one jump reads as a teleport and never crosses the
        # threshold that decides the axis.
        for step in range(1, 13):
            page.mouse.move(start_x, start_y + distance * step / 12)
            page.wait_for_timeout(16)
        page.mouse.up()

    def test_the_now_playing_card_closes_when_pulled_down(self, browser, static_server):
        computer = FakeComputer()
        page, errors = open_phone(browser, static_server, computer)
        page.locator("#appView").wait_for(state="visible")
        page.locator("#recentList .track").first.click()
        page.locator("#player").wait_for(state="visible")
        page.locator("#playerOpen").click()
        page.locator(".sheet-now").wait_for(state="visible")

        self._drag_down(page, ".sheet-now .sheet-grabber")

        page.locator(".sheet-now").wait_for(state="detached")
        # Closing the card is a minimise, not a stop.
        assert page.evaluate("() => window.__rainetteAudio.paused") is False
        page.locator("#player").wait_for(state="visible")
        assert not errors, errors
        page.close()

    def test_the_artwork_is_a_drag_handle_too(self, browser, static_server):
        """The grabber is a small target; the art is the obvious big one."""
        computer = FakeComputer()
        page, errors = open_phone(browser, static_server, computer)
        page.locator("#appView").wait_for(state="visible")
        page.locator("#recentList .track").first.click()
        page.locator("#player").wait_for(state="visible")
        page.locator("#playerOpen").click()
        page.locator(".sheet-now").wait_for(state="visible")

        self._drag_down(page, ".sheet-now .now-art-shell")

        page.locator(".sheet-now").wait_for(state="detached")
        assert not errors, errors
        page.close()
