"""The computer's own MP3s: scanning, identity, and serving the bytes.

The properties under test here are the ones whose failure is silent and
expensive. A scanner that deletes looks fine until somebody unplugs a drive; an
identity derived from the path looks fine until somebody reorganises a folder.
Both destroy playlists, and both would be discovered by a user rather than by a
crash, so they are pinned here.
"""

import ntpath
import os
import tempfile
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

import local_library
import music_bridge
import server
import shared
import state
from companion import CompanionRegistry
from state import MusicState


ORIGIN = "https://music-pwa-web.vercel.app"

# Enough of a file to have a size and be readable. Nothing here parses audio:
# mutagen is optional, and every assertion below is about identity, walking, or
# HTTP rather than about tags.
TRACK_BYTES = b"ID3\x03\x00\x00\x00" + bytes(range(256)) * 8

# One silent MPEG-1 Layer III frame: 128 kbps, 44.1 kHz, so 144*128000/44100 =
# 417 bytes. Forty of them is about a second, which is enough for mutagen to
# recognise the file and report a real duration.
MP3_FRAME = b"\xff\xfb\x90\x00" + bytes(413)
REAL_MP3_BYTES = MP3_FRAME * 40


def write_track(folder: Path, name: str, *, body: bytes = TRACK_BYTES, mtime: float = 1_700_000_000.5) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(body)
    os.utime(path, (mtime, mtime))
    return path


class LocalScannerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.music = self.root / "Music"
        self.state = MusicState(self.root / "music.db")

    def tearDown(self):
        self.tmp.cleanup()

    def register_and_scan(self, root=None):
        target = str(root or self.music)
        self.state.add_local_root(target)
        return local_library.scan(self.state, [target])

    # ── What counts as music ────────────────────────────────────────────────

    def test_extension_set_matches_the_phone(self):
        """`AUDIO_PATTERN` at pwa/src/local.js:149, transcribed.

        If the two ends disagree, the same folder is a different library
        depending on which screen you are looking at.
        """
        for name in ("a.mp3", "a.m4a", "a.aac", "a.flac", "a.wav", "a.ogg",
                     "a.oga", "a.opus", "a.weba", "a.webm", "a.aif", "a.aiff",
                     "a.alac", "A.MP3", "a.FLAC"):
            self.assertTrue(local_library.is_audio_file(name), name)
        for name in ("a.txt", "a.mp4", "a.jpg", "a", "a.mp3.txt", ""):
            self.assertFalse(local_library.is_audio_file(name), name)

    def test_filename_fallback_matches_the_phone(self):
        """Ported from `parseFilename`, pwa/src/local.js:207."""
        self.assertEqual(local_library.parse_filename("01 - Nils - Says.mp3"),
                         {"artist": "Nils", "title": "Says"})
        self.assertEqual(local_library.parse_filename("Nils - Says.flac"),
                         {"artist": "Nils", "title": "Says"})
        self.assertEqual(local_library.parse_filename("Nils - Says - Reprise.mp3"),
                         {"artist": "Nils", "title": "Says - Reprise"})
        self.assertEqual(local_library.parse_filename("justatitle.opus"),
                         {"artist": "", "title": "justatitle"})
        self.assertEqual(local_library.parse_filename(""),
                         {"artist": "", "title": "Unknown"})

    # ── Tags ────────────────────────────────────────────────────────────────

    def test_tags_win_over_the_filename_and_the_filename_carries_the_gap(self):
        write_track(self.music, "01 - Folder Artist - Folder Title.mp3")
        write_track(self.music, "02 - Untagged Artist - Untagged Title.mp3")
        tagged = {}

        def fake_read_tags(path):
            if "Folder Title" in str(path):
                return {"title": "Tagged Title", "artist": "Tagged Artist",
                        "album": "Tagged Album", "duration_s": 214.5}
            return {}

        real = local_library.read_tags
        local_library.read_tags = fake_read_tags
        try:
            self.register_and_scan()
        finally:
            local_library.read_tags = real

        tagged = {t["title"]: t for t in self.state.list_music_tracks()}
        self.assertEqual(tagged["Tagged Title"]["artist"], "Tagged Artist")
        self.assertEqual(tagged["Tagged Title"]["duration_s"], 214.5)
        self.assertEqual(tagged["Tagged Title"]["metadata"]["album_name"], "Tagged Album")
        # No tags at all is not a failure; the filename is still information.
        self.assertEqual(tagged["Untagged Title"]["artist"], "Untagged Artist")

    def test_a_computer_without_mutagen_still_builds_a_library(self):
        """The dependency is optional in the way yt-dlp is in music_bridge.

        The fixture is deliberately a file whose tags *would* be readable, so
        this asserts the guard clause rather than an unparseable file.
        """
        path = write_track(self.music, "01 - Nils - Says.mp3", body=REAL_MP3_BYTES)
        if local_library.MUTAGEN_AVAILABLE:
            from mutagen.id3 import ID3, TIT2
            tags = ID3()
            tags.add(TIT2(encoding=3, text="Tagged Title"))
            tags.save(str(path))

        available = local_library.MUTAGEN_AVAILABLE
        local_library.MUTAGEN_AVAILABLE = False
        try:
            self.assertEqual(local_library.read_tags(str(path)), {})
            result = self.register_and_scan()
        finally:
            local_library.MUTAGEN_AVAILABLE = available

        self.assertEqual(result["added"], 1)
        row = self.state.list_music_tracks()[0]
        self.assertEqual((row["title"], row["artist"]), ("Says", "Nils"))
        self.assertIsNone(row["duration_s"])

    def test_real_id3_tags_are_read_when_mutagen_is_installed(self):
        if not local_library.MUTAGEN_AVAILABLE:
            self.skipTest("mutagen is not installed in this environment")
        from mutagen.id3 import ID3, TALB, TIT2, TPE1

        path = write_track(self.music, "01 - Filename Artist - Filename Title.mp3",
                           body=REAL_MP3_BYTES)
        tags = ID3()
        tags.add(TIT2(encoding=3, text="Real Title"))
        tags.add(TPE1(encoding=3, text="Real Artist"))
        tags.add(TALB(encoding=3, text="Real Album"))
        tags.save(str(path))

        self.register_and_scan()

        row = self.state.list_music_tracks()[0]
        self.assertEqual(row["title"], "Real Title")
        self.assertEqual(row["artist"], "Real Artist")
        self.assertEqual(row["metadata"]["album_name"], "Real Album")
        # A real duration, which the phone's own reader cannot produce at all —
        # pwa/src/local.js:196 hardcodes zero.
        self.assertGreater(row["duration_s"], 0.5)

    # ── Walking ─────────────────────────────────────────────────────────────

    def test_scan_walks_subfolders_and_skips_hidden_and_non_audio(self):
        write_track(self.music / "Album", "01 - Nils - Says.mp3")
        write_track(self.music / "Album" / "Extras", "Nils - Coda.flac")
        write_track(self.music, ".hidden.mp3")
        write_track(self.music / ".hidden_folder", "buried.mp3")
        write_track(self.music / "node_modules", "vendored.mp3")
        (self.music / "notes.txt").write_bytes(b"not music")

        result = self.register_and_scan()

        self.assertEqual(result["scanned"], 2)
        self.assertEqual(result["added"], 2)
        titles = sorted(t["title"] for t in self.state.list_music_tracks())
        self.assertEqual(titles, ["Coda", "Says"])

    def test_skip_dir_names_fold_case_the_way_windows_does(self):
        """``node_modules`` is skipped by exact name on POSIX; on a
        case-folding filesystem a differently-cased ``Node_Modules`` is the
        same directory entry and must be skipped too.
        """
        write_track(self.music, "01 - Nils - Says.mp3")
        write_track(self.music / "Node_Modules", "vendored.mp3")

        original_normcase = os.path.normcase
        os.path.normcase = str.lower
        try:
            result = self.register_and_scan()
        finally:
            os.path.normcase = original_normcase

        self.assertEqual(result["scanned"], 1)
        titles = [t["title"] for t in self.state.list_music_tracks()]
        self.assertEqual(titles, ["Says"])

    def test_symlinks_pointing_outside_the_root_are_never_followed(self):
        """A folder of symlinks must not become a file-disclosure primitive.

        Grants name a track id rather than a path, so this is the second layer
        rather than the first — which is precisely the layer that has to hold
        when the first one turns out to be wrong.
        """
        secret_dir = self.root / "private"
        secret = write_track(secret_dir, "id_rsa.mp3")
        write_track(self.music, "Nils - Real.mp3")
        try:
            os.symlink(secret, self.music / "stolen.mp3")
            os.symlink(secret_dir, self.music / "stolen_folder")
        except (OSError, NotImplementedError):
            self.skipTest("this platform will not create symlinks")

        found = sorted(os.path.basename(p) for p in local_library.iter_audio_files(str(self.music)))

        self.assertEqual(found, ["Nils - Real.mp3"])
        result = self.register_and_scan()
        self.assertEqual(result["scanned"], 1)
        paths = [t["file_path"] for t in self.state.list_music_tracks()]
        secret_real = os.path.realpath(str(secret_dir))
        self.assertFalse(any(p.startswith(secret_real) for p in paths), paths)

    def test_a_symlink_that_stays_inside_the_root_is_still_scanned(self):
        """Containment, not a blanket ban on symlinks.

        Somebody who organises their library with links inside it has done
        nothing wrong, and refusing those would be a different bug.
        """
        real = write_track(self.music / "Album", "Nils - Says.mp3")
        try:
            os.symlink(real, self.music / "Favourites.mp3")
        except (OSError, NotImplementedError):
            self.skipTest("this platform will not create symlinks")

        found = sorted(os.path.basename(p) for p in local_library.iter_audio_files(str(self.music)))
        self.assertEqual(found, ["Favourites.mp3", "Nils - Says.mp3"])

    def test_a_directory_symlink_loop_never_recurses_without_end(self):
        """The Windows-junction blind spot, reproduced here without Windows.

        A real NTFS junction is a reparse point tagged
        ``IO_REPARSE_TAG_MOUNT_POINT`` rather than ``IO_REPARSE_TAG_SYMLINK``,
        so ``os.path.islink()`` -- which is exactly what
        ``os.walk(followlinks=False)`` relies on to decline recursion -- does
        not recognise one as a link at all. A junction that loops back on a
        folder already walked would otherwise recurse until the process falls
        over. Patching ``islink`` to always say "not a link" reproduces that
        blind spot on this platform, so the containment/visited guard
        ``iter_audio_files`` now applies to *directories* (not just files) is
        what actually has to hold here, the same way it would have to on
        Windows.
        """
        write_track(self.music, "Nils - Real.mp3")
        loop = self.music / "Loop"
        try:
            loop.symlink_to(self.music, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("this platform will not create symlinks")

        real_islink = os.path.islink
        os.path.islink = lambda p: False
        try:
            found = sorted(os.path.basename(p) for p in local_library.iter_audio_files(str(self.music)))
        finally:
            os.path.islink = real_islink

        self.assertEqual(found, ["Nils - Real.mp3"])

    def test_oversized_files_are_skipped_not_imported(self):
        write_track(self.music, "Nils - Says.wav")
        original = local_library.LOCAL_SCAN_MAX_FILE_BYTES
        local_library.LOCAL_SCAN_MAX_FILE_BYTES = 8
        try:
            result = self.register_and_scan()
        finally:
            local_library.LOCAL_SCAN_MAX_FILE_BYTES = original
        self.assertEqual(result["scanned"], 1)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(self.state.list_music_tracks(), [])

    def test_a_locked_file_is_skipped_without_aborting_the_scan(self):
        """A file another process holds open -- a player, an indexer,
        antivirus -- is extremely common on Windows and raises
        ``PermissionError`` (a subclass of ``OSError``) the moment anything
        tries to stat or open it. One such file must cost a skipped row, never
        the rest of the folder.
        """
        write_track(self.music, "Nils - Says.mp3")
        write_track(self.music, "Nils - Locked.mp3")

        real_stat = local_library.os.stat

        def flaky_stat(path, *args, **kwargs):
            if os.path.basename(str(path)) == "Nils - Locked.mp3":
                raise PermissionError(13, "Permission denied")
            return real_stat(path, *args, **kwargs)

        local_library.os.stat = flaky_stat
        try:
            result = self.register_and_scan()
        finally:
            local_library.os.stat = real_stat

        self.assertEqual(result["scanned"], 2)
        self.assertEqual(result["added"], 1)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(
            [os.path.basename(row["file_path"]) for row in self.state.list_music_tracks()],
            ["Nils - Says.mp3"],
        )

    def test_a_locked_files_tags_fall_back_to_its_filename(self):
        """The same locked-file story, one layer in: mutagen's own file open
        can raise ``PermissionError`` even when ``os.stat`` on the same path
        succeeded a moment earlier (the lock is taken and released by another
        process in between). ``read_tags`` must absorb that too, the same way
        it already absorbs a corrupt tag block.
        """
        if not local_library.MUTAGEN_AVAILABLE:
            self.skipTest("mutagen is not installed in this environment")
        write_track(self.music, "Nils - Locked.mp3")

        real_mutagen_file = local_library._MutagenFile

        def flaky_open(path, *args, **kwargs):
            raise PermissionError(13, "Permission denied")

        local_library._MutagenFile = flaky_open
        try:
            result = self.register_and_scan()
        finally:
            local_library._MutagenFile = real_mutagen_file

        self.assertEqual(result["added"], 1)
        rows = self.state.list_music_tracks()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "Locked")  # parse_filename fallback

    # ── Identity ────────────────────────────────────────────────────────────

    def test_source_id_is_opaque_and_survives_a_retag(self):
        """Neither the path nor `name|size|mtime` may be the identity.

        A retag rewrites the tags and the modification time. If the id were
        derived from either, this second scan would create a *second* row and
        the playlist below would be pointing at a track nobody can see.
        """
        path = write_track(self.music, "01 - Nils - Says.mp3")
        self.register_and_scan()
        before = self.state.list_music_tracks()[0]
        playlist = self.state.create_playlist("Evening")
        self.state.add_track_to_playlist(playlist["id"], before["id"])

        # Retag: different bytes, different size, different mtime, same file.
        path.write_bytes(TRACK_BYTES + b"NEW TAGS AND THEN SOME")
        os.utime(path, (1_800_000_000.0, 1_800_000_000.0))
        again = local_library.scan(self.state, [str(self.music)])

        after = self.state.list_music_tracks()
        self.assertEqual(len(after), 1)
        self.assertEqual(again["added"], 0)
        self.assertEqual(again["updated"], 1)
        self.assertEqual(after[0]["id"], before["id"])
        self.assertEqual(after[0]["source_id"], before["source_id"])
        self.assertNotIn("-", after[0]["source_id"])  # a bare uuid4 hex, not a path
        self.assertNotIn(str(self.music), after[0]["source_id"])
        self.assertEqual([t["id"] for t in self.state.list_playlist_tracks(playlist["id"])],
                         [before["id"]])

    def test_a_moved_file_repairs_in_place_and_keeps_its_playlist(self):
        source = write_track(self.music / "Inbox", "01 - Nils - Says.mp3")
        self.register_and_scan()
        before = self.state.list_music_tracks()[0]
        playlist = self.state.create_playlist("Evening")
        self.state.add_track_to_playlist(playlist["id"], before["id"])

        # Reorganise: same file, new folder. mtime and size are preserved by a
        # move, which is exactly what the repair matches on.
        destination = self.music / "Albums" / "Spaces"
        destination.mkdir(parents=True)
        os.replace(source, destination / "01 - Nils - Says.mp3")

        # One pass, not two: the walk finishes before anything is reconciled,
        # so the row marked for the old path is available to be repaired by the
        # new one in the same scan.
        first = local_library.scan(self.state, [str(self.music)])
        self.assertEqual(first["moved"], 1)
        self.assertEqual(first["added"], 0, "a move must not create a second row")
        self.assertEqual(first["missing"], 0, "the file is still under the root, just moved")

        after = self.state.list_music_tracks()
        self.assertEqual(len(after), 1)
        self.assertEqual(after[0]["id"], before["id"])
        self.assertEqual(after[0]["source_id"], before["source_id"])
        # Stored resolved, because that is the path containment was checked
        # against and the one that actually opens.
        self.assertEqual(after[0]["file_path"],
                         os.path.realpath(str(destination / "01 - Nils - Says.mp3")))
        self.assertEqual(after[0]["missing_since"], "")
        self.assertEqual([t["id"] for t in self.state.list_playlist_tracks(playlist["id"])],
                         [before["id"]])

    def test_move_repair_only_claims_rows_a_scan_gave_up_on(self):
        """A copy must not steal the original's row.

        Two identical files that are both present are two tracks; only a row
        already marked missing is a candidate for repair.
        """
        write_track(self.music, "Nils - Says.mp3")
        self.register_and_scan()
        original = self.state.list_music_tracks()[0]

        write_track(self.music / "Backup", "Nils - Says.mp3")
        local_library.scan(self.state, [str(self.music)])

        rows = self.state.list_music_tracks()
        self.assertEqual(len(rows), 2)
        self.assertEqual(len({r["source_id"] for r in rows}), 2)
        self.assertIn(original["id"], {r["id"] for r in rows})

    # ── Absence ─────────────────────────────────────────────────────────────

    def test_a_deleted_file_is_marked_missing_and_never_removed(self):
        write_track(self.music, "Nils - Says.mp3")
        write_track(self.music, "Nils - Coda.mp3")
        self.register_and_scan()
        playlist = self.state.create_playlist("Evening")
        for track in self.state.list_music_tracks():
            self.state.add_track_to_playlist(playlist["id"], track["id"])

        (self.music / "Nils - Says.mp3").unlink()
        result = local_library.scan(self.state, [str(self.music)])

        self.assertEqual(result["missing"], 1)
        rows = {t["title"]: t for t in self.state.list_music_tracks()}
        self.assertEqual(len(rows), 2, "a scanner marks; it must never delete")
        self.assertTrue(rows["Says"]["missing_since"])
        self.assertEqual(rows["Coda"]["missing_since"], "")
        self.assertEqual(len(self.state.list_playlist_tracks(playlist["id"])), 2)

    def test_an_unavailable_root_marks_nothing_at_all(self):
        """The unplugged external drive.

        Marking the whole library missing because a volume is not mounted is,
        from the point of view of anyone reading a playlist, the same damage as
        deleting it.
        """
        write_track(self.music, "Nils - Says.mp3")
        self.register_and_scan()

        gone = self.root / "NotMounted"
        self.state.add_local_root(str(gone))
        result = local_library.scan(self.state, [str(gone)])

        self.assertTrue(result["scanned_roots"][0]["error"])
        self.assertEqual(result["missing"], 0)
        self.assertEqual(self.state.list_music_tracks()[0]["missing_since"], "")

    def test_a_returning_file_clears_its_missing_mark(self):
        path = write_track(self.music, "Nils - Says.mp3")
        self.register_and_scan()
        body = path.read_bytes()
        path.unlink()
        local_library.scan(self.state, [str(self.music)])
        self.assertTrue(self.state.list_music_tracks()[0]["missing_since"])

        write_track(self.music, "Nils - Says.mp3", body=body)
        local_library.scan(self.state, [str(self.music)])

        rows = self.state.list_music_tracks()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["missing_since"], "")

    def test_forgetting_a_root_keeps_its_tracks(self):
        write_track(self.music, "Nils - Says.mp3")
        self.register_and_scan()

        self.assertTrue(self.state.remove_local_root(str(self.music)))

        self.assertEqual(self.state.list_local_roots(), [])
        self.assertEqual(len(self.state.list_music_tracks()), 1)

    # ── Reporting ───────────────────────────────────────────────────────────

    def test_scan_only_walks_registered_roots(self):
        """A phone may re-run a scan; it may never say where.

        Folder choice happens at the computer. An unregistered path arriving
        here is not an instruction, it is something to drop.
        """
        write_track(self.music, "Nils - Says.mp3")
        elsewhere = self.root / "private"
        write_track(elsewhere, "Secret - Thing.mp3")
        self.state.add_local_root(str(self.music))

        result = local_library.scan(self.state, [str(elsewhere)])

        self.assertEqual(result["scanned"], 0)
        self.assertEqual(result["ignored"], [str(elsewhere)])
        self.assertEqual(self.state.list_music_tracks(), [])

    def test_progress_is_reported_and_the_root_record_is_stamped(self):
        for index in range(3):
            write_track(self.music, f"Nils - Track {index}.mp3")
        seen = []
        self.state.add_local_root(str(self.music))

        local_library.scan(self.state, [str(self.music)], on_progress=seen.append)

        self.assertTrue(seen)
        self.assertEqual(seen[-1]["scanned"], 3)
        root = self.state.list_local_roots()[0]
        self.assertTrue(root["last_scan_at"])
        self.assertEqual(root["last_error"], "")
        self.assertEqual(root["track_count"], 3)

    def test_an_unchanged_file_is_not_re_read_on_a_rescan(self):
        """Rescanning is the common operation, not scanning.

        A folder is scanned once and rescanned forever, so re-reading every tag
        block to arrive back at the state we started in is most of the cost of
        the feature for none of its value.
        """
        write_track(self.music, "Nils - Says.mp3")
        write_track(self.music, "Nils - Coda.mp3")
        self.register_and_scan()

        reads = []
        real = local_library.read_tags
        local_library.read_tags = lambda path: (reads.append(path), {})[1]
        try:
            again = local_library.scan(self.state, [str(self.music)])
            self.assertEqual(again["unchanged"], 2)
            self.assertEqual(reads, [], "an unchanged file must not be re-read")

            # Touching one file re-reads that one and no others.
            os.utime(self.music / "Nils - Says.mp3", (1_900_000_000.0, 1_900_000_000.0))
            third = local_library.scan(self.state, [str(self.music)])
        finally:
            local_library.read_tags = real

        self.assertEqual(third["unchanged"], 1)
        self.assertEqual(third["updated"], 1)
        self.assertEqual(third["added"], 0)
        self.assertEqual([os.path.basename(p) for p in reads], ["Nils - Says.mp3"])

    def test_a_returning_file_is_never_reported_unchanged(self):
        """It has to travel through the upsert, or its mark is never cleared."""
        path = write_track(self.music, "Nils - Says.mp3")
        self.register_and_scan()
        body = path.read_bytes()
        stamp = path.stat().st_mtime
        path.unlink()
        local_library.scan(self.state, [str(self.music)])

        write_track(self.music, "Nils - Says.mp3", body=body, mtime=stamp)
        back = local_library.scan(self.state, [str(self.music)])

        self.assertEqual(back["unchanged"], 0)
        self.assertEqual(self.state.list_music_tracks()[0]["missing_since"], "")

    def test_status_counts_tracks_bytes_and_absences(self):
        write_track(self.music, "Nils - Says.mp3")
        write_track(self.music, "Nils - Coda.mp3")
        self.register_and_scan()
        (self.music / "Nils - Says.mp3").unlink()
        local_library.scan(self.state, [str(self.music)])

        status = local_library.status(self.state)

        self.assertEqual(status["tracks"], 2)
        self.assertEqual(status["missing"], 1)
        self.assertEqual(status["bytes"], len(TRACK_BYTES) * 2)
        self.assertEqual([r["path"] for r in status["roots"]], [str(self.music)])


class WindowsPathCaseInsensitivityTests(unittest.TestCase):
    """Windows (and macOS's usual default) filesystems fold case; POSIX does
    not. These pin the ``os.path.normcase`` contract that the containment
    check, the rescan bookkeeping, and move-repair all now depend on, by
    patching ``normcase`` to fold case the way Windows does rather than
    relying on this machine's own volume happening to do the same -- so the
    behaviour is verified here even though nothing in this repo runs on
    Windows.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.music = self.root / "Music"
        self.state = MusicState(self.root / "music.db")

    def tearDown(self):
        self.tmp.cleanup()

    def register_and_scan(self):
        self.state.add_local_root(str(self.music))
        return local_library.scan(self.state, [str(self.music)])

    def test_within_is_case_sensitive_on_this_platform_by_default(self):
        """The floor: POSIX must keep telling `Song.mp3` and `song.mp3` apart."""
        self.assertTrue(local_library._within("/Music/Library", "/Music/Library/Song.mp3"))
        self.assertFalse(local_library._within("/Music/Library", "/MUSIC/LIBRARY/Song.mp3"))

    def test_within_folds_case_the_way_windows_does(self):
        original_normcase = os.path.normcase
        os.path.normcase = str.lower
        try:
            self.assertTrue(local_library._within("/Music/Library", "/MUSIC/LIBRARY/Song.mp3"))
            self.assertFalse(local_library._within("/Music/Library", "/MUSIC/Other/Song.mp3"))
        finally:
            os.path.normcase = original_normcase

    def test_an_unchanged_file_is_recognised_even_if_its_path_comes_back_recased(self):
        """The scan-to-scan case round-trip this is defence in depth for: a
        mapped drive, a reparse point, or simply Windows' own realpath can
        hand back a path cased differently than the one already stored for
        the very same file.
        """
        path = write_track(self.music, "Nils - Says.mp3")
        self.register_and_scan()
        stored = self.state.list_music_tracks()[0]["file_path"]
        root = os.path.dirname(stored)
        recased = stored.upper()
        stat = path.stat()

        original_normcase = os.path.normcase
        state.os.path.normcase = str.lower
        try:
            unchanged = self.state.unchanged_local_paths(
                root, {recased: (int(stat.st_size), float(stat.st_mtime))}
            )
        finally:
            state.os.path.normcase = original_normcase

        self.assertEqual(unchanged, {recased})

    def test_a_present_file_is_not_marked_missing_over_a_case_difference(self):
        write_track(self.music, "Nils - Says.mp3")
        self.register_and_scan()
        stored = self.state.list_music_tracks()[0]["file_path"]
        root = os.path.dirname(stored)

        original_normcase = os.path.normcase
        state.os.path.normcase = str.lower
        try:
            marked = self.state.mark_local_tracks_missing(root, {stored.upper()})
        finally:
            state.os.path.normcase = original_normcase

        self.assertEqual(marked, 0)
        self.assertEqual(self.state.list_music_tracks()[0]["missing_since"], "")

    def test_move_repair_matches_a_basename_that_only_changed_case(self):
        """A folder reorganisation that renames ``song.mp3`` to ``Song.mp3``
        while its size and mtime survive the move must repair the existing
        row in place on Windows -- not create a second one and leave the
        first dangling as missing forever. On POSIX the two basenames really
        are different files, so ``os.path.normcase`` is patched to fold case
        the way Windows does, the same as the other tests in this class.
        """
        state_obj = MusicState(self.root / "case-move.db")
        original_path = str(self.music / "Album" / "song.mp3")
        original = state_obj.upsert_local_track(
            file_path=original_path, file_size=1000, file_mtime=123456.0,
            title="Says", artist="Nils",
        )
        state_obj.mark_track_missing(original["id"])

        moved_path = str(self.music / "Elsewhere" / "Song.mp3")
        original_normcase = os.path.normcase
        state.os.path.normcase = str.lower
        try:
            result = state_obj.upsert_local_track(
                file_path=moved_path, file_size=1000, file_mtime=123456.0,
                title="Says", artist="Nils",
            )
        finally:
            state.os.path.normcase = original_normcase

        self.assertEqual(result["local_action"], "moved")
        self.assertEqual(result["id"], original["id"])
        rows = state_obj.list_music_tracks()
        self.assertEqual(len(rows), 1, "a case-only rename must not create a second row")
        self.assertEqual(rows[0]["file_path"], moved_path)
        self.assertEqual(rows[0]["missing_since"], "")


class WindowsRootSeparatorTests(unittest.TestCase):
    """A root recorded with the "wrong" trailing separator must still work.

    ``os.sep`` is ``"\\"`` on Windows, so a root that ends in a forward slash
    (typed by a user, or handed back by a web-based folder picker that never
    thinks in native separators) or a doubled-up separator (a drive root, once
    normcase folds a stray ``/`` onto the ``\\`` already there) must still
    collapse to exactly one trailing separator before it is used as a
    containment prefix. Getting the order wrong -- stripping the separator
    before folding case, instead of after -- leaves a duplicate that no stored
    path can ever start with, which silently breaks the rescan optimisation
    (``unchanged_local_paths``) and, worse, absence detection
    (``mark_local_tracks_missing``) for that entire root. Reproduced here with
    ``os.sep`` and ``os.path.normcase`` patched to Windows' own values, the
    same technique as ``WindowsPathCaseInsensitivityTests`` above.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.music = self.root / "Music"
        self.state = MusicState(self.root / "music.db")
        self.original_sep = os.sep
        self.original_normcase = os.path.normcase
        os.sep = "\\"
        # A stand-in for ntpath.normcase: fold case *and* fold "/" onto "\\",
        # exactly what makes the ordering of rstrip vs. normcase matter.
        state.os.path.normcase = lambda s: str(s).replace("/", "\\").lower()

    def tearDown(self):
        os.sep = self.original_sep
        state.os.path.normcase = self.original_normcase
        self.tmp.cleanup()

    def test_within_tolerates_a_root_given_with_a_trailing_forward_slash(self):
        """A drive root has to keep its trailing separator (``C:\\`` means
        something different from ``C:``), and a user or a web-based folder
        picker may hand one back with the "wrong" slash regardless. Built from
        a real temp-directory root rather than a literal ``C:\\...`` path, so
        ``os.path.realpath`` -- which ``_within`` calls first and which is not
        patched here -- still resolves something real on this platform.
        """
        write_track(self.music, "Nils - Says.mp3")
        real_root = os.path.realpath(str(self.music))
        candidate = os.path.join(real_root, "Nils - Says.mp3")
        self.assertTrue(local_library._within(real_root + "/", candidate))

    def test_unchanged_local_paths_matches_under_a_forward_slash_root(self):
        self.state.upsert_local_track(
            file_path="C:/Music/Song.mp3", file_size=10, file_mtime=1.0, title="Song",
        )
        unchanged = self.state.unchanged_local_paths(
            "C:/Music/", {"C:/Music/Song.mp3": (10, 1.0)}
        )
        self.assertEqual(unchanged, {"C:/Music/Song.mp3"})

    def test_mark_local_tracks_missing_finds_rows_under_a_forward_slash_root(self):
        row = self.state.upsert_local_track(
            file_path="C:/Music/Song.mp3", file_size=10, file_mtime=1.0, title="Song",
        )
        marked = self.state.mark_local_tracks_missing("C:/Music/", set())
        self.assertEqual(marked, 1)
        refreshed = self.state.get_local_track(track_id=row["id"])
        self.assertNotEqual(refreshed["missing_since"], "")


class LongPathTests(unittest.TestCase):
    r"""Windows refuses any file operation on an absolute path once it is
    roughly 260 characters long, which a music library nested a few
    artist/album/disc folders deep hits routinely. ``long_path()`` is the
    ``\\?\``-prefixed escape hatch for that -- exercised here with ``os.name``
    and ``os.path.isabs`` patched to behave like Windows, since neither
    changes just because ``os.name`` is reassigned on a POSIX interpreter.
    """

    def setUp(self):
        self.original_name = os.name
        self.original_isabs = os.path.isabs

    def tearDown(self):
        local_library.os.name = self.original_name
        local_library.os.path.isabs = self.original_isabs

    def _pretend_windows(self):
        local_library.os.name = "nt"
        local_library.os.path.isabs = ntpath.isabs

    def test_is_a_no_op_off_windows(self):
        self.assertEqual(local_library.os.name, self.original_name)
        self.assertEqual(local_library.long_path("/Music/Song.mp3"), "/Music/Song.mp3")

    def test_prefixes_an_absolute_drive_path_on_windows(self):
        self._pretend_windows()
        self.assertEqual(
            local_library.long_path(r"C:\Music\Song.mp3"),
            r"\\?\C:\Music\Song.mp3",
        )

    def test_prefixes_a_unc_share_with_its_own_form(self):
        self._pretend_windows()
        self.assertEqual(
            local_library.long_path(r"\\server\share\Song.mp3"),
            r"\\?\UNC\server\share\Song.mp3",
        )

    def test_an_already_prefixed_path_is_left_alone(self):
        self._pretend_windows()
        given = r"\\?\C:\Music\Song.mp3"
        self.assertEqual(local_library.long_path(given), given)

    def test_a_relative_path_is_left_alone(self):
        r"""``\\?\`` is only valid in front of a fully-qualified path; a
        relative path is passed straight through rather than mangled into one
        that resolves nowhere.
        """
        self._pretend_windows()
        self.assertEqual(local_library.long_path("Song.mp3"), "Song.mp3")

    def test_describe_file_stats_through_the_long_path_form(self):
        """The wiring, not just the helper: on Windows ``describe_file`` must
        actually hand ``os.stat`` the prefixed path, not the original one.
        """
        self._pretend_windows()
        seen = {}
        real_stat_result = os.stat_result((0o100644, 1, 2, 1, 0, 0, 123, 0, 0, 0))

        def fake_stat(path, *args, **kwargs):
            seen["path"] = path
            return real_stat_result

        original_stat = local_library.os.stat
        local_library.os.stat = fake_stat
        try:
            local_library.describe_file(r"C:\Music\Song.mp3")
        finally:
            local_library.os.stat = original_stat

        self.assertEqual(seen["path"], r"\\?\C:\Music\Song.mp3")


class LocalBridgeCommandTests(unittest.TestCase):
    """The command surface, with `shared` wired to a scratch database."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.music = self.root / "Music"
        self.state = MusicState(self.root / "music.db")
        self.sent = []
        self.old_state, self.old_notify = shared.STATE, shared._notify
        shared.STATE = self.state
        shared._notify = self.sent.append

    def tearDown(self):
        shared.STATE, shared._notify = self.old_state, self.old_notify
        self.tmp.cleanup()

    def last(self, message_type):
        return next(m for m in reversed(self.sent) if m.get("type") == message_type)

    def test_a_phone_may_list_roots_but_never_name_one(self):
        music_bridge.cmd_music_local_roots(
            {"id": "1", "action": "add", "path": "/", "origin_device_id": "phone-1"}
        )
        refused = self.last("music_local_roots_result")

        self.assertFalse(refused["ok"])
        self.assertEqual(self.state.list_local_roots(), [])

        # The same command from the computer itself carries no origin device.
        music_bridge.cmd_music_local_roots({"id": "2", "action": "add", "path": str(self.music)})
        allowed = self.last("music_local_roots_result")

        self.assertTrue(allowed["ok"])
        self.assertEqual([r["path"] for r in allowed["roots"]], [str(self.music)])

        music_bridge.cmd_music_local_roots(
            {"id": "3", "action": "remove", "path": str(self.music), "origin_device_id": "phone-1"}
        )
        self.assertFalse(self.last("music_local_roots_result")["ok"])
        self.assertEqual(len(self.state.list_local_roots()), 1)

    def test_a_local_track_resolves_without_yt_dlp(self):
        """A file on disk needs no extractor and does not expire."""
        write_track(self.music, "01 - Nils - Says.mp3")
        self.state.add_local_root(str(self.music))
        local_library.scan(self.state, [str(self.music)])
        track = self.state.list_music_tracks()[0]

        available = music_bridge.YTDLP_AVAILABLE
        music_bridge.YTDLP_AVAILABLE = False
        try:
            music_bridge.cmd_music_stream_url({"id": "9", "track_id": track["id"],
                                               "source_id": track["source_id"]})
        finally:
            music_bridge.YTDLP_AVAILABLE = available

        result = self.last("music_stream_url_result")
        self.assertTrue(result["ok"])
        self.assertTrue(result["local"])
        self.assertEqual(result["track_id"], track["id"])
        self.assertEqual(result["content_type"], "audio/mpeg")
        self.assertNotIn("expires_hint_s", result)

    def test_a_scan_command_reports_progress_and_a_result(self):
        write_track(self.music, "Nils - Says.mp3")
        self.state.add_local_root(str(self.music))

        music_bridge._local_scan_worker("7", [str(self.music)])

        self.assertTrue(any(m["type"] == "music_local_scan_progress" for m in self.sent))
        result = self.last("music_local_scan_result")
        self.assertTrue(result["ok"])
        self.assertEqual(result["added"], 1)
        self.assertEqual(result["tracks"], 1)
        # Both lists survive the merge: what was walked, and what is registered.
        self.assertEqual(len(result["scanned_roots"]), 1)
        self.assertEqual([r["path"] for r in result["roots"]], [str(self.music)])

    def test_a_second_scan_is_refused_while_one_is_running(self):
        self.state.add_local_root(str(self.music))
        music_bridge._local_scan_lock.acquire()
        try:
            music_bridge._local_scan_worker("8", [str(self.music)])
        finally:
            music_bridge._local_scan_lock.release()

        refused = self.last("music_local_scan_result")
        self.assertFalse(refused["ok"])
        self.assertTrue(refused["busy"])

    def test_unplayable_codecs_are_marked_for_the_device_that_reported_them(self):
        write_track(self.music, "Nils - Says.opus")
        write_track(self.music, "Nils - Coda.mp3")
        self.state.add_local_root(str(self.music))
        local_library.scan(self.state, [str(self.music)])

        music_bridge.cmd_music_client_capabilities({
            "id": "4", "origin_device_id": "phone-1",
            "can_play": {"audio/ogg": False, "audio/mpeg": True, "audio/flac": True},
        })
        self.assertTrue(self.last("music_client_capabilities_result")["ok"])

        music_bridge.cmd_music_library_index({"id": "5", "origin_device_id": "phone-1"})
        index = self.last("music_library_index_result")

        marks = {t["title"]: t.get("playable_on_device") for t in index["tracks"]}
        self.assertIs(marks["Says"], False)
        self.assertIsNone(marks["Coda"])
        self.assertEqual(index["capabilities_device_id"], "phone-1")
        reason = next(t["unplayable_reason"] for t in index["tracks"] if t["title"] == "Says")
        self.assertEqual(reason, "This computer can play it; your phone can't.")

        # A device that has said nothing gets no marks rather than wrong ones.
        music_bridge.cmd_music_library_index({"id": "6", "origin_device_id": "phone-2"})
        quiet = self.last("music_library_index_result")
        self.assertEqual(quiet["capabilities_device_id"], "")
        self.assertTrue(all("playable_on_device" not in t for t in quiet["tracks"]))


class LocalCommandSurfaceTests(unittest.TestCase):
    """What a paired phone is and is not allowed to reach."""

    def test_the_local_commands_are_dispatchable_and_allowed_on_the_lan(self):
        for command in ("music_local_roots", "music_local_scan",
                        "music_local_status", "music_client_capabilities"):
            with self.subTest(command=command):
                self.assertIn(command, music_bridge.DISPATCH)
                self.assertIn(command, server.COMPANION_COMMAND_TYPES)

    def test_root_mutation_is_gated_on_a_field_the_server_overwrites(self):
        """The load-bearing link behind `cmd_music_local_roots`.

        That handler refuses `add` and `remove` when `origin_device_id` is
        present. The refusal is only sound because the gateway overwrites that
        field for this command — otherwise a phone would simply omit it and name
        any folder on the computer as a music root.
        """
        self.assertIn("music_local_roots", server._DEVICE_STAMPED_TYPES)
        self.assertIn("music_client_capabilities", server._DEVICE_STAMPED_TYPES)
        self.assertIn("music_library_index", server._DEVICE_STAMPED_TYPES)

    def test_a_scan_is_acknowledged_rather_than_waited_on(self):
        """A hundred thousand files outlive any request timeout."""
        self.assertIn("music_local_scan", server.COMPANION_ONE_WAY_COMMAND_TYPES)

    def test_local_library_events_reach_every_paired_device(self):
        """One computer, one library: a scan is news for all of them."""
        for event in ("music_local_roots_result", "music_local_scan_result",
                      "music_local_scan_progress", "music_local_status_result"):
            with self.subTest(event=event):
                self.assertIn(event, server.CompanionSyncBroker._SYNC_TYPES)
                self.assertNotIn(event, server.CompanionSyncBroker._SESSION_TYPES)


class LocalAudioRouteTests(unittest.IsolatedAsyncioTestCase):
    """`GET /audio/{grant}` for a grant that names a file rather than a URL."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.music = self.root / "Music"
        self.state = MusicState(self.root / "music.db")
        self.old_state = shared.STATE
        shared.STATE = self.state

        self.body = bytes(range(256)) * 40   # 10 240 bytes, every value present
        write_track(self.music, "01 - Nils - Says.mp3", body=self.body)
        self.state.add_local_root(str(self.music))
        local_library.scan(self.state, [str(self.music)])
        self.track = self.state.list_music_tracks()[0]

        self.registry = CompanionRegistry(now=lambda: 1_000)
        invitation = self.registry.create_invitation(ttl_s=60)
        request = self.registry.request_pairing(invitation["token"], "Pixel")
        self.device = self.registry.approve(request["request_id"])
        self.client = TestClient(TestServer(
            server.build_companion_app(self.registry, allowed_origins={ORIGIN})
        ))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        shared.STATE = self.old_state
        self.tmp.cleanup()

    def grant(self, track_id=None):
        return self.registry.create_relay_grant(
            self.device["device_id"], ttl_s=600, kind="local",
            track_id=track_id if track_id is not None else self.track["id"],
        )["token"]

    async def test_whole_file_is_served_with_its_content_type_and_cors(self):
        response = await self.client.get("/audio/" + self.grant(), headers={"Origin": ORIGIN})

        self.assertEqual(response.status, 200)
        self.assertEqual(await response.read(), self.body)
        self.assertEqual(response.headers["Content-Type"], "audio/mpeg")
        self.assertEqual(response.headers.get("Accept-Ranges"), "bytes")
        self.assertEqual(response.headers.get("Access-Control-Allow-Origin"), ORIGIN)

    async def test_range_requests_are_answered_with_206_and_content_range(self):
        """Seeking is the whole reason this route exists.

        `web.FileResponse` implements Range; the assertions here are that we
        actually let it, rather than re-deriving a solved problem badly.
        """
        response = await self.client.get(
            "/audio/" + self.grant(),
            headers={"Origin": ORIGIN, "Range": "bytes=100-199"},
        )

        self.assertEqual(response.status, 206)
        self.assertEqual(await response.read(), self.body[100:200])
        self.assertEqual(response.headers["Content-Range"], f"bytes 100-199/{len(self.body)}")
        self.assertEqual(response.headers["Content-Length"], "100")

    async def test_an_open_ended_range_runs_to_the_end_of_the_file(self):
        response = await self.client.get(
            "/audio/" + self.grant(),
            headers={"Origin": ORIGIN, "Range": f"bytes={len(self.body) - 10}-"},
        )

        self.assertEqual(response.status, 206)
        self.assertEqual(await response.read(), self.body[-10:])

    async def test_a_range_past_the_end_is_refused_with_416(self):
        response = await self.client.get(
            "/audio/" + self.grant(),
            headers={"Origin": ORIGIN, "Range": "bytes=999999-1000000"},
        )

        self.assertEqual(response.status, 416)

    async def test_a_deleted_file_answers_404_and_marks_the_row_missing(self):
        (self.music / "01 - Nils - Says.mp3").unlink()

        response = await self.client.get("/audio/" + self.grant(), headers={"Origin": ORIGIN})

        self.assertEqual(response.status, 404)
        self.assertTrue(self.state.get_track(self.track["id"])["missing_since"])
        self.assertIsNotNone(self.state.get_track(self.track["id"]), "404 must not delete the row")

    async def test_a_grant_naming_no_track_is_refused_before_anything_is_read(self):
        with self.assertRaises(ValueError):
            self.registry.create_relay_grant(self.device["device_id"], kind="local", track_id="")

        unknown = self.grant(track_id="trk_does_not_exist")
        response = await self.client.get("/audio/" + unknown, headers={"Origin": ORIGIN})
        self.assertEqual(response.status, 404)

    async def test_revoking_the_device_cuts_local_audio_too(self):
        token = self.grant()
        self.assertEqual(
            (await self.client.get("/audio/" + token, headers={"Origin": ORIGIN})).status, 200
        )

        self.registry.revoke(self.device["device_id"])

        response = await self.client.get("/audio/" + token, headers={"Origin": ORIGIN})
        self.assertEqual(response.status, 404)


if __name__ == "__main__":
    unittest.main()
