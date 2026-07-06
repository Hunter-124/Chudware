#!/usr/bin/env bash
# ===========================================================================
#  AI-CAD build script (Linux) — thin wrapper around the CMake presets.
# ---------------------------------------------------------------------------
#  Builds the native CMake/Ninja projects that make up AI-CAD:
#    core -> the C++ OCCT kernel (libaicad.so), via the linux-core preset
#    app  -> AI-CAD2, the cross-platform ImGui GUI over the shared ui/abstract
#            schema, via the linux-app preset (which also builds core transitively)
#
#  (The legacy host/ desktop GUI was retired in T-G11 — ui/imgui is the single
#  GUI target now, so this script no longer has a "host" mode.)
#
#  Usage:
#    ./build.sh            Default: build the app (linux-app preset).
#    ./build.sh app        Same as default.
#    ./build.sh core       Only the C++ kernel (linux-core preset).
#    ./build.sh all        core, then app.
#    ./build.sh clean      Wipe the app build dir (core kept — OCCT is expensive
#                          to reconfigure), then run the default build.
#
#  Close any running AI-CAD / MCP server first if a link step reports the output
#  is busy (both hold libaicad.so).
# ===========================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODE="${1:-default}"

command -v cmake >/dev/null 2>&1 || { echo "[build] ERROR: cmake not found on PATH"; exit 1; }
command -v ninja >/dev/null 2>&1 || { echo "[build] ERROR: ninja not found on PATH"; exit 1; }

build_core() { cmake --preset linux-core && cmake --build --preset linux-core; }
build_app()  { cmake --preset linux-app  && cmake --build --preset linux-app;  }

case "$MODE" in
  clean)
    echo "[build] wiping the app build dir (core kept) ..."
    rm -rf "$ROOT/build/linux-app"
    MODE=default
    ;;
esac

case "$MODE" in
  core)         build_core ;;
  app|default)  build_app ;;
  all)          build_core; build_app ;;
  *)
    echo "[build] ERROR: unknown mode '$MODE' (use: default|app|core|all|clean)"
    exit 1
    ;;
esac

echo ""
echo "[build] DONE ($MODE)."
BIN="$ROOT/build/linux-app/bin/AI-CAD2"
[ -x "$BIN" ] && echo "[build] Run the GUI:  $BIN  (set AI_CAD_LIBAICAD to the built libaicad.so)"
exit 0
