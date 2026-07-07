"""Music player command handlers.

Ported from jarvis (``jarvis_control_modules/music_bridge.py``). Playlist/track
CRUD are thin sync wrappers over the SQLite state. Search and stream-URL
resolution hit yt-dlp, which blocks on network I/O, so those run on a daemon
thread and broadcast their result via the thread-safe ``shared.notify_browsers``.
No audio bytes ever flow through the server — the browser's <audio> element
consumes the resolved URL directly.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import shared

# yt-dlp is an optional dependency; the player degrades gracefully without it.
try:
    import yt_dlp  # type: ignore

    YTDLP_AVAILABLE = True
    _ytdlp_error = ""
except Exception as exc:  # pragma: no cover - only when dep missing
    YTDLP_AVAILABLE = False
    _ytdlp_error = str(exc)

try:
    from ytmusicapi import YTMusic  # type: ignore

    YTMUSIC_AVAILABLE = True
    _ytmusic_error = ""
except Exception as exc:  # pragma: no cover - optional metadata layer
    YTMUSIC_AVAILABLE = False
    _ytmusic_error = str(exc)

_ytmusic_client = None
_ytmusic_lock = threading.Lock()

# Conservative TTL hint (~6h) surfaced to the client so it re-resolves a stream
# on a long pause/resume rather than only discovering staleness via a failed load.
STREAM_URL_TTL_HINT_S = 21600
STREAM_URL_CACHE_TTL_S = min(STREAM_URL_TTL_HINT_S, 5 * 60 * 60)

_stream_cache_lock = threading.Lock()
_stream_url_cache: dict[str, dict[str, Any]] = {}

_SEARCH_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "extract_flat": True,   # metadata only, no per-result stream resolution
    "skip_download": True,
    "default_search": "ytsearch",
}

_STREAM_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "skip_download": True,
    # Bias toward broadly HTML5-<audio>-compatible containers over a bare
    # bestaudio (which can hand back formats Chrome/Edge won't play).
    "format": "bestaudio[ext=m4a]/bestaudio/best",
}


def _run_bg(target, *args):
    threading.Thread(target=target, args=args, name="rainette-music", daemon=True).start()


def _stream_cache_get(source_id: str) -> dict[str, Any] | None:
    now = time.time()
    with _stream_cache_lock:
        cached = _stream_url_cache.get(source_id)
        if not cached:
            return None
        if float(cached.get("expires_at") or 0) <= now:
            _stream_url_cache.pop(source_id, None)
            return None
        return dict(cached)


def _stream_cache_set(source_id: str, *, url: str, title: str = "", artist: str = "",
                      duration_s=None, thumbnail_url: str = "") -> None:
    with _stream_cache_lock:
        _stream_url_cache[source_id] = {
            "url": url,
            "title": title,
            "artist": artist,
            "duration_s": duration_s,
            "thumbnail_url": thumbnail_url,
            "expires_at": time.time() + STREAM_URL_CACHE_TTL_S,
        }


def _stream_cache_invalidate(source_id: str) -> None:
    with _stream_cache_lock:
        _stream_url_cache.pop(source_id, None)


# ── Search ───────────────────────────────────────────────────────────────────

def cmd_music_search(msg):
    req_id = msg.get("id")
    query = str(msg.get("query") or "").strip()
    if not YTDLP_AVAILABLE:
        shared.notify_browsers({"type": "music_search_result", "id": req_id, "ok": False,
                                "msg": "yt-dlp not installed: " + _ytdlp_error, "items": []})
        return
    if not query:
        shared.notify_browsers({"type": "music_search_result", "id": req_id, "ok": False, "msg": "empty query", "items": []})
        return
    _run_bg(_search_worker, req_id, query)


def _search_worker(req_id, query):
    try:
        items = _yt_dlp_search_items(query, limit=20)
        shared.notify_browsers({"type": "music_search_result", "id": req_id, "ok": True, "items": items, "query": query})
    except Exception as e:
        shared.notify_browsers({"type": "music_search_result", "id": req_id, "ok": False, "msg": str(e), "items": []})


def _yt_dlp_search_items(query: str, *, limit: int = 20) -> list[dict[str, Any]]:
    with yt_dlp.YoutubeDL(_SEARCH_OPTS) as ydl:
        info = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
    items = []
    for entry in (info or {}).get("entries", []) or []:
        if not entry:
            continue
        items.append({
            "source": "youtube",
            "source_id": entry.get("id") or "",
            "title": entry.get("title") or "(untitled)",
            "artist": entry.get("uploader") or entry.get("channel") or "",
            "duration_s": entry.get("duration"),
            "thumbnail_url": _pick_thumb(entry),
            "metadata": {"source_detail": "yt-dlp"},
        })
    return [it for it in items if it["source_id"]]


def _pick_thumb(entry):
    thumbs = entry.get("thumbnails")
    if isinstance(thumbs, list) and thumbs:
        return thumbs[-1].get("url", "") or ""
    return entry.get("thumbnail") or ""


def _ytmusic():
    if not YTMUSIC_AVAILABLE:
        raise RuntimeError("ytmusicapi not installed: " + _ytmusic_error)
    global _ytmusic_client
    with _ytmusic_lock:
        if _ytmusic_client is None:
            _ytmusic_client = YTMusic()
        return _ytmusic_client


def _ytm_thumb(item: dict[str, Any]) -> str:
    thumbs = item.get("thumbnails")
    if isinstance(thumbs, list) and thumbs:
        return thumbs[-1].get("url", "") or ""
    return ""


def _ytm_artists(item: dict[str, Any]) -> list[dict[str, str]]:
    out = []
    artists = item.get("artists")
    if isinstance(artists, list):
        for artist in artists:
            if not isinstance(artist, dict):
                continue
            name = str(artist.get("name") or "").strip()
            if name:
                out.append({"name": name, "id": str(artist.get("id") or artist.get("browseId") or "").strip()})
    if not out and item.get("artist"):
        out.append({"name": str(item.get("artist") or ""), "id": ""})
    return out


def _ytm_track(item: dict[str, Any], album_hint: dict[str, Any] | None = None) -> dict[str, Any] | None:
    video_id = item.get("videoId") or item.get("video_id")
    if not video_id:
        return None
    artists = _ytm_artists(item)
    primary = artists[0] if artists else {"name": "", "id": ""}
    album = item.get("album") if isinstance(item.get("album"), dict) else (album_hint or {})
    album_name = str(album.get("name") or album.get("title") or "").strip()
    album_id = str(album.get("id") or album.get("browseId") or "").strip()
    duration = item.get("duration_seconds")
    if duration is None:
        duration = item.get("duration_s")
    metadata = {
        "source_detail": "ytmusic",
        "result_type": item.get("resultType") or item.get("videoType") or "",
        "artists": artists,
        "artist_id": primary.get("id") or "",
        "album": {"name": album_name, "id": album_id} if (album_name or album_id) else {},
        "album_name": album_name,
        "album_id": album_id,
    }
    return {
        "source": "youtube",
        "source_id": str(video_id),
        "title": item.get("title") or "(untitled)",
        "artist": primary.get("name") or "",
        "duration_s": duration,
        "thumbnail_url": _ytm_thumb(item) or _ytm_thumb(album) or "",
        "metadata": metadata,
    }


def _ytm_artist(item: dict[str, Any]) -> dict[str, Any] | None:
    artist_id = item.get("browseId") or item.get("channelId") or item.get("id")
    name = item.get("artist") or item.get("title") or item.get("name")
    if not artist_id and not name:
        return None
    return {
        "id": str(artist_id or ""),
        "name": str(name or "Unknown artist"),
        "thumbnail_url": _ytm_thumb(item),
        "subscribers": item.get("subscribers") or "",
        "result_type": item.get("resultType") or "artist",
    }


def _ytm_album(item: dict[str, Any], artist_hint: str = "") -> dict[str, Any] | None:
    album_id = item.get("browseId") or item.get("id")
    title = item.get("title") or item.get("name")
    if not album_id and not title:
        return None
    artists = _ytm_artists(item)
    artist = artist_hint or (artists[0]["name"] if artists else str(item.get("artist") or ""))
    return {
        "id": str(album_id or ""),
        "title": str(title or "Unknown album"),
        "artist": artist,
        "artist_id": artists[0]["id"] if artists else "",
        "year": item.get("year") or "",
        "thumbnail_url": _ytm_thumb(item),
        "result_type": item.get("resultType") or "album",
        "browse_id": str(album_id or ""),
        "params": item.get("params") or "",
    }


# ── Stream URL resolution (only when the user actually presses play) ──────────

def cmd_music_stream_url(msg):
    req_id = msg.get("id")
    source_id = str(msg.get("source_id") or "").strip()
    track_id = str(msg.get("track_id") or "").strip()
    track_payload = msg.get("track") if isinstance(msg.get("track"), dict) else None
    prefetch = bool(msg.get("prefetch"))
    force_refresh = bool(msg.get("force_refresh") or msg.get("invalidate_cache"))
    if not YTDLP_AVAILABLE:
        shared.notify_browsers({"type": "music_stream_url_result", "id": req_id, "ok": False,
                                "msg": "yt-dlp not installed", "track_id": track_id})
        return
    if not source_id:
        shared.notify_browsers({"type": "music_stream_url_result", "id": req_id, "ok": False, "msg": "source_id required", "track_id": track_id})
        return
    if not prefetch and not track_id and track_payload:
        try:
            track_id = _upsert_track_from_msg(track_payload)
        except Exception:
            track_id = ""
    if force_refresh:
        _stream_cache_invalidate(source_id)
    elif cached := _stream_cache_get(source_id):
        if track_id and not prefetch:
            try:
                shared.STATE.log_play(track_id)
            except Exception:
                pass
        ttl = max(1, int(float(cached.get("expires_at") or 0) - time.time()))
        shared.notify_browsers({
            "type": "music_stream_url_result", "id": req_id, "ok": True,
            "track_id": track_id, "source_id": source_id, "url": cached.get("url", ""),
            "expires_hint_s": min(ttl, STREAM_URL_CACHE_TTL_S), "cached": True,
            "title": cached.get("title") or "", "artist": cached.get("artist") or "",
            "duration_s": cached.get("duration_s"), "thumbnail_url": cached.get("thumbnail_url") or "",
        })
        return
    _run_bg(_stream_worker, req_id, source_id, track_id, not prefetch)


def _stream_worker(req_id, source_id, track_id, log_play=True):
    try:
        url = source_id if source_id.startswith("http") else f"https://www.youtube.com/watch?v={source_id}"
        with yt_dlp.YoutubeDL(_STREAM_OPTS) as ydl:
            info = ydl.extract_info(url, download=False)
        stream_url = info.get("url")
        if not stream_url:
            # Some extractors nest the playable URL under requested formats.
            for fmt in reversed(info.get("requested_formats") or info.get("formats") or []):
                if fmt.get("url"):
                    stream_url = fmt["url"]
                    break
        if not stream_url:
            raise RuntimeError("no playable stream url returned")
        title = info.get("title") or ""
        artist = info.get("uploader") or ""
        duration_s = info.get("duration")
        thumbnail_url = _pick_thumb(info)
        _stream_cache_set(source_id, url=stream_url, title=title, artist=artist,
                          duration_s=duration_s, thumbnail_url=thumbnail_url)
        if log_play and track_id:
            try:
                shared.STATE.log_play(track_id)
            except Exception:
                pass
        shared.notify_browsers({
            "type": "music_stream_url_result", "id": req_id, "ok": True,
            "track_id": track_id, "source_id": source_id, "url": stream_url,
            "expires_hint_s": STREAM_URL_CACHE_TTL_S, "cached": False,
            "title": title, "artist": artist,
            "duration_s": duration_s, "thumbnail_url": thumbnail_url,
        })
    except Exception as e:
        shared.notify_browsers({"type": "music_stream_url_result", "id": req_id, "ok": False, "msg": str(e), "track_id": track_id})


# ── Playlist / track CRUD (sync, thin over MusicState) ────────────────────────

def cmd_music_playlist_list(msg):
    req_id = msg.get("id")
    try:
        shared.notify_browsers({"type": "music_playlist_list_result", "id": req_id, "ok": True, "playlists": shared.STATE.list_playlists()})
    except Exception as e:
        shared.notify_browsers({"type": "music_playlist_list_result", "id": req_id, "ok": False, "msg": str(e), "playlists": []})


def cmd_music_playlist_create(msg):
    req_id = msg.get("id")
    try:
        pl = shared.STATE.create_playlist(str(msg.get("name") or ""), str(msg.get("description") or ""))
        shared.notify_browsers({"type": "music_playlist_created", "id": req_id, "ok": True, "playlist": pl, "playlists": shared.STATE.list_playlists()})
    except Exception as e:
        shared.notify_browsers({"type": "music_playlist_created", "id": req_id, "ok": False, "msg": str(e)})


def cmd_music_playlist_rename(msg):
    req_id = msg.get("id")
    try:
        pl = shared.STATE.rename_playlist(str(msg.get("playlist_id") or ""), str(msg.get("name") or ""))
        shared.notify_browsers({"type": "music_playlist_renamed", "id": req_id, "ok": True, "playlist": pl, "playlists": shared.STATE.list_playlists()})
    except Exception as e:
        shared.notify_browsers({"type": "music_playlist_renamed", "id": req_id, "ok": False, "msg": str(e)})


def cmd_music_playlist_delete(msg):
    req_id = msg.get("id")
    try:
        ok = shared.STATE.delete_playlist(str(msg.get("playlist_id") or ""))
        shared.notify_browsers({"type": "music_playlist_deleted", "id": req_id, "ok": ok, "playlists": shared.STATE.list_playlists()})
    except Exception as e:
        shared.notify_browsers({"type": "music_playlist_deleted", "id": req_id, "ok": False, "msg": str(e)})


def _upsert_track_from_msg(msg) -> str:
    metadata = msg.get("metadata") if isinstance(msg.get("metadata"), dict) else {}
    track = shared.STATE.upsert_track(
        source=str(msg.get("source") or "youtube"),
        source_id=str(msg.get("source_id") or ""),
        title=str(msg.get("title") or ""),
        artist=str(msg.get("artist") or ""),
        duration_s=msg.get("duration_s"),
        thumbnail_url=str(msg.get("thumbnail_url") or ""),
        metadata=metadata,
    )
    return track["id"]


def cmd_music_playlist_add_track(msg):
    req_id = msg.get("id")
    try:
        playlist_id = str(msg.get("playlist_id") or "")
        track_id = str(msg.get("track_id") or "").strip() or _upsert_track_from_msg(msg)
        shared.STATE.add_track_to_playlist(playlist_id, track_id)
        shared.notify_browsers({"type": "music_playlist_track_added", "id": req_id, "ok": True,
                                "playlist_id": playlist_id, "tracks": shared.STATE.list_playlist_tracks(playlist_id)})
    except Exception as e:
        shared.notify_browsers({"type": "music_playlist_track_added", "id": req_id, "ok": False, "msg": str(e)})


def cmd_music_playlist_remove_track(msg):
    req_id = msg.get("id")
    try:
        playlist_id = str(msg.get("playlist_id") or "")
        ok = shared.STATE.remove_track_from_playlist(playlist_id, str(msg.get("track_id") or ""))
        shared.notify_browsers({"type": "music_playlist_track_removed", "id": req_id, "ok": ok,
                                "playlist_id": playlist_id, "tracks": shared.STATE.list_playlist_tracks(playlist_id)})
    except Exception as e:
        shared.notify_browsers({"type": "music_playlist_track_removed", "id": req_id, "ok": False, "msg": str(e)})


def cmd_music_playlist_tracks(msg):
    req_id = msg.get("id")
    try:
        playlist_id = str(msg.get("playlist_id") or "")
        shared.notify_browsers({"type": "music_playlist_tracks_result", "id": req_id, "ok": True,
                                "playlist_id": playlist_id, "tracks": shared.STATE.list_playlist_tracks(playlist_id)})
    except Exception as e:
        shared.notify_browsers({"type": "music_playlist_tracks_result", "id": req_id, "ok": False, "msg": str(e), "tracks": []})


def cmd_music_recent(msg):
    req_id = msg.get("id")
    try:
        shared.notify_browsers({"type": "music_recent_result", "id": req_id, "ok": True, "tracks": shared.STATE.list_recent_plays(limit=40)})
    except Exception as e:
        shared.notify_browsers({"type": "music_recent_result", "id": req_id, "ok": False, "msg": str(e), "tracks": []})


def cmd_music_now_playing_set(msg):
    """Broadcast the current track to every open tab so the mini-player and
    full page stay in sync. Pure fan-out — no persistence beyond play history
    (which cmd_music_stream_url already logs)."""
    track = msg.get("track") if isinstance(msg.get("track"), dict) else None
    playback_state = msg.get("state") or "playing"
    payload = {
        "type": "music_now_playing",
        "ok": True,
        "track": track,
        "state": playback_state,
        "playing": bool(msg.get("playing")),
        "loop": bool(msg.get("loop")),
        "current_time": msg.get("current_time") or 0,
        "duration": msg.get("duration") or 0,
    }
    if isinstance(msg.get("queue"), list):
        payload.update({
            "queue": msg.get("queue"),
            "index": msg.get("index", -1),
            "queue_count": msg.get("queue_count", len(msg.get("queue") or [])),
            "queue_duration": msg.get("queue_duration") or 0,
        })
    shared.notify_browsers(payload)


def cmd_music_remote_play(msg):
    """Relay a 'play this queue' command from the browser window to the detached
    player window. Pure fan-out — the player window owns the actual <audio>."""
    shared.notify_browsers(msg)


def cmd_music_remote_control(msg):
    """Relay a transport command (toggle/next/prev/loop/seek/volume) from the
    browser window to the detached player window."""
    shared.notify_browsers(msg)


def cmd_music_request_state(msg):
    """Relay the player window's 'what's the current queue?' request to the
    browser window (which answers with a fresh music_remote_play). Covers the
    case where the player window connects after a play was already issued."""
    shared.notify_browsers(msg)


def cmd_music_catalog_search(msg):
    req_id = msg.get("id")
    query = str(msg.get("query") or "").strip()
    if not query:
        shared.notify_browsers({"type": "music_catalog_search_result", "id": req_id, "ok": False, "msg": "empty query", "songs": [], "artists": [], "albums": []})
        return
    _run_bg(_catalog_search_worker, req_id, query)


def _catalog_search_worker(req_id, query):
    try:
        if YTMUSIC_AVAILABLE:
            yt = _ytmusic()
            seen = set()
            songs = []
            for raw in (yt.search(query, filter="songs", limit=25) or []) + (yt.search(query, filter="videos", limit=8) or []):
                track = _ytm_track(raw)
                if not track or track["source_id"] in seen:
                    continue
                seen.add(track["source_id"])
                songs.append(track)
            artists = [a for a in (_ytm_artist(raw) for raw in (yt.search(query, filter="artists", limit=10) or [])) if a]
            albums = [a for a in (_ytm_album(raw) for raw in (yt.search(query, filter="albums", limit=12) or [])) if a]
            shared.notify_browsers({
                "type": "music_catalog_search_result",
                "id": req_id,
                "ok": True,
                "query": query,
                "source": "ytmusic",
                "songs": songs,
                "artists": artists,
                "albums": albums,
            })
            return
        if not YTDLP_AVAILABLE:
            raise RuntimeError("yt-dlp not installed: " + _ytdlp_error)
        songs = _yt_dlp_search_items(query, limit=30)
        shared.notify_browsers({
            "type": "music_catalog_search_result",
            "id": req_id,
            "ok": True,
            "query": query,
            "source": "yt-dlp",
            "songs": songs,
            "artists": [],
            "albums": [],
            "msg": "Install ytmusicapi for artist and album catalog browsing.",
        })
    except Exception as exc:
        shared.notify_browsers({"type": "music_catalog_search_result", "id": req_id, "ok": False, "msg": str(exc), "songs": [], "artists": [], "albums": []})


def cmd_music_artist_catalog(msg):
    req_id = msg.get("id")
    artist_id = str(msg.get("artist_id") or msg.get("id_value") or "").strip()
    name = str(msg.get("name") or msg.get("artist") or "").strip()
    if not artist_id and not name:
        shared.notify_browsers({"type": "music_artist_catalog_result", "id": req_id, "ok": False, "msg": "artist id or name required"})
        return
    _run_bg(_artist_catalog_worker, req_id, artist_id, name)


def _artist_catalog_worker(req_id, artist_id, name):
    try:
        if not YTMUSIC_AVAILABLE:
            if not YTDLP_AVAILABLE:
                raise RuntimeError("ytmusicapi and yt-dlp are not installed")
            songs = _yt_dlp_search_items(name or artist_id, limit=50)
            shared.notify_browsers({
                "type": "music_artist_catalog_result",
                "id": req_id,
                "ok": True,
                "source": "yt-dlp",
                "artist": {"id": artist_id, "name": name or artist_id},
                "songs": songs,
                "albums": [],
                "singles": [],
                "msg": "Install ytmusicapi for full artist album/single catalog browsing.",
            })
            return
        yt = _ytmusic()
        if not artist_id:
            matches = yt.search(name, filter="artists", limit=1) or []
            first = _ytm_artist(matches[0]) if matches else None
            artist_id = first.get("id", "") if first else ""
        if not artist_id:
            raise RuntimeError("artist not found")
        info = yt.get_artist(artist_id) or {}
        artist = {
            "id": artist_id,
            "name": info.get("name") or name or "Unknown artist",
            "description": info.get("description") or "",
            "thumbnail_url": _ytm_thumb(info),
            "subscribers": info.get("subscribers") or "",
        }
        songs = _section_tracks(yt, info.get("songs"), limit=100)
        videos = _section_tracks(yt, info.get("videos"), limit=50)
        albums = _artist_release_list(yt, artist_id, info.get("albums"), artist["name"])
        singles = _artist_release_list(yt, artist_id, info.get("singles"), artist["name"])
        shared.notify_browsers({
            "type": "music_artist_catalog_result",
            "id": req_id,
            "ok": True,
            "source": "ytmusic",
            "artist": artist,
            "songs": songs,
            "videos": videos,
            "albums": albums,
            "singles": singles,
        })
    except Exception as exc:
        shared.notify_browsers({"type": "music_artist_catalog_result", "id": req_id, "ok": False, "msg": str(exc)})


def _section_tracks(yt, section, *, limit: int) -> list[dict[str, Any]]:
    if not isinstance(section, dict):
        return []
    raw_items = []
    browse_id = section.get("browseId")
    if browse_id:
        try:
            playlist = yt.get_playlist(browse_id, limit=limit) or {}
            raw_items = playlist.get("tracks") or []
        except Exception:
            raw_items = section.get("results") or []
    else:
        raw_items = section.get("results") or []
    tracks = []
    seen = set()
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        track = _ytm_track(raw)
        if not track or track["source_id"] in seen:
            continue
        seen.add(track["source_id"])
        tracks.append(track)
        if len(tracks) >= limit:
            break
    return tracks


def _artist_release_list(yt, artist_id: str, section, artist_name: str) -> list[dict[str, Any]]:
    if not isinstance(section, dict):
        return []
    raw_items = section.get("results") or []
    params = section.get("params")
    if params:
        try:
            raw_items = yt.get_artist_albums(artist_id, params) or raw_items
        except Exception:
            pass
    return [album for album in (_ytm_album(raw, artist_name) for raw in raw_items if isinstance(raw, dict)) if album]


def cmd_music_album_tracks(msg):
    req_id = msg.get("id")
    album_id = str(msg.get("album_id") or msg.get("browse_id") or "").strip()
    title = str(msg.get("title") or msg.get("album") or "").strip()
    artist = str(msg.get("artist") or "").strip()
    if not album_id and not title:
        shared.notify_browsers({"type": "music_album_tracks_result", "id": req_id, "ok": False, "msg": "album id or title required", "tracks": []})
        return
    _run_bg(_album_tracks_worker, req_id, album_id, title, artist)


def _album_tracks_worker(req_id, album_id, title, artist):
    try:
        album = {"id": album_id, "title": title, "artist": artist}
        if YTMUSIC_AVAILABLE and album_id:
            yt = _ytmusic()
            info = yt.get_album(album_id) or {}
            album = _ytm_album({**info, "browseId": album_id}, artist) or album
            album_hint = {"name": album.get("title") or title, "id": album_id, "thumbnails": info.get("thumbnails") or []}
            tracks = [t for t in (_ytm_track(raw, album_hint) for raw in (info.get("tracks") or [])) if t]
            shared.notify_browsers({"type": "music_album_tracks_result", "id": req_id, "ok": True, "source": "ytmusic", "album": album, "tracks": tracks})
            return
        if not YTDLP_AVAILABLE:
            raise RuntimeError("yt-dlp not installed: " + _ytdlp_error)
        query = " ".join(part for part in [artist, title] if part)
        tracks = _yt_dlp_search_items(query, limit=30)
        shared.notify_browsers({"type": "music_album_tracks_result", "id": req_id, "ok": True, "source": "yt-dlp", "album": album, "tracks": tracks})
    except Exception as exc:
        shared.notify_browsers({"type": "music_album_tracks_result", "id": req_id, "ok": False, "msg": str(exc), "tracks": []})


def cmd_music_library_index(msg):
    req_id = msg.get("id")
    try:
        limit = max(1, min(int(msg.get("limit", 500) or 500), 1000))
    except Exception:
        limit = 500
    try:
        shared.notify_browsers({"type": "music_library_index_result", "id": req_id, "ok": True, **shared.STATE.music_library_index(limit=limit)})
    except Exception as exc:
        shared.notify_browsers({"type": "music_library_index_result", "id": req_id, "ok": False, "msg": str(exc), "tracks": [], "artists": [], "albums": []})


def cmd_music_status(msg):
    """Report whether the streaming backend is available (for the UI banner)."""
    req_id = msg.get("id")
    shared.notify_browsers({"type": "music_status", "id": req_id, "ok": True,
                            "ytdlp_available": YTDLP_AVAILABLE, "ytdlp_error": _ytdlp_error,
                            "ytmusic_available": YTMUSIC_AVAILABLE, "ytmusic_error": _ytmusic_error})


DISPATCH = {
    "music_search":                 cmd_music_search,
    "music_stream_url":             cmd_music_stream_url,
    "music_playlist_list":          cmd_music_playlist_list,
    "music_playlist_create":        cmd_music_playlist_create,
    "music_playlist_rename":        cmd_music_playlist_rename,
    "music_playlist_delete":        cmd_music_playlist_delete,
    "music_playlist_add_track":     cmd_music_playlist_add_track,
    "music_playlist_remove_track":  cmd_music_playlist_remove_track,
    "music_playlist_tracks":        cmd_music_playlist_tracks,
    "music_recent":                 cmd_music_recent,
    "music_now_playing_set":        cmd_music_now_playing_set,
    "music_remote_play":            cmd_music_remote_play,
    "music_remote_control":         cmd_music_remote_control,
    "music_request_state":          cmd_music_request_state,
    "music_catalog_search":         cmd_music_catalog_search,
    "music_artist_catalog":         cmd_music_artist_catalog,
    "music_album_tracks":           cmd_music_album_tracks,
    "music_library_index":          cmd_music_library_index,
    "music_status":                 cmd_music_status,
}
