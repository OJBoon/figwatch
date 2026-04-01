#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
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

# ── Step 2: Bundle the watcher code ──────────────────────────────
echo "📦 Bundling watcher..."
mkdir -p "$RESOURCES/watcher"
cp -R "$PROJECT_ROOT/watcher/index.js" "$RESOURCES/watcher/"
cp -R "$PROJECT_ROOT/watcher/router.js" "$RESOURCES/watcher/"
cp -R "$PROJECT_ROOT/watcher/utils.js" "$RESOURCES/watcher/"
cp -R "$PROJECT_ROOT/watcher/handlers" "$RESOURCES/watcher/"
cp -R "$PROJECT_ROOT/watcher/skills" "$RESOURCES/watcher/"

# Install watcher npm dependencies into the bundle
cd "$RESOURCES/watcher"
cp "$PROJECT_ROOT/watcher/package.json" .
cp "$PROJECT_ROOT/watcher/package-lock.json" . 2>/dev/null || true
npm install --production 2>&1 | tail -2
cd "$SCRIPT_DIR"

# ── Step 3: Bundle Node.js binary ────────────────────────────────
echo "📦 Bundling Node.js..."
ARCH=$(uname -m)
NODE_VERSION="v22.15.0"

if [ "$ARCH" = "arm64" ]; then
    NODE_PLATFORM="darwin-arm64"
else
    NODE_PLATFORM="darwin-x64"
fi

NODE_TAR="node-${NODE_VERSION}-${NODE_PLATFORM}.tar.gz"
NODE_URL="https://nodejs.org/dist/${NODE_VERSION}/${NODE_TAR}"
NODE_CACHE="/tmp/${NODE_TAR}"

# Download if not cached
if [ ! -f "$NODE_CACHE" ]; then
    echo "   Downloading Node.js ${NODE_VERSION}..."
    curl -sL "$NODE_URL" -o "$NODE_CACHE"
fi

# Extract just the node binary
mkdir -p /tmp/node-extract-$$
tar xzf "$NODE_CACHE" -C /tmp/node-extract-$$ --strip-components=2 "node-${NODE_VERSION}-${NODE_PLATFORM}/bin/node"
cp /tmp/node-extract-$$/node "$RESOURCES/node"
chmod +x "$RESOURCES/node"
rm -rf /tmp/node-extract-$$

# ── Step 4: Bundle app icon ──────────────────────────────────────
echo "🎨 Adding app icon..."
if [ -f "$SCRIPT_DIR/AppIcon.icns" ]; then
    cp "$SCRIPT_DIR/AppIcon.icns" "$RESOURCES/AppIcon.icns"

    # Update Info.plist to reference the icon
    /usr/libexec/PlistBuddy -c "Add :CFBundleIconFile string AppIcon" "$APP/Contents/Info.plist" 2>/dev/null || \
    /usr/libexec/PlistBuddy -c "Set :CFBundleIconFile AppIcon" "$APP/Contents/Info.plist"
fi

# ── Step 5: Bundle menu bar icon ─────────────────────────────────
if [ -f "$SCRIPT_DIR/FigWatch-icon.pdf" ]; then
    cp "$SCRIPT_DIR/FigWatch-icon.pdf" "$RESOURCES/FigWatch-icon.pdf"
fi

# ── Step 6: Sign ─────────────────────────────────────────────────
echo "🔏 Signing..."
codesign --force --deep --sign - "$APP" 2>/dev/null || true

# ── Step 7: Create distributable zip ─────────────────────────────
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
