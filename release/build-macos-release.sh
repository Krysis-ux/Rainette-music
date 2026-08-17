#!/usr/bin/env bash
#
# Build Rainette Music as a macOS .app bundle.
#
# The macOS counterpart of build-windows-release.ps1, kept as a separate script
# on purpose: that one's -Phase ValidateSet, switch-branch ordering and
# credential-isolation rules are asserted verbatim by tests/test_release_packaging.py,
# so bolting a macOS phase onto it would break the Windows contract.
#
# What this deliberately does NOT do is the signing half. Producing a bundle a
# stranger can download and open without a Gatekeeper warning needs a paid Apple
# Developer ID plus notarization; everything here works with no certificate and
# no Apple account, which is what a personal-use port actually needs. See
# release/README-macos.md for the exact difference.
#
# Usage:
#   release/build-macos-release.sh [--version X.Y.Z] [--dmg]
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAGE="$ROOT/release/stage-macos"
OUTPUT="$ROOT/release/out-macos"
APP_NAME="Rainette Music"
BUNDLE_ID="com.rainette.music"
# The published asset name. Spelled out rather than derived from APP_NAME
# because a release page lists it beside RainetteMusicSetup.exe, and a filename
# with a space in it is the one thing that reliably breaks a download link.
DMG_NAME="RainetteMusic-macOS.dmg"
# What the in-app updater downloads, and the manifest naming it. Both names are
# pinned in main.py (MACOS_UPDATE_ASSET / MACOS_MANIFEST_ASSET); changing one
# without the other means the updater stops finding releases.
ZIP_NAME="RainetteMusic-macOS.zip"
MANIFEST_NAME="latest-macos.json"
ENTRY_POINT="$ROOT/main.py"
WEB_DIR="$ROOT/web"
SOURCE_ICON="$ROOT/web/assets/rainette-icon.ico"
PNG_ICON="$ROOT/web/assets/rainette-icon-256.png"

PYTHON="${PYTHON:-$ROOT/.venv-mac/bin/python}"
[ -x "$PYTHON" ] || PYTHON="$(command -v python3)"

WANT_DMG=0
REQUESTED_VERSION=""
while [ $# -gt 0 ]; do
    case "$1" in
        --dmg) WANT_DMG=1; shift ;;
        --version) REQUESTED_VERSION="${2:-}"; shift 2 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

cd "$ROOT"

APP_VERSION="$("$PYTHON" -c 'import version; print(version.normalize(version.APP_VERSION))')"
if [ -n "$REQUESTED_VERSION" ]; then
    # Same gate the Windows script applies: a tag that disagrees with version.py
    # would ship an app whose updater compares against the wrong number.
    WANTED="$("$PYTHON" -c 'import version, sys; print(version.normalize(sys.argv[1]))' "$REQUESTED_VERSION")"
    if [ "$APP_VERSION" != "$WANTED" ]; then
        echo "version.APP_VERSION ($APP_VERSION) does not match --version ($WANTED). Update version.py first." >&2
        exit 1
    fi
fi
echo "==> Building $APP_NAME $APP_VERSION"

rm -rf "$STAGE" "$OUTPUT"
mkdir -p "$STAGE" "$OUTPUT"

# ── Icon ────────────────────────────────────────────────────────────────────
# AppKit cannot read a Windows .ico. sips and iconutil both ship with macOS, so
# the conversion needs no Python imaging dependency. The checked-in .ico is left
# untouched: tests/test_build_script.py asserts it is a real multi-frame ICO.
ICNS="$STAGE/rainette-icon.icns"
ICONSET="$STAGE/rainette-icon.iconset"
mkdir -p "$ICONSET"
ICON_SOURCE="$PNG_ICON"
if [ ! -f "$ICON_SOURCE" ]; then
    ICON_SOURCE="$STAGE/icon-from-ico.png"
    sips -s format png "$SOURCE_ICON" --out "$ICON_SOURCE" >/dev/null
fi
for size in 16 32 64 128 256 512; do
    sips -z $size $size "$ICON_SOURCE" --out "$ICONSET/icon_${size}x${size}.png" >/dev/null
    double=$((size * 2))
    sips -z $double $double "$ICON_SOURCE" --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$ICNS"
echo "==> Icon: $ICNS"

# ── Bundle ──────────────────────────────────────────────────────────────────
# Mirrors the Windows PyInstaller call, with the macOS deltas:
#   --windowed              emits a real .app (--noconsole is the Windows spelling)
#   --add-data "src:dst"    colon separator; Windows uses a semicolon
#   --osx-bundle-identifier fixes the identity macOS keys Keychain/TCC/WKWebView off
# --onedir (not --onefile) because codesigning and notarization operate on a
# bundle tree, and onefile re-extracts a ~200MB payload on every launch.
# No --version-file: that is a Win32 PE resource with no macOS meaning; its role
# is filled by the Info.plist keys written below.
"$PYTHON" -m PyInstaller --noconfirm --clean --onedir --windowed \
    --name "$APP_NAME" \
    --distpath "$STAGE" \
    --workpath "$STAGE/work" \
    --specpath "$STAGE/spec" \
    --icon "$ICNS" \
    --osx-bundle-identifier "$BUNDLE_ID" \
    --add-data "$WEB_DIR:web" \
    --add-data "$ICNS:web/assets" \
    --collect-all webview \
    --collect-all ytmusicapi \
    --collect-all yt_dlp \
    --collect-all qrcode \
    --collect-all mutagen \
    "$ENTRY_POINT"

APP_BUNDLE="$STAGE/$APP_NAME.app"
[ -d "$APP_BUNDLE" ] || { echo "PyInstaller produced no .app bundle" >&2; exit 1; }

# ── Info.plist ──────────────────────────────────────────────────────────────
PLIST="$APP_BUNDLE/Contents/Info.plist"
set_plist() {
    /usr/libexec/PlistBuddy -c "Delete :$1" "$PLIST" >/dev/null 2>&1 || true
    /usr/libexec/PlistBuddy -c "Add :$1 $2 $3" "$PLIST"
}
set_plist CFBundleShortVersionString string "$APP_VERSION"
set_plist CFBundleVersion string "$APP_VERSION"
set_plist CFBundleName string "$APP_NAME"
set_plist CFBundleDisplayName string "$APP_NAME"
# Without this the WebView renders upscaled from 1x and looks soft on Retina.
set_plist NSHighResolutionCapable bool true
# 13.0, not 11.0: the stylesheet uses color-mix() in 33 places, which WebKit only
# supports from Safari 16.2 (macOS 13). Claiming 11.0 would install cleanly on a
# Mac where the entire colour system silently fails to resolve.
set_plist LSMinimumSystemVersion string "13.0"
set_plist LSApplicationCategoryType string "public.app-category.music"
# The companion gateway serves paired phones over the LAN. macOS 15+ gates local
# network access and shows this sentence in the permission prompt.
set_plist NSLocalNetworkUsageDescription string \
    "Rainette Music uses your local network so a paired phone can browse and play your library."
echo "==> Info.plist updated"

# ── Signature ───────────────────────────────────────────────────────────────
# An ad-hoc signature is not optional on Apple Silicon: an unsigned arm64 binary
# will not execute at all. This needs no certificate and no Apple account.
# RAINETTE_CODESIGN_IDENTITY lets a real "Developer ID Application" identity be
# used instead, which is what a public download would require.
IDENTITY="${RAINETTE_CODESIGN_IDENTITY:--}"
codesign --force --deep --sign "$IDENTITY" "$APP_BUNDLE" 2>&1 | sed 's/^/    /' || true
if codesign --verify --deep --strict "$APP_BUNDLE" 2>/dev/null; then
    echo "==> Signed with identity: $IDENTITY"
else
    echo "==> WARNING: signature verification failed; the app may refuse to launch" >&2
fi

ditto "$APP_BUNDLE" "$OUTPUT/$APP_NAME.app"

# ── Optional disk image ─────────────────────────────────────────────────────
if [ "$WANT_DMG" -eq 1 ]; then
    DMG_ROOT="$STAGE/dmg"
    rm -rf "$DMG_ROOT"; mkdir -p "$DMG_ROOT"
    # ditto, not cp: an .app is a tree of symlinks and extended attributes, and
    # cp -R silently flattens enough of it to invalidate the code signature.
    ditto "$APP_BUNDLE" "$DMG_ROOT/$APP_NAME.app"
    # The drag-to-install target. Without it the window shows one icon and no
    # indication of where it is supposed to go.
    ln -s /Applications "$DMG_ROOT/Applications"
    # UDZO is the compressed, read-only format every macOS since 10.1 mounts
    # without a helper. The volume carries the version so a mounted image says
    # which build it is -- two downloads a month apart otherwise mount under
    # the same name and are indistinguishable in Finder.
    hdiutil create -quiet -srcfolder "$DMG_ROOT" -volname "$APP_NAME $APP_VERSION" \
        -format UDZO -ov "$OUTPUT/$DMG_NAME"
    echo "==> Disk image: $OUTPUT/$DMG_NAME"
fi

# ── Self-update payload ─────────────────────────────────────────────────────
#
# The disk image is for a human dragging an icon; the updater takes a zip,
# because expanding one is a single `ditto -xk` rather than mounting, copying
# and unmounting a volume from inside the app that is being replaced.
#
# `ditto -ck --keepParent` is the archiver that preserves the symlinks and
# extended attributes an .app's code signature is computed over. `zip -r` does
# not, and a bundle archived with it fails `codesign --verify` on the other
# side -- which the updater treats, correctly, as a tampered download.
ditto -ck --keepParent "$APP_BUNDLE" "$OUTPUT/$ZIP_NAME"
ARCHIVE_SHA256="$(shasum -a 256 "$OUTPUT/$ZIP_NAME" | awk '{print $1}')"

# Schema 2, same as the Windows manifest, so the updater's existing signature
# check and hash-chain validation cover this one unchanged. No Authenticode
# block: that layer is Windows-only, and the Ed25519 signature over these bytes
# is the sole root of trust on both platforms.
cat > "$OUTPUT/$MANIFEST_NAME" <<JSON
{
  "schema": 2,
  "version": "$APP_VERSION",
  "channel": "release",
  "artifact": "$ZIP_NAME",
  "sha256": "$ARCHIVE_SHA256"
}
JSON
echo "==> Update archive: $OUTPUT/$ZIP_NAME"
echo "==> Update manifest: $OUTPUT/$MANIFEST_NAME"

echo
echo "Built: $OUTPUT/$APP_NAME.app"
echo "Run it with:  open \"$OUTPUT/$APP_NAME.app\""
echo
echo "Because this build is ad-hoc signed rather than notarized, macOS will"
echo "quarantine it if it is ever downloaded from the internet. Clear that with:"
echo "  xattr -dr com.apple.quarantine \"$OUTPUT/$APP_NAME.app\""
