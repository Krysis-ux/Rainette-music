"""Live canary: is what YouTube hands us today still *audio*, and still openable?"""

from __future__ import annotations

import os
import unittest
import urllib.error
import urllib.request

import music_bridge

LIVE = os.environ.get("RAINETTE_LIVE_CANARY") == "1"

# Failures that mean "we never got an answer", as opposed to "the answer was wrong".
_INCONCLUSIVE = (
    "sign in to confirm",          # bot challenge -- datacenter IP
    "not a bot",
    "http error 429",              # rate limited
    "too many requests",
    "certificate_verify_failed",   # a network intercepting TLS
    "certificate verify failed",
    "unable to download",          # transient network
    "the page needs to be reloaded",
    "video unavailable",           # the sample went away, not a format change
    "private video",
)


def _is_inconclusive(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _INCONCLUSIVE)

# Long-lived, widely-mirrored, unlikely to be taken down. Two of them so a
# single odd video cannot decide the verdict either way.
CANARY_SOURCES = ("dQw4w9WgXcQ", "9bZkp7q19f0")


def _open_like_a_media_element(url: str, headers: dict[str, str]) -> tuple[int, str]:
    """Exactly what <audio> does on load: an unbounded range, nothing else."""
    request = urllib.request.Request(url, headers={**headers, "Range": "bytes=0-"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        return exc.code, ""


@unittest.skipUnless(LIVE, "set RAINETTE_LIVE_CANARY=1 to check against real YouTube")
class StreamCanaryTests(unittest.TestCase):
    def setUp(self):
        music_bridge._last_muxed_fallback = None

    def _resolve_or_skip(self, source_id):
        """Resolve, or declare the run inconclusive -- never a false alarm."""
        music_bridge._stream_cache_invalidate(source_id)
        try:
            return music_bridge._extract_stream(source_id)
        except Exception as exc:
            if _is_inconclusive(exc):
                self.skipTest(
                    f"YouTube would not answer this host, so nothing was "
                    f"measured: {str(exc)[:160]}"
                )
            raise

    def test_a_resolved_track_is_audio_and_opens_on_an_unbounded_range(self):
        for source_id in CANARY_SOURCES:
            with self.subTest(source=source_id):
                resolved = self._resolve_or_skip(source_id)
                status, content_type = _open_like_a_media_element(
                    resolved["url"], resolved["http_headers"])
                if status in (403, 429):
                    self.skipTest(
                        f"upstream returned {status} to this host before any "
                        f"format could be judged"
                    )

                self.assertIn(
                    status, (200, 206),
                    f"upstream answered {status} to the open-ended range a media "
                    f"element opens with. This is the July 2026 failure: the URL "
                    f"is chunk-served, and every track will fail at its first byte "
                    f"with an error that only says 'format not supported'.",
                )
                self.assertTrue(
                    content_type.startswith("audio/"),
                    f"the stream is being served as {content_type!r}. This is the "
                    f"August 2026 failure: a muxed video format is playing through "
                    f"an <audio> element, wasting the bandwidth of a video to hear "
                    f"a song and failing outright on stricter mobile engines.",
                )

    def test_no_muxed_fallback_was_needed(self):
        """A fallback still plays, so only this names it before users do."""
        for source_id in CANARY_SOURCES:
            self._resolve_or_skip(source_id)
        fallback = music_bridge.last_muxed_fallback()
        self.assertIsNone(
            fallback,
            f"no player client offered an audio-only format: {fallback}. Playback "
            f"still works, but every phone is now downloading video to hear audio "
            f"-- add a healthy client to music_bridge._PLAYER_CLIENT_CANDIDATES.",
        )


if __name__ == "__main__":
    unittest.main()
