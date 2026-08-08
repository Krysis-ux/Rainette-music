"""Local HTTP + WebSocket server for the standalone Rainette Music app.

One aiohttp process serves the static ``web/`` frontend and a ``/ws`` WebSocket.
Incoming WS messages are routed to ``music_bridge.DISPATCH``; handler results are
broadcast back to every connected client. yt-dlp workers run on daemon threads
and push results back onto the event loop thread-safely, so the loop never blocks
on network I/O.
"""

from __future__ import annotations

import asyncio
import contextvars
import hmac
import json
import os
import re
import secrets
import socket
import sys
import threading
import time
import uuid
import urllib.parse
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import aiohttp
from aiohttp import WSMsgType, web

import shared
import music_bridge
from companion import CompanionRegistry
from state import MusicState

BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "web"


def app_data_dir() -> Path:
    """Per-user writable directory for the database, artwork, and credentials.

    Every entry point must agree on this path.  An earlier revision fell back to
    ``BASE_DIR`` off Windows, so the companion gateway kept its database beside
    the source tree while the desktop app used the platform location: two
    processes, two different ``music.db`` files, one very confusing bug report.
    """
    if os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return root / "Rainette Music"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Rainette Music"
    root = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return root / "Rainette Music"


APP_DATA_DIR = app_data_dir()
DB_PATH = APP_DATA_DIR / "music.db"
ARTWORK_DIR = APP_DATA_DIR / "playlist-artwork"
MAX_PLAYLIST_ARTWORK_BYTES = 5 * 1024 * 1024
APP_TOKEN = secrets.token_urlsafe(32)
CLIENT_KEY = web.AppKey("rainette_http_client", aiohttp.ClientSession)
COMPANION_REGISTRY_KEY = web.AppKey("rainette_companion_registry", CompanionRegistry)
COMMAND_TIMEOUT_KEY = web.AppKey("rainette_companion_command_timeout", float)
SYNC_BROKER_KEY = web.AppKey("rainette_companion_sync_broker", object)
ALLOWED_ORIGINS_KEY = web.AppKey("rainette_companion_allowed_origins", frozenset)
PAIR_LIMITER_KEY = web.AppKey("rainette_companion_pair_limiter", object)

# Identifies which paired phone caused the music_bridge handler that is running
# right now to fan out.  Playback events are routed back to that device only, so
# two phones on one computer never overwrite each other's now-playing state.
# The desktop's own windows leave this empty and keep receiving everything.
_origin_device: contextvars.ContextVar[str] = contextvars.ContextVar(
    "rainette_origin_device_id", default=""
)

_ARTWORK_TYPES = {
    "image/png": ("png", lambda data: data.startswith(b"\x89PNG\r\n\x1a\n")),
    "image/jpeg": ("jpg", lambda data: data.startswith(b"\xff\xd8\xff")),
    "image/webp": ("webp", lambda data: len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"),
}
_ARTWORK_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+_[0-9a-f]{32}\.(?:png|jpg|webp)$")

# Ports tried in order until one binds (handles a stale instance / port clash).
PORT_RANGE = range(8777, 8788)

# Unlike the loopback UI, a paired phone has to reconnect to the same LAN port
# after the desktop process restarts.  The first successfully bound port is
# persisted below APP_DATA_DIR; this small range is used only before a phone is
# paired (or after every old pairing has been revoked).
COMPANION_PORT_RANGE = range(47878, 47888)
COMPANION_PORT_FILENAME = "companion-port"
PWA_CONFIG_FILENAME = "pwa-config.json"

# The production PWA. The operator can point Rainette at their own deployment
# (a fork, or a Vercel preview) from Settings → Mobile; this is only the default.
DEFAULT_PWA_URL = "https://music-pwa-web.vercel.app"

# The /audio proxy only relays these hosts (googlevideo serves the actual audio;
# youtube/ytimg for redirects/art). Keeps the local proxy from being a general
# open relay even though it is bound to 127.0.0.1 only.
_ALLOWED_AUDIO_HOSTS = ("googlevideo.com", "youtube.com", "ytimg.com", "ggpht.com")

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".png": "image/png",
    ".woff2": "font/woff2",
}

# Mobile may invoke only the source-neutral music contract needed by the
# companion UI. Desktop settings, overlays, and arbitrary bridge messages are
# intentionally absent from this LAN allowlist.
COMPANION_COMMAND_TYPES = frozenset({
    "music_search",
    "music_stream_url",
    "music_catalog_search",
    "music_artist_catalog",
    "music_album_tracks",
    "music_mix_from_seed",
    "music_library_index",
    "music_artist_follow",
    "music_artist_unfollow",
    "music_followed_artists",
    "music_playlist_list",
    "music_playlist_create",
    "music_playlist_rename",
    "music_playlist_delete",
    "music_playlist_update_meta",
    "music_playlist_folder_create",
    "music_playlist_folder_rename",
    "music_playlist_folder_delete",
    "music_playlist_folder_move",
    "music_smart_playlist_create",
    "music_smart_playlist_update",
    "music_smart_playlist_delete",
    "music_smart_playlist_tracks",
    "music_playlist_add_track",
    "music_playlist_remove_track",
    "music_playlist_tracks",
    "music_recent",
    "music_top_artists",
    "music_insights",
    "music_queue_session_save",
    "music_queue_session_list",
    "music_queue_session_delete",
    "music_now_playing_set",
    "music_progress",
    "music_remote_play",
    "music_remote_control",
    "music_output_transfer",
    "music_output_transfer_result",
    "music_request_state",
    "music_open_artist",
    "music_status",
})

# These playback messages are fan-out notifications, not request/response
# calls. Their bridge handlers intentionally do not emit an id-correlated
# result, so the HTTP API acknowledges them immediately after dispatch. Output
# transfer is intentionally excluded: the target has to confirm it loaded
# before the source may pause.
COMPANION_ONE_WAY_COMMAND_TYPES = frozenset({
    "music_now_playing_set",
    "music_progress",
    "music_remote_play",
    "music_remote_control",
    "music_output_transfer_result",
    "music_request_state",
    "music_open_artist",
})


class _DeviceEventLog:
    """One replayable event log belonging to a single paired device."""

    def __init__(self, *, history_limit: int) -> None:
        self._history_limit = history_limit
        self._revision = 0
        self._events: list[dict] = []
        self.last_read_at = time.monotonic()
        self.condition = threading.Condition()

    def publish_locked(self, message: dict) -> None:
        """Append one event.  Caller must hold ``self.condition``."""
        self._revision += 1
        self._events.append({"revision": self._revision, "message": dict(message)})
        if len(self._events) > self._history_limit:
            del self._events[: len(self._events) - self._history_limit]
        self.condition.notify_all()

    def read_after_locked(self, after: int) -> dict:
        first = self._events[0]["revision"] if self._events else self._revision
        # A client can be ahead when the desktop process (and therefore this
        # in-memory log) restarts.  Without this branch a phone would keep
        # polling an impossible future revision forever.  Falling behind the
        # retained history has the same recovery contract.
        reset_required = after > self._revision or bool(self._events and after < first - 1)
        events = [] if reset_required else [event for event in self._events if event["revision"] > after]
        return {"revision": self._revision, "reset_required": reset_required, "events": events}


class CompanionSyncBroker:
    """Replayable event logs, one per paired companion device.

    The desktop hub is the source of truth.  Phones long-poll their own log and
    use its monotonic revision to detect reconnect gaps without opening the
    desktop WebSocket to anything but loopback.

    Two categories of event are routed differently, and the split is the whole
    point of this class:

    * **Playback state** (``_SESSION_TYPES``) belongs to whichever device caused
      it.  A phone hitting pause must not pause a different phone, so these are
      delivered only to the originating device's log.
    * **Catalog state** (everything else in ``_SYNC_TYPES``) describes the one
      shared music library on this computer.  A playlist created on one phone
      genuinely should appear on all of them, so these still fan out.

    Output transfer inverts the rule: its entire purpose is to reach a *different*
    device, so it routes by ``target_device_id`` instead of by origin.
    """

    _SESSION_TYPES = frozenset({
        "music_now_playing", "music_progress", "music_remote_play",
        "music_remote_control", "music_request_state", "music_open_artist",
    })

    _TARGETED_TYPES = frozenset({
        "music_output_transfer", "music_output_transfer_result",
    })

    _SYNC_TYPES = frozenset({
        "music_now_playing", "music_progress", "music_remote_play",
        "music_remote_control", "music_output_transfer",
        "music_library_index_result", "music_playlist_list_result",
        "music_followed_artists_result", "music_recent_result",
        "music_top_artists_result", "music_insights_result",
        "music_playlist_tracks_result", "music_smart_playlist_tracks_result",
        "music_queue_session_list_result",
        "music_artist_followed", "music_artist_unfollowed",
        "music_playlist_created", "music_playlist_renamed",
        "music_playlist_deleted", "music_playlist_meta_updated",
        "music_playlist_folder_created", "music_playlist_folder_renamed",
        "music_playlist_folder_deleted", "music_playlist_folder_moved",
        "music_smart_playlist_created", "music_smart_playlist_updated",
        "music_smart_playlist_deleted", "music_playlist_track_added",
        "music_playlist_track_removed", "music_queue_session_saved",
        "music_queue_session_deleted",
    })

    # A log is dropped once its phone has not polled for this long.  Phones
    # long-poll on a 25s cycle, so this is many missed cycles, not a live device.
    _IDLE_EVICTION_S = 15 * 60

    def __init__(self, *, history_limit: int = 256) -> None:
        self._history_limit = max(32, int(history_limit))
        self._lock = threading.Lock()
        self._logs: dict[str, _DeviceEventLog] = {}

    def _log_for(self, device_id: str) -> _DeviceEventLog:
        """Return (creating if needed) the log belonging to one device."""
        with self._lock:
            log = self._logs.get(device_id)
            if log is None:
                log = _DeviceEventLog(history_limit=self._history_limit)
                self._logs[device_id] = log
            return log

    def _evict_idle_locked(self) -> None:
        cutoff = time.monotonic() - self._IDLE_EVICTION_S
        for device_id in [key for key, log in self._logs.items() if log.last_read_at < cutoff]:
            self._logs.pop(device_id, None)

    def forget(self, device_id: str) -> None:
        """Drop a device's log, used when its pairing is revoked."""
        with self._lock:
            self._logs.pop(str(device_id), None)

    def _recipients(self, message: dict, origin_device_id: str) -> list[_DeviceEventLog]:
        message_type = message.get("type")
        with self._lock:
            self._evict_idle_locked()
            if message_type in self._TARGETED_TYPES:
                target = str(message.get("target_device_id") or "")
                # An unaddressed transfer is a desktop-only broadcast; a phone
                # with no matching log simply has nothing to receive.
                log = self._logs.get(target)
                return [log] if log is not None else []
            if message_type in self._SESSION_TYPES:
                if not origin_device_id:
                    # Desktop-originated playback: no phone session owns it.
                    return []
                log = self._logs.get(origin_device_id)
                return [log] if log is not None else []
            return list(self._logs.values())

    def publish(self, message: dict, origin_device_id: str = "") -> None:
        if not isinstance(message, dict) or message.get("type") not in self._SYNC_TYPES:
            return
        for log in self._recipients(message, str(origin_device_id or "")):
            with log.condition:
                log.publish_locked(message)

    def read_after(self, device_id: str, after: int, wait_s: float) -> dict:
        log = self._log_for(str(device_id))
        with log.condition:
            log.last_read_at = time.monotonic()
            result = log.read_after_locked(after)
            if result["events"] or result["reset_required"] or wait_s <= 0:
                return result
            log.condition.wait(timeout=min(max(wait_s, 0), 25))
            log.last_read_at = time.monotonic()
            return log.read_after_locked(after)


class CommandBroker:
    """Match thread-safe Hub broadcasts to one awaiting HTTP request."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._waiters: dict[str, tuple[asyncio.AbstractEventLoop, asyncio.Future]] = {}

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._waiters)

    async def dispatch_and_wait(self, message: dict, *, timeout_s: float) -> dict:
        request_id = str(message["id"])
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        with self._lock:
            if request_id in self._waiters:
                raise ValueError("a command with this id is already pending")
            self._waiters[request_id] = (loop, future)
        try:
            _dispatch(message)
            return await asyncio.wait_for(future, timeout=timeout_s)
        finally:
            with self._lock:
                current = self._waiters.get(request_id)
                if current is not None and current[1] is future:
                    self._waiters.pop(request_id, None)

    def receive(self, message: dict) -> None:
        request_id = message.get("id") if isinstance(message, dict) else None
        if request_id is None:
            return
        # The transfer request itself is broadcast to prospective targets with
        # the caller's id.  It is not an acknowledgement: resolving here would
        # pause the source before the target loaded anything.  Only the
        # explicit music_output_transfer_result may complete that waiter.
        if message.get("type") == "music_output_transfer":
            return
        with self._lock:
            waiter = self._waiters.get(str(request_id))
        if waiter is None:
            return
        loop, future = waiter

        def resolve() -> None:
            if not future.done():
                future.set_result(dict(message))

        loop.call_soon_threadsafe(resolve)


class Hub:
    """Fan-out of server → browser messages, safe to call from any thread."""

    def __init__(self) -> None:
        self._queues: set[asyncio.Queue] = set()
        self.loop: asyncio.AbstractEventLoop | None = None

    def register(self, queue: asyncio.Queue) -> None:
        self._queues.add(queue)

    def unregister(self, queue: asyncio.Queue) -> None:
        self._queues.discard(queue)

    def broadcast(self, msg: dict) -> None:
        """Wired into shared.notify_browsers. Called from the loop thread
        (CRUD handlers) and from daemon threads (yt-dlp workers) alike.

        ``_origin_device`` is read here rather than threaded through every
        handler signature: contextvars propagate into the daemon threads that
        yt-dlp workers run on, so a search started by one phone still returns to
        that phone alone without music_bridge needing to know phones exist.
        """
        command_broker.receive(msg)
        companion_sync_broker.publish(msg, _origin_device.get(""))
        loop = self.loop
        if loop is None:
            return
        data = json.dumps(msg)
        loop.call_soon_threadsafe(self._enqueue, data)

    def _enqueue(self, data: str) -> None:
        for queue in list(self._queues):
            queue.put_nowait(data)


hub = Hub()
command_broker = CommandBroker()
companion_registry = CompanionRegistry(storage_path=APP_DATA_DIR / "companion-devices.json")
companion_sync_broker = CompanionSyncBroker()
_companion_runtime: dict = {}
_companion_lock = threading.Lock()


def _dispatch(msg: dict) -> None:
    handler = music_bridge.DISPATCH.get(msg.get("type"))
    if handler is None:
        return  # unknown / overlay-only messages are harmless no-ops
    try:
        handler(msg)
    except Exception as exc:  # pragma: no cover - defensive
        hub.broadcast({"type": "error", "id": msg.get("id"), "ok": False, "msg": str(exc)})


async def _ws_writer(ws: web.WebSocketResponse, queue: asyncio.Queue) -> None:
    try:
        while True:
            data = await queue.get()
            await ws.send_str(data)
    except (asyncio.CancelledError, ConnectionResetError):
        pass


async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    if not _request_authorized(request):
        return web.Response(status=403, text="Forbidden")
    ws = web.WebSocketResponse(heartbeat=30, max_msg_size=0)
    await ws.prepare(request)
    queue: asyncio.Queue = asyncio.Queue()
    hub.register(queue)
    writer = asyncio.create_task(_ws_writer(ws, queue))
    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except (ValueError, TypeError):
                    continue
                if isinstance(data, dict):
                    _dispatch(data)
            elif msg.type == WSMsgType.ERROR:
                break
    finally:
        hub.unregister(queue)
        writer.cancel()
    return ws


def _safe_web_path(tail: str) -> Path | None:
    """Resolve a request path under WEB_DIR, rejecting traversal."""
    rel = (tail or "").strip("/") or "index.html"
    candidate = (WEB_DIR / rel).resolve()
    try:
        candidate.relative_to(WEB_DIR.resolve())
    except ValueError:
        return None
    if candidate.is_dir():
        candidate = candidate / "index.html"
    return candidate


async def static_handler(request: web.Request) -> web.StreamResponse:
    path = _safe_web_path(request.match_info.get("tail", ""))
    if path is None or not path.is_file():
        return web.Response(status=404, text="Not found")
    content_type = _CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")
    return web.FileResponse(path, headers={"Content-Type": content_type, "Cache-Control": "no-cache"})


def _json_error(status: int, message: str) -> web.Response:
    return web.json_response({"ok": False, "msg": message}, status=status)


def _request_authorized(request: web.Request) -> bool:
    supplied = request.headers.get("X-Rainette-Token") or request.query.get("token", "")
    if not supplied or not hmac.compare_digest(str(supplied), APP_TOKEN):
        return False
    origin = str(request.headers.get("Origin") or "").rstrip("/")
    expected = f"{request.scheme}://{request.host}".rstrip("/")
    return not origin or origin == expected


def _managed_artwork_path(artwork_key: str) -> Path | None:
    key = str(artwork_key or "").strip()
    if not _ARTWORK_KEY_RE.fullmatch(key) or Path(key).name != key:
        return None
    root = ARTWORK_DIR.resolve()
    candidate = (root / key).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate


async def playlist_artwork_upload(request: web.Request) -> web.StreamResponse:
    if not _request_authorized(request):
        return _json_error(403, "forbidden")
    playlist_id = str(request.match_info.get("playlist_id") or "").strip()
    playlist = shared.STATE.get_playlist(playlist_id) if shared.STATE is not None else None
    if playlist is None:
        return _json_error(404, "playlist not found")
    try:
        reader = await request.multipart()
        field = await reader.next()
    except Exception:
        return _json_error(400, "multipart image is required")
    if field is None or field.name != "file" or not field.filename:
        return _json_error(400, "file field is required")
    declared = str(field.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
    type_info = _ARTWORK_TYPES.get(declared)
    if type_info is None:
        return _json_error(400, "only PNG, JPEG, and WebP images are supported")
    data = bytearray()
    while True:
        chunk = await field.read_chunk(64 * 1024)
        if not chunk:
            break
        data.extend(chunk)
        if len(data) > MAX_PLAYLIST_ARTWORK_BYTES:
            return _json_error(413, "playlist artwork exceeds 5 MiB")
    extension, matches_magic = type_info
    payload = bytes(data)
    if not payload or not matches_magic(payload):
        return _json_error(400, "image content does not match its declared type")

    safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", playlist_id).strip("_") or "playlist"
    key = f"{safe_id}_{uuid.uuid4().hex}.{extension}"
    ARTWORK_DIR.mkdir(parents=True, exist_ok=True)
    path = _managed_artwork_path(key)
    if path is None:  # defensive; generated keys always satisfy the pattern
        return _json_error(500, "could not allocate artwork file")
    old_key = str(playlist.get("artwork_key") or "")
    try:
        path.write_bytes(payload)
        shared.STATE.update_playlist_artwork(playlist_id, key)
    except KeyError:
        path.unlink(missing_ok=True)
        return _json_error(404, "playlist not found")
    except Exception as exc:
        path.unlink(missing_ok=True)
        return _json_error(500, str(exc))
    old_path = _managed_artwork_path(old_key)
    if old_path is not None and old_path != path:
        try:
            old_path.unlink(missing_ok=True)
        except OSError:
            pass
    return web.json_response({
        "ok": True,
        "artwork_key": key,
        "artwork_url": "/playlist-artwork/" + key,
    })


async def playlist_artwork_get(request: web.Request) -> web.StreamResponse:
    path = _managed_artwork_path(request.match_info.get("artwork_key", ""))
    if path is None or not path.is_file():
        return _json_error(404, "artwork not found")
    return web.FileResponse(path, headers={"Cache-Control": "public, max-age=31536000, immutable"})


async def playlist_artwork_delete(request: web.Request) -> web.StreamResponse:
    if not _request_authorized(request):
        return _json_error(403, "forbidden")
    playlist_id = str(request.match_info.get("playlist_id") or "").strip()
    playlist = shared.STATE.get_playlist(playlist_id) if shared.STATE is not None else None
    if playlist is None:
        return _json_error(404, "playlist not found")
    old_path = _managed_artwork_path(str(playlist.get("artwork_key") or ""))
    shared.STATE.update_playlist_artwork(playlist_id, "")
    if old_path is not None:
        try:
            old_path.unlink(missing_ok=True)
        except OSError:
            pass
    return web.json_response({"ok": True, "artwork_key": "", "artwork_url": ""})


def _audio_host_allowed(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in _ALLOWED_AUDIO_HOSTS)


async def audio_proxy(request: web.Request) -> web.StreamResponse:
    """Stream a resolved audio URL through our own origin.

    Web Audio's MediaElementSource outputs silence for cross-origin media, so
    the equalizer/volume graph only works if the <audio> src is same-origin.
    This relays the bytes (honouring Range so seeking still works) from the
    googlevideo stream. Audio only flows through Python when the EQ is in use.
    """
    src = request.query.get("u", "")
    if not src or not _audio_host_allowed(src):
        return web.Response(status=400, text="bad or disallowed url")
    forward = {"User-Agent": request.headers.get("User-Agent", "Mozilla/5.0")}
    if "Range" in request.headers:
        forward["Range"] = request.headers["Range"]
    session: aiohttp.ClientSession = request.app[CLIENT_KEY]
    try:
        upstream = await session.get(src, headers=forward)
    except Exception as exc:  # network/DNS/SSL failure
        return web.Response(status=502, text=str(exc))
    resp = web.StreamResponse(status=upstream.status)
    for header in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"):
        if header in upstream.headers:
            resp.headers[header] = upstream.headers[header]
    resp.headers.setdefault("Accept-Ranges", "bytes")
    resp.headers["Cache-Control"] = "no-store"
    try:
        await resp.prepare(request)
        async for chunk in upstream.content.iter_chunked(65536):
            await resp.write(chunk)
        await resp.write_eof()
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        upstream.release()
    return resp


async def _on_startup(app: web.Application) -> None:
    app[CLIENT_KEY] = aiohttp.ClientSession()


async def _on_cleanup(app: web.Application) -> None:
    session = app.get(CLIENT_KEY)
    if session is not None:
        await session.close()


class RateLimiter:
    """Fixed-window attempt cap, keyed by caller.

    The pairing endpoints are the only unauthenticated surface on the gateway.
    Without a cap, an invitation token (or a request id) could be brute-forced
    through the operator's own tunnel at line rate.
    """

    def __init__(self, *, limit: int = 20, window_s: float = 60.0,
                 now: Callable[[], float] = time.monotonic) -> None:
        self._limit = max(1, int(limit))
        self._window_s = max(1.0, float(window_s))
        self._now = now
        self._lock = threading.Lock()
        self._hits: dict[str, list[float]] = {}

    def allow(self, key: str) -> bool:
        now = self._now()
        cutoff = now - self._window_s
        with self._lock:
            for stale in [k for k, hits in self._hits.items() if not hits or hits[-1] < cutoff]:
                self._hits.pop(stale, None)
            hits = [hit for hit in self._hits.get(key, []) if hit >= cutoff]
            if len(hits) >= self._limit:
                self._hits[key] = hits
                return False
            hits.append(now)
            self._hits[key] = hits
            return True


def _client_key(request: web.Request) -> str:
    """Identify a caller for rate limiting.

    Behind the operator's tunnel every request arrives from loopback, so the
    forwarded-for hint is used when present and the peer address is the
    fallback.  This is abuse damping, not authentication; a spoofed header can
    only ever cost the spoofer their own bucket.
    """
    forwarded = str(request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
    return forwarded or (request.remote or "unknown")


def _origin_allowed(request: web.Request) -> bool:
    origin = str(request.headers.get("Origin") or "").rstrip("/")
    return not origin or origin in request.app[ALLOWED_ORIGINS_KEY]


def _apply_cors(request: web.Request, response: web.StreamResponse) -> web.StreamResponse:
    origin = str(request.headers.get("Origin") or "").rstrip("/")
    if origin and origin in request.app[ALLOWED_ORIGINS_KEY]:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
        response.headers["Access-Control-Max-Age"] = "600"
        response.headers["Vary"] = "Origin"
    return response


@web.middleware
async def _companion_cors(request: web.Request, handler):
    """Admit only the exact PWA origins this computer was configured for.

    The gateway is reachable over the public internet through the operator's
    tunnel, so a wildcard here would let any website drive their music.
    """
    if not _origin_allowed(request):
        return web.json_response({"ok": False, "msg": "origin is not allowed"}, status=403)
    if request.method == "OPTIONS":
        return _apply_cors(request, web.Response(status=204))
    return _apply_cors(request, await handler(request))


@web.middleware
async def _companion_auth(request: web.Request, handler):
    """Authorize every companion route except pairing and audio redemption.

    ``/audio/`` is exempt because a browser media element cannot send headers;
    that route authenticates on the unguessable grant in its own path instead
    (see ``CompanionRegistry.resolve_relay``).
    """
    if request.path in {"/pair/request", "/pair/result"} or request.path.startswith("/audio/"):
        return await handler(request)
    registry = request.app[COMPANION_REGISTRY_KEY]
    auth = str(request.headers.get("Authorization") or "")
    scheme, _, token = auth.partition(" ")
    device_id = registry.device_id_for_token(token) if scheme.lower() == "bearer" else None
    if device_id is None:
        return web.json_response({"ok": False, "msg": "device authorization required"}, status=401)
    request["companion_device_id"] = device_id
    return await handler(request)


async def companion_pair_request(request: web.Request) -> web.StreamResponse:
    if not request.app[PAIR_LIMITER_KEY].allow(_client_key(request)):
        return _json_error(429, "too many pairing attempts; wait a moment and try again")
    try:
        payload = await request.json()
        result = request.app[COMPANION_REGISTRY_KEY].request_pairing(
            str(payload.get("invitation") or ""),
            str(payload.get("device_name") or ""),
        )
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        return _json_error(400, str(exc))
    return web.json_response({"ok": True, **result}, status=202)


async def companion_pair_result(request: web.Request) -> web.StreamResponse:
    if not request.app[PAIR_LIMITER_KEY].allow(_client_key(request)):
        return _json_error(429, "too many pairing attempts; wait a moment and try again")
    try:
        payload = await request.json()
        result = request.app[COMPANION_REGISTRY_KEY].pairing_result(
            str(payload.get("request_id") or ""),
            str(payload.get("invitation") or ""),
        )
    except (TypeError, json.JSONDecodeError) as exc:
        return _json_error(400, str(exc))
    if result is None:
        return _json_error(404, "pairing result is not available")
    status = 202 if result["status"] == "pending" else 410 if result["status"] == "expired" else 200
    return web.json_response({"ok": result["status"] == "approved", **result}, status=status)


async def companion_pair_ack(request: web.Request) -> web.StreamResponse:
    try:
        payload = await request.json()
        request_id = str(payload.get("request_id") or "")
    except (TypeError, json.JSONDecodeError):
        return _json_error(400, "a pairing request id is required")
    if not request_id:
        return _json_error(400, "a pairing request id is required")
    acknowledged = request.app[COMPANION_REGISTRY_KEY].acknowledge_pairing(
        request_id,
        request["companion_device_id"],
    )
    if not acknowledged:
        return _json_error(404, "pairing result is not available")
    return web.json_response({"ok": True, "request_id": request_id})


async def companion_status(request: web.Request) -> web.StreamResponse:
    return web.json_response({
        "ok": True,
        "device_id": request["companion_device_id"],
        "capabilities": ["pairing", "library", "events", "output-transfer"],
    })


async def companion_events(request: web.Request) -> web.StreamResponse:
    """Return companion events after a known revision, waiting up to 25s."""
    try:
        after = max(0, int(request.query.get("after", "0")))
        wait_s = min(25.0, max(0.0, float(request.query.get("wait", "25"))))
    except ValueError:
        return _json_error(400, "after and wait must be numeric")
    broker = request.app[SYNC_BROKER_KEY]
    device_id = request["companion_device_id"]
    payload = await asyncio.to_thread(broker.read_after, device_id, after, wait_s)
    payload.update({"ok": True, "device_id": device_id})
    return web.json_response(payload)


async def companion_command(request: web.Request) -> web.StreamResponse:
    """Dispatch one authenticated mobile command through the desktop bridge."""
    try:
        payload = await request.json()
    except (TypeError, json.JSONDecodeError):
        return _json_error(400, "a JSON command object is required")
    if not isinstance(payload, dict):
        return _json_error(400, "a JSON command object is required")
    command_type = payload.get("type")
    if command_type not in COMPANION_COMMAND_TYPES or command_type not in music_bridge.DISPATCH:
        return _json_error(400, "command type is not allowed")
    request_id = payload.get("id")
    if request_id is None:
        request_id = "mobile_" + uuid.uuid4().hex
        payload["id"] = request_id
    elif not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 200:
        return _json_error(400, "command id is invalid")
    device_id = request["companion_device_id"]
    # Everything this command fans out is attributed to the calling phone, so
    # its playback events come back to it and to no other paired device.
    token = _origin_device.set(device_id)
    try:
        if command_type in COMPANION_ONE_WAY_COMMAND_TYPES:
            _dispatch(payload)
            return web.json_response({
                "ok": True,
                "id": request_id,
                "type": f"{command_type}_accepted",
            })
        try:
            result = await command_broker.dispatch_and_wait(
                payload,
                timeout_s=request.app[COMMAND_TIMEOUT_KEY],
            )
        except asyncio.TimeoutError:
            return _json_error(504, "desktop command timed out")
        except ValueError as exc:
            return _json_error(409, str(exc))
    finally:
        _origin_device.reset(token)

    # A phone never receives a raw googlevideo URL. Swap it for an opaque grant
    # that only this device's token can redeem, so a leaked media URL is not a
    # usable credential and cannot be replayed by another paired phone.
    if command_type == "music_stream_url" and result.get("ok") and result.get("url"):
        try:
            ttl = min(max(int(result.get("expires_hint_s") or 3600), 60), 21_600)
            grant = request.app[COMPANION_REGISTRY_KEY].create_relay_grant(
                device_id, str(result["url"]), ttl_s=ttl
            )
        except (TypeError, ValueError) as exc:
            return _json_error(502, str(exc))
        result = dict(result)
        result["url"] = "/audio/" + grant["token"]
        result["relayed_by"] = "user-pc"
    return web.json_response(result)


async def companion_audio_relay(request: web.Request) -> web.StreamResponse:
    """Relay an opaque, short-lived, device-bound grant with Range support."""
    upstream_url = request.app[COMPANION_REGISTRY_KEY].resolve_relay(
        request.match_info.get("grant", "")
    )
    if not upstream_url or not _audio_host_allowed(upstream_url):
        return _json_error(404, "relay grant is not available")
    forward = {"User-Agent": request.headers.get("User-Agent", "Rainette Mobile")}
    if "Range" in request.headers:
        forward["Range"] = request.headers["Range"]
    try:
        upstream = await request.app[CLIENT_KEY].get(upstream_url, headers=forward)
    except Exception as exc:
        return _json_error(502, str(exc))
    response = web.StreamResponse(status=upstream.status)
    for header in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"):
        if header in upstream.headers:
            response.headers[header] = upstream.headers[header]
    response.headers.setdefault("Accept-Ranges", "bytes")
    response.headers["Cache-Control"] = "no-store"
    try:
        await response.prepare(request)
        async for chunk in upstream.content.iter_chunked(65536):
            await response.write(chunk)
        await response.write_eof()
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        upstream.release()
    return response


def build_companion_app(
    registry: CompanionRegistry | None = None,
    *,
    command_timeout_s: float = 25.0,
    sync_broker: CompanionSyncBroker | None = None,
    allowed_origins: set[str] | frozenset[str] | None = None,
    pair_limiter: RateLimiter | None = None,
) -> web.Application:
    """Build the companion API without exposing desktop web routes.

    The listener is started separately from :func:`start`; keeping it a small,
    separate app means the desktop launch token and the static UI are never
    reachable through the operator's public tunnel — only these routes are.
    """
    app = web.Application(middlewares=[_companion_cors, _companion_auth])
    app[COMPANION_REGISTRY_KEY] = registry or companion_registry
    app[COMMAND_TIMEOUT_KEY] = float(command_timeout_s)
    app[SYNC_BROKER_KEY] = sync_broker or companion_sync_broker
    app[ALLOWED_ORIGINS_KEY] = frozenset(
        normalize_origin(origin) for origin in (allowed_origins or configured_pwa_origins())
    )
    app[PAIR_LIMITER_KEY] = pair_limiter or RateLimiter()
    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    app.router.add_post("/pair/request", companion_pair_request)
    app.router.add_post("/pair/result", companion_pair_result)
    app.router.add_post("/pair/ack", companion_pair_ack)
    app.router.add_get("/status", companion_status)
    app.router.add_get("/events", companion_events)
    app.router.add_post("/command", companion_command)
    app.router.add_get("/audio/{grant}", companion_audio_relay)
    return app


def build_app() -> web.Application:
    app = web.Application()
    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/audio", audio_proxy)
    app.router.add_post("/playlist-artwork/{playlist_id}", playlist_artwork_upload)
    app.router.add_delete("/playlist-artwork/{playlist_id}", playlist_artwork_delete)
    app.router.add_get("/playlist-artwork/{artwork_key}", playlist_artwork_get)
    app.router.add_get("/{tail:.*}", static_handler)
    return app


def _run_loop(app: web.Application, ports, port_holder: dict, ready: threading.Event) -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    hub.loop = loop
    runner = web.AppRunner(app)
    loop.run_until_complete(runner.setup())

    bound = None
    last_err: Exception | None = None
    for port in ports:
        site = web.TCPSite(runner, "127.0.0.1", port)
        try:
            loop.run_until_complete(site.start())
            bound = port
            break
        except OSError as exc:  # port in use — try the next one
            last_err = exc
            continue
    if bound is None:
        port_holder["error"] = last_err or RuntimeError("no free port")
        ready.set()
        return

    port_holder["port"] = bound
    ready.set()
    loop.run_forever()


def start(preferred: int | None = None) -> int:
    """Boot state + server on a background thread. Returns the bound port.

    ``preferred`` (or the ``RAINETTE_MUSIC_PORT`` env var) is tried first, then
    the default range.
    """
    if preferred is None:
        env = os.environ.get("RAINETTE_MUSIC_PORT")
        preferred = int(env) if (env and env.isdigit()) else None
    ports = ([preferred] if preferred else []) + [p for p in PORT_RANGE if p != preferred]

    state = MusicState(DB_PATH)
    shared.configure(state=state, notify=hub.broadcast, policy={"playlist_artwork_dir": ARTWORK_DIR})

    app = build_app()
    port_holder: dict = {}
    ready = threading.Event()
    thread = threading.Thread(
        target=_run_loop, args=(app, ports, port_holder, ready), name="rainette-music-server", daemon=True
    )
    thread.start()
    ready.wait(15)
    if "error" in port_holder:
        raise port_holder["error"]
    port = port_holder.get("port")
    if not port:
        raise RuntimeError("music server did not start in time")
    return port


def normalize_origin(value: str) -> str:
    """Reduce a URL to a bare scheme://host[:port] origin, or reject it."""
    parsed = urlparse(str(value or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"invalid origin: {value}")
    return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")


def _pwa_config_path() -> Path:
    return APP_DATA_DIR / PWA_CONFIG_FILENAME


def read_pwa_config() -> dict:
    """Return the operator's PWA address and public tunnel address.

    These are routing data, not secrets: the PWA is a public static site and
    the tunnel hostname is already visible to anyone who can reach it.
    """
    try:
        payload = json.loads(_pwa_config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"pwa_url": DEFAULT_PWA_URL, "public_url": ""}
    if not isinstance(payload, dict):
        return {"pwa_url": DEFAULT_PWA_URL, "public_url": ""}
    return {
        "pwa_url": str(payload.get("pwa_url") or DEFAULT_PWA_URL),
        "public_url": str(payload.get("public_url") or ""),
    }


def write_pwa_config(pwa_url: str, public_url: str) -> dict:
    """Validate and persist the two addresses pairing links are built from."""
    pwa = str(pwa_url or "").strip() or DEFAULT_PWA_URL
    public = str(public_url or "").strip()
    normalize_origin(pwa)  # raises ValueError on anything unusable
    if public:
        parsed = urlparse(public)
        local = (parsed.hostname or "") in {"localhost", "127.0.0.1", "::1"}
        # An HTTPS page cannot call a plain-HTTP endpoint on another device, so
        # a non-local companion address that is not HTTPS can never work; fail
        # here rather than minting a pairing link that silently cannot connect.
        if parsed.scheme != "https" and not (parsed.scheme == "http" and local):
            raise ValueError("the public companion address must use trusted HTTPS")
    config = {"pwa_url": pwa.rstrip("/"), "public_url": public.rstrip("/")}
    path = _pwa_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(config, separators=(",", ":")), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
    return config


def configured_pwa_origins() -> frozenset[str]:
    """Exact origins the gateway will answer, never a wildcard."""
    origins = {normalize_origin(DEFAULT_PWA_URL)}
    configured = read_pwa_config().get("pwa_url") or ""
    if configured:
        try:
            origins.add(normalize_origin(configured))
        except ValueError:
            pass
    for extra in str(os.environ.get("RAINETTE_PWA_ORIGIN", "")).split(","):
        if extra.strip():
            try:
                origins.add(normalize_origin(extra))
            except ValueError:
                continue
    return frozenset(origins)


def _companion_port_path() -> Path:
    return APP_DATA_DIR / COMPANION_PORT_FILENAME


def _valid_port(value: object, *, allow_zero: bool = False) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("companion port must be numeric") from exc
    minimum = 0 if allow_zero else 1
    if port < minimum or port > 65535:
        raise ValueError("companion port must be between 1 and 65535")
    return port


def _read_companion_port() -> int | None:
    try:
        value = _companion_port_path().read_text(encoding="ascii").strip()
        return _valid_port(value)
    except (OSError, ValueError):
        return None


def _persist_companion_port(port: int) -> None:
    """Atomically remember the endpoint port; it is routing data, not a secret."""
    selected = _valid_port(port)
    path = _companion_port_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(str(selected), encoding="ascii")
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _active_companion_devices() -> bool:
    return any(not bool(device.get("revoked")) for device in companion_registry.devices())


def _companion_port_candidates(requested: int | None) -> tuple[list[int], bool]:
    """Return bind candidates and whether changing the selected port is safe.

    Once a device is paired, its pinned endpoint contains the port.  A busy
    persisted port must therefore fail loudly instead of silently moving the
    listener somewhere the phone cannot discover.  With no active devices we
    may safely fall back and persist the newly selected port.
    """
    persisted = _read_companion_port()
    active_devices = _active_companion_devices()
    if requested is not None:
        explicit = _valid_port(requested, allow_zero=True)
        if active_devices and persisted is not None and explicit != persisted:
            raise RuntimeError(
                f"companion port {persisted} is pinned by paired devices; revoke them before changing it"
            )
        return [explicit], not active_devices
    if persisted is not None:
        candidates = [persisted]
        if not active_devices:
            candidates.extend(port for port in COMPANION_PORT_RANGE if port != persisted)
        return candidates, not active_devices
    configured = os.environ.get("RAINETTE_COMPANION_PORT", "").strip()
    preferred = _valid_port(configured) if configured else COMPANION_PORT_RANGE.start
    candidates = [preferred]
    candidates.extend(port for port in COMPANION_PORT_RANGE if port != preferred)
    # A legacy installation can have paired-device records but no saved port
    # because older releases used an unknowable ephemeral endpoint.  Selecting
    # and persisting a defined port is the only safe migration; the old phone
    # will be shown as reconnecting until it is explicitly re-paired.
    return candidates, True


def _bind_companion_socket(host: str, port: int) -> socket.socket:
    """Bind the companion listener without Windows wildcard-port sharing.

    ``asyncio.create_server``/``TCPSite`` may successfully bind a host on
    Windows while another process already owns ``127.0.0.1`` on the same port.
    That makes the persisted tunnel target ambiguous and defeats the fail-closed
    paired-device port policy.  SO_EXCLUSIVEADDRUSE reserves the complete port
    before aiohttp takes ownership of the socket.
    """
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name == "nt":
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        listener.bind((host, port))
        listener.setblocking(False)
        return listener
    except Exception:
        listener.close()
        raise


def _run_companion_loop(app: web.Application, host: str, ports: list[int],
                         holder: dict, ready: threading.Event) -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    runner = web.AppRunner(app)
    try:
        loop.run_until_complete(runner.setup())
        last_error: Exception | None = None
        for candidate in ports:
            listener: socket.socket | None = None
            try:
                listener = _bind_companion_socket(host, candidate)
                site = web.SockSite(runner, listener)
                loop.run_until_complete(site.start())
                sockets = site._server.sockets if site._server is not None else []  # aiohttp exposes no public port accessor
                holder["port"] = int(sockets[0].getsockname()[1]) if sockets else candidate
                break
            except OSError as exc:
                last_error = exc
                if listener is not None:
                    listener.close()
        if "port" not in holder:
            raise RuntimeError("no companion port is available") from last_error
        holder["loop"] = loop
    except Exception as exc:
        holder["error"] = exc
    finally:
        ready.set()
    if "error" not in holder:
        loop.run_forever()
    loop.run_until_complete(runner.cleanup())
    loop.close()


def _stop_companion_runtime(runtime: dict, *, timeout_s: float = 5.0) -> bool:
    loop = runtime.get("loop")
    thread = runtime.get("thread")
    if loop is not None and loop.is_running():
        loop.call_soon_threadsafe(loop.stop)
    if thread is not None and thread is not threading.current_thread():
        thread.join(timeout=max(0.1, float(timeout_s)))
    return not bool(thread and thread.is_alive())


def stop_companion(*, timeout_s: float = 5.0) -> bool:
    """Stop the companion listener (for orderly shutdown and restart tests)."""
    with _companion_lock:
        runtime = dict(_companion_runtime)
        _companion_runtime.clear()
    if not runtime:
        return True
    return _stop_companion_runtime(runtime, timeout_s=timeout_s)


def start_companion(*, host: str = "127.0.0.1", port: int | None = None) -> dict:
    """Start the companion gateway on loopback, on a durable port.

    The listener stays on ``127.0.0.1`` by design.  Phones reach it through the
    operator's own trusted HTTPS tunnel, which terminates TLS with a real
    certificate a browser will accept — a self-signed LAN certificate cannot be
    pinned by a browser the way a native app could, and forwarding this port on
    the router would publish an unencrypted gateway to the internet.

    This does not alter the loopback desktop app or make its routes public.
    Callers use :func:`create_companion_invitation` to obtain the QR payload.
    Once a phone is paired the selected port is never silently changed.
    """
    with _companion_lock:
        if _companion_runtime.get("port"):
            return dict(_companion_runtime)
        ports, may_change_port = _companion_port_candidates(port)
        holder: dict = {"host": host}
        ready = threading.Event()
        thread = threading.Thread(
            target=_run_companion_loop,
            args=(build_companion_app(companion_registry), host, ports, holder, ready),
            name="rainette-companion-server",
            daemon=True,
        )
        thread.start()
        if not ready.wait(15):
            raise RuntimeError("companion listener did not start in time")
        if "error" in holder:
            raise holder["error"]
        selected_port = int(holder["port"])
        persisted = _read_companion_port()
        if persisted is not None and selected_port != persisted and not may_change_port:
            _stop_companion_runtime({**holder, "thread": thread})
            raise RuntimeError(
                f"companion port {persisted} is unavailable; paired-device endpoint was not changed"
            )
        try:
            _persist_companion_port(selected_port)
        except Exception:
            _stop_companion_runtime({**holder, "thread": thread})
            raise
        _companion_runtime.update(holder)
        _companion_runtime["thread"] = thread
        return dict(_companion_runtime)


def start_paired_companion() -> dict | None:
    """Restore the LAN listener automatically when durable devices exist."""
    if not _active_companion_devices():
        return None
    return start_companion()


def create_companion_invitation(*, ttl_s: int = 300) -> dict:
    """Return the short-lived pairing payload for the QR code.

    The invitation alone grants nothing: it only lets a phone *ask*, and the
    operator still has to approve that request on the desktop before any
    credential exists.  The endpoint is the operator's configured tunnel; the
    loopback fallback is useful for same-machine testing only, since an HTTPS
    page on a phone cannot reach another device's localhost.
    """
    runtime = start_companion()
    config = read_pwa_config()
    invitation = companion_registry.create_invitation(ttl_s=ttl_s)
    endpoint = config.get("public_url") or f"http://127.0.0.1:{runtime['port']}"
    pairing_url = (
        config["pwa_url"].rstrip("/")
        + "/#"
        + urllib.parse.urlencode({
            "endpoint": endpoint.rstrip("/"),
            "invitation": invitation["token"],
        })
    )
    return {
        "version": 2,
        "endpoint": endpoint,
        "pwa_url": config["pwa_url"],
        "pairing_url": pairing_url,
        "invitation": invitation["token"],
        "expires_at": invitation["expires_at"],
        "tunnel_configured": bool(config.get("public_url")),
    }


def approve_companion_request(request_id: str) -> dict:
    approved = companion_registry.approve(request_id)
    return {
        "device_id": approved["device_id"],
        "name": approved["device_name"],
        "revoked": False,
    }


def companion_management_state() -> dict:
    return {
        "pending": companion_registry.pending_requests(),
        "devices": companion_registry.devices(),
    }


def reject_companion_request(request_id: str) -> bool:
    return companion_registry.reject(request_id)


def revoke_companion_device(device_id: str) -> bool:
    revoked = companion_registry.revoke(device_id)
    if revoked:
        # Drop the phone's queued events too, so a revoked device cannot drain
        # one last batch of another session's state on its way out.
        companion_sync_broker.forget(device_id)
    return revoked


if __name__ == "__main__":
    import sys
    pref = int(sys.argv[1]) if (len(sys.argv) > 1 and sys.argv[1].isdigit()) else None
    bound_port = start(preferred=pref)
    print(f"Rainette Music server on http://127.0.0.1:{bound_port}/  (Ctrl+C to stop)")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
