# Rainette Music for Android

The same phone interface as the PWA (`../pwa`, packaged locally, never loaded
from the web), over a player that runs on the phone itself. It searches and
plays with no computer: search and stream resolution use
[NewPipeExtractor](https://github.com/TeamNewPipe/NewPipeExtractor) on the
device, and audio is played by a Media3 service that keeps going with the
screen off. A paired computer is optional and adds its library on top.

Licensed **GPL-3.0-only** (see `LICENSE`), because it includes NewPipeExtractor.

## How it fits together

```text
pwa/ (shared UI)  ── player.js ── owner? ──► <audio> element   (phone files; the browser)
                                   └──────► native-engine.js ──► RainettePlayer plugin
                                                                  └─► PlaybackService (Media3)
                                                                       ├─ ResolvingDataSource → StreamResolver (NewPipe, on device)
                                                                       └─ ChunkedDataSource (1 MB ranges; unranged reads are throttled)
```

- `pwa/src/source-provider.js` decides which engine owns a queue: the native
  service when it can stream every track itself, the `<audio>` element
  otherwise. Exactly one owner at a time; `player.js` silences one before the
  other starts.
- The page names tracks by id. It never gives the service a URL: the service
  derives `rainette://youtube/<id>` and resolves it when the player opens it,
  so queue advance needs no JavaScript and no awake WebView.
- `play` resolves only once the track has loaded, which is what desktop
  handoffs wait on before pausing the computer.
- Expired or revoked stream URLs (HTTP 403/410) are re-resolved at most twice;
  network loss is retried with backoff (2, 4, 8, 16, 30 s) and then reported.
- Only trusted controllers (this app, system UI, Bluetooth) can connect to the
  media session.

## Build

Requires JDK 21 and an Android SDK with platform 36.

```bash
cd mobile && npm ci && npx cap sync android
```

```bash
cd mobile/android && ./gradlew assembleDebug testDebugUnitTest
```

The debug build installs as `com.krysisux.rainettemusic.dev`, beside any
existing install. No release signing is configured yet: see `AUDIT.md` §2 for
the old app's key and the upgrade question it raises.

## Tests

- `app/src/test`: JVM tests for the chunked reader, stream selection, expiry,
  error classification and the URI rules (`./gradlew testDebugUnitTest`).
- `tests/test_pwa_native_app.py`: the real PWA against a fake native bridge
  (engine choice, one owner, stale answers, transport, error wording).
- `tests/test_mobile_policy.py`: dependency allowlist (no ads, analytics or
  paid services), no other servers, license, signing hygiene.

## Verified so far (emulator only)

`scripts/emulator_check.py` drives the debug build over Chrome DevTools and
reads playback from Android's own `dumpsys media_session` and `dumpsys audio`.
On an Android 15 (API 35) arm64 emulator, Google APIs image, host audio off,
all 20 checks pass:

- The app opens into music with no pairing screen.
- On-device search answers in ~1.1 s.
- Playback starts in 2–3 s, and Android reports the app's AudioTrack started.
- The `<audio>` element stays silent while the service plays.
- A play request resolves only once its track has loaded.
- Repeated PLAY and PAUSE media keys stay absolute.
- The next track starts with the app backgrounded and the screen off.
- The media notification is posted.
- Playback recovers ~2 s after the network returns.
- Status bar icons are visible on dark and light themes.

```bash
python mobile/scripts/emulator_check.py out/
```

## Verified on a physical phone

A Samsung Galaxy Note 8 (SM-N950U, Android 9 / API 28), with Chrome 103 as
its WebView provider. The same script passes every automated check:

- The app boots into music.
- Status bar icons are visible on dark and light themes.
- On-device search in 1.7–1.9 s; playback starts in about 4 s.
- Android reports the AudioTrack started; the `<audio>` element stays silent.
- A play request resolves only once its track has loaded.
- Repeated PLAY and PAUSE keys stay absolute.
- The next track starts with the app backgrounded and the screen off.
- The media notification is posted.
- Recovery takes 0.2–9.5 s after Wi-Fi and data return.
- Coming back to the app shows the service's current track. This was checked
  by hand after unlocking: the automated step needs the secure lock screen
  dismissed, and the script waits for that.

The phone found a bug the emulator and the fake bridge both missed:

- **Symptom:** the page never heard the service's events.
- **Cause:** Capacitor wipes its plugin listeners on `onPageStarted`. On this
  phone that callback arrived after the page had registered its listeners.
- **Fix:** events now travel through a plugin-owned `watch` channel.
- **Guard:** the fake bridge simulates the reset.

**AVG antivirus on this phone flagged the debug build as "Suspicious app
activity".** Expect the same from some antivirus apps for any public APK:
YouTube-extraction apps, NewPipe included, are commonly flagged.

## Not done yet

- **On the phone, by hand:**
  - Lock-screen buttons.
  - A Bluetooth headset or car.
  - Unplugging wired headphones.
  - A phone call interruption.
  - Long background periods under Samsung's battery manager.
  - A local file played offline.
- **Release:** release-signed build; upgrade from 0.2.6.
- Pairing from the app needs a computer running a Rainette release that
  accepts the app's origin, `https://rainette.localhost` (added in
  `server.py`, unreleased). Not yet verified end to end.
- Phone files can't join a queue the service is streaming. They play through
  the element, which, unlike the service, depends on the WebView staying awake.
- Artist pages, lyrics and the equaliser still need a computer, or are hidden.
- Release signing, CI workflow, website download: not started (see the plan).
