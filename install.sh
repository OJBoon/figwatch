#!/usr/bin/env bash
# FigWatch installer — downloads the latest release and installs it to /Applications.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/OJBoon/figwatch/main/install.sh | bash

set -euo pipefail

REPO="OJBoon/figwatch"
APP_NAME="FigWatch.app"
INSTALL_DIR="/Applications"
TMP_DIR="$(mktemp -d -t figwatch-install)"

trap 'rm -rf "$TMP_DIR"' EXIT

if [[ "$(uname)" != "Darwin" ]]; then
  echo "❌ FigWatch only runs on macOS." >&2
  exit 1
fi

echo "→ Fetching latest release metadata…"
# Get the browser_download_url for the .zip asset from the latest release
ZIP_URL="$(curl -fsSL "https://api.github.com/repos/${REPO}/releases/latest" \
  | grep -o '"browser_download_url": *"[^"]*\.zip"' \
  | head -1 \
  | sed 's/.*"browser_download_url": *"\([^"]*\)".*/\1/')"

if [[ -z "${ZIP_URL}" ]]; then
  echo "❌ Couldn't find a FigWatch.zip asset on the latest release." >&2
  echo "   Visit https://github.com/${REPO}/releases and download it manually." >&2
  exit 1
fi

TAG="$(echo "${ZIP_URL}" | sed -E 's|.*/download/([^/]+)/.*|\1|')"
echo "→ Latest: ${TAG}"

echo "→ Downloading ${ZIP_URL##*/}…"
curl -fsSL "${ZIP_URL}" -o "${TMP_DIR}/FigWatch.zip"

echo "→ Extracting…"
/usr/bin/ditto -x -k "${TMP_DIR}/FigWatch.zip" "${TMP_DIR}/extracted"

NEW_APP="${TMP_DIR}/extracted/${APP_NAME}"
if [[ ! -d "${NEW_APP}" ]]; then
  echo "❌ ${APP_NAME} not found inside the downloaded zip." >&2
  exit 1
fi

# If FigWatch is running, quit it cleanly before replacing
if pgrep -x FigWatch > /dev/null; then
  echo "→ Quitting running FigWatch…"
  osascript -e 'tell application "FigWatch" to quit' 2>/dev/null || true
  for _ in $(seq 1 20); do
    pgrep -x FigWatch > /dev/null || break
    sleep 0.25
  done
fi

TARGET="${INSTALL_DIR}/${APP_NAME}"
if [[ -d "${TARGET}" ]]; then
  echo "→ Removing existing ${TARGET}…"
  rm -rf "${TARGET}"
fi

echo "→ Installing to ${TARGET}…"
/usr/bin/ditto "${NEW_APP}" "${TARGET}"

echo "→ Clearing Gatekeeper quarantine…"
/usr/bin/xattr -dr com.apple.quarantine "${TARGET}" 2>/dev/null || true

echo "→ Launching FigWatch…"
open "${TARGET}"

echo ""
echo "✅ FigWatch ${TAG} is installed and running."
echo "   Click the FigWatch icon in your menu bar to get started."
