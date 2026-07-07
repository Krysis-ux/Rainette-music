"""Local HTTP + WebSocket server for the standalone Rainette Music app.

One aiohttp process serves the static ``web/`` frontend and a ``/ws`` WebSocket.
Incoming WS messages are routed to ``music_bridge.DISPATCH``; handler results are
broadcast back to every connected client. yt-dlp workers run on daemon threads
and push results back onto the event loop thread-safely, so the loop never blocks
on network I/O.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
from aiohttp import WSMsgType, web

import shared
import music_bridge
from state import MusicState

BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "web"
DB_PATH = BASE_DIR / "music.db"

# Ports tried in order until one binds (handles a stale instance / port clash).
PORT_RANGE = range(8777, 8788)

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
        (CRUD handlers) and from daemon threads (yt-dlp workers) alike."""
        loop = self.loop
        if loop is None:
            return
        data = json.dumps(msg)
        loop.call_soon_threadsafe(self._enqueue, data)

    def _enqueue(self, data: str) -> None:
        for queue in list(self._queues):
            queue.put_nowait(data)


hub = Hub()


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
    session: aiohttp.ClientSession = request.app["client"]
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
    app["client"] = aiohttp.ClientSession()


async def _on_cleanup(app: web.Application) -> None:
    session = app.get("client")
    if session is not None:
        await session.close()


def build_app() -> web.Application:
    app = web.Application()
    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/audio", audio_proxy)
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
    shared.configure(state=state, notify=hub.broadcast)

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


if __name__ == "__main__":
    import sys
    pref = int(sys.argv[1]) if (len(sys.argv) > 1 and sys.argv[1].isdigit()) else None
    bound_port = start(preferred=pref)
    print(f"Rainette Music server on http://127.0.0.1:{bound_port}/  (Ctrl+C to stop)")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
