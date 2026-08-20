"""Downloading a track onto the computer.

Two things are being protected here and they are different in kind.

The first is a boundary: a phone must not be able to make this computer write
files. The phone has its own download path and its own storage, and the same
reasoning that stops it naming a scan root (``cmd_music_local_roots``) stops it
naming a download. That check is worth a test because it is invisible when it
works and unnoticeable when it breaks.

The second is a shape: a download has to arrive as something the existing
library machinery already understands -- a tagged file, in a watched folder,
complete or not there at all. The partial-file rule matters most: a scan that
runs against a half-written ``.part`` would index a fragment as a song.
"""

import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import local_library
import music_bridge
import shared


class RecordingState:
    """Just enough state to watch the root be registered."""

    def __init__(self):
        self.roots = []

    def add_local_root(self, path):
        self.roots.append(str(path))
        return {"path": str(path)}


class FakeResponse(io.BytesIO):
    """A urlopen result: a readable body plus headers, usable as a context."""

    def __init__(self, body: bytes, content_type: str = "audio/mp4"):
        super().__init__(body)
        self.headers = {"Content-Type": content_type, "Content-Length": str(len(body))}

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()
        return False


class PhoneMayNotWriteToThisComputerTests(unittest.TestCase):
    def test_a_command_stamped_with_a_device_id_is_refused(self):
        """The stamp is the gateway's mark of a command that came from a phone.

        ``server.py`` overwrites whatever the phone supplied, so its presence
        cannot be forged and its absence cannot be faked from off-machine.
        """
        sent = []
        with mock.patch.object(shared, "notify_browsers", sent.append):
            music_bridge.cmd_music_download_track({
                "id": "d1",
                "origin_device_id": "phone-1",
                "track": {"source_id": "abc", "title": "Anything"},
            })

        self.assertEqual(len(sent), 1, sent)
        self.assertFalse(sent[0]["ok"])
        self.assertIn("computer", sent[0]["msg"])

    def test_the_command_is_not_on_the_phone_allowlist(self):
        # Belt and braces: the origin check above is the guard, but the command
        # should never reach it from a phone in the first place.
        import server

        self.assertNotIn("music_download_track", server.COMPANION_COMMAND_TYPES)
        self.assertNotIn("music_download_track", server.COMPANION_ONE_WAY_COMMAND_TYPES)


class FileNameTests(unittest.TestCase):
    def test_characters_no_filesystem_accepts_are_removed(self):
        stem = music_bridge._safe_stem("AC/DC", 'Back: In*Black?')
        for bad in '<>:"/\\|?*':
            self.assertNotIn(bad, stem)

    def test_a_very_long_title_is_cut_rather_than_refused(self):
        stem = music_bridge._safe_stem("Artist", "T" * 400)
        self.assertLessEqual(len(stem), 120)

    def test_a_track_with_no_artist_is_still_named(self):
        self.assertEqual(music_bridge._safe_stem("", "Solo"), "Solo")

    def test_a_track_with_nothing_at_all_still_gets_a_name(self):
        # An empty name would be a file called ".m4a", which is a hidden file on
        # every platform this runs on and invisible in the folder it lands in.
        self.assertTrue(music_bridge._safe_stem("", ""))


class DownloadShapeTests(unittest.TestCase):
    def setUp(self):
        self.body = b"\x00\x00\x00\x20ftypM4A " + b"\xab" * 4096

    def test_the_file_lands_named_after_the_track(self):
        with mock.patch.object(music_bridge, "resolve_stream_url_sync", return_value="https://x/y"), \
             mock.patch.object(music_bridge, "stream_request_headers", return_value={}), \
             mock.patch.object(music_bridge.urllib.request, "urlopen",
                               return_value=FakeResponse(self.body)), \
             mock.patch.object(music_bridge, "_fetch_artwork", return_value=None):
            with tempfile.TemporaryDirectory() as folder:
                landed = music_bridge._download_one(
                    {"source_id": "abc", "title": "Glass Harbour", "artist": "Nova Reef"},
                    Path(folder), lambda *_: None,
                )
                self.assertEqual(landed.name, "Nova Reef - Glass Harbour.m4a")
                self.assertEqual(landed.read_bytes(), self.body)

    def test_nothing_partial_is_left_behind(self):
        """A ``.part`` survivor is the failure this is about.

        ``local_library`` walks the folder on a timer. A fragment left with a
        real audio suffix would be indexed as a song and play as silence, so the
        rename is the last thing that happens and only happens once.
        """
        with mock.patch.object(music_bridge, "resolve_stream_url_sync", return_value="https://x/y"), \
             mock.patch.object(music_bridge, "stream_request_headers", return_value={}), \
             mock.patch.object(music_bridge.urllib.request, "urlopen",
                               return_value=FakeResponse(self.body)), \
             mock.patch.object(music_bridge, "_fetch_artwork", return_value=None):
            with tempfile.TemporaryDirectory() as folder:
                music_bridge._download_one(
                    {"source_id": "abc", "title": "T", "artist": "A"},
                    Path(folder), lambda *_: None,
                )
                left = sorted(p.name for p in Path(folder).iterdir())
                self.assertEqual(left, ["A - T.m4a"])

    def test_an_empty_body_fails_and_leaves_no_file(self):
        with mock.patch.object(music_bridge, "resolve_stream_url_sync", return_value="https://x/y"), \
             mock.patch.object(music_bridge, "stream_request_headers", return_value={}), \
             mock.patch.object(music_bridge.urllib.request, "urlopen",
                               return_value=FakeResponse(b"")), \
             mock.patch.object(music_bridge, "_fetch_artwork", return_value=None):
            with tempfile.TemporaryDirectory() as folder:
                with self.assertRaises(RuntimeError):
                    music_bridge._download_one(
                        {"source_id": "abc", "title": "T", "artist": "A"},
                        Path(folder), lambda *_: None,
                    )
                self.assertEqual(list(Path(folder).iterdir()), [])

    def test_the_suffix_follows_the_content_type(self):
        with mock.patch.object(music_bridge, "resolve_stream_url_sync", return_value="https://x/y"), \
             mock.patch.object(music_bridge, "stream_request_headers", return_value={}), \
             mock.patch.object(music_bridge.urllib.request, "urlopen",
                               return_value=FakeResponse(self.body, "audio/mpeg")), \
             mock.patch.object(music_bridge, "_fetch_artwork", return_value=None):
            with tempfile.TemporaryDirectory() as folder:
                landed = music_bridge._download_one(
                    {"source_id": "abc", "title": "T", "artist": "A"},
                    Path(folder), lambda *_: None,
                )
                self.assertEqual(landed.suffix, ".mp3")

    def test_what_lands_is_something_the_library_counts_as_music(self):
        # The whole design rests on this: the download is handed to the existing
        # scanner rather than to a second code path, so the suffix it writes has
        # to be one the scanner accepts.
        self.assertIn(".m4a", local_library.AUDIO_SUFFIXES)


class WorkerTests(unittest.TestCase):
    def test_the_folder_is_watched_even_when_every_track_fails(self):
        """Registered before anything is written, not after a success.

        A run where every track failed still leaves a folder the user can drop
        files into and have indexed, rather than an orphan nobody watches.
        """
        state = RecordingState()
        sent = []
        with mock.patch.object(shared, "STATE", state), \
             mock.patch.object(shared, "notify_browsers", sent.append), \
             mock.patch.object(music_bridge, "_download_one", side_effect=RuntimeError("no")), \
             mock.patch.object(music_bridge.local_library, "scan", return_value={}), \
             mock.patch.object(music_bridge, "_local_status_payload", return_value={}), \
             tempfile.TemporaryDirectory() as folder:
            with mock.patch.object(music_bridge, "downloads_dir", return_value=Path(folder)):
                music_bridge._download_worker("d2", [{"source_id": "a", "title": "One"}])

        self.assertEqual(state.roots, [folder])
        result = [m for m in sent if m["type"] == "music_download_result"][-1]
        self.assertFalse(result["ok"])
        self.assertEqual(result["failed"], 1)

    def test_one_bad_track_does_not_abandon_the_rest(self):
        state = RecordingState()
        sent = []
        calls = {"n": 0}

        def flaky(track, folder, on_bytes):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("this one is unavailable")
            return Path(folder) / "x.m4a"

        with mock.patch.object(shared, "STATE", state), \
             mock.patch.object(shared, "notify_browsers", sent.append), \
             mock.patch.object(music_bridge, "_download_one", side_effect=flaky), \
             mock.patch.object(music_bridge.local_library, "scan", return_value={}), \
             mock.patch.object(music_bridge, "_local_status_payload", return_value={}), \
             tempfile.TemporaryDirectory() as folder:
            with mock.patch.object(music_bridge, "downloads_dir", return_value=Path(folder)):
                music_bridge._download_worker("d3", [
                    {"source_id": "a", "title": "One"},
                    {"source_id": "b", "title": "Two"},
                    {"source_id": "c", "title": "Three"},
                ])

        result = [m for m in sent if m["type"] == "music_download_result"][-1]
        self.assertEqual((result["done"], result["failed"], result["total"]), (2, 1, 3))
        self.assertTrue(result["ok"])

    def test_the_watched_folder_is_rescanned_once_not_per_track(self):
        # The walk costs the same for one track as for thirty; doing it per
        # track turns a playlist download into thirty walks of the same folder.
        state = RecordingState()
        scans = []
        with mock.patch.object(shared, "STATE", state), \
             mock.patch.object(shared, "notify_browsers", lambda *_: None), \
             mock.patch.object(music_bridge, "_download_one", return_value=Path("x")), \
             mock.patch.object(music_bridge.local_library, "scan",
                               side_effect=lambda *a, **k: scans.append(a) or {}), \
             mock.patch.object(music_bridge, "_local_status_payload", return_value={}), \
             tempfile.TemporaryDirectory() as folder:
            with mock.patch.object(music_bridge, "downloads_dir", return_value=Path(folder)):
                music_bridge._download_worker("d4", [
                    {"source_id": "a", "title": "One"},
                    {"source_id": "b", "title": "Two"},
                ])

        self.assertEqual(len(scans), 1, scans)

    def test_an_empty_list_says_so_rather_than_starting(self):
        sent = []
        with mock.patch.object(shared, "notify_browsers", sent.append):
            music_bridge._download_worker("d5", [])
        self.assertFalse(sent[0]["ok"])
        self.assertEqual(sent[0]["total"], 0)


if __name__ == "__main__":
    unittest.main()
