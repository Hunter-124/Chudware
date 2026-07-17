#!/usr/bin/env bash
# ===========================================================================
#  Billet build script (Linux) — thin wrapper around CMake/Ninja.
# ---------------------------------------------------------------------------
#  Builds the native CMake/Ninja projects that make up Billet (formerly AI-CAD):
#    core -> the C++ OCCT kernel (libaicad.so)
#    app  -> Billet host GUI (cmake -S host -B host/build-linux + ninja Billet)
#
#  The ui/imgui / ui/abstract (AI-CAD2) app is retired. host/ is the single
#  cross-platform GUI target (builds on Linux with GCC/Clang + on Windows with
#  MSVC). Binary: host/build-linux/bin/Billet (plus an AI-CAD compat symlink)
#
#  Usage:
#    ./build.sh            Default: build the host GUI.
#    ./build.sh app        Same as default.
#    ./build.sh core       Only the C++ kernel (linux-core preset).
#    ./build.sh all        core, then host GUI.
#    ./build.sh wasm       Emscripten WASM host (full app); stages to
#                          ~/billet-web/app/. Requires emsdk (~/.emsdk).
#    ./build.sh stage      Deploy the built engine (libaicad.so + Netgen/lib3mf
#                          sibling .so files) AND the aicad-hostd daemon from
#                          core/build-occt-linux/bin into services/dist/AI-CAD/
#                          — the dist dir core_bridge/daemon_bridge resolve
#                          first. This replaces the old doc'd manual cp+mv;
#                          both binaries MUST come from the same build tree or
#                          clients compute different hostd instance keys.
#    ./build.sh clean      Wipe host/build-linux (core kept — OCCT is expensive
#                          to reconfigure), then run the default build.
#
#  Close any running Billet / MCP server first if a link step reports the output
#  is busy (both hold libaicad.so).
# ===========================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODE="${1:-default}"

command -v cmake >/dev/null 2>&1 || { echo "[build] ERROR: cmake not found on PATH"; exit 1; }
command -v ninja >/dev/null 2>&1 || { echo "[build] ERROR: ninja not found on PATH"; exit 1; }

build_core() { cmake --preset linux-core && cmake --build --preset linux-core; }
build_app()  {
    cmake -S "$ROOT/host" -B "$ROOT/host/build-linux" -G Ninja -DCMAKE_BUILD_TYPE=Release
    ninja -C "$ROOT/host/build-linux" Billet
    # Host dlopens libaicad next to the executable (cpp_core resolve tier 3). Keep it
    # in sync with the OCCT engine so core fixes (e.g. extrude flip) actually reach the GUI.
    stage_libaicad_next_to_host
}
# Emscripten web client (full app). Sources emsdk, configures host/ with
# emcmake (CEF off), builds Billet, stages html/js/wasm/data into
# ~/billet-web/app/ (served by the gateway /app or host/wasm/coop_server.py).
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
    echo "[build] emcc: $(emcc -v 2>&1 | head -1)"
    emcmake cmake -S "$ROOT/host" -B "$ROOT/host/build-wasm" -G Ninja \
        -DCMAKE_BUILD_TYPE=Release \
        -DAI_CAD_ENABLE_CEF=OFF
    cmake --build "$ROOT/host/build-wasm" --target Billet
    local out="$ROOT/host/build-wasm/bin"
    local stage="${BILLET_WEB_APP:-$HOME/billet-web/app}"
    mkdir -p "$stage"
    # Stage the emscripten bundle (html/js/wasm + any .data preload packs).
    # emcc 6.x with PROXY_TO_PTHREAD embeds workers in Billet.js (no separate
    # Billet.worker.js); still stage any *worker*.js if a future toolchain emits one.
    local staged_names=""
    for f in "$out"/Billet.html "$out"/Billet.js "$out"/Billet.wasm \
             "$out"/Billet.data "$out"/Billet.worker.js "$out"/*worker*.js; do
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
    if [ -f "$stage/Billet.html" ]; then
        cp -f "$stage/Billet.html" "$stage/index.html"
        echo "[build] staged index.html (copy of Billet.html)"
    fi
    # Cache-bust: version the bundle URLs so browsers never pair a fresh Billet.js with a
    # stale Billet.wasm/.data (fatal LinkError). VER = short wasm content hash. index.html is
    # served no-cache, so every client picks up the new versioned URLs immediately.
    if [ -f "$stage/Billet.wasm" ]; then
        VER="$(sha1sum "$stage/Billet.wasm" | cut -c1-12)"
        for hf in "$stage/index.html" "$stage/Billet.html"; do
            [ -f "$hf" ] || continue
            sed -i \
                -e "s/__BILLET_VER__/$VER/g" \
                -e "s#src=Billet\.js#src=\"Billet.js?v=$VER\"#g" \
                -e "s#src=coi-serviceworker\.js#src=\"coi-serviceworker.js?v=$VER\"#g" \
                -e "s#src=\"Billet\.js\"#src=\"Billet.js?v=$VER\"#g" \
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
        for cf in "$stage"/Billet.wasm "$stage"/Billet.js "$stage"/Billet.data \
                  "$stage"/index.html "$stage"/Billet.html "$stage"/coi-serviceworker.js; do
            [ -f "$cf" ] || continue
            command -v brotli >/dev/null 2>&1 && brotli -f -q 11 -o "$cf.br" "$cf"
            command -v gzip  >/dev/null 2>&1 && gzip  -f -k -9 "$cf"
        done
        echo "[build] precompressed bundle (.br/.gz) for gateway content-encoding"
    fi
    echo "[build] WASM bundle ready: $stage  (serve with: python3 host/wasm/coop_server.py)"
}


# Full-engine OCCT tree (libaicad.so + aicad-hostd) — the binaries the LIVE
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
    cmake --build "$ROOT/core/build-occt-linux" --target aicad aicad-hostd
    stage_libaicad_next_to_host
}

# Copy the full-engine libaicad.so next to host/build-linux/bin/Billet so the
# running GUI's in-process cpp_core (resolve tier 3: next to executable) picks up
# the latest engine. Without this, ./build.sh core/daemon updates trees the host
# never loads. Atomic tmp+mv — never cp over a live mmap'd .so (SIGBUS risk).
stage_libaicad_next_to_host() {
    local src=""
    if [ -f "$ROOT/core/build-occt-linux/bin/libaicad.so" ]; then
        src="$ROOT/core/build-occt-linux/bin/libaicad.so"
    elif [ -f "$ROOT/core/build-linux/bin/libaicad.so" ]; then
        src="$ROOT/core/build-linux/bin/libaicad.so"
    else
        return 0
    fi
    local dst_dir="$ROOT/host/build-linux/bin"
    if [ -d "$dst_dir" ]; then
        local tmp="$dst_dir/.libaicad.so.staging.$$"
        cp "$src" "$tmp"
        mv -f "$tmp" "$dst_dir/libaicad.so"
        echo "[stage] libaicad.so -> $dst_dir/ (host in-process engine)"
    fi
}

# Stage the engine + hostd daemon into services/dist/AI-CAD (the $AI_CAD_DIST
# dir that core_bridge's _lib_candidates and daemon_bridge's resolve_hostd_bin
# try FIRST). Every file is deployed copy-to-temp + atomic `mv` in the target
# dir — NEVER `cp` over a live in-use .so/binary (SIGBUS in the mmap'ed
# process; see docs/HOSTD.md "Upgrades"). The running daemon notices the mtime
# flip and self-drains; MCP engines reload on respawn.
stage_dist() {
    local src=""
    if [ -f "$ROOT/core/build-occt-linux/bin/libaicad.so" ]; then
        src="$ROOT/core/build-occt-linux/bin"
    elif [ -f "$ROOT/core/build-linux/bin/libaicad.so" ]; then
        src="$ROOT/core/build-linux/bin"
        echo "[stage] WARNING: staging the SLIM core/build-linux engine (no OCCT/Netgen —"
        echo "[stage]          FEA/CFD RPCs unavailable). Build core/build-occt-linux for the full engine."
    else
        echo "[stage] ERROR: no built libaicad.so (run ./build.sh core first)"
        return 1
    fi
    local dst="$ROOT/services/dist/AI-CAD"
    mkdir -p "$dst"
    # libaicad.so first, then its RUNPATH ($ORIGIN) sibling deps, then the
    # daemon that dlopens it. aicad-hostd MUST be staged from the SAME build
    # tree as libaicad.so: clients key the daemon instance on the hostd
    # binary's realpath, so a dist libaicad with a build-dir hostd silently
    # splits clients across two engines.
    local staged=0 f tmp
    for f in libaicad.so libngcore.so libnglib.so lib3mf.so aicad-hostd; do
        if [ ! -e "$src/$f" ]; then
            if [ "$f" = "libaicad.so" ] || [ "$f" = "aicad-hostd" ]; then
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
    stage_libaicad_next_to_host
}

case "$MODE" in
  clean)
    echo "[build] wiping host/build-linux (core kept) ..."
    rm -rf "$ROOT/host/build-linux"
    MODE=default
    ;;
esac

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
BIN="$ROOT/host/build-linux/bin/Billet"
[ -x "$BIN" ] && echo "[build] Run the GUI:  $BIN  (set AI_CAD_LIBAICAD to the built libaicad.so)"
exit 0
