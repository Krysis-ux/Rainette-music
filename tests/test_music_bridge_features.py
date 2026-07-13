import unittest
import tempfile
from pathlib import Path

import music_bridge


class FakeState:
    def __init__(self):
        self.calls = []
        self.delete_result = True
        self.playlist_artwork_key = ""

    def list_playlists(self):
        return [{"id": "pl_1", "name": "Manual", "kind": "manual"}]

    def list_playlist_folders(self):
        return [{"id": "fld_1", "name": "Folder"}]

    def create_playlist_folder(self, name):
        self.calls.append(("create_folder", name))
        return {"id": "fld_new", "name": name}

    def rename_playlist_folder(self, folder_id, name):
        self.calls.append(("rename_folder", folder_id, name))
        return {"id": folder_id, "name": name}

    def delete_playlist_folder(self, folder_id):
        self.calls.append(("delete_folder", folder_id))
        return True

    def move_playlist_folder(self, folder_id, position):
        self.calls.append(("move_folder", folder_id, position))
        return {"id": folder_id, "position": position}

    def update_playlist_meta(self, playlist_id, **kwargs):
        self.calls.append(("update_meta", playlist_id, kwargs))
        return {"id": playlist_id, **kwargs}

    def create_smart_playlist(self, name, rules):
        self.calls.append(("create_smart", name, rules))
        return {"id": "pl_smart", "name": name, "kind": "smart", "rules": rules}

    def update_smart_playlist(self, playlist_id, **kwargs):
        self.calls.append(("update_smart", playlist_id, kwargs))
        return {"id": playlist_id, "kind": "smart", **kwargs}

    def delete_playlist(self, playlist_id):
        self.calls.append(("delete_playlist", playlist_id))
        return self.delete_result

    def get_playlist(self, playlist_id):
        return {"id": playlist_id, "artwork_key": self.playlist_artwork_key}

    def follow_artist(self, *, artist_id, name, thumbnail_url):
        self.calls.append(("follow_artist", artist_id, name, thumbnail_url))
        return {"artist_key": "id:" + artist_id.lower(), "artist_id": artist_id, "name": name, "thumbnail_url": thumbnail_url}

    def unfollow_artist(self, *, artist_id, name):
        self.calls.append(("unfollow_artist", artist_id, name))
        return True

    def list_followed_artists(self):
        return [{"artist_key": "id:artist-1", "artist_id": "artist-1", "name": "Artist One"}]

    def music_library_index(self, *, limit=500):
        self.calls.append(("library_index", limit))
        return {"tracks": [], "artists": [], "albums": [], "followed_artists": self.list_followed_artists()}

    def smart_playlist_tracks(self, playlist_id):
        self.calls.append(("smart_tracks", playlist_id))
        return [{"source": "youtube", "source_id": "abc", "title": "Track"}]

    def save_queue_session(self, **kwargs):
        self.calls.append(("save_session", kwargs))
        return {"id": kwargs.get("session_id") or "qs_1", "name": kwargs["name"], "tracks": kwargs["tracks"], "track_count": len(kwargs["tracks"])}

    def list_queue_sessions(self):
        return [{"id": "qs_1", "name": "Session", "tracks": [], "track_count": 0}]

    def delete_queue_session(self, session_id):
        self.calls.append(("delete_session", session_id))
        return True


class MusicBridgeFeatureTests(unittest.TestCase):
    def test_network_clients_use_the_windows_system_trust_store(self):
        self.assertTrue(music_bridge.SYSTEM_TRUST_ENABLED)

    def setUp(self):
        self.state = FakeState()
        self.messages = []
        self.old_state = music_bridge.shared.STATE
        self.old_notify = music_bridge.shared.notify_browsers
        music_bridge.shared.STATE = self.state
        music_bridge.shared.notify_browsers = self.messages.append

    def tearDown(self):
        music_bridge.shared.STATE = self.old_state
        music_bridge.shared.notify_browsers = self.old_notify

    def dispatch(self, type_, **payload):
        music_bridge.DISPATCH[type_]({"type": type_, "id": "req", **payload})
        return self.messages[-1]

    def test_playlist_folder_and_meta_commands(self):
        cases = [
            ("music_playlist_folder_create", {"name": "Focus"}, "music_playlist_folder_created"),
            ("music_playlist_folder_rename", {"folder_id": "fld_1", "name": "Renamed"}, "music_playlist_folder_renamed"),
            ("music_playlist_folder_delete", {"folder_id": "fld_1"}, "music_playlist_folder_deleted"),
            ("music_playlist_folder_move", {"folder_id": "fld_1", "position": 2}, "music_playlist_folder_moved"),
            ("music_playlist_update_meta", {"playlist_id": "pl_1", "folder_id": "fld_1", "pinned": True}, "music_playlist_meta_updated"),
        ]
        for command, payload, expected_type in cases:
            with self.subTest(command=command):
                msg = self.dispatch(command, **payload)
                self.assertEqual(msg["type"], expected_type)
                self.assertTrue(msg["ok"])
                self.assertIn("playlists", msg)
                self.assertIn("folders", msg)

    def test_smart_playlist_commands(self):
        rules = {"match": "all", "rules": [{"field": "artist", "op": "contains", "value": "rain"}]}
        create = self.dispatch("music_smart_playlist_create", name="Smart", rules=rules)
        update = self.dispatch("music_smart_playlist_update", playlist_id="pl_smart", name="Smart 2", rules=rules)
        tracks = self.dispatch("music_smart_playlist_tracks", playlist_id="pl_smart")
        delete = self.dispatch("music_smart_playlist_delete", playlist_id="pl_smart")

        self.assertEqual(create["type"], "music_smart_playlist_created")
        self.assertEqual(update["type"], "music_smart_playlist_updated")
        self.assertEqual(tracks["tracks"][0]["source_id"], "abc")
        self.assertEqual(delete["type"], "music_smart_playlist_deleted")

    def test_queue_session_commands(self):
        track = {"source": "youtube", "source_id": "abc", "title": "Track"}
        saved = self.dispatch("music_queue_session_save", name="Session", tracks=[track], index=0)
        listed = self.dispatch("music_queue_session_list")
        deleted = self.dispatch("music_queue_session_delete", session_id="qs_1")

        self.assertEqual(saved["type"], "music_queue_session_saved")
        self.assertEqual(saved["session"]["track_count"], 1)
        self.assertEqual(listed["type"], "music_queue_session_list_result")
        self.assertEqual(deleted["type"], "music_queue_session_deleted")

    def test_remote_relay_commands_are_pure_fanout(self):
        # cmd_music_remote_play / cmd_music_remote_control / cmd_music_request_state
        # are trusted to relay the inbound message verbatim to every connected
        # window (the browser and detached player windows reconcile playback
        # state between themselves) - regression guard against a "fix" that
        # accidentally starts filtering/renaming fields a client depends on.
        cases = [
            ("music_remote_play", {"tracks": [{"source_id": "abc"}], "index": 0}),
            ("music_remote_control", {"action": "queue_play_index", "index": 2}),
            ("music_request_state", {}),
            ("music_open_artist", {"artist_id": "artist-1", "name": "Artist One"}),
        ]
        for command, payload in cases:
            with self.subTest(command=command):
                msg = self.dispatch(command, **payload)
                self.assertEqual(msg["type"], command)
                for key, value in payload.items():
                    self.assertEqual(msg[key], value)

    def test_mix_command_broadcasts_deterministic_tracks(self):
        old_run_bg = music_bridge._run_bg
        old_mix = music_bridge._mix_from_seed
        try:
            music_bridge._run_bg = lambda target, *args: target(*args)
            music_bridge._mix_from_seed = lambda seed: ([{"source": "youtube", "source_id": "abc", "title": "Track"}], "Built from test")
            msg = self.dispatch("music_mix_from_seed", seed={"kind": "artist", "name": "Rainette"})
        finally:
            music_bridge._run_bg = old_run_bg
            music_bridge._mix_from_seed = old_mix

        self.assertEqual(msg["type"], "music_mix_from_seed_result")
        self.assertTrue(msg["ok"])
        self.assertEqual(msg["tracks"][0]["source_id"], "abc")

    def test_follow_artist_commands_broadcast_refreshed_list(self):
        followed = self.dispatch(
            "music_artist_follow",
            artist_id="artist-1",
            name="Artist One",
            thumbnail_url="cover.jpg",
        )
        listed = self.dispatch("music_followed_artists")
        unfollowed = self.dispatch("music_artist_unfollow", artist_id="artist-1", name="Artist One")

        self.assertEqual(followed["type"], "music_artist_followed")
        self.assertEqual(followed["artist"]["name"], "Artist One")
        self.assertEqual(followed["followed_artists"][0]["artist_id"], "artist-1")
        self.assertEqual(listed["type"], "music_followed_artists_result")
        self.assertEqual(unfollowed["type"], "music_artist_unfollowed")
        self.assertTrue(unfollowed["removed"])

    def test_library_index_keeps_followed_artists_field(self):
        msg = self.dispatch("music_library_index", limit=25)
        self.assertTrue(msg["ok"])
        self.assertEqual(msg["followed_artists"][0]["name"], "Artist One")

    def test_smart_playlist_delete_removes_managed_artwork(self):
        old_policy = music_bridge.shared.POLICY
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            key = "pl_smart_0123456789abcdef0123456789abcdef.png"
            (root / key).write_bytes(b"image")
            self.state.playlist_artwork_key = key
            music_bridge.shared.POLICY = {"playlist_artwork_dir": root}
            try:
                msg = self.dispatch("music_smart_playlist_delete", playlist_id="pl_smart")
            finally:
                music_bridge.shared.POLICY = old_policy
            self.assertTrue(msg["ok"])
            self.assertFalse((root / key).exists())

    def test_missing_playlist_delete_keeps_current_state_payload(self):
        self.state.delete_result = False
        msg = self.dispatch("music_playlist_delete", playlist_id="missing")
        self.assertFalse(msg["ok"])
        self.assertEqual(msg["playlists"][0]["id"], "pl_1")
        self.assertEqual(msg["folders"][0]["id"], "fld_1")


if __name__ == "__main__":
    unittest.main()
