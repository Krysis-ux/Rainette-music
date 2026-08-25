"""How a resolved stream is asked for, and what happens when upstream says no.

Two live bugs, both of which read to the user as "no music plays, it says the
format is unsupported": yt-dlp's default player client returns chunked URLs that
refuse the open-ended range a media element opens with, and upstream failures
were streamed to the element as though they were audio.
"""

import unittest

from aiohttp.test_utils import TestClient, TestServer

from companion import CompanionRegistry
import music_bridge
import server

ORIGIN = "https://music-pwa-web.vercel.app"
UPSTREAM = "https://rr1---sn-x.googlevideo.com/videoplayback?x=1"
SOURCE_ID = "test-source-id"

# The shape yt-dlp returns: an identity that has nothing to do with the phone.
RESOLVED_HEADERS = {
    "User-Agent": "com.google.android.youtube/1.0 (Linux; Android 11)",
    "Accept-Language": "en-us,en;q=0.5",
}


class _FakeContent:
    def __init__(self, payload):
        self._payload = payload

    async def iter_chunked(self, _size):
        if self._payload:
            yield self._payload


class _FakeUpstream:
    def __init__(self, status=200, payload=b"audio-bytes", headers=None):
        self.status = status
        self.headers = headers if headers is not None else {"Content-Type": "audio/mp4"}
        self.content = _FakeContent(payload)
        self.released = False

    def release(self):
        self.released = True


class _RecordingSession:
    def __init__(self, upstream=None):
        self.upstream = upstream or _FakeUpstream()
        self.seen = []

    async def get(self, url, headers=None):
        self.seen.append({"url": url, "headers": dict(headers or {})})
        return self.upstream

    async def close(self):
        """The app closes whatever session it holds on cleanup."""


async def _install(app, session):
    app[server.CLIENT_KEY] = session


class _StubYDL:
    """Stands in for yt_dlp.YoutubeDL, answering per player client.

    `_extract_stream` picks a client by putting it in `extractor_args`, so the
    stub reads the options back out to decide which canned answer to give --
    the same way the real extractor's behaviour varies by client.
    """

    calls: list[str | None] = []

    def __init__(self, opts):
        client = (opts.get("extractor_args", {})
                      .get("youtube", {})
                      .get("player_client", [None]))[0]
        self._client = client
        type(self).calls.append(client)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=False):
        answer = self.answers.get(self._client)
        if isinstance(answer, Exception):
            raise answer
        if answer is None:
            raise RuntimeError(f"no canned answer for client {self._client!r}")
        return dict(answer)


def _audio_only(url="https://rr1---sn-x.googlevideo.com/videoplayback?a=1"):
    """What a healthy resolve looks like: format 140, no video track."""
    return {"url": url, "vcodec": "none", "acodec": "mp4a.40.2", "ext": "m4a",
            "format_id": "140", "title": "T", "uploader": "U", "duration": 1,
            "http_headers": dict(RESOLVED_HEADERS)}


def _muxed(url="https://rr1---sn-x.googlevideo.com/videoplayback?a=18"):
    """Format 18: plays, but it is a 360p video wearing `video/mp4`."""
    return {"url": url, "vcodec": "avc1.42001E", "acodec": "mp4a.40.2", "ext": "mp4",
            "format_id": "18", "title": "T", "uploader": "U", "duration": 1,
            "http_headers": dict(RESOLVED_HEADERS)}


class PlayerClientTests(unittest.TestCase):
    """What a resolve must *produce*, rather than which client it must ask.

    The previous version of this class asserted that ANDROID was pinned. That
    assertion passed on the day every phone stopped playing: YouTube had cut
    ANDROID back to format 18 alone, so the pin was still in place and still
    green while it served a muxed video to every <audio> element. Pinning a
    client is a bet on a third party; the property worth protecting is that
    whatever comes back is audio.
    """

    def setUp(self):
        self._real = music_bridge.yt_dlp.YoutubeDL
        _StubYDL.calls = []

    def tearDown(self):
        music_bridge.yt_dlp.YoutubeDL = self._real
        music_bridge._last_muxed_fallback = None

    def _use(self, answers):
        _StubYDL.answers = answers
        music_bridge.yt_dlp.YoutubeDL = _StubYDL

    def test_yt_dlps_own_default_is_among_the_candidates(self):
        """Never asking the default is how a stale pin survives a yt-dlp fix."""
        self.assertIn(
            None, music_bridge._PLAYER_CLIENT_CANDIDATES,
            "letting yt-dlp choose has to stay an option: it is the only "
            "candidate that improves when yt-dlp ships an extractor fix",
        )

    def test_an_audio_only_stream_is_preferred_over_a_muxed_one(self):
        """The regression: a video format must never win while audio exists."""
        self._use({None: _muxed(), "android": _audio_only()})
        got = music_bridge._extract_stream("abc123")
        self.assertEqual(got["url"], _audio_only()["url"])
        self.assertIsNone(music_bridge.last_muxed_fallback())

    def test_the_first_healthy_client_wins_without_asking_the_rest(self):
        self._use({None: _audio_only(), "android": _muxed()})
        got = music_bridge._extract_stream("abc123")
        self.assertEqual(got["url"], _audio_only()["url"])
        self.assertEqual(_StubYDL.calls, [None])

    def test_video_only_still_plays_but_is_recorded(self):
        """Silence is worse than video -- but an invisible fallback is worst."""
        self._use({client: _muxed() for client in music_bridge._PLAYER_CLIENT_CANDIDATES})
        got = music_bridge._extract_stream("abc123")
        self.assertEqual(got["url"], _muxed()["url"])
        noted = music_bridge.last_muxed_fallback()
        self.assertIsNotNone(noted, "a muxed fallback that nobody can see is the bug itself")
        self.assertEqual(noted["format_id"], "18")

    def test_a_client_youtube_has_stopped_serving_is_skipped(self):
        self._use({None: RuntimeError("The page needs to be reloaded"),
                   "android": _audio_only()})
        got = music_bridge._extract_stream("abc123")
        self.assertEqual(got["url"], _audio_only()["url"])

    def test_every_candidate_failing_raises_rather_than_returning_nothing(self):
        self._use({c: RuntimeError("nope") for c in music_bridge._PLAYER_CLIENT_CANDIDATES})
        with self.assertRaises(RuntimeError):
            music_bridge._extract_stream("abc123")


class AudioRelayTests(unittest.IsolatedAsyncioTestCase):
    def tearDown(self):
        music_bridge._stream_cache_invalidate(SOURCE_ID)

    async def _serve(self, session, *, source_id=SOURCE_ID):
        registry = CompanionRegistry(now=lambda: 1_000)
        invitation = registry.create_invitation(ttl_s=60)
        request = registry.request_pairing(invitation["token"], "Pixel")
        approved = registry.approve(request["request_id"])
        grant = registry.create_relay_grant(
            approved["device_id"], UPSTREAM, ttl_s=600, source_id=source_id
        )
        app = server.build_companion_app(registry, allowed_origins={ORIGIN})
        app.on_startup.append(lambda instance: _install(instance, session))
        client = TestClient(TestServer(app))
        await client.start_server()
        return client, grant

    async def test_relay_asks_upstream_in_the_resolvers_voice(self):
        """The identity that minted the URL is the one that may redeem it."""
        music_bridge._stream_cache_set(SOURCE_ID, url=UPSTREAM, http_headers=RESOLVED_HEADERS)
        session = _RecordingSession()
        client, grant = await self._serve(session)
        try:
            response = await client.get(
                "/audio/" + grant["token"],
                headers={"Origin": ORIGIN, "User-Agent": "iPhone-Safari-UA"},
            )
            self.assertEqual(response.status, 200)
            sent = session.seen[0]["headers"]
            self.assertEqual(sent.get("User-Agent"), RESOLVED_HEADERS["User-Agent"])
            self.assertNotIn("iPhone", sent.get("User-Agent", ""))
        finally:
            await client.close()

    async def test_the_callers_own_range_is_forwarded_untouched(self):
        """Range is the one header that genuinely belongs to the caller."""
        music_bridge._stream_cache_set(SOURCE_ID, url=UPSTREAM, http_headers=RESOLVED_HEADERS)
        session = _RecordingSession(_FakeUpstream(status=206))
        client, grant = await self._serve(session)
        try:
            await client.get(
                "/audio/" + grant["token"],
                headers={"Origin": ORIGIN, "Range": "bytes=0-"},
            )
            self.assertEqual(session.seen[0]["headers"].get("Range"), "bytes=0-")
        finally:
            await client.close()

    async def test_an_unresolved_source_still_reaches_upstream(self):
        """An older grant carries no source; it must degrade, not break."""
        session = _RecordingSession()
        client, grant = await self._serve(session, source_id="")
        try:
            response = await client.get(
                "/audio/" + grant["token"],
                headers={"Origin": ORIGIN, "User-Agent": "iPhone-Safari-UA"},
            )
            self.assertEqual(response.status, 200)
            self.assertEqual(session.seen[0]["headers"].get("User-Agent"), "iPhone-Safari-UA")
        finally:
            await client.close()

    async def test_an_upstream_refusal_is_never_streamed_as_audio(self):
        """A 403 page fed to <audio> is what produced the useless toast."""
        refusal = _FakeUpstream(status=403, payload=b"<html>denied</html>",
                                headers={"Content-Type": "text/html"})
        client, grant = await self._serve(_RecordingSession(refusal), source_id="")
        try:
            response = await client.get("/audio/" + grant["token"], headers={"Origin": ORIGIN})
            body = await response.read()

            self.assertEqual(response.status, 502)
            self.assertNotIn(b"denied", body)
            self.assertEqual(response.headers.get("Access-Control-Allow-Origin"), ORIGIN)
        finally:
            await client.close()


class _StatusSequenceSession:
    """Answers per URL, so a retry against a fresh URL can differ from the first."""

    def __init__(self, by_url):
        self._by_url = by_url
        self.seen = []

    async def get(self, url, headers=None):
        self.seen.append({"url": url, "headers": dict(headers or {})})
        return self._by_url[url]

    async def close(self):
        return None


class RelayRetryIdentityTests(unittest.IsolatedAsyncioTestCase):
    """A retry has to carry the identity of the URL it is retrying."""

    FRESH = "https://rr1---sn-x.googlevideo.com/videoplayback?x=2"
    FRESH_HEADERS = {"User-Agent": "com.google.android.youtube/2.0 (Linux; Android 14)"}

    def tearDown(self):
        music_bridge._stream_cache_invalidate(SOURCE_ID)

    async def test_headers_are_refreshed_alongside_the_reresolved_url(self):
        # The grant's current URL is stale and its identity is refused.
        music_bridge._stream_cache_set(SOURCE_ID, url=UPSTREAM, http_headers=RESOLVED_HEADERS)
        session = _StatusSequenceSession({
            UPSTREAM: _FakeUpstream(status=403, payload=b"denied",
                                    headers={"Content-Type": "text/html"}),
            self.FRESH: _FakeUpstream(status=206, headers={"Content-Type": "audio/mp4"}),
        })

        def _reresolved(source_id, force_refresh=False):
            # A real re-resolve rewrites both halves of the pair.
            music_bridge._stream_cache_set(
                SOURCE_ID, url=self.FRESH, http_headers=self.FRESH_HEADERS)
            return self.FRESH

        registry = CompanionRegistry(now=lambda: 1_000)
        invitation = registry.create_invitation(ttl_s=60)
        request = registry.request_pairing(invitation["token"], "Pixel")
        approved = registry.approve(request["request_id"])
        grant = registry.create_relay_grant(
            approved["device_id"], UPSTREAM, ttl_s=600, source_id=SOURCE_ID)
        app = server.build_companion_app(registry, allowed_origins={ORIGIN})
        app.on_startup.append(lambda instance: _install(instance, session))
        client = TestClient(TestServer(app))
        await client.start_server()

        real = music_bridge.resolve_stream_url_sync
        music_bridge.resolve_stream_url_sync = _reresolved
        try:
            response = await client.get(
                "/audio/" + grant["token"],
                headers={"Origin": ORIGIN, "Range": "bytes=0-"},
            )
            self.assertEqual(response.status, 206)
            self.assertEqual(len(session.seen), 2, "the 403 should have been retried once")
            self.assertEqual(session.seen[1]["url"], self.FRESH)
            self.assertEqual(
                session.seen[1]["headers"].get("User-Agent"),
                self.FRESH_HEADERS["User-Agent"],
                "the retry re-sent the identity upstream had just refused, so it "
                "could only fail the same way -- and the fault looked like the phone's",
            )
            self.assertEqual(session.seen[1]["headers"].get("Range"), "bytes=0-")
        finally:
            music_bridge.resolve_stream_url_sync = real
            await client.close()


class ResolvedHeaderPlumbingTests(unittest.TestCase):
    def tearDown(self):
        music_bridge._stream_cache_invalidate(SOURCE_ID)

    def test_resolver_keeps_the_headers_its_url_was_signed_for(self):
        class _FakeYDL:
            def __init__(self, _opts):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            def extract_info(self, _url, download=False):
                return {"url": UPSTREAM, "http_headers": RESOLVED_HEADERS,
                        "title": "T", "uploader": "A", "duration": 10}

        original = music_bridge.yt_dlp.YoutubeDL
        music_bridge.yt_dlp.YoutubeDL = _FakeYDL
        try:
            found = music_bridge._extract_stream(SOURCE_ID)
        finally:
            music_bridge.yt_dlp.YoutubeDL = original
        self.assertEqual(found["http_headers"], RESOLVED_HEADERS)

    def test_cached_headers_are_what_the_relay_reads_back(self):
        music_bridge._stream_cache_set(SOURCE_ID, url=UPSTREAM, http_headers=RESOLVED_HEADERS)
        self.assertEqual(music_bridge.stream_request_headers(SOURCE_ID), RESOLVED_HEADERS)

    def test_an_unknown_source_reports_no_headers_rather_than_guessing(self):
        self.assertEqual(music_bridge.stream_request_headers("never-resolved"), {})


class ResolveFailureMessageTests(unittest.TestCase):
    """A blocked network is the one failure worth naming exactly."""

    def test_a_tls_intercepting_network_is_named_as_such(self):
        message = music_bridge.describe_resolve_failure(
            Exception("ERROR: [youtube] X: Unable to download API page: "
                      "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")
        )
        self.assertIn("network", message.lower())
        self.assertNotIn("CERTIFICATE_VERIFY_FAILED", message)

    def test_an_unrecognised_failure_is_passed_through_unchanged(self):
        self.assertEqual(music_bridge.describe_resolve_failure(Exception("weird")), "weird")


if __name__ == "__main__":
    unittest.main()


class PhoneDiagnosticContractTests(unittest.TestCase):
    """What the phone asks when it is trying to explain a failure.

    This is a source-level contract because the thing worth pinning is a single
    header value, and the cost of getting it wrong is not a broken feature but a
    *misleading* one -- which is far more expensive to find.
    """

    @classmethod
    def setUpClass(cls):
        root = __import__("pathlib").Path(__file__).resolve().parents[1]
        cls.player = (root / "pwa" / "src" / "player.js").read_text(encoding="utf-8")
        cls.mirror_root = root

    def _diagnose_body(self, source):
        start = source.index("async function diagnoseStreamFailure")
        return source[start:source.index("\naudio.addEventListener('error'", start)]

    def _diagnose_code(self, source):
        """The body with comments stripped -- prose may discuss the old value."""
        lines = [line for line in self._diagnose_body(source).splitlines()
                 if not line.lstrip().startswith(("//", "*", "/*"))]
        return "\n".join(lines)

    def test_the_diagnostic_opens_the_same_range_a_media_element_does(self):
        body = self._diagnose_code(self.player)
        self.assertIn(
            "Range: 'bytes=0-'", body,
            "the diagnostic has to reproduce what <audio> actually sends. A "
            "bounded 'bytes=0-0' is answered 206 by upstreams that refuse the "
            "open-ended range, so the one failure this function exists to "
            "explain was reported as 'your phone could not play this format'",
        )
        self.assertNotIn("bytes=0-0", body)

    def test_the_mirrored_client_carries_the_same_fix(self):
        """pwa/ and the deployed mirror must not diverge on this of all things."""
        mirror = self.mirror_root.parent / "music-pwa-web" / "src" / "player.js"
        if not mirror.is_file():
            self.skipTest("the Vercel mirror is not checked out beside this repo")
        self.assertEqual(
            self._diagnose_code(mirror.read_text(encoding="utf-8")),
            self._diagnose_code(self.player),
            "the phone runs the mirror, so a fix that lands only in pwa/ never "
            "reaches a single user",
        )

    def test_the_diagnostic_does_not_download_the_track_it_is_diagnosing(self):
        body = self._diagnose_code(self.player)
        self.assertIn(
            "body?.cancel()", body,
            "an open-ended range with no cancel would pull the whole track "
            "down just to read a status code",
        )


class TrustStoreTests(unittest.TestCase):
    """The frozen macOS build shipped with no certificate authorities at all.

    ``no-certifi`` tells yt-dlp to use the operating system's trust store. On
    Windows that picks up enterprise roots, which is the point. On macOS Python
    cannot read the Keychain, and a frozen build's OpenSSL default paths point
    at the *build* machine's Homebrew directory — so it means "no roots", every
    request to YouTube fails before it starts, and the app told users on their
    own home Wi-Fi that a firewall was intercepting the connection.
    """

    def test_system_trust_is_windows_only(self):
        import os
        if os.name == "nt":
            self.assertIn("no-certifi", music_bridge._SYSTEM_TRUST_COMPAT)
        else:
            self.assertEqual(
                music_bridge._SYSTEM_TRUST_COMPAT, set(),
                "off Windows this must be empty so yt-dlp uses the certifi "
                "bundle the app already ships; the OS store is not reachable "
                "from a frozen Python here",
            )

    def test_no_roots_is_reported_as_our_fault_not_the_network(self):
        original = music_bridge._has_trust_roots
        music_bridge._has_trust_roots = lambda: False
        try:
            message = music_bridge.describe_resolve_failure(
                RuntimeError("CERTIFICATE_VERIFY_FAILED certificate verify failed"))
        finally:
            music_bridge._has_trust_roots = original
        self.assertIn("fault in the app", message)
        self.assertNotIn("firewall", message)

    def test_a_real_interception_still_names_the_network(self):
        original = music_bridge._has_trust_roots
        music_bridge._has_trust_roots = lambda: True
        try:
            message = music_bridge.describe_resolve_failure(
                RuntimeError("CERTIFICATE_VERIFY_FAILED certificate verify failed"))
        finally:
            music_bridge._has_trust_roots = original
        self.assertIn("firewall", message)
