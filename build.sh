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
  all)          build_core; build_app ;;
  *)
    echo "[build] ERROR: unknown mode '$MODE' (use: default|app|core|all|clean)"
    exit 1
    ;;
esac

echo ""
echo "[build] DONE ($MODE)."
BIN="$ROOT/host/build-linux/bin/Billet"
[ -x "$BIN" ] && echo "[build] Run the GUI:  $BIN  (set AI_CAD_LIBAICAD to the built libaicad.so)"
exit 0
