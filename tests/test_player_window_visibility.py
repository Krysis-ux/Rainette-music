"""Real WebView2 integration test for the "play doesn't start until the mini
player is opened" bug.

Root cause (confirmed by direct measurement, not guesswork): a player window
that is ever truly .hide()'d or .minimize()'d reports document.hidden === true
via the Page Visibility API. WebView2/Chromium then throttles MEDIA RESOURCE
LOADING for that document indefinitely - audio.play() gets called and returns
a promise, but the promise never settles (neither resolves nor rejects) because
the underlying media never finishes loading (readyState stays HAVE_NOTHING).
This is a different mechanism from the user-gesture autoplay policy fixed by
_patch_webview2_autoplay(); both fixes are required.

The remedy (see main.py: PLAYER_PARK_POS, show_player, _park_player) is to
never truly hide/minimize the player window - keep it "shown" (document.hidden
stays false) but positioned far off-screen when not in use.

This test spins up the real server and real two-window pywebview/WebView2 app
(mirroring _try_pywebview exactly), injects a track into the real main-window
Recent view, and clicks that row's actual Play button while the player stays
parked off-screen. A passive WebSocket observer requires the resulting relay,
playing state, and advancing progress. Only yt-dlp's network call is stubbed
with a local WAV so the test doesn't depend on real YouTube access; everything
else is the production code path (including miniplayer.js's own
_loadCurrent()/_applyAndPlay()).

Skipped on non-Windows or when pywebview/WebView2 aren't available, since this
exercises the real native window stack.
"""
import asyncio
import base64
import io
import json
import struct
import sys
import threading
import time
import wave
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

pytest.importorskip("aiohttp")
webview = pytest.importorskip("webview")

if sys.platform != "win32":
    pytest.skip("player window visibility behavior is WebView2/Windows-specific", allow_module_level=True)

import aiohttp  # noqa: E402
import main  # noqa: E402
import music_bridge  # noqa: E402
import server  # noqa: E402
import shared  # noqa: E402


def _valid_wav_data_url(seconds: float = 2.0, rate: int = 4000) -> str:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(struct.pack("<h", 0) * int(rate * seconds))
    return "data:audio/wav;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


AUDIO_URL = _valid_wav_data_url()
PROBE_TRACK = {
    "id": "trk_probe",
    "source": "youtube",
    "source_id": "probe-track",
    "title": "Probe",
    "artist": "Probe",
    "duration_s": 2,
    "thumbnail_url": "",
}


async def _observe_playback(port: int, token: str, timeout_s: float,
                            ready: threading.Event) -> list[dict]:
    """Passively observe the row click's relay and resulting playback.

    The probe must not send ``music_remote_play`` itself: doing so would bypass
    the exact UI path this regression is meant to protect.
    """
    url = f"ws://127.0.0.1:{port}/ws?token={token}"
    events: list[dict] = []
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(url) as ws:
            ready.set()
            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                try:
                    msg = await asyncio.wait_for(
                        ws.receive_json(), timeout=min(0.5, max(0.1, deadline - time.monotonic()))
                    )
                except (asyncio.TimeoutError, TypeError, ValueError):
                    continue
                if not isinstance(msg, dict):
                    continue
                msg_type = msg.get("type")
                if msg_type not in ("music_remote_play", "music_now_playing", "music_progress"):
                    continue
                source_id = msg.get("source_id") or ""
                if msg_type == "music_remote_play":
                    tracks = msg.get("tracks") if isinstance(msg.get("tracks"), list) else []
                    index = int(msg.get("index") or 0)
                    if 0 <= index < len(tracks) and isinstance(tracks[index], dict):
                        source_id = tracks[index].get("source_id") or ""
                elif msg_type == "music_now_playing" and isinstance(msg.get("track"), dict):
                    source_id = msg["track"].get("source_id") or ""
                events.append({
                    "type": msg_type,
                    "source_id": source_id,
                    "playing": msg.get("playing"),
                    "current_time": msg.get("current_time"),
                })
                if _advanced(events):
                    break
    return events


def _click_probe_row(main_window):
    """Render a genuine track row in the real main window and click its Play button."""
    message = json.dumps({"type": "music_recent_result", "ok": True, "tracks": [PROBE_TRACK]})
    return main_window.evaluate_js(f"""
        (() => {{
            const recent = document.querySelector('#rwMusicTabs [data-tab="recent"]');
            if (!recent) return 'Recent tab did not mount';
            recent.click();
            document.dispatchEvent(new CustomEvent('rainette:helper-message', {{ detail: {message} }}));
            const row = [...document.querySelectorAll('.rw-track-card')]
                .find(card => card.dataset.trackKey === 'youtube:probe-track');
            const play = row?.querySelector('.rw-play-action');
            if (!play) return 'Probe track Play button did not render';
            play.click();
            return true;
        }})()
    """)


def _run_hidden_playback_probe(reveal_before_play: bool) -> dict:
    """Boots the real two-window app (mirroring main._try_pywebview) and clicks a
    real main-window track row while the player is parked off-screen. If
    reveal_before_play is True, the window is moved on-screen first, as a
    positive control proving the harness itself can detect genuine playback."""
    port = server.start()
    token = server.APP_TOKEN
    url = f"http://127.0.0.1:{port}/"

    def stub_stream_url(msg):
        shared.notify_browsers({
            "type": "music_stream_url_result", "id": msg.get("id"), "ok": True,
            "track_id": msg.get("track_id") or "", "source_id": msg.get("source_id") or "",
            "url": AUDIO_URL, "expires_hint_s": 3600, "cached": False,
            "title": "Probe", "artist": "Probe", "duration_s": 2, "thumbnail_url": "",
        })

    music_bridge.DISPATCH["music_stream_url"] = stub_stream_url
    main._patch_webview2_autoplay()

    token_q = main.urllib.parse.quote(token, safe="")
    api = main.WindowApi()
    main_window = webview.create_window(
        main.WINDOW_TITLE, f"{url}?remote=1&token={token_q}", js_api=api,
        width=main.WINDOW_SIZE[0], height=main.WINDOW_SIZE[1], min_size=main.MIN_SIZE,
    )
    player_window = webview.create_window(
        "Player", f"{url}miniplayer.html?token={token_q}", js_api=api,
        width=main.PLAYER_SIZE[0], height=main.PLAYER_SIZE[1], min_size=main.PLAYER_SIZE,
        x=main.PLAYER_PARK_POS[0], y=main.PLAYER_PARK_POS[1],
        frameless=True, easy_drag=False, resizable=False,
        shadow=False, background_color="#FFFFFF",
    )
    api.bind_main(main_window)
    api.bind_player(player_window)
    main_loaded = threading.Event()
    player_loaded = threading.Event()
    main_window.events.loaded += lambda *_: main_loaded.set()

    # Mirror production's lifecycle exactly: changing ShowInTaskbar before the
    # second WebView2 controller finishes loading aborts that controller.
    def on_player_loaded(*_):
        api._hide_player_from_taskbar()
        player_loaded.set()

    player_window.events.loaded += on_player_loaded

    result: dict = {"events": [], "click_result": None, "error": ""}

    def on_started():
        try:
            api.player_hide()
            api._shape_player()
            if not main_loaded.wait(timeout=10):
                result["error"] = "main WebView2 did not reach loaded within 10 seconds"
                return
            if not player_loaded.wait(timeout=10):
                result["error"] = "player WebView2 did not reach loaded within 10 seconds"
                return
            if reveal_before_play:
                api.reveal_player()
            time.sleep(0.2)

            ws_result: dict = {}
            ws_ready = threading.Event()

            def ws_thread():
                try:
                    ws_result["events"] = asyncio.run(
                        _observe_playback(port, token, timeout_s=6.0, ready=ws_ready)
                    )
                except Exception as exc:  # diagnostics for a failed native probe
                    ws_result["error"] = repr(exc)
                    ws_ready.set()

            t = threading.Thread(target=ws_thread, daemon=True)
            t.start()
            if not ws_ready.wait(timeout=3):
                result["error"] = "playback observer WebSocket did not connect within 3 seconds"
                return
            if ws_result.get("error"):
                result["error"] = f"playback observer failed: {ws_result['error']}"
                return
            result["click_result"] = _click_probe_row(main_window)
            t.join(timeout=8)
            result["events"] = ws_result.get("events", [])
            if ws_result.get("error"):
                result["error"] = f"playback observer failed: {ws_result['error']}"
        except Exception as exc:
            result["error"] = repr(exc)
        finally:
            main_window.destroy()
            player_window.destroy()

    webview.start(on_started)
    return result


def _advanced(events: list[dict]) -> bool:
    relayed = any(
        e.get("type") == "music_remote_play" and e.get("source_id") == PROBE_TRACK["source_id"]
        for e in events
    )
    playing = any(
        e.get("type") == "music_now_playing"
        and e.get("source_id") == PROBE_TRACK["source_id"]
        and e.get("playing") is True
        for e in events
    )
    times = [
        e["current_time"] for e in events
        if e.get("type") == "music_progress"
        and e.get("source_id") == PROBE_TRACK["source_id"]
        and isinstance(e.get("current_time"), (int, float))
    ]
    return relayed and playing and len(times) >= 2 and max(times) > 0.3


@pytest.mark.slow
def test_audio_plays_while_player_window_stays_parked_offscreen():
    """The actual reported bug: play a track and never open the mini player."""
    result = _run_hidden_playback_probe(reveal_before_play=False)
    assert not result["error"], result["error"]
    assert result["click_result"] is True, result["click_result"]
    assert _advanced(result["events"]), (
        "clicking a real track row never produced relayed, advancing playback while "
        f"the player stayed parked off-screen. Broadcasts observed: {result['events']}"
    )


@pytest.mark.slow
def test_audio_plays_when_player_window_is_revealed():
    """Positive control: proves the harness itself can detect real playback,
    so a pass on the hidden case above isn't a false negative from a broken probe."""
    result = _run_hidden_playback_probe(reveal_before_play=True)
    assert not result["error"], result["error"]
    assert result["click_result"] is True, result["click_result"]
    assert _advanced(result["events"]), (
        f"positive control failed to detect row-click playback: {result['events']}"
    )
