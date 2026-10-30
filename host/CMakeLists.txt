cmake_minimum_required(VERSION 3.20)

# Product version (semver from annotated git tags). host/ is its own CMake project
# (`cmake -S host -B host/build-linux`), so it includes core's version module
# directly rather than inheriting anything — one derive implementation, shared with
# the root and core builds, so all three report the same string.
include("${CMAKE_CURRENT_SOURCE_DIR}/../core/cmake/ChudwareVersion.cmake")
chudware_version_derive()

# The numeric triple, not CHUDWARE_VERSION: an overridden version may carry a
# prerelease suffix (0.1.0-beta.42), which project(VERSION) rejects outright.
project(CHUDWARE_Host
        VERSION ${CHUDWARE_VERSION_MAJOR}.${CHUDWARE_VERSION_MINOR}.${CHUDWARE_VERSION_PATCH}
        LANGUAGES C CXX)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

# MSVC: <windows.h> defines min()/max() macros that clobber std::min, std::max and
# std::numeric_limits<>::max. core/CMakeLists.txt already suppresses this
# project-wide, but host/ is a SEPARATE CMake project that compiles several core
# sources directly (core/util/platform_paths.cpp, core/security/integrity.cpp,
# tweetnacl.c), so it does not inherit that definition. Mirror it here.
#
# NOMINMAX only. Deliberately NOT WIN32_LEAN_AND_MEAN: core/doc/formats.cpp
# documents that it needs <windows.h> pulled in FULL and that a lean include
# would poison a later one.
if(MSVC)
  add_compile_definitions(NOMINMAX)
endif()

# <build>/generated/api/chudware_version.h — build tree, never the source tree.
# ${CMAKE_CURRENT_SOURCE_DIR}/../core is already on the include path below, so
# `#include "api/chudware_version.h"` resolves the same way "api/libchudware.h" does.
chudware_version_header("${CMAKE_BINARY_DIR}/generated")
set(CHUDWARE_GENERATED_INCLUDE_DIR "${CMAKE_BINARY_DIR}/generated")

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# Developer checkouts may execute source-backed sidecars and built-in prompt files.
# Release builders set this OFF; production code has no runtime bypass.
option(CHUDWARE_DEVELOPER_BUILD "Enable source-backed developer execution paths" ON)
if(CHUDWARE_DEVELOPER_BUILD)
  add_compile_definitions(CHUDWARE_DEVELOPER_BUILD=1)
endif()

# Link-time optimization is opt-in so developer and gate builds retain their fast links.
# Release packaging enables it explicitly; unsupported toolchains degrade to a warning.
option(CHUDWARE_LTO "Enable interprocedural (link-time) optimization for release artifacts" OFF)
if(CHUDWARE_LTO)
  if(POLICY CMP0069)
    cmake_policy(SET CMP0069 NEW)
    set(CMAKE_POLICY_DEFAULT_CMP0069 NEW)
  endif()
  include(CheckIPOSupported)
  check_ipo_supported(RESULT _chudware_ipo_ok OUTPUT _chudware_ipo_msg LANGUAGES CXX)
  if(_chudware_ipo_ok)
    include("${CMAKE_CURRENT_SOURCE_DIR}/../core/cmake/MsvcLtoPool.cmake")
    message(STATUS "CHUDWARE_LTO: interprocedural optimization ENABLED")
  else()
    message(WARNING "CHUDWARE_LTO requested but IPO unsupported by this toolchain: ${_chudware_ipo_msg}")
  endif()
endif()

if(MSVC)
  add_compile_options(/utf-8 /MP)
endif()

include("${CMAKE_CURRENT_SOURCE_DIR}/../core/cmake/PinnedGitSource.cmake")

# --- Embedded browser backend selection -------------------------------------
# CEF is THE embedded-browser backend since the ScreencastWebView removal
# (2026-07-07); the screencast backend no longer exists and this option no
# longer selects between two implementations.
#
# Default ON: a normal Linux or Windows build carries the vendor sign-in
# browser, and CEF is load-bearing beyond the visible browser tab — a separate
# headless CEF daemon is the fetch primitive behind 100+ Parts Library adapters
# (services/browser/cef_client.py). Requires the CEF SDK under extern/cef/; see
# cmake/cef.cmake and docs/design/cef_webview.md.
#
# Turn OFF only for an SDK-less build. makeWebView then returns NullWebView,
# which paints an explanatory message and reports available() == false; every
# call site already branches on that.
option(CHUDWARE_ENABLE_CEF "Build the in-process CEF off-screen browser backend" ON)

# Web/WASM builds never ship CEF. Force the option off so cmake/cef.cmake is
# never included (the SDK is native-only). Native default stays ON above.
# macOS is the same story: no CEF binary distribution is vendored for Darwin, and
# the Mach-O one is a .app bundle with a helper-process layout this build does not
# produce — so the Mac desktop build gets NullWebView, matching the macos-* presets.
if(EMSCRIPTEN OR APPLE)
  set(CHUDWARE_ENABLE_CEF OFF CACHE BOOL "Build the in-process CEF off-screen browser backend" FORCE)
endif()


# --- GLFW (window + GL context) ---------------------------------------------
# Native: FetchContent glfw 3.4. EMSCRIPTEN: contrib.glfw3 port (link flags below).
if(NOT EMSCRIPTEN)
  set(GLFW_BUILD_DOCS OFF CACHE BOOL "" FORCE)
  set(GLFW_BUILD_TESTS OFF CACHE BOOL "" FORCE)
  set(GLFW_BUILD_EXAMPLES OFF CACHE BOOL "" FORCE)
  set(GLFW_INSTALL OFF CACHE BOOL "" FORCE)
  # Primary commit: https://api.github.com/repos/glfw/glfw/commits/3.4
  chudware_populate_pinned_git(glfw https://github.com/glfw/glfw.git
    7b6aead9fb88b3623e3b3725ebb42670cbe4c579)
  add_subdirectory("${glfw_SOURCE_DIR}" "${glfw_BINARY_DIR}" EXCLUDE_FROM_ALL)
endif()

# --- glad (GL 3.3 core loader) — vendored, pre-generated into extern/glad ----
# Compiled directly into the target; no configure-time fetch/generation.

# --- stb (single-header PNG writer) -----------------------------------------
# Primary master identity: https://api.github.com/repos/nothings/stb/commits/2c980bb59875b0d32144a71867fbdebb2f77cd20
chudware_populate_pinned_git(stb https://github.com/nothings/stb.git
  2c980bb59875b0d32144a71867fbdebb2f77cd20)

# --- nlohmann/json (worker IPC) ---------------------------------------------
set(JSON_BuildTests OFF CACHE INTERNAL "")
# Same authenticated source identity as the core.
chudware_populate_pinned_git(json https://github.com/nlohmann/json.git
  9cca280a4d0ccf0c08f47a99aa71d1b0e52f8d03)
add_subdirectory("${json_SOURCE_DIR}" "${json_BINARY_DIR}" EXCLUDE_FROM_ALL)

# --- Dear ImGui (docking branch) — GUI --------------------------------------
# Primary commit: https://api.github.com/repos/ocornut/imgui/commits/v1.91.6-docking
chudware_populate_pinned_git(imgui https://github.com/ocornut/imgui.git
  947aa9c9722bd6ff740dd757da609ff41f4d3ba3)

# --- miniz (ZIP read for `.step+` component feature lists) ------------------
# The host talks to the worker over IPC, so build its own copy from the same
# pinned 3.0.2 split sources as core (no unauthenticated release amalgamation).
chudware_populate_pinned_git(miniz https://github.com/richgel999/miniz.git
  293d4db1b7d0ffee9756d035b9ac6f7431ef8492)
add_library(chudware_host_miniz STATIC
  ${miniz_SOURCE_DIR}/miniz.c ${miniz_SOURCE_DIR}/miniz_zip.c
  ${miniz_SOURCE_DIR}/miniz_tinfl.c ${miniz_SOURCE_DIR}/miniz_tdef.c)
file(MAKE_DIRECTORY "${miniz_BINARY_DIR}")
file(CONFIGURE OUTPUT "${miniz_BINARY_DIR}/miniz_export.h"
  CONTENT "#pragma once\n#define MINIZ_EXPORT\n" NEWLINE_STYLE LF)
target_include_directories(chudware_host_miniz PUBLIC "${miniz_SOURCE_DIR}" "${miniz_BINARY_DIR}")

# Shared host sources (native + EMSCRIPTEN). Desktop-only / platform TUs are
# stripped or swapped below for the web target.
set(CHUDWARE_CORE_SOURCES
  src/main.cpp
  src/app.cpp
  src/app_commands.cpp   # ribbon command router + family handlers (extracted from app.cpp)
  src/ui/scenarios.cpp   # scripted screenshot/test scenarios (extracted from app.cpp)
  src/core/app_state.cpp
  src/core/jobs.cpp
  src/core/context_ref.cpp  # context-picker chip record + wire/prompt forms (GL-free; unit-tested)
  # --- AI Copilot (one chat impl, two hosts) ---
  src/ui/copilot/ui_common.cpp          # glass/bubble toolkit + shared small-UI helpers
  src/ui/copilot/chat_log.cpp           # message list rendering + stick-to-bottom
  src/ui/copilot/activity_timeline.cpp  # per-turn typed-block activity timeline
  src/ui/copilot/cards.cpp              # plan/clarify/corporate/swarm cards + AI-VC
  src/ui/copilot/feed.cpp               # feedTurnActivity stream-event folding
  src/ui/copilot/agent_overview.cpp     # live sub-agent roster + todo checklist above the composer
  src/ui/copilot/conversations.cpp      # conversation tabs + chat-history persistence
  src/ui/copilot/conv_ids.cpp           # session-id allocation (GL-free; unit-tested)
  src/ui/copilot/agent_text.cpp         # doc-id + atlas-glyph text sanitizers (GL-free; unit-tested)
  src/ui/copilot/text_wrap.cpp          # composer hard-break tracking (GL-free; unit-tested)
  src/ui/copilot/tool_phrase.cpp        # tool-call human phrasing + grouping keys (GL-free; unit-tested)
  src/ui/copilot/stream_rows.cpp        # streamed-delta row routing/order (GL-free; unit-tested)
  src/ui/copilot/ask_answers.cpp        # ask_user question batches: prefill/answers/persist (GL-free; unit-tested)
  src/ui/copilot/magic_keywords.cpp     # magic-keyword matcher (standalone prose words; GL-free; unit-tested)
  src/ui/clipboard_image.cpp            # system clipboard -> PNG on disk (paste-an-image)
  src/ui/copilot/image_lightbox.cpp     # attachment thumbnails + full-size image viewer
  src/ui/copilot/context_estimate.cpp   # transcript token/cost estimate (GL-free; unit-tested)
  src/ui/copilot/settings.cpp           # AI settings sections + model dropdown
  src/ui/copilot/composer.cpp           # chat input + send path + composer chrome
  src/ui/copilot/context_chips.cpp      # context-picker chips: add/resolve, draw, focus in viewport
  src/ui/copilot/panel.cpp              # drawAIPanel host + ai_workspace reuse hooks
  src/ui/settings_modal.cpp
  src/ui/onboarding.cpp       # first-run account/provider/test/workspace wizard
  src/ui/provider_logos.cpp             # provider brand color + badge (Settings -> Providers)
  src/ui/provider_usage.cpp             # shared subscription usage poll (Settings + composer rings)
  src/ui/chudcode_descriptors.cpp       # Chudcode worker payload parsing (GL-free; unit-tested)
  src/ui/chudcode_session_actions.cpp   # Chudcode session recovery actions + link (GL-free; unit-tested)
  src/ui/oauth_capture.cpp  # in-app xAI multi-account CEF OAuth (Settings → Accounts)
  src/ui/web_capabilities.cpp  # WASM-build gap table (ui::webBuild / ui::webGapReason)
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
  ../core/util/aicm_handoff.cpp   # aicm-shm:// map for loadAicmFile
  src/render/id_pass.cpp
  src/render/overlays.cpp
  src/render/doc_thumbnail.cpp      # offscreen shaded DocMesh previews (AI Active Work cards)
  src/render/splat_renderer.cpp     # RE-28: 3DGS splat renderer
  src/render/re_points.cpp          # RE-21: sparse point-cloud + camera-frusta overlay (GL)
  src/render/re_points_fixture.cpp  # RE-21: GL-free fixture builder (also linked by ctest)
  src/render/ref_datum_geom.cpp     # GL-free batched datum overlay geometry (also linked by ctest)
  src/core/selection.cpp
  # --- end C15 ---
  # --- C16: feature tree + doc mirror ---
  src/core/doc_mirror.cpp
  ../services/worker/cpp_core.cpp
  ../core/util/platform_paths.cpp # executablePath(): one readlink/_NSGetExecutablePath/GetModuleFileNameW
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
  src/ui/sketch_relations_panel.cpp   # Display/Delete Relations panel (sketch_relations_panel.h)
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
  src/ui/sim_manager.cpp           # core: state/JSON, refresh/persist, CRUD, command routing
  src/ui/sim/sim_manager_tree.cpp
  src/ui/sim/sim_manager_forms.cpp
  src/ui/sim/sim_manager_run.cpp
  src/ui/sim/sim_manager_results.cpp
  src/ui/sim/sim_manager_viewport.cpp
  src/ui/sim/sim_manager_flow.cpp
  src/ui/sim/sim_manager_motion.cpp
  src/ui/sim/sim_manager_em.cpp
  src/ui/sim/sim_manager_topology.cpp
  src/ui/sim/sim_manager_electronics.cpp
  src/ui/piping_panel.cpp
  src/ui/hydraulic_panel.cpp
  src/ui/sim/hydraulic_canvas.cpp
  src/ui/sim/hydraulic_forms.cpp
  src/ui/sim/sim_scene.cpp         # viewport <-> sim-panel 3D scene seam (dispatcher)
  src/ui/sim/pipe_scene.cpp        # Piping producer: shaded network, picking, draw/insert tools
  src/ui/sim/pipe_sketch.cpp       # 3D pipe line tool: working plane, snapping, rubber band
  src/ui/sim/pipe_mesh.cpp         # pipe/fitting tube tessellation (cached per network revision)
  src/ui/sim/hydraulic_scene.cpp   # Hydraulics producer: 3D components + hoses, placement tool
  src/ui/sim_topology.cpp
  src/ui/sim_topology_bc.cpp
  src/ui/legend.cpp
  src/ui/ui_audit.cpp
  src/ui/evaluate_dialogs.cpp
  # --- end C19 ---
  # --- C20b: assembly UI panels ---
  src/ui/asm_panels.cpp
  # --- end C20b ---
  # --- Equations / Global Variables editor ---
  src/ui/equations_dialog.cpp
  src/ui/profiler_window.cpp   # in-app sampling-profiler window (text report + flamegraph)
  src/ui/command_search.cpp    # unified settings + modeling-tools search popup
  # --- ECAD 2D GUI (schematic / PCB editor) ---
  src/ui/ecad/ecad_mirror.cpp
  src/ui/ecad/ecad_canvas.cpp
  src/ui/ecad/ecad_panel.cpp
  # --- end ECAD ---
  # --- CAM (computer-aided machining) workspace: toolpaths / post / sim ---
  src/ui/cam/cam_mirror.cpp
  src/ui/cam/cam_panel.cpp
  # --- end CAM ---
  # --- Manufacture: device / run / approval operator surfaces (mfg_* lane mirror) ---
  src/ui/manufacture/mfg_mirror.cpp
  src/ui/manufacture/manufacture_panel.cpp
  src/ui/manufacture/device_panel.cpp
  src/ui/manufacture/run_panel.cpp
  src/ui/manufacture/approval_dialog.cpp
  src/ui/manufacture/attention_panel.cpp
  # --- end Manufacture ---
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
  # --- app-wide visual vocabulary + motion language ---
  # kit.cpp is the ONE implementation of card / section header / row / chip / button / progress /
  # status glyph / empty state; the per-workspace helpers forward here. anim.cpp owns the shared
  # easing curves and the continuous-redraw frame request the idle throttle in app.cpp consumes.
  src/ui/anim.cpp
  src/ui/widgets/kit.cpp
  src/ui/widgets/kit_form.cpp
  src/ui/widgets/kit_plot.cpp
  src/ui/widgets/kit_panel.cpp
  src/ui/sim/sim_common.cpp
  src/ui/sim/sim_pump_drive.cpp
  src/ui/tesseract.cpp  # brand mark: live 4D tesseract projection (copilot logo + Appearance preview)
  src/win_chrome.cpp
  src/stb_impl.cpp
  # --- C14: ribbon widgets + command table + icon loader ---
  src/ui/icons.cpp
  src/ui/icons_sdf.cpp  # liquid-glass v2: runtime SDF glyphs + halo (drawIconGlass)
  src/ui/widgets/ribbon.cpp
  src/ui/widgets/web_view_backend.cpp  # IWebViewBackend factory → CEF (or NullWebView without the SDK)
  src/ui/widgets/interwebz.cpp
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
  src/ui/plm/versions_tab.cpp # PLM Versions and history commit DAG
  src/ui/plm/plm_compare.cpp # PLM Compare page: rendered geometry diff of two revisions
  src/ui/plm/plm_quickbar.cpp # embedded per-doc PLM control (status-bar chip + popover)
  src/ui/plm/graph_tab.cpp     # PLM Graph tab (cad_graph_get/blast draw-list)
  src/ui/plm/graph_force.cpp    # PLM Graph force-directed layout engine (headless)
  src/ui/plm/graph_changes.cpp  # PLM Graph "what changed since I last looked" (cad_graph_changes + watch overlay)
  src/ui/plm/graph_thumbs.cpp   # PLM Graph grid-view iso thumbnails (.plm/thumbs cache)
  src/ui/ai/ai_workspace.cpp
  src/ui/ai/agent_dag.cpp        # per-conversation execution trace + canvas
  src/ui/ai/current_run_view.cpp # project-scoped roster + selected-agent inspector
  src/ui/ai/activity_view.cpp    # Activity view: Active Work + Documents composed sections
  src/ui/ai/insights_view.cpp    # Insights view: Leaderboard + Metrics composed sections
  src/ui/ai/active_work.cpp     # Active Work: live grid of agent-modified docs + op tickers
  src/ui/ai/ai_prefs.cpp        # AI workspace UI prefs (verbosity / follow / previews)
  src/ui/ai/metrics_view.cpp    # AI workspace Metrics view (token spend / usage aggregation)
  src/ui/ai/plan_board.cpp      # Interactive Session Plan board + ai_vc timeline/rollback
  src/ui/ai/docs_panel.cpp      # AI-tab document attach / preview / edit panel
  src/ui/ai/rules_authoring.cpp  # Settings->AI->Rules authoring popup (skill/context pack/rule)
  src/ui/reveng/reveng_workspace.cpp  # RE-03 Reverse Engineering workspace shell
  src/ui/firmware/fw_workspace.cpp    # Firmware workspace (projects/build/emulation/trace)
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
# Dear ImGui test-engine item hooks for the UI audit (CHUDWARE_SHOT=simaudit, CHUDWARE_IDAUDIT=1):
# with IMGUI_ENABLE_TEST_ENGINE ImGui reports every submitted item to the hooks src/ui/ui_audit.cpp
# defines. ImGui and the custom item annotation sites get the define (enum values and hook call
# sites, no struct fields). They belong to Chudware, which also compiles ui_audit.cpp.
# Each hook site is one branch on ImGuiContext::TestEngineHookItems, false outside an active audit.
set_property(SOURCE
  ${imgui_SOURCE_DIR}/imgui.cpp
  ${imgui_SOURCE_DIR}/imgui_draw.cpp
  ${imgui_SOURCE_DIR}/imgui_tables.cpp
  ${imgui_SOURCE_DIR}/imgui_widgets.cpp
  src/ui/widgets/kit.cpp
  src/ui/widgets/kit_panel.cpp
  src/ui/widgets/kit_form.cpp
  src/ui/copilot/cards.cpp
  src/ui/sim/sim_manager_forms.cpp
  src/ui/sim/sim_manager_flow.cpp
  src/ui/sim/sim_manager_motion.cpp
  src/ui/sim/sim_manager_electronics.cpp
  src/ui/ui_audit.cpp
  APPEND PROPERTY COMPILE_DEFINITIONS IMGUI_ENABLE_TEST_ENGINE)

if(EMSCRIPTEN)
  # Full app under EMSCRIPTEN: same core list, swap IPC transport, stub bootstrap/
  # discovery/spawn, drop glad + camera (browser has no V4L2/MF).
  set(CHUDWARE_WASM_SOURCES ${CHUDWARE_CORE_SOURCES})
  list(APPEND CHUDWARE_WASM_SOURCES
    src/wasm_stubs.cpp
    ../core/util/tweetnacl.c
    ../core/util/randombytes.cpp
    ../services/ipc/ipc_transport_wasm.cpp
    src/ui/reveng/webcam_capture.cpp   # RE-19 widget (uses camera_stub under web)
    src/platform/camera_stub.cpp)
  # Desktop-only TUs that must not enter the wasm link (if present in core list).
  # camera / glad / native IPC / bootstrap are added only on the native branch.
  add_executable(Chudware ${CHUDWARE_WASM_SOURCES})
else()
  set(CHUDWARE_NATIVE_SOURCES ${CHUDWARE_CORE_SOURCES})
  list(APPEND CHUDWARE_NATIVE_SOURCES
    ../core/util/randombytes.cpp
    ../core/util/tweetnacl.c
    ../core/security/integrity.cpp
    ../core/agent/http.cpp
    src/update/updater.cpp
    src/update/windows_update_helper.cpp
    src/update/apply_linux.cpp
    src/update/apply_windows.cpp
    src/update/apply_macos.cpp
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

if(CHUDWARE_LTO AND _chudware_ipo_ok)
  set_property(TARGET Chudware PROPERTY INTERPROCEDURAL_OPTIMIZATION ON)
endif()


# Embed the Windows application icon + version info into chudware.exe so File
# Explorer and the taskbar show the brand icon instead of the default. The .rc
# references assets/app.ico (generated by assets/make_logo.py).
if(NOT EMSCRIPTEN)
  if(WIN32)
    target_sources(Chudware PRIVATE app.rc)
  endif()
  if(WIN32)
    add_executable(chudware-update-helper WIN32
      src/update/windows_update_helper_main.cpp
      src/update/windows_update_helper.cpp
      ../core/security/integrity.cpp
      ../core/util/randombytes.cpp
      ../core/util/tweetnacl.c)
    target_include_directories(chudware-update-helper PRIVATE
      ${CMAKE_CURRENT_SOURCE_DIR}/src
      ${CMAKE_CURRENT_SOURCE_DIR}/../core)
    target_compile_definitions(chudware-update-helper PRIVATE NOMINMAX WIN32_LEAN_AND_MEAN)
    target_link_libraries(chudware-update-helper PRIVATE
      nlohmann_json::nlohmann_json shell32 ole32 bcrypt crypt32)
    set_target_properties(chudware-update-helper PROPERTIES
      RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
    add_dependencies(Chudware chudware-update-helper)
  endif()

  # Native camera backend — one per OS. camera_stub.cpp is always compiled in (it
  # carries CameraBackend::create() and the synthetic fallback), so macOS, which
  # has no AVFoundation backend, needs no extra TU: create() falls through to the
  # stub there. camera_v4l2.cpp is Linux-only and camera_mf.cpp Windows-only.
  if(WIN32)
    target_sources(Chudware PRIVATE src/platform/camera_mf.cpp)
  elseif(NOT APPLE)
    target_sources(Chudware PRIVATE src/platform/camera_v4l2.cpp)
  endif()
endif()


if(EMSCRIPTEN)
  target_include_directories(Chudware PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/src
    ${CMAKE_CURRENT_SOURCE_DIR}/../core
    ${CHUDWARE_GENERATED_INCLUDE_DIR}   # api/chudware_version.h (product semver)
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
  # Keep expensive runtime diagnostics for debug builds, not the MinSizeRel
  # bundle staged by ./build.sh wasm. Emscripten defaults vary by build type;
  # explicitly turn them off on production links so this never regresses.
  if(CMAKE_BUILD_TYPE STREQUAL "Debug")
    target_link_options(Chudware PRIVATE
      "SHELL:-sASSERTIONS=1"
      "SHELL:-sSTACK_OVERFLOW_CHECK=1")
  else()
    target_link_options(Chudware PRIVATE
      "SHELL:-sASSERTIONS=0"
      "SHELL:-sSTACK_OVERFLOW_CHECK=0")
  endif()
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
    ${CHUDWARE_GENERATED_INCLUDE_DIR}   # api/chudware_version.h (product semver)
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
  if(WIN32)
    target_link_libraries(Chudware PRIVATE winhttp bcrypt crypt32 ws2_32)
  else()
    find_package(CURL REQUIRED)
    target_link_libraries(Chudware PRIVATE CURL::libcurl)
    if(APPLE)
      target_link_libraries(Chudware PRIVATE "-framework CoreFoundation" "-framework Security")
    else()
      find_package(X11 REQUIRED)
      target_link_libraries(Chudware PRIVATE rt X11::X11)  # shm_open + native EWMH window move/resize
    endif()
  endif()
endif()

# CEF off-screen browser backend (opt-in). cmake/cef.cmake finds the CEF binary
# distribution, adds the cef_web_view sources + libcef_dll_wrapper, defines
# CHUDWARE_ENABLE_CEF, and registers the mandatory subprocess-helper target. Kept
# out of the default build entirely (no-op when the option is OFF).
if(CHUDWARE_ENABLE_CEF AND NOT EMSCRIPTEN)
  include(${CMAKE_CURRENT_SOURCE_DIR}/cmake/cef.cmake)
endif()
if(MSVC)
  target_link_options(Chudware PRIVATE $<$<CONFIG:Release>:/DEBUG:NONE>)
elseif(APPLE)
  target_link_options(Chudware PRIVATE $<$<CONFIG:Release>:-Wl,-x>)
else()
  target_link_options(Chudware PRIVATE $<$<CONFIG:Release>:-Wl,--strip-all>)
endif()

if(APPLE AND CMAKE_CXX_COMPILER_ID MATCHES "GNU|Clang")
  target_compile_options(Chudware PRIVATE
    $<$<CONFIG:Release>:-ffunction-sections>
    $<$<CONFIG:Release>:-fdata-sections>)
  target_link_options(Chudware PRIVATE $<$<CONFIG:Release>:-Wl,-dead_strip>)
elseif(CMAKE_SYSTEM_NAME STREQUAL "Linux" AND CMAKE_CXX_COMPILER_ID MATCHES "GNU|Clang")
  target_compile_options(Chudware PRIVATE
    $<$<CONFIG:Release>:-ffunction-sections>
    $<$<CONFIG:Release>:-fdata-sections>)
  target_link_options(Chudware PRIVATE $<$<CONFIG:Release>:-Wl,--gc-sections>)
endif()
if(WIN32)
  target_link_libraries(Chudware PRIVATE dwmapi comdlg32 dbghelp shell32 ole32)  # comdlg32: C41 file dialogs; dbghelp: crash stack/minidump; shell32/ole32: reveal-in-Explorer
  # windows.h min/max macros break CEF headers (std::numeric_limits<>::max()) and
  # any other code that uses std::min/std::max — always define NOMINMAX on the host.
  target_compile_definitions(Chudware PRIVATE NOMINMAX WIN32_LEAN_AND_MEAN)
  # Commercial release binaries carry no PDB/debug directory. This is only a
  # speed bump against casual patching, not an anti-tamper claim.
  if(MSVC)
    target_link_options(Chudware PRIVATE $<$<CONFIG:Release>:/OPT:REF /OPT:ICF>)
    # Real-UI scenarios instantiate enough JSON code to exceed COFF's section limit.
    set_source_files_properties(src/ui/scenarios.cpp PROPERTIES COMPILE_OPTIONS /bigobj)
    # MSVC 14.51 (VS 2026): UCRT <cmath> is missing C99 float math declarations in the
    # global namespace; force-include the shim on C++ TUs only (see extern/fix_math.h).
    # Do NOT quote the path inside /FI — under Ninja+MSVC the quotes become part of the
    # filename (C1083). Ninja quotes the whole /FI<path> token correctly without them.
    target_compile_options(Chudware PRIVATE
      $<$<COMPILE_LANGUAGE:CXX>:/FI${CMAKE_CURRENT_SOURCE_DIR}/extern/fix_math.h>)
  endif()
endif()

if(WIN32)
  add_executable(chudware-uninstall-helper
    src/update/windows_uninstall_helper.cpp
    ../core/security/integrity.cpp
    ../core/util/randombytes.cpp
    ../core/util/tweetnacl.c)
  target_include_directories(chudware-uninstall-helper PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/../core)
  target_compile_definitions(chudware-uninstall-helper PRIVATE NOMINMAX WIN32_LEAN_AND_MEAN)
  target_link_libraries(chudware-uninstall-helper PRIVATE
    nlohmann_json::nlohmann_json bcrypt crypt32)
  set_target_properties(chudware-uninstall-helper PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
endif()

set_target_properties(Chudware PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")

if(NOT EMSCRIPTEN)
enable_testing()

# Authenticated private worker-control framing/endpoint codec (GL-free).
add_executable(test_worker_control tests/test_worker_control.cpp)
target_include_directories(test_worker_control PRIVATE
  ${CMAKE_CURRENT_SOURCE_DIR}/../services/worker)
target_link_libraries(test_worker_control PRIVATE nlohmann_json::nlohmann_json)
set_target_properties(test_worker_control PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_test(NAME test_worker_control COMMAND test_worker_control)

# Online agent_admin_turn routing/result/event contract. jobs.cpp also owns the
# detached launchers; function sections let this GL-free target retain only the
# pure routing helpers under test without linking the desktop/worker runtime.
add_executable(test_agent_job_routing
  tests/test_agent_job_routing.cpp
  src/core/jobs.cpp
  src/core/context_ref.cpp)
target_include_directories(test_agent_job_routing PRIVATE
  ${CMAKE_CURRENT_SOURCE_DIR}/src
  ${CMAKE_CURRENT_SOURCE_DIR}/../core
  ${CMAKE_CURRENT_SOURCE_DIR}/../services/worker
  ${CMAKE_CURRENT_SOURCE_DIR}/../extern/glad/include
  ${imgui_SOURCE_DIR})
target_link_libraries(test_agent_job_routing PRIVATE nlohmann_json::nlohmann_json)
if(MSVC)
  target_compile_options(test_agent_job_routing PRIVATE /Gy)
  target_link_options(test_agent_job_routing PRIVATE /OPT:REF)
elseif(APPLE)
  target_compile_options(test_agent_job_routing PRIVATE -ffunction-sections -fdata-sections)
  target_link_options(test_agent_job_routing PRIVATE -Wl,-dead_strip)
else()
  target_compile_options(test_agent_job_routing PRIVATE -ffunction-sections -fdata-sections)
  target_link_options(test_agent_job_routing PRIVATE -Wl,--gc-sections)
endif()
set_target_properties(test_agent_job_routing PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_test(NAME test_agent_job_routing COMMAND test_agent_job_routing)

add_executable(test_command_capability
  tests/test_command_capability.cpp
  src/ui/ribbon_def.cpp)
target_include_directories(test_command_capability PRIVATE
  ${CMAKE_CURRENT_SOURCE_DIR}/src)
set_target_properties(test_command_capability PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_test(NAME test_command_capability COMMAND test_command_capability)

# --- Signed desktop auto-update (GL-free security + semver contract) ---
add_executable(test_updater
  tests/test_updater.cpp
  src/update/updater.cpp
  ../core/security/integrity.cpp
  src/update/apply_linux.cpp
  src/update/apply_windows.cpp
  src/update/windows_update_helper.cpp
  src/update/apply_macos.cpp
  ../core/util/randombytes.cpp
  ../core/util/tweetnacl.c
  ../core/agent/http.cpp)
target_include_directories(test_updater PRIVATE
  ${CMAKE_CURRENT_SOURCE_DIR}/src
  ${CMAKE_CURRENT_SOURCE_DIR}/../core)
target_link_libraries(test_updater PRIVATE nlohmann_json::nlohmann_json)
if(WIN32)
  target_link_libraries(test_updater PRIVATE winhttp bcrypt crypt32 ws2_32 shell32 ole32)
else()
  target_link_libraries(test_updater PRIVATE CURL::libcurl)
  if(APPLE)
    target_link_libraries(test_updater PRIVATE "-framework CoreFoundation" "-framework Security")
  endif()
endif()
set_target_properties(test_updater PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")

add_executable(test_security
  tests/test_security.cpp
  ../core/security/integrity.cpp
  ../core/util/randombytes.cpp
  ../core/util/tweetnacl.c)
target_include_directories(test_security PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/../core)
target_link_libraries(test_security PRIVATE nlohmann_json::nlohmann_json)
if(WIN32)
  target_link_libraries(test_security PRIVATE bcrypt crypt32)
elseif(APPLE)
  target_link_libraries(test_security PRIVATE "-framework CoreFoundation" "-framework Security")
endif()
set_target_properties(test_security PROPERTIES RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_test(NAME test_security COMMAND test_security)
add_test(NAME test_updater COMMAND test_updater)
if(CHUDWARE_DEVELOPER_BUILD AND NOT WIN32)
  find_package(Python3 COMPONENTS Interpreter REQUIRED)
  add_test(NAME test_updater_https
    COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_updater_https.py
            $<TARGET_FILE:test_updater>)
endif()
# --- end signed desktop auto-update ---

# --- RE-07: CameraBackend platform abstraction (V4L2 / MF / stub) ---
add_executable(test_camera_backend
  src/platform/camera_stub.cpp
  src/stb_impl.cpp          # RE-40: V4L2 MJPEG frames decode via stb_image
  tests/test_camera_backend.cpp)
if(WIN32)
  target_sources(test_camera_backend PRIVATE src/platform/camera_mf.cpp)
elseif(NOT APPLE)
  target_sources(test_camera_backend PRIVATE src/platform/camera_v4l2.cpp)
endif()   # macOS: camera_stub.cpp above is the whole backend set
target_include_directories(test_camera_backend PRIVATE
  ${CMAKE_CURRENT_SOURCE_DIR}/src
  ${CMAKE_CURRENT_SOURCE_DIR}/../core
  ${stb_SOURCE_DIR})
set_target_properties(test_camera_backend PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_test(NAME test_camera_backend
  COMMAND test_camera_backend
  WORKING_DIRECTORY "${CMAKE_BINARY_DIR}")
# Real V4L2 smoke: ctest -R test_camera_backend with CHUDWARE_TEST_CAMERA=1 env set externally
# --- end RE-07 ---

# --- RE-28: 3DGS splat loader + depth-sort test (pure-CPU, no GL context) ---
add_executable(test_splat_renderer
  tests/test_splat_renderer.cpp
  src/render/splat_renderer.cpp
  ${CMAKE_CURRENT_SOURCE_DIR}/../extern/glad/src/gl.c)  # glad fn-ptr globals (GL never invoked here)
target_include_directories(test_splat_renderer PRIVATE
  ${CMAKE_CURRENT_SOURCE_DIR}/src
  ${CMAKE_CURRENT_SOURCE_DIR}/../core
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

# --- batched reference-datum overlay geometry: links the REAL ref_datum_geom.cpp (GL-free).
# Guards the batching rewrite: the test replays the OLD per-primitive upload+draw sequence and
# asserts the batch expands to a byte-identical (mode, color, width, vertex) stream, so a future
# edit to the builder cannot silently move a dash, cone or plane border on screen.
add_executable(test_ref_datum_geom
  src/render/ref_datum_geom.cpp
  tests/test_ref_datum_geom.cpp)
target_include_directories(test_ref_datum_geom PRIVATE
  ${CMAKE_CURRENT_SOURCE_DIR}/src)
set_target_properties(test_ref_datum_geom PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_test(NAME test_ref_datum_geom
  COMMAND test_ref_datum_geom
  WORKING_DIRECTORY "${CMAKE_BINARY_DIR}")
# --- end datum batch ---

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

# --- composer context-picker chips: links the REAL context_ref.cpp (GL-free). The chip record
# is the turn payload's context_refs element and is persisted with drafts and transcripts.
add_executable(test_context_ref
  src/core/context_ref.cpp
  tests/test_context_ref.cpp)
target_include_directories(test_context_ref PRIVATE
  ${CMAKE_CURRENT_SOURCE_DIR}/src)
target_link_libraries(test_context_ref PRIVATE nlohmann_json::nlohmann_json)
set_target_properties(test_context_ref PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_test(NAME test_context_ref
  COMMAND test_context_ref
  WORKING_DIRECTORY "${CMAKE_BINARY_DIR}")

# --- ask_user question card: links the REAL ask_answers.cpp (GL-free). Covers suggested-default
# prefill, the RAW answer JSON per kind sent with the `answer` control action, required gating,
# agent.answer folding and the session-file round trip.
add_executable(test_ask_answers
  src/ui/copilot/ask_answers.cpp
  tests/test_ask_answers.cpp)
target_include_directories(test_ask_answers PRIVATE
  ${CMAKE_CURRENT_SOURCE_DIR}/src)
target_link_libraries(test_ask_answers PRIVATE nlohmann_json::nlohmann_json)
set_target_properties(test_ask_answers PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_test(NAME test_ask_answers
  COMMAND test_ask_answers
  WORKING_DIRECTORY "${CMAKE_BINARY_DIR}")

# --- magic keywords: links the REAL magic_keywords.cpp (GL-free). Covers standalone/punctuation
# hits, embedded-word misses, code-span/fence/markup masking, case sensitivity and the hue sweep.
add_executable(test_magic_keywords
  src/ui/copilot/magic_keywords.cpp
  tests/test_magic_keywords.cpp)
target_include_directories(test_magic_keywords PRIVATE
  ${CMAKE_CURRENT_SOURCE_DIR}/src)
set_target_properties(test_magic_keywords PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_test(NAME test_magic_keywords
  COMMAND test_magic_keywords
  WORKING_DIRECTORY "${CMAKE_BINARY_DIR}")

# --- viewport catch-up after tess-less revisions: header-only core/tess_catchup.h (no GL /
# imgui / AppState). Guards the frozen-assembly bug: asm_mate / asm_add_component / pose
# sweeps publish without a mesh and the active viewport never redrew until the turn ended.
add_executable(test_tess_catchup tests/test_tess_catchup.cpp)
target_include_directories(test_tess_catchup PRIVATE
  ${CMAKE_CURRENT_SOURCE_DIR}/src)
set_target_properties(test_tess_catchup PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_test(NAME test_tess_catchup
  COMMAND test_tess_catchup
  WORKING_DIRECTORY "${CMAKE_BINARY_DIR}")
# --- end viewport catch-up ---

# --- Chudcode worker payload parsing: links the REAL chudcode_descriptors.cpp (no GL /
# imgui / AppState). Guards the sanitized provider-descriptor spellings, the usable-state
# whitelist, streamed chudcode_login open_url normalization, session-stats provenance
# (hasCost vs fabricated $0), tool-state rows, and the /compact chat-replacement guard
# (Chudcode online compaction omits messages — the visible chat must never be wiped).
add_executable(test_chudcode_descriptors
  src/ui/chudcode_descriptors.cpp
  tests/test_chudcode_descriptors.cpp)
target_include_directories(test_chudcode_descriptors PRIVATE
  ${CMAKE_CURRENT_SOURCE_DIR}/src)
target_link_libraries(test_chudcode_descriptors PRIVATE nlohmann_json::nlohmann_json)
set_target_properties(test_chudcode_descriptors PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_test(NAME test_chudcode_descriptors
  COMMAND test_chudcode_descriptors
  WORKING_DIRECTORY "${CMAKE_BINARY_DIR}")
# --- end Chudcode payload parsing ---

# --- Chudcode session recovery: links the REAL chudcode_session_actions.cpp (no GL /
# imgui / AppState). Guards the four contract-frozen worker methods (export / branch /
# repair / close): request params, tolerant response parsers, refusal + session_not_live
# triage, and the per-conversation link's JSON round-trip (id/file/health only — the link
# must never duplicate a transcript).
add_executable(test_chudcode_session_actions
  src/ui/chudcode_session_actions.cpp
  tests/test_chudcode_session_actions.cpp)
target_include_directories(test_chudcode_session_actions PRIVATE
  ${CMAKE_CURRENT_SOURCE_DIR}/src)
target_link_libraries(test_chudcode_session_actions PRIVATE nlohmann_json::nlohmann_json)
set_target_properties(test_chudcode_session_actions PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_test(NAME test_chudcode_session_actions
  COMMAND test_chudcode_session_actions
  WORKING_DIRECTORY "${CMAKE_BINARY_DIR}")
# --- end Chudcode session recovery ---

# --- AsyncRpc streamed-event channel: links the REAL async_rpc.cpp against a stub
# WorkerClient defined inside the test TU (call/abortPending/dtor only), so the queueing
# rules are the shipped ones. Guards: the 4-arg call() stays event-free, streamed events
# deliver ONLY inside drain() on the calling thread (in order, before their Done), and
# shutdown() drops undelivered callbacks.
add_executable(test_async_rpc
  src/core/async_rpc.cpp
  tests/test_async_rpc.cpp)
target_include_directories(test_async_rpc PRIVATE
  ${CMAKE_CURRENT_SOURCE_DIR}/src)
target_link_libraries(test_async_rpc PRIVATE nlohmann_json::nlohmann_json)
find_package(Threads REQUIRED)
target_link_libraries(test_async_rpc PRIVATE Threads::Threads)
set_target_properties(test_async_rpc PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_test(NAME test_async_rpc
  COMMAND test_async_rpc
  WORKING_DIRECTORY "${CMAKE_BINARY_DIR}")
# --- end AsyncRpc streamed events ---

# --- WorkerClient daemon-mode long runs: links the REAL worker_client.cpp + engine_backend.cpp
# + IPC client/transport + async_rpc.cpp against an in-process fake hostd on a private socket.
# Guards the 2026-09-27 starvation: an hours-long agent_admin_turn held callMtx_, so a
# worker-routed call on the GUI's single-FIFO st.rpc lane — and every tab-switch doc_activate
# queued behind it — waited out the whole turn. Also: two long runs still never overlap.
# Developer builds only: the fake hostd is reached through CHUDWARE_HOSTD_SOCKET/_BIN, which a
# production client (CHUDWARE_DEVELOPER_BUILD=OFF, the release legs) deliberately ignores in
# favour of the manifest-owned hostd — there it fails "canonical manifest-owned hostd binary
# unavailable" by design. The strict linux-gate (developer build) keeps running it.
if(UNIX AND CHUDWARE_DEVELOPER_BUILD)
  add_executable(test_worker_long_run
    tests/test_worker_long_run.cpp
    src/core/async_rpc.cpp
    src/bootstrap.cpp
    ../services/worker/worker_client.cpp
    ../services/worker/engine_backend.cpp
    ../services/worker/cpp_core.cpp
    ../services/ipc/ipc_client.cpp
    ../services/ipc/ipc_transport.cpp
    ../services/ipc/ipc_discovery.cpp
    ../core/util/platform_paths.cpp
    ../core/security/integrity.cpp
    ../core/util/randombytes.cpp
    ../core/util/tweetnacl.c)
  target_include_directories(test_worker_long_run PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/src
    ${CMAKE_CURRENT_SOURCE_DIR}/../services/worker
    ${CMAKE_CURRENT_SOURCE_DIR}/../core
    ${miniz_SOURCE_DIR})
  target_link_libraries(test_worker_long_run PRIVATE
    nlohmann_json::nlohmann_json chudware_host_miniz Threads::Threads ${CMAKE_DL_LIBS})
  if(APPLE)
    target_link_libraries(test_worker_long_run PRIVATE
      "-framework CoreFoundation" "-framework Security")
  endif()
  set_target_properties(test_worker_long_run PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
  add_test(NAME test_worker_long_run
    COMMAND test_worker_long_run
    WORKING_DIRECTORY "${CMAKE_BINARY_DIR}")
endif()
# --- end WorkerClient daemon-mode long runs ---

# --- Async tab switch vs the per-frame mirror reconcile: header-only core/switch_hold.h (no
# GL / imgui / AppState). Guards the 2026-09-27 "click a tab, it flashes Loading for a frame,
# then kicks me back to the last part and deselects the tab".
add_executable(test_switch_hold tests/test_switch_hold.cpp)
target_include_directories(test_switch_hold PRIVATE
  ${CMAKE_CURRENT_SOURCE_DIR}/src)
set_target_properties(test_switch_hold PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_test(NAME test_switch_hold
  COMMAND test_switch_hold
  WORKING_DIRECTORY "${CMAKE_BINARY_DIR}")
# --- end async tab switch hold ---

# --- Sketch consumption: header-only core/sketch_consumers.h (no GL / imgui / AppState).
# One rule for the FeatureManager (nest a consumed sketch) and the viewport ghosts (don't draw
# it). Guards the 2026-09-29 "extruded part still shows its sketch / looks unmerged".
add_executable(test_sketch_consumers tests/test_sketch_consumers.cpp)
target_include_directories(test_sketch_consumers PRIVATE
  ${CMAKE_CURRENT_SOURCE_DIR}/src)
target_link_libraries(test_sketch_consumers PRIVATE nlohmann_json::nlohmann_json)
set_target_properties(test_sketch_consumers PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_test(NAME test_sketch_consumers
  COMMAND test_sketch_consumers
  WORKING_DIRECTORY "${CMAKE_BINARY_DIR}")
# --- end sketch consumption ---

# --- CEF metrics spool bound: links the REAL GL/CEF-free filesystem cleanup.
# Exercises both exact Chromium spool directories without loading libcef or a GL context.
add_executable(test_cef_spool_reaper
  src/ui/widgets/cef/cef_spool_reaper.cpp
  tests/test_cef_spool_reaper.cpp)
target_include_directories(test_cef_spool_reaper PRIVATE
  ${CMAKE_CURRENT_SOURCE_DIR}/src)
set_target_properties(test_cef_spool_reaper PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_test(NAME test_cef_spool_reaper
  COMMAND test_cef_spool_reaper
  WORKING_DIRECTORY "${CMAKE_BINARY_DIR}")
# --- end CEF metrics spool bound ---

add_executable(test_external_open
  tests/test_external_open.cpp
  src/ui/file_dialog.cpp
  src/ui/web_capabilities.cpp)
target_include_directories(test_external_open PRIVATE
  ${CMAKE_CURRENT_SOURCE_DIR}/src)
target_link_libraries(test_external_open PRIVATE Threads::Threads)
if(WIN32)
  target_link_libraries(test_external_open PRIVATE shell32 ole32 comdlg32)
endif()
set_target_properties(test_external_open PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_test(NAME test_external_open COMMAND test_external_open)
set_tests_properties(test_external_open PROPERTIES SKIP_RETURN_CODE 77)

add_executable(test_graph_thumbnail
  tests/test_graph_thumbnail.cpp
  src/stb_impl.cpp)
target_include_directories(test_graph_thumbnail PRIVATE
  ${CMAKE_CURRENT_SOURCE_DIR}/src
  ${miniz_SOURCE_DIR}
  ${stb_SOURCE_DIR})
target_link_libraries(test_graph_thumbnail PRIVATE chudware_host_miniz)
set_target_properties(test_graph_thumbnail PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_test(NAME test_graph_thumbnail COMMAND test_graph_thumbnail)

add_executable(test_aicm_bounds
  tests/test_aicm_bounds.cpp
  src/render/aicm_loader.cpp
  ../core/util/aicm_handoff.cpp)
target_include_directories(test_aicm_bounds PRIVATE
  ${CMAKE_CURRENT_SOURCE_DIR}/src
  ${CMAKE_CURRENT_SOURCE_DIR}/../core)
target_link_libraries(test_aicm_bounds PRIVATE Threads::Threads)
if(UNIX AND NOT APPLE)
  target_link_libraries(test_aicm_bounds PRIVATE rt)
endif()
set_target_properties(test_aicm_bounds PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_test(NAME test_aicm_bounds COMMAND test_aicm_bounds
  "${CMAKE_CURRENT_SOURCE_DIR}/../contracts/fixtures/box.aicm")

add_executable(test_cef_trace_bounds tests/test_cef_trace_bounds.cpp)
target_include_directories(test_cef_trace_bounds PRIVATE
  ${CMAKE_CURRENT_SOURCE_DIR}/src
  ${CMAKE_CURRENT_SOURCE_DIR}/../services/ipc)
target_link_libraries(test_cef_trace_bounds PRIVATE Threads::Threads)
if(WIN32)
  target_link_libraries(test_cef_trace_bounds PRIVATE advapi32)
endif()
set_target_properties(test_cef_trace_bounds PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_test(NAME test_cef_trace_disabled COMMAND test_cef_trace_bounds --disabled)
add_test(NAME test_cef_trace_explicit_opt_in COMMAND test_cef_trace_bounds --not-opted-in)
add_test(NAME test_cef_trace_bounds COMMAND test_cef_trace_bounds --enabled)

# --- copilot agent-authored text sanitizers: links the REAL agent_text.cpp (no GL / imgui /
# AppState). Guards two live chat defects: tool-call cards printing the opaque engine doc id
# ("doc-cpp00000039") where the part's name belongs, and U+2192 arrows from the Python harness
# rendering as notdef '?' because the atlas table (src/ui/glyph_ranges.h) never requests the
# arrows block. Both sides read that one header-only table (no source edits needed).
add_executable(test_agent_text
  src/ui/copilot/agent_text.cpp
  tests/test_agent_text.cpp)
target_include_directories(test_agent_text PRIVATE
  ${CMAKE_CURRENT_SOURCE_DIR}/src)
set_target_properties(test_agent_text PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_test(NAME test_agent_text
  COMMAND test_agent_text
  WORKING_DIRECTORY "${CMAKE_BINARY_DIR}")
# --- end agent-authored text sanitizers ---

# --- composer prelude usage-ring selection (GL-free, header-only rule) ---
# Guards the reported "cluttered" prelude row: a windowless lifetime "Total quota" ring used to
# ride between the rolling window ring and the context gauge purely because it came first in
# catalog order. Rolling windows are actionable and win; a total only shows when alone.
add_executable(test_usage_rings
  tests/test_usage_rings.cpp)
target_include_directories(test_usage_rings PRIVATE
  ${CMAKE_CURRENT_SOURCE_DIR}/src
  ${imgui_SOURCE_DIR})
set_target_properties(test_usage_rings PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_test(NAME test_usage_rings
  COMMAND test_usage_rings
  WORKING_DIRECTORY "${CMAKE_BINARY_DIR}")
# --- end composer prelude usage-ring selection ---

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
  ${CMAKE_CURRENT_SOURCE_DIR}/src
  ${CMAKE_CURRENT_SOURCE_DIR}/../core)
set_target_properties(test_clipboard_image PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_test(NAME test_clipboard_image
  COMMAND test_clipboard_image
  WORKING_DIRECTORY "${CMAKE_BINARY_DIR}")
# --- end pasted-image attachment naming ---

# --- transcript context estimate (GL-free) ---
# The context meter has to answer "what will continuing this session cost me?" for a transcript
# that has not been sent yet (a restored session, or a fresh app start), where no provider has
# reported a token count. That is an ESTIMATE, and an estimate that silently drifts from reality
# is worse than none — so the counting rules are pinned here rather than eyeballed in the GUI.
add_executable(test_context_estimate
  src/ui/copilot/context_estimate.cpp
  tests/test_context_estimate.cpp)
target_include_directories(test_context_estimate PRIVATE
  ${CMAKE_CURRENT_SOURCE_DIR}/src)
set_target_properties(test_context_estimate PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_test(NAME test_context_estimate
  COMMAND test_context_estimate
  WORKING_DIRECTORY "${CMAKE_BINARY_DIR}")
# --- end transcript context estimate ---

# --- streamed-delta row routing (GL-free) ---
# A turn is ONE ordered column, so which box a streamed delta joins decides where its text
# lands on screen. Two earlier attempts at "the second thinking block renders above the
# sentence before it" failed because the rules were eyeballed in the running GUI; they are
# pinned here instead, including the counter-pressure (an empty channel flush must NOT split
# one reasoning stream into several boxes).
add_executable(test_stream_rows
  src/ui/copilot/stream_rows.cpp
  tests/test_stream_rows.cpp)
target_include_directories(test_stream_rows PRIVATE
  ${CMAKE_CURRENT_SOURCE_DIR}/src)
set_target_properties(test_stream_rows PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_test(NAME test_stream_rows
  COMMAND test_stream_rows
  WORKING_DIRECTORY "${CMAKE_BINARY_DIR}")
# --- end streamed-delta row routing ---

# --- CAM mirror parsing/timing + simulation-field mesh conversion (GL-free) ---
# Links the real mirror implementation only. These contracts drive viewport overlays and the
# integrated operation editor but require neither a renderer nor a running engine daemon.
add_executable(test_cam_mirror
  src/ui/cam/cam_mirror.cpp
  tests/test_cam_mirror.cpp)
target_include_directories(test_cam_mirror PRIVATE
  ${CMAKE_CURRENT_SOURCE_DIR}/src)
target_link_libraries(test_cam_mirror PRIVATE
  nlohmann_json::nlohmann_json)
set_target_properties(test_cam_mirror PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_test(NAME test_cam_mirror
  COMMAND test_cam_mirror
  WORKING_DIRECTORY "${CMAKE_BINARY_DIR}")
# --- end CAM mirror parsing/timing + simulation-field mesh conversion ---

add_executable(test_sim_topology
  src/ui/sim_topology.cpp
  src/ui/sim_topology_bc.cpp
  tests/test_sim_topology.cpp)
target_compile_definitions(test_sim_topology PRIVATE CHUDWARE_SIM_TOPOLOGY_MODEL_ONLY=1)
target_include_directories(test_sim_topology PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/src)
target_link_libraries(test_sim_topology PRIVATE nlohmann_json::nlohmann_json)
set_target_properties(test_sim_topology PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_test(NAME test_sim_topology COMMAND test_sim_topology
  WORKING_DIRECTORY "${CMAKE_BINARY_DIR}")

# Real-GL picking profiler; hidden context, deterministic geometry, no engine session.
add_executable(profile_viewport
  ../tools/profile_viewport.cpp
  src/render/id_pass.cpp
  ../extern/glad/src/gl.c)
target_include_directories(profile_viewport PRIVATE
  ${CMAKE_CURRENT_SOURCE_DIR}/src
  ${CMAKE_CURRENT_SOURCE_DIR}/src/render
  ${CMAKE_CURRENT_SOURCE_DIR}/../extern/glad/include
  ${stb_SOURCE_DIR})
target_link_libraries(profile_viewport PRIVATE glfw)
set_target_properties(profile_viewport PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")

# Copy brand/font assets next to the exe so the GUI can load them at runtime.
add_custom_command(TARGET Chudware POST_BUILD
  COMMAND ${CMAKE_COMMAND} -E copy_directory
          "${CMAKE_CURRENT_SOURCE_DIR}/../assets"
          "$<TARGET_FILE_DIR:Chudware>/assets"
  COMMENT "Copying assets next to chudware.exe")


endif()  # NOT EMSCRIPTEN (native tests + asset copy)
