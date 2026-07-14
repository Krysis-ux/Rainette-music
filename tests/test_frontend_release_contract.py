import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class FrontendReleaseContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.music = (ROOT / "web" / "rainette_music.js").read_text(encoding="utf-8")
        cls.settings = (ROOT / "web" / "rainette_settings.js").read_text(encoding="utf-8")
        cls.miniplayer = (ROOT / "web" / "miniplayer.js").read_text(encoding="utf-8")
        cls.css = (ROOT / "web" / "rainette_pages.css").read_text(encoding="utf-8")

    def test_navigation_uses_songs_following_and_recents(self):
        self.assertIn("songs: { label: 'Songs'", self.music)
        self.assertIn("following: { label: 'Following'", self.music)
        self.assertIn("recent: { label: 'Recents'", self.music)
        nav = re.search(r"function navItems\(\).*?const ids = \[(.*?)\];", self.music, re.S)
        self.assertIsNotNone(nav)
        self.assertIn("'songs'", nav.group(1))
        self.assertIn("'following'", nav.group(1))
        self.assertNotIn("'artists'", nav.group(1))
        self.assertNotIn("'albums'", nav.group(1))
        self.assertIn("['songs', 'Songs']", self.settings)
        self.assertIn("['following', 'Following']", self.settings)

    def test_search_has_recent_artists_and_result_filters_with_songs_first(self):
        self.assertIn("searchFilter: 'all'", self.music)
        self.assertIn("['all', 'All']", self.music)
        self.assertIn("['songs', 'Songs']", self.music)
        self.assertIn("['artists', 'Artists']", self.music)
        self.assertIn("['albums', 'Albums']", self.music)
        self.assertIn("Recent artists", self.music)
        songs = self.music.index("section('Songs')", self.music.index("function renderResults"))
        artists = self.music.index("section('Artists')", self.music.index("function renderResults"))
        self.assertLess(songs, artists)

    def test_every_track_menu_can_add_to_playlist_and_picker_can_create(self):
        menu = self.music[self.music.index("async function openTrackMenu"):self.music.index("function renderCurrent")]
        self.assertNotIn("includePlaylist", menu)
        self.assertIn("label: 'Add to playlist'", menu)
        picker = self.music[self.music.index("async function openAddToPlaylist"):self.music.index("function queueSummary")]
        self.assertIn("Create new playlist", picker)
        self.assertIn("music_playlist_create", picker)

    def test_playlist_artwork_upload_and_render_contract(self):
        self.assertIn("rainetteAuthHeaders", self.music)
        self.assertIn("accept = 'image/png,image/jpeg,image/webp'", self.music)
        self.assertIn("/playlist-artwork/", self.music)
        self.assertIn("pl.artwork_key", self.music)
        self.assertIn("Choose artwork", self.music)

    def test_recents_exposes_song_artist_and_album_modes(self):
        self.assertIn("recentMode: 'songs'", self.music)
        self.assertIn("function recentArtists", self.music)
        self.assertIn("function recentAlbums", self.music)

    def test_docked_player_has_live_volume_control(self):
        dock = self.music[self.music.index("function ensureDockedBar"):self.music.index("function renderDockedBar")]
        self.assertIn("type = 'range'", dock)
        self.assertIn("rw-now-volume", dock)
        self.assertIn("RainetteMusic?.setVolume", dock)

    def test_miniplayer_artist_opens_main_artist_profile(self):
        self.assertIn("music_open_artist", self.miniplayer)
        self.assertIn("main_reveal", self.miniplayer)
        self.assertIn("mp-artist-link", self.miniplayer)
        self.assertIn("case 'music_open_artist'", self.music)

    def test_shell_uses_branded_asset_instead_of_letter_mark(self):
        self.assertIn("rainette-icon-256.png", self.music)
        self.assertNotIn('class="rw-music-mark" aria-hidden="true">R<', self.music)
        logo = ROOT / "web" / "assets" / "rainette-icon-256.png"
        self.assertTrue(logo.is_file())
        self.assertEqual(logo.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")

    def test_broken_remote_thumbnails_fall_back_without_a_broken_image_icon(self):
        thumb = self.music[self.music.index("function thumbBox"):self.music.index("function iconBtn")]
        self.assertIn("addEventListener('error'", thumb)
        self.assertIn("img.remove()", thumb)

    def test_music_body_is_the_single_page_scroll_owner(self):
        self.assertRegex(self.css, r"#rwMusicBody\s*\{[^}]*overflow-y:\s*auto")
        self.assertIn("--rw-docked-clearance", self.css)
        self.assertRegex(self.css, r"\.rw-insights-scroll\s*\{[^}]*overflow:\s*visible")
        self.assertRegex(self.css, r"\.rw-playlist-groups\s*\{[^}]*overflow:\s*visible")

    def test_progress_has_no_width_transition(self):
        self.assertNotIn("transition: width 0.5s linear", self.css)

    def test_midnight_and_lyrics_polish_hooks_exist(self):
        self.assertIn(".rw-theme-midnight #rwMusicPage .rw-bubble", self.css)
        self.assertIn(".rw-now-view-lyrics.is-manual-scroll", self.css)
        self.assertRegex(self.css, r"\.rw-now-view-lyrics-line\s*\{[^}]*font-size:\s*(?:18|19|20)px")

    def test_mobile_page_contract(self):
        mobile = (ROOT / "web" / "rainette_mobile.js").read_text(encoding="utf-8")
        self.assertIn("rainette-music-android.apk", mobile)
        self.assertIn("New pairing code", mobile)
        self.assertIn("Download APK", mobile)
        self.assertIn("Approve", mobile)
        self.assertIn("Reject", mobile)
        self.assertIn("Revoke", mobile)
        self.assertIn("companion_create_invitation", mobile)
        self.assertIn("companion_management_state", mobile)
        self.assertIn("companion_approve_request", mobile)
        self.assertIn("companion_reject_request", mobile)
        self.assertIn("companion_revoke_device", mobile)
        self.assertIn("mountGeneration", mobile)
        self.assertIn("isCurrentMount", mobile)
        self.assertIn("Release status unavailable", mobile)
        self.assertIn("network or certificate error", mobile)
        self.assertIn("official GitHub link to retry", mobile)
        self.assertIn("releaseStatus === 'unavailable'", mobile)


if __name__ == "__main__":
    unittest.main()
