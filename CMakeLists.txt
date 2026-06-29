# Top-level CMake dispatcher for the layered AI-CAD layout.
# Presets: CMakePresets.json (linux-core, linux-app).
# The Linux gate (scripts/gate.sh) configures core/ directly; presets use this file.
cmake_minimum_required(VERSION 3.20)
project(aicad LANGUAGES C CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

option(AICAD_BUILD_CORE     "Build core/ (libaicad)"           ON)
option(AICAD_BUILD_APP      "Build app/ (ImGui GUI, pre-P3)"   OFF)
option(AICAD_BUILD_UI_IMGUI "Build ui/imgui/ (post-P3)"        OFF)

if(AICAD_BUILD_CORE)
    add_subdirectory(core)
endif()
if(AICAD_BUILD_APP)
    add_subdirectory(app)
endif()
if(AICAD_BUILD_UI_IMGUI)
    add_subdirectory(ui/imgui)
endif()
