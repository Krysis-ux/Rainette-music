"""Importing a playlist from a pasted link or list, without the network."""

import json
from pathlib import Path

import pytest

import music_bridge
import playlist_links as pl
from state import MusicState


def fake_web(pages):
    """A `get` that answers from {url-prefix: body}, recording what was asked."""
    asked = []

    def get(url, data=None, headers=None):
        asked.append((url, data))
        for prefix, body in pages.items():
            if url.startswith(prefix):
                return url, body(data) if callable(body) else body
        raise OSError(f"offline: {url}")

    get.asked = asked
    return get


def spotify_embed(n, name="Mix", token="tok"):
    data = {"props": {"pageProps": {"state": {
        "data": {"entity": {"name": name, "trackList": [{"title": f"Song {i}", "subtitle": f"Artist {i}, Guest"} for i in range(n)]}},
        "settings": {"session": {"accessToken": token}},
    }}}}
    return f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script></html>'


def spotify_page(total):
    def answer(data):
        offset = data["variables"]["offset"]
        items = [{"itemV2": {"data": {"name": f"Song {i}", "artists": {"items": [{"profile": {"name": f"Artist {i}"}}]},
                                      "albumOfTrack": {"name": "LP"}}}} for i in range(offset, min(total, offset + 100))]
        return json.dumps({"data": {"playlistV2": {"content": {"totalCount": total, "items": items}}}})
    return answer


def test_spotify_reads_past_the_embeds_hundred():
    get = fake_web({"https://open.spotify.com/embed/playlist/": spotify_embed(100),
                    pl.SPOTIFY_PATHFINDER: spotify_page(250)})
    found = pl.read("look https://open.spotify.com/playlist/37i9dQZF1DWXRqgorJj26U?si=x", get=get)
    assert (found["source"], found["name"], found["total"], found["partial"]) == ("spotify", "Mix", 250, False)
    assert len(found["entries"]) == 250
    assert found["entries"][0] == {"title": "Song 0", "artist": "Artist 0", "album": ""}
    assert found["entries"][249]["album"] == "LP"


def test_spotify_keeps_the_first_hundred_when_paging_fails():
    get = fake_web({"https://open.spotify.com/embed/playlist/": spotify_embed(100)})
    found = pl.read("spotify:playlist:37i9dQZF1DWXRqgorJj26U", get=get)
    assert len(found["entries"]) == 100 and found["partial"] is True


def test_a_private_spotify_playlist_says_what_to_do():
    get = fake_web({"https://open.spotify.com/embed/": "<html>nothing</html>"})
    with pytest.raises(pl.LinkError, match="public"):
        pl.read("https://open.spotify.com/intl-fr/playlist/37i9dQZF1DWXRqgorJj26U", get=get)


def test_apple_music_reads_the_page_data():
    data = {"data": [{"data": {"sections": [
        {"itemKind": "containerDetailHeaderLockup", "items": [{"title": "Today's Hits", "trackCount": 3}]},
        {"itemKind": "trackLockup", "items": [
            {"title": "One", "artistName": "A", "tertiaryLinks": [{"title": "One - Single"}]},
            {"title": "Two", "artistName": "B"}]},
        {"itemKind": "squareLockup", "items": [{"title": "Not a song", "artistName": "X"}]},
    ]}}]}
    page = f'<script type="application/json" id="serialized-server-data">{json.dumps(data)}</script>'
    get = fake_web({"https://music.apple.com/us/playlist/pl.f4d1": page})
    found = pl.read("https://music.apple.com/us/playlist/todays-hits/pl.f4d1?ls", get=get)
    assert found["name"] == "Today's Hits" and found["total"] == 3 and found["partial"] is True
    assert found["entries"] == [{"title": "One", "artist": "A", "album": "One - Single"}, {"title": "Two", "artist": "B", "album": ""}]


def test_deezer_pages_through_its_api():
    rows = [{"title": f"T{i}", "artist": {"name": "Ar"}, "album": {"title": "Al"}} for i in range(150)]
    get = fake_web({
        "https://api.deezer.com/playlist/9/tracks?index=100": json.dumps({"data": rows[100:]}),
        "https://api.deezer.com/playlist/9": json.dumps({"title": "Dz", "nb_tracks": 150, "tracks": {"data": rows[:100]}}),
    })
    found = pl.read("https://www.deezer.com/en/playlist/9", get=get)
    assert (found["name"], found["total"], len(found["entries"])) == ("Dz", 150, 150)


def test_youtube_links_are_handed_back_by_id():
    found = pl.read("https://music.youtube.com/playlist?list=PLFgquLnL59alCl_2TQvOiD5Vgm1hCaGSI", get=fake_web({}))
    assert found["youtube_playlist_id"] == "PLFgquLnL59alCl_2TQvOiD5Vgm1hCaGSI" and found["entries"] == []


def test_share_short_links_are_followed_and_nothing_else_is_opened():
    get = fake_web({"https://spotify.link/": "<a href='https://open.spotify.com/playlist/37i9dQZF1DWXRqgorJj26U'>",
                    "https://open.spotify.com/embed/playlist/": spotify_embed(2)})
    assert len(pl.read("https://spotify.link/AbC123", get=get)["entries"]) == 2
    with pytest.raises(pl.LinkError):
        pl.read("http://169.254.169.254/latest/meta-data", get=get)
    assert all(not url.startswith("http://169") for url, _ in get.asked)


def test_pasted_lists_and_exportify_csv():
    csv_text = 'Track URI,"Track Name","Artist Name(s)","Album Name"\nx,"Nokia","Drake, Someone","Some, Album"\nx,"",A,B\n'
    assert pl.parse_text(csv_text) == [{"title": "Nokia", "artist": "Drake", "album": "Some, Album"}]
    assert pl.parse_text("1. Drake - Nokia\nJust A Title\n") == [
        {"title": "Nokia", "artist": "Drake", "album": ""}, {"title": "Just A Title", "artist": "", "album": ""}]
    assert not pl.looks_like_link("Drake - Nokia") and pl.looks_like_link("x https://deezer.com/playlist/1 y")
    assert pl.looks_like_link(" https://example.com/x")  # so it is told why, not searched for


def test_matching_prefers_the_right_song_and_refuses_a_different_one():
    wanted = {"title": "Her Majesty - Remastered 2009", "artist": "The Beatles"}
    candidates = [{"source_id": "a", "title": "Her Majesty", "artist": "Cover Band"},
                  {"source_id": "b", "title": "Her Majesty", "artist": "The Beatles"},
                  {"source_id": "c", "title": "Something", "artist": "The Beatles"}]
    assert pl.best_match(candidates, wanted)["source_id"] == "b"
    assert pl.best_match(candidates[2:], wanted) is None


def test_the_engine_builds_the_playlist_in_order(tmp_path, monkeypatch):
    messages = []
    monkeypatch.setattr(music_bridge.shared, "STATE", MusicState(Path(tmp_path) / "music.db"))
    monkeypatch.setattr(music_bridge.shared, "notify_browsers", messages.append)
    monkeypatch.setattr(music_bridge, "_run_bg", lambda target, *args: target(*args))
    songs = {"One": {"source": "youtube", "source_id": "v1", "title": "One", "artist": "A"},
             "Three": {"source": "youtube", "source_id": "v3", "title": "Three", "artist": "C"}}
    monkeypatch.setattr(music_bridge, "_find_song", lambda entry: songs.get(entry["title"]))

    music_bridge.DISPATCH["music_playlist_import"]({"id": "r", "text": "A - One\nB - Two\nC - Three", "name": "Mine"})

    progress = [m for m in messages if m["type"] == "music_playlist_import_progress"]
    result = messages[-1]
    assert [m["done"] for m in progress] == [1, 2, 3] and all("id" not in m for m in progress)
    assert result["type"] == "music_playlist_import_result" and result["ok"] is True
    assert (result["added"], result["missing"], result["playlist"]["name"]) == (2, [{"title": "Two", "artist": "B"}], "Mine")
    tracks = music_bridge.shared.STATE.list_playlist_tracks(result["playlist"]["id"])
    assert [t["source_id"] for t in tracks] == ["v1", "v3"]


def test_the_engine_reports_a_page_without_a_playlist_plainly(monkeypatch):
    messages = []
    monkeypatch.setattr(music_bridge.shared, "notify_browsers", messages.append)
    monkeypatch.setattr(music_bridge, "_run_bg", lambda target, *args: target(*args))
    monkeypatch.setattr(pl, "fetch", fake_web({"https://example.com/": "<html>no playlist here</html>"}))
    music_bridge.DISPATCH["music_playlist_link"]({"id": "r", "url": "https://example.com/x"})
    assert messages[-1]["ok"] is False and messages[-1]["msg"].startswith("Rainette can't read a playlist from that page")


def test_soundcloud_fills_in_the_tracks_its_page_leaves_out():
    hydration = [{"hydratable": "playlist", "data": {"title": "Set", "track_count": 3, "tracks": [
        {"id": 1, "title": "One", "user": {"username": "uploader"}, "publisher_metadata": {"artist": "Real Artist"}},
        {"id": 2}, {"id": 3}]}}]
    page = (f'<script>window.__sc_hydration = {json.dumps(hydration)};</script>'
            '<script crossorigin src="https://a-v2.sndcdn.com/assets/1-a.js"></script>')
    get = fake_web({
        "https://soundcloud.com/user/sets/set": page,
        "https://a-v2.sndcdn.com/assets/1-a.js": 'x={client_id:"' + "c" * 32 + '"}',
        "https://api-v2.soundcloud.com/tracks?ids=2,3&client_id=" + "c" * 32:
            json.dumps([{"id": 3, "title": "Three", "user": {"username": "C"}}, {"id": 2, "title": "Two", "user": {"username": "B"}}]),
    })
    found = pl.read("https://m.soundcloud.com/user/sets/set?si=1", get=get)
    assert [(e["title"], e["artist"]) for e in found["entries"]] == [("One", "Real Artist"), ("Two", "B"), ("Three", "C")]
    assert found["partial"] is False


def test_tidal_keeps_the_embed_order_and_reads_the_size():
    embed = 'window.tidalMediaSessionMetadata={"9":{"title":"B","artist":"X, Y","album":"L"},"1":{"title":"A","artist":"Z"}};\n'
    share = '<meta property="og:title" content="Mix &amp; Match"><meta property="og:description" content="Playlist - Mix - 120 items">'
    get = fake_web({"https://embed.tidal.com/playlists/": embed, "https://tidal.com/browse/playlist/": share})
    found = pl.read("https://listen.tidal.com/playlist/1cbce60c-2412-451e-a1e1-39faf87b617b", get=get)
    assert [e["title"] for e in found["entries"]] == ["B", "A"] and found["entries"][0]["artist"] == "X"
    assert (found["name"], found["total"], found["partial"]) == ("Mix & Match", 120, True)


def test_jiosaavn_and_netease_read_their_open_apis():
    song = {"title": "Chann (From &quot;Udta Teer&quot;)", "more_info": {"album": "Udta Teer", "artistMap": {"primary_artists": [{"name": "Arijit"}]}}}
    get = fake_web({
        "https://www.jiosaavn.com/api.php": json.dumps({"title": "Hits", "list_count": "1", "list": [song]}),
        "https://music.163.com/api/playlist/detail?id=3778678": json.dumps({"result": {"name": "热歌榜", "trackCount": 1,
            "tracks": [{"name": "Hong Kong", "artists": [{"name": "The Weeknd"}], "album": {"name": "HK"}}]}}),
    })
    saavn = pl.read("https://www.jiosaavn.com/featured/weekly-top-songs/8MT-LQlP35c_", get=get)
    assert saavn["entries"] == [{"title": 'Chann (From "Udta Teer")', "artist": "Arijit", "album": "Udta Teer"}]
    assert get.asked[0][0].startswith("https://www.jiosaavn.com/api.php?__call=webapi.get&token=8MT-LQlP35c_&type=playlist")
    netease = pl.read("https://music.163.com/#/playlist?id=3778678", get=get)
    assert netease["entries"] == [{"title": "Hong Kong", "artist": "The Weeknd", "album": "HK"}]


def test_any_page_with_a_schema_org_track_list_can_be_read():
    playlist = {"@type": "MusicPlaylist", "name": "Classical", "numTracks": 2, "track": [
        {"@type": "MusicRecording", "name": "Allegro", "byArtist": "Edith", "inAlbum": "Tour"},
        {"@type": "MusicRecording", "name": "Adagio", "byArtist": {"name": "Karl"}}]}
    album = {"@type": "MusicAlbum", "name": "Alpha", "byArtist": {"name": "C418"}, "numTracks": 3,
             "track": {"itemListElement": [{"item": {"name": "Key"}}, {"item": {"name": "Door"}}]}}
    ld = lambda data: f'<script type="application/ld+json">{json.dumps(data)}</script>'  # noqa: E731
    get = fake_web({"https://www.qobuz.com/": ld(playlist), "https://c418.bandcamp.com/": ld({"@type": "Organization"}) + ld(album)})
    qobuz = pl.read("https://www.qobuz.com/us-en/playlists/classical/6862731", get=get)
    assert [(e["title"], e["artist"], e["album"]) for e in qobuz["entries"]] == [("Allegro", "Edith", "Tour"), ("Adagio", "Karl", "")]
    bandcamp = pl.read("https://c418.bandcamp.com/album/alpha", get=get)
    assert [(e["title"], e["artist"], e["album"]) for e in bandcamp["entries"]] == [("Key", "C418", "Alpha"), ("Door", "C418", "Alpha")]
    assert bandcamp["partial"] is True


def test_a_list_past_the_ceiling_says_it_was_cut():
    big = {"@type": "MusicPlaylist", "name": "Huge", "track": [{"name": f"Song {i}", "byArtist": "A"} for i in range(2500)]}
    page = f'<script type="application/ld+json">{json.dumps(big)}</script>'
    found = pl.read("https://example.org/huge", get=fake_web({"https://example.org/": page}))
    assert (len(found["entries"]), found["total"], found["partial"]) == (2000, 2500, True)


def test_the_computer_never_opens_its_own_network():
    for url in ("https://127.0.0.1/x", "https://[::1]/x", "https://10.0.0.5/", "https://169.254.169.254/latest", "http://example.com/"):
        with pytest.raises(pl.LinkError):
            pl._check_public(url)


def test_other_converters_csvs_and_m3u_files():
    exportify = ('"Track URI","Track Name","Artist URI(s)","Artist Name(s)","Album URI","Album Name"\n'
                 '"spotify:track:x","Nokia","spotify:artist:y","Drake","spotify:album:z","Some Album"\n')
    wanted = [{"title": "Nokia", "artist": "Drake", "album": "Some Album"}]
    assert pl.parse_text(exportify) == wanted
    assert pl.parse_text("Track name,Artist name,Album,Playlist name,Type,ISRC\nNokia,Drake,Some Album,Mine,Favorite,US1\n") == wanted
    assert pl.parse_text("title,artist,album,isrc\nNokia,Drake,Some Album,US1\n") == wanted
    m3u = "#EXTM3U\n#EXTINF:215,Drake - Nokia\nC:\\Music\\x.mp3\n"
    assert pl.parse_text(m3u) == [{"title": "Nokia", "artist": "Drake", "album": ""}]
    assert pl.parse_text("/Users/me/Music/Drake - Nokia.mp3\n") == [{"title": "Nokia", "artist": "Drake", "album": ""}]
    assert pl.parse_text("AC/DC - Back In Black\n") == [{"title": "Back In Black", "artist": "AC/DC", "album": ""}]
