#!/usr/bin/env bash
# packaging/linux/build_appimage.sh — assemble a self-contained AppImage for the Chudware
# host GUI (binary named chudware).
# Protected Python services and the OMP TypeScript bridge are compiled into adjacent
# sidecars. The AppImage contains no repository-source runtime or provisioning wheelhouse.
# Requires production builds and generated protected assets:
#   python3 packaging/embed_protected_assets.py --source-root . \
#     --output-dir host/build-linux/generated/protected-assets
#   cmake -S host -B host/build-linux -G Ninja -DCMAKE_BUILD_TYPE=Release \
#     -DCHUDWARE_DEVELOPER_BUILD=OFF
#   ninja -C host/build-linux Chudware
#
# Usage:
#   packaging/linux/build_appimage.sh --prepare-signing --key-id ID \
#     --host-bin BIN --hostd-bin BIN --libchudware-bin LIB --protected-assets-dir DIR [options]
#   packaging/linux/build_appimage.sh --finalize-signing SIG --release-public-key KEY [options]
#
#   --out DIR           output directory for the .AppImage (default: <repo>/dist)
#   --host-bin FILE     explicit production GUI binary (required for prepare)
#   --hostd-bin FILE    explicit production host daemon (required for prepare)
#   --libchudware-bin FILE  explicit production engine library (required unless demo mode)
#   --protected-assets-dir DIR  generated protected tables (required for prepare)
#   --prepare-signing   stage immutable AppDir, write canonical manifest/preimage, then exit
#   --key-id ID         release key id recorded in the prepared manifest
#   --finalize-signing SIG  verify/embed detached signature and archive the retained AppDir
#   --release-public-key KEY  PEM path or canonical base64 Ed25519 release public key
#   --keep-appdir       retain AppDir after final archive creation
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUT="$REPO/dist"
WITH_LIBCHUDWARE=1
KEEP_APPDIR=0
MODE=""
KEY_ID="${CHUDWARE_RELEASE_KEY_ID:-}"
FINALIZE_SIGNATURE=""
RELEASE_PUBLIC_KEY="${CHUDWARE_RELEASE_PUBLIC_KEY:-}"
RELEASE_PYTHON="${CHUDWARE_RELEASE_PYTHON:-python3}"
PROTECTED_ASSETS_OUT=""
BIN=""
HOSTD=""
LIBCHUDWARE=""
APPIMAGETOOL_VERSION="1.9.1"
APPIMAGETOOL_SHA256="ed4ce84f0d9caff66f50bcca6ff6f35aae54ce8135408b3fa33abfc3cb384eb0"

while [ $# -gt 0 ]; do
    case "$1" in
        --out) OUT="$2"; shift 2 ;;
        --host-bin) BIN="$2"; shift 2 ;;
        --hostd-bin) HOSTD="$2"; shift 2 ;;
        --libchudware-bin) LIBCHUDWARE="$2"; shift 2 ;;
        --protected-assets-dir) PROTECTED_ASSETS_OUT="$2"; shift 2 ;;
        --with-libchudware) WITH_LIBCHUDWARE=1; shift ;;   # legacy no-op (now the default)
        --no-libchudware) WITH_LIBCHUDWARE=0; shift ;;
        --keep-appdir) KEEP_APPDIR=1; shift ;;
        --prepare-signing) [ -z "$MODE" ] || { echo "error: choose exactly one signing mode" >&2; exit 2; }; MODE=prepare; shift ;;
        --key-id) KEY_ID="$2"; shift 2 ;;
        --finalize-signing) [ -z "$MODE" ] || { echo "error: choose exactly one signing mode" >&2; exit 2; }; MODE=finalize; FINALIZE_SIGNATURE="$2"; shift 2 ;;
        --release-public-key) RELEASE_PUBLIC_KEY="$2"; shift 2 ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

[ -n "$MODE" ] || { echo "error: --prepare-signing or --finalize-signing is required" >&2; exit 2; }
if [ "$MODE" = prepare ]; then
    [ -n "$KEY_ID" ] || { echo "error: --key-id is required with --prepare-signing" >&2; exit 2; }
else
    [ -f "$FINALIZE_SIGNATURE" ] && [ -s "$FINALIZE_SIGNATURE" ] || {
        echo "error: --finalize-signing must name a detached signature" >&2; exit 2;
    }
    [ -n "$RELEASE_PUBLIC_KEY" ] || {
        echo "error: --release-public-key is required with --finalize-signing" >&2; exit 2;
    }
fi

require_production_cache() {
    local build_dir="$1" label="$2" cache="$1/CMakeCache.txt"
    [ -f "$cache" ] || { echo "error: $label has no CMakeCache.txt: $cache" >&2; exit 1; }
    grep -Fxq 'CHUDWARE_DEVELOPER_BUILD:BOOL=OFF' "$cache" || {
        echo "error: $label was not explicitly configured with CHUDWARE_DEVELOPER_BUILD=OFF" >&2
        exit 1
    }
}

build_omp_bridge() {
    local destination="$1"
    command -v bun >/dev/null 2>&1 || { echo "error: Bun is required to compile chudware-omp-bridge" >&2; exit 1; }
    echo "==> installing locked vendored OMP workspace"
    (cd "$REPO/vendor/omp" && bun install --frozen-lockfile)
    (cd "$REPO/vendor/omp" && bun build --compile --sourcemap=none --packages=bundle \
        --allow-unresolved='' --allow-unresolved='@img/sharp-*/versions' --allow-unresolved='@img/sharp-libvips-*/lib' --allow-unresolved='@img/sharp-libvips-*/package' --allow-unresolved='@img/sharp-libvips-*/versions' --allow-unresolved='@img/sharp-libvips-dev-*/include' --allow-unresolved='@img/sharp-libvips-dev-*/lib' \
        --no-compile-autoload-dotenv --no-compile-autoload-bunfig \
        --no-compile-autoload-package-json bridge/src/bridge.ts --outfile "$destination")
    [ -x "$destination" ] || { echo "error: Bun did not produce $destination" >&2; exit 1; }
}

generate_protected_assets() {
    echo "==> generating protected prompt and role tables"
    "$RELEASE_PYTHON" "$REPO/packaging/embed_protected_assets.py" \
        --source-root "$REPO" --output-dir "$PROTECTED_ASSETS_OUT"
    [ -f "$PROTECTED_ASSETS_OUT/python/chudware_protected_assets.py" ] || {
        echo "error: protected Python asset table was not generated" >&2
        exit 1
    }
    [ -f "$PROTECTED_ASSETS_OUT/include/chudware/protected_assets.generated.h" ] || {
        echo "error: protected C++ asset table was not generated" >&2
        exit 1
    }
}

prepare_signing_metadata() {
    local root="$1" request="$2"
    PYTHONPATH="$REPO${PYTHONPATH:+:$PYTHONPATH}" "$RELEASE_PYTHON" -c '
import sys
from pathlib import Path
from services.installer.payload_manifest import build_manifest, verify_manifest_payload, write_manifest, write_signing_preimage
root = Path(sys.argv[1])
manifest, canonical, _ = build_manifest(root, version=sys.argv[2], platform="linux",
                                        arch="x86_64", key_id=sys.argv[3])
write_manifest(root, canonical)
verify_manifest_payload(root, canonical)
write_signing_preimage(Path(sys.argv[4]), manifest["key_id"], canonical)
' "$root" "$VERSION" "$KEY_ID" "$request"
}

finalize_signing_metadata() {
    local root="$1"
    PYTHONPATH="$REPO${PYTHONPATH:+:$PYTHONPATH}" "$RELEASE_PYTHON" -c '
import json, sys
from pathlib import Path
from services.installer.payload_manifest import canonical_manifest_bytes, finalize_signature
root = Path(sys.argv[1])
canonical = (root / "payload-manifest.v1.json").read_bytes()
manifest = json.loads(canonical.decode("utf-8"))
if canonical_manifest_bytes(manifest) != canonical:
    raise SystemExit("retained payload manifest is not exact canonical JSON")
finalize_signature(root, key_id=manifest["key_id"], canonical_json=canonical,
                   signature_path=Path(sys.argv[2]), public_key=sys.argv[3])
' "$root" "$FINALIZE_SIGNATURE" "$RELEASE_PUBLIC_KEY"
    chmod 0644 "$root/payload-manifest.v1.json" "$root/payload-manifest.v1.sig"
}

emit_artifact_signing_request() {
    local artifact="$1" request="$artifact.artifact-signing-request.bin"
    PYTHONPATH="$REPO${PYTHONPATH:+:$PYTHONPATH}" "$RELEASE_PYTHON" -c '
import hashlib, sys
from pathlib import Path
artifact, request, key_id, version = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], sys.argv[4]
digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
request.write_bytes(b"\0".join((b"chudware-artifact-v1", key_id.encode("ascii"),
    version.encode("ascii"), b"linux", b"x86_64", digest.encode("ascii"))))
' "$artifact" "$request" "$KEY_ID" "$VERSION"
    echo "==> artifact signing request: $request"
}

verify_staged_manifest() {
    local root="$1"
    PYTHONPATH="$REPO${PYTHONPATH:+:$PYTHONPATH}" "$RELEASE_PYTHON" -c '
import sys
from pathlib import Path
from services.installer.payload_manifest import verify_manifest_payload
root = Path(sys.argv[1])
verify_manifest_payload(root, (root / "payload-manifest.v1.json").read_bytes())
' "$root"
}

build_python_sidecar() {
    local module="$1" destination="$2" name work
    name="$(basename "$destination")"
    work="$OUT/.nuitka-$name"
    rm -rf "$work"
    mkdir -p "$work"
    "$RELEASE_PYTHON" -c 'import nuitka' >/dev/null 2>&1 || {
        echo "error: Nuitka is missing; install packaging/requirements-release.txt into the release Python" >&2
        exit 1
    }
    echo "==> compiling $name"
    local -a includes=("--include-module=chudware_protected_assets" "--include-module=services.$module")
    if [ "$module" = "worker_main" ]; then
        includes+=(
            --include-module=services.parts.vendor_parts
            --include-module=services.deliverables.worker_rpc
            --include-module=services.parts.worker_rpc
            --include-module=services.agent.worker_rpc
            --include-module=services.reveng.worker_rpc
            --include-module=services.dag.worker_rpc
            --include-module=services.agent.subagent_rpc
            --include-module=services.plm.worker_rpc
            --include-module=services.sim.em.worker_rpc
            --include-module=services.sim.multiphysics_rpc
            --include-module=services.manufacturing.printers.worker_rpc
        )
    fi
    (cd "$REPO" && PYTHONPATH="$PROTECTED_ASSETS_OUT/python${PYTHONPATH:+:$PYTHONPATH}" \
        "$RELEASE_PYTHON" -m nuitka \
        --mode=onefile --remove-output \
        --output-dir="$work" --output-filename="$name" \
        --nofollow-import-to='*.tests' --nofollow-import-to='*.test_*' \
        "${includes[@]}" services/compiled_entry.py)
    [ -x "$work/$name" ] || { echo "error: Nuitka did not produce $work/$name" >&2; exit 1; }
    cp "$work/$name" "$destination"
    chmod 0755 "$destination"
    rm -rf "$work"
}

audit_production_payload() {
    local root="$1" bad
    bad="$(find "$root" \( -type l -o -type f \( \
        -name '*.py' -o -name '*.pyc' -o -name '*.pyo' -o -name '*.ts' -o -name '*.tsx' \
        -o -name '*.js' -o -name '*.jsx' -o -name '*.map' -o -name '*.pdb' \
        -o -name 'test_*' -o -name '*_test.*' \) -o -type d \( \
        -name '__pycache__' -o -name 'node_modules' -o -name '.git' -o -name '.github' \
        -o -name '.svn' -o -name '*.dSYM' \) \) -print -quit)"
    [ -z "$bad" ] || { echo "error: forbidden source/debug/repository artifact survived staging: $bad" >&2; exit 1; }
}

APPDIR="$OUT/AppDir"
if [ "$MODE" = prepare ]; then
    [ -n "$BIN" ] && [ -n "$HOSTD" ] && [ -n "$PROTECTED_ASSETS_OUT" ] || {
        echo "error: --host-bin, --hostd-bin, and --protected-assets-dir are required" >&2
        exit 2
    }
    if [ ! -x "$BIN" ]; then
        echo "error: $BIN not found — build it first:" >&2
        echo '  ./build.sh all' >&2
        exit 1
    fi

    VERSION_HELPER="$(sed -n '/^chudware_read_version() {/,/^}/p' "$REPO/build.sh")"
    [ -n "$VERSION_HELPER" ] || { echo "error: chudware_read_version missing from build.sh" >&2; exit 1; }
    # shellcheck disable=SC2294
    _eval_version_helper() { eval "$VERSION_HELPER"; }
    _eval_version_helper
    HOST_BUILD="$(dirname "$(dirname "$BIN")")"
    chudware_read_version "$HOST_BUILD" || {
        echo "error: generated/api/chudware_version.h is missing or invalid under $HOST_BUILD" >&2
        exit 1
    }
    VERSION="$CHUDWARE_VERSION"
    echo "==> version from generated header: $VERSION"
    rm -rf "$APPDIR"
    HOST_BUILD="$(dirname "$(dirname "$BIN")")"
    require_production_cache "$HOST_BUILD" "host GUI"
    # The staging below copies straight into these directories; nothing else
    # creates them (the later `mkdir -p "$OUT"` runs after all of it), so a
    # prepare run without this died on the first cp.
    mkdir -p "$APPDIR/usr/bin" "$APPDIR/usr/lib"
else
    [ -d "$APPDIR" ] && [ -f "$APPDIR/payload-manifest.v1.json" ] || {
        echo "error: retained prepared AppDir is missing under $OUT" >&2; exit 1;
    }
    VERSION="$(PYTHONPATH="$REPO" "$RELEASE_PYTHON" -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["version"])' "$APPDIR/payload-manifest.v1.json")"
fi

if [ "$MODE" = prepare ]; then

echo "==> staging binary + resources"
cp "$BIN" "$APPDIR/usr/bin/chudware"
ASSETS_SRC="$HOST_BUILD/bin/assets"
[ -d "$ASSETS_SRC" ] || {
    echo "error: production AppImage requires staged GUI assets: $ASSETS_SRC" >&2
    exit 1
}
rsync -a --exclude='*.py' --exclude='*.pyc' --exclude='test_*' \
    --exclude='.git*' "$ASSETS_SRC/" "$APPDIR/usr/bin/assets/"

# CEF is mandatory on Linux: the GUI and browser daemon initialize it at runtime.
# Require the complete CMake-staged closure before any manifest is produced.
CEF_SRC="$(dirname "$BIN")"
CEF_REQUIRED=(
    libcef.so chudware_cef_helper chrome-sandbox icudtl.dat v8_context_snapshot.bin
    chrome_100_percent.pak chrome_200_percent.pak resources.pak
    libEGL.so libGLESv2.so
)
for f in "${CEF_REQUIRED[@]}"; do
    [ -f "$CEF_SRC/$f" ] || {
        echo "error: production AppImage requires CEF runtime file: $CEF_SRC/$f" >&2
        exit 1
    }
done
[ -d "$CEF_SRC/locales" ] && find "$CEF_SRC/locales" -maxdepth 1 -type f -name '*.pak' -print -quit | grep -q . || {
    echo "error: production AppImage requires CEF locale resources: $CEF_SRC/locales" >&2
    exit 1
}
echo "==> staging mandatory CEF runtime"
CEF_STRIPPED="$REPO/build/tools/libcef-stripped.so"
mkdir -p "$REPO/build/tools"
if [ ! -f "$CEF_STRIPPED" ] || [ "$CEF_SRC/libcef.so" -nt "$CEF_STRIPPED" ]; then
    strip --strip-debug -o "$CEF_STRIPPED" "$CEF_SRC/libcef.so"
fi
cp "$CEF_STRIPPED" "$APPDIR/usr/bin/libcef.so"
for f in "${CEF_REQUIRED[@]:1}" libvk_swiftshader.so libvulkan.so.1 vk_swiftshader_icd.json; do
    [ -e "$CEF_SRC/$f" ] && cp "$CEF_SRC/$f" "$APPDIR/usr/bin/$f"
done
cp -a "$CEF_SRC/locales" "$APPDIR/usr/bin/locales"

# Engine host daemon (chudware-hostd, core/hostd) — the shared engine process the GUI
# and MCP servers attach to (CHUDWARE_ENGINE_DAEMON=1). Same build-dir preference as
# the engine below: full occt build first, slim fallback with a loud warning.
HOSTD_BUILD="$(dirname "$(dirname "$HOSTD")")"
require_production_cache "$HOSTD_BUILD" "chudware-hostd"
cp "$HOSTD" "$APPDIR/usr/bin/chudware-hostd"
echo "==> staged chudware-hostd ($HOSTD)"

if [ "$WITH_LIBCHUDWARE" = "1" ]; then
    [ -n "$LIBCHUDWARE" ] && [ -f "$LIBCHUDWARE" ] || {
        echo "error: --libchudware-bin is required unless --no-libchudware is used" >&2
        exit 1
    }
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
fi

# Protected Python and TypeScript are compiled into adjacent production sidecars. Packaged
# builds never provision from repository source and never fall back to source entrypoints.
[ -f "$REPO/services/compiled_entry.py" ] || {
    echo "error: services/compiled_entry.py is required for release sidecars" >&2
    exit 1
}
generate_protected_assets
build_python_sidecar worker_main "$APPDIR/usr/bin/chudware-worker"
build_python_sidecar mcp.server "$APPDIR/usr/bin/chudware-mcp"
build_omp_bridge "$APPDIR/usr/bin/chudware-omp-bridge"


cp "$REPO/LICENSE-EULA.txt" "$APPDIR/usr/bin/LICENSE-EULA.txt"
cat > "$APPDIR/README" <<'EOF'
Chudware AppImage
=================

Launching this AppImage is subject to the Chudware End User License Agreement bundled at
usr/bin/LICENSE-EULA.txt. Release payload ownership and integrity are recorded by the top-level
payload-manifest.v1.json and payload-manifest.v1.sig files.
EOF

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
exec "$HERE/usr/bin/chudware" "$@"
EOF
chmod +x "$APPDIR/AppRun"
# The staged binary is usr/bin/chudware, matching the desktop entry's Exec.
# Lowercase on disk so the desktop entry's Icon=chudware resolves exactly.
cp "$REPO/packaging/linux/chudware.desktop" "$APPDIR/chudware.desktop"
cp "$REPO/assets/logo_icon.png" "$APPDIR/chudware.png"
cp "$APPDIR/chudware.png" "$APPDIR/.DirIcon"

audit_production_payload "$APPDIR"
REQUEST="$OUT/Chudware-$VERSION-linux-x86_64.payload-signing-request.bin"
prepare_signing_metadata "$APPDIR" "$REQUEST"
echo "==> prepared immutable AppDir: $APPDIR"
echo "==> signing request: $REQUEST"
exit 0
fi

finalize_signing_metadata "$APPDIR"
audit_production_payload "$APPDIR"
verify_staged_manifest "$APPDIR"

APPIMAGETOOL="$REPO/build/tools/appimagetool-$APPIMAGETOOL_VERSION-x86_64.AppImage"
APPIMAGETOOL_URL="https://github.com/AppImage/appimagetool/releases/download/$APPIMAGETOOL_VERSION/appimagetool-x86_64.AppImage"
if [ ! -f "$APPIMAGETOOL" ]; then
    echo "==> fetching pinned appimagetool $APPIMAGETOOL_VERSION"
    mkdir -p "$REPO/build/tools"
    curl -fL --retry 3 -o "$APPIMAGETOOL.tmp" "$APPIMAGETOOL_URL"
    mv "$APPIMAGETOOL.tmp" "$APPIMAGETOOL"
fi
actual_appimagetool_sha256="$(sha256sum "$APPIMAGETOOL" | awk '{print $1}')"
[ "$actual_appimagetool_sha256" = "$APPIMAGETOOL_SHA256" ] || {
    echo "error: appimagetool $APPIMAGETOOL_VERSION SHA256 mismatch: expected $APPIMAGETOOL_SHA256, got $actual_appimagetool_sha256" >&2
    exit 1
}
chmod 0755 "$APPIMAGETOOL"

mkdir -p "$OUT"
OUTFILE="$OUT/Chudware-$VERSION-linux-x86_64.AppImage"
echo "==> checking staged ELF dependencies"
ldd_targets=()
while IFS= read -r -d '' candidate; do
    if file -b "$candidate" | grep -q 'ELF'; then
        ldd_targets+=("$candidate")
    fi
done < <(find "$APPDIR/usr/bin" "$APPDIR/usr/lib" -type f -print0)
[ "${#ldd_targets[@]}" -gt 0 ] || { echo "error: staged package contains no ELF binaries" >&2; exit 1; }
unresolved="$(
    LD_LIBRARY_PATH="$APPDIR/usr/lib:${LD_LIBRARY_PATH:-}" \
        ldd "${ldd_targets[@]}" 2>/dev/null | awk '/not found/{print}'
)"
if [ -n "$unresolved" ]; then
    echo "error: unresolved staged libraries:" >&2
    printf '%s\n' "$unresolved" >&2
    exit 1
fi
echo "==> staged ELF dependency check: ${#ldd_targets[@]} files, zero unresolved libraries"

SIGN_IDENTITY="${CHUDWARE_SIGN_IDENTITY:-}"
NOTARIZE_PROFILE="${CHUDWARE_NOTARIZE_PROFILE:-}"
if [ -z "$SIGN_IDENTITY" ]; then
    echo "WARNING: unsigned build — SmartScreen/Gatekeeper will warn users" >&2
else
    echo "==> CHUDWARE_SIGN_IDENTITY is set; Linux platform signing is supplied by packaging/publish.py's Ed25519 release signature"
fi
[ -z "$NOTARIZE_PROFILE" ] || echo "==> CHUDWARE_NOTARIZE_PROFILE is macOS-only and is not used for this AppImage"

echo "==> building $OUTFILE"
if ! ARCH=x86_64 VERSION="$VERSION" "$APPIMAGETOOL" "$APPDIR" "$OUTFILE" 2>"$OUT/appimagetool.log"; then
    echo "==> direct run failed, retrying with --appimage-extract-and-run (no-FUSE environments)"
    ARCH=x86_64 VERSION="$VERSION" "$APPIMAGETOOL" --appimage-extract-and-run "$APPDIR" "$OUTFILE"
fi
emit_artifact_signing_request "$OUTFILE"
if [ "$KEEP_APPDIR" != "1" ]; then
    rm -rf "$APPDIR"
fi

echo "==> built: $OUTFILE"
ls -la "$OUTFILE"
