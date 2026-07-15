#!/usr/bin/env bash
# stage.sh — assemble Webservice/image/stage/ from the host-built Billet tree.
# Build output only (not committed). Run from the repo root or any cwd.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/../.." && pwd)"
STAGE="$SCRIPT_DIR/stage"
BIN_SRC="$REPO/host/build-linux/bin"

die() { echo "ERROR: $*" >&2; exit 1; }

[ -x "$BIN_SRC/Billet" ] || die "missing $BIN_SRC/Billet — build host GUI first"
[ -f "$BIN_SRC/libaicad.so" ] || die "missing $BIN_SRC/libaicad.so"

echo "==> staging into $STAGE"
rm -rf "$STAGE"
mkdir -p "$STAGE/bin" "$STAGE/lib" "$STAGE/share"

# ---- Billet binary + compat symlink -----------------------------------------
cp -a "$BIN_SRC/Billet" "$STAGE/bin/Billet"
ln -sfn Billet "$STAGE/bin/AI-CAD"
chmod +x "$STAGE/bin/Billet"

# ---- Engine shared libs -----------------------------------------------------
# Prefer the full OCCT engine from core/build-occt-linux; fall back to the
# copy next to the GUI binary (host/build-linux/bin), then slim core/build-linux.
LIBAICAD=""
for cand in \
    "$REPO/core/build-occt-linux/bin/libaicad.so" \
    "$BIN_SRC/libaicad.so" \
    "$REPO/core/build-linux/bin/libaicad.so"
do
    if [ -f "$cand" ]; then LIBAICAD="$cand"; break; fi
done
[ -n "$LIBAICAD" ] || die "libaicad.so not found"
cp -a "$LIBAICAD" "$STAGE/lib/libaicad.so"
# Also next to the exe — cpp_core ensureLoaded looks beside Billet first,
# then $AI_CAD_LIBAICAD, then bare "libaicad.so" via LD_LIBRARY_PATH.
cp -a "$LIBAICAD" "$STAGE/bin/libaicad.so"
echo "    libaicad.so <- $LIBAICAD (lib/ + bin/)"

if [ -f "$BIN_SRC/lib3mf.so" ]; then
    cp -a "$BIN_SRC/lib3mf.so" "$STAGE/lib/lib3mf.so"
    cp -a "$BIN_SRC/lib3mf.so" "$STAGE/bin/lib3mf.so"
    echo "    lib3mf.so"
fi

# Netgen (FEA meshing) — absolute RPATH on host libaicad; re-stage next to it.
NG_LIB="$REPO/third_party/netgen/install/lib"
if [ -f "$NG_LIB/libnglib.so" ]; then
    cp -a "$NG_LIB/libnglib.so" "$NG_LIB/libngcore.so" "$STAGE/lib/"
    cp -a "$NG_LIB/libnglib.so" "$NG_LIB/libngcore.so" "$STAGE/bin/"
    echo "    netgen libnglib.so + libngcore.so"
fi

# ---- aicad-hostd (engine daemon) --------------------------------------------
HOSTD=""
for cand in \
    "$REPO/core/build-occt-linux/bin/aicad-hostd" \
    "$REPO/core/build-linux/bin/aicad-hostd" \
    "$BIN_SRC/aicad-hostd" \
    "$BIN_SRC/hostd"
do
    if [ -x "$cand" ]; then HOSTD="$cand"; break; fi
done
if [ -n "$HOSTD" ]; then
    cp -a "$HOSTD" "$STAGE/bin/aicad-hostd"
    chmod +x "$STAGE/bin/aicad-hostd"
    echo "    aicad-hostd <- $HOSTD"
else
    echo "WARNING: aicad-hostd not found — session will fall back to in-process engine" >&2
fi

# ---- Assets (fonts, icons, logos) — required at runtime next to the exe ----
# ui_theme.cpp loads assetsDir + "/fonts/"; icons live under assets/icons/.
if [ -d "$BIN_SRC/assets" ]; then
    cp -a "$BIN_SRC/assets" "$STAGE/bin/assets"
    echo "    assets/ (from bin)"
elif [ -d "$REPO/host/assets" ]; then
    cp -a "$REPO/host/assets" "$STAGE/bin/assets"
    echo "    assets/ (from host/assets)"
else
    die "no assets/ directory found"
fi
# Ensure fonts are present even if the bin tree is incomplete.
if [ ! -d "$STAGE/bin/assets/fonts" ] && [ -d "$REPO/assets/fonts" ]; then
    mkdir -p "$STAGE/bin/assets/fonts"
    cp -a "$REPO/assets/fonts/." "$STAGE/bin/assets/fonts/"
    echo "    assets/fonts/ (from repo assets/fonts)"
fi

# ---- CEF runtime (REQUIRED — AI_CAD_ENABLE_CEF is compile-time; no disable flag) ----
# Advisor §A6 asked to disable CEF for container stability. Grep of host/src shows
# AI_CAD_ENABLE_CEF is a compile-time #ifdef only; the linked Billet NEEDs libcef.so
# and CefInitialize aborts without icudtl.dat/pak next to the exe. Shipping stripped
# CEF is the only viable path. AI_CAD_CEF_LAZY=1 (entrypoint) defers cold init.
echo "==> staging CEF runtime (cannot disable at runtime)"
CEF_SRC="$BIN_SRC"
[ -f "$CEF_SRC/libcef.so" ] || die "libcef.so missing — Billet will not load"

CEF_STRIPPED="$REPO/build/tools/libcef-stripped.so"
mkdir -p "$REPO/build/tools"
if [ ! -f "$CEF_STRIPPED" ] || [ "$CEF_SRC/libcef.so" -nt "$CEF_STRIPPED" ]; then
    echo "    stripping libcef.so debug sections -> $CEF_STRIPPED"
    strip --strip-debug -o "$CEF_STRIPPED" "$CEF_SRC/libcef.so"
fi
cp -a "$CEF_STRIPPED" "$STAGE/bin/libcef.so"

for f in \
    ai_cad_cef_helper chrome-sandbox icudtl.dat v8_context_snapshot.bin \
    chrome_100_percent.pak chrome_200_percent.pak resources.pak \
    libEGL.so libGLESv2.so libvk_swiftshader.so libvulkan.so.1 \
    vk_swiftshader_icd.json
do
    if [ -e "$CEF_SRC/$f" ]; then
        cp -a "$CEF_SRC/$f" "$STAGE/bin/$f"
    fi
done
# English locale only — full locales/ is huge and unused for CAD streaming seats.
if [ -d "$CEF_SRC/locales" ]; then
    mkdir -p "$STAGE/bin/locales"
    for f in en-US.pak en-US_FEMININE.pak en-US_MASCULINE.pak en-US_NEUTER.pak \
             en-GB.pak en-GB_FEMININE.pak en-GB_MASCULINE.pak en-GB_NEUTER.pak; do
        [ -f "$CEF_SRC/locales/$f" ] && cp -a "$CEF_SRC/locales/$f" "$STAGE/bin/locales/"
    done
    # Always ship at least one locale if the en-* names differ across CEF builds.
    if [ -z "$(ls -A "$STAGE/bin/locales" 2>/dev/null)" ]; then
        cp -a "$CEF_SRC/locales/." "$STAGE/bin/locales/"
    fi
    echo "    locales/ (en subset or full)"
fi
chmod +x "$STAGE/bin/ai_cad_cef_helper" 2>/dev/null || true
chmod 4755 "$STAGE/bin/chrome-sandbox" 2>/dev/null || true

# ---- Manifest ---------------------------------------------------------------
{
    echo "staged_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "billet=$(stat -c %s "$STAGE/bin/Billet" 2>/dev/null || echo '?')"
    echo "libaicad_src=$LIBAICAD"
    echo "hostd=${HOSTD:-none}"
    echo "cef=stripped"
} > "$STAGE/MANIFEST.txt"

echo "==> stage complete:"
du -sh "$STAGE" "$STAGE/bin" "$STAGE/lib" 2>/dev/null || true
ls -la "$STAGE/bin" | head -40
