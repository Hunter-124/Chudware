#!/usr/bin/env bash
# Assemble the AppleClang host build into a redistributable, source-free Chudware.app and zip.
# Two phase: --prepare-signing --key-id ID retains the immutable .app and emits a preimage;
# --finalize-signing SIG --release-public-key KEY seals and archives that exact retained bundle.
# Release configure: generate into host/build-macos-<arch>/generated/protected-assets, then
# configure host and core/hostd with -DCHUDWARE_DEVELOPER_BUILD=OFF -DCHUDWARE_LTO=ON.
set -euo pipefail
for name in "${!PYTHON@}" "${!LD_@}" "${!DYLD_@}"; do
    [ -z "$name" ] || unset "$name"
done
unset __PYVENV_LAUNCHER__ VIRTUAL_ENV

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUT="$REPO/dist"
KEEP_APP=0
MODE=""
KEY_ID="${CHUDWARE_RELEASE_KEY_ID:-}"
FINALIZE_SIGNATURE=""
RELEASE_PUBLIC_KEY="${CHUDWARE_RELEASE_PUBLIC_KEY:-}"
RELEASE_PYTHON="${CHUDWARE_RELEASE_PYTHON:-}"
TRUSTED_BASE="${CHUDWARE_BASE_PYTHON:-${CHUDWARE_CI_BASE_PYTHON:-}}"
CHUD_BIN="${CHUDWARE_CHUD_BIN:-}"

while [ "$#" -gt 0 ]; do
    case "$1" in
        --out)
            [ "$#" -ge 2 ] || { echo "error: --out requires a directory" >&2; exit 2; }
            OUT="$2"
            shift 2
            ;;
        --chud-bin)
            [ "$#" -ge 2 ] || { echo "error: --chud-bin requires a file" >&2; exit 2; }
            CHUD_BIN="$2"; shift 2 ;;
        --keep-app) KEEP_APP=1; shift ;;
        --prepare-signing)
            [ -z "$MODE" ] || { echo "error: choose exactly one signing mode" >&2; exit 2; }
            MODE=prepare; shift ;;
        --key-id)
            [ "$#" -ge 2 ] || { echo "error: --key-id requires an id" >&2; exit 2; }
            KEY_ID="$2"; shift 2 ;;
        --finalize-signing)
            [ -z "$MODE" ] || { echo "error: choose exactly one signing mode" >&2; exit 2; }
            [ "$#" -ge 2 ] || { echo "error: --finalize-signing requires a signature" >&2; exit 2; }
            MODE=finalize; FINALIZE_SIGNATURE="$2"; shift 2 ;;
        --release-public-key)
            [ "$#" -ge 2 ] || { echo "error: --release-public-key requires a PEM path or canonical base64 key" >&2; exit 2; }
            RELEASE_PUBLIC_KEY="$2"; shift 2 ;;
        *) echo "error: unknown argument: $1" >&2; exit 2 ;;
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
        --target "macos-$ARCH" --input "$CHUD_BIN" --output "$destination"
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
manifest, canonical, _ = build_manifest(root, version=sys.argv[2], platform="macos",
                                        arch=sys.argv[3], key_id=sys.argv[4])
write_manifest(root, canonical)
verify_manifest_payload(root, canonical)
write_signing_preimage(Path(sys.argv[5]), manifest["key_id"], canonical)
' "$root" "$VERSION" "$ARCH" "$KEY_ID" "$request"
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
    chmod 0644 "$MANIFEST_PATH" "$SIGNATURE_PATH"
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

verify_manifest_rows_unchanged() {
    local root="$1"
    release_python -c '
import json, sys
from pathlib import Path
from services.installer.payload_manifest import canonical_manifest_bytes, snapshot_payload
root, manifest_path = map(Path, sys.argv[1:])
canonical = manifest_path.read_bytes()
manifest = json.loads(canonical.decode("utf-8"))
if canonical_manifest_bytes(manifest) != canonical or manifest["platform"] != "macos":
    raise SystemExit("external macOS payload manifest is invalid")
rows = manifest["files"]
kinds = {row["path"]: row["kind"] for row in rows}
actual = [item.manifest_row() for item in snapshot_payload(
    root, [row["path"] for row in rows], kind_for_path=lambda path: kinds[path])]
if actual != rows:
    raise SystemExit("manifest-owned macOS payload bytes changed after outer signing")
' "$root" "$MANIFEST_PATH"
}

archive_signed_bundle() {
    local zip_path="$1"
    rm -f "$zip_path"
    ditto -c -k --sequesterRsrc --keepParent "$APP" "$zip_path"
}

emit_artifact_signing_request() {
    # `local a="$1" b="$a"` does NOT work: the shell expands every word before
    # the builtin assigns any of them, so $artifact is still unset (fatal under
    # `set -u`). Two statements.
    local artifact="$1"
    local request="$artifact.artifact-signing-request.bin"
    release_python -c '
import hashlib, sys
from pathlib import Path
artifact, request, key_id, version, arch = (Path(sys.argv[1]), Path(sys.argv[2]),
    sys.argv[3], sys.argv[4], sys.argv[5])
digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
request.write_bytes(b"\0".join((b"chudware-artifact-v1", key_id.encode("ascii"),
    version.encode("ascii"), b"macos", arch.encode("ascii"), digest.encode("ascii"))))
' "$artifact" "$request" "$KEY_ID" "$VERSION" "$ARCH"
    echo "==> artifact signing request: $request"
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
    # Both sidecars re-enter compiled_entry as `--module services.sim.job_worker <job_id>`
    # for background simulation jobs (services/sim/jobs.py), and the topology tool/study
    # modules back those jobs; forcing them fails the build if any is missing.
    local -a includes=("--include-module=chudware_protected_assets" "--include-module=services.$module"
                       "--include-package=chudcode_rpc"
                       "--include-module=services.sim.job_worker"
                       "--include-module=services.sim.topology"
                       "--include-module=services.mcp.topology_tools")
    # pycolmap is a bundled runtime package declared in packaging/requirements-release.txt.
    # It publishes arm64 macOS wheels only, so Intel builds ship without sparse SfM.
    if [ "$(uname -m)" = arm64 ]; then
        includes+=(--include-package=pycolmap)
    fi
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
# core/sim/mesher.cpp resolves the package relative to libchudware.dylib, so it belongs in
# Contents/Frameworks beside the engine. Only the four runtime artifacts are carried.
#
# The engine's own load commands decide whether anything ships. polymesh pins no prebuilt
# ONNX Runtime archive for macOS today, so an engine built here links none and needs no
# package — that build stages nothing and stays valid. An advisor-linked engine whose
# package is incomplete would ship a permanently dead optimizer that silently falls back on
# every study, so that case fails packaging instead.
ADVISOR_PACKAGE_DIR="polymesh-advisor"
ADVISOR_REQUIRED_FILES=(model.onnx normalization.json clamps.json ood.json)

stage_polymesh_advisor() {
    local engine="$1" srcdir="$2" destdir="$3" dep base candidate found f
    local deps=()
    while IFS= read -r dep; do
        case "$dep" in *onnxruntime*) deps+=("$dep") ;; esac
    done < <(otool -L "$engine" | sed -n '2,$p' | awk '{print $1}')
    if [ "${#deps[@]}" -eq 0 ]; then
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
    # One ONNX Runtime per name the engine records, staged under that exact name: the build
    # tree keeps libonnxruntime.dylib plus versioned symlinks, and the payload may hold no
    # symlinks at all (audit_production_payload), so the resolvable name must be a real
    # file. Rewriting the engine's reference here rather than leaving it to the dependency
    # walk below keeps it correct even when the runtime came from a build tree that walk
    # would not have bundled from; the walk then sees an already-final @loader_path entry.
    for dep in "${deps[@]}"; do
        base="${dep##*/}"
        found=""
        for candidate in "$srcdir/$base" "$srcdir/libonnxruntime.dylib"; do
            if [ -f "$candidate" ]; then found="$candidate"; break; fi
        done
        [ -n "$found" ] || {
            echo "error: $engine needs $base but no ONNX Runtime is staged beside it in $srcdir" >&2
            echo "       rebuild the engine so CMake stages the ONNX Runtime; the advisor cannot load without it" >&2
            exit 1
        }
        if [ ! -f "$destdir/$base" ]; then
            cp -L "$found" "$destdir/$base"
            chmod u+w "$destdir/$base"
        fi
        [ "$dep" = "@loader_path/$base" ] ||
            install_name_tool -change "$dep" "@loader_path/$base" "$engine"
    done
    echo "==> staged advisor package (${#ADVISOR_REQUIRED_FILES[@]} artifacts) + ${#deps[@]} ONNX Runtime into $destdir"
}

[ "$(uname -s)" = "Darwin" ] || {
    echo "error: packaging/macos/build_app.sh must run on macOS" >&2
    exit 1
}

ARCH="$(uname -m)"
case "$ARCH" in
    arm64) PLATFORM_SUFFIX="macos-arm64"; BREW_PREFIX="/opt/homebrew" ;;
    x86_64) PLATFORM_SUFFIX="macos-x86_64"; BREW_PREFIX="/usr/local" ;;
    *) echo "error: unsupported macOS architecture: $ARCH" >&2; exit 1 ;;
esac
APP="$OUT/Chudware.app"
CONTENTS="$APP/Contents"
MACOS="$CONTENTS/MacOS"
RESOURCES="$CONTENTS/Resources"
MANIFEST_PATH="$APP/payload-manifest.v1.json"
SIGNATURE_PATH="$APP/payload-manifest.v1.sig"
FRAMEWORKS="$CONTENTS/Frameworks"
SIGN_IDENTITY="${CHUDWARE_SIGN_IDENTITY:-}"
NOTARIZE_PROFILE="${CHUDWARE_NOTARIZE_PROFILE:-}"

if [ "$MODE" = prepare ]; then
    BUILD="$REPO/host/build-$PLATFORM_SUFFIX"
    PROTECTED_ASSETS_OUT="$BUILD/generated/protected-assets"
    BIN="$BUILD/bin/chudware"
    [ -x "$BIN" ] || { echo "error: $BIN not found; run ./build.sh all on this Mac first" >&2; exit 1; }
    require_production_cache "$BUILD" "host GUI"
    VERSION_HELPER="$(sed -n '/^chudware_read_version() {/,/^}/p' "$REPO/build.sh")"
    [ -n "$VERSION_HELPER" ] || { echo "error: chudware_read_version missing from build.sh" >&2; exit 1; }
    # shellcheck disable=SC2294
    _eval_version_helper() { eval "$VERSION_HELPER"; }
    _eval_version_helper
    chudware_read_version "$BUILD" || {
        echo "error: $BUILD/generated/api/chudware_version.h is missing or invalid; reconfigure the host build" >&2
        exit 1
    }
    VERSION="$CHUDWARE_VERSION"
    for tool in otool install_name_tool ditto plutil; do
        command -v "$tool" >/dev/null 2>&1 || { echo "error: required tool not found: $tool" >&2; exit 1; }
    done
    rm -rf "$APP"
    mkdir -p "$MACOS" "$RESOURCES" "$FRAMEWORKS"
else
    [ -d "$APP" ] && [ -f "$MANIFEST_PATH" ] || {
        echo "error: retained prepared Chudware.app or in-bundle manifest is missing under $OUT" >&2; exit 1;
    }
    VERSION="$(release_python -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["version"])' "$MANIFEST_PATH")"
    command -v ditto >/dev/null 2>&1 || { echo "error: required tool not found: ditto" >&2; exit 1; }
fi

if [ "$MODE" = prepare ]; then

sed "s/@CHUDWARE_VERSION@/$VERSION/g" "$REPO/packaging/macos/Info.plist" > "$CONTENTS/Info.plist"
plutil -lint "$CONTENTS/Info.plist" >/dev/null
cp "$REPO/packaging/macos/Chudware.icns" "$RESOURCES/Chudware.icns"
cp "$REPO/LICENSE-EULA.txt" "$RESOURCES/LICENSE-EULA.txt"
cp "$REPO/packaging/macos/RELEASE_NOTES.md" "$RESOURCES/RELEASE_NOTES.md"
cp "$BIN" "$MACOS/Chudware-bin"
chmod +x "$MACOS/Chudware-bin"

if [ -d "$BUILD/bin/assets" ]; then
    rsync -a --exclude='*.py' --exclude='*.pyc' --exclude='test_*' --exclude='.git*' \
        "$BUILD/bin/assets/" "$MACOS/assets/"
elif [ -d "$REPO/assets" ]; then
    rsync -a --exclude='*.py' --exclude='*.pyc' --exclude='test_*' --exclude='.git*' \
        "$REPO/assets/" "$MACOS/assets/"
fi

ENGINE=""
for candidate in \
    "$BUILD/bin/libchudware.dylib" \
    "$REPO/core/build-occt-$PLATFORM_SUFFIX/bin/libchudware.dylib" \
    "$REPO/core/build-$PLATFORM_SUFFIX/bin/libchudware.dylib" \
    "$REPO/build/$PLATFORM_SUFFIX/bin/libchudware.dylib"; do
    if [ -f "$candidate" ]; then ENGINE="$candidate"; break; fi
done
[ -n "$ENGINE" ] || {
    echo "error: no libchudware.dylib found for $ARCH; run ./build.sh all" >&2
    exit 1
}
cp "$ENGINE" "$FRAMEWORKS/libchudware.dylib"
chmod u+w "$FRAMEWORKS/libchudware.dylib"
ENGINE_DIR="$(dirname "$ENGINE")"
for sibling in libnglib.dylib libngcore.dylib lib3mf.dylib; do
    if [ -f "$ENGINE_DIR/$sibling" ]; then
        cp "$ENGINE_DIR/$sibling" "$FRAMEWORKS/$sibling"
        chmod u+w "$FRAMEWORKS/$sibling"
    fi
done

# Advisor model package + ONNX Runtime, beside the engine that resolves them. Runs before
# the dependency walk below so the staged runtime is already the copy that walk resolves.
stage_polymesh_advisor "$FRAMEWORKS/libchudware.dylib" "$ENGINE_DIR" "$FRAMEWORKS"

HOSTD=""
for candidate in \
    "$REPO/core/build-occt-$PLATFORM_SUFFIX/bin/chudware-hostd" \
    "$REPO/core/build-$PLATFORM_SUFFIX/bin/chudware-hostd" \
    "$REPO/build/$PLATFORM_SUFFIX/bin/chudware-hostd"; do
    if [ -x "$candidate" ]; then HOSTD="$candidate"; break; fi
done
[ -n "$HOSTD" ] || {
    echo "error: production macOS app requires chudware-hostd for $ARCH" >&2
    exit 1
}
HOSTD_BUILD="$(dirname "$(dirname "$HOSTD")")"
require_production_cache "$HOSTD_BUILD" "chudware-hostd"
cp "$HOSTD" "$MACOS/chudware-hostd"
chmod +x "$MACOS/chudware-hostd"

# Protected services and Chudcode are compiled sidecars. No repository Python,
# TypeScript, generated compiler source, source maps, or provisioning wheelhouse enters the app.
[ -f "$REPO/services/compiled_entry.py" ] || {
    echo "error: services/compiled_entry.py is required for release sidecars" >&2
    exit 1
}
generate_protected_assets
build_python_sidecar worker_main "$MACOS/chudware-worker"
build_python_sidecar mcp.server "$MACOS/chudware-mcp"
stage_chudcode "$MACOS/chud"


# Resolve @rpath/@loader_path entries when deciding whether a dependency belongs in the
# bundle. Homebrew's OCCT bottles normally use absolute Cellar names, but formula revisions
# can switch to @rpath without notice.
resolve_dependency() {
    local owner="$1" dep="$2" candidate occt_prefix
    case "$dep" in
        /*) candidate="$dep" ;;
        @loader_path/*) candidate="$(dirname "$owner")/${dep#@loader_path/}" ;;
        @executable_path/*) candidate="$MACOS/${dep#@executable_path/}" ;;
        @rpath/*)
            candidate="$(dirname "$owner")/${dep#@rpath/}"
            if [ ! -f "$candidate" ]; then candidate="$FRAMEWORKS/${dep#@rpath/}"; fi
            if [ ! -f "$candidate" ]; then candidate="$ENGINE_DIR/${dep#@rpath/}"; fi
            if [ ! -f "$candidate" ] && command -v brew >/dev/null 2>&1; then
                occt_prefix="$(brew --prefix opencascade 2>/dev/null || true)"
                if [ -n "$occt_prefix" ]; then candidate="$occt_prefix/lib/${dep#@rpath/}"; fi
            fi
            ;;
        *) return 1 ;;
    esac
    [ -f "$candidate" ] || return 1
    (cd "$(dirname "$candidate")" && printf '%s/%s\n' "$PWD" "$(basename "$candidate")")
}

should_bundle() {
    case "$1" in
        "$BREW_PREFIX"/*|"$REPO"/core/build-*/*|"$REPO"/build/*) return 0 ;;
        *) return 1 ;;
    esac
}

TMP_QUEUE="$(mktemp -d "${TMPDIR:-/tmp}/chudware-dylibs.XXXXXX")"
trap 'rm -rf "$TMP_QUEUE"' EXIT
QUEUE="$TMP_QUEUE/queue"
SEEN="$TMP_QUEUE/seen"
: > "$QUEUE"
: > "$SEEN"
printf '%s\n' "$MACOS/Chudware-bin" >> "$QUEUE"
for bundled_dylib in "$FRAMEWORKS"/*.dylib; do
    [ -f "$bundled_dylib" ] && printf '%s\n' "$bundled_dylib" >> "$QUEUE"
done
[ -n "$HOSTD" ] && printf '%s\n' "$MACOS/chudware-hostd" >> "$QUEUE"
# chud is already release-signed: never run install_name_tool over its load commands.
for sidecar in "$MACOS/chudware-worker" "$MACOS/chudware-mcp"; do
    printf '%s\n' "$sidecar" >> "$QUEUE"
done

index=1
while :; do
    current="$(sed -n "${index}p" "$QUEUE")"
    [ -n "$current" ] || break
    index=$((index + 1))
    grep -Fqx "$current" "$SEEN" && continue
    printf '%s\n' "$current" >> "$SEEN"
    chmod u+w "$current"

    while IFS= read -r dependency; do
        [ -n "$dependency" ] || continue
        resolved="$(resolve_dependency "$current" "$dependency" || true)"
        [ -n "$resolved" ] || continue
        should_bundle "$resolved" || continue
        base="$(basename "$resolved")"
        destination="$FRAMEWORKS/$base"
        if [ ! -f "$destination" ]; then
            cp -L "$resolved" "$destination"
            chmod u+w "$destination"
            printf '%s\n' "$destination" >> "$QUEUE"
        fi
        case "$current" in
            "$MACOS"/*) replacement="@loader_path/../Frameworks/$base" ;;
            *) replacement="@loader_path/$base" ;;
        esac
        [ "$dependency" = "$replacement" ] || install_name_tool -change "$dependency" "$replacement" "$current"
    done < <(otool -L "$current" | sed -n '2,$p' | awk '{print $1}')

    # CMake's ELF token is literal in Mach-O load commands. Translate every such LC_RPATH;
    # copying host/cmake/cef.cmake's $ORIGIN form would leave an unusable bundle.
    while IFS= read -r old_rpath; do
        [ -n "$old_rpath" ] || continue
        case "$old_rpath" in
            \$ORIGIN*) new_rpath="@loader_path${old_rpath#\$ORIGIN}" ;;
            *) continue ;;
        esac
        install_name_tool -rpath "$old_rpath" "$new_rpath" "$current"
    done < <(otool -l "$current" | awk '/cmd LC_RPATH/{want=1; next} want && $1=="path"{print $2; want=0}')

done

for dylib in "$FRAMEWORKS"/*.dylib; do
    [ -f "$dylib" ] || continue
    install_name_tool -id "@rpath/$(basename "$dylib")" "$dylib"
done

cat > "$MACOS/Chudware" <<'LAUNCHER'
#!/bin/sh
set -eu
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
EULA="$HERE/../Resources/LICENSE-EULA.txt"
STATE="$HOME/Library/Application Support/Chudware/eula-@CHUDWARE_VERSION@.accepted"
if [ ! -f "$STATE" ]; then
    while :; do
        choice=$(osascript <<'APPLESCRIPT'
button returned of (display dialog "Please review the Chudware End User License Agreement before using Chudware. By selecting Accept, you agree to its terms." with title "Chudware License Agreement" buttons {"Decline", "View License", "Accept"} default button "Accept" cancel button "Decline" with icon caution)
APPLESCRIPT
        ) || exit 1
        case "$choice" in
            "Accept") mkdir -p "$(dirname "$STATE")"; printf '%s\n' accepted > "$STATE"; break ;;
            "View License") open "$EULA" ;;
            *) exit 1 ;;
        esac
    done
fi
export CHUDWARE_LIBCHUDWARE="$HERE/../Frameworks/libchudware.dylib"
if [ -x "$HERE/chudware-hostd" ]; then export CHUDWARE_HOSTD_BIN="$HERE/chudware-hostd"; fi
if [ -x "$HERE/chud" ]; then export CHUDWARE_CHUDCODE_BIN="$HERE/chud"; fi
exec "$HERE/Chudware-bin" "$@"
LAUNCHER
sed -i '' "s/@CHUDWARE_VERSION@/$VERSION/g" "$MACOS/Chudware"
chmod +x "$MACOS/Chudware"



# Fail before signing if any non-system dependency still resolves outside the bundle.
for executable in "$MACOS/Chudware-bin" "$MACOS/chudware-hostd" \
    "$MACOS/chudware-worker" "$MACOS/chudware-mcp" "$MACOS/chud" \
    "$FRAMEWORKS"/*.dylib; do
    [ -f "$executable" ] || continue
    while IFS= read -r dependency; do
        dependency_target=""
        case "$dependency" in
            /System/*|/usr/lib/*) continue ;;
            @loader_path/*) dependency_target="$(dirname "$executable")/${dependency#@loader_path/}" ;;
            @rpath/*) dependency_target="$FRAMEWORKS/${dependency#@rpath/}" ;;
            @executable_path/*) dependency_target="$MACOS/${dependency#@executable_path/}" ;;
        esac
        if [ -z "$dependency_target" ] || [ ! -f "$dependency_target" ]; then
            echo "error: unresolved external Mach-O dependency in $executable: $dependency" >&2
            exit 1
        fi
    done < <(otool -L "$executable" | sed -n '2,$p' | awk '{print $1}')
done

if [ -n "$SIGN_IDENTITY" ]; then
    echo "==> signing nested Mach-O payloads with identity: $SIGN_IDENTITY"
    echo "==> verifying release-signed Chudcode artifact without re-signing it"
    codesign --verify --strict --verbose=2 "$MACOS/chud"
    while IFS= read -r -d '' nested; do
        [ "$nested" = "$MACOS/chud" ] && continue
        file -b "$nested" | grep -q 'Mach-O' || continue
        codesign --force --options runtime --timestamp --sign "$SIGN_IDENTITY" "$nested"
        codesign --verify --strict --verbose=2 "$nested"
    done < <(find "$MACOS" "$FRAMEWORKS" -type f -print0)
else
    echo "WARNING: preview build has no Apple platform identity; payload signing remains mandatory" >&2
fi

audit_production_payload "$APP"
REQUEST="$OUT/Chudware-$VERSION-macos-$ARCH.payload-signing-request.bin"
prepare_signing_metadata "$APP" "$REQUEST"
echo "==> prepared immutable app: $APP"
echo "==> signing request: $REQUEST"
exit 0
fi

finalize_signing_metadata "$APP"
audit_production_payload "$APP"
verify_staged_manifest "$APP"

if [ -n "$SIGN_IDENTITY" ]; then
    echo "==> signing outer bundle seal without re-signing nested payloads"
    codesign --force --options runtime --timestamp --sign "$SIGN_IDENTITY" "$APP"
    codesign --verify --deep --strict --verbose=2 "$APP"
    verify_manifest_rows_unchanged "$APP"
fi

mkdir -p "$OUT"
command -v zip >/dev/null 2>&1 || { echo "error: required tool not found: zip" >&2; exit 1; }
ZIP="$OUT/Chudware-$VERSION-macos-$ARCH.zip"
archive_signed_bundle "$ZIP"

if [ -n "$NOTARIZE_PROFILE" ]; then
    [ -n "$SIGN_IDENTITY" ] || {
        echo "error: CHUDWARE_NOTARIZE_PROFILE requires CHUDWARE_SIGN_IDENTITY" >&2
        exit 1
    }
    codesign --verify --strict --verbose=2 "$MACOS/chud"
    echo "==> notarizing with keychain profile: $NOTARIZE_PROFILE"
    xcrun notarytool submit "$ZIP" --keychain-profile "$NOTARIZE_PROFILE" --wait
    xcrun stapler staple "$APP"
    xcrun stapler validate "$APP"
    codesign --verify --deep --strict --verbose=2 "$APP"
    codesign --verify --strict --verbose=2 "$MACOS/chud"
    spctl --assess --type execute --verbose=2 "$APP"
    verify_manifest_rows_unchanged "$APP"
    archive_signed_bundle "$ZIP"
fi
emit_artifact_signing_request "$ZIP"
# Same retention the Linux builder does: the bundle about to be deleted holds
# the only copy of the signed manifest pair, and finalize_release.py's
# `complete` stage re-reads it to name it and its key id in the publish bundle.
cp "$MANIFEST_PATH" "$OUT/payload-manifest.v1.json"
cp "$SIGNATURE_PATH" "$OUT/payload-manifest.v1.sig"

if [ "$KEEP_APP" != "1" ]; then rm -rf "$APP"; fi
size="$(stat -f %z "$ZIP")"
echo "==> built: $ZIP ($size bytes)"
