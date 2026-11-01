"""The phone client inside the native app, driven against a fake native bridge.

Capacitor injects `window.Capacitor` into the app's WebView before any page
script runs; this stands in for it with a scripted `RainettePlayer` plugin that
records every call and answers the way RainettePlayerPlugin.kt does. That is
what lets the shared interface be tested without a phone: which engine owns
the audio, that the <audio> element stays silent while the service plays,
that a stale answer cannot replace a newer song, and that a browser with no
bridge is untouched.

None of this proves the native player plays. That needs a device; see
mobile/AUDIT.md for what has and has not been run on one.
"""

import json
from urllib.parse import urlparse

import pytest

from test_pwa_client_behaviour import (  # noqa: F401  (fixtures are used by name)
    ENDPOINT, FAKE_AUDIO_SCRIPT, FakeComputer, browser, static_server,
)

SONGS = [
    {"source": "youtube", "source_id": f"song{n:07d}", "title": f"Song {n}", "artist": "Phone Artist",
     "duration_s": 200 + n, "thumbnail_url": ""}
    for n in range(1, 5)
]

FAKE_BRIDGE = """
(() => {
    const songs = %s;
    const log = [];
    const listeners = {};
    const watchers = [];
    const failures = {};
    let held = null;
    let protocol = 1;
    const service = { generation: 0, playing: false, loading: false, ended: false, index: -1,
                      source_id: '', position_s: 0, duration_s: 0, queue: [] };
    // A service that was already playing before this page opened.
    Object.assign(service, window.__presetService || {});
    const snapshot = () => ({ ...service, queue: undefined });
    window.__native = {
        log, failures, service,
        hold() { held = 'waiting'; },
        setProtocol(value) { protocol = value; },
        emit(event, data) { for (const cb of watchers) cb({ ...data, event }); },
        advance() {
            service.index += 1;
            service.source_id = service.queue[service.index].source_id;
            service.position_s = 0;
            this.emit('state', snapshot());
        },
    };
    // Capacitor drops every plugin listener when the WebView reports the page
    // started (bridge.reset() in onPageStarted). On a real Galaxy Note 8 that
    // report arrived after the page's modules had registered theirs, so the
    // fake does the same: anything registered before 'load' is forgotten.
    window.addEventListener('load', () => { for (const key of Object.keys(listeners)) delete listeners[key]; });
    window.Capacitor = {
        isNativePlatform: () => true,
        isPluginAvailable: name => name === 'RainettePlayer',
        addListener(plugin, event, callback) {
            (listeners[event] ||= []).push(callback);
            return { remove() {} };
        },
        // The plugin's own watch channel, which bridge.reset() does not clear.
        nativeCallback(plugin, method, options, callback) {
            log.push({ method, options: JSON.parse(JSON.stringify(options || {})) });
            if (method === 'watch') watchers.push(callback);
            return String(watchers.length);
        },
        nativePromise(plugin, method, options = {}) {
            log.push({ method, options: JSON.parse(JSON.stringify(options)) });
            if (failures[method]) {
                const failure = failures[method];
                delete failures[method];
                return Promise.reject(failure);
            }
            switch (method) {
                case 'capabilities': return Promise.resolve({ search: true, playback: true, protocol });
                case 'search': return Promise.resolve({ songs });
                case 'play': {
                    // A newer play cancels one still loading, as the plugin does.
                    if (held && held.reject) held.reject({ code: 'canceled', message: 'replaced' });
                    Object.assign(service, { generation: options.generation, playing: !options.start_paused,
                        loading: false, index: options.index, queue: options.queue,
                        source_id: options.queue[options.index].source_id,
                        position_s: options.position_s || 0, duration_s: 200 });
                    if (held === 'waiting') {
                        return new Promise((resolve, reject) => { held = { resolve, reject }; });
                    }
                    held = null;
                    return Promise.resolve(snapshot());
                }
                case 'resume': service.playing = true; return Promise.resolve(snapshot());
                case 'pause': service.playing = false; return Promise.resolve(snapshot());
                case 'seek': service.position_s = options.position_s; return Promise.resolve(snapshot());
                case 'skip':
                    service.index = Math.max(0, Math.min(service.queue.length - 1, service.index + options.offset));
                    service.source_id = service.queue[service.index].source_id;
                    return Promise.resolve(snapshot());
                case 'setQueue':
                    service.queue = options.queue; service.index = options.index;
                    return Promise.resolve(snapshot());
                case 'stop': Object.assign(service, { playing: false, index: -1, source_id: '', queue: [] });
                    return Promise.resolve(snapshot());
                case 'resolve': return Promise.resolve({ url: 'https://stream.example/' + options.source_id,
                    expires_at_ms: Date.now() + 3600000 });
                case 'getState':
                    return Promise.resolve(options.with_queue
                        ? { ...snapshot(), queue: service.queue.map(t => ({ ...t, source: 'youtube' })) }
                        : snapshot());
                default: return Promise.resolve(snapshot());
            }
        },
    };
})();
""" % json.dumps(SONGS)

# Counts and orders what the <audio> element is asked to do, beside the bridge log.
ELEMENT_LOG = """
(() => {
    const real = window.Audio.prototype.play;
    window.Audio.prototype.play = function () {
        window.__native?.log.push({ method: 'element.play', options: { src: this.src } });
        return real.call(this);
    };
})();
"""


def open_app(browser, base_url, *, paired=False, bridge=True, preset=None, touch=False):
    page = browser.new_page(viewport={"width": 390, "height": 844}, has_touch=touch)
    page.set_default_timeout(8_000)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    companion_requests = []
    page.add_init_script(FAKE_AUDIO_SCRIPT)
    if preset:
        page.add_init_script("window.__presetService = %s;" % json.dumps(preset))
    if bridge:
        page.add_init_script(FAKE_BRIDGE)
    page.add_init_script(ELEMENT_LOG)
    computer = FakeComputer()
    if paired:
        page.add_init_script(
            "localStorage.setItem('rainette.pwa.endpoint', %s);"
            "localStorage.setItem('rainette.pwa.token', 'test-token');"
            "localStorage.setItem('rainette.pwa.device_id', 'phone-1');" % json.dumps(ENDPOINT)
        )

    def companion(route):
        companion_requests.append(urlparse(route.request.url).path)
        parsed = urlparse(route.request.url)
        if parsed.path == "/status":
            body = {"ok": True, "name": "Studio Mac", "device_id": "phone-1"}
        elif parsed.path == "/command":
            body = computer.handle_command(json.loads(route.request.post_data or "{}"))
        else:
            body = {"ok": True, "revision": 0, "events": [], "reset_required": False}
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    page.route(f"{ENDPOINT}/**", companion)
    page.goto(base_url + "index.html", wait_until="domcontentloaded")
    return page, errors, companion_requests


def native_calls(page, method=None):
    log = page.evaluate("() => window.__native.log")
    return [entry for entry in log if method is None or entry["method"] == method]


def search_and_play(page, row=0):
    page.click('#tabBar [data-tab="search"]')
    page.fill("#searchInput", "song")
    page.press("#searchInput", "Enter")
    page.locator("#searchResults .track").nth(row).click()
    page.wait_for_function("() => document.querySelector('#playPauseButton').dataset.state === 'playing'")


class TestFirstLaunch:
    def test_the_app_opens_into_music_not_pairing(self, browser, static_server):
        page, errors, requests = open_app(browser, static_server)
        page.locator("#appView").wait_for(state="visible")
        assert page.locator("#setupView").is_hidden()
        assert page.locator("#tabBar").is_visible()
        assert page.locator("#statusText").inner_text() == "On this phone"
        assert requests == [], "an unpaired app must not reach for a computer"
        # Home is not a blank screen on first launch: it says where to start.
        assert "Search for a song" in page.locator("#recentList").inner_text()
        assert not errors, errors
        page.close()

    def test_a_browser_still_starts_on_pairing(self, browser, static_server):
        page, errors, _ = open_app(browser, static_server, bridge=False)
        page.locator("#setupView").wait_for(state="visible")
        assert page.locator("#appView").is_hidden()
        assert page.locator("#setupBack").is_hidden()
        assert page.locator("#setupTitle").inner_text().startswith("Point this phone")
        assert not errors, errors
        page.close()

    def test_a_computer_is_an_option_in_settings_not_a_gate(self, browser, static_server):
        page, errors, _ = open_app(browser, static_server)
        page.click('#tabBar [data-tab="settings"]')
        assert page.locator("#connectComputerButton").is_visible()
        for hidden in ("#disconnectButton", "#testConnectionButton", "#linkToggle", "#equalizerButton", "#installHelpButton"):
            assert page.locator(hidden).is_hidden(), f"{hidden} is a browser or pairing control"
        page.click("#connectComputerButton")
        page.locator("#setupView").wait_for(state="visible")
        assert page.locator("#setupTitle").inner_text() == "Connect to a computer."
        page.click("#setupBack")
        page.locator("#appView").wait_for(state="visible")
        assert page.locator("#setupView").is_hidden()
        assert not errors, errors
        page.close()

    def test_a_paired_app_keeps_its_pairing_and_joins_quietly(self, browser, static_server):
        page, errors, requests = open_app(browser, static_server, paired=True)
        page.locator("#appView").wait_for(state="visible")
        page.wait_for_function("() => document.querySelector('#statusText').textContent === 'Studio Mac'")
        assert "/status" in requests
        assert page.evaluate("() => localStorage.getItem('rainette.pwa.token')") == "test-token"
        assert page.locator("#setupView").is_hidden()
        assert not errors, errors
        page.close()


class TestReopening:
    def test_reopening_over_music_that_kept_playing_shows_it_without_restarting(self, browser, static_server):
        queue = [{k: s[k] for k in ("source_id", "title", "artist", "thumbnail_url")} for s in SONGS]
        preset = {"generation": 7, "playing": True, "index": 1, "source_id": SONGS[1]["source_id"],
                  "position_s": 30, "duration_s": 200, "queue": queue}
        page, errors, _ = open_app(browser, static_server, preset=preset)
        page.wait_for_function("() => document.querySelector('#playerTitle').textContent === 'Song 2'")
        assert page.locator("#player").is_visible()
        assert page.locator("#playPauseButton").get_attribute("data-state") == "playing"
        assert native_calls(page, "play") == [], "reopening must not restart what is playing"
        # It adopted the service's generation, so it keeps following it.
        page.evaluate("() => window.__native.advance()")
        page.wait_for_function("() => document.querySelector('#playerTitle').textContent === 'Song 3'")
        assert not errors, errors
        page.close()

    def test_an_idle_service_leaves_the_page_empty(self, browser, static_server):
        page, errors, _ = open_app(browser, static_server)
        page.locator("#appView").wait_for(state="visible")
        page.wait_for_timeout(300)
        assert page.locator("#player").is_hidden()
        assert not errors, errors
        page.close()


class TestSearchOnThePhone:
    def test_search_is_answered_by_the_phone(self, browser, static_server):
        page, errors, requests = open_app(browser, static_server)
        page.click('#tabBar [data-tab="search"]')
        page.fill("#searchInput", "song")
        page.press("#searchInput", "Enter")
        page.locator("#searchResults .track").first.wait_for()
        assert page.locator("#searchResults .track").count() == len(SONGS)
        assert native_calls(page, "search")[0]["options"] == {"query": "song"}
        assert requests == []
        assert not errors, errors
        page.close()

    def test_an_older_search_answering_late_does_not_replace_a_newer_one(self, browser, static_server):
        page, errors, _ = open_app(browser, static_server)
        page.evaluate("""() => {
            const real = window.Capacitor.nativePromise;
            window.Capacitor.nativePromise = (plugin, method, options) => {
                if (method !== 'search' || options.query !== 'old') return real(plugin, method, options);
                return new Promise(resolve => setTimeout(() => resolve({ songs: [
                    { source: 'youtube', source_id: 'staleSong01', title: 'Stale', artist: 'x', duration_s: 1 },
                ] }), 800));
            };
        }""")
        page.click('#tabBar [data-tab="search"]')
        page.fill("#searchInput", "old")
        page.press("#searchInput", "Enter")
        page.fill("#searchInput", "song")
        page.press("#searchInput", "Enter")
        page.locator("#searchResults .track").first.wait_for()
        page.wait_for_timeout(1200)
        assert page.locator("#searchResults .track").count() == len(SONGS)
        assert "Stale" not in page.locator("#searchResults").inner_text()
        assert not errors, errors
        page.close()

    def test_a_failed_search_says_what_happened(self, browser, static_server):
        page, errors, _ = open_app(browser, static_server)
        page.evaluate("() => { window.__native.failures.search = { code: 'offline', message: 'x' }; }")
        page.click('#tabBar [data-tab="search"]')
        page.fill("#searchInput", "song")
        page.press("#searchInput", "Enter")
        page.wait_for_function("() => document.querySelector('#searchMessage').textContent.includes('No internet')")
        assert not errors, errors
        page.close()


class TestOneAudioOwner:
    def test_a_streamed_song_plays_in_the_service_and_never_in_the_element(self, browser, static_server):
        page, errors, _ = open_app(browser, static_server)
        search_and_play(page)
        play = native_calls(page, "play")
        assert len(play) == 1
        assert [t["source_id"] for t in play[0]["options"]["queue"]] == [s["source_id"] for s in SONGS]
        # The page names tracks; it never hands the service a URL to play.
        assert all(set(t) == {"source_id", "title", "artist", "thumbnail_url", "duration_s"} for t in play[0]["options"]["queue"])
        assert native_calls(page, "element.play") == []
        assert page.locator("#playerTitle").inner_text() == "Song 1"
        assert not errors, errors
        page.close()

    def test_a_phone_file_stops_the_service_before_the_element_plays(self, browser, static_server):
        page, errors, _ = open_app(browser, static_server)
        search_and_play(page)
        page.evaluate("""async () => {
            const local = await import('./src/local.js');
            const player = await import('./src/player.js');
            const file = new File([new Uint8Array(4096)], 'Mine.mp3', { type: 'audio/mpeg' });
            await local.importOne(file);
            const [track] = await local.listLocalTracks();
            await player.playTrack(track, [track], 0);
        }""")
        order = [entry["method"] for entry in native_calls(page)]
        assert "stop" in order and "element.play" in order
        assert order.index("stop") < order.index("element.play"), order
        assert page.evaluate("async () => (await import('./src/player.js')).nativeOwns()") is False
        assert not errors, errors
        page.close()

    def test_a_mixed_list_keeps_streamed_songs_on_the_service(self, browser, static_server):
        """The Note 8 finding: one imported file in Recently Played sent every
        streamed song in that list to the element, with no lock-screen controls."""
        page, errors, _ = open_app(browser, static_server)
        page.locator("#appView").wait_for(state="visible")
        result = page.evaluate("""async songs => {
            const local = await import('./src/local.js');
            const player = await import('./src/player.js');
            const { state } = await import('./src/state.js');
            await local.importOne(new File([new Uint8Array(4096)], 'Mine.mp3', { type: 'audio/mpeg' }));
            const [file] = await local.listLocalTracks();
            const mixed = [songs[0], file, songs[1], songs[2]];
            await player.playTrack(mixed[2], mixed, 2);
            const streamed = { owner: player.nativeOwns(), queue: state.queue.map(t => t.source_id), index: state.queueIndex };
            await player.playTrack(mixed[1], mixed, 1);
            const filed = { owner: player.nativeOwns(), queue: state.queue.map(t => t.source_id), index: state.queueIndex };
            return { streamed, filed, fileId: file.source_id };
        }""", SONGS)
        assert result["streamed"] == {"owner": True, "queue": [s["source_id"] for s in SONGS[:3]], "index": 1}
        assert [t["source_id"] for t in native_calls(page, "play")[-1]["options"]["queue"]] == [s["source_id"] for s in SONGS[:3]]
        assert result["filed"] == {"owner": False, "queue": [result["fileId"]], "index": 0}
        assert not errors, errors
        page.close()

    def test_a_slow_load_cannot_replace_a_newer_song(self, browser, static_server):
        page, errors, _ = open_app(browser, static_server)
        page.click('#tabBar [data-tab="search"]')
        page.fill("#searchInput", "song")
        page.press("#searchInput", "Enter")
        page.evaluate("() => window.__native.hold()")
        page.locator("#searchResults .track").nth(0).click()
        page.locator("#searchResults .track").nth(2).click()
        page.wait_for_function("() => document.querySelector('#playerTitle').textContent === 'Song 3'")
        first = native_calls(page, "play")[0]["options"]["generation"]
        # The service reports, late, on the request that was replaced.
        page.evaluate("""g => window.__native.emit('state', { generation: g, playing: true, index: 0,
            source_id: 'song0000001', position_s: 5, duration_s: 200 })""", first)
        page.wait_for_timeout(300)
        assert page.locator("#playerTitle").inner_text() == "Song 3"
        assert page.locator("#toast").inner_text() == "", "a replaced request is not an error"
        assert not errors, errors
        page.close()


class TestTransport:
    def test_repeated_play_never_pauses_and_repeated_pause_never_plays(self, browser, static_server):
        page, errors, _ = open_app(browser, static_server)
        search_and_play(page)
        page.evaluate("async () => { const p = await import('./src/player.js'); await p.play(); await p.play(); }")
        assert native_calls(page, "pause") == []
        assert page.evaluate("async () => (await import('./src/player.js')).isPlaying()") is True
        page.evaluate("async () => { const p = await import('./src/player.js'); p.pause(); p.pause(); }")
        page.wait_for_function("async () => !(await import('./src/player.js')).isPlaying()")
        assert [e["method"] for e in native_calls(page)[-2:]] == ["pause", "pause"]
        assert not errors, errors
        page.close()

    def test_the_page_follows_the_service_to_the_next_track(self, browser, static_server):
        page, errors, _ = open_app(browser, static_server)
        search_and_play(page)
        page.evaluate("() => window.__native.advance()")
        page.wait_for_function("() => document.querySelector('#playerTitle').textContent === 'Song 2'")
        assert not errors, errors
        page.close()

    def test_the_page_hears_the_service_even_though_capacitor_reset_its_listeners(self, browser, static_server):
        """The Note 8 bug: listeners made at module load were wiped by bridge.reset()."""
        page, errors, _ = open_app(browser, static_server)
        search_and_play(page)
        generation = native_calls(page, "play")[-1]["options"]["generation"]
        page.evaluate("""g => window.__native.emit('error', { generation: g, code: 'unavailable',
            message: 'raw native text', index: 0, source_id: 'song0000001' })""", generation)
        page.wait_for_function("() => document.querySelector('#toast').textContent.includes('no longer available')")
        page.evaluate("() => window.__native.advance()")
        page.wait_for_function("() => document.querySelector('#playerTitle').textContent === 'Song 2'")
        assert not errors, errors
        page.close()

    def test_next_on_the_last_track_stops_rather_than_doing_nothing(self, browser, static_server):
        page, errors, _ = open_app(browser, static_server)
        search_and_play(page, row=3)
        page.click("#nextButton")
        page.wait_for_function("() => document.querySelector('#playPauseButton').dataset.state === 'paused'")
        assert native_calls(page, "skip") == []
        assert native_calls(page, "seek")[-1]["options"] == {"position_s": 0}
        assert not errors, errors
        page.close()

    def test_a_queue_edit_reaches_the_service_without_restarting(self, browser, static_server):
        page, errors, _ = open_app(browser, static_server)
        search_and_play(page)
        page.evaluate("""async () => {
            const p = await import('./src/player.js');
            p.queueMove(3, 1);
        }""")
        page.wait_for_function("() => window.__native.log.some(e => e.method === 'setQueue')")
        assert len(native_calls(page, "play")) == 1
        sent = native_calls(page, "setQueue")[-1]["options"]
        assert [t["source_id"] for t in sent["queue"]][:2] == ["song0000001", "song0000004"]
        assert not errors, errors
        page.close()


class TestFailures:
    @pytest.mark.parametrize("code, words", [
        ("region", "available in your country"),
        ("unavailable", "no longer available"),
        ("rate_limited", "slow down"),
    ])
    def test_a_failure_is_worded_for_people(self, browser, static_server, code, words):
        page, errors, _ = open_app(browser, static_server)
        page.evaluate("c => { window.__native.failures.play = { code: c, message: 'raw native text' }; }", code)
        page.click('#tabBar [data-tab="search"]')
        page.fill("#searchInput", "song")
        page.press("#searchInput", "Enter")
        page.locator("#searchResults .track").first.click()
        page.wait_for_function("w => document.querySelector('#toast').textContent.includes(w)", arg=words)
        assert "raw native text" not in page.locator("#toast").inner_text()
        page.wait_for_function("() => document.querySelector('#playPauseButton').dataset.state !== 'loading'")
        assert not errors, errors
        page.close()

    def test_a_mismatched_native_half_is_refused(self, browser, static_server):
        page, errors, _ = open_app(browser, static_server)
        page.evaluate("() => window.__native.setProtocol(2)")
        page.click('#tabBar [data-tab="search"]')
        page.fill("#searchInput", "song")
        page.press("#searchInput", "Enter")
        page.locator("#searchResults .track").first.click()
        page.wait_for_function("() => document.querySelector('#toast').textContent.includes('Reinstall')")
        assert native_calls(page, "play") == []
        assert not errors, errors
        page.close()


# A finger drag, as the phone's WebView delivers it: touch pointer events on
# the surface, moving in steps. Synthetic events arrive in the same instant,
# so the gesture sees distance and no speed, which is the stricter case.
SWIPE = """([selector, dx, dy]) => {
    const surface = document.querySelector(selector);
    const box = surface.getBoundingClientRect();
    const x = box.left + box.width / 2;
    const y = box.top + box.height / 2;
    const fire = (type, f) => surface.dispatchEvent(new PointerEvent(type, {
        pointerId: 41, pointerType: 'touch', isPrimary: true, bubbles: true, cancelable: true,
        clientX: x + dx * f, clientY: y + dy * f }));
    fire('pointerdown', 0);
    for (const f of [0.1, 0.3, 0.6, 1]) fire('pointermove', f);
    fire('pointerup', 1);
}"""


def title_is(page, title):
    page.wait_for_function("t => document.querySelector('#playerTitle').textContent === t", arg=title)


class TestSwipesOnWhatIsPlaying:
    def test_swiping_the_mini_bar_left_plays_the_next_song(self, browser, static_server):
        page, errors, _ = open_app(browser, static_server)
        search_and_play(page)
        page.evaluate(SWIPE, [".player-open", -160, 0])
        title_is(page, "Song 2")
        assert [call["options"] for call in native_calls(page, "skip")] == [{"offset": 1}]
        page.wait_for_timeout(300)
        assert page.locator(".sheet-now").count() == 0, "a swipe is not also a tap on the bar"
        assert not errors, errors
        page.close()

    def test_swiping_it_right_asks_for_the_previous_song(self, browser, static_server):
        page, errors, _ = open_app(browser, static_server)
        search_and_play(page, row=1)
        page.evaluate(SWIPE, [".player-open", 160, 0])
        title_is(page, "Song 1")
        assert [call["options"] for call in native_calls(page, "skip")] == [{"offset": -1}]
        assert not errors, errors
        page.close()

    def test_a_short_sideways_drag_changes_nothing(self, browser, static_server):
        page, errors, _ = open_app(browser, static_server)
        search_and_play(page)
        page.evaluate(SWIPE, [".player-open", -30, 0])
        page.wait_for_timeout(600)
        assert native_calls(page, "skip") == []
        assert page.locator("#playerTitle").inner_text() == "Song 1"
        # It springs back to where it was rather than staying displaced.
        assert page.evaluate("() => document.querySelector('#playerArt').style.transform") == ""
        assert not errors, errors
        page.close()

    def test_pulling_the_mini_bar_up_opens_now_playing(self, browser, static_server):
        page, errors, _ = open_app(browser, static_server)
        search_and_play(page)
        page.evaluate(SWIPE, [".player-open", 0, -90])
        page.locator(".sheet-now").wait_for(state="visible")
        assert native_calls(page, "skip") == []
        assert not errors, errors
        page.close()

    def test_swiping_the_cover_changes_the_song_and_keeps_the_card_open(self, browser, static_server):
        page, errors, _ = open_app(browser, static_server)
        search_and_play(page)
        page.click("#playerOpen")
        page.locator(".sheet-now .now-art-shell").wait_for(state="visible")
        page.evaluate(SWIPE, [".sheet-now .now-art-shell", -160, 0])
        page.wait_for_function("() => document.querySelector('.sheet-now .now-title').textContent === 'Song 2'")
        assert page.locator(".sheet-now").is_visible()
        assert not errors, errors
        page.close()


def touch_drag(page, selector, dx, steps=12):
    """A real touch drag through Chromium's input pipeline, not dispatched
    events: only real touches get the implicit pointer capture a phone does."""
    box = page.locator(selector).first.bounding_box()
    x, y = box["x"] + box["width"] * 0.8, box["y"] + box["height"] / 2
    cdp = page.context.new_cdp_session(page)
    point = lambda px: [{"x": px, "y": y}]
    cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": point(x)})
    for i in range(1, steps + 1):
        cdp.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": point(x + dx * i / steps)})
        page.wait_for_timeout(16)
    cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})


class TestRealTouches:
    def test_a_finger_swiping_the_cover_changes_the_song(self, browser, static_server):
        page, errors, _ = open_app(browser, static_server, touch=True)
        search_and_play(page)
        page.click("#playerOpen")
        page.locator(".sheet-now .now-art-shell").wait_for(state="visible")
        # Measured once the card has finished sliding up, or the finger lands
        # where the cover was mid-slide.
        page.wait_for_function("() => !document.querySelector('.sheet-now .sheet-panel').getAnimations().length")
        # A touch is first captured by the cover image under the finger; when
        # the swipe claims it for the cover's frame, the image reports losing
        # it. That is the swipe taking hold, and it must not end the swipe.
        touch_drag(page, ".sheet-now .now-art-shell", -200)
        page.wait_for_function("() => document.querySelector('.sheet-now .now-title').textContent === 'Song 2'")
        assert not errors, errors
        page.close()


class TestWithNoComputer:
    """The app plays with nothing paired, so nothing in it may lead to a screen
    that only says to go and pair."""

    def test_an_artist_page_is_made_from_the_phones_own_search(self, browser, static_server):
        page, errors, requests = open_app(browser, static_server)
        page.click('#tabBar [data-tab="search"]')
        page.fill("#searchInput", "song")
        page.press("#searchInput", "Enter")
        page.locator("#searchResults .collection-row").first.click()
        page.locator(".sheet-catalog .track").first.wait_for()
        assert page.locator(".sheet-catalog .track").count() == len(SONGS)
        assert native_calls(page, "search")[-1]["options"] == {"query": "Phone Artist"}
        # Following is kept on a computer, so it is not offered here.
        assert page.locator(".sheet-catalog .catalog-follow").count() == 0
        assert "Connect a computer" not in page.locator(".sheet-catalog").inner_text()
        assert requests == []
        assert not errors, errors
        page.close()

    def test_the_song_menu_offers_only_what_works_here(self, browser, static_server):
        page, errors, _ = open_app(browser, static_server)
        page.click('#tabBar [data-tab="search"]')
        page.fill("#searchInput", "song")
        page.press("#searchInput", "Enter")
        page.locator("#searchResults .track-more").first.click()
        menu = page.locator(".sheet").last
        menu.get_by_text("Play next").wait_for()
        text = menu.inner_text()
        assert "Add to playlist" in text
        for missing in ("Lyrics", "Download", "Save a copy"):
            assert missing not in text, f"{missing} needs a computer"
        assert native_calls(page, "play") == [], "the menu button must not also play the row"
        assert not errors, errors
        page.close()

    def test_now_playing_leaves_out_lyrics_and_fits_the_screen(self, browser, static_server):
        page, errors, _ = open_app(browser, static_server)
        search_and_play(page)
        page.click("#playerOpen")
        page.locator(".sheet-now .now-actions").wait_for(state="visible")
        assert "Lyrics" not in page.locator(".sheet-now .now-actions").inner_text()
        # Everything on the card is reachable without scrolling it.
        assert page.evaluate("""() => {
            const body = document.querySelector('.sheet-now .sheet-body');
            return body.scrollHeight <= body.clientHeight + 1;
        }""")
        assert not errors, errors
        page.close()

    def test_now_playing_still_fits_with_taller_text(self, browser, static_server):
        page, errors, _ = open_app(browser, static_server)
        # Another platform's fonts, or a larger text setting, make every line
        # taller; the cover is what gives way, and it stays square.
        page.add_style_tag(content=".sheet-now * { line-height: 1.7 !important; }")
        search_and_play(page)
        page.click("#playerOpen")
        page.locator(".sheet-now .now-actions").wait_for(state="visible")
        assert page.evaluate("""() => {
            const body = document.querySelector('.sheet-now .sheet-body');
            return body.scrollHeight <= body.clientHeight + 1;
        }""")
        art = page.locator(".sheet-now .now-art-shell").bounding_box()
        assert art["height"] >= 140 and abs(art["width"] - art["height"]) < 1, art
        assert not errors, errors
        page.close()

    def test_the_library_holds_what_this_phone_has_played(self, browser, static_server):
        page, errors, requests = open_app(browser, static_server)
        search_and_play(page)
        page.click('#tabBar [data-tab="library"]')
        page.locator("#libraryList .track").first.wait_for()
        assert "Song 1" in page.locator("#libraryList").inner_text()
        page.click("#libraryModeArtists")
        page.locator("#libraryList .collection-row").first.wait_for()
        assert "Phone Artist" in page.locator("#libraryList").inner_text()
        assert requests == []
        assert not errors, errors
        page.close()


class TestSearchFollowsTyping:
    def test_results_arrive_once_typing_pauses(self, browser, static_server):
        page, errors, _ = open_app(browser, static_server)
        page.click('#tabBar [data-tab="search"]')
        page.locator("#searchInput").press_sequentially("song", delay=40)
        page.locator("#searchResults .track").first.wait_for()
        # One search for the word, not one per letter.
        assert [call["options"] for call in native_calls(page, "search")] == [{"query": "song"}]
        assert not errors, errors
        page.close()

    def test_enter_after_typing_does_not_search_again(self, browser, static_server):
        page, errors, _ = open_app(browser, static_server)
        page.click('#tabBar [data-tab="search"]')
        page.locator("#searchInput").press_sequentially("song", delay=40)
        page.locator("#searchResults .track").first.wait_for()
        # The source answers the same words in a new order, so a second search
        # would reshuffle the list under the finger about to tap it.
        page.press("#searchInput", "Enter")
        page.wait_for_timeout(300)
        assert len(native_calls(page, "search")) == 1
        assert not errors, errors
        page.close()

    def test_enter_retries_a_search_that_failed(self, browser, static_server):
        page, errors, _ = open_app(browser, static_server)
        page.evaluate("() => { window.__native.failures.search = { code: 'offline', message: 'x' }; }")
        page.click('#tabBar [data-tab="search"]')
        page.fill("#searchInput", "song")
        page.press("#searchInput", "Enter")
        page.wait_for_function("() => document.querySelector('#searchMessage').textContent.includes('No internet')")
        page.press("#searchInput", "Enter")
        page.locator("#searchResults .track").first.wait_for()
        assert len(native_calls(page, "search")) == 2
        assert not errors, errors
        page.close()

    def test_coming_back_to_results_does_not_raise_the_keyboard(self, browser, static_server):
        page, errors, _ = open_app(browser, static_server)
        page.click('#tabBar [data-tab="search"]')
        page.fill("#searchInput", "song")
        page.press("#searchInput", "Enter")
        page.locator("#searchResults .track").first.wait_for()
        page.click('#tabBar [data-tab="home"]')
        page.click('#tabBar [data-tab="search"]')
        assert page.evaluate("() => document.activeElement?.id") != "searchInput"
        assert not errors, errors
        page.close()


def tap_middle(page, selector):
    box = page.locator(selector).first.bounding_box()
    page.touchscreen.tap(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)


class TestOnATouchscreen:
    """A finger is wide and the artist line is half of a row, so on a phone
    the row is one target. The artist is in the row's menu and Now Playing."""

    def test_a_tap_on_the_artist_line_plays_the_song(self, browser, static_server):
        page, errors, _ = open_app(browser, static_server, touch=True)
        page.click('#tabBar [data-tab="search"]')
        page.fill("#searchInput", "song")
        page.press("#searchInput", "Enter")
        tap_middle(page, "#searchResults .track .link-inline")
        page.wait_for_function("() => document.querySelector('#playPauseButton').dataset.state === 'playing'")
        assert page.locator(".sheet-catalog").count() == 0
        assert not errors, errors
        page.close()

    def test_a_tap_on_the_mini_bars_artist_opens_now_playing(self, browser, static_server):
        page, errors, _ = open_app(browser, static_server, touch=True)
        search_and_play(page)
        tap_middle(page, "#playerArtist")
        page.locator(".sheet-now").wait_for(state="visible")
        assert page.locator(".sheet-catalog").count() == 0
        assert not errors, errors
        page.close()

    def test_the_volume_slider_ends_where_the_phone_can_play(self, browser, static_server):
        page, errors, _ = open_app(browser, static_server, touch=True)
        search_and_play(page)
        page.click("#playerOpen")
        # The app's own player stops at 100%; a slider running to 200% put
        # full volume in the middle and sprang back from everything above it.
        slider = page.locator('.sheet-now [role="slider"][aria-label="Volume"]')
        assert slider.get_attribute("aria-valuemax") == "100"
        assert not errors, errors
        page.close()
