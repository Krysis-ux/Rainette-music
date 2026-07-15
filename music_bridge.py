"""Music player command handlers.

Playlist/track CRUD are thin sync wrappers over the SQLite state. Search and stream-URL
resolution hit yt-dlp, which blocks on network I/O, so those run on a daemon
thread and broadcast their result via the thread-safe ``shared.notify_browsers``.
No audio bytes ever flow through the server — the browser's <audio> element
consumes the resolved URL directly.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import shared

# yt-dlp normally prefers certifi.  ``no-certifi`` asks its own request layer to
# load the operating-system trust store instead, including enterprise roots on
# Windows, without replacing ``ssl.SSLContext`` process-wide.  The old global
# truststore injection installed a client-only context in the stdlib module;
# that made the companion's TLS *server* accept and then drop every connection.
_SYSTEM_TRUST_COMPAT = {"no-certifi"}
SYSTEM_TRUST_ENABLED = True

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
    "compat_opts": _SYSTEM_TRUST_COMPAT,
}

_STREAM_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "skip_download": True,
    # Bias toward broadly HTML5-<audio>-compatible containers over a bare
    # bestaudio (which can hand back formats Chrome/Edge won't play).
    "format": "bestaudio[ext=m4a]/bestaudio/best",
    "compat_opts": _SYSTEM_TRUST_COMPAT,
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
        shared.notify_browsers({
            "type": "music_playlist_list_result",
            "id": req_id,
            "ok": True,
            "playlists": shared.STATE.list_playlists(),
            "folders": shared.STATE.list_playlist_folders(),
        })
    except Exception as e:
        shared.notify_browsers({"type": "music_playlist_list_result", "id": req_id, "ok": False, "msg": str(e), "playlists": [], "folders": []})


def _broadcast_playlist_state(msg_type: str, req_id, *, ok: bool = True, extra: dict[str, Any] | None = None, error: Exception | None = None) -> None:
    payload = {
        "type": msg_type,
        "id": req_id,
        "ok": ok,
        "playlists": shared.STATE.list_playlists(),
        "folders": shared.STATE.list_playlist_folders(),
    }
    if extra:
        payload.update(extra)
    if error is not None:
        payload["msg"] = str(error)
    shared.notify_browsers(payload)


def cmd_music_playlist_create(msg):
    req_id = msg.get("id")
    try:
        pl = shared.STATE.create_playlist(str(msg.get("name") or ""), str(msg.get("description") or ""))
        _broadcast_playlist_state("music_playlist_created", req_id, extra={"playlist": pl})
    except Exception as e:
        shared.notify_browsers({"type": "music_playlist_created", "id": req_id, "ok": False, "msg": str(e)})


def cmd_music_playlist_rename(msg):
    req_id = msg.get("id")
    try:
        pl = shared.STATE.rename_playlist(str(msg.get("playlist_id") or ""), str(msg.get("name") or ""))
        _broadcast_playlist_state("music_playlist_renamed", req_id, extra={"playlist": pl})
    except Exception as e:
        shared.notify_browsers({"type": "music_playlist_renamed", "id": req_id, "ok": False, "msg": str(e)})


def cmd_music_playlist_delete(msg):
    req_id = msg.get("id")
    try:
        playlist_id = str(msg.get("playlist_id") or "")
        existing = shared.STATE.get_playlist(playlist_id)
        artwork_key = str((existing or {}).get("artwork_key") or "")
        ok = shared.STATE.delete_playlist(playlist_id)
        if ok and artwork_key:
            _delete_managed_artwork(artwork_key)
        _broadcast_playlist_state("music_playlist_deleted", req_id, ok=ok)
    except Exception as e:
        shared.notify_browsers({"type": "music_playlist_deleted", "id": req_id, "ok": False, "msg": str(e)})


def _delete_managed_artwork(artwork_key: str) -> None:
    policy = shared.POLICY if isinstance(shared.POLICY, dict) else {}
    root_value = policy.get("playlist_artwork_dir")
    if not root_value:
        return
    root = Path(root_value).resolve()
    key = str(artwork_key or "").strip()
    if not key or Path(key).name != key:
        return
    candidate = (root / key).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return
    try:
        candidate.unlink(missing_ok=True)
    except OSError:
        pass


def cmd_music_playlist_update_meta(msg):
    req_id = msg.get("id")
    try:
        pl = shared.STATE.update_playlist_meta(
            str(msg.get("playlist_id") or ""),
            folder_id=msg.get("folder_id") if "folder_id" in msg else None,
            pinned=bool(msg.get("pinned")) if "pinned" in msg else None,
            position=msg.get("position") if "position" in msg else None,
        )
        _broadcast_playlist_state("music_playlist_meta_updated", req_id, extra={"playlist": pl})
    except Exception as e:
        shared.notify_browsers({"type": "music_playlist_meta_updated", "id": req_id, "ok": False, "msg": str(e)})


def cmd_music_playlist_folder_create(msg):
    req_id = msg.get("id")
    try:
        folder = shared.STATE.create_playlist_folder(str(msg.get("name") or ""))
        _broadcast_playlist_state("music_playlist_folder_created", req_id, extra={"folder": folder})
    except Exception as e:
        shared.notify_browsers({"type": "music_playlist_folder_created", "id": req_id, "ok": False, "msg": str(e)})


def cmd_music_playlist_folder_rename(msg):
    req_id = msg.get("id")
    try:
        folder = shared.STATE.rename_playlist_folder(str(msg.get("folder_id") or ""), str(msg.get("name") or ""))
        _broadcast_playlist_state("music_playlist_folder_renamed", req_id, extra={"folder": folder})
    except Exception as e:
        shared.notify_browsers({"type": "music_playlist_folder_renamed", "id": req_id, "ok": False, "msg": str(e)})


def cmd_music_playlist_folder_delete(msg):
    req_id = msg.get("id")
    try:
        ok = shared.STATE.delete_playlist_folder(str(msg.get("folder_id") or ""))
        _broadcast_playlist_state("music_playlist_folder_deleted", req_id, ok=ok)
    except Exception as e:
        shared.notify_browsers({"type": "music_playlist_folder_deleted", "id": req_id, "ok": False, "msg": str(e)})


def cmd_music_playlist_folder_move(msg):
    req_id = msg.get("id")
    try:
        folder = shared.STATE.move_playlist_folder(str(msg.get("folder_id") or ""), int(msg.get("position") or 0))
        _broadcast_playlist_state("music_playlist_folder_moved", req_id, extra={"folder": folder})
    except Exception as e:
        shared.notify_browsers({"type": "music_playlist_folder_moved", "id": req_id, "ok": False, "msg": str(e)})


def cmd_music_smart_playlist_create(msg):
    req_id = msg.get("id")
    try:
        pl = shared.STATE.create_smart_playlist(str(msg.get("name") or ""), msg.get("rules") if isinstance(msg.get("rules"), dict) else {})
        _broadcast_playlist_state("music_smart_playlist_created", req_id, extra={"playlist": pl})
    except Exception as e:
        shared.notify_browsers({"type": "music_smart_playlist_created", "id": req_id, "ok": False, "msg": str(e)})


def cmd_music_smart_playlist_update(msg):
    req_id = msg.get("id")
    try:
        pl = shared.STATE.update_smart_playlist(
            str(msg.get("playlist_id") or ""),
            name=str(msg.get("name")) if "name" in msg else None,
            rules=msg.get("rules") if isinstance(msg.get("rules"), dict) else None,
        )
        _broadcast_playlist_state("music_smart_playlist_updated", req_id, extra={"playlist": pl})
    except Exception as e:
        shared.notify_browsers({"type": "music_smart_playlist_updated", "id": req_id, "ok": False, "msg": str(e)})


def cmd_music_smart_playlist_delete(msg):
    req_id = msg.get("id")
    try:
        playlist_id = str(msg.get("playlist_id") or "")
        existing = shared.STATE.get_playlist(playlist_id)
        artwork_key = str((existing or {}).get("artwork_key") or "")
        ok = shared.STATE.delete_playlist(playlist_id)
        if ok and artwork_key:
            _delete_managed_artwork(artwork_key)
        _broadcast_playlist_state("music_smart_playlist_deleted", req_id, ok=ok)
    except Exception as e:
        shared.notify_browsers({"type": "music_smart_playlist_deleted", "id": req_id, "ok": False, "msg": str(e)})


def cmd_music_smart_playlist_tracks(msg):
    req_id = msg.get("id")
    playlist_id = str(msg.get("playlist_id") or "")
    try:
        tracks = shared.STATE.smart_playlist_tracks(playlist_id)
        shared.notify_browsers({"type": "music_smart_playlist_tracks_result", "id": req_id, "ok": True, "playlist_id": playlist_id, "tracks": tracks})
    except Exception as e:
        shared.notify_browsers({"type": "music_smart_playlist_tracks_result", "id": req_id, "ok": False, "playlist_id": playlist_id, "tracks": [], "msg": str(e)})


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


def cmd_music_recent_delete(msg):
    """Forget plays, then reply with the refreshed list so the tab re-renders.

    ``scope`` is 'track' (needs track_id), 'artist' (needs artist_key, used by
    the Insights top-artist rows), or 'all'. Recents is grouped by track, so a
    single entry has no per-play id - removing one means forgetting that track's
    plays, which is also why the same command backs Insights' remove actions.
    """
    req_id = msg.get("id")
    try:
        scope = str(msg.get("scope") or "track")
        if scope == "all":
            removed = shared.STATE.clear_play_history()
        elif scope == "artist":
            removed = shared.STATE.delete_artist_play_history(str(msg.get("artist_key") or ""))
        else:
            track_id = str(msg.get("track_id") or "").strip()
            if not track_id:
                raise ValueError("track_id is required")
            removed = shared.STATE.delete_play_history(track_id)
        shared.notify_browsers({
            "type": "music_recent_deleted", "id": req_id, "ok": True,
            "scope": scope, "removed": int(removed or 0),
            "tracks": shared.STATE.list_recent_plays(limit=40),
        })
    except Exception as e:
        shared.notify_browsers({
            "type": "music_recent_deleted", "id": req_id, "ok": False, "msg": str(e),
            "tracks": shared.STATE.list_recent_plays(limit=40),
        })


def cmd_music_clear_data(msg):
    """Erase the selected categories of local user data (Settings -> Danger zone).

    Irreversible by design: the picker in the UI is the confirmation step. The
    reply carries refreshed lists so every open surface resets without a reload.
    """
    req_id = msg.get("id")
    try:
        categories = msg.get("categories")
        if not isinstance(categories, list):
            raise ValueError("categories must be a list")
        result = shared.STATE.clear_user_data(categories)
        for artwork_key in result.get("artwork_keys") or []:
            _delete_managed_artwork(artwork_key)
        shared.notify_browsers({
            "type": "music_data_cleared", "id": req_id, "ok": True,
            "cleared": result.get("cleared") or [],
            "counts": result.get("counts") or {},
            "tracks": shared.STATE.list_recent_plays(limit=40),
            "playlists": shared.STATE.list_playlists(),
            "folders": shared.STATE.list_playlist_folders(),
            "followed_artists": shared.STATE.list_followed_artists(),
        })
    except Exception as e:
        shared.notify_browsers({"type": "music_data_cleared", "id": req_id, "ok": False, "msg": str(e), "cleared": []})


def cmd_music_top_artists(msg):
    req_id = msg.get("id")
    try:
        shared.notify_browsers({"type": "music_top_artists_result", "id": req_id, "ok": True, "artists": shared.STATE.list_top_artists(limit=8)})
    except Exception as e:
        shared.notify_browsers({"type": "music_top_artists_result", "id": req_id, "ok": False, "msg": str(e), "artists": []})


def _public_track_from_msg(track: dict[str, Any]) -> dict[str, Any]:
    metadata = track.get("metadata") if isinstance(track.get("metadata"), dict) else {}
    return {
        "id": str(track.get("id") or ""),
        "source": str(track.get("source") or "youtube"),
        "source_id": str(track.get("source_id") or "").strip(),
        "title": str(track.get("title") or "(untitled)")[:400],
        "artist": str(track.get("artist") or "")[:200],
        "duration_s": track.get("duration_s"),
        "thumbnail_url": str(track.get("thumbnail_url") or "")[:600],
        "metadata": metadata,
    }


def _clean_track_list(raw_tracks) -> list[dict[str, Any]]:
    out = []
    for raw in raw_tracks or []:
        if isinstance(raw, dict):
            item = _public_track_from_msg(raw)
            if item["source_id"]:
                out.append(item)
    return out[:300]


def _broadcast_queue_sessions(msg_type: str, req_id, *, ok: bool = True,
                              extra: dict[str, Any] | None = None, error: Exception | None = None) -> None:
    payload = {
        "type": msg_type,
        "id": req_id,
        "ok": ok,
        "sessions": shared.STATE.list_queue_sessions() if ok else [],
    }
    if extra:
        payload.update(extra)
    if error is not None:
        payload["msg"] = str(error)
    shared.notify_browsers(payload)


def cmd_music_queue_session_save(msg):
    req_id = msg.get("id")
    try:
        session = shared.STATE.save_queue_session(
            name=str(msg.get("name") or ""),
            tracks=_clean_track_list(msg.get("tracks") or []),
            index=int(msg.get("index") or 0),
            is_last=bool(msg.get("is_last")),
            session_id=str(msg.get("session_id") or "") or None,
        )
        _broadcast_queue_sessions("music_queue_session_saved", req_id, extra={"session": session})
    except Exception as e:
        shared.notify_browsers({"type": "music_queue_session_saved", "id": req_id, "ok": False, "sessions": [], "msg": str(e)})


def cmd_music_queue_session_list(msg):
    req_id = msg.get("id")
    try:
        _broadcast_queue_sessions("music_queue_session_list_result", req_id)
    except Exception as e:
        shared.notify_browsers({"type": "music_queue_session_list_result", "id": req_id, "ok": False, "sessions": [], "msg": str(e)})


def cmd_music_queue_session_delete(msg):
    req_id = msg.get("id")
    try:
        ok = shared.STATE.delete_queue_session(str(msg.get("session_id") or ""))
        _broadcast_queue_sessions("music_queue_session_deleted", req_id, ok=ok)
    except Exception as e:
        shared.notify_browsers({"type": "music_queue_session_deleted", "id": req_id, "ok": False, "sessions": [], "msg": str(e)})


REPEAT_MODES = ("off", "all", "one")


def _repeat_fields(msg) -> dict:
    """Normalise the repeat/loop pair of a playback message.

    Mirrors web/repeat_mode.mjs. Returns an empty dict when the producer said
    nothing about repeat, so the fan-out cannot silently reset a receiver that
    does have a setting.
    """
    mode = msg.get("repeat")
    if mode not in REPEAT_MODES:
        if not isinstance(msg.get("loop"), bool):
            return {}
        mode = "all" if msg["loop"] else "off"
    return {"repeat": mode, "loop": mode != "off"}


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
        # Repeat is a three-state string ('off' | 'all' | 'one') with `loop` kept
        # as a derived boolean for older consumers. Never coerce `repeat` through
        # bool(): bool("off") is True. A producer that sends neither (the phone,
        # which has no repeat control of its own) must leave the receiver's own
        # setting alone rather than have an absent field mean "off".
        **_repeat_fields(msg),
        "current_time": msg.get("current_time") or 0,
        "duration": msg.get("duration") or 0,
        # Older desktop engines do not send an output id. They are the only
        # legacy producer, so treating an omitted value as desktop keeps the
        # companion's transport controls routed to the device that owns audio.
        # Phone-originated state always supplies ``phone`` explicitly.
        "output_device_id": str(msg.get("output_device_id") or "desktop"),
    }
    if isinstance(msg.get("queue"), list):
        payload.update({
            "queue": msg.get("queue"),
            "index": msg.get("index", -1),
            "queue_count": msg.get("queue_count", len(msg.get("queue") or [])),
            "queue_duration": msg.get("queue_duration") or 0,
        })
    shared.notify_browsers(payload)


def cmd_music_progress(msg):
    """Relay a lightweight playback-position tick from whichever window owns the
    <audio> to every other window, so the main-window docked bar / Now Playing
    view can show a moving progress bar without the engine re-sending the whole
    queue each tick. Pure fan-out."""
    shared.notify_browsers({
        "type": "music_progress",
        "current_time": msg.get("current_time") or 0,
        "duration": msg.get("duration") or 0,
        "playing": bool(msg.get("playing")),
        "source_id": msg.get("source_id") or "",
    })


# ── Lyrics (LRCLIB — free, no API key) ───────────────────────────────────────
_LYRICS_BASE = "https://lrclib.net/api/get"
_lyrics_cache_lock = threading.Lock()
_lyrics_cache: dict[str, dict[str, Any]] = {}


def _lyrics_key(track: dict) -> str:
    return f"{track.get('source') or 'youtube'}:{track.get('source_id') or ''}"


def cmd_music_lyrics(msg):
    """Fetch lyrics for a track from LRCLIB. Network I/O runs on a daemon thread
    and the result is broadcast back keyed on the request id (helperRequest)."""
    req_id = msg.get("id")
    track = msg.get("track") if isinstance(msg.get("track"), dict) else None
    if not track or not (track.get("title")):
        shared.notify_browsers({"type": "music_lyrics_result", "id": req_id, "ok": False, "msg": "no track"})
        return
    key = _lyrics_key(track)
    with _lyrics_cache_lock:
        cached = _lyrics_cache.get(key)
    if cached is not None:
        shared.notify_browsers({"type": "music_lyrics_result", "id": req_id, "ok": True, **cached})
        return
    _run_bg(_lyrics_worker, req_id, track, key)


def _lyrics_worker(req_id, track, key):
    try:
        params = {
            "track_name": track.get("title") or "",
            "artist_name": track.get("artist") or "",
        }
        duration = track.get("duration_s")
        if isinstance(duration, (int, float)) and duration > 0:
            params["duration"] = int(duration)
        url = _LYRICS_BASE + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": "RainetteMusic (local desktop app)"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        result = {
            "plain": data.get("plainLyrics") or "",
            "synced": data.get("syncedLyrics") or "",
            "instrumental": bool(data.get("instrumental")),
        }
        with _lyrics_cache_lock:
            _lyrics_cache[key] = result
        shared.notify_browsers({"type": "music_lyrics_result", "id": req_id, "ok": True, **result})
    except urllib.error.HTTPError as e:
        # 404 = LRCLIB has no match for this track; cache the empty result so we
        # don't re-hit the network every time the Now Playing view reopens.
        empty = {"plain": "", "synced": "", "instrumental": False, "not_found": e.code == 404}
        if e.code == 404:
            with _lyrics_cache_lock:
                _lyrics_cache[key] = empty
        shared.notify_browsers({"type": "music_lyrics_result", "id": req_id, "ok": True, **empty})
    except Exception as e:
        shared.notify_browsers({"type": "music_lyrics_result", "id": req_id, "ok": False, "msg": str(e)})


def cmd_music_remote_play(msg):
    """Relay a 'play this queue' command from the browser window to the detached
    player window. Pure fan-out — the player window owns the actual <audio>."""
    shared.notify_browsers(msg)


def cmd_music_remote_control(msg):
    """Relay a transport command (toggle/next/prev/loop/seek/volume) from the
    browser window to the detached player window."""
    shared.notify_browsers(msg)


def cmd_music_output_transfer(msg):
    """Request a single-output handoff without stopping the current device.

    The receiving device confirms it has loaded the shared queue before it
    emits its next now-playing state.  Until that confirmation the source is
    intentionally left untouched, which makes a failed phone/desktop transfer
    non-destructive.
    """
    shared.notify_browsers({
        "type": "music_output_transfer",
        "id": msg.get("id"),
        "target_device_id": str(msg.get("target_device_id") or ""),
        "source_device_id": str(msg.get("source_device_id") or ""),
        "queue": msg.get("queue") if isinstance(msg.get("queue"), list) else [],
        "index": int(msg.get("index") or 0),
        "current_time": max(0, float(msg.get("current_time") or 0)),
        "playing": bool(msg.get("playing")),
        "loop": bool(msg.get("loop")),
    })


def cmd_music_output_transfer_result(msg):
    """Relay a target device's load acknowledgement to the waiting source.

    Transfer requests deliberately wait for this message before pausing the
    old output.  Keeping the acknowledgement in the normal bridge fan-out
    means both an authenticated phone and the loopback desktop can act as the
    target without introducing a second response channel.
    """
    payload = {
        "type": "music_output_transfer_result",
        "id": msg.get("id"),
        "ok": bool(msg.get("ok")),
        "target_device_id": str(msg.get("target_device_id") or ""),
        "source_device_id": str(msg.get("source_device_id") or ""),
    }
    if "current_time" in msg:
        try:
            payload["current_time"] = max(0, float(msg.get("current_time") or 0))
        except (TypeError, ValueError):
            payload["current_time"] = 0
    if msg.get("msg"):
        payload["msg"] = str(msg.get("msg"))[:500]
    shared.notify_browsers(payload)


def cmd_music_request_state(msg):
    """Relay the player window's 'what's the current queue?' request to the
    browser window (which answers with a fresh music_remote_play). Covers the
    case where the player window connects after a play was already issued."""
    shared.notify_browsers(msg)


def cmd_music_open_artist(msg):
    """Relay an artist-profile request from the detached player to the main UI."""
    shared.notify_browsers(msg)


def cmd_music_theme_set(msg):
    """Relay a Settings theme change to every open window so an already-open
    player window updates live instead of waiting for its next reload."""
    shared.notify_browsers(msg)


def cmd_music_accent_set(msg):
    """Relay a Settings accent-color change to every open window, same as
    cmd_music_theme_set."""
    shared.notify_browsers(msg)


def cmd_music_eq_state(msg):
    """Relay the player window's live EQ state (on/off + band gains) to the
    Settings panel so its controls reflect reality, including changes made
    from the player window itself before Settings was ever opened."""
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
            # The four catalog searches are independent network calls - running
            # them in parallel cuts perceived search latency to the slowest one
            # instead of the sum. Failures still propagate via .result() so the
            # error path is identical to the old sequential version.
            with ThreadPoolExecutor(max_workers=4, thread_name_prefix="rainette-search") as pool:
                songs_f = pool.submit(yt.search, query, filter="songs", limit=25)
                videos_f = pool.submit(yt.search, query, filter="videos", limit=8)
                artists_f = pool.submit(yt.search, query, filter="artists", limit=10)
                albums_f = pool.submit(yt.search, query, filter="albums", limit=12)
                raw_songs = (songs_f.result() or []) + (videos_f.result() or [])
                raw_artists = artists_f.result() or []
                raw_albums = albums_f.result() or []
            seen = set()
            songs = []
            for raw in raw_songs:
                track = _ytm_track(raw)
                if not track or track["source_id"] in seen:
                    continue
                seen.add(track["source_id"])
                songs.append(track)
            artists = [a for a in (_ytm_artist(raw) for raw in raw_artists) if a]
            albums = [a for a in (_ytm_album(raw) for raw in raw_albums) if a]
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


def _track_key(track: dict[str, Any]) -> str:
    return f"{track.get('source') or 'youtube'}:{track.get('source_id') or ''}"


def _track_album_name(track: dict[str, Any]) -> str:
    metadata = track.get("metadata") if isinstance(track.get("metadata"), dict) else {}
    album = metadata.get("album") if isinstance(metadata.get("album"), dict) else {}
    return str(metadata.get("album_name") or album.get("name") or "").strip().casefold()


def _track_album_id(track: dict[str, Any]) -> str:
    metadata = track.get("metadata") if isinstance(track.get("metadata"), dict) else {}
    album = metadata.get("album") if isinstance(metadata.get("album"), dict) else {}
    return str(metadata.get("album_id") or album.get("id") or "").strip()


def _dedupe_mix(tracks: list[dict[str, Any]], *, cap: int = 30) -> list[dict[str, Any]]:
    seen = set()
    out = []
    for raw in tracks:
        if not isinstance(raw, dict):
            continue
        track = _public_track_from_msg(raw)
        key = _track_key(track)
        if key.endswith(":") or key in seen:
            continue
        seen.add(key)
        out.append(track)
        if len(out) >= cap:
            break
    return out


def _catalog_artist_tracks(artist_id: str = "", name: str = "", *, limit: int = 30) -> list[dict[str, Any]]:
    if YTMUSIC_AVAILABLE and artist_id:
        yt = _ytmusic()
        info = yt.get_artist(artist_id) or {}
        return _section_tracks(yt, info.get("songs"), limit=limit)
    query = name or artist_id
    if query and YTDLP_AVAILABLE:
        return _yt_dlp_search_items(query, limit=limit)
    return []


def _catalog_album_tracks(album_id: str = "", title: str = "", artist: str = "", *, limit: int = 30) -> list[dict[str, Any]]:
    if YTMUSIC_AVAILABLE and album_id:
        yt = _ytmusic()
        info = yt.get_album(album_id) or {}
        album_hint = {"name": title or info.get("title") or "", "id": album_id, "thumbnails": info.get("thumbnails") or []}
        return [t for t in (_ytm_track(raw, album_hint) for raw in (info.get("tracks") or [])) if t][:limit]
    query = " ".join(part for part in [artist, title] if part).strip()
    if query and YTDLP_AVAILABLE:
        return _yt_dlp_search_items(query, limit=limit)
    return []


def _mix_from_seed(seed: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    kind = str(seed.get("kind") or "track")
    local = shared.STATE.list_music_tracks(limit=1000)
    candidates: list[dict[str, Any]] = []
    status = "Built from local library"

    if kind == "track":
        track = seed.get("track") if isinstance(seed.get("track"), dict) else seed
        seed_track = _public_track_from_msg(track)
        album_id = _track_album_id(seed_track)
        album_name = _track_album_name(seed_track)
        artist = str(seed_track.get("artist") or "").strip()
        if seed_track.get("source_id"):
            candidates.append(seed_track)
        if album_id or album_name:
            candidates.extend([
                t for t in local
                if (album_id and _track_album_id(t) == album_id) or (album_name and _track_album_name(t) == album_name)
            ])
        if artist:
            candidates.extend([t for t in local if str(t.get("artist") or "").casefold() == artist.casefold()])
            if len(_dedupe_mix(candidates, cap=30)) < 12:
                candidates.extend(_catalog_artist_tracks(name=artist, limit=30))
                status = "Built from local library and artist catalog"

    elif kind == "album":
        album = seed.get("album") if isinstance(seed.get("album"), dict) else seed
        album_id = str(album.get("id") or album.get("browse_id") or "").strip()
        title = str(album.get("title") or album.get("name") or "").strip()
        artist = str(album.get("artist") or "").strip()
        title_key = title.casefold()
        candidates.extend([
            t for t in local
            if (album_id and _track_album_id(t) == album_id) or (title_key and _track_album_name(t) == title_key)
        ])
        if len(_dedupe_mix(candidates, cap=30)) < 8:
            candidates.extend(_catalog_album_tracks(album_id, title, artist, limit=30))
            status = "Built from album catalog"
        if artist and len(_dedupe_mix(candidates, cap=30)) < 12:
            candidates.extend([t for t in local if str(t.get("artist") or "").casefold() == artist.casefold()])

    else:
        artist_seed = seed.get("artist") if isinstance(seed.get("artist"), dict) else seed
        artist_id = str(artist_seed.get("id") or artist_seed.get("artist_id") or "").strip()
        name = str(artist_seed.get("name") or artist_seed.get("artist") or "").strip()
        if name:
            candidates.extend([t for t in local if str(t.get("artist") or "").casefold() == name.casefold()])
        if len(_dedupe_mix(candidates, cap=30)) < 12:
            candidates.extend(_catalog_artist_tracks(artist_id, name, limit=30))
            status = "Built from artist catalog"

    return _dedupe_mix(candidates, cap=30), status


def cmd_music_mix_from_seed(msg):
    req_id = msg.get("id")
    seed = msg.get("seed") if isinstance(msg.get("seed"), dict) else {}
    _run_bg(_mix_worker, req_id, seed)


def _mix_worker(req_id, seed):
    try:
        tracks, status = _mix_from_seed(seed)
        shared.notify_browsers({
            "type": "music_mix_from_seed_result",
            "id": req_id,
            "ok": True,
            "tracks": tracks,
            "status": status,
            "count": len(tracks),
        })
    except Exception as exc:
        shared.notify_browsers({"type": "music_mix_from_seed_result", "id": req_id, "ok": False, "tracks": [], "msg": str(exc)})


def cmd_music_library_index(msg):
    req_id = msg.get("id")
    try:
        limit = max(1, min(int(msg.get("limit", 500) or 500), 1000))
    except Exception:
        limit = 500
    try:
        shared.notify_browsers({"type": "music_library_index_result", "id": req_id, "ok": True, **shared.STATE.music_library_index(limit=limit)})
    except Exception as exc:
        shared.notify_browsers({"type": "music_library_index_result", "id": req_id, "ok": False, "msg": str(exc), "tracks": [], "artists": [], "albums": [], "followed_artists": []})


def cmd_music_artist_follow(msg):
    req_id = msg.get("id")
    try:
        artist = shared.STATE.follow_artist(
            artist_id=str(msg.get("artist_id") or ""),
            name=str(msg.get("name") or ""),
            thumbnail_url=str(msg.get("thumbnail_url") or ""),
        )
        shared.notify_browsers({
            "type": "music_artist_followed", "id": req_id, "ok": True,
            "artist": artist, "followed_artists": shared.STATE.list_followed_artists(),
        })
    except Exception as exc:
        shared.notify_browsers({"type": "music_artist_followed", "id": req_id, "ok": False, "msg": str(exc), "followed_artists": []})


def cmd_music_artist_unfollow(msg):
    req_id = msg.get("id")
    try:
        removed = shared.STATE.unfollow_artist(
            artist_id=str(msg.get("artist_id") or ""),
            name=str(msg.get("name") or ""),
        )
        shared.notify_browsers({
            "type": "music_artist_unfollowed", "id": req_id, "ok": True,
            "removed": removed, "followed_artists": shared.STATE.list_followed_artists(),
        })
    except Exception as exc:
        shared.notify_browsers({"type": "music_artist_unfollowed", "id": req_id, "ok": False, "msg": str(exc), "followed_artists": []})


def cmd_music_followed_artists(msg):
    req_id = msg.get("id")
    try:
        shared.notify_browsers({
            "type": "music_followed_artists_result", "id": req_id, "ok": True,
            "followed_artists": shared.STATE.list_followed_artists(),
        })
    except Exception as exc:
        shared.notify_browsers({"type": "music_followed_artists_result", "id": req_id, "ok": False, "msg": str(exc), "followed_artists": []})


def cmd_music_insights(msg):
    """Aggregate local play history into the Insights payload (all local SQLite,
    no network). Synchronous like the other CRUD-style handlers."""
    req_id = msg.get("id")
    try:
        days = int(msg.get("days", 7) or 0)
    except Exception:
        days = 7
    try:
        shared.notify_browsers({"type": "music_insights_result", "id": req_id, "ok": True,
                                **shared.STATE.listening_insights(days=days)})
    except Exception as exc:
        shared.notify_browsers({"type": "music_insights_result", "id": req_id, "ok": False, "msg": str(exc)})


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
    "music_playlist_update_meta":   cmd_music_playlist_update_meta,
    "music_playlist_folder_create": cmd_music_playlist_folder_create,
    "music_playlist_folder_rename": cmd_music_playlist_folder_rename,
    "music_playlist_folder_delete": cmd_music_playlist_folder_delete,
    "music_playlist_folder_move":   cmd_music_playlist_folder_move,
    "music_smart_playlist_create":  cmd_music_smart_playlist_create,
    "music_smart_playlist_update":  cmd_music_smart_playlist_update,
    "music_smart_playlist_delete":  cmd_music_smart_playlist_delete,
    "music_smart_playlist_tracks":  cmd_music_smart_playlist_tracks,
    "music_playlist_add_track":     cmd_music_playlist_add_track,
    "music_playlist_remove_track":  cmd_music_playlist_remove_track,
    "music_playlist_tracks":        cmd_music_playlist_tracks,
    "music_recent":                 cmd_music_recent,
    "music_recent_delete":          cmd_music_recent_delete,
    "music_clear_data":             cmd_music_clear_data,
    "music_top_artists":            cmd_music_top_artists,
    "music_queue_session_save":     cmd_music_queue_session_save,
    "music_queue_session_list":     cmd_music_queue_session_list,
    "music_queue_session_delete":   cmd_music_queue_session_delete,
    "music_now_playing_set":        cmd_music_now_playing_set,
    "music_progress":               cmd_music_progress,
    "music_lyrics":                 cmd_music_lyrics,
    "music_remote_play":            cmd_music_remote_play,
    "music_remote_control":         cmd_music_remote_control,
    "music_output_transfer":        cmd_music_output_transfer,
    "music_output_transfer_result": cmd_music_output_transfer_result,
    "music_request_state":          cmd_music_request_state,
    "music_open_artist":            cmd_music_open_artist,
    "music_theme_set":              cmd_music_theme_set,
    "music_accent_set":             cmd_music_accent_set,
    "music_eq_state":               cmd_music_eq_state,
    "music_catalog_search":         cmd_music_catalog_search,
    "music_artist_catalog":         cmd_music_artist_catalog,
    "music_album_tracks":           cmd_music_album_tracks,
    "music_mix_from_seed":          cmd_music_mix_from_seed,
    "music_library_index":          cmd_music_library_index,
    "music_artist_follow":          cmd_music_artist_follow,
    "music_artist_unfollow":        cmd_music_artist_unfollow,
    "music_followed_artists":       cmd_music_followed_artists,
    "music_insights":               cmd_music_insights,
    "music_status":                 cmd_music_status,
}
