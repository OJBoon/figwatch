#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BUILD_DIR="$SCRIPT_DIR/build"
APP="$BUILD_DIR/FigWatch.app"

echo "🔨 Building FigWatch..."

# ── Step 1: Build the py2app bundle ──────────────────────────────
cd "$SCRIPT_DIR"
rm -rf dist build FigWatch.app 2>/dev/null
python3 setup.py py2app 2>&1 | tail -3
rm -rf "$APP" 2>/dev/null
mkdir -p "$BUILD_DIR"
mv dist/FigWatch.app "$APP"

RESOURCES="$APP/Contents/Resources"

# ── Step 2: Bundle Python watcher + handlers + skills ────────────
echo "📦 Bundling watcher..."
cp "$SCRIPT_DIR/watcher.py" "$RESOURCES/"
cp -R "$SCRIPT_DIR/handlers" "$RESOURCES/"
cp -R "$SCRIPT_DIR/skills" "$RESOURCES/"

# ── Step 3: Bundle app icon ──────────────────────────────────────
echo "🎨 Adding app icon..."
if [ -f "$SCRIPT_DIR/AppIcon.icns" ]; then
    cp "$SCRIPT_DIR/AppIcon.icns" "$RESOURCES/AppIcon.icns"
    /usr/libexec/PlistBuddy -c "Add :CFBundleIconFile string AppIcon" "$APP/Contents/Info.plist" 2>/dev/null || \
    /usr/libexec/PlistBuddy -c "Set :CFBundleIconFile AppIcon" "$APP/Contents/Info.plist"
fi

# ── Step 4: Bundle menu bar icon ─────────────────────────────────
if [ -f "$SCRIPT_DIR/FigWatch-icon.pdf" ]; then
    cp "$SCRIPT_DIR/FigWatch-icon.pdf" "$RESOURCES/FigWatch-icon.pdf"
fi

# ── Step 5: Sign ─────────────────────────────────────────────────
echo "🔏 Signing..."
codesign --force --deep --sign - "$APP" 2>/dev/null || true

# ── Step 6: Create distributable zip ─────────────────────────────
echo "📦 Creating zip..."
cd "$BUILD_DIR"
zip -r -q "FigWatch.zip" "FigWatch.app"

SIZE=$(du -sh "$APP" | cut -f1)
ZIP_SIZE=$(du -sh "FigWatch.zip" | cut -f1)

echo ""
echo "✅ Built: $APP ($SIZE)"
echo "📦 Zip:   $BUILD_DIR/FigWatch.zip ($ZIP_SIZE)"
echo ""
echo "To install:  unzip FigWatch.zip && mv FigWatch.app /Applications/"
echo "To run now:  open '$APP'"
