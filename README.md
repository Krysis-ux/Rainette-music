# Rainette Music

A self-contained desktop music player for Windows, with an optional Android
companion app for remote control over your local network.

## Install

Download **RainetteMusicSetup.exe** from the
[latest release](https://github.com/Krysis-ux/Rainette-music/releases/latest)
and run it. The app keeps itself up to date: releases are signed, and the app
verifies each update's signature before installing it silently in the
background.

## Features

- Search songs, artists, and albums; browse artist catalogs and albums
- Playlists, folders, smart playlists, and a saved library
- Detached always-on-top mini player with volume and a 5-band equalizer
- Listening insights — daily, weekly, and monthly rhythm charts
- Phone remote control via secure local-network pairing (QR code)

## Development

```
pip install -r requirements.txt
pythonw main.py   # run from source (windowed)
python main.py    # run with a console for errors
python -m pytest  # test suite
```

Releases are built by CI from `v*` tags — see [release/README.md](release/README.md).
