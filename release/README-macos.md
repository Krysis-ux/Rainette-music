# Building Rainette Music for macOS

The Windows release pipeline is unchanged — see [README.md](README.md). This is
the macOS side, which is a separate script on purpose:
`tests/test_release_packaging.py` asserts the Windows PowerShell script's phase
list, switch-branch order and credential isolation verbatim, so adding a macOS
phase to it would break the Windows contract.

## Build

```bash
release/build-macos-release.sh
```

Produces `release/out-macos/Rainette Music.app`. Add `--dmg` for a
drag-to-Applications disk image, and `--version X.Y.Z` to assert the build
version matches `version.APP_VERSION` (the same gate the Windows script applies).

## What differs from the Windows build

| | Windows | macOS |
|---|---|---|
| PyInstaller mode | `--onedir --noconsole` | `--onedir --windowed` (this is what emits a real `.app`) |
| Data separator | `--add-data "web;web"` | `--add-data "web:web"` — colon |
| Icon | `rainette-icon.ico` | `.icns`, generated at build time with `sips` + `iconutil` |
| Version metadata | Win32 resource via `make_version_file.py` | `Info.plist` keys |
| Identity | `AppUserModelID` | `CFBundleIdentifier` (`com.rainette.music`) |
| Installer | Inno Setup `RainetteMusicSetup.exe` | `.app` bundle; `--dmg` builds the published `RainetteMusic-macOS.dmg` |
| Firewall | `netsh advfirewall` rule | none possible; macOS prompts once |

The checked-in `.ico` is never modified — `tests/test_build_script.py` requires
it to stay a real multi-frame ICO. The `.icns` is derived into the build stage.

## Signing: what needs a paid Apple account, and what doesn't

This is the part that usually surprises people, so it is spelled out.

**Works with no certificate and no Apple account:**

- Building the `.app` and running it on your own machine.
- The **ad-hoc signature** the build script applies (`codesign --sign -`). This
  is not optional on Apple Silicon: an unsigned arm64 binary will not execute at
  all. It needs no identity.
- Sharing it with someone who is willing to right-click → Open once, or run
  `xattr -dr com.apple.quarantine "Rainette Music.app"`.

**Needs the Apple Developer Program (~$99/yr):**

- A **Developer ID Application** signature.
- The **hardened runtime** (`--options runtime`), which notarization requires.
- **Notarization** (`xcrun notarytool submit --wait`) and **stapling**
  (`xcrun stapler staple`).
- A download a stranger can open with no warning and no terminal command.

To use a real identity, set `RAINETTE_CODESIGN_IDENTITY` before building:

```bash
RAINETTE_CODESIGN_IDENTITY="Developer ID Application: Your Name (TEAMID)" \
  release/build-macos-release.sh --dmg
```

If you later enable the hardened runtime, expect to need
`com.apple.security.cs.allow-jit`,
`com.apple.security.cs.allow-unsigned-executable-memory` and
`com.apple.security.cs.disable-library-validation` — PyInstaller bundles a
Python interpreter that loads code dynamically, and `yt-dlp` spawns
subprocesses.

## In-app updates

The built-in updater installs the **signed Windows installer** and verifies it
with Authenticode, so it refuses to run anywhere else:
`apply_update` returns `unsupported` with an explanation rather than downloading
an `.exe` macOS cannot run. Update a macOS build by rebuilding it.

The Ed25519 half of the release infrastructure (`release/sign_manifest.py`,
`release/new_signing_key.py`, `version.UPDATE_SIGNER_PUBLIC_KEY`, and the
manifest verification in `main.py`) is already platform-neutral and would be
reusable as-is if a macOS update channel is ever added — that work would need a
separate `latest-macos.json` manifest plus a helper that swaps the bundle after
the running app exits, since a bundle cannot reliably overwrite itself.

## CI

No macOS job was added to `.github/workflows/release.yml`. That workflow is
pinned by tests that count its artifact uploads and assert `publish`'s exact
`needs:` string, so a macOS job there would fail the suite. If macOS releases
are wanted in CI, add a **separate workflow file** — the tests only read
`release.yml`.
