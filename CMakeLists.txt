# Minimal top-level dispatcher so CMakePresets (linux-core / linux-app) can wrap
# the existing per-subdirectory builds (core/, app/) before the real layered
# top-level CMake lands in refactor node N1a. The Linux gate (scripts/gate.sh)
# does NOT use this file — it configures core/ directly with `cmake -S core`.
# This file exists only so `cmake --preset linux-core` / `--preset linux-app`
# resolve a source tree. N1a replaces it with the real layered build graph.
cmake_minimum_required(VERSION 3.20)
project(aicad_top LANGUAGES NONE)

option(AICAD_BUILD_CORE "Add the core/ (libaicad) subdirectory" OFF)
option(AICAD_BUILD_APP  "Add the app/ (ImGui GUI) subdirectory"  OFF)

if(AICAD_BUILD_CORE)
    add_subdirectory(core)
endif()
if(AICAD_BUILD_APP)
    add_subdirectory(app)
endif()
