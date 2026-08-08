# Releasing Rainette Music

## One-time setup (signing identity)

Self-updates are trusted via an Ed25519 signature over each release's
`latest.json`, verified inside the app against the public key
committed in `version.py`.

1. `python release/new_signing_key.py`
2. Commit the printed **public** key into `version.py`
   (`UPDATE_SIGNER_PUBLIC_KEY`).
3. Add the printed **private** key as the GitHub Actions secret
   `UPDATE_SIGNING_KEY` (used by the `release-signing` environment in the
   workflow).
4. **Back the private key up offline.** Losing it means no build can ever
   self-update again; leaking it means an attacker can sign updates.

## Shipping a release

1. Bump `version.py` (`APP_VERSION`) — the workflow refuses a tag that does
   not match it.
2. Commit, then tag and push: `git tag v0.2.3 && git push origin v0.2.3`
3. CI runs tests, builds the Windows installer (`-Phase Release`), signs
   `latest.json` in an isolated job, and publishes all assets to the GitHub
   release.

Installed apps poll GitHub every 6 hours (or via Settings → Check now), verify
the manifest signature, the installer hash, and the release channel, then
install silently and relaunch.

Note: a build can only self-update if its *own* baked-in public key matches
the signature — so the first keyed release must be installed by hand once.

## Local builds

Windows test build (never installable by the updater — `channel: local-test`):

    powershell -ExecutionPolicy Bypass -File .\release\build-windows-release.ps1 -Version 0.2.3

Windows release build without CI (then sign the manifest yourself):

    powershell -ExecutionPolicy Bypass -File .\release\build-windows-release.ps1 -Version 0.2.3 -Phase Release
    $env:RAINETTE_UPDATE_SIGNING_KEY = '<base64 private key>'
    python release\sign_manifest.py release\out\latest.json

The phone client ships separately: `pwa/` is a static site deployed from
`Krysis-ux/music-pwa-web`, so it has no build step and is never a release asset
here.

Artifacts land in `release\out` (git-ignored). The optional
`SignAndPackage` phase adds Windows Authenticode on top if a code-signing
certificate ever exists; the updater then enforces both layers.
