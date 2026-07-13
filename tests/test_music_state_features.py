import tempfile
import unittest
from pathlib import Path

from state import MusicState


class MusicStateFeatureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = MusicState(Path(self.tmp.name) / "music.db")

    def tearDown(self):
        self.tmp.cleanup()

    def add_track(self, title, artist="Rainette", album="Desk Sessions", duration=180):
        return self.state.upsert_track(
            source_id=title.lower().replace(" ", "-"),
            title=title,
            artist=artist,
            duration_s=duration,
            thumbnail_url="",
            metadata={"album_name": album, "album": {"name": album}},
        )

    def test_playlist_folders_and_metadata(self):
        folder = self.state.create_playlist_folder("Focus")
        playlist = self.state.create_playlist("Deep Work")

        updated = self.state.update_playlist_meta(playlist["id"], folder_id=folder["id"], pinned=True, position=3)
        self.assertEqual(updated["folder_id"], folder["id"])
        self.assertTrue(updated["pinned"])
        self.assertEqual(updated["position"], 3)

        playlists = self.state.list_playlists()
        self.assertEqual(playlists[0]["id"], playlist["id"])
        self.assertEqual(playlists[0]["kind"], "manual")

        second = self.state.create_playlist_folder("Later")
        moved = self.state.move_playlist_folder(second["id"], 0)
        self.assertEqual(moved["position"], 0)
        self.assertEqual(self.state.list_playlist_folders()[0]["id"], second["id"])

        self.assertTrue(self.state.delete_playlist_folder(folder["id"]))
        unfiled = self.state.list_playlists()[0]
        self.assertEqual(unfiled["folder_id"], "")

    def test_smart_playlist_rules_are_deterministic(self):
        wanted = self.add_track("Warm Static", artist="Rainette")
        self.add_track("Cold Static", artist="Other")
        self.state.log_play(wanted["id"])

        smart = self.state.create_smart_playlist(
            "Rainette only",
            {"match": "all", "rules": [{"field": "artist", "op": "contains", "value": "rain"}], "sort": "title", "limit": 20},
        )
        tracks = self.state.smart_playlist_tracks(smart["id"])

        self.assertEqual([t["title"] for t in tracks], ["Warm Static"])
        listed = [p for p in self.state.list_playlists() if p["id"] == smart["id"]][0]
        self.assertEqual(listed["kind"], "smart")
        self.assertEqual(listed["track_count"], 1)

    def test_queue_sessions_save_list_delete(self):
        track = self.add_track("Queue Track")
        last = self.state.save_queue_session(name="Last session", tracks=[track], index=0, is_last=True)
        manual = self.state.save_queue_session(name="Evening", tracks=[track], index=0, is_last=False)

        sessions = self.state.list_queue_sessions()
        self.assertEqual(sessions[0]["id"], last["id"])
        self.assertTrue(sessions[0]["is_last"])
        self.assertEqual(sessions[1]["name"], "Evening")
        self.assertEqual(sessions[1]["track_count"], 1)

        self.assertTrue(self.state.delete_queue_session(manual["id"]))
        self.assertEqual([s["id"] for s in self.state.list_queue_sessions()], [last["id"]])

    def test_listening_insights_aggregates_play_history(self):
        favourite = self.add_track("Repeat One", artist="Rainette", duration=120)
        other = self.add_track("Side Track", artist="Someone Else", duration=60)
        for _ in range(3):
            self.state.log_play(favourite["id"])
        self.state.log_play(other["id"])

        insights = self.state.listening_insights(days=7)

        self.assertEqual(insights["window_days"], 7)
        self.assertEqual(insights["total_plays"], 4)
        self.assertEqual(insights["total_minutes"], 7)   # 3*120s + 60s = 420s
        self.assertEqual(insights["unique_tracks"], 2)
        self.assertEqual(insights["unique_artists"], 2)
        self.assertEqual(insights["top_tracks"][0]["title"], "Repeat One")
        self.assertEqual(insights["top_tracks"][0]["play_count"], 3)
        self.assertEqual(insights["top_artists"][0]["name"], "Rainette")
        self.assertEqual(len(insights["daily"]), 7)
        # All plays happened "now", so today's bucket carries all of them.
        self.assertEqual(insights["daily"][-1]["count"], 4)
        self.assertEqual(sum(d["count"] for d in insights["daily"]), 4)

    def test_listening_insights_empty_history(self):
        insights = self.state.listening_insights(days=0)
        self.assertEqual(insights["total_plays"], 0)
        self.assertEqual(insights["top_tracks"], [])
        self.assertEqual(len(insights["daily"]), 30)   # all-time chart shows last 30 days

    def test_followed_artists_are_persistent_upserts(self):
        first = self.state.follow_artist(
            artist_id="UC_Rainette",
            name="Rainette",
            thumbnail_url="https://img/old.jpg",
        )
        followed_at = "2000-01-01T00:00:00+00:00"
        with self.state.connect() as conn:
            conn.execute(
                "UPDATE music_followed_artists SET followed_at = ? WHERE artist_key = ?",
                (followed_at, first["artist_key"]),
            )
        updated = self.state.follow_artist(
            artist_id="uc_rainette",
            name="Rainette Music",
            thumbnail_url="https://img/new.jpg",
        )

        self.assertEqual(updated["artist_key"], "id:uc_rainette")
        self.assertEqual(updated["followed_at"], followed_at)
        self.assertEqual(updated["name"], "Rainette Music")
        self.assertEqual(self.state.list_followed_artists(), [updated])
        self.assertTrue(self.state.unfollow_artist(artist_id="UC_RAINETTE", name=""))
        self.assertEqual(self.state.list_followed_artists(), [])

    def test_name_only_follow_uses_normalized_identity(self):
        first = self.state.follow_artist(artist_id="", name="  The   Artist  ", thumbnail_url="")
        second = self.state.follow_artist(artist_id="", name="the artist", thumbnail_url="cover.jpg")

        self.assertEqual(first["artist_key"], "name:the artist")
        self.assertEqual(second["artist_key"], first["artist_key"])
        self.assertEqual(len(self.state.list_followed_artists()), 1)

    def test_playlist_artwork_migration_and_metadata(self):
        playlist = self.state.create_playlist("Artwork")
        updated = self.state.update_playlist_artwork(playlist["id"], "pl_safe_abc.png")

        self.assertEqual(updated["artwork_key"], "pl_safe_abc.png")
        self.assertEqual(self.state.get_playlist(playlist["id"])["artwork_key"], "pl_safe_abc.png")
        cleared = self.state.update_playlist_artwork(playlist["id"], "")
        self.assertEqual(cleared["artwork_key"], "")
        with self.state.connect() as conn:
            playlist_columns = {row["name"] for row in conn.execute("PRAGMA table_info(music_playlists)")}
            followed_table = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='music_followed_artists'"
            ).fetchone()
        self.assertIn("artwork_key", playlist_columns)
        self.assertIsNotNone(followed_table)

    def test_library_index_includes_followed_artists(self):
        self.state.follow_artist(artist_id="channel-1", name="Followed", thumbnail_url="art.jpg")
        library = self.state.music_library_index()
        self.assertEqual(library["followed_artists"][0]["name"], "Followed")


if __name__ == "__main__":
    unittest.main()
