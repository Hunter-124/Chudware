# ============================================================================
#  AI-CAD — new GUI (app/)  ·  clean-room ImGui shell
#  Branch: feat/gui-rewrite.  Stack: Dear ImGui (docking) + GLFW + OpenGL.
#  Reuses the already-fetched deps under host/build/_deps so configure needs NO
#  network. See app/DESIGN.md for the architecture.
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

# Repo root = ui/imgui/../..  — deps + reused host sources resolve from here.
get_filename_component(REPO "${CMAKE_CURRENT_SOURCE_DIR}/../.." ABSOLUTE)
set(HOST     "${REPO}/host")
# Reuse host's already-fetched FetchContent sources. On Linux the build lives in
# host/build-linux; on Windows in host/build. Fall back through candidates.
if(EXISTS "${HOST}/build/_deps/glfw-src")
  set(DEPS "${HOST}/build/_deps")
elseif(EXISTS "${HOST}/build-linux/_deps/glfw-src")
  set(DEPS "${HOST}/build-linux/_deps")
else()
  set(DEPS "${HOST}/build/_deps")   # will error below with a clear message
endif()
set(GLAD_DIR "${HOST}/extern/glad")

# --- sanity: the reused dep sources must exist (host was built at least once) ---
foreach(d glfw imgui json stb)
  if(NOT EXISTS "${DEPS}/${d}-src")
    message(FATAL_ERROR
      "Missing ${DEPS}/${d}-src — configure host once (build.bat host) so FetchContent "
      "populates host/build/_deps, or edit DEPS in app/CMakeLists.txt.")
  endif()
endforeach()

# --- GLFW (use the already-checked-out source; no download) ------------------
set(GLFW_BUILD_DOCS     OFF CACHE BOOL "" FORCE)
set(GLFW_BUILD_TESTS    OFF CACHE BOOL "" FORCE)
set(GLFW_BUILD_EXAMPLES OFF CACHE BOOL "" FORCE)
set(GLFW_INSTALL        OFF CACHE BOOL "" FORCE)
add_subdirectory("${DEPS}/glfw-src" "${CMAKE_BINARY_DIR}/glfw" EXCLUDE_FROM_ALL)

# --- Dear ImGui (docking) ----------------------------------------------------
set(IMGUI "${DEPS}/imgui-src")
add_library(imgui STATIC
  ${IMGUI}/imgui.cpp
  ${IMGUI}/imgui_draw.cpp
  ${IMGUI}/imgui_tables.cpp
  ${IMGUI}/imgui_widgets.cpp
  ${IMGUI}/backends/imgui_impl_glfw.cpp
  ${IMGUI}/backends/imgui_impl_opengl3.cpp)
target_include_directories(imgui PUBLIC ${IMGUI} ${IMGUI}/backends)
target_link_libraries(imgui PUBLIC glfw)

# --- glad (GL loader) --------------------------------------------------------
add_library(glad STATIC ${GLAD_DIR}/src/gl.c)
target_include_directories(glad PUBLIC ${GLAD_DIR}/include)

# --- ui/abstract (framework-neutral command registry + view-model seam) ----
add_subdirectory("${REPO}/ui/abstract" "${CMAKE_BINARY_DIR}/ui_abstract" EXCLUDE_FROM_ALL)

# --- new app sources ---------------------------------------------------------
file(GLOB_RECURSE APP_SRC CONFIGURE_DEPENDS "${CMAKE_CURRENT_SOURCE_DIR}/src/*.cpp")

# Reused, UI-independent host sources (no ImGui dependency). They use quote-relative includes
# that resolve from host/src, so we include host headers via ${HOST} as "src/...". NOTE: we add
# ${HOST} (the host/ dir) — NOT ${HOST}/src — because host/src/math.h shadows the system <math.h>
# and breaks <cmath>; host/ has no such file.
#  - render/aicm_loader.cpp: pure-CPU AICM mesh parser (always reused by the viewport).
list(APPEND APP_SRC ${HOST}/src/render/aicm_loader.cpp)

#  - core/cpp_core.cpp: the flat-ABI libaicad loader (real doc engine; no Python worker needed).
set(USE_BRIDGE ON CACHE BOOL "Link the reused libaicad flat-ABI loader (real engine)")
if(USE_BRIDGE)
  if(EXISTS "${HOST}/src/core/cpp_core.cpp")
    list(APPEND APP_SRC ${HOST}/../services/worker/cpp_core.cpp)
  endif()
endif()

# NOTE: no WIN32 subsystem flag — we use int main() and keep a console for dev logs.
# A shipping build flips to /SUBSYSTEM:WINDOWS /ENTRY:mainCRTStartup.
add_executable(AI-CAD2 ${APP_SRC})
target_include_directories(AI-CAD2 PRIVATE
  ${CMAKE_CURRENT_SOURCE_DIR}/src
  ${REPO}/ui/abstract
  ${HOST}
  ${DEPS}/json-src/single_include
  ${DEPS}/stb-src)
target_link_libraries(AI-CAD2 PRIVATE imgui glad glfw aicad_ui_abstract)
if(USE_BRIDGE)
  if(WIN32)
    target_compile_definitions(AI-CAD2 PRIVATE AICAD_HAVE_BRIDGE=1
      AICAD_DEFAULT_LIBAICAD="${REPO}/core/build-occt/bin/Release/libaicad.dll")
  else()
    target_compile_definitions(AI-CAD2 PRIVATE AICAD_HAVE_BRIDGE=1
      AICAD_DEFAULT_LIBAICAD="${REPO}/core/build-occt-linux/bin/libaicad.so")
  endif()
endif()
if(WIN32)
  target_link_libraries(AI-CAD2 PRIVATE opengl32 dwmapi comdlg32)
else()
  # GLFW brings in X11/Wayland and OpenGL on Linux; add dl for dlopen + pthread for std::thread.
  target_link_libraries(AI-CAD2 PRIVATE dl pthread)
endif()

# Registry smoke tests (no GL/engine dependency) — lives in ui/abstract/.
add_executable(test_command_registry_app test/test_command_registry.cpp)
target_link_libraries(test_command_registry_app PRIVATE aicad_ui_abstract)

# Stage the resources next to the exe so the app finds fonts/icons at runtime.
add_custom_command(TARGET AI-CAD2 POST_BUILD
  COMMAND ${CMAKE_COMMAND} -E copy_directory
          "${CMAKE_CURRENT_SOURCE_DIR}/resources" "$<TARGET_FILE_DIR:AI-CAD2>/resources"
  COMMAND ${CMAKE_COMMAND} -E copy_directory
          "${HOST}/assets/fonts" "$<TARGET_FILE_DIR:AI-CAD2>/resources/fonts"
  COMMAND ${CMAKE_COMMAND} -E copy_directory
          "${HOST}/assets/icons" "$<TARGET_FILE_DIR:AI-CAD2>/resources/icons"
  VERBATIM)
