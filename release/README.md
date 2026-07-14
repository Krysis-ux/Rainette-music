# Local release workflow

Windows test build: `powershell -ExecutionPolicy Bypass -File .\release\build-windows-release.ps1 -Version 1.0.0`

Windows publish-ready build: set `RAINETTE_CODESIGN_CERT_PATH` and `RAINETTE_CODESIGN_CERT_PASSWORD`, then add `-RequireSigning`. The command fails before building if the signing inputs or tools are missing.

Android release: set `ANDROID_KEYSTORE_PATH`, `ANDROID_KEYSTORE_PASSWORD`, `ANDROID_KEY_ALIAS`, and `ANDROID_KEY_PASSWORD`, then run `powershell -ExecutionPolicy Bypass -File .\mobile\build-release.ps1 -Version 1.0.0`.

Artifacts and checksum files land in `release\out`. Upload only verified, signed artifacts to Vercel Blob and copy their HTTPS URLs into the download site's `release-manifest.json`.
