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


def test_the_engine_reports_a_bad_link_plainly(monkeypatch):
    messages = []
    monkeypatch.setattr(music_bridge.shared, "notify_browsers", messages.append)
    monkeypatch.setattr(music_bridge, "_run_bg", lambda target, *args: target(*args))
    music_bridge.DISPATCH["music_playlist_link"]({"id": "r", "url": "https://example.com/x"})
    assert messages[-1] == {"type": "music_playlist_link_result", "id": "r", "ok": False,
                            "msg": "Paste a Spotify, Apple Music, Deezer or YouTube playlist link."}
