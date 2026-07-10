cmake_minimum_required(VERSION 3.20)
project(AI_CAD_Host LANGUAGES C CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
if(MSVC)
  add_compile_options(/utf-8 /MP)
endif()

include(FetchContent)

# --- Embedded browser backend selection -------------------------------------
# OFF (default): the app ships only the ScreencastWebView (worker-side headless
# Chromium streamed over browser_embed_* RPCs) — no extra deps, always builds.
# ON: additionally compile the in-process CEF off-screen backend (native
# <select>/clipboard/autofill). Requires a CEF binary distribution; point
# CEF_ROOT at it. See docs/cef_webview.md and cmake/cef.cmake. The build stays
# green with this OFF so CI and contributors never need the ~1 GB CEF SDK.
# CEF is THE embedded-browser backend since the screencast removal (2026-07-07).
# Default ON: a normal build carries the vendor sign-in browser. Requires the CEF
# SDK under extern/cef/ (see cmake/cef.cmake / docs/cef_webview.md). Turn OFF only
# for an SDK-less build — makeWebView then returns a NullWebView (no browser).
option(AI_CAD_ENABLE_CEF "Build the in-process CEF off-screen browser backend" ON)

# --- GLFW (window + GL context) ---------------------------------------------
set(GLFW_BUILD_DOCS OFF CACHE BOOL "" FORCE)
set(GLFW_BUILD_TESTS OFF CACHE BOOL "" FORCE)
set(GLFW_BUILD_EXAMPLES OFF CACHE BOOL "" FORCE)
set(GLFW_INSTALL OFF CACHE BOOL "" FORCE)
FetchContent_Declare(glfw
  GIT_REPOSITORY https://github.com/glfw/glfw.git
  GIT_TAG 3.4)

# --- glad (GL 3.3 core loader) — vendored, pre-generated into extern/glad ----
# Compiled directly into the target; no configure-time fetch/generation.

# --- stb (single-header PNG writer) -----------------------------------------
FetchContent_Declare(stb
  GIT_REPOSITORY https://github.com/nothings/stb.git
  GIT_TAG master)

# --- nlohmann/json (worker IPC) ---------------------------------------------
set(JSON_BuildTests OFF CACHE INTERNAL "")
FetchContent_Declare(json
  URL https://github.com/nlohmann/json/releases/download/v3.11.3/json.tar.xz)

# --- Dear ImGui (docking branch) — GUI --------------------------------------
FetchContent_Declare(imgui
  GIT_REPOSITORY https://github.com/ocornut/imgui.git
  GIT_TAG v1.91.6-docking)

FetchContent_MakeAvailable(glfw stb json imgui)

add_executable(Billet
  src/main.cpp
  src/app.cpp
  src/ui/scenarios.cpp   # scripted screenshot/test scenarios (extracted from app.cpp)
  src/core/app_state.cpp
  src/core/jobs.cpp
  src/ui/legacy_chat.cpp
  src/ui/settings_modal.cpp
  src/ui/console_panel.cpp
  # --- W1-B: core infrastructure (toast / cursor manager / shared appearance picker) ---
  src/ui/toast.cpp
  src/ui/cursors.cpp
  src/ui/appearance_common.cpp
  # --- end W1-B ---
  src/ui/title_bar.cpp
  src/ui/layout.cpp   # Wave 1C/1D: dock layout (buildDefaultLayout) + tabbed left pane
  src/ui/status_bar.cpp
  src/ui/viewport_panel.cpp
  src/mesh.cpp
  src/renderer.cpp
  # --- C15: AICM loader + ID-buffer picking + overlays + selection ---
  src/render/aicm_loader.cpp
  ../core/util/aicm_handoff.cpp   # T-PERF10: aicm-shm:// map for loadAicmFile
  src/render/id_pass.cpp
  src/render/overlays.cpp
  src/render/splat_renderer.cpp     # RE-28: 3DGS splat renderer
  src/render/re_points.cpp          # RE-21: sparse point-cloud + camera-frusta overlay (GL)
  src/render/re_points_fixture.cpp  # RE-21: GL-free fixture builder (also linked by ctest)
  src/core/selection.cpp
  # --- end C15 ---
  # --- C16: feature tree + doc mirror ---
  src/core/doc_mirror.cpp
  ../services/worker/cpp_core.cpp
  ../services/worker/worker_router.cpp
  src/ui/feature_tree.cpp
  src/ui/file_dialog.cpp   # C41: native Win32 Save/Open dialogs
  src/ui/file_explorer.cpp # Wave 3: File Explorer panel (workspace browser + recent)
  # --- end C16 ---
  # --- document tabs (multi-document strip + per-tab switching) ---
  src/core/doc_tabs.cpp
  src/ui/doc_tabs.cpp
  # --- end document tabs ---
  # --- DRAWINGS module: 2D sheet renderer for Drawing tabs ---
  src/ui/drawing_panel.cpp
  # --- end DRAWINGS ---
  # --- C39: async worker dispatch (per-frame drained callbacks on the GL thread) ---
  src/core/async_rpc.cpp
  # --- end C39 ---
  # --- C17: PropertyManager + data-driven feature forms ---
  src/ui/property_manager.cpp
  src/ui/pm_forms.cpp
  # --- end C17 ---
  # --- C18: sketch mode UI ---
  src/ui/sketch_mode.cpp
  src/ui/airfoil.cpp   # NACA 4-/5-digit + .dat airfoil profile generator (SketchTool::Airfoil)
  src/ui/sketch3d_mode.cpp   # 3D free-space sketcher (sketch3d features)
  # --- end C18 ---
  # --- Configurations panel (SolidWorks ConfigurationManager) ---
  src/ui/config_panel.cpp
  # --- end Configurations ---
  # (the tabbed left-pane shell lives in src/ui/layout.cpp — see Wave 1C/1D above)
  # --- Design Table panel (SolidWorks editable config grid) ---
  src/ui/design_table_panel.cpp
  # --- end Design Table ---
  # --- Weldment Cut List panel (SolidWorks Weldment Cut List) ---
  src/ui/cutlist_panel.cpp
  # --- end Cut List ---
  # --- C19: simulation + flow + evaluate UI ---
  src/core/sim_client.cpp
  src/ui/sim_manager.cpp
  src/ui/legend.cpp
  src/ui/evaluate_dialogs.cpp
  # --- end C19 ---
  # --- C20b: assembly UI panels ---
  src/ui/asm_panels.cpp
  # --- end C20b ---
  # --- Equations / Global Variables editor ---
  src/ui/equations_dialog.cpp
  src/ui/profiler_window.cpp   # in-app sampling-profiler window (text report + flamegraph)
  src/ui/command_search.cpp    # unified settings + modeling-tools search popup
  # --- C22b: Activity panel + prompt editor + orchestrator wiring ---
  src/ui/activity_panel.cpp
  # --- end C22b ---
  # --- Wave N: AI-ECAD 2D GUI (schematic / PCB editor) ---
  src/ui/ecad/ecad_mirror.cpp
  src/ui/ecad/ecad_canvas.cpp
  src/ui/ecad/ecad_panel.cpp
  # --- end Wave N ---
  # --- CAM (computer-aided machining) workspace: toolpaths / post / sim ---
  src/ui/cam/cam_mirror.cpp
  src/ui/cam/cam_panel.cpp
  # --- end CAM ---
  # --- C25: mouse gesture wheel + S shortcut bar ---
  src/ui/mouse_gestures.cpp
  src/ui/camera_anim.cpp   # C25 W2-D: eased camera tween (presets / Normal-To / zoom-to-fit)
  src/ui/shortcut_bar.cpp
  src/ui/view_selector.cpp
  src/ui/section_tool.cpp   # C25 W3-I: SolidWorks-style Section View (clip plane + cap)
  # --- end C25 ---
  ../services/worker/worker_client.cpp
  # --- Phase 4 (shared engine daemon): backend seam + IPC client + event drain ---
  ../services/worker/engine_backend.cpp
  ../services/ipc/ipc_transport.cpp
  ../services/ipc/ipc_transport_win.cpp
  ../services/ipc/ipc_discovery.cpp
  ../services/ipc/ipc_discovery_win.cpp
  ../services/ipc/ipc_client.cpp
  src/core/engine_events.cpp
  # --- end Phase 4 ---
  src/bootstrap.cpp
  src/ui_theme.cpp
  src/win_chrome.cpp
  src/stb_impl.cpp
  # --- C14: ribbon widgets + command table + icon loader ---
  src/ui/icons.cpp
  src/ui/icons_sdf.cpp  # liquid-glass v2: runtime SDF glyphs + halo (drawIconGlass)
  src/ui/widgets/ribbon.cpp
  src/ui/widgets/web_view_backend.cpp  # IWebViewBackend factory → CEF (or NullWebView without the SDK)
  src/ui/ribbon_def.cpp
  # --- end C14 ---
  # --- [parts-plm sources] workspace bodies (parts-port agent appends here) ---
  src/ui/parts/parts_workspace.cpp
  src/ui/parts/parts_widgets.cpp
  src/ui/parts/parts_taxonomy_pane.cpp
  src/ui/parts/parts_filters.cpp
  src/ui/parts/parts_results.cpp
  src/ui/parts/parts_vendors.cpp
  src/ui/parts/parts_browser_panel.cpp
  src/ui/plm/plm_workspace.cpp
  src/ui/plm/plm_pages.cpp   # plmDraw*Page implementations (split out of plm_workspace)
  src/ui/plm/plm_state.cpp   # plmInvalidate/plmEnsureOverview/plmProjectName
  src/ui/plm/plm_quickbar.cpp # embedded per-doc PLM control (status-bar chip + popover)
  src/ui/ai/ai_workspace.cpp
  src/ui/reveng/reveng_workspace.cpp  # RE-03 Reverse Engineering workspace shell
  src/ui/firmware/fw_workspace.cpp    # T-FIN5 Firmware workspace (projects/build/emulation/trace)
  src/ui/reveng/dim_review.cpp        # RE-22 dim-confirm review + deviation panels
  src/ui/reveng/img_tex.cpp          # RE-08 JPEG/PNG->GL texture (shared by RE-19 gallery)
  src/ui/reveng/webcam_capture.cpp   # RE-19 webcam widget + coverage overlay
  src/ui/reveng/image_gallery.cpp    # RE-19 thumbnail grid + drag-drop gallery
  src/ui/reveng/sources_panel.cpp    # RE-30 source-mode entry panel (Scan/Datasheet/Web/Library)
  # --- end [parts-plm sources] ---
  ${CMAKE_CURRENT_SOURCE_DIR}/../extern/glad/src/gl.c
  ${imgui_SOURCE_DIR}/imgui.cpp
  ${imgui_SOURCE_DIR}/imgui_draw.cpp
  ${imgui_SOURCE_DIR}/imgui_tables.cpp
  ${imgui_SOURCE_DIR}/imgui_widgets.cpp
  ${imgui_SOURCE_DIR}/backends/imgui_impl_glfw.cpp
  ${imgui_SOURCE_DIR}/backends/imgui_impl_opengl3.cpp)

# Embed the Windows application icon + version info into Billet.exe so File
# Explorer and the taskbar show the brand icon instead of the default. The .rc
# references assets/app.ico (generated by assets/make_logo.py).
if(WIN32)
  target_sources(Billet PRIVATE app.rc)
endif()

target_include_directories(Billet PRIVATE
  ${CMAKE_CURRENT_SOURCE_DIR}/src
  ${CMAKE_CURRENT_SOURCE_DIR}/../core
  ${CMAKE_CURRENT_SOURCE_DIR}/../services/worker
  ${CMAKE_CURRENT_SOURCE_DIR}/../services/ipc
  ${stb_SOURCE_DIR}
  ${CMAKE_CURRENT_SOURCE_DIR}/../extern/glad/include
  ${imgui_SOURCE_DIR}
  ${imgui_SOURCE_DIR}/backends)
target_link_libraries(Billet PRIVATE glfw nlohmann_json::nlohmann_json)
if(UNIX AND NOT APPLE)
  target_link_libraries(Billet PRIVATE rt)  # shm_open (older glibc)
endif()

# CEF off-screen browser backend (opt-in). cmake/cef.cmake finds the CEF binary
# distribution, adds the cef_web_view sources + libcef_dll_wrapper, defines
# AI_CAD_ENABLE_CEF, and registers the mandatory subprocess-helper target. Kept
# out of the default build entirely (no-op when the option is OFF).
if(AI_CAD_ENABLE_CEF)
  include(${CMAKE_CURRENT_SOURCE_DIR}/cmake/cef.cmake)
endif()
if(WIN32)
  target_link_libraries(Billet PRIVATE dwmapi comdlg32 dbghelp)  # comdlg32: C41 file dialogs; dbghelp: crash stack/minidump
  # Emit debug info even in Release so crash stacks symbolize (PDB next to the exe).
  if(MSVC)
    target_compile_options(Billet PRIVATE $<$<CONFIG:Release>:/Zi>)
    target_link_options(Billet PRIVATE $<$<CONFIG:Release>:/DEBUG /OPT:REF /OPT:ICF>)
    # MSVC 14.51 (VS 2026): UCRT <cmath> is missing C99 float math declarations in the
    # global namespace; force-include the shim on C++ TUs only (see extern/fix_math.h).
    target_compile_options(Billet PRIVATE
      $<$<COMPILE_LANGUAGE:CXX>:/FI"${CMAKE_CURRENT_SOURCE_DIR}/extern/fix_math.h">)
  endif()
endif()

set_target_properties(Billet PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")

# --- C15: optional pure-CPU loader test (no GL/GLFW). Built with the default target set. ---
add_executable(test_aicm
  src/render/aicm_loader.cpp
  ../core/util/aicm_handoff.cpp
  src/render/test_aicm_cpp/test_aicm.cpp)
target_include_directories(test_aicm PRIVATE
  ${CMAKE_CURRENT_SOURCE_DIR}/../core
  ${CMAKE_CURRENT_SOURCE_DIR}/../extern/glad/include)
if(UNIX AND NOT APPLE)
  target_link_libraries(test_aicm PRIVATE rt)
endif()
set_target_properties(test_aicm PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
# --- end C15 ---

# --- RE-08: image decode + GL texture util test (pure-CPU, no GL context) ---
enable_testing()
add_executable(test_img_tex
  tests/test_img_tex.cpp)
target_include_directories(test_img_tex PRIVATE
  ${stb_SOURCE_DIR})
set_target_properties(test_img_tex PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_test(NAME test_img_tex
  COMMAND test_img_tex "${CMAKE_BINARY_DIR}/test_img_tex_tmp"
  WORKING_DIRECTORY "${CMAKE_BINARY_DIR}")
# --- end RE-08 ---

# --- RE-07: CameraBackend platform abstraction (V4L2 / MF / stub) ---
add_executable(test_camera_backend
  src/platform/camera_stub.cpp
  tests/test_camera_backend.cpp)
if(NOT WIN32)
  target_sources(test_camera_backend PRIVATE src/platform/camera_v4l2.cpp)
else()
  target_sources(test_camera_backend PRIVATE src/platform/camera_mf.cpp)
endif()
target_include_directories(test_camera_backend PRIVATE
  ${CMAKE_CURRENT_SOURCE_DIR}/src)
set_target_properties(test_camera_backend PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_test(NAME test_camera_backend
  COMMAND test_camera_backend
  WORKING_DIRECTORY "${CMAKE_BINARY_DIR}")
# Real V4L2 smoke: ctest -R test_camera_backend with AICAD_TEST_CAMERA=1 env set externally
# --- end RE-07 ---

# --- RE-22: dim-review accept/edit FSM unit test (no GL / imgui / rpc needed) ---
add_executable(test_dim_review_fsm
  tests/test_dim_review_fsm.cpp)
set_target_properties(test_dim_review_fsm PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_test(NAME test_dim_review_fsm
  COMMAND test_dim_review_fsm
  WORKING_DIRECTORY "${CMAKE_BINARY_DIR}")
# --- end RE-22 ---

# --- RE-28: 3DGS splat loader + depth-sort test (pure-CPU, no GL context) ---
add_executable(test_splat_renderer
  tests/test_splat_renderer.cpp
  src/render/splat_renderer.cpp
  ${CMAKE_CURRENT_SOURCE_DIR}/../extern/glad/src/gl.c)  # glad fn-ptr globals (GL never invoked here)
target_include_directories(test_splat_renderer PRIVATE
  ${CMAKE_CURRENT_SOURCE_DIR}/src
  ${CMAKE_CURRENT_SOURCE_DIR}/../extern/glad/include)
set_target_properties(test_splat_renderer PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_test(NAME test_splat_renderer
  COMMAND test_splat_renderer
          "${CMAKE_CURRENT_SOURCE_DIR}/tests/fixtures/splat_tiny.ply"
  WORKING_DIRECTORY "${CMAKE_BINARY_DIR}")
# --- end RE-28 ---

# --- RE-21: sparse point-cloud + frusta fixture geometry test (no GL context) ---
# Links only re_points_fixture.cpp (GL-free) + the test driver — no glad/GLFW needed.
add_executable(test_re_points
  src/render/re_points_fixture.cpp
  tests/test_re_points.cpp)
target_include_directories(test_re_points PRIVATE
  ${CMAKE_CURRENT_SOURCE_DIR}/src
  ${CMAKE_CURRENT_SOURCE_DIR}/../extern/glad/include)
set_target_properties(test_re_points PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_test(NAME test_re_points
  COMMAND test_re_points
  WORKING_DIRECTORY "${CMAKE_BINARY_DIR}")
# --- end RE-21 ---

# Copy brand/font assets next to the exe so the GUI can load them at runtime.
add_custom_command(TARGET Billet POST_BUILD
  COMMAND ${CMAKE_COMMAND} -E copy_directory
          "${CMAKE_CURRENT_SOURCE_DIR}/../assets"
          "$<TARGET_FILE_DIR:Billet>/assets"
  COMMENT "Copying assets next to Billet.exe")

# Compatibility symlink: existing scripts/harnesses launch .../bin/AI-CAD, so ship
# an AI-CAD -> Billet symlink beside the canonical binary (T-G10 ratified pattern).
# Windows has no reliable unprivileged symlinks, so skip it there.
if(NOT WIN32)
  add_custom_command(TARGET Billet POST_BUILD
    COMMAND ${CMAKE_COMMAND} -E create_symlink
            "$<TARGET_FILE_NAME:Billet>"
            "$<TARGET_FILE_DIR:Billet>/AI-CAD"
    COMMENT "Creating AI-CAD compatibility symlink -> Billet")
endif()
