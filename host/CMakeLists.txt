cmake_minimum_required(VERSION 3.20)
project(AI_CAD_Host LANGUAGES C CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
if(MSVC)
  add_compile_options(/utf-8 /MP)
endif()

include(FetchContent)

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

add_executable(AI-CAD
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
  src/ui/status_bar.cpp
  src/ui/viewport_panel.cpp
  src/mesh.cpp
  src/renderer.cpp
  # --- C15: AICM loader + ID-buffer picking + overlays + selection ---
  src/render/aicm_loader.cpp
  src/render/id_pass.cpp
  src/render/overlays.cpp
  src/core/selection.cpp
  # --- end C15 ---
  # --- C16: feature tree + doc mirror ---
  src/core/doc_mirror.cpp
  src/ui/feature_tree.cpp
  src/ui/file_dialog.cpp   # C41: native Win32 Save/Open dialogs
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
  # --- end C18 ---
  # --- Configurations panel (SolidWorks ConfigurationManager) ---
  src/ui/config_panel.cpp
  # --- end Configurations ---
  # --- SolidWorks-style tabbed left pane shell ---
  src/ui/left_pane.cpp
  # --- end left pane ---
  # --- Design Table panel (SolidWorks editable config grid) ---
  src/ui/design_table_panel.cpp
  # --- end Design Table ---
  # --- Weldment Cut List panel (SolidWorks Weldment Cut List) ---
  src/ui/cutlist_panel.cpp
  # --- end Cut List ---
  # --- C19: simulation + flow + evaluate UI ---
  src/core/sim_client.cpp
  src/ui/sim_panel.cpp
  src/ui/legend.cpp
  src/ui/evaluate_dialogs.cpp
  # --- end C19 ---
  # --- C20b: assembly UI panels ---
  src/ui/asm_panels.cpp
  # --- end C20b ---
  # --- Equations / Global Variables editor ---
  src/ui/equations_dialog.cpp
  # --- C22b: Activity panel + prompt editor + orchestrator wiring ---
  src/ui/activity_panel.cpp
  # --- end C22b ---
  # --- Wave N: AI-ECAD 2D GUI (schematic / PCB editor) ---
  src/ui/ecad/ecad_mirror.cpp
  src/ui/ecad/ecad_canvas.cpp
  src/ui/ecad/ecad_panel.cpp
  # --- end Wave N ---
  # --- C25: mouse gesture wheel + S shortcut bar ---
  src/ui/mouse_gestures.cpp
  src/ui/camera_anim.cpp   # C25 W2-D: eased camera tween (presets / Normal-To / zoom-to-fit)
  src/ui/shortcut_bar.cpp
  src/ui/view_selector.cpp
  src/ui/section_tool.cpp   # C25 W3-I: SolidWorks-style Section View (clip plane + cap)
  # --- end C25 ---
  src/worker_client.cpp
  src/bootstrap.cpp
  src/ui_theme.cpp
  src/win_chrome.cpp
  src/stb_impl.cpp
  # --- C14: ribbon widgets + command table + icon loader ---
  src/ui/icons.cpp
  src/ui/widgets/ribbon.cpp
  src/ui/ribbon_def.cpp
  # --- end C14 ---
  extern/glad/src/gl.c
  ${imgui_SOURCE_DIR}/imgui.cpp
  ${imgui_SOURCE_DIR}/imgui_draw.cpp
  ${imgui_SOURCE_DIR}/imgui_tables.cpp
  ${imgui_SOURCE_DIR}/imgui_widgets.cpp
  ${imgui_SOURCE_DIR}/backends/imgui_impl_glfw.cpp
  ${imgui_SOURCE_DIR}/backends/imgui_impl_opengl3.cpp)

target_include_directories(AI-CAD PRIVATE
  ${stb_SOURCE_DIR}
  ${CMAKE_CURRENT_SOURCE_DIR}/extern/glad/include
  ${imgui_SOURCE_DIR}
  ${imgui_SOURCE_DIR}/backends)
target_link_libraries(AI-CAD PRIVATE glfw nlohmann_json::nlohmann_json)
if(WIN32)
  target_link_libraries(AI-CAD PRIVATE dwmapi comdlg32)  # comdlg32: C41 file dialogs
endif()

set_target_properties(AI-CAD PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")

# --- C15: optional pure-CPU loader test (no GL/GLFW). Built with the default target set. ---
add_executable(test_aicm
  src/render/aicm_loader.cpp
  src/render/test_aicm_cpp/test_aicm.cpp)
target_include_directories(test_aicm PRIVATE
  ${CMAKE_CURRENT_SOURCE_DIR}/extern/glad/include)
set_target_properties(test_aicm PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
# --- end C15 ---

# Copy brand/font assets next to the exe so the GUI can load them at runtime.
add_custom_command(TARGET AI-CAD POST_BUILD
  COMMAND ${CMAKE_COMMAND} -E copy_directory
          "${CMAKE_CURRENT_SOURCE_DIR}/assets"
          "$<TARGET_FILE_DIR:AI-CAD>/assets"
  COMMENT "Copying assets next to AI-CAD.exe")
