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


class PlayerClientTests(unittest.TestCase):
    """The pin that keeps playable URLs coming back."""

    def test_stream_resolution_pins_a_progressive_player_client(self):
        client = (music_bridge._STREAM_OPTS["extractor_args"]["youtube"]["player_client"])
        self.assertIn(
            "android", client,
            "the ANDROID client is what returns a progressive URL; without it "
            "yt-dlp falls back to a chunk-served one that a browser's <audio> "
            "cannot open, and every track fails as a format error",
        )
        self.assertNotIn("android_vr", [c.lower() for c in client])


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
