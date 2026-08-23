"""Live canary: is what YouTube hands us today still *audio*, and still openable?

Every other test in this suite runs against a stub, which is right -- CI must not
depend on a third party's mood. But the two failures that have actually stopped
the music both happened *outside* the code, in what YouTube chose to serve, and
were therefore invisible to a green suite:

  * July 2026 -- the default player client began returning chunk-served URLs
    that answer the open-ended ``Range: bytes=0-`` a media element opens with as
    403. Every track failed at its first byte.
  * August 2026 -- the ANDROID client we had pinned in response was cut back to
    format 18 alone, a muxed 360p *video*. Every phone was handed ``video/mp4``
    to play in an ``<audio>`` element.

Neither is a bug anyone can write a unit test for, because neither is in our
code. What can be tested is the contract we depend on, against the real thing.
That is what this file does, and it is why it is opt-in rather than deleted:

    RAINETTE_LIVE_CANARY=1 python -m pytest tests/test_stream_canary.py -v

Run it on a schedule. When it goes red, playback is *already* broken for users
and the message they will report is "it says the format is not supported" --
which points at neither cause.
"""

from __future__ import annotations

import os
import unittest
import urllib.error
import urllib.request

import music_bridge

LIVE = os.environ.get("RAINETTE_LIVE_CANARY") == "1"

# Failures that mean "we never got an answer", as opposed to "the answer was
# wrong". The distinction is the whole value of this file: a canary that cannot
# tell being blocked from being broken cries wolf, and a check that is always
# red is worse than no check, because people stop reading it.
#
# YouTube refuses datacenter IPs with a bot challenge, so this is the normal
# outcome on a cloud CI runner and says nothing at all about our code.
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
