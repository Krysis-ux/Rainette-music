# Rainette Desktop Mobile Page Design

## Summary

Add a **Mobile** destination to the desktop sidebar. It explains Android installation, links to the official GitHub Release APK, and creates a separate short-lived QR code for pairing the installed app with the current desktop over the same Wi-Fi.

The page uses three explicit steps: **Download**, **Install**, and **Pair**. Downloading and pairing never share a QR code because the first opens a browser while the second must deep-link into the installed Rainette app.

## Page and Data Flow

- Add `Mobile` to the main navigation and render its content through a focused mobile-page module rather than adding more pairing logic to the main music renderer.
- The Android download button and install QR target `https://github.com/Krysis-ux/Rainette-music/releases/latest/download/rainette-music-android.apk`.
- Show Android installation guidance and clearly state that the phone may ask permission to install an app from the browser/GitHub.
- `New pairing code` calls the existing desktop-native companion API. The response supplies an expiring `rainette://pair` URI containing the LAN endpoint, one-time invitation, and pinned certificate fingerprint; Python renders it as a local QR data URL with no third-party web service.
- While the page is open, poll the desktop-native API for pending phone requests and paired devices. A pending request shows the device name with Approve/Reject controls; paired devices show a Revoke control.
- When pywebview/native APIs are unavailable, keep the APK download usable but show that pairing requires the installed Rainette desktop app.

## APK Publication

- Add a GitHub Actions release workflow using JDK 21 and the existing Capacitor project. A version tag builds a release APK named `rainette-music-android.apk` and attaches it to the corresponding GitHub Release.
- Signing uses repository secrets `ANDROID_KEYSTORE_BASE64`, `ANDROID_KEYSTORE_PASSWORD`, `ANDROID_KEY_ALIAS`, and `ANDROID_KEY_PASSWORD`. The workflow must fail clearly when signing material is absent; it must never publish an unsigned APK as a production download.
- Until a signed release exists, the page labels the download as not yet published rather than implying that installation is available.

## Failure Handling and Security

- Invitation QR codes expire after five minutes and can be manually refreshed.
- The QR contains only the short-lived invitation and certificate fingerprint, never the desktop launch token or a permanent device credential.
- Pairing requires explicit desktop approval. Rejected, expired, and revoked devices cannot access library, playback, or relay routes.
- Failed QR generation, listener startup, GitHub download availability, and pairing requests appear inline with actionable messages.

## Verification

- Contract tests verify the Mobile navigation entry, GitHub APK URL, separate install/pair QR labels, expiry state, and native API method names.
- Companion tests cover invitation creation, pending-device listing, approval/rejection, expiry, and revocation.
- Browser tests verify the page at desktop and narrow widths, including the no-native fallback and broken-image placeholders.
- The release workflow validates the Capacitor sync and Android release build before uploading an APK.

## Assumptions

- Android is the first downloadable mobile platform. iOS remains a personal Xcode build until an Apple distribution path is chosen.
- GitHub Releases provides free APK hosting; Rainette does not operate a download server.
- The phone and desktop must share a local network for pairing and companion playback.
