# Rainette Music

(currently in beta — please report anything broken on the issues page)

A self-contained desktop music player for Windows, with a companion web app that
puts the same library on your phone.

## Install

Download **RainetteMusicSetup.exe** from the
[latest release](https://github.com/Krysis-ux/Rainette-music/releases/latest)
and run it. The app keeps itself up to date: releases are signed, and the app
verifies each update's signature before installing it silently in the
background.

## Rainette on your phone

There is nothing to install from a store. The phone client is a web app that
runs on iPhone and Android alike:

**[music-pwa-web.vercel.app](https://music-pwa-web.vercel.app)**

Your music still lives on your computer. The phone is a remote: search, library,
and audio all come from the machine you paired with, so nothing is uploaded
anywhere and there is no account to make.

Setting it up is two steps, both in the desktop app's **Settings → Mobile**:

1. **Download cloudflared**, then **Generate HTTPS tunnel**. Your phone talks
   directly to your computer, so the computer needs an address on the internet
   that uses HTTPS. The first button fetches Cloudflare's `cloudflared` helper
   once, from Cloudflare's own release page; the second runs it, waits until the
   address actually answers, and fills that address into *Public address for
   this computer* for you. The tunnel carries Rainette's phone gateway and
   nothing else, and it closes when you close Rainette.
   If you already run a named Cloudflare tunnel, Tailscale Funnel, or your own
   reverse proxy, paste its address into that field instead and skip both
   buttons. Never port-forward the companion port on your router.
2. **Create a pairing code** and scan it with the phone.

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
pythonw main.py   # run from source (windowed)
python main.py    # run with a console for errors
python -m pytest  # test suite
```

Releases are built by CI from `v*` tags — see [release/README.md](release/README.md).
