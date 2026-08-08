#!/usr/bin/env bash
# ===========================================================================
#  Chudware build script (Linux) — thin wrapper around CMake/Ninja.
# ---------------------------------------------------------------------------
#  Builds the native CMake/Ninja projects that make up Chudware (formerly Billet / AI-CAD):
#    core -> the C++ OCCT kernel (libchudware.so)
#    app  -> Chudware host GUI (cmake -S host -B host/build-linux + ninja Chudware)
#
#  The ui/imgui / ui/abstract (AI-CAD2) app is retired. host/ is the single
#  cross-platform GUI target (builds on Linux with GCC/Clang + on Windows with
#  MSVC). Binary: host/build-linux/bin/chudware (plus Chudware and Chudware compat symlinks)
#
#  Usage:
#    ./build.sh            Default: build the host GUI.
#    ./build.sh app        Same as default.
#    ./build.sh core       Only the C++ kernel (linux-core preset).
#    ./build.sh all        core, then host GUI.
#    ./build.sh wasm       Emscripten WASM host (full app); stages to
#                          ~/chudware-web/app/. Requires emsdk (~/.emsdk).
#    ./build.sh stage      Deploy the built engine (libchudware.so + Netgen/lib3mf
#                          sibling .so files) AND the chudware-hostd daemon from
#                          core/build-occt-linux/bin into services/dist/Chudware/
#                          — the dist dir core_bridge/daemon_bridge resolve
#                          first. This replaces the old doc'd manual cp+mv;
#                          both binaries MUST come from the same build tree or
#                          clients compute different hostd instance keys.
#    ./build.sh clean      Wipe host/build-linux (core kept — OCCT is expensive
#                          to reconfigure), then run the default build.
#
#  Close any running Chudware / MCP server first if a link step reports the output
#  is busy (both hold libchudware.so).
# ===========================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODE="${1:-default}"

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
# `--preset linux-core` writes to build/linux-core/ (CMakePresets.json binaryDir), which is NOT
# where anything READS the engine from: scripts/gate.sh prefers core/build-occt-linux/bin then
# core/build-linux/bin, and the GUI dlopens host/build-linux/bin. Those trees are also configured
# differently — the preset build is ~0.8 MB smaller because CHUDWARE_WITH_NETGEN is off, so copying
# it over a full engine would silently DELETE the FEA/CFD mesher (gate.sh:450-456 documents that
# exact trap). So we do not stage it; we REPORT, because the silent version of this cost a full day
# of measurements on 2026-07-29: `./build.sh core` printed DONE while every consumer kept loading a
# two-day-old .so, and six live agent sweeps were scored against stale code before anyone noticed.
# Uses `find -newer -print -quit` per consumer rather than sorting every source: piping find into
# `xargs ls -t | head -1` makes xargs die of SIGPIPE, and under this script's `set -o pipefail`
# that aborted the whole build with exit 125 (caught 2026-07-29 — `./build.sh all` silently stopped
# after the core stage). No pipes here, and the whole thing is guarded so a diagnostic can never
# fail a build.
# `strict` (any non-empty first arg) turns the diagnostic into a GATE: used after build_core has
# actually tried to rebuild, where a still-stale consumer is a real failure rather than a notice.
report_engine_staleness() {
    local strict="${1:-}" consumer hit stale=0
    for consumer in "$ROOT/core/build-occt-linux/bin/libchudware.so" \
                    "$ROOT/core/build-linux/bin/libchudware.so" \
                    "$ROOT/host/build-linux/bin/libchudware.so"; do
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
            echo "[build]       ninja -C core/build-linux bin/libchudware.so   # or ./build.sh all"
        fi
    done
    # The daemon is a SEPARATE executable that dlopens the staged libchudware; it does not
    # link the engine's simulation/modeling objects (see the chudware-hostd target below).
    # Only daemon sources and its CppCore loader can make the executable stale. Comparing
    # every engine source here made a successful lib-only rebuild permanently fail this
    # gate even though the fresh library is the artifact that contains those changes.
    for consumer in "$ROOT/core/build-occt-linux/bin/chudware-hostd" \
                    "$ROOT/core/build-linux/bin/chudware-hostd" \
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
    for tree in core/build-occt-linux core/build-linux; do
        [ -f "$ROOT/$tree/build.ninja" ] || continue
        echo "[build] core engine -> $tree/bin/libchudware.so (consumed tree)"
        ninja -C "$ROOT/$tree" bin/libchudware.so || rc=$?
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
        tlist="$(ninja -C "$ROOT/$tree" -t targets all 2>/dev/null || true)"
        case "$tlist" in
            *"bin/chudware-hostd:"*)
                echo "[build] core daemon -> $tree/bin/chudware-hostd"
                ninja -C "$ROOT/$tree" bin/chudware-hostd || rc=$?
                ;;
        esac
        built=1
    done
    if [ "$built" -eq 0 ]; then
        echo "[build] no configured core tree found — configuring the linux-core preset."
        echo "[build] NOTE: build/linux-core is NOT read by gate.sh, the GUI, or"
        echo "[build]       CHUDWARE_LIBCHUDWARE. Run ./build.sh all for a tree they load."
        cmake --preset linux-core && cmake --build --preset linux-core || rc=$?
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
    cmake -S "$ROOT/host" -B "$ROOT/host/build-linux" -G Ninja -DCMAKE_BUILD_TYPE=Release
    ninja -C "$ROOT/host/build-linux" Chudware
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
    echo "[build] WASM bundle ready: $stage  (serve with: python3 host/wasm/coop_server.py)"
}


# Full-engine OCCT tree (libchudware.so + chudware-hostd) — the binaries the LIVE
# daemon actually serves. Not covered by `core` (slim linux-core preset) or
# `app`: engine-behavior changes MUST rebuild this (then restart/drain hostd)
# or the running daemon keeps executing old code — mega-8 F7/F8 root cause.
# The tree is configured once by hand (OCCT/Netgen cache); this only runs the
# incremental build.
build_daemon() {
    if [ ! -f "$ROOT/core/build-occt-linux/CMakeCache.txt" ]; then
        echo "[build] ERROR: core/build-occt-linux is not configured (OCCT tree; see docs/HOSTD.md)"
        return 1
    fi
    cmake --build "$ROOT/core/build-occt-linux" --target chudware chudware-hostd
    stage_libchudware_next_to_host
}

# Copy the full-engine libchudware.so next to host/build-linux/bin/chudware so the
# running GUI's in-process cpp_core (resolve tier 3: next to executable) picks up
# the latest engine. Without this, ./build.sh core/daemon updates trees the host
# never loads. Atomic tmp+mv — never cp over a live mmap'd .so (SIGBUS risk).
stage_libchudware_next_to_host() {
    local src=""
    if [ -f "$ROOT/core/build-occt-linux/bin/libchudware.so" ]; then
        src="$ROOT/core/build-occt-linux/bin/libchudware.so"
    elif [ -f "$ROOT/core/build-linux/bin/libchudware.so" ]; then
        src="$ROOT/core/build-linux/bin/libchudware.so"
    else
        return 0
    fi
    local dst_dir="$ROOT/host/build-linux/bin"
    if [ -d "$dst_dir" ]; then
        local tmp="$dst_dir/.libchudware.so.staging.$$"
        cp "$src" "$tmp"
        mv -f "$tmp" "$dst_dir/libchudware.so"
        echo "[stage] libchudware.so -> $dst_dir/ (host in-process engine)"
    fi
}

# Stage the engine + hostd daemon into services/dist/Chudware (the $CHUDWARE_DIST
# dir that core_bridge's _lib_candidates and daemon_bridge's resolve_hostd_bin
# try FIRST). Every file is deployed copy-to-temp + atomic `mv` in the target
# dir — NEVER `cp` over a live in-use .so/binary (SIGBUS in the mmap'ed
# process; see docs/HOSTD.md "Upgrades"). The running daemon notices the mtime
# flip and self-drains; MCP engines reload on respawn.
stage_dist() {
    local src=""
    if [ -f "$ROOT/core/build-occt-linux/bin/libchudware.so" ]; then
        src="$ROOT/core/build-occt-linux/bin"
    elif [ -f "$ROOT/core/build-linux/bin/libchudware.so" ]; then
        src="$ROOT/core/build-linux/bin"
        echo "[stage] WARNING: staging the SLIM core/build-linux engine (no OCCT/Netgen —"
        echo "[stage]          FEA/CFD RPCs unavailable). Build core/build-occt-linux for the full engine."
    else
        echo "[stage] ERROR: no built libchudware.so (run ./build.sh core first)"
        return 1
    fi
    local dst="$ROOT/services/dist/Chudware"
    mkdir -p "$dst"
    # libchudware.so first, then its RUNPATH ($ORIGIN) sibling deps, then the
    # daemon that dlopens it. chudware-hostd MUST be staged from the SAME build
    # tree as libchudware.so: clients key the daemon instance on the hostd
    # binary's realpath, so a dist libchudware with a build-dir hostd silently
    # splits clients across two engines.
    local staged=0 f tmp
    for f in libchudware.so libngcore.so libnglib.so lib3mf.so chudware-hostd; do
        if [ ! -e "$src/$f" ]; then
            if [ "$f" = "libchudware.so" ] || [ "$f" = "chudware-hostd" ]; then
                echo "[stage] WARNING: $f not found in $src — skipped"
            fi
            continue
        fi
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
    echo "[build] wiping host/build-linux (core kept) ..."
    rm -rf "$ROOT/host/build-linux"
    MODE=default
    ;;
esac

# Pack the OMP bridge deps for any build that ships/runs the engine (skip pure staging).
case "$MODE" in core|app|default|daemon|all) ensure_omp_deps ;; esac

case "$MODE" in
  core)         build_core ;;
  app|default)  build_app ;;
  daemon)       build_daemon ;;
  wasm)         build_wasm; echo ""; echo "[build] DONE (wasm)."; exit 0 ;;
  all)          build_core; build_app; build_daemon ;;
  stage)        stage_dist; echo ""; echo "[build] DONE (stage)."; exit 0 ;;
  *)
    echo "[build] ERROR: unknown mode '$MODE' (use: default|app|core|daemon|wasm|all|stage|clean)"
    exit 1
    ;;
esac

echo ""
echo "[build] DONE ($MODE)."
BIN="$ROOT/host/build-linux/bin/chudware"
[ -x "$BIN" ] && echo "[build] Run the GUI:  $BIN  (set CHUDWARE_LIBCHUDWARE to the built libchudware.so)"
exit 0
