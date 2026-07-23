# Top-level CMake dispatcher for the layered Chudware layout.
# Presets: CMakePresets.json (linux-core, windows-core, windows-occt).
# The host GUI (host/) has its own CMakeLists.txt — build it directly:
#   cmake -S host -B host/build-linux -G Ninja -DCMAKE_BUILD_TYPE=Release
#   ninja -C host/build-linux Chudware
# The Linux gate (scripts/gate.sh) configures core/ via this file's linux-core
# preset; the host GUI stage builds from host/ directly.
#
# NOTE: ui/imgui and ui/abstract are retired. host/ is the single GUI.
cmake_minimum_required(VERSION 3.20)
project(chudware LANGUAGES C CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

option(CHUDWARE_BUILD_CORE "Build core/ (libchudware)" ON)

if(CHUDWARE_BUILD_CORE)
    add_subdirectory(core)
endif()
