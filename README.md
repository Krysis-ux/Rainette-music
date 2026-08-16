# Rainette Music

(currently in beta — please report anything broken on the issues page)

A self-contained desktop music player for Windows and macOS, with a companion
web app that puts the same library on your phone.

## Install

### Windows

Download **RainetteMusicSetup.exe** from the
[latest release](https://github.com/Krysis-ux/Rainette-music/releases/latest)
and run it. The app keeps itself up to date: releases are signed, and the app
verifies each update's signature before installing it silently in the
background.

### macOS

There is no prebuilt download yet — run it from this folder (macOS 13+, Apple
Silicon or Intel).

**Easiest:** double-click **`Start Rainette Music.command`** in Finder. It opens
a Terminal window, sets everything up on first run, and starts the app. Keep
that window open while you listen; closing it stops the app.

The first launch takes a few minutes while it downloads dependencies. Later
launches are immediate.

Equivalently, from a terminal:

```bash
./run-macos.sh
```

If something doesn't work, this prints a setup check and the last lines of the
log instead of failing silently:

```bash
./run-macos.sh --doctor
```

And if playback looks like it is working but you hear nothing, this plays a test
tone through the exact same path Rainette uses, then asks macOS whether the
sound really reached your output device — which separates "the app is broken"
from "the audio is going to a device you aren't listening to":

```bash
./run-macos.sh --test-audio
```

To build a double-clickable `Rainette Music.app` instead:

```bash
./release/build-macos-release.sh
```

The bundle lands in `release/out-macos/`. It is ad-hoc signed, which is enough
to run it on your own machine; shipping it to other people needs an Apple
Developer ID and notarization. See
[release/README-macos.md](release/README-macos.md) for the details, including
exactly which steps need a paid Apple account and which don't.

The built-in updater only installs the signed Windows release, so a macOS build
updates by rebuilding rather than in place.

## Rainette on your phone

There is nothing to install from a store. The phone client is a web app that
runs on iPhone and Android alike:

**[music-pwa-web.vercel.app](https://music-pwa-web.vercel.app)**

Your music still lives on your computer. The phone is a remote: search, library,
and audio all come from the machine you paired with, so nothing is uploaded
anywhere and there is no account to make.

Setting it up is two steps, both in the desktop app's **Settings → Mobile**:

1. **Choose how your phone reaches this computer**, then start it. Your phone
   talks directly to your computer, so the computer needs an address on the
   internet that uses HTTPS. Rainette offers several ways to get one, and they
   trade setup against how long the address lasts:

   | | What it costs you | What you get |
   |---|---|---|
   | **Limited tunnel** *(default)* | Nothing. One button fetches Cloudflare's `cloudflared` helper from Cloudflare's own release page. | Works immediately. The address changes every time Rainette restarts, so the phone has to scan a new code. |
   | **Private link** *(recommended)* | The free Tailscale app, on this computer and on your phone, signed in once. | A permanent address with a real certificate, reachable **only from your own devices** — the gateway is never exposed to the open internet. |
   | **High-quality tunnel** | The same Tailscale sign-in, plus a one-time consent. | A permanent address that anyone on the internet can reach. Only worth it for a guest's phone that will not install Tailscale. |
   | **Your own Cloudflare tunnel** | A Cloudflare account and a domain already on Cloudflare. | A permanent address you control. |
   | **Bring your own address** | You already run a reverse proxy or VPS. | Paste its address and skip the buttons entirely. |

   Whichever you pick, the tunnel carries Rainette's phone gateway and nothing
   else. Never port-forward the companion port on your router.
2. **Create a pairing code** and scan it with the phone.

A phone remembers the computers it has paired with, so coming back is a tap
rather than another QR code — the credential never expires, only the address
changes. On a Limited tunnel that address moves on every restart, which is the
one thing the other options buy you.

Pairing is two-sided on purpose. The phone shows up in a waiting list and gets
access only once you approve it there. Each approved phone has its own listening
session, so two people can search and play at the same time without interrupting
each other, and you can revoke one phone without touching the rest.

A generated tunnel gets a fresh address every time it starts. An already-paired
phone just needs the new address, not a new approval, so re-scanning the current
QR code reconnects it in one step.

The phone client's source lives in [`pwa/`](pwa/) and is mirrored to
[`Krysis-ux/music-pwa-web`](https://github.com/Krysis-ux/music-pwa-web), which is
what deploys to Vercel.

## Features

- Search songs, artists, and albums; browse artist catalogs and albums
- Playlists, folders, smart playlists, and a saved library
- Detached always-on-top mini player with volume and a 5-band equalizer
- Lyrics, and listening insights with daily, weekly, and monthly rhythm charts
- Phone access through the Rainette Music PWA, with per-device pairing

## Development

```
pip install -r requirements.txt
pythonw main.py   # Windows: run from source (windowed)
python main.py    # run with a console for errors
python -m pytest  # test suite
```

On macOS use `./run-macos.sh` (there is no `pythonw`); `requirements.txt` already
pulls in the pyobjc frameworks pywebview needs there. The suite runs the same
way on both platforms — the Windows-specific window and updater tests pin their
own platform branch so they keep guarding the Windows build when run on a Mac,
and `tests/test_macos_desktop.py` covers the macOS branch.

Releases are built by CI from `v*` tags — see [release/README.md](release/README.md)
for Windows and [release/README-macos.md](release/README-macos.md) for macOS.
