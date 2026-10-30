# Native mobile: Phase 0 audit

Baseline for the native Android/iOS work on `feat/native-mobile`. Everything here was observed on 2026-10-27; nothing below is inferred from older docs.

## 1. Baseline

| Repository | Branch | SHA |
|---|---|---|
| Krysis-ux/Rainette-music | main | `f59e8acc0407c52f4e8787a477c6ff289daec667` (v0.5.1) |
| Krysis-ux/music-pwa-web | main | `0cf736606a184655bb59bd212d57046d3e442499` |
| Krysis-ux/Rainette-music-website | main | `07b78105ecfb5f385a432d054d17cccb47ef98fe` |

- `pwa/` and the mirror are byte-identical except `README.md` (and the mirror's own `.github/`).
- CI on main is green: `PWA companion checks` and `Publish Rainette release` for v0.5.1.
- Unmerged local branches exist (`feat/native-macos-ui` +1, `fix/playback-and-background-audio` +2). They are not touched by this work.

### Releases

Every GitHub release from v0.2.3 to v0.5.1 carries Windows and (from v0.2.8) macOS assets. **No release has ever carried an APK.** The repository's only Actions secret is `UPDATE_SIGNING_KEY`; the `ANDROID_*` secrets the old workflow expected were never set.

### Tests (macOS 27, Python 3.12.13, Playwright 1.64.1 Chromium)

```
python -m pytest -q   →  613 passed, 3 skipped, 860 subtests passed in 164.75s
```

The three skips are one WebView2/Windows-only module (`tests/test_player_window_visibility.py`) and two opt-in live canaries (`tests/test_stream_canary.py`). Run with `RAINETTE_LIVE_CANARY=1`, the canaries pass (2 passed). One subcase, `dQw4w9WgXcQ`, was sub-skipped because upstream returned 403 to this host, so the desktop resolver works against real YouTube today with one source refused. The full suite gives the same result on `feat/native-mobile` with this directory added. `node --check` passes for `pwa/app.js`, `pwa/sw.js` and every `pwa/src/*.js`. No failures existed before this work, so any failure from here on is introduced by it.

### Screenshots

98 PWA screenshots at 360, 390, 412 and 768 CSS px, Midnight and Light themes, plus reduced motion: first launch, home, search, results, library, empty local library, playlists, mini-player, now playing, queue, settings. They were captured against the behaviour suite's `FakeComputer` with zero page errors in all 17 sessions. They are kept outside the repo (`rainette-work/baseline/screens-before/`, about 98 PNGs at 2×), together with the script that made them so the "after" set is captured the same way.

## 2. The missing Android source

The website's `docs/RELEASE_HANDOFF.md` documents `mobile/build-release.ps1`, but `mobile/` was **deliberately deleted** in `e9dfa3d` (2026-08-08, "refactor: remove the Android companion app"). That commit records the reason: the PWA replaced it, and the Capacitor app "was the buggiest surface in the project". It was a remote for the desktop, not a standalone player.

The APK still sitting at `Rainette-music-website/public/downloads/rainette-music-android.apk` (SHA-256 `485804b9…7e60e`, 6.8 MB) is that app:

| Field | Value |
|---|---|
| Package | `com.krysisux.rainettemusic` |
| Version | `0.2.6`, versionCode `3`, minSdk 23, targetSdk 35 |
| Shell | Capacitor, `webDir: ../web` (the desktop UI), custom `RainetteCompanionPlugin` |
| Signer | `CN=Rainette Music Local Test`, RSA 3072, valid to 2036 |
| Cert SHA-256 | `28:3E:FA:33:21:B1:73:7B:44:33:47:FB:29:07:2A:54:95:12:98:76:90:88:84:CF:75:5E:32:13:D6:A0:53:C9` |
| Signature schemes | v1 + v2 |
| Keystore | `%LOCALAPPDATA%\Rainette Music\signing\rainette-local-test.jks` on the Windows PC. **Not on this Mac** (searched). |

### Can a new APK update an existing install?

Only if it is signed with that same key. Android refuses an update whose certificate differs from the installed copy (`INSTALL_FAILED_UPDATE_INCOMPATIBLE`), with no workaround short of uninstalling. The options are:

1. **Recover the Windows keystore** and keep `com.krysisux.rainettemusic`. This gives a true in-place upgrade from 0.2.6, with versionCode > 3. The key's CN says "Local Test", but Android does not care, and it is valid until 2036. It must then become a GitHub secret and be backed up; losing it again strands every user.
2. **New key, new package ID** (e.g. `com.krysisux.rainette`). This installs side by side with the old app, and users remove 0.2.6 themselves. Nothing is misrepresented.
3. **New key, same package ID.** Installs fail over 0.2.6 with a confusing error. Not recommended.

**Decision (2026-10-30): option 2.** The app is `com.krysisux.rainette`, signed by a new release key (certificate SHA-256 in `mobile/android/release-cert.sha256`). It installs beside 0.2.6 and never claims to update it.

The old app held only desktop-pairing state (it was a remote), so there is little user data to migrate out of it. Whatever the choice, the new app must never be described as an in-place update unless option 1 is verified on a device that has 0.2.6 installed.

## 3. Website state

- The page renders only Windows and macOS rows. Android was removed from the UI in `b90f9c2`, but `src/release.ts` still models an `android` target pointing at `releases/latest/download/rainette-music-android.apk`, **an asset that has never existed**. It is not rendered, so nothing is broken today, but it must not be rendered before a real asset exists.
- **Pre-existing defect:** `public/release-manifest.json` has no `macos` key, so `validManifest()` rejects it and the page always uses `releaseFallback`. Downloads still work, because the fallback is correct.
- **Pre-existing hazard:** the stale 0.2.6 APK is still publicly served at `/downloads/rainette-music-android.apk`, unlinked. It wraps the old desktop UI and is not expected to work with 0.5.x.
- `PRODUCT.md` still says "Windows computer or Android phone", and `RELEASE_HANDOFF.md` still documents the removed Android build.

## 4. Toolchain on this Mac

| Tool | State |
|---|---|
| Xcode | 27.0 installed, but `xcode-select` points at CommandLineTools. Usable via `DEVELOPER_DIR`. **No iOS simulator runtime installed.** No iPhone attached. |
| Android SDK | platform 35, build-tools 34/35, platform-tools. **No emulator, no system image, no device attached.** |
| JDK | OpenJDK 21 (and 27, pulled in by Homebrew Gradle) |
| Gradle | 9.8.2 (installed for the PoC) |

Every "real device" acceptance item in the plan is therefore **not run** until a device is connected.

## 5. Compatibility inventory

What must keep working, where it lives, and what already guards it. "Risk" is the risk from the native mobile work specifically.

| Behaviour | Where | Guarded by | Risk |
|---|---|---|---|
| Desktop playback (Windows, macOS native, Classic) | `main.py`, `server.py`, `music_bridge.py`, `web/`, `macos/` | `test_window_api`, `test_music_bridge_features`, `test_macos_desktop`, `test_browser_ui_smoke` | Low. Mobile work must not touch these. |
| Phone relay `/audio` grants, cert roots | `server.py`, `shared.py` | `test_audio_relay_identity`, `test_trust_roots` | Low. The native path does not use the relay. |
| Pairing, tunnels, sessions | `companion.py`, `server.py`, `transport.py`, `pwa/app.js` | `test_companion*`, `test_pwa_session_e2e`, `test_transport`, `test_tunnel` | Medium. The native app must keep pairing optional but intact. |
| PWA playback: one `<audio>`, prefetch 2 ahead, bounded re-resolve (2 retries, 30 s refill) | `pwa/src/player.js` | `TestBackgroundPlayback`, `TestTransport`, `test_a_failed_play_clears_the_loading_flag` | **High.** This is where the native engine plugs in. |
| Absolute play/pause (CarPlay loop) | `player.js` `play`/`pause`/`wireMediaSession`, `applyRemoteVerb` | `TestCarTransportCommands`, `test_the_desktop_states_intent_rather_than_flipping` | **High.** Native media controls must keep these semantics. |
| iOS audio-session category, attached `<audio>` | `player.js` `declarePlaybackSession`, `attachAudio` | `TestAudioSessionCategory` | Medium |
| Lock-screen Media Session metadata/position | `player.js` | `test_the_lock_screen_is_told_what_is_playing` | **High.** Native owns this when native plays. |
| Linked mode (mirror the desktop) | `player.js` `isLinked`, `sync.js` | `test_a_linked_phone_*`, `test_unlinking_stops_the_mirror` | Medium |
| Output handoff, ack only after load | `player.js` `adoptTransfer`, `sync.js` | `test_phone_client_answers_a_transfer`, `test_ownership_moves_only_once_the_target_has_the_track`, `test_desktop_output_transfer_ack_waits_for_media_load` | **High.** A native player must ack only once it has loaded. |
| Playback-target ownership broadcast | `target.js`, `player.js` `claimPlaybackHere` | `test_starting_playback_is_itself_the_claim`, `test_a_phone_cannot_claim_playback_for_another_device` | Medium |
| Local imports (IndexedDB `rainette-local`/`tracks`, blob playback) | `pwa/src/local.js`, `import.js` | `TestDownloadedMusicWithoutPairing` | **High.** Blob URLs must never go through `mediaUrl()`. |
| Downloads (resume, no pairing) | `pwa/src/downloads.js` | `TestDownloadsSurviveABadLink`, `test_track_downloads` | Medium |
| Playlists, queue, history, play counts | `playlists.js`, `queue.js`, `state.js` | `test_music_state_features`, client tests | Medium. Stable `trackKey` identity must hold across engines. |
| Settings/prefs sync, per device | `prefs.js`, `prefsync.js` | `test_one_phones_settings_never_reach_another` and neighbours | Low |
| Equalizer / volume (Web Audio graph) | `audio.js`, `eq.js` | `TestAudioSessionCategory` (volume slider cases) | **High.** The Web Audio EQ cannot shape native audio, so native needs its own or must hide the control honestly. |
| Artwork | `state.js` `artworkUrl`, `local.js` `localArtworkUrl` | `test_server_artwork`, contract tests | Low |
| Backup/restore (no audio files) | `pwa/src/backup.js` | none dedicated | Medium. New tests are needed before reuse. |
| Service-worker cache digest | `pwa/sw.js` | `test_the_cache_name_matches_the_files_it_would_hold`, `test_every_client_module_is_precached` | **High.** Every new module must be added to `SHELL` and the digest updated. |
| PWA install (manifest, Add to Home Screen) | `index.html`, `manifest.webmanifest`, `app.js` install dialog | contract tests | Low |
| Existing Android installs (0.2.6) | none, source deleted | none | See §2. |

## 6. Android extractor proof of concept

`mobile/poc/newpipe-jvm/` runs NewPipeExtractor **v0.26.5** (pinned, latest release, 2026-08-15) on a plain JVM, with the JDK's own HTTP client as its downloader. No Rainette server and no third-party server sit in the path. Logs: `run-1.log` to `run-3.log`.

| Check | Result |
|---|---|
| YouTube Music song search ("daft punk one more time") | 20 songs in ~1.0 s, correct titles/artists/durations |
| Resolve three consecutive tracks | 1.3–2.1 s each; audio-only AAC (itag 139/140) and Opus (249/250/251), all progressive HTTP |
| Stream URL lifetime | `expire` ≈ 6 h after resolution |
| Fetch audio | `audio/mp4`, starts with an MP4 `ftyp` box |
| Decode | All three full downloads decode with CoreAudio to 320.3 s / 366.1 s / 320.4 s, matching catalog durations (no inserted audio) |
| Tampered expiry | HTTP 403, a clean signal to re-resolve |
| Unavailable ID | Throws `ParsingException: JSON response is too short`, **not** `ContentNotAvailableException`. Error mapping must not trust exception types alone. |
| **Throttling** | One open-ended GET of a whole 5.2 MB track ran at **36 KB/s** (143 s). 1 MB `Range` chunks ran at **~4 MB/s** (whole track in ~1.3 s). The Android data source **must** read in bounded chunks, as NewPipe's own player does. Media3's default open-ended request would play, barely above real time, and prefetch/downloads would crawl. |

This proves the extractor works from a JVM on this network today. It does **not** prove it on an Android device, where Android's TLS stack, the different client identity and battery-saver behaviour all apply.

## 7. Licensing, terms, and honesty constraints

- **NewPipeExtractor is GPL-3.0.** Distributing an APK that includes it makes that APK a combined work under GPL-3.0, which means corresponding source and GPL terms for what is linked into it. This repository has **no LICENSE file** today (all rights reserved by default). Shipping the Android app needs an explicit licensing decision by the owner. Its transitive dependencies (Rhino MPL-2.0, jsoup MIT, nanojson) are GPL-compatible. If GPL is unacceptable, the realistic alternative is yt-dlp (Unlicense) running on-device via Chaquopy (MIT). It is heavier, but permissive.
- **YouTubeKit is MIT.** Its remote fallback must stay disabled. It resolves a known video ID; it does not search. iOS search is a separate problem.
- **YouTube's Terms of Service** prohibit accessing content other than through YouTube's own player and APIs, and downloading without permission. Background playback is a paid YouTube feature. The desktop app already carries this exposure; a public APK widens it. Google Play would reject such an app, so the APK is sideload-only. App Store distribution of a native iOS build would almost certainly be rejected (guideline 5.2.3). None of this is legal advice.
- **Ad-free claim:** Rainette's own pipeline inserts nothing, and the decoded durations above match the catalog. Upstream server-side ad insertion is a known possibility on some YouTube clients. If the extractor's client is moved onto it, ads could arrive inside the stream. The duration check is the test that would catch it, and it stays in the native test suite.
- **iOS distribution:** a free Apple ID signs builds that expire after 7 days, at most 3 apps, and reinstalling needs a Mac with Xcode (or a sideloading tool). Anything beyond that needs the paid Apple Developer Program, which the "no paid dependency" rule excludes. The PWA stays the public iPhone path.

## 8. Pre-existing defects found during the audit (not introduced here)

1. **PWA:** an empty search shows "Nothing matched that." twice: `#searchMessage` and an empty-state paragraph (`pwa/app.js` `search()`).
2. **Website:** `release-manifest.json` lacks `macos`, so the manifest is always rejected (§3).
3. **Website:** the stale 0.2.6 APK is publicly served (§3).
4. **PWA tests (fixed on this branch):** `tests/test_pwa_client_behaviour.py` failed intermittently on `main` (1 failure in each of 2 runs). The cause was the static test server's listen backlog of 5. Chromium opens ~40 module requests at once, macOS resets one, and the app never boots. `request_queue_size = 128` fixes it.
5. **PWA (fixed on this branch):** Home never painted its empty-state message. `renderRecent` compared an empty history with its initial empty list and returned early.
6. **PWA (suspected, unverified):** tracks stored on the computer reach the phone with `source: 'local'`, the same marker the phone uses for its own files. So `isLocalTrack` may send them to the phone's IndexedDB instead of the relay. This needs a check against a real desktop library.

## 9. Phase 0 exit

Done: baseline SHAs, releases, the full test run, screenshots, the Android history and signing analysis, the website state, the compatibility inventory, and the extractor PoC.

Blocked on the owner, before any APK can be published:

- **Signing:** can the Windows keystore be recovered (option 1), or should the app use a new package ID (option 2)?
- **License:** GPL-3.0 for the Android app (needed for NewPipeExtractor), or the permissive yt-dlp route?
- **Devices:** a physical Android phone for acceptance tests. An iPhone plus an iOS runtime/Apple ID for the iOS PoC.
