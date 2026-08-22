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

# Product version (semver from annotated git tags). Included BEFORE project() so
# project(VERSION) carries the same numbers the generated header does. The same
# module is included by core/CMakeLists.txt, which is configured standalone
# (`cmake -S core`) for core/build-occt-linux — this file never runs for that tree.
include(core/cmake/ChudwareVersion.cmake)
chudware_version_derive()

# The numeric triple, not CHUDWARE_VERSION: an overridden version may carry a
# prerelease suffix (0.1.0-beta.42), which project(VERSION) rejects outright.
project(chudware VERSION ${CHUDWARE_VERSION_MAJOR}.${CHUDWARE_VERSION_MINOR}.${CHUDWARE_VERSION_PATCH}
        LANGUAGES C CXX)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

option(CHUDWARE_BUILD_CORE "Build core/ (libchudware)" ON)

if(CHUDWARE_BUILD_CORE)
    add_subdirectory(core)
endif()
