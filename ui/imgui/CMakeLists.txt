# ============================================================================
#  AI-CAD2 — new GUI (ui/imgui/)  ·  clean-room ImGui shell
#  Stack: Dear ImGui (docking) + GLFW + OpenGL + ui/viewport + platform + worker.
# ============================================================================
cmake_minimum_required(VERSION 3.20)
project(AICAD2 LANGUAGES C CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_C_STANDARD 11)
if(MSVC)
  add_compile_options(/utf-8 /EHsc /MP)
  add_compile_definitions(NOMINMAX WIN32_LEAN_AND_MEAN _CRT_SECURE_NO_WARNINGS)
endif()

get_filename_component(REPO "${CMAKE_CURRENT_SOURCE_DIR}/../.." ABSOLUTE)
set(GLAD_DIR "${REPO}/extern/glad")

include(FetchContent)

set(GLFW_BUILD_DOCS     OFF CACHE BOOL "" FORCE)
set(GLFW_BUILD_TESTS    OFF CACHE BOOL "" FORCE)
set(GLFW_BUILD_EXAMPLES OFF CACHE BOOL "" FORCE)
set(GLFW_INSTALL        OFF CACHE BOOL "" FORCE)
FetchContent_Declare(glfw
  GIT_REPOSITORY https://github.com/glfw/glfw.git
  GIT_TAG 3.4)
FetchContent_Declare(stb
  GIT_REPOSITORY https://github.com/nothings/stb.git
  GIT_TAG master)
set(JSON_BuildTests OFF CACHE INTERNAL "")
FetchContent_Declare(json
  URL https://github.com/nlohmann/json/releases/download/v3.11.3/json.tar.xz)
FetchContent_Declare(imgui
  GIT_REPOSITORY https://github.com/ocornut/imgui.git
  GIT_TAG v1.91.6-docking)
FetchContent_MakeAvailable(glfw stb json imgui)

set(AICAD_STB_INCLUDE_DIR "${stb_SOURCE_DIR}" CACHE INTERNAL "")

# --- glad (GL loader) — root preset may create glad before viewport ------------
if(NOT TARGET glad)
  if(NOT EXISTS "${GLAD_DIR}/src/gl.c")
    message(FATAL_ERROR "Missing ${GLAD_DIR}/src/gl.c — host/extern/glad required")
  endif()
  add_library(glad STATIC ${GLAD_DIR}/src/gl.c)
  target_include_directories(glad PUBLIC ${GLAD_DIR}/include)
endif()

# --- Dear ImGui (docking) ----------------------------------------------------
set(IMGUI "${imgui_SOURCE_DIR}")
add_library(imgui_lib STATIC
  ${IMGUI}/imgui.cpp
  ${IMGUI}/imgui_draw.cpp
  ${IMGUI}/imgui_tables.cpp
  ${IMGUI}/imgui_widgets.cpp
  ${IMGUI}/backends/imgui_impl_glfw.cpp
  ${IMGUI}/backends/imgui_impl_opengl3.cpp)
target_include_directories(imgui_lib PUBLIC ${IMGUI} ${IMGUI}/backends)
target_link_libraries(imgui_lib PUBLIC glfw)

# --- ui/abstract -------------------------------------------------------------
if(NOT TARGET aicad_ui_abstract)
  add_subdirectory("${REPO}/ui/abstract" "${CMAKE_BINARY_DIR}/ui_abstract" EXCLUDE_FROM_ALL)
endif()
# T-U7: adapter .cpps (piping_view, hydraulic_view, structural_view) need nlohmann/json.
# json is fetched by this CMakeLists; wire its include dir into aicad_ui_abstract here.
target_include_directories(aicad_ui_abstract PRIVATE ${json_SOURCE_DIR}/single_include)

# --- platform + viewport (viewport may already be added by root preset) -------
add_subdirectory("${REPO}/platform" "${CMAKE_BINARY_DIR}/aicad_platform" EXCLUDE_FROM_ALL)
target_link_libraries(aicad_platform PUBLIC glfw)
target_include_directories(aicad_platform PRIVATE ${AICAD_STB_INCLUDE_DIR})

if(NOT TARGET aicad_ui_viewport)
  add_subdirectory("${REPO}/ui/viewport" "${CMAKE_BINARY_DIR}/ui_viewport" EXCLUDE_FROM_ALL)
endif()

# --- sources -----------------------------------------------------------------
file(GLOB_RECURSE APP_SRC CONFIGURE_DEPENDS "${CMAKE_CURRENT_SOURCE_DIR}/src/*.cpp")
list(FILTER APP_SRC EXCLUDE REGEX ".*/engine/engine\\.cpp$")
list(FILTER APP_SRC EXCLUDE REGEX ".*/engine/engine_pick\\.cpp$")
list(FILTER APP_SRC EXCLUDE REGEX ".*/engine/engine_sketch\\.cpp$")

file(GLOB WORKER_SRC CONFIGURE_DEPENDS "${REPO}/services/worker/*.cpp")

add_executable(AI-CAD2 ${APP_SRC} ${WORKER_SRC})
set_target_properties(AI-CAD2 PROPERTIES RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")

target_include_directories(AI-CAD2 PRIVATE
  ${CMAKE_CURRENT_SOURCE_DIR}/src
  ${REPO}
  ${REPO}/ui/abstract
  ${REPO}/services/worker
  ${json_SOURCE_DIR}/single_include
  ${AICAD_STB_INCLUDE_DIR})

find_package(OpenGL REQUIRED)

target_link_libraries(AI-CAD2 PRIVATE
  imgui_lib glad glfw
  aicad_ui_abstract aicad_platform aicad_ui_viewport
  OpenGL::GL)

if(WIN32)
  target_compile_definitions(AI-CAD2 PRIVATE AICAD_HAVE_BRIDGE=1
    AICAD_DEFAULT_LIBAICAD="${REPO}/core/build-occt/bin/Release/libaicad.dll")
  target_link_libraries(AI-CAD2 PRIVATE opengl32 dwmapi comdlg32)
  if(MSVC)
    # MSVC 14.51 (VS 2026): UCRT <cmath> is missing C99 float math declarations in the global
    # namespace; force-include the shim on C++ TUs only (parity with host/CMakeLists.txt — X1/T-WIN1).
    # ui/imgui keeps its OWN copy (extern/fix_math.h) so it survives the host/ deletion (T-G11).
    target_compile_options(AI-CAD2 PRIVATE
      $<$<COMPILE_LANGUAGE:CXX>:/FI"${CMAKE_CURRENT_SOURCE_DIR}/extern/fix_math.h">)
  endif()
else()
  target_compile_definitions(AI-CAD2 PRIVATE AICAD_HAVE_BRIDGE=1
    AICAD_DEFAULT_LIBAICAD="${REPO}/core/build-occt-linux/bin/libaicad.so")
  target_link_libraries(AI-CAD2 PRIVATE dl pthread)
endif()

if(NOT TARGET test_command_registry)
  add_executable(test_command_registry_app test/test_command_registry.cpp)
  target_include_directories(test_command_registry_app PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/src)
  target_link_libraries(test_command_registry_app PRIVATE aicad_ui_abstract)
endif()

add_custom_command(TARGET AI-CAD2 POST_BUILD
  COMMAND ${CMAKE_COMMAND} -E copy_directory
          "${CMAKE_CURRENT_SOURCE_DIR}/resources" "$<TARGET_FILE_DIR:AI-CAD2>/resources"
  COMMAND ${CMAKE_COMMAND} -E copy_directory
          "${REPO}/assets/fonts" "$<TARGET_FILE_DIR:AI-CAD2>/resources/fonts"
  COMMAND ${CMAKE_COMMAND} -E copy_directory
          "${REPO}/assets/icons" "$<TARGET_FILE_DIR:AI-CAD2>/resources/icons"
  VERBATIM)

# Additive ShopCraft alias: create a symlink ShopCraft -> AI-CAD2 in the bin dir
# so scripts using the new name work without breaking any existing AI-CAD2 references.
if(NOT WIN32)
    add_custom_command(TARGET AI-CAD2 POST_BUILD
        COMMAND ${CMAKE_COMMAND} -E create_symlink
            "$<TARGET_FILE_NAME:AI-CAD2>"
            "$<TARGET_FILE_DIR:AI-CAD2>/ShopCraft"
        COMMENT "Creating ShopCraft -> AI-CAD2 symlink"
    )
endif()

if(WIN32)
    add_custom_command(TARGET AI-CAD2 POST_BUILD
        COMMAND ${CMAKE_COMMAND} -E copy_if_different
            "$<TARGET_FILE:AI-CAD2>"
            "$<TARGET_FILE_DIR:AI-CAD2>/ShopCraft.exe"
        COMMENT "Creating ShopCraft.exe copy"
    )
endif()
