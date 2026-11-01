# Rainette Music for Android and iPhone

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

The app ID is `com.krysisux.rainette`, deliberately not the 0.2.6 app's
`com.krysisux.rainettemusic`. That app's signing key is not available, so this
installs beside it rather than claiming to be an update. Anyone with 0.2.6
uninstalls it by hand. Debug builds add `.dev`.

## Releasing

The release key is `rainette-android-release.p12`, with its password in
`rainette-android-release.password`. The maintainer keeps both, outside the
repository. It is RSA 4096, valid to 2054. Its certificate fingerprint is
committed in `android/release-cert.sha256`. A local release build points
`RAINETTE_ANDROID_KEYSTORE` at the `.p12` and reads the password into
`RAINETTE_ANDROID_KEYSTORE_PASSWORD`.

**Back both files up somewhere safe.** If they are lost, no future build can
update an installed copy: every user would have to uninstall first.

CI signs with two repository secrets made from those files:
`ANDROID_RELEASE_KEYSTORE_BASE64` and `ANDROID_RELEASE_KEYSTORE_PASSWORD`.

1. Push a `vMAJOR.MINOR.PATCH` tag. The *Android app* workflow
   (`.github/workflows/android.yml`) builds the signed APK and verifies it: the
   signer against the committed fingerprint, package
   `com.krysisux.rainette`, the version from the tag, not debuggable. It keeps
   the APK and its SHA-256 as the `rainette-android-release` artifact. Nothing
   is published.
2. Download that artifact and install it on a phone. Run
   `python mobile/scripts/release_smoke.py rainette-music-android.apk out/`
   (this one needs the phone unlocked), then the hands-on checks below.
3. Only then, run the workflow manually with that run's id and the tag. It
   re-verifies the same file and attaches it, with its checksum, to the GitHub
   release.
4. Only after the asset exists: show the Android download on the website (a
   separate change).

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

By hand on the same phone, with a 1-second state recorder watching:

- **Lock-screen controls:** Pause, Play and Next all work while locked.
- **Long background:** locked for 11 minutes on the native player, the queue
  advanced through three songs by itself and was still playing at the end.
- **Offline phone file:** with airplane mode on, an imported file (a generated
  test tone) played. The native player was stopped first, then the element
  played the file.
- **Mixed lists (bug found and fixed):** after one imported file, Recently
  Played was a mixed list, and tapping a streamed song in it went to the
  element. Android still advanced that queue in the background on this phone,
  but there were no lock-screen controls and no native network recovery.
  Queues are now one kind of track: the tapped track's kind decides.

The **signed release build** (0.5.2, `com.krysisux.rainette`, not
debuggable) passes `scripts/release_smoke.py` on the same phone, through its
real UI:

- It opens into music.
- On-device search works.
- Tapping a song's artwork starts it on the native player in 3.5 s.
- It keeps playing with the screen off.

That run found a bug in the shared interface: tapping a song's cover did
nothing, in the browser too. Fixed: the cover `<img>` lacked the `track-art`
class that lets taps through.

**AVG antivirus on this phone flagged the debug build as "Suspicious app
activity".** Expect the same from some antivirus apps for any public APK:
YouTube-extraction apps, NewPipe included, are commonly flagged.

## iPhone

The same interface and the same plugin contract (`RainettePlayer`, protocol 1)
over an iOS player, so `pwa/src/native-engine.js` runs unchanged.

- `ios/RainettePlayer/`: a Swift package with the player. It builds for macOS
  too, so its tests run on a Mac with no iPhone.
  - `MusicSearch`: YouTube Music's own song search, asked directly.
  - `StreamResolver`: [YouTubeKit](https://github.com/alexeichhorn/YouTubeKit)
    0.4.10 (MIT), **local extractor only**. Its remote fallback is never
    enabled; `tests/test_mobile_policy.py` fails the build if it is.
  - `ChunkedLoader`: serves AVPlayer in 1 MB ranges through a private
    `rainette-yt://` address. Expired addresses are re-resolved at most twice;
    a lost connection is retried at 2, 4, 8, 16 and 30 s.
  - `PlayerEngine`: the queue, repeat, previous and next rules, and recovery
    as soon as the network returns.
  - `NowPlaying`: the lock screen, Control Center and headphone buttons, plus
    the audio session (calls and unplugged headphones pause it).
- `ios/App/App/RainettePlayerPlugin.swift`: the Capacitor plugin, method for
  method what `RainettePlayerPlugin.kt` is.

```bash
cd mobile && npx cap copy ios
```

```bash
cd mobile/ios/RainettePlayer && swift test
```

```bash
cd mobile/ios/RainettePlayer && RAINETTE_LIVE=1 swift test --filter AgainstTheSource
```

The live tests search, resolve, fetch 1 MB and play two songs muted, all from
the Mac. On 2026-10-31 they passed: search in 0.8 s, 1 MB in 0.36 s, the first
song loaded in 6.2 s, then next and a seek to 2:00 both played.

Installing on a phone needs an Apple ID signed in to Xcode (Settings →
Accounts; a free one works), the phone plugged in once, trusted, and in
Developer Mode. A free Apple ID's build stops opening after 7 days and is
reinstalled from the Mac. It cannot go on the App Store (guideline 5.2.3).

**Not run on an iPhone yet:** everything above that needs one, including the
lock screen, background playback, calls, Bluetooth and the WebView itself.

## Not done yet

- **On the phone, not run yet:**
  - A Bluetooth headset or car.
  - Unplugging wired headphones.
  - A phone call interruption.
- A mixed list no longer plays straight through from a phone file into a
  streamed song. Each tap plays one kind of track.
- **Release:** release-signed build; upgrade from 0.2.6.
- Pairing from the app needs a computer running a Rainette release that
  accepts the app's origin, `https://rainette.localhost` (added in
  `server.py`, unreleased). Not yet verified end to end.
- Phone files can't join a queue the service is streaming. They play through
  the element, which, unlike the service, depends on the WebView staying awake.
- Artist pages, lyrics and the equaliser still need a computer, or are hidden.
- Release signing, CI workflow, website download: not started (see the plan).
