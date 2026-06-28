#!/usr/bin/env bash
# ===========================================================================
#  AI-CAD build script (Linux)  --  the build.bat counterpart for Linux dev.
# ---------------------------------------------------------------------------
#  Builds the native CMake/Ninja projects that make up AI-CAD:
#    core -> core/build-occt-linux/bin/libaicad.so   (the C++ OCCT kernel)
#    host -> host/build-linux/bin/AI-CAD             (the product: the desktop GUI)
#    app  -> app/build-linux/AI-CAD2                 (the in-progress ImGui shell rewrite)
#
#  Usage:
#    ./build.sh            Default: build the kernel (if missing) + the host GUI (the product,
#                          == build.bat's default, minus the Windows dist staging).
#    ./build.sh host       Same as default (kernel-if-missing + host AI-CAD).
#    ./build.sh core       Only the C++ kernel (libaicad.so).
#    ./build.sh app        Only the in-progress app/ rewrite (AI-CAD2 + its registry test).
#    ./build.sh all        core + host + app.
#    ./build.sh test       Build the host, then run the host's pure-CPU loader test (test_aicm).
#    ./build.sh clean      Wipe host/app build dirs (NOT core -- the OCCT kernel is expensive to
#                          reconfigure), then run the default build.
#
#  Close any running AI-CAD / MCP server first if a link step reports the output is busy
#  (both hold libaicad.so).
# ===========================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODE="${1:-default}"

CORE_SRC="$ROOT/core";  CORE_BUILD="$ROOT/core/build-occt-linux"
HOST_SRC="$ROOT/host";  HOST_BUILD="$ROOT/host/build-linux"
APP_SRC="$ROOT/app";    APP_BUILD="$ROOT/app/build-linux"
LIBAICAD="$CORE_BUILD/bin/libaicad.so"
HOST_EXE="$HOST_BUILD/bin/AI-CAD"

command -v cmake >/dev/null 2>&1 || { echo "[build] ERROR: cmake not found on PATH"; exit 1; }
command -v ninja >/dev/null 2>&1 || { echo "[build] ERROR: ninja not found on PATH"; exit 1; }

# build_project <name> <src> <build_dir> [targets...]
#   Configures (Ninja) only when the build dir has no build.ninja, then compiles.
build_project() {
  local name="$1" src="$2" bdir="$3"; shift 3
  if [ ! -f "$bdir/build.ninja" ]; then
    echo "[build] configuring $name ..."
    cmake -S "$src" -B "$bdir" -G Ninja || { echo "[build] FAILED: configure $name"; exit 1; }
  fi
  echo "[build] compiling $name ..."
  if [ "$#" -gt 0 ]; then
    cmake --build "$bdir" --target "$@" || { echo "[build] FAILED: compile $name"; exit 1; }
  else
    cmake --build "$bdir" || { echo "[build] FAILED: compile $name"; exit 1; }
  fi
}

# Stage the freshly-built kernel next to the host exe. The host dlopen()s
# "<exe_dir>/libaicad.so" (host/src/core/cpp_core.cpp), and nothing else refreshes it,
# so a core rebuild MUST recopy it here or the running GUI keeps loading a stale kernel
# (the cause of "C++ fixes pass tests but the product is unchanged"). Copy only when the
# fresh lib differs, and only if the host bin dir exists.
stage_lib() {
  local hostbin="$HOST_BUILD/bin"
  if [ -f "$LIBAICAD" ] && [ -d "$hostbin" ]; then
    if ! cmp -s "$LIBAICAD" "$hostbin/libaicad.so" 2>/dev/null; then
      cp -f "$LIBAICAD" "$hostbin/libaicad.so"
      echo "[build] staged fresh libaicad.so -> $hostbin/"
    fi
  fi
}

build_core() { build_project core "$CORE_SRC" "$CORE_BUILD"; stage_lib; }
build_host() { build_project host "$HOST_SRC" "$HOST_BUILD" AI-CAD; stage_lib; }
build_app()  { build_project app  "$APP_SRC"  "$APP_BUILD" AI-CAD2 test_command_registry; }

# Ensure the kernel the GUI dlopen()s exists; build it once if absent.
ensure_core() {
  if [ ! -f "$LIBAICAD" ]; then
    echo "[build] libaicad.so not found -- building the C++ kernel first ..."
    build_core
  fi
}

case "$MODE" in
  clean)
    echo "[build] cleaning host/build-linux and app/build-linux (core kept) ..."
    rm -rf "$HOST_BUILD" "$APP_BUILD"
    MODE=default
    ;;
esac

case "$MODE" in
  core)     build_core ;;
  host)     ensure_core; build_host ;;
  app)      build_app ;;
  all)      build_core; build_host; build_app ;;
  default)  ensure_core; build_host ;;
  test)
    ensure_core; build_host
    echo "[build] building + running the host loader test (test_aicm) ..."
    build_project host "$HOST_SRC" "$HOST_BUILD" test_aicm
    "$HOST_BUILD/bin/test_aicm" && echo "[build] test_aicm: PASS"
    ;;
  *)
    echo "[build] ERROR: unknown mode '$MODE' (use: default|host|core|app|all|test|clean)"
    exit 1
    ;;
esac

echo ""
echo "[build] DONE ($MODE)."
[ -x "$HOST_EXE" ] && echo "[build] Run the GUI:  $HOST_EXE"
exit 0
