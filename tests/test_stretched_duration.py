"""Guards against the doubled-playtime bug on macOS and iOS.

AVFoundation reads some YouTube m4a streams as exactly twice their real length
(261.04s for a file AudioToolbox reads as 130.52s). It renders the frames the
file actually has and plays silence for the rest, so the clock stays honest
while the total is wrong and `ended` arrives minutes late. WebKit decodes
through AVFoundation, so the macOS desktop and the iPhone both saw it; Chromium
on Windows did not.

Both players therefore have to treat the library's duration_s as authoritative
and finish on it. These tests pin that, because the failure is silent: the app
still plays, it just lies about the length and then sits quiet.
"""

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DesktopPlayerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.miniplayer = (ROOT / "web" / "miniplayer.js").read_text(encoding="utf-8")

    def test_duration_prefers_the_library_figure(self):
        self.assertIn("function trueDuration()", self.miniplayer)
        self.assertIn("duration_s", self.miniplayer)

    def test_broadcasts_carry_the_corrected_total(self):
        # The modal and the phone render whatever these send.
        self.assertIn("duration: trueDuration()", self.miniplayer)
        self.assertIn("current_time: trueTime()", self.miniplayer)
        self.assertNotIn(
            "duration: audio && Number.isFinite(audio.duration) ? audio.duration",
            self.miniplayer,
        )

    def test_finishes_on_the_real_duration(self):
        self.assertIn("function _guardStretchedEnd()", self.miniplayer)
        self.assertIn("_guardStretchedEnd();", self.miniplayer)
        self.assertIn("function _isStretched()", self.miniplayer)

    def test_seek_labels_do_not_read_the_raw_element(self):
        self.assertNotIn("els.cur.textContent = fmt(audio.currentTime)", self.miniplayer)
        self.assertNotIn("els.dur.textContent = fmt(audio.duration)", self.miniplayer)


class PhonePlayerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        pwa = ROOT / "pwa" / "src"
        cls.player = (pwa / "player.js").read_text(encoding="utf-8")
        cls.state = (pwa / "state.js").read_text(encoding="utf-8")
        cls.tracks = (pwa / "tracks.js").read_text(encoding="utf-8")

    def test_track_duration_reads_the_field_the_computer_actually_sends(self):
        # The desktop calls this duration_s everywhere; reading `duration` alone
        # returned undefined, which is what left audio.duration unchallenged.
        self.assertIn("export function trackDuration(track)", self.state)
        self.assertIn("track?.duration_s", self.state)

    def test_duration_prefers_the_library_figure(self):
        self.assertIn("return trackDuration(state.currentTrack)", self.player)
        self.assertNotIn("Number(state.currentTrack?.duration) || 0", self.player)

    def test_finishes_on_the_real_duration(self):
        self.assertIn("function guardTrueEnd()", self.player)
        self.assertIn("guardTrueEnd();", self.player)
        self.assertIn("function isStretched()", self.player)

    def test_row_and_queue_totals_use_the_same_helper(self):
        self.assertIn("trackDuration(track)", self.tracks)
        self.assertIn("trackDuration(track)", self.state)
        self.assertNotIn("Number(track.duration)", self.tracks)


if __name__ == "__main__":
    unittest.main()
