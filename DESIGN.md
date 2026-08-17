# Rainette Music — standalone desktop app

A self-contained Rainette Music player that runs
on its own as a native desktop app launched by `Rainette Music.exe`.

## What it is

The full Rainette Music experience:

- Search songs, artists, and albums (YouTube Music catalog via `ytmusicapi`, streams via `yt-dlp`)
- Browse artist catalogs (songs / videos / albums / singles) and album track lists
- Playlists (create / rename / delete / add / remove / reorder-by-append)
- Saved library index (artists + albums derived from played/added tracks)
- Recently played history
- The persistent draggable liquid-glass mini-player (queue, seek, loop, prev/next)
- A **detached player window** you can drag across both monitors, with a **volume**
  slider and a **5-band graphic equalizer** (Bass / Low / Mid / High / Treble) plus
  presets (Flat, Bass Boost, Vocal, Treble)

Playback is ad-free: `yt-dlp` resolves a direct audio stream URL and the browser's
`<audio>` element streams it. Audio bytes pass through Python only when the EQ is
enabled (see "Equalizer" below).

## Architecture

```
Rainette Music.exe        PyInstaller launcher (built from main.py)
start.bat                 zero-build fallback (pythonw main.py)
main.py                   opens the native window, starts the local server
server.py                 aiohttp: serves web/ + a /ws WebSocket on one port
music_bridge.py           command handlers
transport.py              how a phone reaches this computer, as a provider seam
tunnel.py                 supervises whichever transport provider is selected
local_library.py          scans folders on this computer for music files
companion.py              pairing, device credentials, audio-relay grants
state.py                  trimmed SQLite layer (music tables only)
shared.py                 runtime-context module (STATE + notify_browsers)
music.db                  created on first run
web/                      the frontend (see below)
```

### Frontend (`web/`)

- `index.html` — minimal shell that mounts the page host + mini-player mount point
- `music_shell.js` — the standalone page shell:
  WebSocket plumbing (`sendHelper`, `helperRequest`), DOM utils (`el`, `btn`),
  the `app` state object, a `RainetteRouter` stub that auto-mounts the page, and
  (in detached mode) a `RainetteMusic` shim that forwards play/transport over the socket
- `rainette_music.js` — the music-page module with Rainette import paths
- `rainette_music_player.js` — the docked bubble engine (browser fallback)
- `miniplayer.html` / `miniplayer.js` — the detached player window: `<audio>` + queue +
  transport + the Web Audio graph (volume gain + 5-band EQ) + native window chrome
- `rainette_tokens.css`, `rainette_pages.css` — shared theme and page styles
- `app.css` — standalone shell overrides (fills the window, no nav gutter)

### Two-window model (native / pywebview path)

`main.py` opens two native windows that share state over the socket:

- **Main window** (`/?remote=1`) — search / catalog / playlists / library. It owns no
  audio; its `RainetteMusic` shim sends `music_remote_play` / `music_remote_control`.
- **Player window** (`/miniplayer.html`) — borderless, movable across monitors (drag via
  `.pywebview-drag-region`), with a pin-on-top toggle and custom minimize/hide buttons
  (exposed from Python via `WindowApi`). It owns the `<audio>` element, transport,
  volume, and EQ, and broadcasts `music_now_playing_set` so the main window stays in sync.

`music_bridge` relays `music_remote_play` / `music_remote_control` between the two windows.
In the browser / Edge-`--app` fallback (no pywebview) there is one window and the player
stays **docked in-page** via `rainette_music_player.js` (no volume/EQ there).

### Equalizer + the `/audio` proxy

Web Audio's `MediaElementSource` outputs silence for cross-origin media, so plain playback
uses the direct googlevideo URL (robust, no bytes through Python). The **first time the EQ
is enabled**, the player builds the audio graph and switches the current track to the
same-origin `/audio?u=<url>` proxy in `server.py` (which forwards `Range` so seeking still
works). From then on that session streams through the proxy so the 5 `BiquadFilter` bands
and the volume gain node take effect. WebView2 autoplay is unlocked via
`WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=--autoplay-policy=no-user-gesture-required` (set in
`main.py`) since the player window is driven remotely and never receives a direct click.

### Data flow

```
window  ⇄  ws://127.0.0.1:<port>/ws  ⇄  server.py dispatch
                                          → music_bridge handlers
                                              → yt-dlp / ytmusicapi (search + stream URL)
                                              → state.py (playlists, tracks, history)
```

The WebSocket message contract keeps the frontend modules in sync with the
native application.

### Where playback lives

One record answers "which surface owns the audio right now", and it lives on
this computer (`music_kv`, read and written through `state.py`). It is broadcast
to every paired device as `music_playback_target`, and every screen that says
"playing on ..." renders it rather than deciding for itself.

That is worth stating because the obvious alternative does not work. Each
surface used to keep its own idea: the desktop had a field nothing ever updated,
the phone had a boolean, and the wire carried a two-value string defaulted to
`"desktop"`. Nothing was authoritative, so pause could not cross devices and
every screen claimed the computer was playing regardless of the truth.

Two rules make it usable. **Starting playback claims ownership** — there is
nothing to hand over, so it needs no handshake, which is why pressing play on a
phone just works. And on an explicit transfer, **ownership moves only once the
target acknowledges it has the track**, so a handoff that fails leaves the
source playing instead of pausing into silence.

### Reaching this computer from a phone

`transport.py` is a provider seam, not a Cloudflare wrapper. Each provider
answers the same small protocol, and unfinished setup is a *result* rather than
an error: `PreflightResult` describes the next thing the person has to do, in
words, with a link, so Settings can render a checklist instead of a stack trace.

`PreflightResult.can_fix` is what turns that checklist into a wizard. A step
carrying only a `url` leaves somebody to go and do a thing in a browser and then
work out what changed; the same step with `can_fix` set is a button in Rainette
that performs it, dispatched through `TunnelManager.setup_step`. The split is
deliberate and is the whole design rule here: **anything the helper binaries can
already do, Rainette does** — `cloudflared tunnel login`, `tunnel create`,
`tunnel route dns`, `tailscale up` — and anything that is irreducibly the
person's stays a link beside the button: creating an account, approving a
sign-in, flipping a switch in a vendor's own dashboard. `setup_step` accepts
only the three named steps rather than dispatching on an arbitrary attribute, so
the settings page cannot reach into a provider through it.

`cloudflare-quick` remains the default and behaves exactly as it did before the
seam existed. The other providers are options a user may choose, never a
migration they are pushed through.

**There is deliberately no plain-LAN provider.** It has been attempted twice
(`zeroconf` in `requirements.txt` is the fossil of one) and it cannot be made to
work well: a page served over HTTPS may not call `http://192.168.x.x` at all
(mixed content), and serving the client over plain HTTP on a private address
costs it secure-context status — which means no service worker, no offline
shell, and no Add to Home Screen. `tailscale-serve` is the version that
survives: on a shared network Tailscale connects the two devices directly, but
with a real certificate. That is why it is labelled **Direct on your network**
and is the recommended option.

### Updating itself

Both desktop platforms update in place, and the root of trust for that is a
single Ed25519 signature over a schema-2 manifest, verified against the public
key committed in `version.py` before any field of the manifest is read. The
payload is then streamed against the hash that signed manifest pins, so a
swapped artifact can never reach disk under the expected name.

**Nothing in that chain involves Apple or Microsoft.** Authenticode is an
optional extra layer on Windows, enabled only when a certificate fingerprint is
pinned; notarisation on macOS buys the absence of a Gatekeeper prompt on a
*manual* download and has no bearing on whether an update verifies. This is why
macOS self-update needs no paid developer identity.

The platforms differ only in what they do with the verified bytes. Windows runs
the Inno Setup installer silently. macOS expands a `ditto -ck` archive — which
preserves the symlinks and extended attributes an `.app`'s signature is computed
over, where `zip -r` does not — then hands the swap to a detached shell that
waits for the app to exit, moves the old bundle aside, moves the new one in, and
relaunches. A failed move puts the old bundle back rather than leaving none.

The one case macOS must refuse is **app translocation**: a quarantined app run
straight from a disk image executes from a read-only shadow copy, so the swap
would fail or write somewhere that vanishes on quit. `_running_macos_bundle`
detects it and says to move the app to Applications, because otherwise the
failure is indistinguishable from the updater being broken.

## The window ("actual app")

`main.py` prefers **pywebview** (a native WebView2 window on Windows 11). If pywebview
is unavailable it falls back to **Microsoft Edge in `--app` mode** (a chromeless window),
then to the default browser. Closing the pywebview window stops the app cleanly.

## Requirements

Python 3.10+ with: `aiohttp`, `yt-dlp`, `ytmusicapi`, `pywebview` (window),
plus `pyinstaller` only to (re)build the exe. See `requirements.txt`.

## Standalone design

- The native desktop tkinter overlay is removed — the in-window mini-player covers it,
  and it avoided the known tkinter teardown crash.
- State is a small music-only SQLite DB (`music.db`).
