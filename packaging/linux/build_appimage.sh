#!/usr/bin/env bash
# packaging/linux/build_appimage.sh — assemble a self-contained AppImage for the ui/imgui GUI
# (AI-CAD2). Mirrors services/installer/build_release.py's "embed-and-extract" payload shape
# (app/ Python packages + wheelhouse/ offline wheels) so the SAME bootstrap.cpp
# findPayload()/provisionRuntime() logic that already runs on Windows also works unmodified
# inside the AppImage's mount (exeDir()/app + exeDir()/wheelhouse via /proc/self/exe).
#
# Requires a prior build of the ui/imgui GUI:
#   flock /tmp/host-build-linux.lock -c \
#     "cmake --preset linux-app && cmake --build build/linux-app --config Release -j\$(nproc)"
#
# Usage:
#   packaging/linux/build_appimage.sh [--out DIR] [--skip-wheels] [--with-libaicad] [--zip-only-appdir]
#
#   --out DIR           output directory for the .AppImage (default: <repo>/dist)
#   --skip-wheels       omit the offline wheelhouse (smaller AppImage; runtime provisioning
#                        then needs network access instead of being fully offline-friendly)
#   --with-libaicad     bundle core/build-linux/bin/libaicad.so if present, and point
#                        AI_CAD_LIBAICAD at it from AppRun (the real engine, not demo mode)
#   --keep-appdir       don't delete the staged AppDir/ after building (for inspection)
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUT="$REPO/dist"
SKIP_WHEELS=0
WITH_LIBAICAD=0
KEEP_APPDIR=0

while [ $# -gt 0 ]; do
    case "$1" in
        --out) OUT="$2"; shift 2 ;;
        --skip-wheels) SKIP_WHEELS=1; shift ;;
        --with-libaicad) WITH_LIBAICAD=1; shift ;;
        --keep-appdir) KEEP_APPDIR=1; shift ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

BIN="$REPO/build/linux-app/bin/AI-CAD2"
if [ ! -x "$BIN" ]; then
    echo "error: $BIN not found — build it first:" >&2
    echo '  flock /tmp/host-build-linux.lock -c \' >&2
    echo '    "cmake --preset linux-app && cmake --build build/linux-app --config Release -j$(nproc)"' >&2
    exit 1
fi

VERSION="$(git -C "$REPO" describe --tags --always --dirty 2>/dev/null || echo 0.0.0-dev)"
echo "==> version: $VERSION"

APPDIR="$OUT/AppDir"
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/bin" "$APPDIR/usr/lib"

echo "==> staging binary + resources"
cp "$BIN" "$APPDIR/usr/bin/AI-CAD2"
if [ -d "$REPO/build/linux-app/bin/resources" ]; then
    cp -a "$REPO/build/linux-app/bin/resources" "$APPDIR/usr/bin/resources"
fi

if [ "$WITH_LIBAICAD" = "1" ]; then
    LIBAICAD="$REPO/core/build-linux/bin/libaicad.so"
    if [ -f "$LIBAICAD" ]; then
        cp "$LIBAICAD" "$APPDIR/usr/lib/libaicad.so"
        echo "==> bundled libaicad.so (real engine, not demo mode)"
    else
        echo "warning: --with-libaicad given but $LIBAICAD not found — shipping demo mode" >&2
    fi
fi

# Python payload (mirrors services/installer/build_release.py:stage_app, minus host/-only
# assets): bootstrap.cpp's findPayload() looks for <exedir>/app/{installer/provision.py,
# requirements.txt} — same convention that already ships in the Windows installer.
echo "==> staging Python app payload"
APP="$APPDIR/usr/bin/app"
mkdir -p "$APP"
for pkg in engine mcp_server agent installer; do
    if [ -d "$REPO/$pkg" ]; then
        rsync -a --exclude='__pycache__' --exclude='*.pyc' --exclude='build' \
              --exclude='test_*' --exclude='.ready.json' "$REPO/$pkg/" "$APP/$pkg/"
    fi
done
cp "$REPO/requirements.txt" "$APP/requirements.txt"

if [ "$SKIP_WHEELS" = "1" ]; then
    echo "==> skipping wheelhouse (--skip-wheels)"
else
    echo "==> downloading offline wheelhouse (pip download -r requirements.txt)"
    WHEELHOUSE="$APPDIR/usr/bin/wheelhouse"
    mkdir -p "$WHEELHOUSE"
    python3 -m pip download -r "$REPO/requirements.txt" -d "$WHEELHOUSE" \
        > "$OUT/wheelhouse_download.log" 2>&1 || {
        echo "warning: wheelhouse download incomplete — see $OUT/wheelhouse_download.log" >&2
        echo "         (provisioning will fall back to PyPI for missing wheels)" >&2
    }
    n=$(find "$WHEELHOUSE" -name '*.whl' -o -name '*.tar.gz' | wc -l)
    echo "==> wheelhouse: $n package archives"
fi

echo "==> writing AppRun + desktop entry + icon"
cat > "$APPDIR/AppRun" <<'EOF'
#!/bin/sh
HERE="$(dirname "$(readlink -f "$0")")"
export LD_LIBRARY_PATH="$HERE/usr/lib:${LD_LIBRARY_PATH:-}"
if [ -f "$HERE/usr/lib/libaicad.so" ] && [ -z "${AI_CAD_LIBAICAD:-}" ]; then
    export AI_CAD_LIBAICAD="$HERE/usr/lib/libaicad.so"
fi
exec "$HERE/usr/bin/AI-CAD2" "$@"
EOF
chmod +x "$APPDIR/AppRun"
cp "$REPO/packaging/linux/AI-CAD.desktop" "$APPDIR/AI-CAD.desktop"
cp "$REPO/host/assets/logo_icon.png" "$APPDIR/AI-CAD.png"

APPIMAGETOOL="$REPO/build/tools/appimagetool"
if [ ! -x "$APPIMAGETOOL" ]; then
    echo "==> fetching appimagetool (cached at build/tools/appimagetool)"
    mkdir -p "$REPO/build/tools"
    curl -sL -o "$APPIMAGETOOL" \
        "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage"
    chmod +x "$APPIMAGETOOL"
fi

mkdir -p "$OUT"
OUTFILE="$OUT/AI-CAD-$VERSION-x86_64.AppImage"
echo "==> building $OUTFILE"
if ! ARCH=x86_64 VERSION="$VERSION" "$APPIMAGETOOL" "$APPDIR" "$OUTFILE" 2>"$OUT/appimagetool.log"; then
    echo "==> direct run failed, retrying with --appimage-extract-and-run (no-FUSE environments)"
    ARCH=x86_64 VERSION="$VERSION" "$APPIMAGETOOL" --appimage-extract-and-run "$APPDIR" "$OUTFILE"
fi

if [ "$KEEP_APPDIR" != "1" ]; then
    rm -rf "$APPDIR"
fi

echo "==> built: $OUTFILE"
ls -la "$OUTFILE"
