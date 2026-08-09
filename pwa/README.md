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

1. On the computer, open Rainette → **Settings → Mobile**. Press **Download
   cloudflared** once, then **Generate HTTPS tunnel**. The desktop app runs the
   tunnel in front of its phone gateway, waits until that address actually
   answers, and fills it into *Public address for this computer*.
2. Create a pairing code there.
3. Scan the QR with the phone, or paste the pairing link into this app.
4. The phone appears in the computer's **Waiting for approval** list. Approve it.
5. The phone receives its own device token and connects.

Step 1 is what makes step 3 work. A pairing code created before the computer has
a public address carries `http://127.0.0.1:<port>`, which on a phone means the
phone itself — an HTTPS page is not permitted to call it, so the request never
leaves the device. Browsers report that only as "Failed to fetch" or "Load
failed", so this app detects it and says what to do instead.

A generated tunnel gets a fresh hostname on every start. A phone that already
holds a device credential only needs the new address, not another approval, so
re-scanning the current QR reconnects it without appearing in the waiting list
again.

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

## Capabilities

Search, library sync, queue and previous/next, recent history, lock-screen
Media Session controls, stream-expiry recovery, and an offline app shell.

Playback still needs the paired computer to be awake and online.
