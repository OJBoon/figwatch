#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BUILD_DIR="$SCRIPT_DIR/build"
APP="$BUILD_DIR/FigWatch.app"

echo "🔨 Building FigWatch..."

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"
mkdir -p "$APP/Contents/Resources"

# Copy the Python script
cp "$SCRIPT_DIR/Sources/FigWatch.py" "$APP/Contents/Resources/FigWatch.py"

# Find the actual Python3 binary path
PYTHON_BIN=$(python3 -c "import sys; print(sys.executable)")
PYTHON_FRAMEWORK=$(python3 -c "import sys, os; print(os.path.dirname(os.path.dirname(sys.executable)))")

# Symlink python3 directly as the bundle executable — this is the key.
# macOS needs the actual binary as the bundle's process for NSStatusBar to work.
ln -sf "$PYTHON_BIN" "$APP/Contents/MacOS/python3"

# Create a tiny wrapper that calls python3 with our script.
# We use the symlinked python3 so it runs inside the .app bundle context.
cat > "$APP/Contents/MacOS/FigWatch" << 'LAUNCHER'
#!/bin/bash
DIR="$(cd "$(dirname "$0")" && pwd)"
exec "$DIR/python3" "$DIR/../Resources/FigWatch.py"
LAUNCHER
chmod +x "$APP/Contents/MacOS/FigWatch"

# Info.plist
cat > "$APP/Contents/Info.plist" << 'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key>
    <string>FigWatch</string>
    <key>CFBundleIdentifier</key>
    <string>com.joybuy.figwatch</string>
    <key>CFBundleName</key>
    <string>FigWatch</string>
    <key>CFBundleDisplayName</key>
    <string>FigWatch</string>
    <key>CFBundleVersion</key>
    <string>1.0.0</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>LSMinimumSystemVersion</key>
    <string>13.0</string>
    <key>LSUIElement</key>
    <true/>
    <key>NSHighResolutionCapable</key>
    <true/>
</dict>
</plist>
PLIST

# Ad-hoc sign
codesign --force --sign - "$APP" 2>/dev/null || true

echo "✅ Built: $APP"
echo ""
echo "To install:"
echo "  cp -R '$APP' /Applications/"
echo ""
echo "To run now:"
echo "  open '$APP'"
