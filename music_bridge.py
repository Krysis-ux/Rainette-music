"""Music player command handlers.

Playlist/track CRUD are thin sync wrappers over the SQLite state. Search and stream-URL
resolution hit yt-dlp, which blocks on network I/O, so those run on a daemon
thread and broadcast their result via the thread-safe ``shared.notify_browsers``.
No audio bytes ever flow through the server — the browser's <audio> element
consumes the resolved URL directly.
"""

from __future__ import annotations

import json
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import audio_outputs
import local_library
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

# A cache entry with less than this left is not worth handing out. Serving one
# used to be the whole bug: the reported hint was the entry's *remaining* life,
# the relay grant was derived from that hint, and a nearly-stale entry therefore
# minted a 60-second grant that 404'd a minute into the track. Re-resolving a
# little early costs one yt-dlp call and removes the entire failure mode.
STREAM_URL_MIN_REMAINING_S = 900

_stream_cache_lock = threading.Lock()
_stream_url_cache: dict[str, dict[str, Any]] = {}

# Per-source resolution locks. A dead upstream URL fails every in-flight Range
# request at once, so without these one stale track spawns a yt-dlp process per
# request instead of one per track.
_resolve_locks_guard = threading.Lock()
_resolve_locks: dict[str, threading.Lock] = {}

_SEARCH_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "extract_flat": True,   # metadata only, no per-result stream resolution
    "skip_download": True,
    "default_search": "ytsearch",
    "compat_opts": _SYSTEM_TRUST_COMPAT,
}

# The player client decides what kind of URL comes back. yt-dlp's default
# (ANDROID_VR) serves chunked URLs that refuse the open-ended `Range: bytes=0-`
# a media element opens with, which surfaces only as "Format error". ANDROID
# returns an ordinary progressive URL. Check this first if every track fails.
_PLAYER_CLIENT_ARGS = {"youtube": {"player_client": ["android"]}}

_STREAM_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "skip_download": True,
    # Prefer containers HTML5 <audio> plays over a bare bestaudio. The middle
    # rungs matter on macOS: WebKit cannot decode Opus in WebM at all, so a
    # source with no m4a would fall to bestaudio and fail silently.
    "format": (
        "bestaudio[ext=m4a]"
        "/bestaudio[acodec^=mp4a]"
        "/bestaudio[ext=mp4]"
        "/bestaudio/best"
    ),
    "extractor_args": _PLAYER_CLIENT_ARGS,
    "compat_opts": _SYSTEM_TRUST_COMPAT,
}


def _run_bg(target, *args):
    threading.Thread(target=target, args=args, name="rainette-music", daemon=True).start()


def _parse_upstream_expiry(url: str) -> float | None:
    """Read googlevideo's own ``expire`` stamp off a stream URL.

    The CDN tells us exactly when it will stop serving a URL, which beats any
    TTL we invent. Anything already past, or implausibly far out, is treated as
    unparseable rather than trusted.
    """
    try:
        raw = urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("expire", [""])[0]
        value = float(raw)
    except (TypeError, ValueError, AttributeError):
        return None
    now = time.time()
    return value if now < value < now + 86_400 else None


def _stream_cache_get(source_id: str, *, min_remaining_s: float = 0.0) -> dict[str, Any] | None:
    now = time.time()
    with _stream_cache_lock:
        cached = _stream_url_cache.get(source_id)
        if not cached:
            return None
        # An entry too close to expiry is dropped rather than served: whoever
        # receives it would build a short-lived grant around it and fail
        # mid-track.
        if float(cached.get("expires_at") or 0) <= now + min_remaining_s:
            _stream_url_cache.pop(source_id, None)
            return None
        return dict(cached)


def _stream_cache_set(source_id: str, *, url: str, title: str = "", artist: str = "",
                      duration_s=None, thumbnail_url: str = "",
                      http_headers: dict[str, str] | None = None) -> None:
    # The CDN's own deadline wins whenever it is sooner than ours; caching past
    # the point the URL stops working is how a "valid" entry serves a dead link.
    upstream_expiry = _parse_upstream_expiry(url)
    expires_at = time.time() + STREAM_URL_CACHE_TTL_S
    if upstream_expiry is not None:
        expires_at = min(expires_at, upstream_expiry)
    with _stream_cache_lock:
        _stream_url_cache[source_id] = {
            "url": url,
            # googlevideo binds a URL to the client that minted it, so the relay
            # has to repeat yt-dlp's headers rather than wear the phone's.
            "http_headers": dict(http_headers or {}),
            "title": title,
            "artist": artist,
            "duration_s": duration_s,
            "thumbnail_url": thumbnail_url,
            "expires_at": expires_at,
        }


def _stream_cache_invalidate(source_id: str) -> None:
    with _stream_cache_lock:
        _stream_url_cache.pop(source_id, None)


def _resolve_lock_for(cache_key: str) -> threading.Lock:
    """One lock per source, so concurrent redemptions collapse into one resolve."""
    with _resolve_locks_guard:
        lock = _resolve_locks.get(cache_key)
        if lock is None:
            lock = threading.Lock()
            _resolve_locks[cache_key] = lock
        return lock


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
        # YouTube Music reports this as a display string ("1.4M"), and only for
        # some result kinds. Passed through as-is so a client can sort by
        # popularity where it exists and fall back to relevance where it does not.
        "view_count": item.get("views") or item.get("view_count") or "",
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
        # "Album" / "EP" / "Single" as YouTube Music classifies the release. The
        # artist catalog returns EPs inside its album section, so without this a
        # client has no way to shelve them separately.
        "release_type": str(item.get("type") or "").strip(),
        "browse_id": str(album_id or ""),
        "params": item.get("params") or "",
    }


# ── Stream URL resolution (only when the user actually presses play) ──────────

def _local_track_for(track_id: str, source_id: str) -> dict[str, Any] | None:
    """The local row a stream request names, if it names one.

    Tried by primary key first because that is what a phone sends back for a
    track it already has; the opaque ``source_id`` is the fallback for a request
    built from a search result rather than from the library.
    """
    try:
        return shared.STATE.get_local_track(track_id=track_id, source_id=source_id)
    except Exception:
        return None


def cmd_music_stream_url(msg):
    req_id = msg.get("id")
    source_id = str(msg.get("source_id") or "").strip()
    track_id = str(msg.get("track_id") or "").strip()
    track_payload = msg.get("track") if isinstance(msg.get("track"), dict) else None
    prefetch = bool(msg.get("prefetch"))
    force_refresh = bool(msg.get("force_refresh") or msg.get("invalidate_cache"))
    cache_key = source_id

    # A file on this computer never touches yt-dlp, and never expires. This
    # branch is deliberately ahead of the YTDLP_AVAILABLE guard below: a
    # computer with no extractor installed can still play its own music.
    local_row = _local_track_for(track_id, source_id)
    if local_row is not None:
        if not prefetch:
            try:
                shared.STATE.log_play(str(local_row.get("id") or ""))
            except Exception:
                pass
        shared.notify_browsers({
            "type": "music_stream_url_result", "id": req_id, "ok": True,
            "track_id": str(local_row.get("id") or ""),
            "source_id": str(local_row.get("source_id") or ""),
            # No `expires_hint_s`: there is nothing to expire, and inventing a
            # number here is what used to collapse the relay grant behind it.
            #
            # The url is the desktop's own route to the file. A phone never sees
            # it: the companion gateway keys off `local` and overwrites this with
            # a grant it mints itself, whatever is here. It used to be the empty
            # string, which was correct for the phone and left the desktop with
            # nothing to play -- every local and downloaded track failed there
            # while working perfectly over the relay.
            "local": True, "cached": False,
            "url": ("/local/" + str(local_row.get("id") or "")) if local_row.get("id") else "",
            "content_type": str(local_row.get("content_type") or ""),
            "missing": bool(str(local_row.get("missing_since") or "")),
            "title": str(local_row.get("title") or ""),
            "artist": str(local_row.get("artist") or ""),
            "duration_s": local_row.get("duration_s"),
            "thumbnail_url": str(local_row.get("thumbnail_url") or ""),
        })
        return

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
        _stream_cache_invalidate(cache_key)
    elif cached := _stream_cache_get(cache_key, min_remaining_s=STREAM_URL_MIN_REMAINING_S):
        if track_id and not prefetch:
            try:
                shared.STATE.log_play(track_id)
            except Exception:
                pass
        # Whatever is left really is left: _stream_cache_get has already dropped
        # anything under STREAM_URL_MIN_REMAINING_S, so this can no longer be the
        # handful of seconds that used to collapse the relay grant behind it.
        ttl = max(STREAM_URL_MIN_REMAINING_S, int(float(cached.get("expires_at") or 0) - time.time()))
        shared.notify_browsers({
            "type": "music_stream_url_result", "id": req_id, "ok": True,
            "track_id": track_id, "source_id": source_id, "url": cached.get("url", ""),
            "expires_hint_s": min(ttl, STREAM_URL_CACHE_TTL_S), "cached": True,
            "title": cached.get("title") or "", "artist": cached.get("artist") or "",
            "duration_s": cached.get("duration_s"), "thumbnail_url": cached.get("thumbnail_url") or "",
        })
        return
    _run_bg(_stream_worker, req_id, source_id, track_id, not prefetch)


def _extract_stream(source_id: str) -> dict[str, Any]:
    """Run yt-dlp for one source and normalise what comes back.

    Split out of the worker so the audio relay can reach the same resolution
    path when a cached URL has gone stale mid-track, instead of a second
    implementation drifting away from this one.
    """
    url = source_id if source_id.startswith("http") else f"https://www.youtube.com/watch?v={source_id}"
    with yt_dlp.YoutubeDL(_STREAM_OPTS) as ydl:
        info = ydl.extract_info(url, download=False)
    stream_url = info.get("url")
    headers = dict(info.get("http_headers") or {})
    if not stream_url:
        # Some extractors nest the playable URL under requested formats.  The
        # headers travel with the format, not with the video, so they are taken
        # from whichever entry actually supplied the URL.
        for fmt in reversed(info.get("requested_formats") or info.get("formats") or []):
            if fmt.get("url"):
                stream_url = fmt["url"]
                headers = dict(fmt.get("http_headers") or headers)
                break
    if not stream_url:
        raise RuntimeError("no playable stream url returned")
    return {
        "url": stream_url,
        "http_headers": headers,
        "title": info.get("title") or "",
        "artist": info.get("uploader") or "",
        "duration_s": info.get("duration"),
        "thumbnail_url": _pick_thumb(info),
    }


def resolve_stream_url_sync(source_id: str, *, force_refresh: bool = False) -> str:
    """Blocking, cache-aware, single-flight stream resolution.

    Called from the audio relay when an upstream URL has been invalidated
    (googlevideo binds them to the resolving IP, so a VPN toggle kills them).
    Single-flight matters here specifically: a stale URL fails on *every*
    in-flight Range request at once, and without the lock that is one yt-dlp
    process per request rather than one per track.
    """
    source_id = str(source_id or "").strip()
    if not source_id:
        raise ValueError("source_id required")
    if not YTDLP_AVAILABLE:
        raise RuntimeError("yt-dlp not installed")
    cache_key = source_id
    if force_refresh:
        _stream_cache_invalidate(cache_key)
    with _resolve_lock_for(cache_key):
        # Another caller may have resolved it while we waited for the lock.
        if cached := _stream_cache_get(cache_key, min_remaining_s=STREAM_URL_MIN_REMAINING_S):
            return str(cached.get("url") or "")
        found = _extract_stream(source_id)
        _stream_cache_set(cache_key, url=found["url"], title=found["title"],
                          artist=found["artist"], duration_s=found["duration_s"],
                          thumbnail_url=found["thumbnail_url"],
                          http_headers=found.get("http_headers"))
        return str(found["url"])


def stream_request_headers(source_id: str) -> dict[str, str]:
    """The headers a resolved stream URL must be re-requested with.

    googlevideo signs a URL for the client that asked for it and answers a
    request wearing anybody else's ``User-Agent`` with 403.  The relay therefore
    cannot forward the phone's headers upstream; it has to repeat yt-dlp's.
    Returns an empty mapping for a source that was never resolved here, which
    leaves the caller free to fall back to whatever it did before.
    """
    cached = _stream_cache_get(str(source_id or "").strip())
    return dict((cached or {}).get("http_headers") or {})


def describe_resolve_failure(exc: BaseException) -> str:
    """Turn a yt-dlp failure into something a person can act on.

    The raw text is Python TLS internals ending in "report this issue", which
    sends the user to file a bug about their own network. A Wi-Fi running TLS
    inspection kills every track the same way, and only the message can say so.
    """
    text = str(exc)
    lowered = text.lower()
    if "certificate_verify_failed" in lowered or "certificate verify failed" in lowered:
        return ("This network is blocking YouTube — its firewall is intercepting "
                "the connection, so Rainette cannot reach the audio. Try another "
                "Wi-Fi network or a phone hotspot.")
    if "unable to download" in lowered and ("timed out" in lowered or "timeout" in lowered):
        return "YouTube did not answer in time. Check this computer's internet connection."
    if "sign in to confirm" in lowered or "bot" in lowered and "confirm" in lowered:
        return "YouTube is asking this computer to prove it is not a bot. Try again shortly."
    if "video unavailable" in lowered or "private video" in lowered:
        return "That track is not available from YouTube any more."
    return text


def _stream_worker(req_id, source_id, track_id, log_play=True):
    try:
        found = _extract_stream(source_id)
        stream_url = found["url"]
        title = found["title"]
        artist = found["artist"]
        duration_s = found["duration_s"]
        thumbnail_url = found["thumbnail_url"]
        _stream_cache_set(source_id,
                          url=stream_url, title=title, artist=artist,
                          duration_s=duration_s, thumbnail_url=thumbnail_url,
                          http_headers=found.get("http_headers"))
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
        shared.notify_browsers({"type": "music_stream_url_result", "id": req_id, "ok": False,
                                "msg": describe_resolve_failure(e), "track_id": track_id})


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

    Mirrors web/repeat_mode.js. Returns an empty dict when the producer said
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
        # Why playback stopped. Dropping it left every failure reading as a
        # bare "playback failed".
        "error_reason": str(msg.get("error_reason") or ""),
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
        # Forwarded alongside loop so a three-state repeat survives the handoff;
        # loop alone cannot tell "repeat one" from "repeat all".
        "repeat": str(msg.get("repeat") or ""),
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

    # A handoff is the one moment both sides agree on who owns the audio, and
    # it is the only place in the codebase that sees a successful transfer from
    # either direction. Ownership moves here and nowhere earlier: a transfer
    # that failed must leave the source playing, which is the contract
    # cmd_music_output_transfer states in prose and nothing used to enforce.
    if payload["ok"] and payload["target_device_id"]:
        target = payload["target_device_id"]
        _publish_playback_target(
            owner_kind="desktop" if target == "desktop" else "phone",
            owner_device_id=target,
            reason="transfer_ack",
        )


def _publish_playback_target(*, owner_kind, owner_device_id, reason,
                             sink_id=None, sink_name=None, owner_name=None):
    """Record who owns playback, then tell every device.

    Deliberately fans out to all of them rather than to the session that caused
    it: a phone showing "playing on the computer" needs to stop saying that the
    moment another device takes over, and it cannot learn that from its own
    session's events.
    """
    patch = {"owner_kind": owner_kind, "owner_device_id": str(owner_device_id or "desktop"),
             "reason": reason}
    if sink_id is not None:
        patch["sink_id"] = str(sink_id)
    if sink_name is not None:
        patch["sink_name"] = str(sink_name)
    # Server-stamped, never taken from a client: this is the string every
    # surface renders as "playing on ...", so a device must not be able to
    # name itself something else.
    patch["owner_name"] = str(owner_name if owner_name is not None else _owner_display_name(owner_kind, owner_device_id))
    try:
        target = shared.STATE.set_playback_target(patch)
    except Exception:
        return None
    shared.notify_browsers({"type": "music_playback_target", "ok": True, **target})
    return target


def _owner_display_name(owner_kind, owner_device_id):
    if owner_kind == "desktop":
        try:
            return socket.gethostname()
        except Exception:
            return "this computer"
    try:
        for device in shared.STATE.list_devices():
            if device.get("device_id") == owner_device_id:
                return device.get("name") or "a phone"
    except Exception:
        pass
    return "a phone"


def cmd_music_playback_target_get(msg):
    """Who owns playback right now."""
    try:
        target = shared.STATE.get_playback_target()
    except Exception:
        target = {"owner_kind": "desktop", "owner_device_id": "desktop", "revision": 0}
    shared.notify_browsers({"type": "music_playback_target_result", "id": msg.get("id"),
                            "ok": True, **target})


def cmd_music_playback_target_set(msg):
    """Claim playback for the caller.

    The owner is derived, never read from the body: a phone can only ever claim
    ownership for itself, and the desktop only for itself. Without that a
    device could hand playback to a third party it has no business moving.
    """
    origin = str(msg.get("origin_device_id") or "")
    kind = str(msg.get("owner_kind") or "").strip()
    if kind not in ("desktop", "phone"):
        kind = "phone" if origin else "desktop"
    owner_device_id = origin if (kind == "phone" and origin) else "desktop"
    target = _publish_playback_target(
        owner_kind=kind,
        owner_device_id=owner_device_id,
        reason=str(msg.get("reason") or "claim_by_play"),
        sink_id=msg.get("sink_id"),
        sink_name=msg.get("sink_name"),
    )
    shared.notify_browsers({"type": "music_playback_target_result", "id": msg.get("id"),
                            "ok": bool(target), **(target or {})})


def cmd_music_device_settings_get(msg):
    """This phone's settings, as this computer last saw them."""
    device_id = str(msg.get("origin_device_id") or "")
    entries = []
    if device_id:
        try:
            entries = shared.STATE.read_device_settings(device_id)
        except Exception:
            entries = []
    shared.notify_browsers({
        "type": "music_device_settings_result", "id": msg.get("id"), "ok": True,
        "device_id": device_id, "server_ms": int(time.time() * 1000), "entries": entries,
    })


def cmd_music_device_settings_put(msg):
    """Merge a phone's settings, per key rather than per blob.

    Prefs travel as one object, so a whole-blob revision would force discarding
    one side whenever two devices touched different keys. Per-key stamps make
    the merge commutative and both edits survive.
    """
    device_id = str(msg.get("origin_device_id") or "")
    entries = msg.get("entries") if isinstance(msg.get("entries"), list) else []
    merged = []
    if device_id:
        try:
            merged = shared.STATE.merge_device_settings(device_id, entries)
        except Exception:
            merged = []
    shared.notify_browsers({
        "type": "music_device_settings_result", "id": msg.get("id"), "ok": bool(device_id),
        "device_id": device_id, "server_ms": int(time.time() * 1000), "entries": merged,
    })


def cmd_music_output_sink_result(msg):
    """Relay the player window's answer to a set_sink request.

    Whether audio could be re-routed is only knowable inside the window that
    owns the <audio> element, and the window that asked is a different one, so
    the answer needs its own fan-out message rather than riding on the
    relayed request (whose echo would arrive first and mean nothing).
    """
    shared.notify_browsers({
        "type": "music_output_sink_result",
        "id": msg.get("id"),
        "routed": bool(msg.get("routed")),
        "sink_id": str(msg.get("sink_id") or ""),
    })


def cmd_music_output_devices(msg):
    """List the audio outputs this computer can play through.

    Shelling out to the OS takes long enough to stutter a picker's opening
    animation, so the probe runs on a daemon thread and the answer arrives as a
    normal id-correlated result. Callers render the system default immediately
    and fill the rest in when this lands.
    """
    req_id = msg.get("id")

    def worker():
        try:
            devices = audio_outputs.list_outputs()
        except Exception as exc:  # pragma: no cover - probe is already defensive
            shared.notify_browsers({
                "type": "music_output_devices_result", "id": req_id,
                "ok": False, "devices": [], "msg": str(exc),
            })
            return
        shared.notify_browsers({
            "type": "music_output_devices_result", "id": req_id,
            "ok": True, "devices": devices,
        })

    _run_bg(worker)


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


def cmd_music_artist_images(msg):
    """Resolve artist artwork for a batch of names in one round trip.

    A phone building an artist list out of its own library has names and nothing
    else — the tracks carry cover art, which is the album's, not the artist's.
    Asking per artist would be one search per row; asking here is one command for
    the whole screen, and the client caches what comes back.
    """
    req_id = msg.get("id")
    raw = msg.get("names")
    names = []
    seen = set()
    for value in raw if isinstance(raw, list) else []:
        name = str(value or "").strip()
        key = name.casefold()
        if not name or key in seen:
            continue
        seen.add(key)
        names.append(name)
        if len(names) >= 40:   # one screen's worth; the client pages the rest
            break
    if not names:
        shared.notify_browsers({"type": "music_artist_images_result", "id": req_id, "ok": True, "artists": []})
        return
    _run_bg(_artist_images_worker, req_id, names)


def _artist_images_worker(req_id, names):
    try:
        if not YTMUSIC_AVAILABLE:
            shared.notify_browsers({
                "type": "music_artist_images_result", "id": req_id, "ok": True, "artists": [],
                "msg": "Install ytmusicapi for artist artwork.",
            })
            return
        yt = _ytmusic()

        def lookup(name):
            try:
                matches = yt.search(name, filter="artists", limit=1) or []
            except Exception:
                return None
            artist = _ytm_artist(matches[0]) if matches else None
            if not artist:
                return None
            # The query is echoed back because the client keys its cache on what
            # it asked for, which is rarely spelled the way the catalog spells it.
            return {**artist, "query": name}

        with ThreadPoolExecutor(max_workers=6, thread_name_prefix="rainette-artist-art") as pool:
            found = [a for a in pool.map(lookup, names) if a]
        shared.notify_browsers({
            "type": "music_artist_images_result", "id": req_id, "ok": True, "artists": found,
        })
    except Exception as exc:
        shared.notify_browsers({
            "type": "music_artist_images_result", "id": req_id, "ok": False, "msg": str(exc), "artists": [],
        })


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
    # The requesting device decides which local tracks get marked unplayable;
    # the result carries whose capabilities it used, so a phone receiving
    # another device's fan-out can see the marks are not about it.
    # Passed only when there is one, so a request with no device behind it calls
    # the same signature it always did.
    device_id = str(msg.get("origin_device_id") or "").strip()
    extra = {"device_id": device_id} if device_id else {}
    try:
        shared.notify_browsers({"type": "music_library_index_result", "id": req_id, "ok": True,
                                **shared.STATE.music_library_index(limit=limit, **extra)})
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
                            "ytmusic_available": YTMUSIC_AVAILABLE, "ytmusic_error": _ytmusic_error,
                            "local_library_available": True,
                            "mutagen_available": local_library.MUTAGEN_AVAILABLE})


# ── Local files on this computer ──────────────────────────────────────────────

def _local_status_payload() -> dict[str, Any]:
    return local_library.status(shared.STATE)


def cmd_music_local_roots(msg):
    """List, add, or forget a watched folder.

    ``add`` and ``remove`` are refused when the message carries an
    ``origin_device_id``, which the gateway stamps on every command arriving
    from a phone (``server.py`` overwrites any value the phone supplied, so its
    absence cannot be forged). Choosing a folder is a decision made at the
    computer through a native picker; a phone that could name a path could name
    ``/`` and turn the library into a directory listing of somebody's home.
    """
    req_id = msg.get("id")
    action = str(msg.get("action") or "list").strip().lower()
    from_phone = bool(str(msg.get("origin_device_id") or "").strip())
    try:
        if action in {"add", "remove"} and from_phone:
            raise PermissionError("choose music folders on the computer itself")
        if action == "add":
            shared.STATE.add_local_root(str(msg.get("path") or ""))
        elif action == "remove":
            shared.STATE.remove_local_root(str(msg.get("path") or ""))
        elif action != "list":
            raise ValueError("unknown action")
        shared.notify_browsers({"type": "music_local_roots_result", "id": req_id, "ok": True,
                                "action": action, **_local_status_payload()})
    except Exception as exc:
        shared.notify_browsers({"type": "music_local_roots_result", "id": req_id, "ok": False,
                                "action": action, "msg": str(exc), "roots": [],
                                "tracks": 0, "missing": 0, "bytes": 0})


# ── Downloading a track onto this computer ────────────────────────────────
#
# The phone has its own download path (``pwa/src/downloads.js``) and keeps what
# it fetches in its own storage. This is the computer's, and it lands the file
# in a real folder so the existing library machinery owns it from there: the
# folder is registered as a scan root once, and after each download that one
# root is rescanned. Nothing here has to teach the library what a track is.
#
# Format is a passthrough, for the same reason it is on the phone: the format
# ladder above asks YouTube for M4A, converting to MP3 would cost a second
# generation of lossy damage plus an ffmpeg dependency this app does not have,
# and ``local_library.AUDIO_SUFFIXES`` already counts ``.m4a`` as music.
#
# Tags, however, are written. A YouTube M4A frequently carries no artist, album
# or cover at all, and a scan of untagged files produces a folder of filenames.
# The catalog row knows the real answers, so they are written into the file --
# which also means the track keeps them if it is ever copied somewhere else.

DOWNLOADS_FOLDER_NAME = "Rainette Downloads"

# A ceiling on one track, so a wedged upstream cannot hold the worker forever.
_DOWNLOAD_TIMEOUT_S = 180
_DOWNLOAD_CHUNK = 1 << 16

# Only one download runs at a time. Each costs a yt-dlp resolve and a stream,
# and a "download all" on a long playlist would otherwise open thirty of both.
_download_lock = threading.Lock()


def downloads_dir() -> Path:
    """Where downloaded tracks land, created on demand.

    Under the user's own Music folder when there is one, because that is where
    a person looks for music and where other players already index. Falls back
    to the app data directory when there is not -- a download that lands
    somewhere odd is better than one that fails.
    """
    music = Path.home() / "Music"
    if music.is_dir():
        base: Path = music
    else:
        # Imported here rather than at module scope: server imports this module
        # while it is still loading, so a top-level import would be circular.
        import server

        base = server.APP_DATA_DIR
    target = base / DOWNLOADS_FOLDER_NAME
    target.mkdir(parents=True, exist_ok=True)
    return target


def _safe_stem(artist: str, title: str) -> str:
    """A file name every platform this runs on will accept.

    Windows is the strictest and is what the character class is drawn from; the
    length cap is there because a very long name is refused outright on some
    filesystems even when every character in it is legal.
    """
    stem = f"{artist} - {title}" if artist else (title or "track")
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", stem)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned[:120] or "track"


def _write_tags(path: Path, track: dict[str, Any], artwork: bytes | None) -> None:
    """Write catalog metadata into the downloaded file.

    Never raises: a file that plays with poor tags beats a download reported as
    failed because a tag block would not take. Mirrors ``local_library.read_tags``
    in being categorically forgiving.
    """
    if not local_library.MUTAGEN_AVAILABLE:
        return
    try:
        from mutagen.mp4 import MP4, MP4Cover  # type: ignore

        audio = MP4(str(path))
        if track.get("title"):
            audio["\xa9nam"] = [str(track["title"])]
        artist = str(track.get("artist") or track.get("uploader") or "")
        if artist:
            audio["\xa9ART"] = [artist]
        album = str(track.get("album") or (track.get("metadata") or {}).get("album_name") or "")
        if album:
            audio["\xa9alb"] = [album]
        if artwork:
            fmt = MP4Cover.FORMAT_PNG if artwork[:8] == b"\x89PNG\r\n\x1a\n" else MP4Cover.FORMAT_JPEG
            audio["covr"] = [MP4Cover(artwork, imageformat=fmt)]
        audio.save()
    except Exception:
        return


def _fetch_artwork(url: str) -> bytes | None:
    if not url or not url.startswith(("http://", "https://")):
        return None
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "RainetteMusic"})
        with urllib.request.urlopen(request, timeout=20) as response:
            data = response.read(4 << 20)
        return data or None
    except Exception:
        return None


def _download_one(track: dict[str, Any], folder: Path, on_bytes) -> Path:
    """Fetch one track into ``folder`` and return where it landed.

    Written to a ``.part`` file and renamed only once the body is complete, so
    an interrupted download never leaves something the next scan would index as
    a track. Raises on failure; the caller counts it and moves on.
    """
    source_id = str(track.get("source_id") or track.get("id") or "")
    if not source_id:
        raise ValueError("that track has no source")

    url = resolve_stream_url_sync(source_id)
    if not url:
        raise RuntimeError("no audio stream came back for that track")

    headers = dict(stream_request_headers(source_id) or {})
    headers.setdefault("User-Agent", "RainetteMusic")
    request = urllib.request.Request(url, headers=headers)

    stem = _safe_stem(str(track.get("artist") or ""), str(track.get("title") or ""))
    with urllib.request.urlopen(request, timeout=_DOWNLOAD_TIMEOUT_S) as response:
        content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        suffix = {
            "audio/mp4": ".m4a", "audio/x-m4a": ".m4a", "audio/aac": ".aac",
            "audio/mpeg": ".mp3", "audio/webm": ".weba", "audio/ogg": ".ogg",
            "audio/flac": ".flac", "audio/wav": ".wav",
        }.get(content_type, ".m4a")
        total = int(response.headers.get("Content-Length") or 0)

        final = folder / (stem + suffix)
        partial = folder / (stem + suffix + ".part")
        received = 0
        with open(partial, "wb") as handle:
            while True:
                chunk = response.read(_DOWNLOAD_CHUNK)
                if not chunk:
                    break
                handle.write(chunk)
                received += len(chunk)
                on_bytes(received, total)

    if not received:
        partial.unlink(missing_ok=True)
        raise RuntimeError("that download arrived empty")

    _write_tags(partial, track, _fetch_artwork(str(track.get("thumbnail_url") or "")))
    partial.replace(final)
    return final


def cmd_music_download_track(msg):
    """Download one track, or a list of them, into the downloads folder.

    Refused outright when the message carries an ``origin_device_id``. That
    stamp is the gateway's mark of a command from a phone, and a phone has no
    business filling this computer's disk -- it has its own download path and
    its own storage. Same discipline as ``cmd_music_local_roots``, for the same
    reason: writing to this machine is a decision made at this machine.
    """
    req_id = msg.get("id")
    if str(msg.get("origin_device_id") or "").strip():
        shared.notify_browsers({"type": "music_download_result", "id": req_id, "ok": False,
                                "msg": "downloads are saved on the computer itself",
                                "done": 0, "failed": 0, "total": 0})
        return
    raw = msg.get("tracks")
    tracks = [t for t in raw if isinstance(t, dict)] if isinstance(raw, (list, tuple)) else []
    single = msg.get("track")
    if not tracks and isinstance(single, dict):
        tracks = [single]
    _run_bg(_download_worker, req_id, tracks)


def _download_worker(req_id, tracks):
    if not tracks:
        shared.notify_browsers({"type": "music_download_result", "id": req_id, "ok": False,
                                "msg": "nothing to download", "done": 0, "failed": 0, "total": 0})
        return
    if not _download_lock.acquire(blocking=False):
        shared.notify_browsers({"type": "music_download_result", "id": req_id, "ok": False,
                                "busy": True, "msg": "a download is already running",
                                "done": 0, "failed": 0, "total": len(tracks)})
        return

    total = len(tracks)
    done = 0
    failed = 0
    first_error = ""
    try:
        folder = downloads_dir()
        # Registered once, before anything is written, so even a run that fails
        # every track leaves the folder watched rather than orphaned.
        try:
            shared.STATE.add_local_root(str(folder))
        except Exception:
            pass

        for index, track in enumerate(tracks):
            title = str(track.get("title") or "track")

            def progress(received, size, _i=index, _t=title):
                shared.notify_browsers({
                    "type": "music_download_progress", "id": req_id,
                    "index": _i, "total": total, "title": _t,
                    "received": received, "size": size,
                    "ratio": (received / size) if size else 0.0,
                })

            try:
                _download_one(track, folder, progress)
                done += 1
            except Exception as exc:
                failed += 1
                if not first_error:
                    first_error = describe_resolve_failure(exc)

        # One scan of one root, after the whole run rather than per track: the
        # walk costs the same either way and thirty of them would not.
        try:
            local_library.scan(shared.STATE, [str(folder)])
        except Exception:
            pass

        shared.notify_browsers({
            "type": "music_download_result", "id": req_id, "ok": done > 0,
            "done": done, "failed": failed, "total": total,
            "folder": str(folder), "msg": first_error if done == 0 else "",
            **_local_status_payload(),
        })
    except Exception as exc:
        shared.notify_browsers({"type": "music_download_result", "id": req_id, "ok": False,
                                "msg": str(exc), "done": done, "failed": failed, "total": total})
    finally:
        _download_lock.release()


def cmd_music_local_status(msg):
    req_id = msg.get("id")
    try:
        shared.notify_browsers({"type": "music_local_status_result", "id": req_id, "ok": True,
                                **_local_status_payload()})
    except Exception as exc:
        shared.notify_browsers({"type": "music_local_status_result", "id": req_id, "ok": False,
                                "msg": str(exc), "roots": [], "tracks": 0, "missing": 0, "bytes": 0})


# One scan at a time. Two concurrent walks of the same folder do the same work
# twice and interleave their progress into nonsense; the reconcile step is
# self-correcting either way, so this is about not wasting a disk, not about
# correctness.
_local_scan_lock = threading.Lock()


def cmd_music_local_scan(msg):
    """Walk the watched folders on a background thread.

    Only already-registered roots are ever walked — ``local_library.scan``
    drops anything else — so a phone may re-run a scan without being able to
    say where.
    """
    req_id = msg.get("id")
    raw = msg.get("roots")
    roots = [str(path or "").strip() for path in raw if str(path or "").strip()] \
        if isinstance(raw, (list, tuple)) else []
    _run_bg(_local_scan_worker, req_id, roots)


def _local_scan_worker(req_id, roots):
    def report(progress: dict[str, Any]) -> None:
        shared.notify_browsers({"type": "music_local_scan_progress", "id": req_id, **progress})

    if not _local_scan_lock.acquire(blocking=False):
        shared.notify_browsers({"type": "music_local_scan_result", "id": req_id, "ok": False,
                                "busy": True, "msg": "a scan is already running",
                                **_local_status_payload()})
        return
    try:
        result = local_library.scan(shared.STATE, roots, on_progress=report)
        shared.notify_browsers({"type": "music_local_scan_result", "id": req_id, "ok": True,
                                **result, **_local_status_payload()})
    except Exception as exc:
        shared.notify_browsers({"type": "music_local_scan_result", "id": req_id, "ok": False,
                                "msg": str(exc), "roots": [], "tracks": 0, "missing": 0, "bytes": 0})
    finally:
        _local_scan_lock.release()


def cmd_music_client_capabilities(msg):
    """Record what one phone's ``canPlayType`` said it can decode.

    iOS Safari plays FLAC in an ``<audio>`` element but not Ogg or Opus. Without
    this the library offers an Opus track the phone silently fails to load, and
    the failure surfaces as "the audio stream expired" — which is not merely
    unhelpful, it points at the wrong subsystem entirely.
    """
    req_id = msg.get("id")
    device_id = str(msg.get("origin_device_id") or msg.get("device_id") or "").strip()
    raw = msg.get("can_play") if isinstance(msg.get("can_play"), dict) else {}
    try:
        stored = shared.STATE.set_device_codecs(device_id, raw)
        shared.notify_browsers({"type": "music_client_capabilities_result", "id": req_id,
                                "ok": bool(device_id), "device_id": device_id,
                                "can_play": stored,
                                "msg": "" if device_id else "no device on this request"})
    except Exception as exc:
        shared.notify_browsers({"type": "music_client_capabilities_result", "id": req_id,
                                "ok": False, "device_id": device_id, "can_play": {}, "msg": str(exc)})


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
    "music_playback_target_get":    cmd_music_playback_target_get,
    "music_playback_target_set":    cmd_music_playback_target_set,
    "music_device_settings_get":    cmd_music_device_settings_get,
    "music_device_settings_put":    cmd_music_device_settings_put,
    "music_output_devices":         cmd_music_output_devices,
    "music_output_sink_result":     cmd_music_output_sink_result,
    "music_request_state":          cmd_music_request_state,
    "music_open_artist":            cmd_music_open_artist,
    "music_theme_set":              cmd_music_theme_set,
    "music_accent_set":             cmd_music_accent_set,
    "music_eq_state":               cmd_music_eq_state,
    "music_catalog_search":         cmd_music_catalog_search,
    "music_artist_catalog":         cmd_music_artist_catalog,
    "music_artist_images":          cmd_music_artist_images,
    "music_album_tracks":           cmd_music_album_tracks,
    "music_mix_from_seed":          cmd_music_mix_from_seed,
    "music_library_index":          cmd_music_library_index,
    "music_artist_follow":          cmd_music_artist_follow,
    "music_artist_unfollow":        cmd_music_artist_unfollow,
    "music_followed_artists":       cmd_music_followed_artists,
    "music_insights":               cmd_music_insights,
    "music_status":                 cmd_music_status,
    "music_local_roots":            cmd_music_local_roots,
    "music_local_scan":             cmd_music_local_scan,
    "music_local_status":           cmd_music_local_status,
    "music_download_track":         cmd_music_download_track,
    "music_client_capabilities":    cmd_music_client_capabilities,
}
