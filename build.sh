#!/usr/bin/env bash
# ===========================================================================
#  Chudware build script (Linux / macOS) — thin wrapper around CMake/Ninja.
# ---------------------------------------------------------------------------
#  Builds the native CMake/Ninja projects that make up Chudware (formerly Billet / AI-CAD):
#    core -> the C++ OCCT kernel (libchudware.$SHLIB_EXT)
#    app  -> Chudware host GUI (cmake -S host -B host/build-$PLATFORM_SUFFIX + ninja Chudware)
#
#  Every tree name and engine filename below is derived from `uname` (see the
#  platform block under the usage text), so the same script builds
#  host/build-linux + libchudware.so on Linux and host/build-macos-arm64 +
#  libchudware.dylib on an Apple-silicon Mac. Windows is not built from here —
#  use CMakePresets.json's windows-* presets from an x64 Native Tools prompt.
#
#  The ui/imgui / ui/abstract (AI-CAD2) app is retired. host/ is the single
#  cross-platform GUI target (builds on Linux with GCC/Clang, macOS with
#  AppleClang, and Windows with MSVC). Binary:
#  host/build-$PLATFORM_SUFFIX/bin/chudware (plus Chudware and Chudware compat symlinks)
#
#  Usage:
#    ./build.sh            Default: build the host GUI.
#    ./build.sh app        Same as default.
#    ./build.sh core       Only the C++ kernel (this host's core preset).
#    ./build.sh all        core, then host GUI.
#    ./build.sh wasm       Emscripten WASM host (full app); stages to
#                          ~/chudware-web/app/. Requires emsdk (~/.emsdk).
#                          Linux-only (GNU sed / sha1sum).
#    ./build.sh stage      Deploy the built engine (libchudware.$SHLIB_EXT + Netgen/lib3mf
#                          sibling shared libs) AND the chudware-hostd daemon from
#                          core/build-occt-$PLATFORM_SUFFIX/bin into services/dist/Chudware/
#                          — the dist dir core_bridge/daemon_bridge resolve
#                          first. This replaces the old doc'd manual cp+mv;
#                          both binaries MUST come from the same build tree or
#                          clients compute different hostd instance keys.
#    ./build.sh clean      Wipe host/build-$PLATFORM_SUFFIX (core kept — OCCT is
#                          expensive to reconfigure), then run the default build.
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
        echo "[build]        an x64 Native Tools Command Prompt (see docs/BUILD_WINDOWS.md)."
        exit 1
        ;;
esac
LIBCHUDWARE="libchudware.$SHLIB_EXT"
HOST_BUILD="$ROOT/host/build-$PLATFORM_SUFFIX"
CORE_OCCT_BUILD="$ROOT/core/build-occt-$PLATFORM_SUFFIX"
CORE_SLIM_BUILD="$ROOT/core/build-$PLATFORM_SUFFIX"

command -v cmake >/dev/null 2>&1 || { echo "[build] ERROR: cmake not found on PATH"; exit 1; }
command -v ninja >/dev/null 2>&1 || { echo "[build] ERROR: ninja not found on PATH"; exit 1; }

# The OMP bun bridge (providers/models/memory) resolves its workspace deps from
# vendor/omp/node_modules, which is gitignored — so a fresh checkout / CI clone has no deps
# and `bun run bridge.ts` fails. Materialize them once from the committed bun.lock (frozen)
# so the built app's provider sign-in + model catalog work out of the box. Skipped when bun
# is absent (the C++ core self-heals at first bridge use) or the deps are already present.
ensure_omp_deps() {
    local vd="$ROOT/vendor/omp"
    [ -f "$vd/package.json" ] || return 0
    [ -d "$vd/node_modules" ] && return 0
    if command -v bun >/dev/null 2>&1; then
        echo "[build] installing OMP bridge deps (vendor/omp) ..."
        ( cd "$vd" && bun install --frozen-lockfile ) || echo "[build] WARN: bun install failed; bridge self-heals at first use"
    else
        echo "[build] WARN: bun not on PATH; OMP bridge deps not pre-installed (self-heals at first use)"
    fi
}

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

# `--preset $CORE_PRESET` writes to build/$CORE_PRESET/ (CMakePresets.json binaryDir), which is NOT
# where anything READS the engine from: scripts/gate.sh prefers core/build-occt-linux/bin then
# core/build-linux/bin, and the GUI dlopens host/build-$PLATFORM_SUFFIX/bin. Those trees are also
# configured differently — the preset build is ~0.8 MB smaller because CHUDWARE_WITH_NETGEN is off,
# so copying it over a full engine would silently DELETE the FEA/CFD mesher (gate.sh:450-456
# documents that exact trap). So we do not stage it; we REPORT, because the silent version of this
# cost a full day of measurements on 2026-07-29: `./build.sh core` printed DONE while every
# consumer kept loading a two-day-old engine, and six live agent sweeps were scored against stale
# code before anyone noticed.
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
        hit="$(find "$ROOT/core" -type d \( -name 'build*' -o -name hostd \) -prune -o \
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
# `./build.sh core` must mean "the engine everything READS is now current". It used to mean
# "configure and build build/linux-core", a tree no consumer opens — so it printed DONE (core) and
# changed nothing observable. Repointing the preset's binaryDir at a consumed tree is NOT the fix:
# the preset configures CHUDWARE_WITH_NETGEN=OFF, so reconfiguring core/build-linux with it would
# silently DELETE the FEA/CFD mesher from a full engine (the ~0.8 MB difference gate.sh:450-456
# documents). Instead we rebuild each consumed tree IN PLACE with its own cache, which preserves
# whatever options that tree was configured with, and then gate on the staleness check so a future
# rename cannot quietly reintroduce the split.
build_core() {
    local rc=0 built=0 tree tlist
    for tree in "$CORE_OCCT_BUILD" "$CORE_SLIM_BUILD"; do
        [ -f "$tree/build.ninja" ] || continue
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
        built=1
    done
    if [ "$built" -eq 0 ]; then
        echo "[build] no configured core tree found — configuring the $CORE_PRESET preset."
        echo "[build] NOTE: build/$CORE_PRESET is NOT read by gate.sh, the GUI, or"
        echo "[build]       CHUDWARE_LIBCHUDWARE. Run ./build.sh all for a tree they load."
        cmake --preset "$CORE_PRESET" && cmake --build --preset "$CORE_PRESET" || rc=$?
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
# ~/chudware-web/app/ (served by the gateway /app or host/wasm/coop_server.py).
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
    local stage="${CHUDWARE_WEB_APP:-$HOME/chudware-web/app}"
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
    # in host/CMakeLists.txt), so a bundle is a DERIVED artifact that goes stale the moment
    # any preloaded asset changes — and it goes stale silently, because the desktop build
    # reads assets/ off disk at startup and therefore never notices. That is not
    # hypothetical: 69690e57 (2026-08-17) re-rendered every ribbon tool icon, the served
    # bundle was still the one linked 2026-08-07, and ten days of stale icons went
    # unnoticed because nothing compared a bundle to its sources (it was finally caught by
    # hand-scanning Chudware.data for PNG bytes). This runs against the directory we just
    # staged and FAILS the build, so nobody walks away from `./build.sh wasm` believing
    # they shipped current assets when they did not.
    local parity_rc=0
    python3 "$ROOT/scripts/check_web_parity.py" --bundle "$stage" || parity_rc=$?
    if [ "$parity_rc" -ne 0 ]; then
        echo "[build] ERROR: web parity gate rejected the staged bundle (exit $parity_rc; 1=stale asset, 2=gate could not evaluate)"
        echo "[build]        $stage is NOT deployable — fix the divergence reported above, then re-run ./build.sh wasm"
        return 1
    fi
    echo "[build] WASM bundle ready: $stage  (serve with: python3 host/wasm/coop_server.py)"
}


# Full-engine OCCT tree (the engine + chudware-hostd) — the binaries the LIVE
# daemon actually serves. Not covered by `core` (slim preset tree) or
# `app`: engine-behavior changes MUST rebuild this (then restart/drain hostd)
# or the running daemon keeps executing old code — mega-8 F7/F8 root cause.
# The tree is configured once by hand (OCCT/Netgen cache); this only runs the
# incremental build.
build_daemon() {
    if [ ! -f "$CORE_OCCT_BUILD/CMakeCache.txt" ]; then
        echo "[build] ERROR: ${CORE_OCCT_BUILD#"$ROOT"/} is not configured (OCCT tree; see docs/HOSTD.md)"
        return 1
    fi
    cmake --build "$CORE_OCCT_BUILD" --target chudware chudware-hostd
    stage_libchudware_next_to_host
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
    fi
}

# Stage the engine + hostd daemon into services/dist/Chudware (the $CHUDWARE_DIST
# dir that core_bridge's _lib_candidates and daemon_bridge's resolve_hostd_bin
# try FIRST). Every file is deployed copy-to-temp + atomic `mv` in the target
# dir — NEVER `cp` over a live in-use image (SIGBUS in the mmap'ed
# process; see docs/HOSTD.md "Upgrades"). The running daemon notices the mtime
# flip and self-drains; MCP engines reload on respawn.
stage_dist() {
    local src=""
    if [ -f "$CORE_OCCT_BUILD/bin/$LIBCHUDWARE" ]; then
        src="$CORE_OCCT_BUILD/bin"
    elif [ -f "$CORE_SLIM_BUILD/bin/$LIBCHUDWARE" ]; then
        src="$CORE_SLIM_BUILD/bin"
        echo "[stage] WARNING: staging the SLIM ${CORE_SLIM_BUILD#"$ROOT"/} engine (no OCCT/Netgen —"
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
    # success. Missing either is now a hard failure with nothing written. The
    # Netgen/lib3mf siblings stay optional: a slim engine legitimately has none.
    local staged=0 f tmp
    for f in "$LIBCHUDWARE" chudware-hostd; do
        if [ ! -e "$src/$f" ]; then
            echo "[stage] ERROR: $f not found in $src — refusing to stage a partial set"
            echo "[stage]        (a mismatched engine/daemon pair splits clients across two"
            echo "[stage]        engines). Build it first: ./build.sh core"
            return 1
        fi
    done
    for f in "$LIBCHUDWARE" "libngcore.$SHLIB_EXT" "libnglib.$SHLIB_EXT" \
             "lib3mf.$SHLIB_EXT" chudware-hostd; do
        [ -e "$src/$f" ] || continue
        tmp="$dst/.$f.staging.$$"
        cp "$src/$f" "$tmp"
        mv -f "$tmp" "$dst/$f"      # atomic rename: safe under a live engine
        staged=$((staged + 1))
        echo "[stage] $f -> $dst/"
    done
    echo "[stage] staged $staged file(s) from $src"
    stage_libchudware_next_to_host
}

case "$MODE" in
  clean)
    echo "[build] wiping ${HOST_BUILD#"$ROOT"/} (core kept) ..."
    rm -rf "$HOST_BUILD"
    MODE=default
    ;;
esac

# Pack the OMP bridge deps for any build that ships/runs the engine (skip pure staging).
case "$MODE" in core|app|default|daemon|all) ensure_omp_deps ;; esac

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
