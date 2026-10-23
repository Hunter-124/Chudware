#!/usr/bin/env bash
# ===========================================================================
#  Chudware build script (Linux / macOS) — thin wrapper around CMake/Ninja.
# ---------------------------------------------------------------------------
#  Builds the native CMake/Ninja projects that make up Chudware:
#    core -> the C++ OCCT kernel (libchudware.$SHLIB_EXT)
#    app  -> Chudware host GUI (cmake -S host -B host/build-$PLATFORM_SUFFIX + ninja Chudware)
#
#  Every tree name and engine filename below is derived from `uname` (see the
#  platform block under the usage text), so the same script builds
#  host/build-linux + libchudware.so on Linux and host/build-macos-arm64 +
#  libchudware.dylib on an Apple-silicon Mac. Windows is not built from here —
#  use CMakePresets.json's windows-* presets from an x64 Native Tools prompt.
#
#  host/ is the single cross-platform GUI target (builds on Linux with
#  GCC/Clang, macOS with AppleClang, and Windows with MSVC). Binary:
#  host/build-$PLATFORM_SUFFIX/bin/chudware
#
#  Usage:
#    ./build.sh            Default: build the host GUI.
#    ./build.sh app        Same as default.
#    ./build.sh core       Rebuild libchudware (plus chudware-hostd and the polymesh
#                          CLI where the tree declares them) in every configured
#                          consumed core tree (core/build-occt-$PLATFORM_SUFFIX,
#                          core/build-$PLATFORM_SUFFIX). With neither configured,
#                          configures and builds this host's core preset instead.
#    ./build.sh daemon     Build libchudware + chudware-hostd in the configured
#                          core/build-occt-$PLATFORM_SUFFIX tree (the full engine the
#                          live daemon serves). Fails if that tree is not configured.
#    ./build.sh all        core, then host GUI, then daemon.
#    ./build.sh wasm       Emscripten WASM host (full app); stages the bundle into
#                          host/build-wasm/app/ (override with CHUDWARE_WEB_APP).
#                          Requires emsdk (~/.emsdk). Linux-only (GNU sed / sha1sum).
#    ./build.sh stage      Deploy libchudware, its optional lib3mf/advisor runtime,
#                          chudware-hostd and (when built) the polymesh CLI from
#                          core/build-occt-$PLATFORM_SUFFIX/bin (falls back to the slim
#                          core/build-$PLATFORM_SUFFIX tree with a warning) into
#                          services/dist/Chudware/ — the dist dir core_bridge/daemon_bridge
#                          resolve first. Engine and daemon MUST come from the same
#                          build tree or clients compute different hostd instance keys.
#    ./build.sh clean      Wipe host/build-$PLATFORM_SUFFIX (core kept — OCCT is
#                          expensive to reconfigure), then run the default build.
#
#  Env:
#    CHUDWARE_POLYMESH_ROOT=<checkout>
#                          Build core against a live Polyhedral-FEA working tree
#                          (default: the pinned third_party fetch). Forwarded as
#                          -DPOLYMESH_SOURCE_DIR; the `polymesh` CLI is staged into
#                          core/build-occt-$PLATFORM_SUFFIX/bin beside chudware-hostd.
#
#  Close any running Chudware / MCP server first if a link step reports the output
#  is busy (both hold the engine).
# ===========================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODE="${1:-default}"

# --- platform / arch --------------------------------------------------------
# One uname read drives every tree name and filename that differs between the
# desktop OSes this script builds on. On Linux each value resolves to the exact
# string that used to be hardcoded, so Linux paths are unchanged.
#   PLATFORM_SUFFIX  build-tree suffix: linux | macos-arm64 | macos-x86_64
#   SHLIB_EXT        engine filename extension: so (ELF) | dylib (Mach-O)
#   CORE_PRESET      the CMakePresets.json configure preset for this host
#   OSX_ARCH         CMAKE_OSX_ARCHITECTURES slice (macOS only)
#   BREW_PREFIX      Homebrew prefix, which differs per slice (macOS only)
case "$(uname -s)" in
    Linux)
        PLATFORM_SUFFIX=linux
        SHLIB_EXT=so
        CORE_PRESET=linux-core
        ;;
    Darwin)
        # Apple silicon installs Homebrew under /opt/homebrew, Intel under
        # /usr/local — the same split the macos-* configure presets encode.
        case "$(uname -m)" in
            arm64)  PLATFORM_SUFFIX=macos-arm64;  OSX_ARCH=arm64;  BREW_PREFIX=/opt/homebrew ;;
            x86_64) PLATFORM_SUFFIX=macos-x86_64; OSX_ARCH=x86_64; BREW_PREFIX=/usr/local    ;;
            *)
                echo "[build] ERROR: unsupported macOS architecture '$(uname -m)'"
                exit 1
                ;;
        esac
        SHLIB_EXT=dylib
        CORE_PRESET="$PLATFORM_SUFFIX"
        ;;
    *)
        echo "[build] ERROR: unsupported platform '$(uname -s)' — this script builds Linux"
        echo "[build]        and macOS. Windows builds run 'cmake --preset windows-occt' from"
        echo "[build]        an x64 Native Tools Command Prompt (see docs/reference/BUILD_WINDOWS.md)."
        exit 1
        ;;
esac
LIBCHUDWARE="libchudware.$SHLIB_EXT"
HOST_BUILD="$ROOT/host/build-$PLATFORM_SUFFIX"
CORE_OCCT_BUILD="$ROOT/core/build-occt-$PLATFORM_SUFFIX"
CORE_SLIM_BUILD="$ROOT/core/build-$PLATFORM_SUFFIX"

command -v cmake >/dev/null 2>&1 || { echo "[build] ERROR: cmake not found on PATH"; exit 1; }
command -v ninja >/dev/null 2>&1 || { echo "[build] ERROR: ninja not found on PATH"; exit 1; }


# --- Product version (see core/cmake/ChudwareVersion.cmake) ------------------
# Read the semver back out of the header CMake generated at CONFIGURE time
# (<tree>/generated/api/chudware_version.h) instead of re-deriving it here. A
# second `git describe` parser written in shell would drift from the CMake one
# the first time either grew a rule, and a package would then be labelled with a
# version the binaries inside it do not report. Reading the header cannot drift:
# it is the exact string compiled into libchudware, the GUI and the daemon.
#
#   chudware_read_version <build-tree>
#       Sets CHUDWARE_VERSION / _GIT_SHA / _BUILD_DIRTY / _BUILD_DATE (exported,
#       so the packaging step and anything it spawns inherit them) and returns 0.
#       Returns 1 without setting anything when the tree has no generated header
#       — a tree configured before the header existed. Callers report and carry
#       on: staging a build is still correct, it just cannot be labelled.
chudware_read_version() {
    local hdr="$1/generated/api/chudware_version.h"
    [ -f "$hdr" ] || return 1
    local ver sha dirty date
    # POSIX BRE only: BSD sed (macOS /usr/bin/sed) does not understand `\+`, so
    # the GNU form matched nothing there and every caller concluded the header
    # was "missing or invalid" -- which stopped packaging/macos/build_app.sh dead
    # on the release runner after a fully successful build and test run.
    ver="$(sed -n 's/^#define CHUDWARE_VERSION[[:space:]][[:space:]]*"\(.*\)"$/\1/p' "$hdr")"
    sha="$(sed -n 's/^#define CHUDWARE_GIT_SHA[[:space:]][[:space:]]*"\(.*\)"$/\1/p' "$hdr")"
    dirty="$(sed -n 's/^#define CHUDWARE_BUILD_DIRTY[[:space:]][[:space:]]*\([0-9][0-9]*\)$/\1/p' "$hdr")"
    date="$(sed -n 's/^#define CHUDWARE_BUILD_DATE[[:space:]][[:space:]]*"\(.*\)"$/\1/p' "$hdr")"
    [ -n "$ver" ] && [ -n "$sha" ] || return 1
    export CHUDWARE_VERSION="$ver" CHUDWARE_GIT_SHA="$sha"
    export CHUDWARE_BUILD_DIRTY="${dirty:-1}" CHUDWARE_BUILD_DATE="$date"
    return 0
}

# `--preset $CORE_PRESET` writes to build/$CORE_PRESET/, which is not where consumers
# load the engine. Rebuild the consumed trees directly and report any stale binary rather
# than printing success for an artifact nothing uses.
# Uses `find -newer -print -quit` per consumer rather than sorting every source: piping find into
# `xargs ls -t | head -1` makes xargs die of SIGPIPE, and under this script's `set -o pipefail`
# that aborted the whole build with exit 125 (caught 2026-07-29 — `./build.sh all` silently stopped
# after the core stage). No pipes here, and the whole thing is guarded so a diagnostic can never
# fail a build.
# `strict` (any non-empty first arg) turns the diagnostic into a GATE: used after build_core has
# actually tried to rebuild, where a still-stale consumer is a real failure rather than a notice.
report_engine_staleness() {
    local strict="${1:-}" consumer hit stale=0
    for consumer in "$CORE_OCCT_BUILD/bin/$LIBCHUDWARE" \
                    "$CORE_SLIM_BUILD/bin/$LIBCHUDWARE" \
                    "$HOST_BUILD/bin/$LIBCHUDWARE"; do
        [ -f "$consumer" ] || continue
        # Prune build trees. They contain GENERATED headers — notably
        # core/build-linux/hostd_gen/version_info.h, refreshed by the "git describe" build-id step
        # on EVERY build — which are newer than any sibling tree's .so by construction. Scanning
        # them made this report stale immediately after a successful build (found 2026-07-29 by
        # actually running ./build.sh core: it flagged core/build-occt-linux against
        # build-linux/hostd_gen/version_info.h). Only AUTHORED sources may mark an engine stale.
        # Also prune core/hostd: those sources build the chudware-hostd DAEMON and are
        # NOT linked into libchudware.so, so they can never make the engine stale. Left
        # in, a daemon-only edit marked every engine permanently stale and the strict
        # gate then failed a build that was in fact correct — a gate that cries wolf
        # is one people learn to ignore. The daemon gets its own check below.
        # core/tests is pruned for the SAME reason: every file there compiles into its own
        # doctest executable (core/CMakeLists.txt `add_executable(test_* tests/test_*.cpp)`)
        # and nothing outside core/tests includes a test header, so a test edit cannot
        # possibly change libchudware.so. Left in, `./build.sh core` after adding one
        # TEST_CASE printed "ERROR: a consumed engine is STILL stale after building" and
        # refused to report success on a build that was completely correct — the strict
        # gate failing over a file it had already proved irrelevant (caught 2026-08-27).
        hit="$(find "$ROOT/core" -type d \( -name 'build*' -o -name hostd -o -name tests \) \
                -prune -o \
                \( -name '*.cpp' -o -name '*.h' \) -newer "$consumer" -print -quit \
                2>/dev/null || true)"
        if [ -n "$hit" ]; then
            stale=1
            echo "[build] WARN: STALE ENGINE ${consumer#"$ROOT"/} is older than ${hit#"$ROOT"/}."
            echo "[build]       Nothing that reads it (gate.sh, the GUI, CHUDWARE_LIBCHUDWARE) will"
            echo "[build]       see your core changes. Rebuild that tree, e.g."
            echo "[build]       ninja -C ${CORE_SLIM_BUILD#"$ROOT"/} bin/$LIBCHUDWARE  # or ./build.sh all"
        fi
    done
    # The daemon is a SEPARATE executable that dlopens the staged libchudware; it does not
    # link the engine's simulation/modeling objects (see the chudware-hostd target below).
    # Only daemon sources and its CppCore loader can make the executable stale. Comparing
    # every engine source here made a successful lib-only rebuild permanently fail this
    # gate even though the fresh library is the artifact that contains those changes.
    for consumer in "$CORE_OCCT_BUILD/bin/chudware-hostd" \
                    "$CORE_SLIM_BUILD/bin/chudware-hostd" \
                    "$ROOT/services/dist/Chudware/chudware-hostd"; do
        [ -f "$consumer" ] || continue
        hit="$(find "$ROOT/core/hostd" -type f \
                \( -name '*.cpp' -o -name '*.h' \) -newer "$consumer" -print -quit \
                2>/dev/null || true)"
        if [ -z "$hit" ]; then
            for input in "$ROOT/services/worker/cpp_core.cpp" \
                         "$ROOT/services/worker/cpp_core.h"; do
                if [ -f "$input" ] && [ "$input" -nt "$consumer" ]; then
                    hit="$input"
                    break
                fi
            done
        fi
        if [ -n "$hit" ]; then
            stale=1
            echo "[build] WARN: STALE DAEMON ${consumer#"$ROOT"/} is older than ${hit#"$ROOT"/}."
            echo "[build]       The GUI's document registry lives in the daemon, so doc/tab fixes"
            echo "[build]       will not reach the app. Rebuild + restage:"
            echo "[build]       ./build.sh core && ./build.sh stage"
        fi
    done
    if [ -n "$strict" ] && [ "$stale" -eq 1 ]; then
        echo "[build] ERROR: a consumed engine is STILL stale after building. Refusing to report"
        echo "[build]        success — this is the exact failure that scored six live agent sweeps"
        echo "[build]        against two-day-old code on 2026-07-29."
        return 1
    fi
    return 0
}
# --- Polyhedral-FEA source selection ---------------------------------------
# CHUDWARE_POLYMESH_ROOT=<checkout> builds core against a LIVE Polyhedral-FEA working tree
# (the repository root, e.g. ~/src/Polyhedral-FEA) instead of the pinned
# third_party fetch. It is forwarded as -DPOLYMESH_SOURCE_DIR, and because the variable is
# cached, an already-configured tree must be re-configured for the switch to take effect —
# and re-configured again (with the flag absent, which clears it) to go back to the pin.
# ProvisionPolymesh.cmake prints which source won on every configure.
POLYMESH_SOURCE_ARG=""
if [ -n "${CHUDWARE_POLYMESH_ROOT:-}" ]; then
    [ -f "$CHUDWARE_POLYMESH_ROOT/CMakeLists.txt" ] || {
        echo "[build] ERROR: CHUDWARE_POLYMESH_ROOT=$CHUDWARE_POLYMESH_ROOT has no CMakeLists.txt"
        exit 1
    }
    POLYMESH_SOURCE_ARG="-DPOLYMESH_SOURCE_DIR=$CHUDWARE_POLYMESH_ROOT"
    echo "[build] Polyhedral-FEA: LOCAL SOURCE $CHUDWARE_POLYMESH_ROOT"
fi

# Re-configure a consumed core tree in place so a POLYMESH_SOURCE_DIR change (in either
# direction) reaches its cache. No-op-cheap when nothing changed; skipped entirely when the
# override is unset AND the tree was never pointed at a local source.
reconfigure_core_tree() {
    local tree="$1"
    if [ -z "$POLYMESH_SOURCE_ARG" ] &&
       ! grep -q '^POLYMESH_SOURCE_DIR:PATH=.' "$tree/CMakeCache.txt" 2>/dev/null; then
        return 0
    fi
    # An empty value CLEARS the cache entry, which is exactly how a tree returns to the pin.
    # The local-source configure FORCE-cached POLYMESH_ROOT to that checkout, so returning to
    # the pin must also restore the managed root or configure looks for <checkout>/src.
    if [ -n "$POLYMESH_SOURCE_ARG" ]; then
        cmake -S "$ROOT/core" -B "$tree" "$POLYMESH_SOURCE_ARG"
    else
        cmake -S "$ROOT/core" -B "$tree" -DPOLYMESH_SOURCE_DIR= \
              -DPOLYMESH_ROOT="$ROOT/third_party/polymesh"
    fi
}

# `./build.sh core` means every consumed native engine is current. Rebuild each configured
# consumed tree in place so its platform-specific dependency paths remain intact.
build_core() {
    local rc=0 built=0 tree tlist
    for tree in "$CORE_OCCT_BUILD" "$CORE_SLIM_BUILD"; do
        [ -f "$tree/build.ninja" ] || continue
        reconfigure_core_tree "$tree" || rc=$?
        echo "[build] core engine -> ${tree#"$ROOT"/}/bin/$LIBCHUDWARE (consumed tree)"
        ninja -C "$tree" "bin/$LIBCHUDWARE" || rc=$?
        # ...and the DAEMON from the same tree. chudware-hostd is a separate binary
        # carrying core/hostd/*, so a core-only build left it untouched: on 2026-07-30
        # a user hit duplicate "AI: sports_bike"/"sports_bike2" tabs against a hostd
        # from four days earlier, because the doc-registry fix that stopped the
        # duplicate mint lives in the daemon and `./build.sh core` never rebuilt it.
        # `stage` then copied that stale daemon next to a fresh engine -- exactly the
        # split this function's own comment says core must not reintroduce.
        # NB: no `| grep -q` here. Under `set -o pipefail` grep exits at the first
        # match, ninja dies on SIGPIPE, and the pipeline status makes this condition
        # FALSE — which silently skipped the daemon build the first time round.
        tlist="$(ninja -C "$tree" -t targets all 2>/dev/null || true)"
        case "$tlist" in
            *"bin/chudware-hostd:"*)
                echo "[build] core daemon -> ${tree#"$ROOT"/}/bin/chudware-hostd"
                ninja -C "$tree" bin/chudware-hostd || rc=$?
                ;;
        esac
        # ...and the Polyhedral-FEA CLI (check|mesh|solve|diag|render), staged into the
        # same bin/ dir. Only trees configured with OCCT + CHUDWARE_WITH_POLYMESH declare
        # it, so the target probe above is the gate — a slim tree simply has no rule.
        case "$tlist" in
            *"bin/polymesh:"*)
                echo "[build] polymesh CLI -> ${tree#"$ROOT"/}/bin/polymesh"
                ninja -C "$tree" bin/polymesh || rc=$?
                ;;
        esac
        built=1
    done
    if [ "$built" -eq 0 ]; then
        echo "[build] no configured core tree found — configuring the $CORE_PRESET preset."
        echo "[build] NOTE: build/$CORE_PRESET is NOT read by gate.sh, the GUI, or"
        echo "[build]       CHUDWARE_LIBCHUDWARE. For a tree they load, configure"
        echo "[build]       ${CORE_OCCT_BUILD#"$ROOT"/} once: cmake -S core -B ${CORE_OCCT_BUILD#"$ROOT"/} -DCHUDWARE_WITH_OCCT=ON"
        cmake --preset "$CORE_PRESET" -DPOLYMESH_SOURCE_DIR="${CHUDWARE_POLYMESH_ROOT:-}" \
            && cmake --build --preset "$CORE_PRESET" || rc=$?
        report_engine_staleness
        return $rc
    fi
    # Keep the GUI's dlopen copy in step: without this, a core fix reaches the tests but never
    # the desktop app the user is looking at.
    stage_libchudware_next_to_host
    report_engine_staleness strict || rc=1
    return $rc
}
build_app()  {
    # macOS: mirror the macos-* configure presets — pin the slice and point CMake at
    # Homebrew, which is not on its default search path. host/ has no preset of its
    # own (it is a separate CMake project), so the flags are passed directly.
    # `${extra[@]+...}` is the empty-array expansion that survives `set -u` under the
    # bash 3.2 that ships with macOS.
    local -a extra=()
    [ "$SHLIB_EXT" = dylib ] && extra=(-DCMAKE_OSX_ARCHITECTURES="$OSX_ARCH"
                                       -DCMAKE_PREFIX_PATH="$BREW_PREFIX")
    cmake -S "$ROOT/host" -B "$HOST_BUILD" -G Ninja -DCMAKE_BUILD_TYPE=Release \
        ${extra[@]+"${extra[@]}"}
    ninja -C "$HOST_BUILD" Chudware
    # Host dlopens libchudware next to the executable (cpp_core resolve tier 3). Keep it
    # in sync with the OCCT engine so core fixes (e.g. extrude flip) actually reach the GUI.
    stage_libchudware_next_to_host
}
# Emscripten web client (full app). Sources emsdk, configures host/ with
# emcmake (CEF off), builds Chudware, stages html/js/wasm/data into
# $CHUDWARE_WEB_APP (default host/build-wasm/app/), which the web gateway serves
# at /app (see web/README.md); host/wasm/coop_server.py serves it standalone.
build_wasm() {
    local emsdk_dir="${EMSDK:-$HOME/.emsdk}"
    if [ ! -f "$emsdk_dir/emsdk_env.sh" ]; then
        echo "[build] ERROR: emsdk not found at $emsdk_dir (clone+install https://github.com/emscripten-core/emsdk)"
        return 1
    fi
    # shellcheck source=/dev/null
    source "$emsdk_dir/emsdk_env.sh"
    command -v emcc >/dev/null 2>&1 || { echo "[build] ERROR: emcc not on PATH after sourcing emsdk"; return 1; }
    command -v emcmake >/dev/null 2>&1 || { echo "[build] ERROR: emcmake not on PATH"; return 1; }
    local build_type="${CHUDWARE_WASM_BUILD_TYPE:-MinSizeRel}"
    echo "[build] emcc: $(emcc -v 2>&1 | head -1)"
    echo "[build] wasm build type: $build_type"
    emcmake cmake -S "$ROOT/host" -B "$ROOT/host/build-wasm" -G Ninja \
        -DCMAKE_BUILD_TYPE="$build_type" \
        -DCHUDWARE_ENABLE_CEF=OFF
    cmake --build "$ROOT/host/build-wasm" --target Chudware
    local out="$ROOT/host/build-wasm/bin"
    local stage="${CHUDWARE_WEB_APP:-$ROOT/host/build-wasm/app}"
    mkdir -p "$stage"
    # Stage the emscripten bundle (html/js/wasm + any .data preload packs).
    # emcc embeds pthread workers in Chudware.js (no separate Chudware.worker.js); the app
    # runs main-thread ImGui + ASYNCIFY, not PROXY_TO_PTHREAD. Still stage any *worker*.js
    # if a future toolchain emits one.
    local staged_names=""
    for f in "$out"/Chudware.html "$out"/Chudware.js "$out"/Chudware.wasm \
             "$out"/Chudware.data "$out"/Chudware.worker.js "$out"/*worker*.js; do
        [ -e "$f" ] || continue
        local base
        base="$(basename "$f")"
        case " $staged_names " in *" $base "*) continue ;; esac
        cp -f "$f" "$stage/"
        staged_names="$staged_names $base"
        echo "[build] staged $base -> $stage/"
    done
    # coi-serviceworker: cross-origin isolation insurance (registered from shell.html).
    cp -f "$ROOT/host/wasm/coi-serviceworker.js" "$stage/" 2>/dev/null \
        && echo "[build] staged coi-serviceworker.js -> $stage/"
    # Prefer the canonical shell name index.html for the static server root.
    if [ -f "$stage/Chudware.html" ]; then
        cp -f "$stage/Chudware.html" "$stage/index.html"
        echo "[build] staged index.html (copy of Chudware.html)"
    fi
    # Cache-bust: version the bundle URLs so browsers never pair a fresh Chudware.js with a
    # stale Chudware.wasm/.data (fatal LinkError). VER = short wasm content hash. index.html is
    # served no-cache, so every client picks up the new versioned URLs immediately.
    if [ -f "$stage/Chudware.wasm" ]; then
        VER="$(sha1sum "$stage/Chudware.wasm" | cut -c1-12)"
        for hf in "$stage/index.html" "$stage/Chudware.html"; do
            [ -f "$hf" ] || continue
            sed -i \
                -e "s/__CHUDWARE_VER__/$VER/g" \
                -e "s#src=Chudware\.js#src=\"Chudware.js?v=$VER\"#g" \
                -e "s#src=coi-serviceworker\.js#src=\"coi-serviceworker.js?v=$VER\"#g" \
                -e "s#src=\"Chudware\.js\"#src=\"Chudware.js?v=$VER\"#g" \
                -e "s#src=\"coi-serviceworker\.js\"#src=\"coi-serviceworker.js?v=$VER\"#g" \
                "$hf"
        done
        echo "[build] cache-busted bundle URLs (v=$VER)"
    fi
    # Precompress the bundle so the gateway (appserve.py) can serve .br/.gz variants:
    # wasm 9.4MB -> ~2.1MB brotli over the wire = far faster first load on real networks.
    # Generated AFTER the cache-bust sed so index.html variants carry the versioned URLs;
    # freshly written (mtime > original) so appserve's staleness guard always accepts them.
    if command -v brotli >/dev/null 2>&1 || command -v gzip >/dev/null 2>&1; then
        for cf in "$stage"/Chudware.wasm "$stage"/Chudware.js "$stage"/Chudware.data \
                  "$stage"/index.html "$stage"/Chudware.html "$stage"/coi-serviceworker.js; do
            [ -f "$cf" ] || continue
            command -v brotli >/dev/null 2>&1 && brotli -f -q 11 -o "$cf.br" "$cf"
            command -v gzip  >/dev/null 2>&1 && gzip  -f -k -9 "$cf"
        done
        echo "[build] precompressed bundle (.br/.gz) for gateway content-encoding"
    fi
    # Parity gate. The web bundle bakes assets/ in at LINK time (the --preload-file flags
    # in host/CMakeLists.txt), so a bundle is a derived artifact that goes stale silently
    # the moment any preloaded asset changes: the desktop build reads assets/ off disk at
    # startup and never notices. This compares the staged bundle against its sources and
    # FAILS the build rather than leaving a stale bundle behind.
    local parity_rc=0
    python3 "$ROOT/scripts/check_web_parity.py" --bundle "$stage" || parity_rc=$?
    if [ "$parity_rc" -ne 0 ]; then
        echo "[build] ERROR: web parity gate rejected the staged bundle (exit $parity_rc; 1=stale asset, 2=gate could not evaluate)"
        echo "[build]        $stage is NOT deployable — fix the divergence reported above, then re-run ./build.sh wasm"
        return 1
    fi
    echo "[build] WASM bundle ready: $stage  (serve with: python3 host/wasm/coop_server.py 8765 $stage)"
}


# Full-engine OCCT tree (the engine + chudware-hostd) served by the live daemon.
# The tree is configured once; this function performs the incremental build.
build_daemon() {
    if [ ! -f "$CORE_OCCT_BUILD/CMakeCache.txt" ]; then
        echo "[build] ERROR: ${CORE_OCCT_BUILD#"$ROOT"/} is not configured — configure it once:"
        echo "[build]        cmake -S core -B ${CORE_OCCT_BUILD#"$ROOT"/} -DCHUDWARE_WITH_OCCT=ON"
        echo "[build]        (daemon/engine layout: docs/reference/HOSTD.md)"
        return 1
    fi
    cmake --build "$CORE_OCCT_BUILD" --target chudware chudware-hostd
    stage_libchudware_next_to_host
}

# Advisor closure of an already-located engine: the ONNX Runtime that engine links plus the
# exact four artifacts pm::advisor::Advisor opens.
#
# core/sim/mesher.cpp resolves the package relative to the engine module. Copying the
# engine without its package would leave advice unavailable while the binary appears current.
# Only the four runtime files are copied; the research campaign corpus stays out of packages.
#
# An engine configured without the advisor links no ONNX Runtime, so this is a no-op for it
# and slim builds keep staging exactly as before. An engine that DOES link it but has no
# complete package beside it is a hard failure: better a refused stage than a dead optimizer.
ADVISOR_PACKAGE_DIR="polymesh-advisor"
ADVISOR_REQUIRED_FILES="model.onnx normalization.json clamps.json ood.json"

# The platform tool that can read a native module's recorded shared-library
# dependencies, and how to get it when it is absent.
engine_inspect_tool() {
    case "$PLATFORM_SUFFIX" in
        macos*) echo otool ;;
        *)      echo readelf ;;
    esac
}
engine_inspect_tool_hint() {
    case "$PLATFORM_SUFFIX" in
        macos*) echo "install the Xcode command line tools: xcode-select --install" ;;
        *)      echo "install binutils (Fedora: dnf install binutils, Debian: apt install binutils)" ;;
    esac
}

# Every shared library the engine records, one raw dependency token per line
# (DT_NEEDED SONAMEs on ELF, load-command paths on Mach-O). A linked native
# module ALWAYS records at least libc / libSystem, so an empty list means the
# tool did not read a real module — not that the module needs nothing.
engine_recorded_deps() {
    case "$PLATFORM_SUFFIX" in
        macos*)
            # otool -L: line 1 is the file name, every later line is a load command
            # ("\t/path/to/lib.dylib (compatibility version ..., current version ...)").
            otool -L "$1" | awk 'NR>1 && /\(compatibility version /{print $1}'
            ;;
        *)
            readelf -d "$1" | sed -n 's/.*(NEEDED).*\[\([^]]*\)\].*/\1/p'
            ;;
    esac
}

# Runtime names the engine itself records (SONAME on ELF, load-command basename on
# Mach-O), one per line. Empty output means "this engine has no advisor" — and it
# means ONLY that, because this decides whether the advisor payload ships and so it
# FAILS CLOSED. A missing readelf/otool, an unreadable path, a non-native file, or a
# tool error all return non-zero instead of the empty output they used to produce,
# which was indistinguishable from an advisor-off build: the stage then shipped an
# advisor-LINKED engine with no ONNX Runtime and no model package beside it, i.e. a
# permanently dead optimizer that silently falls back to the heuristic mesher choice
# on every study while the staged engine looks perfectly current.
engine_onnxruntime_names() {
    local engine="$1" tool deps
    tool="$(engine_inspect_tool)"
    if [ ! -f "$engine" ] || [ ! -r "$engine" ]; then
        echo "[stage] ERROR: $engine is not a readable regular file, so its ONNX Runtime" >&2
        echo "[stage]        linkage cannot be read. Refusing to guess whether the mesh" >&2
        echo "[stage]        advisor ships. Build the engine first: ./build.sh core" >&2
        return 1
    fi
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "[stage] ERROR: $tool is required to read what the engine links and decide" >&2
        echo "[stage]        whether the mesh advisor payload ships with it. Without it a" >&2
        echo "[stage]        staged advisor-enabled engine would be left with no runtime and" >&2
        echo "[stage]        no model package — a dead optimizer. Fix and re-run this stage:" >&2
        echo "[stage]        $(engine_inspect_tool_hint)" >&2
        return 1
    fi
    if ! deps="$(engine_recorded_deps "$engine")"; then
        echo "[stage] ERROR: $tool could not read $engine (its own error is above)." >&2
        echo "[stage]        A stage cannot tell an advisor-off engine from an advisor-on one" >&2
        echo "[stage]        it failed to inspect, so this is a hard failure. Rebuild the" >&2
        echo "[stage]        engine: ./build.sh core" >&2
        return 1
    fi
    if [ -z "$deps" ]; then
        echo "[stage] ERROR: $tool read $engine but found no recorded shared-library" >&2
        echo "[stage]        dependencies at all. Every linked native module records at" >&2
        echo "[stage]        least the C runtime, so this file is not a usable engine and" >&2
        echo "[stage]        its advisor linkage is unknown. Rebuild it: ./build.sh core" >&2
        return 1
    fi
    # Inspection succeeded on a real module: no onnxruntime among its dependencies is
    # the genuine advisor-off answer, and prints nothing.
    printf '%s\n' "$deps" | awk -F/ '/onnxruntime/{print $NF}' | sort -u
}

stage_advisor_beside_engine() {
    local engine="$1" dst_dir="$2" src_dir names f name candidate found tmp
    src_dir="$(dirname "$engine")"
    # Inspection failure is a stage failure: the `|| true` this replaces turned a
    # missing readelf/otool or an unreadable engine into "no advisor to stage".
    if ! names="$(engine_onnxruntime_names "$engine")"; then
        echo "[stage]        (refusing to stage $engine with its advisor state unknown)" >&2
        return 1
    fi
    # Inspected clean and links no ONNX Runtime: a real advisor-off engine, nothing to add.
    [ -n "$names" ] || return 0
    for f in $ADVISOR_REQUIRED_FILES; do
        if [ ! -f "$src_dir/$ADVISOR_PACKAGE_DIR/$f" ]; then
            echo "[stage] ERROR: $engine links the ONNX Runtime but its advisor package is"
            echo "[stage]        incomplete: $src_dir/$ADVISOR_PACKAGE_DIR/$f is missing."
            echo "[stage]        Refusing to stage an engine whose mesh advisor can never load"
            echo "[stage]        (it would silently fall back on every study). Rebuild the"
            echo "[stage]        engine so CMake stages the package: ./build.sh core"
            return 1
        fi
    done
    # One ONNX Runtime per name the engine records, under that exact name: the build tree
    # keeps libonnxruntime.$SHLIB_EXT plus versioned links while the engine records the
    # SONAME, and the copy has to be the name the loader will actually look for.
    for name in $names; do
        found=""
        for candidate in "$src_dir/$name" "$src_dir/libonnxruntime.$SHLIB_EXT"; do
            if [ -f "$candidate" ]; then
                found="$candidate"
                break
            fi
        done
        if [ -z "$found" ]; then
            echo "[stage] ERROR: $engine needs $name but no ONNX Runtime sits beside it in"
            echo "[stage]        $src_dir. Rebuild the engine: ./build.sh core"
            return 1
        fi
        tmp="$dst_dir/.$name.staging.$$"
        cp -L "$found" "$tmp"
        mv -f "$tmp" "$dst_dir/$name"   # atomic rename: safe under a live engine
        echo "[stage] $name -> $dst_dir/"
    done
    mkdir -p "$dst_dir/$ADVISOR_PACKAGE_DIR"
    for f in $ADVISOR_REQUIRED_FILES; do
        tmp="$dst_dir/$ADVISOR_PACKAGE_DIR/.$f.staging.$$"
        cp -L "$src_dir/$ADVISOR_PACKAGE_DIR/$f" "$tmp"
        mv -f "$tmp" "$dst_dir/$ADVISOR_PACKAGE_DIR/$f"
    done
    echo "[stage] $ADVISOR_PACKAGE_DIR/ (4 advisor artifacts) -> $dst_dir/"
}

# Copy the full-engine engine library next to $HOST_BUILD/bin/chudware so the
# running GUI's in-process cpp_core (resolve tier 3: next to executable) picks up
# the latest engine. Without this, ./build.sh core/daemon updates trees the host
# never loads. Atomic tmp+mv — never cp over a live mmap'd image (SIGBUS risk).
stage_libchudware_next_to_host() {
    local src=""
    if [ -f "$CORE_OCCT_BUILD/bin/$LIBCHUDWARE" ]; then
        src="$CORE_OCCT_BUILD/bin/$LIBCHUDWARE"
    elif [ -f "$CORE_SLIM_BUILD/bin/$LIBCHUDWARE" ]; then
        src="$CORE_SLIM_BUILD/bin/$LIBCHUDWARE"
    else
        return 0
    fi
    local dst_dir="$HOST_BUILD/bin"
    if [ -d "$dst_dir" ]; then
        local tmp="$dst_dir/.$LIBCHUDWARE.staging.$$"
        cp "$src" "$tmp"
        mv -f "$tmp" "$dst_dir/$LIBCHUDWARE"
        echo "[stage] $LIBCHUDWARE -> $dst_dir/ (host in-process engine)"
        # The GUI loads this copy in-process, so its advisor package has to be here too —
        # the engine resolves the package next to ITSELF, not next to the build tree it
        # came from.
        stage_advisor_beside_engine "$src" "$dst_dir" || return 1
    fi
}

# Stage the engine + hostd daemon into services/dist/Chudware (the $CHUDWARE_DIST
# dir that core_bridge's _lib_candidates and daemon_bridge's resolve_hostd_bin
# try FIRST). Every file is deployed copy-to-temp + atomic `mv` in the target
# dir — NEVER `cp` over a live in-use image (SIGBUS in the mmap'ed
# process; see docs/reference/HOSTD.md "Installer integration" → Upgrades). The
# running daemon notices the mtime flip and self-drains; MCP engines reload on respawn.
stage_dist() {
    local src=""
    if [ -f "$CORE_OCCT_BUILD/bin/$LIBCHUDWARE" ]; then
        src="$CORE_OCCT_BUILD/bin"
    elif [ -f "$CORE_SLIM_BUILD/bin/$LIBCHUDWARE" ]; then
        src="$CORE_SLIM_BUILD/bin"
        echo "[stage] WARNING: staging the SLIM ${CORE_SLIM_BUILD#"$ROOT"/} engine (no OCCT —"
        echo "[stage]          FEA/CFD RPCs unavailable). Build ${CORE_OCCT_BUILD#"$ROOT"/} for the full engine."
    else
        echo "[stage] ERROR: no built $LIBCHUDWARE (run ./build.sh core first)"
        return 1
    fi
    local dst="$ROOT/services/dist/Chudware"
    mkdir -p "$dst"
    # Label the staged set with the version the binaries were compiled with, and
    # export it for the packaging step that consumes this dir. `dirname $src`
    # is the build tree; the header sits at <tree>/generated/api/.
    if chudware_read_version "$(dirname "$src")"; then
        echo "[stage] chudware $CHUDWARE_VERSION ($CHUDWARE_GIT_SHA, $CHUDWARE_BUILD_DATE)"
        if [ "$CHUDWARE_BUILD_DIRTY" = "1" ]; then
            echo "[stage] WARNING: built from a dirty or untagged tree — not a release build."
        fi
    else
        echo "[stage] WARNING: $(dirname "$src")/generated/api/chudware_version.h missing —"
        echo "[stage]          this tree predates versioning; reconfigure to label the build."
    fi
    # The engine first, then its RUNPATH ($ORIGIN / @loader_path) sibling deps, then
    # the daemon that dlopens it. chudware-hostd MUST be staged from the SAME build
    # tree as the engine: clients key the daemon instance on the hostd binary's
    # realpath, so a dist engine beside a build-dir hostd silently splits clients
    # across two engines.
    #
    # The engine and the daemon are REQUIRED, and both are checked BEFORE anything
    # is copied. Staging without one of them used to print a WARNING and return 0 —
    # the worst outcome available: dist/ ends up half-updated (a fresh engine next
    # to a four-day-old daemon) and the caller, a release script or a CI job, reads
    # success. Missing either is a hard failure with nothing written. Optional lib3mf
    # remains best-effort because a slim engine legitimately has none.
    local staged=0 f tmp
    for f in "$LIBCHUDWARE" chudware-hostd; do
        if [ ! -e "$src/$f" ]; then
            echo "[stage] ERROR: $f not found in $src — refusing to stage a partial set"
            echo "[stage]        (a mismatched engine/daemon pair splits clients across two"
            echo "[stage]        engines). Build it first: ./build.sh core"
            return 1
        fi
    done
    # polymesh rides along when present: the MCP polymesh_cli lane resolves it beside
    # whichever chudware-hostd is served, and dist/ is first in that search order.
    for f in "$LIBCHUDWARE" "lib3mf.$SHLIB_EXT" chudware-hostd polymesh; do
        [ -e "$src/$f" ] || continue
        tmp="$dst/.$f.staging.$$"
        cp "$src/$f" "$tmp"
        mv -f "$tmp" "$dst/$f"      # atomic rename: safe under a live engine
        staged=$((staged + 1))
        echo "[stage] $f -> $dst/"
    done
    echo "[stage] staged $staged file(s) from $src"
    stage_advisor_beside_engine "$src/$LIBCHUDWARE" "$dst" || return 1
    stage_libchudware_next_to_host
}

case "$MODE" in
  clean)
    echo "[build] wiping ${HOST_BUILD#"$ROOT"/} (core kept) ..."
    rm -rf "$HOST_BUILD"
    MODE=default
    ;;
esac


case "$MODE" in
  core)         build_core ;;
  app|default)  build_app ;;
  daemon)       build_daemon ;;
  wasm)
    # Linux-only: build_wasm's staging step uses sha1sum and GNU `sed -i`, neither of
    # which exists on macOS, and a Mac WASM build is not a goal.
    [ "$PLATFORM_SUFFIX" = linux ] || { echo "[build] ERROR: wasm builds are Linux-only"; exit 1; }
    build_wasm; echo ""; echo "[build] DONE (wasm)."; exit 0 ;;
  all)          build_core; build_app; build_daemon ;;
  stage)        stage_dist; echo ""; echo "[build] DONE (stage)."; exit 0 ;;
  *)
    echo "[build] ERROR: unknown mode '$MODE' (use: default|app|core|daemon|wasm|all|stage|clean)"
    exit 1
    ;;
esac

echo ""
echo "[build] DONE ($MODE)."
BIN="$HOST_BUILD/bin/chudware"
[ -x "$BIN" ] && echo "[build] Run the GUI:  $BIN  (set CHUDWARE_LIBCHUDWARE to the built $LIBCHUDWARE)"
exit 0
