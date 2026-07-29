cmake_minimum_required(VERSION 3.20)
project(CHUDWARE_Host LANGUAGES C CXX)

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
option(CHUDWARE_ENABLE_CEF "Build the in-process CEF off-screen browser backend" ON)

# Web/WASM builds never ship CEF. Force the option off so cmake/cef.cmake is
# never included (the SDK is native-only). Native default stays ON above.
if(EMSCRIPTEN)
  set(CHUDWARE_ENABLE_CEF OFF CACHE BOOL "Build the in-process CEF off-screen browser backend" FORCE)
endif()


# --- GLFW (window + GL context) ---------------------------------------------
# Native: FetchContent glfw 3.4. EMSCRIPTEN: contrib.glfw3 port (link flags below).
if(NOT EMSCRIPTEN)
  set(GLFW_BUILD_DOCS OFF CACHE BOOL "" FORCE)
  set(GLFW_BUILD_TESTS OFF CACHE BOOL "" FORCE)
  set(GLFW_BUILD_EXAMPLES OFF CACHE BOOL "" FORCE)
  set(GLFW_INSTALL OFF CACHE BOOL "" FORCE)
  FetchContent_Declare(glfw
    GIT_REPOSITORY https://github.com/glfw/glfw.git
    GIT_TAG 3.4)
endif()

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

# --- miniz (ZIP read for `.step+` component feature lists) ------------------
# Same amalgamated release core uses (core/CMakeLists.txt). host talks to the
# worker over IPC and does NOT link libchudware, so it vendors its own copy to read
# document.json out of a component `.step+` for the assembly tree's nested rows.
# The release ships miniz.c/miniz.h with no root CMakeLists, so populate + build
# a tiny static lib ourselves (C is already enabled on this project).
# miniz: host reads document.json out of component `.step+` zips (assembly tree).
FetchContent_Declare(miniz
  URL https://github.com/richgel999/miniz/releases/download/3.0.2/miniz-3.0.2.zip)
FetchContent_GetProperties(miniz)
if(NOT miniz_POPULATED)
  FetchContent_Populate(miniz)
endif()
add_library(chudware_host_miniz STATIC ${miniz_SOURCE_DIR}/miniz.c)
target_include_directories(chudware_host_miniz PUBLIC ${miniz_SOURCE_DIR})

if(EMSCRIPTEN)
  FetchContent_MakeAvailable(stb json imgui)
else()
  FetchContent_MakeAvailable(glfw stb json imgui)
endif()

# Shared host sources (native + EMSCRIPTEN). Desktop-only / platform TUs are
# stripped or swapped below for the web target.
set(CHUDWARE_CORE_SOURCES
  src/main.cpp
  src/app.cpp
  src/app_commands.cpp   # T-RF11: ribbon command router + family handlers (extracted from app.cpp)
  src/ui/scenarios.cpp   # scripted screenshot/test scenarios (extracted from app.cpp)
  src/core/app_state.cpp
  src/core/jobs.cpp
  # --- AI Copilot (T-RF6: split of the legacy_chat.cpp monolith — one chat impl, two hosts) ---
  src/ui/copilot/ui_common.cpp          # glass/bubble toolkit + shared small-UI helpers
  src/ui/copilot/chat_log.cpp           # message list rendering + stick-to-bottom
  src/ui/copilot/activity_timeline.cpp  # per-turn typed-block activity timeline
  src/ui/copilot/cards.cpp              # plan/clarify/corporate/swarm cards + AI-VC
  src/ui/copilot/feed.cpp               # feedTurnActivity stream-event folding
  src/ui/copilot/conversations.cpp      # conversation tabs + chat-history persistence
  src/ui/copilot/conv_ids.cpp           # session-id allocation (GL-free; unit-tested)
  src/ui/copilot/agent_text.cpp         # doc-id + atlas-glyph text sanitizers (GL-free; unit-tested)
  src/ui/copilot/text_wrap.cpp          # composer hard-break tracking (GL-free; unit-tested)
  src/ui/copilot/tool_phrase.cpp        # tool-call human phrasing + grouping keys (GL-free; unit-tested)
  src/ui/clipboard_image.cpp            # system clipboard -> PNG on disk (paste-an-image)
  src/ui/copilot/image_lightbox.cpp     # attachment thumbnails + full-size image viewer
  src/ui/copilot/settings.cpp           # AI settings sections + model dropdown
  src/ui/copilot/composer.cpp           # chat input + send path + composer chrome
  src/ui/copilot/panel.cpp              # drawAIPanel host + ai_workspace reuse hooks
  src/ui/settings_modal.cpp
  src/ui/provider_logos.cpp             # provider brand color + badge (Settings -> Providers)
  src/ui/provider_usage.cpp             # shared subscription usage poll (Settings + composer rings)
  src/ui/oauth_capture.cpp  # in-app xAI multi-account CEF OAuth (Settings → Accounts)
  src/ui/web_capabilities.cpp  # T-RF12: WASM-build gap table (ui::webBuild / ui::webGapReason)
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
  src/ui/feature_tree_nested.cpp # recursive cached referenced-part/assembly document tree
  src/ui/component_features.cpp  # assembly tree: cached read of a component `.step+` feature list
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
  ../services/ipc/ipc_client.cpp
  src/core/engine_events.cpp
  # --- end Phase 4 ---
  src/ui_theme.cpp
  src/ui/tesseract.cpp  # brand mark: live 4D tesseract projection (copilot logo + Appearance preview)
  src/win_chrome.cpp
  src/stb_impl.cpp
  # --- C14: ribbon widgets + command table + icon loader ---
  src/ui/icons.cpp
  src/ui/icons_sdf.cpp  # liquid-glass v2: runtime SDF glyphs + halo (drawIconGlass)
  src/ui/widgets/ribbon.cpp
  src/ui/widgets/web_view_backend.cpp  # IWebViewBackend factory → CEF (or NullWebView without the SDK)
  src/ui/widgets/interwebz.cpp
  src/ui/widgets/interwebz_demo.cpp
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
  src/ui/web_panel.cpp   # "Web" title-bar chip: full-window in-app CEF browser overlay
  src/ui/plm/plm_workspace.cpp
  src/ui/plm/plm_pages.cpp   # plmDraw*Page implementations (split out of plm_workspace)
  src/ui/plm/plm_state.cpp   # plmInvalidate/plmEnsureOverview/plmProjectName
  src/ui/plm/plm_quickbar.cpp # embedded per-doc PLM control (status-bar chip + popover)
  src/ui/plm/graph_tab.cpp     # PLM Graph tab (cad_graph_get/blast draw-list)
  src/ui/plm/graph_force.cpp    # PLM Graph force-directed layout engine (headless)
  src/ui/plm/graph_thumbs.cpp   # PLM Graph grid-view iso thumbnails (.plm/thumbs cache)
  src/ui/plm_overview.cpp        # compact PLM overview (left manager tab, left of Explorer)
  src/ui/ai/ai_workspace.cpp
  src/ui/ai/active_work.cpp     # Active Work: live grid of agent-modified docs + op tickers
  src/ui/ai/ai_prefs.cpp        # AI workspace UI prefs (verbosity / follow / previews)
  src/ui/ai/metrics_view.cpp    # AI workspace Metrics view (token spend / usage aggregation)
  src/ui/ai/plan_board.cpp      # Interactive Session Plan board + ai_vc timeline/rollback
  src/ui/ai/docs_panel.cpp      # AI-tab document attach / preview / edit panel
  src/ui/reveng/reveng_workspace.cpp  # RE-03 Reverse Engineering workspace shell
  src/ui/firmware/fw_workspace.cpp    # T-FIN5 Firmware workspace (projects/build/emulation/trace)
  src/ui/reveng/dim_review.cpp        # RE-22 dim-confirm review + deviation panels
  src/ui/reveng/img_tex.cpp          # RE-08 JPEG/PNG->GL texture (shared by RE-19 gallery)
  src/ui/reveng/image_gallery.cpp    # RE-19 thumbnail grid + drag-drop gallery
  src/ui/reveng/sources_panel.cpp    # RE-30 source-mode entry panel (Scan/Datasheet/Web/Library)
  src/ui/reveng/stage_panels.cpp     # Scale / Model / Deliver stage panels
  src/ui/reveng/reveng_widgets.cpp   # RE-40 shared visual kit (cards/chips/previews/steps)
  # --- end [parts-plm sources] ---
  # --- FsBackend + browser fetch/pick (native + web) ---
  src/core/fs_backend.cpp
  src/core/web_fetch.cpp
  src/core/web_file_pick.cpp
  # --- end FsBackend ---
  ${imgui_SOURCE_DIR}/imgui.cpp
  ${imgui_SOURCE_DIR}/imgui_draw.cpp
  ${imgui_SOURCE_DIR}/imgui_tables.cpp
  ${imgui_SOURCE_DIR}/imgui_widgets.cpp
  ${imgui_SOURCE_DIR}/backends/imgui_impl_glfw.cpp
  ${imgui_SOURCE_DIR}/backends/imgui_impl_opengl3.cpp
)

if(EMSCRIPTEN)
  # Full app under EMSCRIPTEN: same core list, swap IPC transport, stub bootstrap/
  # discovery/spawn, drop glad + camera (browser has no V4L2/MF).
  set(CHUDWARE_WASM_SOURCES ${CHUDWARE_CORE_SOURCES})
  list(APPEND CHUDWARE_WASM_SOURCES
    src/wasm_stubs.cpp
    ../services/ipc/ipc_transport_wasm.cpp
    src/ui/reveng/webcam_capture.cpp   # RE-19 widget (uses camera_stub under web)
    src/platform/camera_stub.cpp)
  # Desktop-only TUs that must not enter the wasm link (if present in core list).
  # camera / glad / native IPC / bootstrap are added only on the native branch.
  add_executable(Chudware ${CHUDWARE_WASM_SOURCES})
else()
  set(CHUDWARE_NATIVE_SOURCES ${CHUDWARE_CORE_SOURCES})
  list(APPEND CHUDWARE_NATIVE_SOURCES
    src/bootstrap.cpp
    ../services/ipc/ipc_transport.cpp
    ../services/ipc/ipc_transport_win.cpp
    ../services/ipc/ipc_discovery.cpp
    ../services/ipc/ipc_discovery_win.cpp
    src/ui/reveng/webcam_capture.cpp   # RE-19 webcam widget + coverage overlay
    src/platform/camera_stub.cpp
    ${CMAKE_CURRENT_SOURCE_DIR}/../extern/glad/src/gl.c)
  add_executable(Chudware ${CHUDWARE_NATIVE_SOURCES})
endif()


# Embed the Windows application icon + version info into chudware.exe so File
# Explorer and the taskbar show the brand icon instead of the default. The .rc
# references assets/app.ico (generated by assets/make_logo.py).
if(NOT EMSCRIPTEN)
  if(WIN32)
    target_sources(Chudware PRIVATE app.rc)
  endif()
  if(NOT WIN32)
    target_sources(Chudware PRIVATE src/platform/camera_v4l2.cpp)
  else()
    target_sources(Chudware PRIVATE src/platform/camera_mf.cpp)
  endif()
endif()


if(EMSCRIPTEN)
  target_include_directories(Chudware PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/src
    ${CMAKE_CURRENT_SOURCE_DIR}/../core
    ${CMAKE_CURRENT_SOURCE_DIR}/../services/worker
    ${CMAKE_CURRENT_SOURCE_DIR}/../services/ipc
    ${stb_SOURCE_DIR}
    ${imgui_SOURCE_DIR}
    ${imgui_SOURCE_DIR}/backends)
  target_compile_definitions(Chudware PRIVATE CHUDWARE_GLES3=1)
  target_link_libraries(Chudware PRIVATE nlohmann_json::nlohmann_json chudware_host_miniz)
  # Assets preloaded at /assets (fonts + icon atlases the full app loads at boot).
  set(_WASM_ASSETS "${CMAKE_CURRENT_SOURCE_DIR}/../assets")
  # Main-thread ImGui with ASYNCIFY yield (emscripten_sleep(0) after swap) +
  # pthread pool for engine/WS workers. No PROXY_TO_PTHREAD / OffscreenCanvas.
  target_link_options(Chudware PRIVATE
    "SHELL:--use-port=contrib.glfw3"
    "SHELL:-lwebsocket.js"
    "SHELL:-lidbfs.js"
    "SHELL:-sWASM=1"
    "SHELL:-pthread"
    "SHELL:-sPTHREAD_POOL_SIZE=8"
    "SHELL:-sASYNCIFY"
    "SHELL:-sASYNCIFY_STACK_SIZE=67108864"
    "SHELL:-sSTACK_SIZE=64MB"
    "SHELL:-sTOTAL_STACK=64MB"
    "SHELL:-sDEFAULT_PTHREAD_STACK_SIZE=2097152"
    "SHELL:-sINITIAL_MEMORY=128MB"
    "SHELL:-sALLOW_MEMORY_GROWTH=1"
    "SHELL:-sMAXIMUM_MEMORY=4GB"
    "SHELL:-sMAX_WEBGL_VERSION=2"
    "SHELL:-sMIN_WEBGL_VERSION=2"
    "SHELL:-sFETCH=1"
    "SHELL:-sFULL_ES3=1"
    "SHELL:-sUSE_WEBGL2=1"
    "SHELL:-sEXIT_RUNTIME=0"
    "SHELL:-sASSERTIONS=1"
    "SHELL:-sSTACK_OVERFLOW_CHECK=1"
    "SHELL:-sEXPORTED_RUNTIME_METHODS=['ccall','cwrap']"
    "SHELL:-sEXPORTED_FUNCTIONS=['_main','_malloc','_free']"
    "SHELL:--shell-file=${CMAKE_CURRENT_SOURCE_DIR}/wasm/shell.html"
    "SHELL:--js-library=${CMAKE_CURRENT_SOURCE_DIR}/wasm/chudware_web_dialogs.js"
    "SHELL:--preload-file=${_WASM_ASSETS}/fonts@/assets/fonts"
    "SHELL:--preload-file=${_WASM_ASSETS}/icons/atlas.json@/assets/icons/atlas.json"
    "SHELL:--preload-file=${_WASM_ASSETS}/icons/atlas_dark.png@/assets/icons/atlas_dark.png"
    "SHELL:--preload-file=${_WASM_ASSETS}/icons/atlas_light.png@/assets/icons/atlas_light.png"
    "SHELL:--preload-file=${_WASM_ASSETS}/logo_icon.png@/assets/logo_icon.png"
    "SHELL:--preload-file=${_WASM_ASSETS}/logo_wordmark.png@/assets/logo_wordmark.png"
    "SHELL:--preload-file=${_WASM_ASSETS}/logo_wordmark_dark.png@/assets/logo_wordmark_dark.png"
  )
  target_compile_options(Chudware PRIVATE
    "SHELL:--use-port=contrib.glfw3"
    "SHELL:-pthread")
  set_target_properties(Chudware PROPERTIES
    SUFFIX ".html"
    OUTPUT_NAME "Chudware")
  # The Emscripten shell is embedded at LINK time via --shell-file, but that flag is
  # NOT a tracked input — without this, editing shell.html silently fails to relink
  # (stale <title>/boot text ship). Declare it so ninja relinks on shell changes.
  set_property(TARGET Chudware APPEND PROPERTY LINK_DEPENDS
    ${CMAKE_CURRENT_SOURCE_DIR}/wasm/shell.html)
  set_property(TARGET Chudware APPEND PROPERTY LINK_DEPENDS
    ${CMAKE_CURRENT_SOURCE_DIR}/wasm/chudware_web_dialogs.js)
else()
  target_include_directories(Chudware PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/src
    ${CMAKE_CURRENT_SOURCE_DIR}/../core
    ${CMAKE_CURRENT_SOURCE_DIR}/../services/worker
    ${CMAKE_CURRENT_SOURCE_DIR}/../services/ipc
    ${stb_SOURCE_DIR}
    ${CMAKE_CURRENT_SOURCE_DIR}/../extern/glad/include
    ${imgui_SOURCE_DIR}
    ${imgui_SOURCE_DIR}/backends)
  target_link_libraries(Chudware PRIVATE glfw nlohmann_json::nlohmann_json chudware_host_miniz)
  # Output name: the produced binary is "chudware" (lowercase). The cmake/ninja
  # target name stays "Chudware" — build.sh and scripts/gate.sh invoke it by that
  # target name (`ninja ... Chudware`), so only the on-disk filename changes here.
  set_target_properties(Chudware PROPERTIES OUTPUT_NAME "chudware")
  if(UNIX AND NOT APPLE)
    target_link_libraries(Chudware PRIVATE rt)  # shm_open (older glibc)
  endif()
endif()

# CEF off-screen browser backend (opt-in). cmake/cef.cmake finds the CEF binary
# distribution, adds the cef_web_view sources + libcef_dll_wrapper, defines
# CHUDWARE_ENABLE_CEF, and registers the mandatory subprocess-helper target. Kept
# out of the default build entirely (no-op when the option is OFF).
if(CHUDWARE_ENABLE_CEF AND NOT EMSCRIPTEN)
  include(${CMAKE_CURRENT_SOURCE_DIR}/cmake/cef.cmake)
endif()
if(WIN32)
  target_link_libraries(Chudware PRIVATE dwmapi comdlg32 dbghelp)  # comdlg32: C41 file dialogs; dbghelp: crash stack/minidump
  # windows.h min/max macros break CEF headers (std::numeric_limits<>::max()) and
  # any other code that uses std::min/std::max — always define NOMINMAX on the host.
  target_compile_definitions(Chudware PRIVATE NOMINMAX WIN32_LEAN_AND_MEAN)
  # Emit debug info even in Release so crash stacks symbolize (PDB next to the exe).
  if(MSVC)
    target_compile_options(Chudware PRIVATE $<$<CONFIG:Release>:/Zi>)
    target_link_options(Chudware PRIVATE $<$<CONFIG:Release>:/DEBUG /OPT:REF /OPT:ICF>)
    # MSVC 14.51 (VS 2026): UCRT <cmath> is missing C99 float math declarations in the
    # global namespace; force-include the shim on C++ TUs only (see extern/fix_math.h).
    # Do NOT quote the path inside /FI — under Ninja+MSVC the quotes become part of the
    # filename (C1083). Ninja quotes the whole /FI<path> token correctly without them.
    target_compile_options(Chudware PRIVATE
      $<$<COMPILE_LANGUAGE:CXX>:/FI${CMAKE_CURRENT_SOURCE_DIR}/extern/fix_math.h>)
  endif()
endif()

set_target_properties(Chudware PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")

if(NOT EMSCRIPTEN)
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
  src/stb_impl.cpp          # RE-40: V4L2 MJPEG frames decode via stb_image
  tests/test_camera_backend.cpp)
if(NOT WIN32)
  target_sources(test_camera_backend PRIVATE src/platform/camera_v4l2.cpp)
else()
  target_sources(test_camera_backend PRIVATE src/platform/camera_mf.cpp)
endif()
target_include_directories(test_camera_backend PRIVATE
  ${CMAKE_CURRENT_SOURCE_DIR}/src
  ${stb_SOURCE_DIR})
set_target_properties(test_camera_backend PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_test(NAME test_camera_backend
  COMMAND test_camera_backend
  WORKING_DIRECTORY "${CMAKE_BINARY_DIR}")
# Real V4L2 smoke: ctest -R test_camera_backend with CHUDWARE_TEST_CAMERA=1 env set externally
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

# --- copilot session-id allocation: links the REAL conv_ids.cpp (no GL / imgui / AppState).
# Guards the restart-collision data loss: convCounter restarted at 0 every launch and
# re-issued c1, c2, c3… over the transcripts already on disk.
add_executable(test_conv_ids
  src/ui/copilot/conv_ids.cpp
  tests/test_conv_ids.cpp)
target_include_directories(test_conv_ids PRIVATE
  ${CMAKE_CURRENT_SOURCE_DIR}/src)
set_target_properties(test_conv_ids PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_test(NAME test_conv_ids
  COMMAND test_conv_ids
  WORKING_DIRECTORY "${CMAKE_BINARY_DIR}")
# --- end session-id allocation ---

# --- copilot agent-authored text sanitizers: links the REAL agent_text.cpp (no GL / imgui /
# AppState). Guards two live chat defects: tool-call cards printing the opaque engine doc id
# ("doc-cpp00000039") where the part's name belongs, and U+2192 arrows from the Python harness
# rendering as notdef '?' because uiGlyphRanges() never requested the arrows block. The test
# also FAILS if the atlas range table mirrored in agent_text.cpp drifts from ui_theme.cpp.
add_executable(test_agent_text
  src/ui/copilot/agent_text.cpp
  tests/test_agent_text.cpp)
target_include_directories(test_agent_text PRIVATE
  ${CMAKE_CURRENT_SOURCE_DIR}/src)
target_compile_definitions(test_agent_text PRIVATE
  UI_THEME_CPP="${CMAKE_CURRENT_SOURCE_DIR}/src/ui_theme.cpp")
set_target_properties(test_agent_text PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_test(NAME test_agent_text
  COMMAND test_agent_text
  WORKING_DIRECTORY "${CMAKE_BINARY_DIR}")
# --- end agent-authored text sanitizers ---

# --- composer hard-break tracking (GL-free) ---
# Guards the bug where Alt/Shift+Enter appeared to do nothing: the composer's hand-rolled word
# wrapper flattens every '\n' to reflow from an idempotent source, so a break the user typed
# was erased on their next keystroke. The offsets live out-of-band and must survive inserts,
# deletes and pastes elsewhere in the buffer — that shifting is what this test pins down.
add_executable(test_text_wrap
  src/ui/copilot/text_wrap.cpp
  tests/test_text_wrap.cpp)
target_include_directories(test_text_wrap PRIVATE
  ${CMAKE_CURRENT_SOURCE_DIR}/src)
set_target_properties(test_text_wrap PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_test(NAME test_text_wrap
  COMMAND test_text_wrap
  WORKING_DIRECTORY "${CMAKE_BINARY_DIR}")
# --- end composer hard-break tracking ---

# --- tool-call human phrasing + grouping (GL-free) ---
# The chat transcript used to print the raw MCP symbol ("Doing . add_features") and stack one
# card per call, so a repair loop read as a wall of identical rows. tool_phrase.cpp turns a
# (tool, target, ok) triple into the sentence the card shows and the key consecutive calls are
# grouped by; both are pure string logic, so they are pinned here rather than eyeballed in the
# running GUI.
add_executable(test_tool_phrase
  src/ui/copilot/tool_phrase.cpp
  tests/test_tool_phrase.cpp)
target_include_directories(test_tool_phrase PRIVATE
  ${CMAKE_CURRENT_SOURCE_DIR}/src)
set_target_properties(test_tool_phrase PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_test(NAME test_tool_phrase
  COMMAND test_tool_phrase
  WORKING_DIRECTORY "${CMAKE_BINARY_DIR}")
# --- end tool-call human phrasing ---

# --- pasted-image attachment naming (GL-free) ---
# Ctrl+V in the composer writes the clipboard bitmap to a file whose name and location the
# rest of the app then trusts. The platform clipboard read cannot be unit-tested here, but the
# naming, the image-extension predicate and the cache-directory layout can — and a bad name is
# exactly what silently strands an attachment or lets it escape the cache dir.
add_executable(test_clipboard_image
  src/ui/clipboard_image.cpp
  tests/test_clipboard_image.cpp)
target_compile_definitions(test_clipboard_image PRIVATE CHUDWARE_CLIPBOARD_IMAGE_TEST=1)
target_include_directories(test_clipboard_image PRIVATE
  ${CMAKE_CURRENT_SOURCE_DIR}/src)
set_target_properties(test_clipboard_image PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_test(NAME test_clipboard_image
  COMMAND test_clipboard_image
  WORKING_DIRECTORY "${CMAKE_BINARY_DIR}")
# --- end pasted-image attachment naming ---

# Copy brand/font assets next to the exe so the GUI can load them at runtime.
add_custom_command(TARGET Chudware POST_BUILD
  COMMAND ${CMAKE_COMMAND} -E copy_directory
          "${CMAKE_CURRENT_SOURCE_DIR}/../assets"
          "$<TARGET_FILE_DIR:Chudware>/assets"
  COMMENT "Copying assets next to chudware.exe")


endif()  # NOT EMSCRIPTEN (native tests + asset copy)
