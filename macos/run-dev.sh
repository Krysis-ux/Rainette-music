#!/bin/sh
# Build and run the native app against this checkout's engine.
# RAINETTE_ENGINE_HOME=/tmp/x runs it against a throwaway library.
set -eu
cd "$(dirname "$0")"
CONFIG="${CONFIG:-debug}"
swift build -c "$CONFIG"
APP=".build/Rainette Music Dev.app"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp ".build/$CONFIG/RainetteMusic" "$APP/Contents/MacOS/RainetteMusic"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleExecutable</key><string>RainetteMusic</string>
  <key>CFBundleIdentifier</key><string>com.rainette.music.dev</string>
  <key>CFBundleName</key><string>Rainette Music</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>dev</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>CFBundleIconFile</key><string>rainette-icon</string>
</dict></plist>
PLIST
ICON="../web/assets/rainette-icon-256.png"
if [ -f "$ICON" ] && [ ! -f "$APP/Contents/Resources/rainette-icon.icns" ]; then
    SET="$(mktemp -d)/rainette-icon.iconset"; mkdir -p "$SET"
    for size in 16 32 128 256; do
        sips -z $size $size "$ICON" --out "$SET/icon_${size}x${size}.png" >/dev/null
        sips -z $((size * 2)) $((size * 2)) "$ICON" --out "$SET/icon_${size}x${size}@2x.png" >/dev/null
    done
    iconutil -c icns "$SET" -o "$APP/Contents/Resources/rainette-icon.icns" || true
fi
codesign --force --sign - "$APP" >/dev/null 2>&1 || true
SOURCE="$(cd .. && pwd)"
PYTHON="${RAINETTE_PYTHON:-$SOURCE/.venv-mac/bin/python}"
[ -x "$PYTHON" ] || PYTHON="$(command -v python3)"
export RAINETTE_SOURCE_DIR="$SOURCE" RAINETTE_PYTHON="$PYTHON"
exec "$APP/Contents/MacOS/RainetteMusic"
