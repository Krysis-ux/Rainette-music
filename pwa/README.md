# Rainette Music PWA

The phone client for Rainette Music. It installs from the browser on iPhone and
Android, and it plays music from the computer you already own.

This directory is a static site with no build step. It is mirrored to
[`Krysis-ux/music-pwa-web`](https://github.com/Krysis-ux/music-pwa-web), which is
what actually deploys to Vercel. Keep the two copies identical.

## Architecture

```text
Phone browser / Home Screen
        ↓ loads the static app once
Rainette PWA on Vercel
        ↓ authenticated HTTPS, only after the desktop approves this phone
Trusted HTTPS tunnel to the user's computer
        ↓ loopback only
Rainette desktop app (server.py companion gateway)
        ↓
music_bridge → yt-dlp / YouTube Music
```

Vercel serves the interface and nothing else. It never runs yt-dlp, never sees a
pairing credential, and never proxies audio. The computer has to be on, with
Rainette running and its tunnel up.

## Pairing

Pairing is deliberately two-sided. Possessing a link is not enough; somebody at
the computer has to approve the phone.

1. On the computer, open Rainette → **Settings → Mobile** and start a
   connection. The default, **Limited tunnel**, needs nothing: one button
   fetches Cloudflare's `cloudflared` helper, and the app runs it in front of
   its phone gateway, waits until the address actually answers, and fills it
   into *Public address for this computer*. **Private link** (Tailscale) trades
   a one-time sign-in for an address that never changes and is reachable only
   from your own devices.
2. Create a pairing code there.
3. Scan the QR with the phone, or paste the pairing link into this app.
4. The phone appears in the computer's **Waiting for approval** list. Approve it.
5. The phone receives its own device token and connects.

Step 1 is what makes step 3 work. A pairing code created before the computer has
a public address carries `http://127.0.0.1:<port>`, which on a phone means the
phone itself — an HTTPS page is not permitted to call it, so the request never
leaves the device. Browsers report that only as "Failed to fetch" or "Load
failed", so this app detects it and says what to do instead.

A Limited tunnel gets a fresh hostname on every start. That is an address
problem, not a trust problem: the credential is `(device_id, device_token)` with
no endpoint bound into it, so a phone that already holds one needs only the new
address, never another approval.

This app therefore keeps a **Reconnect** list of the computers it has paired
with, so returning to one is a tap rather than another QR code. The list and the
credentials live in this browser's own storage and are never uploaded; the
tokens are kept in a separate map from the rows, so serialising or dumping the
list cannot leak one. Choosing a provider with a stable address on the computer
removes the problem entirely.

Each approved phone gets a distinct credential and its own listening session, so
two people can search and play independently without interrupting each other.
Revoking one phone on the computer leaves the others untouched.

The endpoint and invitation travel in the link's URL fragment. Browsers never
send fragments in an HTTP request, so the host serving this page never receives
them.

## Deploying

Import the repository as its own Vercel project.

- Framework preset: **Other**
- Build command / output directory: leave both empty
- Environment variables: none

`vercel.json` already sets the security headers and service-worker scope.

Use one stable production URL for pairing. Preview deployments have different
origins; to pair against one, add that exact origin in the desktop app's Mobile
settings first. Never use a wildcard origin.

## Security boundaries

- Pairing requires explicit approval on the computer. An invitation alone grants
  nothing and expires in five minutes.
- Credentials live only in this browser's local storage.
- The desktop gateway accepts exact origins only, and rate-limits pairing.
- Audio is served through short-lived, per-device relay grants with HTTP Range
  support. Raw upstream media URLs are never sent to the phone, and a revoked
  phone's grants stop resolving immediately.
- The gateway reuses Rainette's existing music-command allowlist, so a phone
  cannot invoke arbitrary desktop functions.
- Do not port-forward the companion port. Use a trusted HTTPS tunnel.

## Layout

No build step, so the modules are loaded directly by the browser.

```text
app.js            transport, its failure diagnosis, pairing, connection, boot
src/bridge.js     the injected seam app.js installs; keeps the graph acyclic
src/state.js      shared state and pure helpers (a leaf: imports nothing)
src/player.js     the <audio>, the queue, the transport, reporting upward
src/sync.js       the long-poll loop, output transfer, desktop mirroring
src/sheets.js     the one modal surface: drag-to-dismiss, stacking, back
src/tracks.js     track rows and their swipe gestures
src/queue.js      the queue sheet: reorder, remove, up-next
src/nowplaying.js the full-screen card the mini bar expands into
src/extras.js     playlists, lyrics, sleep timer, output picker, track menu
```

`app.js` keeps transport and pairing because those are the parts that must not
be guessed at, and a release test pins their diagnosis helpers to that file.

Every module is precached by `sw.js`. **Adding one means adding it to `SHELL`
and bumping `CACHE`**, or an installed phone keeps serving the previous set —
the worker answers same-origin GETs cache-first.

## Capabilities

Search, library sync, a full now-playing card, an editable queue with
swipe-to-queue and drag reordering, playlists, lyrics, a sleep timer, shuffle
and three-state repeat, recent history, lock-screen Media Session controls,
stream-expiry recovery, and an offline app shell.

### Two sessions, or one

By default a phone runs its **own** session: what it plays is its own, and two
phones on one computer never interrupt each other.

**Linked mode** (Settings → Link to my computer, or Play on → the computer) is
the other choice: the phone stops running a session and mirrors the desktop's,
showing what the computer plays and driving that transport instead of its own.
It is opt-in per device, re-asserted on every poll — so it survives a desktop
restart — and it never affects any other paired phone.

### Play on

The desktop can hand its session to a phone, and the phone acknowledges only
once the audio has actually loaded; a failed handoff leaves the desktop playing
rather than pausing both devices into silence.

The picker also lists the computer's real audio outputs by name, including a
connected Bluetooth speaker. The phone cannot re-route the computer's audio, so
choosing one links this phone to that computer rather than pretending otherwise.

Playback still needs the paired computer to be awake and online.
