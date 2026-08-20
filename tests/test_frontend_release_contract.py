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

    def test_desktop_output_transfer_ack_waits_for_media_load(self):
        transfer = self.miniplayer[
            self.miniplayer.index("function beginOutputTransfer"):
            self.miniplayer.index("function streamFresh")
        ]
        self.assertIn("pendingOutputTransfer", transfer)
        self.assertIn("Desktop could not load the transfer in time", transfer)
        playing = self.miniplayer[self.miniplayer.index("on('playing'"):
                                  self.miniplayer.index("on('pause'")]
        self.assertIn("finishOutputTransfer(true)", playing)
        terminal = self.miniplayer[self.miniplayer.index("function _terminalLoadFailure"):
                                   self.miniplayer.index("function _currentMediaEvent")]
        self.assertIn("finishOutputTransfer(false", terminal)

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
        self.assertRegex(self.css, r"\.rw-music-shell\s*\{[^}]*grid-template-rows:\s*minmax\(0,\s*1fr\)\s+auto")
        self.assertRegex(self.css, r"\.rw-now-bar\s*\{[^}]*grid-area:\s*player[^}]*position:\s*relative")
        self.assertNotIn("--rw-docked-clearance", self.css)
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
        self.assertIn("music-pwa-web.vercel.app", mobile)
        self.assertIn("New pairing code", mobile)
        self.assertIn("Open the Rainette PWA", mobile)
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
        self.assertIn("pwa_config_get", mobile)
        self.assertIn("pwa_config_set", mobile)
        self.assertIn("tunnel_status", mobile)
        self.assertIn("tunnel_helper_download", mobile)
        self.assertIn("tunnel_start", mobile)
        self.assertIn("tunnel_stop", mobile)
        self.assertIn("Download cloudflared", mobile)
        self.assertIn("Generate HTTPS tunnel", mobile)
        # The generated address has to land in the field pairing actually reads,
        # so what the phone will be told is always visible on screen.
        self.assertIn("rwPublicUrl", mobile)
        # A loopback endpoint is the one failure the phone cannot explain for
        # itself, so the desktop panel has to name it.
        self.assertIn("endpoint_is_local", mobile)
        self.assertIn("pairing_url", mobile)
        # Pairing must stay desktop-approved: the panel never mints a credential
        # on its own, it only asks the native bridge for an invitation.
        self.assertNotIn("device_token", mobile)

    def test_phone_client_contract(self):
        """The phone client must explain a failed connection, not relay a TypeError.

        `fetch` collapses mixed-content blocking, DNS failure, refused
        connections and rejected CORS into one message — "Failed to fetch" or
        "Load failed" — so every one of those paths has to go through the
        client's own diagnosis instead of surfacing the browser's wording.
        """
        client = (ROOT / "pwa" / "app.js").read_text(encoding="utf-8")
        self.assertIn("describeTransportFailure", client)
        self.assertIn("unusableEndpointReason", client)
        self.assertIn("isLoopbackHost", client)
        # A phone that already holds a credential only needs the new address
        # when a Quick Tunnel hostname rotates; it must not re-ask for approval.
        self.assertIn("adoptEndpoint", client)
        # Every request has to go through the wrapper, or the raw TypeError
        # reaches the user again.
        self.assertNotIn("await fetch(", client.replace("return await fetch(url, options);", ""))

    def test_phone_client_cache_is_versioned_with_the_pairing_client(self):
        """A returning phone must not keep serving the previous app.js.

        The worker answers stale-while-revalidate, so a client change reaches an
        installed phone one load late unless the cache name changes with it.

        The name now carries a `-<digest>` suffix of the shell's contents, which
        is what makes the two impossible to disagree about; that the digest is
        the *right* one is checked in test_output_and_phone_sync.py. Here it is
        only allowed for, so this contract keeps testing what it is about -- the
        version moving forward.
        """
        worker = (ROOT / "pwa" / "sw.js").read_text(encoding="utf-8")
        self.assertRegex(worker, r"const CACHE = 'rainette-pwa-v(?:[3-9]|\d{2,})(?:-[0-9a-f]{8})?'")


if __name__ == "__main__":
    unittest.main()
