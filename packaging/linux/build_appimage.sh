#!/usr/bin/env bash
# packaging/linux/build_appimage.sh — assemble a self-contained AppImage for the Chudware
# host GUI (binary named Chudware; an Chudware compat symlink ships beside it).
# Mirrors services/installer/build_release.py's "embed-and-extract" payload shape
# (app/ Python packages + wheelhouse/ offline wheels) so the SAME bootstrap.cpp
# findPayload()/provisionRuntime() logic that already runs on Windows also works unmodified
# inside the AppImage's mount (exeDir()/app + exeDir()/wheelhouse via /proc/self/exe).
#
# Requires a prior build of the host GUI:
#   cmake -S host -B host/build-linux -G Ninja -DCMAKE_BUILD_TYPE=Release
#   ninja -C host/build-linux Chudware
#
# Usage:
#   packaging/linux/build_appimage.sh [--out DIR] [--skip-wheels] [--no-libchudware] [--keep-appdir]
#
#   --out DIR           output directory for the .AppImage (default: <repo>/dist)
#   --skip-wheels       omit the offline wheelhouse (smaller AppImage; runtime provisioning
#                        then needs network access instead of being fully offline-friendly)
#   --no-libchudware       do NOT bundle libchudware.so (demo mode / smaller image). Bundling is
#                        the DEFAULT for release builds: the full FEA engine from
#                        core/build-occt-linux is preferred, the slim core/build-linux one
#                        is a loud-warning fallback. (--with-libchudware is accepted as a
#                        no-op for backward compatibility.)
#   --keep-appdir       don't delete the staged AppDir/ after building (for inspection)
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUT="$REPO/dist"
SKIP_WHEELS=0
WITH_LIBCHUDWARE=1
KEEP_APPDIR=0

while [ $# -gt 0 ]; do
    case "$1" in
        --out) OUT="$2"; shift 2 ;;
        --skip-wheels) SKIP_WHEELS=1; shift ;;
        --with-libchudware) WITH_LIBCHUDWARE=1; shift ;;   # legacy no-op (now the default)
        --no-libchudware) WITH_LIBCHUDWARE=0; shift ;;
        --keep-appdir) KEEP_APPDIR=1; shift ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

BIN="$REPO/host/build-linux/bin/Chudware"
# Stale build dirs may only have the old binary name; fall back so packaging still works.
[ -e "$BIN" ] || BIN="$REPO/host/build-linux/bin/Chudware"
if [ ! -x "$BIN" ]; then
    echo "error: $BIN not found — build it first:" >&2
    echo '  cmake -S host -B host/build-linux -G Ninja -DCMAKE_BUILD_TYPE=Release' >&2
    echo '  ninja -C host/build-linux Chudware' >&2
    exit 1
fi

VERSION="$(git -C "$REPO" describe --tags --always --dirty 2>/dev/null || echo 0.0.0-dev)"
echo "==> version: $VERSION"

APPDIR="$OUT/AppDir"
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/bin" "$APPDIR/usr/lib"

echo "==> staging binary + resources"
cp "$BIN" "$APPDIR/usr/bin/Chudware"
ln -sf Chudware "$APPDIR/usr/bin/Chudware"  # compat symlink for legacy launch paths
if [ -d "$REPO/host/build-linux/bin/assets" ]; then
    cp -a "$REPO/host/build-linux/bin/assets" "$APPDIR/usr/bin/assets"
fi

# CEF runtime — the GUI initializes Chromium unconditionally at startup (the embedded
# vendor browser has no toggle; see host/src/main.cpp), and CefInitialize HARD-ABORTS
# ("Invalid file descriptor to ICU data received") when icudtl.dat/pak files aren't next
# to the exe. Without this block the AppImage cannot boot at all. libcef.so carries
# ~940 MB of debug sections; ship a --strip-debug copy, cached and re-stripped only when
# the source .so changes.
CEF_SRC="$REPO/host/build-linux/bin"
if [ -f "$CEF_SRC/libcef.so" ]; then
    echo "==> staging CEF runtime"
    CEF_STRIPPED="$REPO/build/tools/libcef-stripped.so"
    mkdir -p "$REPO/build/tools"
    if [ ! -f "$CEF_STRIPPED" ] || [ "$CEF_SRC/libcef.so" -nt "$CEF_STRIPPED" ]; then
        echo "==> stripping libcef.so debug sections (cached at build/tools/)"
        strip --strip-debug -o "$CEF_STRIPPED" "$CEF_SRC/libcef.so"
    fi
    cp "$CEF_STRIPPED" "$APPDIR/usr/bin/libcef.so"
    for f in chudware_cef_helper chrome-sandbox icudtl.dat v8_context_snapshot.bin \
             chrome_100_percent.pak chrome_200_percent.pak resources.pak \
             libEGL.so libGLESv2.so libvk_swiftshader.so libvulkan.so.1 \
             vk_swiftshader_icd.json; do
        [ -e "$CEF_SRC/$f" ] && cp "$CEF_SRC/$f" "$APPDIR/usr/bin/$f"
    done
    [ -d "$CEF_SRC/locales" ] && cp -a "$CEF_SRC/locales" "$APPDIR/usr/bin/locales"
else
    echo "WARNING: CEF runtime not found in $CEF_SRC — the packaged GUI will abort at startup (CefInitialize needs icudtl.dat/pak next to the exe)" >&2
fi

# Engine host daemon (chudware-hostd, core/hostd) — the shared engine process the GUI
# and MCP servers attach to (CHUDWARE_ENGINE_DAEMON=1). Same build-dir preference as
# the engine below: full occt build first, slim fallback with a loud warning.
HOSTD=""
for cand in "$REPO/core/build-occt-linux/bin/chudware-hostd" "$REPO/core/build-linux/bin/chudware-hostd"; do
    [ -x "$cand" ] && { HOSTD="$cand"; break; }
done
if [ -n "$HOSTD" ]; then
    case "$HOSTD" in
        */build-linux/*) echo "WARNING: staging chudware-hostd from the SLIM core/build-linux tree (core/build-occt-linux/bin/chudware-hostd not built) — daemon will load whatever engine AppRun points it at, but rebuild the occt tree for release" >&2 ;;
    esac
    cp "$HOSTD" "$APPDIR/usr/bin/chudware-hostd"
    echo "==> staged chudware-hostd ($HOSTD)"
else
    echo "WARNING: chudware-hostd not built (core/build-occt-linux or core/build-linux) — AppImage ships WITHOUT the engine daemon; daemon mode (the default) will fall back to in-process" >&2
fi

if [ "$WITH_LIBCHUDWARE" = "1" ]; then
    # Prefer the FULL engine (FEA/sim, OCCT geometry) from core/build-occt-linux; the
    # slim core/build-linux .so has no sim solvers and is only a loud-warning fallback.
    # (This was a real shipped bug: the AppImage used to bundle the slim engine.)
    LIBCHUDWARE=""
    for cand in "$REPO/core/build-occt-linux/bin/libchudware.so" "$REPO/core/build-linux/bin/libchudware.so"; do
        [ -f "$cand" ] && { LIBCHUDWARE="$cand"; break; }
    done
    if [ -n "$LIBCHUDWARE" ]; then
        case "$LIBCHUDWARE" in
            */build-linux/*) echo "WARNING: bundling the SLIM core/build-linux engine (no FEA/sim) — build core/build-occt-linux for a release AppImage" >&2 ;;
        esac
        cp "$LIBCHUDWARE" "$APPDIR/usr/lib/libchudware.so"
        # Sibling runtime .so deps that live next to the occt engine (Netgen mesher,
        # lib3mf export) — libchudware.so resolves them via its RUNPATH/LD_LIBRARY_PATH,
        # so they must ride along or FEA meshing breaks on a clean machine. (The full
        # OCCT 7.9 closure is bundled by the excludelist walk just below.)
        LIBDIR="$(dirname "$LIBCHUDWARE")"
        for sib in libnglib.so libngcore.so lib3mf.so; do
            if [ -f "$LIBDIR/$sib" ]; then
                cp "$LIBDIR/$sib" "$APPDIR/usr/lib/$sib"
            fi
        done
        echo "==> bundled libchudware.so + siblings from $LIBDIR (real engine, not demo mode)"

        # OCCT 7.9 + engine runtime closure. The engine links 34 libTK*.so.7.9 plus
        # TBB and a networking tail; none of it is bundled by default, so the AppImage
        # cannot open a document on a machine without OCCT 7.9 installed (verified: the
        # libTK* deps resolve to /lib64 on the build host but are "not found" in a clean
        # fedora:rawhide container). ldd emits the FULL flattened transitive closure, so
        # seeding with libchudware.so (+ the mesh/export siblings and chudware-hostd, which
        # dlopens the engine) captures OCCT and everything OCCT itself pulls. We copy
        # every resolved dep whose basename is NOT on the AppImage excludelist — glibc,
        # libstdc++/libgcc_s, the GL/X11 graphics-driver stack, and freetype/fontconfig/
        # harfbuzz stay host-provided (they interoperate with the host's fonts/drivers).
        # AppRun prepends usr/lib to LD_LIBRARY_PATH, so presence is enough — no rpath
        # patching. PORTABILITY: keeping glibc/libstdc++ host-provided means an AppImage
        # built here (glibc 2.43) still needs host glibc >= 2.43; build on an older base
        # for broad distro coverage — a separate axis from OCCT bundling.
        EXCLUDELIST="$REPO/build/tools/excludelist"
        if [ ! -f "$EXCLUDELIST" ]; then
            echo "==> fetching AppImage excludelist (cached at build/tools/excludelist)"
            curl -sL -o "$EXCLUDELIST" \
                "https://raw.githubusercontent.com/AppImage/pkg2appimage/master/excludelist" \
                || echo "WARNING: excludelist fetch failed — dep bundling may pull host-reserved libs" >&2
        fi
        mapfile -t EXCL < <(grep -vE '^[[:space:]]*#|^[[:space:]]*$' "$EXCLUDELIST" 2>/dev/null | awk '{print $1}')
        is_excluded() {  # 0 if $1 starts with any excludelist basename (case globs = literal, +/. safe)
            local base="$1" e
            for e in "${EXCL[@]}"; do case "$base" in "$e"*) return 0 ;; esac; done
            return 1
        }
        SEEDS=("$LIBCHUDWARE")
        for s in "$APPDIR/usr/lib/libnglib.so" "$APPDIR/usr/lib/libngcore.so" \
                 "$APPDIR/usr/lib/lib3mf.so" "$APPDIR/usr/bin/chudware-hostd"; do
            [ -e "$s" ] && SEEDS+=("$s")
        done
        echo "==> bundling engine runtime closure (OCCT 7.9 + deps) into usr/lib"
        while read -r dep; do
            [ -f "$dep" ] || continue
            base="$(basename "$dep")"
            is_excluded "$base" && continue
            [ -e "$APPDIR/usr/lib/$base" ] && continue
            cp -L "$dep" "$APPDIR/usr/lib/$base"
        done < <(ldd "${SEEDS[@]}" 2>/dev/null | awk '/=> \//{print $3}' | sort -u)
        ntk=$(find "$APPDIR/usr/lib" -name 'libTK*.so.*' | wc -l)
        nlib=$(find "$APPDIR/usr/lib" -maxdepth 1 -name '*.so*' | wc -l)
        echo "==> usr/lib now has $nlib shared objects ($ntk OCCT libTK*)"
        [ "$ntk" -ge 30 ] || echo "WARNING: only $ntk OCCT libs bundled (expected ~34) — engine may not load on a clean machine" >&2
    else
        echo "WARNING: no libchudware.so found in core/build-occt-linux or core/build-linux — shipping demo mode (use --no-libchudware to silence)" >&2
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
if [ -f "$HERE/usr/lib/libchudware.so" ] && [ -z "${CHUDWARE_LIBCHUDWARE:-}" ]; then
    export CHUDWARE_LIBCHUDWARE="$HERE/usr/lib/libchudware.so"
fi
# Engine host daemon (chudware-hostd): point clients at the bundled binary, and pin
# the daemon instance key to the AppImage FILE path ($APPIMAGE, set by the runtime).
# The squashfs mount point is randomized per launch, so hashing the resolved hostd
# binary path (the default in ipc_discovery) would mint a NEW key — and daemon —
# every run. Hashing $APPIMAGE keeps one stable daemon per installed AppImage while
# two different AppImage versions still get different keys and coexist. Socket/lock
# live in $XDG_RUNTIME_DIR (host fs) — nothing daemon-related inside this read-only
# mount. Respect pre-set env so tests/dev can override.
if [ -x "$HERE/usr/bin/chudware-hostd" ] && [ -z "${CHUDWARE_HOSTD_BIN:-}" ]; then
    export CHUDWARE_HOSTD_BIN="$HERE/usr/bin/chudware-hostd"
fi
if [ -z "${CHUDWARE_INSTANCE_KEY:-}" ]; then
    export CHUDWARE_INSTANCE_KEY="$(printf %s "${APPIMAGE:-$HERE}" | sha256sum | cut -c1-12)"
fi
exec "$HERE/usr/bin/Chudware" "$@"
EOF
chmod +x "$APPDIR/AppRun"
# The staged binary is usr/bin/Chudware (with an Chudware compat symlink), matching the
# .desktop's Exec=Chudware.
# Lowercase on disk — a capital-C path here silently failed and left the AppDir with no
# .desktop, so nothing bound the staged Chudware.png as the AppImage's icon.
cp "$REPO/packaging/linux/chudware.desktop" "$APPDIR/Chudware.desktop"
cp "$REPO/assets/logo_icon.png" "$APPDIR/Chudware.png"
ln -sf Chudware.png "$APPDIR/.DirIcon"

APPIMAGETOOL="$REPO/build/tools/appimagetool"
if [ ! -x "$APPIMAGETOOL" ]; then
    echo "==> fetching appimagetool (cached at build/tools/appimagetool)"
    mkdir -p "$REPO/build/tools"
    curl -sL -o "$APPIMAGETOOL" \
        "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage"
    chmod +x "$APPIMAGETOOL"
fi

mkdir -p "$OUT"
OUTFILE="$OUT/Chudware-$VERSION-x86_64.AppImage"
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
