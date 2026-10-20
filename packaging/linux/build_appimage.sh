#!/usr/bin/env bash
# packaging/linux/build_appimage.sh — assemble a self-contained AppImage for the Chudware
# host GUI (binary named chudware).
# Protected Python services and the compiled Chudcode RPC binary are adjacent sidecars.
# The AppImage contains no repository-source runtime or provisioning wheelhouse.
# Requires production builds and generated protected assets:
#   python3 packaging/embed_protected_assets.py --source-root . \
#     --output-dir host/build-linux/generated/protected-assets
#   cmake -S host -B host/build-linux -G Ninja -DCMAKE_BUILD_TYPE=Release \
#     -DCHUDWARE_DEVELOPER_BUILD=OFF -DCHUDWARE_LTO=ON
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
#   --chud-bin FILE     verified pinned Chudcode release binary (or CHUDWARE_CHUD_BIN)
#   --prepare-signing   stage immutable AppDir, write canonical manifest/preimage, then exit
#   --key-id ID         release key id recorded in the prepared manifest
#   --finalize-signing SIG  verify/embed detached signature and archive the retained AppDir
#   --release-public-key KEY  PEM path or canonical base64 Ed25519 release public key
#   --keep-appdir       retain AppDir after final archive creation
set -euo pipefail
# Clear startup/loader overrides before the first child process, including the
# independently trusted base used to inspect the compiler runtime offline.
for name in "${!PYTHON@}" "${!LD_@}" "${!DYLD_@}"; do
    [ -z "$name" ] || unset "$name"
done
unset __PYVENV_LAUNCHER__ VIRTUAL_ENV

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUT="$REPO/dist"
WITH_LIBCHUDWARE=1
KEEP_APPDIR=0
MODE=""
KEY_ID="${CHUDWARE_RELEASE_KEY_ID:-}"
FINALIZE_SIGNATURE=""
RELEASE_PUBLIC_KEY="${CHUDWARE_RELEASE_PUBLIC_KEY:-}"
RELEASE_PYTHON="${CHUDWARE_RELEASE_PYTHON:-}"
TRUSTED_BASE="${CHUDWARE_BASE_PYTHON:-${CHUDWARE_CI_BASE_PYTHON:-}}"
PROTECTED_ASSETS_OUT=""
BIN=""
HOSTD=""
LIBCHUDWARE=""
CHUD_BIN="${CHUDWARE_CHUD_BIN:-}"
APPIMAGETOOL_VERSION="1.9.1"
APPIMAGETOOL_SHA256="ed4ce84f0d9caff66f50bcca6ff6f35aae54ce8135408b3fa33abfc3cb384eb0"

while [ $# -gt 0 ]; do
    case "$1" in
        --out) OUT="$2"; shift 2 ;;
        --host-bin) BIN="$2"; shift 2 ;;
        --hostd-bin) HOSTD="$2"; shift 2 ;;
        --libchudware-bin) LIBCHUDWARE="$2"; shift 2 ;;
        --chud-bin) CHUD_BIN="$2"; shift 2 ;;
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
    [ -n "$CHUD_BIN" ] || { echo "error: --chud-bin or CHUDWARE_CHUD_BIN is required" >&2; exit 2; }
else
    [ -f "$FINALIZE_SIGNATURE" ] && [ -s "$FINALIZE_SIGNATURE" ] || {
        echo "error: --finalize-signing must name a detached signature" >&2; exit 2;
    }
    [ -n "$RELEASE_PUBLIC_KEY" ] || {
        echo "error: --release-public-key is required with --finalize-signing" >&2; exit 2;
    }
fi
case "$TRUSTED_BASE" in /*) ;; *) echo "error: CHUDWARE_BASE_PYTHON must identify a separately authenticated absolute base Python" >&2; exit 1 ;; esac
case "$RELEASE_PYTHON" in /*) ;; *) echo "error: CHUDWARE_RELEASE_PYTHON must identify the managed compiler runtime" >&2; exit 1 ;; esac
COMPILER_VENV="$(dirname "$(dirname "$RELEASE_PYTHON")")"
"$TRUSTED_BASE" -I -S -c 'import sys;sys.path.insert(0,sys.argv[1]);from services.installer.provision import main;sys.exit(main(sys.argv[2:]))' \
    "$REPO" --check-inputs --base-python "$TRUSTED_BASE" --venv-dir "$COMPILER_VENV" \
    --wheelhouse "${CHUDWARE_WHEELHOUSE:-$REPO/wheelhouse}"

release_python() {
    "$RELEASE_PYTHON" -I -c 'import sys;sys.path.insert(0,sys.argv[1]);from services.installer.provision import run_python_entry;run_python_entry(sys.argv[1],sys.argv[2],sys.argv[3:])' \
        "$REPO" "${PROTECTED_ASSETS_OUT:+$PROTECTED_ASSETS_OUT/python}" "$@"
}


require_production_cache() {
    local build_dir="$1" label="$2" cache="$1/CMakeCache.txt"
    [ -f "$cache" ] || { echo "error: $label has no CMakeCache.txt: $cache" >&2; exit 1; }
    grep -Fxq 'CHUDWARE_DEVELOPER_BUILD:BOOL=OFF' "$cache" || {
        echo "error: $label was not explicitly configured with CHUDWARE_DEVELOPER_BUILD=OFF" >&2
        exit 1
    }
}

stage_chudcode() {
    local destination="$1"
    [ -n "$CHUD_BIN" ] || {
        echo "error: --chud-bin or CHUDWARE_CHUD_BIN is required" >&2
        exit 1
    }
    release_python "$REPO/scripts/pin_chudcode.py" stage \
        --target linux-x86_64 --input "$CHUD_BIN" --output "$destination"
    release_python "$REPO/scripts/pin_chudcode.py" stage-notices \
        --output-dir "$(dirname "$destination")"
    cp "$REPO/vendor/chudcode/CHUDCODE_ARTIFACT.lock" \
        "$(dirname "$destination")/CHUDCODE_ARTIFACT.lock"
}

generate_protected_assets() {
    echo "==> generating protected prompt and role tables"
    release_python "$REPO/packaging/embed_protected_assets.py" \
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
    release_python -c '
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
    release_python -c '
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
    # Split: `local a="$1" b="$a"` expands every word before the builtin
    # assigns any of them, so $artifact would still be unset (fatal under -u).
    local artifact="$1"
    local request="$artifact.artifact-signing-request.bin"
    release_python -c '
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
    release_python -c '
import sys
from pathlib import Path
from services.installer.payload_manifest import verify_manifest_payload
root = Path(sys.argv[1])
verify_manifest_payload(root, (root / "payload-manifest.v1.json").read_bytes())
' "$root"
}

stage_desktop_entry() {
    # appimagetool MUTATES the AppDir's desktop entry while packing: it writes
    # X-AppImage-Version=$VERSION into the [Desktop Entry] group (via GKeyFile,
    # i.e. after the group's last key=value line, before trailing comments).
    # The payload manifest is hashed and signed BEFORE packing, so leaving that
    # mutation to the tool ships an AppImage whose desktop entry no longer
    # matches the signed manifest — the extracted-artifact verification and the
    # runtime integrity check both reject it. Writing the key here makes the
    # staged file a fixed point of the tool's rewrite (proven: a second pack of
    # the rewritten file is byte-identical), and the post-pack check below
    # fails loudly if a future appimagetool touches anything else.
    local source="$1" destination="$2" version="$3"
    release_python -c '
import re, sys
from pathlib import Path
source, destination, version = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
lines = source.read_text(encoding="utf-8").splitlines(keepends=True)
group = next((i for i, line in enumerate(lines) if line.strip() == "[Desktop Entry]"), None)
if group is None:
    raise SystemExit("desktop entry has no [Desktop Entry] group")
end = next((i for i in range(group + 1, len(lines)) if lines[i].lstrip().startswith("[")), len(lines))
entry = f"X-AppImage-Version={version}\n"
key = re.compile(r"\s*X-AppImage-Version\s*=")
existing = next((i for i in range(group + 1, end) if key.match(lines[i])), None)
if existing is not None:
    lines[existing] = entry
else:
    keys = [i for i in range(group + 1, end) if re.match(r"\s*[A-Za-z0-9-]+(\[[^]]*\])?\s*=", lines[i])]
    if not keys:
        raise SystemExit("desktop entry group has no keys")
    lines.insert(keys[-1] + 1, entry)
destination.write_text("".join(lines), encoding="utf-8")
' "$source" "$destination" "$version"
    chmod 0644 "$destination"
}

build_python_sidecar() {
    local module="$1" destination="$2" name work
    name="$(basename "$destination")"
    work="$OUT/.nuitka-$name"
    rm -rf "$work"
    mkdir -p "$work"
    release_python -c 'import nuitka' >/dev/null 2>&1 || {
        echo "error: Nuitka is missing; install packaging/requirements-release.txt into the release Python" >&2
        exit 1
    }
    echo "==> compiling $name"
    # pycolmap is a bundled runtime package declared in packaging/requirements-release.txt;
    # forcing it makes a release Python without it fail here instead of shipping without it.
    # Both sidecars re-enter compiled_entry as `--module services.sim.job_worker <job_id>`
    # for background simulation jobs (services/sim/jobs.py), and the topology tool/study
    # modules back those jobs; forcing them fails the build if any is missing.
    local -a includes=("--include-module=chudware_protected_assets" "--include-module=services.$module"
                       "--include-package=chudcode_rpc"
                       "--include-package=pycolmap"
                       "--include-module=services.sim.job_worker"
                       "--include-module=services.sim.topology"
                       "--include-module=services.mcp.topology_tools")
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
    (cd "$REPO" && release_python -m nuitka \
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

# Learned mesh-advisor runtime (the polymesh ONNX advisor).
#
# core/sim/mesher.cpp resolves the package relative to libchudware.so, so it belongs in
# usr/lib beside the engine. Only the four runtime artifacts are carried; campaign evidence
# and dashboards stay out of the AppImage.
#
# The engine's own DT_NEEDED entries decide whether any of this ships. An engine configured
# without the advisor links no ONNX Runtime and needs no package, so such a build stages
# nothing and stays valid. An advisor-linked engine whose package is incomplete would ship a
# permanently dead optimizer that silently falls back on every study, so that case fails
# packaging instead of reaching a customer.
ADVISOR_PACKAGE_DIR="polymesh-advisor"
ADVISOR_REQUIRED_FILES=(model.onnx normalization.json clamps.json ood.json)

engine_onnxruntime_sonames() {  # DT_NEEDED names matching libonnxruntime, one per line
    readelf -d "$1" 2>/dev/null |
        sed -n 's/.*(NEEDED).*\[\(libonnxruntime[^]]*\)\].*/\1/p'
}

stage_polymesh_advisor() {
    local engine="$1" destdir="$2" srcdir f soname candidate found
    srcdir="$(dirname "$engine")"
    command -v readelf >/dev/null 2>&1 || {
        echo "error: readelf (binutils) is required to check the engine's ONNX Runtime linkage" >&2
        exit 1
    }
    local sonames=()
    mapfile -t sonames < <(engine_onnxruntime_sonames "$engine")
    if [ "${#sonames[@]}" -eq 0 ]; then
        echo "==> engine links no ONNX Runtime: mesh advisor disabled, no advisor payload to stage"
        return 0
    fi
    for f in "${ADVISOR_REQUIRED_FILES[@]}"; do
        [ -f "$srcdir/$ADVISOR_PACKAGE_DIR/$f" ] || {
            echo "error: $engine links ONNX Runtime but its advisor package is incomplete:" >&2
            echo "       missing $srcdir/$ADVISOR_PACKAGE_DIR/$f" >&2
            echo "       rebuild the engine so CMake stages the advisor package; refusing to ship a dead optimizer" >&2
            exit 1
        }
    done
    mkdir -p "$destdir/$ADVISOR_PACKAGE_DIR"
    for f in "${ADVISOR_REQUIRED_FILES[@]}"; do
        cp -L "$srcdir/$ADVISOR_PACKAGE_DIR/$f" "$destdir/$ADVISOR_PACKAGE_DIR/$f"
        chmod 0644 "$destdir/$ADVISOR_PACKAGE_DIR/$f"
    done
    # One ONNX Runtime, staged under the exact name the engine asks for. The build tree
    # keeps libonnxruntime.so plus its versioned symlinks while the engine records the
    # SONAME (libonnxruntime.so.1), and the payload may hold no symlinks at all
    # (audit_production_payload), so the resolvable name has to be the real file.
    for soname in "${sonames[@]}"; do
        found=""
        for candidate in "$srcdir/$soname" "$srcdir/libonnxruntime.so"; do
            if [ -f "$candidate" ]; then found="$candidate"; break; fi
        done
        [ -n "$found" ] || {
            echo "error: $engine needs $soname but no ONNX Runtime is staged beside it in $srcdir" >&2
            echo "       rebuild the engine so CMake stages the ONNX Runtime; the advisor cannot load without it" >&2
            exit 1
        }
        cp -L "$found" "$destdir/$soname"
        chmod 0755 "$destdir/$soname"
    done
    echo "==> staged advisor package (${#ADVISOR_REQUIRED_FILES[@]} artifacts) + ${#sonames[@]} ONNX Runtime into $destdir"
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
    VERSION="$(release_python -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["version"])' "$APPDIR/payload-manifest.v1.json")"
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
        # Optional lib3mf runtime lives beside the engine and must ride along when present.
        LIBDIR="$(dirname "$LIBCHUDWARE")"
        if [ -f "$LIBDIR/lib3mf.so" ]; then
            cp "$LIBDIR/lib3mf.so" "$APPDIR/usr/lib/lib3mf.so"
        fi
        echo "==> bundled libchudware.so + siblings from $LIBDIR (real engine, not demo mode)"

        # Advisor model package + ONNX Runtime, beside the engine that resolves them. Runs
        # before the closure walk below so the walk finds the staged runtime already in
        # place instead of pulling the build tree's _deps copy under a name the packaged
        # engine cannot resolve.
        stage_polymesh_advisor "$LIBCHUDWARE" "$APPDIR/usr/lib"

        # OCCT 7.9 + engine runtime closure. The engine links 34 libTK*.so.7.9 plus
        # TBB and a networking tail; none of it is bundled by default, so the AppImage
        # cannot open a document on a machine without OCCT 7.9 installed (verified: the
        # libTK* deps resolve to /lib64 on the build host but are "not found" in a clean
        # fedora:rawhide container). ldd emits the FULL flattened transitive closure, so
        # seeding with libchudware.so, its optional siblings, and chudware-hostd captures
        # OCCT and everything OCCT itself pulls. We copy
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
        # HOST-PROVIDED BY US, on top of the upstream excludelist.
        #
        # `ldd` reports only link-time deps. A library that finds its real implementation by
        # dlopen()ing a plugin at runtime therefore gets bundled WITHOUT the plugins it needs,
        # which is strictly worse than not bundling it: the host copy would have worked.
        #
        # libflexiblas is exactly that, and shipping it naked was a hard crash. FlexiBLAS is a
        # BLAS *dispatcher*: at init it reads the host's /etc/flexiblasrc (e.g.
        # "default = openblas-openmp") and dlopens the matching backend from the host's plugin
        # directory. None of those backends are link-time deps, so none were bundled, and the
        # bundled dispatcher could not even load its fallback:
        #
        #   flexiblas  Failed to load the BLAS fallback library.  Abort!
        #
        # abort() inside a dlopen() has no recoverable failure path, so it killed the whole
        # process. chudware-hostd dlopens libchudware.so from its loader thread AFTER binding
        # the IPC socket, so the daemon accepted a connection, the GUI announced "Engine daemon
        # reconnected", and then the process died — restarting every ~15s indefinitely, with
        # every RPC failing "Connection refused" (observed 2026-08-27: 27 crashes in 7 minutes,
        # 27 coredumps, all SIGABRT in flexiblas_init under core::CppCore::ensureLoaded).
        #
        # Leaving it host-provided is correct, not a workaround: locating the host's BLAS is
        # the dispatcher's entire job, and any distro whose packages link it ships it.
        LOCAL_EXCL=(libflexiblas)
        is_locally_excluded() {
            local base="$1" e
            for e in "${LOCAL_EXCL[@]}"; do case "$base" in "$e"*) return 0 ;; esac; done
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
            is_locally_excluded "$base" && continue
            [ -e "$APPDIR/usr/lib/$base" ] && continue
            cp -L "$dep" "$APPDIR/usr/lib/$base"
        done < <(ldd "${SEEDS[@]}" 2>/dev/null | awk '/=> \//{print $3}' | sort -u)
        ntk=$(find "$APPDIR/usr/lib" -name 'libTK*.so.*' | wc -l)
        nlib=$(find "$APPDIR/usr/lib" -maxdepth 1 -name '*.so*' | wc -l)
        echo "==> usr/lib now has $nlib shared objects ($ntk OCCT libTK*)"
        [ "$ntk" -ge 30 ] || echo "WARNING: only $ntk OCCT libs bundled (expected ~34) — engine may not load on a clean machine" >&2

        # GATE: the bundled engine must actually LOAD from the staged tree.
        #
        # Everything above only proves files were copied. It cannot catch a bundled library
        # that aborts in its own initializer, which is precisely how the flexiblas crash
        # shipped: the AppImage built clean, passed its lib counts, and then killed hostd on
        # every start. This reproduces exactly what chudware-hostd does — dlopen the engine
        # with usr/lib on the search path, the same thing AppRun sets up — so any dep that
        # aborts, is missing, or is unresolvable fails the BUILD instead of the user's machine.
        if command -v python3 >/dev/null 2>&1; then
            echo "==> verifying the bundled engine dlopens from the staged AppDir"
            if LD_LIBRARY_PATH="$APPDIR/usr/lib" python3 -c "
import ctypes, sys
try:
    ctypes.CDLL('$APPDIR/usr/lib/libchudware.so')
except BaseException as e:
    print('dlopen failed: %s' % e, file=sys.stderr)
    sys.exit(1)
" 2>&1; then
                echo "==> engine dlopen OK"
            else
                echo "error: the bundled libchudware.so does not load from the staged AppDir." >&2
                echo "       A dependency is missing, unresolvable, or aborts during init." >&2
                echo "       This is a SHIP BLOCKER: hostd dlopens this on every start, and an" >&2
                echo "       abort inside dlopen kills the daemon with no recovery path." >&2
                echo "       If a bundled dispatcher-style library resolves its real backend by" >&2
                echo "       dlopen (BLAS, GL, codecs), add it to LOCAL_EXCL and leave it" >&2
                echo "       host-provided rather than shipping it without its plugins." >&2
                exit 1
            fi
        else
            echo "WARNING: python3 unavailable — skipping the engine dlopen gate" >&2
        fi
fi

# Protected Python services and Chudcode are compiled sidecars. Packaged builds never
# provision from repository source and never fall back to source entrypoints.
[ -f "$REPO/services/compiled_entry.py" ] || {
    echo "error: services/compiled_entry.py is required for release sidecars" >&2
    exit 1
}
generate_protected_assets
build_python_sidecar worker_main "$APPDIR/usr/bin/chudware-worker"
build_python_sidecar mcp.server "$APPDIR/usr/bin/chudware-mcp"
stage_chudcode "$APPDIR/usr/bin/chud"


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
if [ -x "$HERE/usr/bin/chud" ] && [ -z "${CHUDWARE_CHUDCODE_BIN:-}" ]; then
    export CHUDWARE_CHUDCODE_BIN="$HERE/usr/bin/chud"
fi
if [ -z "${CHUDWARE_INSTANCE_KEY:-}" ]; then
    export CHUDWARE_INSTANCE_KEY="$(printf %s "${APPIMAGE:-$HERE}" | sha256sum | cut -c1-12)"
fi
exec "$HERE/usr/bin/chudware" "$@"
EOF
chmod +x "$APPDIR/AppRun"
# The staged binary is usr/bin/chudware, matching the desktop entry's Exec.
# Lowercase on disk so the desktop entry's Icon=chudware resolves exactly.
stage_desktop_entry "$REPO/packaging/linux/chudware.desktop" "$APPDIR/chudware.desktop" "$VERSION"
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
    # The first attempt gets as far as writing the AppImage (mksquashfs reaches 100%)
    # before its FUSE mount fails, and mksquashfs will not write over that leftover:
    # the retry died with "mksquashfs exited with code 1 / sfs_mksquashfs error", which
    # is how the nightly Linux beta failed on 2026-09-04. Start the retry from nothing.
    rm -f "$OUTFILE"
    ARCH=x86_64 VERSION="$VERSION" "$APPIMAGETOOL" --appimage-extract-and-run "$APPDIR" "$OUTFILE"
fi
# appimagetool rewrites files inside the AppDir as it packs (see
# stage_desktop_entry). The packed bytes are the shipped payload, so re-verify
# the manifest against the staged tree: a mismatch here means the artifact would
# fail its own runtime integrity check, and must never reach the publish step.
verify_staged_manifest "$APPDIR"
echo "==> packed payload still matches the signed manifest"
emit_artifact_signing_request "$OUTFILE"
# The AppDir holds the only copy of the signed manifest pair and is about to be
# removed, but finalize_release.py's `complete` stage re-reads that manifest to
# name it (and its key id) in the publish bundle: without this it aborts with
# "handoff must contain exactly one payload-manifest.v1.json, found 0" and the
# release ships with no evidence bundle, which blocks promotion to stable.
cp "$APPDIR/payload-manifest.v1.json" "$OUT/payload-manifest.v1.json"
cp "$APPDIR/payload-manifest.v1.sig" "$OUT/payload-manifest.v1.sig"
if [ "$KEEP_APPDIR" != "1" ]; then
    rm -rf "$APPDIR"
fi

echo "==> built: $OUTFILE"
ls -la "$OUTFILE"
