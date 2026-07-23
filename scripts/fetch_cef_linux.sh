#!/usr/bin/env bash
# Download + extract the CEF minimal binary distribution for Linux x64
# (Chromium 149 — matches services/browser/stealth.py cookie interop).
# Mirrors scripts/fetch_cef_windows.ps1 for the Linux host build.
#
# Usage (from repo root):
#   bash scripts/fetch_cef_linux.sh
#
# Result: extern/cef/cef_binary_*_linux64_minimal/  (gitignored)
# Then rebuild the host with CEF ON (the default):
#   ./build.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="$ROOT/extern/cef"
VERSION="149.0.6+g0d0eeb6+chromium-149.0.7827.201"
FILENAME="cef_binary_${VERSION}_linux64_minimal.tar.bz2"
# Spotify CDN encodes + as %2B
URLNAME="${FILENAME//+/%2B}"
URL="https://cef-builds.spotifycdn.com/$URLNAME"
ARCHIVE="$DEST/cef_binary_149.0.6_linux64_minimal.tar.bz2"
EXTRACTED="$DEST/cef_binary_${VERSION}_linux64_minimal"

mkdir -p "$DEST"

if [ -f "$EXTRACTED/Release/libcef.so" ]; then
    echo "CEF already present: $EXTRACTED"
    exit 0
fi

echo "Downloading $URL ..."
curl -L --retry 3 --retry-delay 2 -o "$ARCHIVE" "$URL"

size=$(stat -c%s "$ARCHIVE" 2>/dev/null || stat -f%z "$ARCHIVE")
if [ "$size" -lt 1000000 ]; then
    echo "ERROR: download failed or too small: $ARCHIVE ($size bytes)" >&2
    exit 1
fi

echo "Extracting ($((size / 1024 / 1024)) MB archive) ..."
python3 -c "
import tarfile, pathlib
archive = pathlib.Path('$ARCHIVE')
dest = pathlib.Path('$DEST')
with tarfile.open(archive, 'r:bz2') as tf:
    tf.extractall(dest)
print('extracted ok')
"

if [ ! -f "$EXTRACTED/Release/libcef.so" ]; then
    echo "ERROR: extract finished but libcef.so missing under $EXTRACTED" >&2
    exit 1
fi

rm -f "$ARCHIVE"
echo "Done. CEF_ROOT=$EXTRACTED"
echo "Rebuild host:  ./build.sh"
