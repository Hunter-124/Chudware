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
  all)          build_core; build_app; build_daemon ;;
  stage)        stage_dist; echo ""; echo "[build] DONE (stage)."; exit 0 ;;
  *)
    echo "[build] ERROR: unknown mode '$MODE' (use: default|app|core|daemon|all|stage|clean)"
    exit 1
    ;;
esac

echo ""
echo "[build] DONE ($MODE)."
BIN="$ROOT/host/build-linux/bin/Billet"
[ -x "$BIN" ] && echo "[build] Run the GUI:  $BIN  (set AI_CAD_LIBAICAD to the built libaicad.so)"
exit 0
