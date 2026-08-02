"""Browser-safe companion gateway for the Rainette Music iPhone PWA.

The PWA is static and can be hosted by Vercel. This process runs on the user's
Windows or Mac computer and is the only place that imports Rainette's existing
``music_bridge``. Searches and stream resolution therefore continue to run
through yt-dlp/ytmusicapi on the user's computer, not in Vercel.

Expose this loopback listener through a trusted HTTPS tunnel (for example a
named Cloudflare Tunnel or Tailscale Funnel). The access token is transferred
in the PWA URL fragment, so it is not sent to the static host or included in
HTTP referrers.
"""

from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import os
import secrets
import socket
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import urlparse

import aiohttp
from aiohttp import web

import music_bridge
import server
import shared
from state import MusicState

_ALLOWED_MEDIA_HOSTS = ("googlevideo.com", "youtube.com", "ytimg.com", "ggpht.com")
_DEFAULT_PORT = 47888
_TOKEN_FILENAME = "pwa-access-token"
_CLIENT_KEY = web.AppKey("rainette_pwa_http_client", aiohttp.ClientSession)
_RUNTIME_KEY = web.AppKey("rainette_pwa_runtime", object)
_TOKEN_KEY = web.AppKey("rainette_pwa_access_token", str)
_ORIGINS_KEY = web.AppKey("rainette_pwa_allowed_origins", frozenset)
_RELAY_KEY = web.AppKey("rainette_pwa_relay_store", object)


def _app_data_dir() -> Path:
    if os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return root / "Rainette Music"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Rainette Music"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "Rainette Music"


def _media_url_allowed(url: str) -> bool:
    try:
        parsed = urlparse(str(url or ""))
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"}:
        return False
    host = (parsed.hostname or "").lower()
    return any(host == domain or host.endswith("." + domain) for domain in _ALLOWED_MEDIA_HOSTS)


@dataclass(frozen=True)
class _RelayGrant:
    upstream_url: str
    expires_at: float


class RelayStore:
    """In-memory, unguessable, expiring handles for upstream audio URLs."""

    def __init__(self, *, now: Callable[[], float] = time.time) -> None:
        self._now = now
        self._lock = threading.RLock()
        self._grants: dict[str, _RelayGrant] = {}

    def create(self, upstream_url: str, *, ttl_s: int = 7200) -> str:
        if not _media_url_allowed(upstream_url):
            raise ValueError("media host is not allowed")
        ttl = min(max(int(ttl_s), 30), 21_600)
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._prune_locked()
            self._grants[token] = _RelayGrant(str(upstream_url), self._now() + ttl)
        return token

    def resolve(self, token: str) -> str | None:
        with self._lock:
            self._prune_locked()
            grant = self._grants.get(str(token or ""))
            return grant.upstream_url if grant is not None else None

    def _prune_locked(self) -> None:
        now = self._now()
        for token in [key for key, grant in self._grants.items() if grant.expires_at <= now]:
            self._grants.pop(token, None)


class EventLog:
    """Thread-safe, replayable event log for reconnecting browser clients."""

    def __init__(self, *, history_limit: int = 256) -> None:
        self._history_limit = max(32, int(history_limit))
        self._condition = threading.Condition()
        self._revision = 0
        self._events: list[dict] = []

    def publish(self, message: dict) -> None:
        if not isinstance(message, dict):
            return
        with self._condition:
            self._revision += 1
            self._events.append({"revision": self._revision, "message": dict(message)})
            if len(self._events) > self._history_limit:
                del self._events[: len(self._events) - self._history_limit]
            self._condition.notify_all()

    def read_after(self, after: int, wait_s: float) -> dict:
        with self._condition:
            result = self._read_after_locked(after)
            if result["events"] or result["reset_required"] or wait_s <= 0:
                return result
            self._condition.wait(timeout=min(max(float(wait_s), 0.0), 25.0))
            return self._read_after_locked(after)

    def _read_after_locked(self, after: int) -> dict:
        first = self._events[0]["revision"] if self._events else self._revision
        reset = after > self._revision or bool(self._events and after < first - 1)
        events = [] if reset else [item for item in self._events if item["revision"] > after]
        return {"revision": self._revision, "reset_required": reset, "events": events}


class RainettePwaRuntime:
    """Routes browser commands through the existing Rainette music handlers."""

    def __init__(self, state: MusicState, *, artwork_dir: Path, timeout_s: float = 30.0) -> None:
        self.state = state
        self.timeout_s = max(1.0, float(timeout_s))
        self.events_log = EventLog()
        self._lock = threading.Lock()
        self._waiters: dict[str, tuple[asyncio.AbstractEventLoop, asyncio.Future]] = {}
        shared.configure(state=state, notify=self._receive, policy={"playlist_artwork_dir": artwork_dir})

    def _receive(self, message: dict) -> None:
        if not isinstance(message, dict):
            return
        self.events_log.publish(message)
        request_id = message.get("id")
        if request_id is None or message.get("type") == "music_output_transfer":
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

    async def command(self, payload: dict) -> dict:
        command_type = str(payload.get("type") or "")
        handler = music_bridge.DISPATCH.get(command_type)
        if handler is None:
            return {"id": payload.get("id"), "ok": False, "msg": "command handler is unavailable"}
        if command_type in server.COMPANION_ONE_WAY_COMMAND_TYPES:
            handler(payload)
            return {
                "id": payload.get("id"),
                "ok": True,
                "type": f"{command_type}_accepted",
            }

        request_id = str(payload["id"])
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        with self._lock:
            if request_id in self._waiters:
                return {"id": request_id, "ok": False, "msg": "duplicate command id"}
            self._waiters[request_id] = (loop, future)
        try:
            handler(payload)
            return await asyncio.wait_for(future, timeout=self.timeout_s)
        except asyncio.TimeoutError:
            return {"id": request_id, "ok": False, "msg": "desktop command timed out"}
        except Exception as exc:
            return {"id": request_id, "ok": False, "msg": str(exc)}
        finally:
            with self._lock:
                current = self._waiters.get(request_id)
                if current is not None and current[1] is future:
                    self._waiters.pop(request_id, None)

    async def events(self, after: int, wait_s: float) -> dict:
        return await asyncio.to_thread(self.events_log.read_after, after, wait_s)


def _normalize_origin(value: str) -> str:
    parsed = urlparse(str(value or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"invalid PWA origin: {value}")
    return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")


def _origin_allowed(request: web.Request) -> bool:
    origin = str(request.headers.get("Origin") or "").rstrip("/")
    return not origin or origin in request.app[_ORIGINS_KEY]


def _apply_cors(request: web.Request, response: web.StreamResponse) -> web.StreamResponse:
    origin = str(request.headers.get("Origin") or "").rstrip("/")
    if origin and origin in request.app[_ORIGINS_KEY]:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
        response.headers["Access-Control-Max-Age"] = "600"
        response.headers["Vary"] = "Origin"
    return response


@web.middleware
async def _cors_middleware(request: web.Request, handler):
    if not _origin_allowed(request):
        return web.json_response({"ok": False, "msg": "origin is not allowed"}, status=403)
    if request.method == "OPTIONS":
        return _apply_cors(request, web.Response(status=204))
    response = await handler(request)
    return _apply_cors(request, response)


@web.middleware
async def _auth_middleware(request: web.Request, handler):
    if request.path.startswith("/audio/"):
        return await handler(request)
    auth = str(request.headers.get("Authorization") or "")
    scheme, _, supplied = auth.partition(" ")
    expected = request.app[_TOKEN_KEY]
    if scheme.lower() != "bearer" or not supplied or not hmac.compare_digest(supplied, expected):
        return web.json_response({"ok": False, "msg": "device authorization required"}, status=401)
    return await handler(request)


async def _status(request: web.Request) -> web.StreamResponse:
    return web.json_response({
        "ok": True,
        "name": socket.gethostname(),
        "capabilities": ["search", "library", "playback", "events", "audio-relay"],
        "architecture": "iphone-pwa-to-user-pc",
    })


async def _command(request: web.Request) -> web.StreamResponse:
    try:
        payload = await request.json()
    except (json.JSONDecodeError, TypeError):
        return web.json_response({"ok": False, "msg": "a JSON command object is required"}, status=400)
    if not isinstance(payload, dict):
        return web.json_response({"ok": False, "msg": "a JSON command object is required"}, status=400)
    command_type = str(payload.get("type") or "")
    if command_type not in server.COMPANION_COMMAND_TYPES or command_type not in music_bridge.DISPATCH:
        return web.json_response({"ok": False, "msg": "command type is not allowed"}, status=400)
    request_id = payload.get("id")
    if request_id is None:
        request_id = "pwa_" + secrets.token_hex(16)
        payload["id"] = request_id
    elif not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 200:
        return web.json_response({"ok": False, "msg": "command id is invalid"}, status=400)

    runtime = request.app[_RUNTIME_KEY]
    result = await runtime.command(payload)
    if command_type == "music_stream_url" and result.get("ok") and result.get("url"):
        try:
            ttl = min(max(int(result.get("expires_hint_s") or 7200), 60), 21_600)
            grant = request.app[_RELAY_KEY].create(str(result["url"]), ttl_s=ttl)
        except (TypeError, ValueError) as exc:
            return web.json_response({"id": request_id, "ok": False, "msg": str(exc)}, status=502)
        result = dict(result)
        result["url"] = "/audio/" + grant
        result["relayed_by"] = "user-pc"
    return web.json_response(result)


async def _events(request: web.Request) -> web.StreamResponse:
    try:
        after = max(0, int(request.query.get("after", "0")))
        wait_s = min(25.0, max(0.0, float(request.query.get("wait", "25"))))
    except ValueError:
        return web.json_response({"ok": False, "msg": "after and wait must be numeric"}, status=400)
    payload = await request.app[_RUNTIME_KEY].events(after, wait_s)
    payload["ok"] = True
    return web.json_response(payload)


async def _audio(request: web.Request) -> web.StreamResponse:
    upstream_url = request.app[_RELAY_KEY].resolve(request.match_info.get("grant", ""))
    if upstream_url is None:
        return web.json_response({"ok": False, "msg": "audio grant is unavailable"}, status=404)
    headers = {"User-Agent": request.headers.get("User-Agent", "Rainette PWA")}
    if "Range" in request.headers:
        headers["Range"] = request.headers["Range"]
    try:
        upstream = await request.app[_CLIENT_KEY].get(upstream_url, headers=headers, allow_redirects=True)
    except Exception as exc:
        return web.json_response({"ok": False, "msg": str(exc)}, status=502)

    response = web.StreamResponse(status=upstream.status)
    for header in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges", "ETag"):
        if header in upstream.headers:
            response.headers[header] = upstream.headers[header]
    response.headers.setdefault("Accept-Ranges", "bytes")
    response.headers["Cache-Control"] = "no-store"
    try:
        await response.prepare(request)
        async for chunk in upstream.content.iter_chunked(64 * 1024):
            await response.write(chunk)
        await response.write_eof()
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        upstream.release()
    return response


async def _on_startup(app: web.Application) -> None:
    timeout = aiohttp.ClientTimeout(total=None, connect=20, sock_read=60)
    app[_CLIENT_KEY] = aiohttp.ClientSession(timeout=timeout)


async def _on_cleanup(app: web.Application) -> None:
    await app[_CLIENT_KEY].close()


def build_app(
    *,
    runtime,
    access_token: str,
    allowed_origins: set[str] | frozenset[str],
    relay_store: RelayStore | None = None,
) -> web.Application:
    token = str(access_token or "").strip()
    if len(token) < 16:
        raise ValueError("access token must contain at least 16 characters")
    origins = frozenset(_normalize_origin(origin) for origin in allowed_origins)
    if not origins:
        raise ValueError("at least one exact PWA origin is required")
    app = web.Application(middlewares=[_cors_middleware, _auth_middleware], client_max_size=256 * 1024)
    app[_RUNTIME_KEY] = runtime
    app[_TOKEN_KEY] = token
    app[_ORIGINS_KEY] = origins
    app[_RELAY_KEY] = relay_store or RelayStore()
    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    app.router.add_get("/status", _status)
    app.router.add_get("/events", _events)
    app.router.add_post("/command", _command)
    app.router.add_get("/audio/{grant}", _audio)
    return app


def load_or_create_access_token(path: Path) -> str:
    try:
        token = path.read_text(encoding="ascii").strip()
        if len(token) >= 32:
            return token
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(48)
    temporary = path.with_name("." + path.name + ".tmp")
    temporary.write_text(token, encoding="ascii")
    try:
        temporary.chmod(0o600)
    except OSError:
        pass
    os.replace(temporary, path)
    return token


def make_pairing_url(pwa_url: str, endpoint: str, access_token: str) -> str:
    pwa = urllib.parse.urlsplit(str(pwa_url).strip())
    if pwa.scheme != "https" or not pwa.netloc:
        raise ValueError("the hosted PWA URL must use HTTPS")
    api = urllib.parse.urlsplit(str(endpoint).strip())
    local_hosts = {"localhost", "127.0.0.1", "::1"}
    if api.scheme != "https" and not (api.scheme == "http" and api.hostname in local_hosts):
        raise ValueError("the public companion endpoint must use trusted HTTPS")
    fragment = urllib.parse.urlencode({"endpoint": endpoint.rstrip("/"), "token": access_token})
    return urllib.parse.urlunsplit((pwa.scheme, pwa.netloc, pwa.path or "/", pwa.query, fragment))


def _parse_origins(values: list[str], pwa_url: str) -> set[str]:
    origins = {_normalize_origin(value) for value in values if str(value).strip()}
    origins.add(_normalize_origin(pwa_url))
    return origins


def _print_pairing_qr(value: str) -> None:
    try:
        import qrcode

        qr = qrcode.QRCode(border=1)
        qr.add_data(value)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
    except Exception:
        return


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Rainette Music iPhone PWA companion")
    parser.add_argument("--host", default="127.0.0.1", help="loopback bind host")
    parser.add_argument("--port", type=int, default=_DEFAULT_PORT)
    parser.add_argument("--pwa-url", default=os.environ.get("RAINETTE_PWA_URL", ""), required=False)
    parser.add_argument("--public-url", default=os.environ.get("RAINETTE_PWA_PUBLIC_URL", ""), required=False)
    parser.add_argument("--origin", action="append", default=[])
    args = parser.parse_args(argv)

    if not args.pwa_url:
        parser.error("--pwa-url (the Vercel HTTPS URL) is required")
    public_url = args.public_url or f"http://localhost:{args.port}"
    origins = _parse_origins(args.origin, args.pwa_url)
    data_dir = _app_data_dir()
    token = load_or_create_access_token(data_dir / _TOKEN_FILENAME)
    pairing_url = make_pairing_url(args.pwa_url, public_url, token)

    artwork_dir = data_dir / "playlist-artwork"
    runtime = RainettePwaRuntime(MusicState(data_dir / "music.db"), artwork_dir=artwork_dir)
    app = build_app(runtime=runtime, access_token=token, allowed_origins=origins)

    print("Rainette iPhone companion")
    print(f"Local listener: http://{args.host}:{args.port}")
    print(f"PWA origin: {', '.join(sorted(origins))}")
    print("Pairing link (the token stays in the URL fragment and is not sent to Vercel):")
    print(pairing_url)
    _print_pairing_qr(pairing_url)
    if not args.public_url:
        print("WARNING: localhost is for testing only. An HTTPS PWA cannot reach it from another device.")
        print("Expose this port through a trusted HTTPS tunnel and restart with --public-url.")

    web.run_app(app, host=args.host, port=args.port, print=None, access_log=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
