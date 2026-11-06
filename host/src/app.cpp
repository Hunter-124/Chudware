// Chudware desktop GUI — branded, themed, docked, with AI chat + provider
// settings.
//
// run_gui(): GLFW/ImGui init, the dockspace host, the per-frame panel calls
// (title bar, Design, Viewport, Console, status bar, Settings/About modals),
// the worker-event drain + job-completion handling, the headless screenshot
// modes, and teardown. The data model, job launcher, and each panel's draw code
// live in core/* and ui/* (see core/app_state.h). Behavior-preserving split of
// the former gui_app.cpp monolith.
#include "gl_compat.h"
//
#define GLFW_INCLUDE_NONE
#include <GLFW/glfw3.h>
#ifdef __EMSCRIPTEN__
#include <emscripten.h>
#include <emscripten/html5.h>
// Worker stderr often never reaches the page console under PROXY_TO_PTHREAD;
// always mirror the last checkpoint onto #status via the browser main thread.
inline void wasmBootDom(const char *msg) {
  // Always EM_ASM: we run on the browser main thread (no PROXY_TO_PTHREAD).
  // MAIN_THREAD_EM_ASM from the main thread + ASYNCIFY can re-enter and blow
  // the stack.
  EM_ASM(
      {
        var el = document.getElementById('status');
        if (el)
          el.textContent = UTF8ToString($0);
        try {
          if (!window.__chudwareLogs)
            window.__chudwareLogs = [];
          window.__chudwareLogs.push('boot: ' + UTF8ToString($0));
          if (window.__chudwareLogs.length > 400)
            window.__chudwareLogs.shift();
          console.log('boot: ' + UTF8ToString($0));
        } catch (e) {
        }
      },
      msg);
}
// The DOM boot chip is diagnostic scaffolding, not application chrome. Once ImGui has
// painted three healthy frames, retire it so it cannot sit over the native status bar.
// shell.html re-shows the chip for any later fatal/runtime error.
inline void wasmBootReady() {
  EM_ASM({
    var el = document.getElementById('status');
    if (el) el.style.display = 'none';
  });
}
#define WASM_BOOT_LOG(...)                                                     \
  do {                                                                         \
    std::fprintf(stderr, __VA_ARGS__);                                         \
    std::fflush(stderr);                                                       \
    char _wb[256];                                                             \
    std::snprintf(_wb, sizeof(_wb), __VA_ARGS__);                              \
    /* strip trailing newline for DOM */                                       \
    size_t _n = std::strlen(_wb);                                              \
    if (_n && _wb[_n - 1] == '\n')                                             \
      _wb[_n - 1] = 0;                                                         \
    wasmBootDom(_wb);                                                          \
  } while (0)
#else
#define WASM_BOOT_LOG(...)                                                     \
  do {                                                                         \
  } while (0)
#endif

#include "imgui.h"
#include "imgui_impl_glfw.h"
#include "imgui_impl_opengl3.h"
#include "imgui_internal.h"
#include "stb_image.h"
#include "stb_image_write.h"

#include <nlohmann/json.hpp>

#include <algorithm>
#include <cerrno>
#include <cctype>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <filesystem>
#include <fstream>
#include <future>
#include <limits>
#include <map>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <vector>
#ifndef _WIN32
#include <csignal>
#include <unistd.h>
#endif
#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#endif

#include "app_commands.h" // ribbon command router (dispatchCmd family table)
#include "bootstrap.h"
#include "mesh.h"
#include "renderer.h"
#include "render/backdrop_text.h"
#include "render/mesh_reference.h"
#include "render/mesh_reference_data.h"
#include "ui_theme.h"
#include "win_chrome.h"
#include "worker_client.h"

#include "render/id_pass.h"
#include "render/overlays.h"
#include "render/splat_renderer.h" // RE-27: splat GL draw in reveng reconstruct stage
#include "render/doc_thumbnail.h"

#include "core/app_state.h"
#include "core/engine_events.h" // Phase 4: per-frame hostd event drain (daemon mode)
#include "core/fs_backend.h"
#include "core/startup_trace.h"
#include "engine_backend.h" // Phase 4: engine::daemonMode() (services/worker)
#include "platform/kicad_launch.h"
#include "ipc_client.h"     // services/ipc EngineClient (orphan recovery RPCs)
#include "ipc_discovery.h" // H2 shot: spawnDetached/resolveHostdBin for hostd bounce

#ifdef CHUDWARE_ENABLE_CEF
#include "ui/widgets/cef/cef_app.h" // CEF process lifecycle (init/pump/shutdown)
#include "ui/widgets/cef/cef_web_view.h"
#endif
#include "core/cpp_core.h" // sampling-profiler RPCs (prof_start/prof_stop) via libchudware
#include "core/jobs.h"
#include "ui/ai/ai_workspace.h" // AI workspace full-body hub (sessions + chat + settings)
#include "ui/ai/agent_dag.h" // feedAgentDagEvent — per-conversation agent execution trace
#include "ui/ai/active_work.h" // Active Work view live op feed
#include "ui/appearance_common.h" // Appearance tab: MaterialPreset table (kMaterials) for presets
#include "ui/cam/cam_panel.h" // CAM: manufacturing workspace (toolpaths / post / sim)
#include "ui/config_panel.h" // Configurations (SolidWorks ConfigurationManager)
#include "ui/console_panel.h"
#include "ui/omp_session_actions.h" // agent.session -> conversation resume link
#include "ui/copilot/copilot.h"
#include "ui/copilot/activity_settlement.h"
#include "ui/copilot/context_chips.h"  // pumpContextRefFocus — chip click waiting on a tab switch
#include "ui/copilot/context_estimate.h" // per-conversation meter routing
#include "ui/cursors.h"       // W1-B: cursor manager
#include "ui/cutlist_panel.h" // Weldment Cut List (SolidWorks Weldment Cut List)
#include "ui/design_table_panel.h" // Design Table (SolidWorks editable config grid)
#include "ui/doc_tabs.h"
#include "ui/drawing_panel.h" // W17: drawing ribbon command dispatch (drawingHandleCommand)
#include "ui/ecad/ecad_mirror.h" // complete type for EcadMirror::setDocId
#include "ui/ecad/ecad_panel.h" // read-only KiCad project hub
#include "ui/metrics.h"
#include "ui/feature_tree.h"
#include "ui/file_dialog.h"
#include "ui/file_explorer.h" // Wave 3: File Explorer + workspace/recent helpers
#include "ui/firmware/fw_workspace.h" // Firmware workspace (drawFirmwareWorkspace + shot fixture)
#include "ui/manufacture/manufacture_panel.h" // P4 manufacturing operator surface + RPC dispatch
#include "ui/manufacture/mfg_mirror.h" // complete read-only manufacturing mirror type
#include "ui/anim.h" // shared motion language + the continuous-redraw frame request
#include "ui/icons.h"
#include "ui/layout.h" // Wave 1C/1D: buildDefaultLayout + tabbed left pane (drawLeftPane)
#include "ui/property_manager.h"
#include "ui/reveng/dim_review.h" // RE-22 dimension-confirm review + deviation panels
#include "ui/reveng/reveng_internal.h" // RE-22: S() + Stage enum for shot seeding
#include "ui/reveng/reveng_widgets.h" // RE-40: kit::setPreviewSlotPainter (3D preview slots)
#include "ui/reveng/reveng_workspace.h" // Reverse Engineering workspace full-body shell (RE-03)
#include "ui/ribbon_def.h"
#include "ui/settings_modal.h"
#include "ui/sketch3d_mode.h" // 3D free-space sketcher (sketch3d features)
#include "ui/sketch_mode.h"   // C18
#include "ui/status_bar.h"
#include "ui/title_bar.h"
#include "ui/toast.h" // W1-B: toast notifications
#ifndef __EMSCRIPTEN__
#include "update/updater.h"
#include "ui/web_capabilities.h"
#endif
#include "ui/viewport_panel.h"
#include "ui/widgets/ribbon.h"
#include "ui/widgets/kit.h"
// --- C19: simulation + flow + evaluate UI ---
#include "core/sim_client.h"
#include "core/frame_stats.h"
#include "ui/evaluate_dialogs.h"
#include "ui/sim_manager.h"
// --- end C19 ---
// --- C20b: assembly UI ---
#include "ui/asm_panels.h"
// --- end C20b ---
#include "ui/command_search.h" // unified settings + modeling-tools search popup
#include "ui/equations_dialog.h" // Equations / Global Variables editor
#include "ui/scad_editor.h"
#include "ui/profiler_window.h" // in-app sampling-profiler window (text report + flamegraph)
#ifndef __EMSCRIPTEN__
#include "ui/scenarios.h" // native scripted screenshot/test scenarios
#endif
#include "ui/web_panel.h" // "Web" title-bar chip: full-window in-app CEF browser
// [workspace-tabs seam] full-body workspace renderers for PLM and PartsLibrary.
#include "ui/parts/parts_workspace.h"
#include "ui/plm/graph_tab.h" // plmGraphLoaded — PLM graph shot sync
#include "ui/plm/plm_quickbar.h" // ui::plmAutoRecordSave — record agent self-saves in PLM
#include "ui/plm/plm_workspace.h"
#include "ui/provider_logos.h" // per-frame provider brand-icon fetch/upload pump

#include <filesystem> // ecad export output dir + W14: flat-pattern DXF export

#ifndef __EMSCRIPTEN__
// main.cpp — single-instance guard. True exactly once per "raise yourself" poke sent by a second
// launch of this install (SIGUSR1 on POSIX, a named event on Windows).
bool guiConsumeActivationRequest();
// main.cpp — the document that second launch carried, if it was an "Open with" ("" = none).
std::string guiTakeActivationDoc();
#endif


// =====================================================================================
// ECAD GUI command wiring. Creates/routes native ECAD documents through ecad_*
// RPCs and feeds full ops-responses into the editor mirror. Every created or
// loaded document is tabbed before adoption so the response lands in that
// tab's per-document editor state; interactive entity commands arm the canvas
// through ecad_panel's placement seam.
// =====================================================================================
namespace {

void swapFrameBuffers(GLFWwindow *win) {
  core::framestats::Scope timing("swap");
  glfwSwapBuffers(win);
  core::framestats::markSwap(timing.elapsedMs());
}

std::string compactionNotice(const nlohmann::json& compaction) {
  const std::string method = jstr(compaction, "method");
  std::string label;
  if (method == "soft") label = "summary";
  else if (method == "snapcompact") label = "snapcompact";
  else if (method == "remote") label = "provider-native compaction";
  else {
    label = jstr(compaction, "strategy");
    if (label == "auto") label.clear();
  }
  std::string notice = "Context compacted";
  if (!label.empty()) notice += " (" + label + ")";
  const auto before = compaction.find("tokensBefore");
  const auto after = compaction.find("tokensAfter");
  const bool hasBefore = before != compaction.end() && before->is_number_integer() &&
                         before->get<int>() >= 0;
  const bool hasAfter = after != compaction.end() && after->is_number_integer() &&
                        after->get<int>() >= 0;
  auto tokens = [](int count) {
    char buffer[32];
    if (count >= 1000) std::snprintf(buffer, sizeof(buffer), "%.1fk", count / 1000.0);
    else std::snprintf(buffer, sizeof(buffer), "%d", count);
    return std::string(buffer);
  };
  const std::string beforeMark = compaction.value("tokensBeforeEstimated", false) ? "~" : "";
  const std::string afterMark = compaction.value("tokensAfterEstimated", false) ? "~" : "";
  if (hasBefore && hasAfter)
    notice += ": " + beforeMark + tokens(before->get<int>()) + " to " +
              afterMark + tokens(after->get<int>()) + " tokens";
  else if (hasBefore)
    notice += ", was " + beforeMark + tokens(before->get<int>()) + " tokens";
  else if (hasAfter)
    notice += ": " + afterMark + tokens(after->get<int>()) + " tokens";
  return notice + ".";
}


// Appearance ribbon: apply a set_appearance op to the whole model (body "b1")
// and reload the mesh so the new color/material shows immediately.
// color==nullptr clears back to the default grey. A direct apply is safe from
// the ribbon dispatcher (it runs before the FeatureManager tree walk this
// frame, like edit_delete / Flatten). "b1" matches the feature-tree root
// convention; per-body edits stay on the tree / viewport context menu.
void applyModelAppearance(AppState &st, const float *color, float metallic,
                          float roughness) {
  if (!st.docMirror.hasDoc() || !st.workerStarted) {
    st.toast(ui::ToastLevel::Info,
             "Open a part first to change its appearance");
    return;
  }
  nlohmann::json op = {{"op", "set_appearance"}, {"id", "b1"}};
  if (color) {
    op["color"] = {color[0], color[1], color[2]};
    op["metallic"] = metallic;
    op["roughness"] = roughness;
  } else
    op["color"] = nullptr;
  ++st.rebuildsInFlight;
  st.docMirror.applyOpsAsync(
      st.rpc, st.worker, nlohmann::json::array({op}),
      [&st](const core::OpsResponse &resp, bool current) {
        --st.rebuildsInFlight;
        if (!current)
          return;
        if (resp.ok && st.isActiveMirrorDoc() && !resp.aicm_path.empty()) {
          st.loadDocMeshAsync(resp.aicm_path);
        } else if (!resp.ok) {
          st.addLog("[err] set_appearance: " + resp.error_message);
        }
      });
}

// Feed a worker ECAD ops-response into the editor mirror; track doc id +
// revision.
void ecadAdopt(AppState &st, const nlohmann::json &res) {
  const std::string id = jstr(res, "doc_id");
  if (!id.empty())
    st.ecadDocId = id;
  st.ecadRevision = jint(res, "revision");
  if (!st.ecadDocId.empty()) {
    ui::ecadEditorMirror().setDocId(st.ecadDocId);
    for (auto &tab : st.docTabs) {
      if ((tab.kind == AppState::DocTab::TabKind::Schematic ||
           tab.kind == AppState::DocTab::TabKind::PCB) &&
          tab.ecadDocId == st.ecadDocId) {
        tab.ecadRevision = st.ecadRevision;
        if (res.contains("dirty") && res["dirty"].is_boolean())
          tab.ecadSessionDirty = res["dirty"].get<bool>();
        if (res.contains("file_changed") && res["file_changed"].is_boolean())
          tab.ecadFileChanged = res["file_changed"].get<bool>();
        if (res.contains("kicad_locked") && res["kicad_locked"].is_boolean())
          tab.ecadKicadLocked = res["kicad_locked"].get<bool>();
        ui::ecadSetFileState(tab.ecadDocId, tab.path, tab.dirty || tab.ecadSessionDirty,
                             tab.ecadFileChanged, tab.ecadKicadLocked);
        break;
      }
    }
  }
  ui::ecadEditorLoadResponse(res);
}

// Blocking ECAD RPC; toasts on transport or {code,message} error. Returns false
// on failure.
bool ecadCall(AppState &st, const char *method, const nlohmann::json &params,
              nlohmann::json &res) {
  std::string err;
  if (!st.worker.call(method, params, res, err)) {
    const std::string message = std::string("ECAD ") + method + ": " + err;
    st.addLog("[err] " + message);
    st.toast(ui::ToastLevel::Error, message);
    return false;
  }
  if (res.is_object() && res.contains("code")) {
    const std::string message = std::string("ECAD ") + method + ": " + jstr(res, "message");
    st.addLog("[err] " + message);
    st.toast(ui::ToastLevel::Error, message);
    return false;
  }
  return true;
}

std::string ecadResponseKind(const nlohmann::json &res) {
  const std::string kind = jstr(res, "kind");
  if (kind == "pcb" || kind == "sch")
    return kind;
  return {};
}

std::string ecadResponseTitle(const nlohmann::json &res) {
  const std::string name = jstr(res, "name");
  if (!name.empty())
    return name;
  auto tree = res.find("tree");
  return tree != res.end() && tree->is_object() ? jstr(*tree, "name")
                                                : std::string();
}

void ecadReportWarnings(AppState &st, const nlohmann::json &res,
                        const std::string &context) {
  auto it = res.find("warnings");
  if (it == res.end() || it->is_null())
    return;
  std::vector<std::string> warnings;
  if (it->is_array()) {
    for (const auto &warning : *it) {
      if (warning.is_string())
        warnings.push_back(warning.get<std::string>());
      else if (!warning.is_null())
        warnings.push_back(warning.dump());
    }
  } else if (it->is_string()) {
    warnings.push_back(it->get<std::string>());
  } else {
    warnings.push_back(it->dump());
  }
  for (const std::string &warning : warnings) {
    st.toast(ui::ToastLevel::Warn, context + ": " + warning);
    st.addLog("[warn] [ecad] " + context + ": " + warning);
  }
}

bool ecadResponseSucceeded(AppState &st, const nlohmann::json &res,
                           const std::string &context) {
  if (!res.contains("ok") || (res["ok"].is_boolean() && res["ok"].get<bool>()))
    return true;
  std::string reason = jstr(res, "message");
  if (reason.empty())
    reason = jstr(res, "error");
  if (reason.empty() && res.contains("erc_errors") &&
      res["erc_errors"].is_number_integer())
    reason = "design has " + std::to_string(res["erc_errors"].get<int>()) +
             " ERC error(s)";
  if (reason.empty())
    reason = "request failed";
  st.toast(ui::ToastLevel::Warn, context + ": " + reason);
  st.addLog("[warn] [ecad] " + context + ": " + reason);
  return false;
}

std::string ecadActiveKind(const AppState &st) {
  if (st.activeTab >= 0 && st.activeTab < (int)st.docTabs.size()) {
    const auto kind = st.docTabs[st.activeTab].kind;
    if (kind == AppState::DocTab::TabKind::Schematic)
      return "sch";
    if (kind == AppState::DocTab::TabKind::PCB)
      return "pcb";
    return {};
  }
  if (ui::ecadEditorMirror().isSch())
    return "sch";
  if (ui::ecadEditorMirror().isPcb())
    return "pcb";
  return {};
}

bool ecadRequireKind(AppState &st, const char *kind, const char *action) {
  if (st.ecadDocId.empty() || ecadActiveKind(st) != kind) {
    st.toast(ui::ToastLevel::Warn,
             std::string(action) + (std::string(kind) == "sch"
                                        ? " requires an active schematic"
                                        : " requires an active board"));
    return false;
  }
  return true;
}

bool ecadAddResponseTab(AppState &st, const nlohmann::json &res,
                        const std::string &fallbackTitle = {}) {
  const std::string id = jstr(res, "doc_id");
  const std::string kind = ecadResponseKind(res);
  if (id.empty() || kind.empty()) {
    st.toast(ui::ToastLevel::Warn,
             id.empty() ? "ECAD response produced no document"
                        : "ECAD response did not identify its document kind");
    return false;
  }
  const std::string path = jstr(res, "path");
  std::string title = path.empty() ? ecadResponseTitle(res)
                                   : std::filesystem::u8path(path).filename().u8string();
  if (title.empty()) title = fallbackTitle;
  st.addEcadTab(kind, id, jint(res, "revision"), title);
  ui::ecadPrimeActiveApp(st);
  auto &tab = st.docTabs[st.activeTab];
  tab.path = path;
  tab.ecadProjectPath = jstr(res, "project");
  st.syncEcadProjectFiles(tab.ecadProjectPath);
  ecadAdopt(st, res);
  return true;
}

// Run an in-house board operation and refresh its preview.
void ecadDocRpc(AppState &st, const char *method, const char *okMsg) {
  if (st.ecadDocId.empty()) {
    st.toast(ui::ToastLevel::Warn, "Open an ECAD document first");
    return;
  }
  nlohmann::json res;
  const int before = st.ecadRevision;
  if (!ecadCall(st, method, {{"doc_id", st.ecadDocId}}, res))
    return;
  ecadAdopt(st, res);
  if (st.ecadRevision > before)
    st.markEcadTabDirty(st.ecadDocId);
  if (okMsg)
    st.toast(ui::ToastLevel::Info, okMsg);
}

// Apply generated AI-board operations with optimistic revision checking.
bool ecadApplyOps(AppState &st, const nlohmann::json &ops) {
  if (st.ecadDocId.empty() || !ops.is_array() || ops.empty())
    return false;
  nlohmann::json res;
  std::string err;
  nlohmann::json params = {{"doc_id", st.ecadDocId},
                           {"base_revision", st.ecadRevision},
                           {"ops", ops}};
  if (!st.worker.call("ecad_apply_ops", params, res, err)) {
    st.toast(ui::ToastLevel::Error, "ECAD edit failed: " + err);
    return false;
  }
  if (res.is_object() && res.contains("code")) {
    if (jstr(res, "code") == "stale_revision") {
      nlohmann::json tr;
      std::string e2;
      if (st.worker.call("ecad_tree", {{"doc_id", st.ecadDocId}}, tr, e2) &&
          !(tr.is_object() && tr.contains("code")))
        ecadAdopt(st, tr);
      st.toast(ui::ToastLevel::Warn, "ECAD edit dropped (stale) - resynced");
    } else {
      st.toast(ui::ToastLevel::Error, "ECAD: " + jstr(res, "message"));
    }
    return false;
  }
  ecadAdopt(st, res);
  return true;
}


bool ecadExport(AppState &st, const char *kind, const char *requiredDocKind,
                const std::string &directory = {}, bool announce = true) {
  if (!ecadRequireKind(st, requiredDocKind, "Export")) return false;
  std::string dir = directory;
  if (dir.empty()) {
    if (st.activeTab >= 0 && st.activeTab < (int)st.docTabs.size() &&
        !st.docTabs[st.activeTab].path.empty())
      dir = (std::filesystem::u8path(st.docTabs[st.activeTab].path).parent_path() / "exports").u8string();
    else if (!st.workspaceDir.empty())
      dir = (std::filesystem::u8path(st.workspaceDir) / "exports").u8string();
    else {
      st.toast(ui::ToastLevel::Warn, "Choose a working directory before exporting");
      return false;
    }
  }
  nlohmann::json res;
  if (!ecadCall(st, "ecad_export",
                {{"doc_id", st.ecadDocId}, {"kind", kind}, {"path", dir}}, res))
    return false;
  ecadReportWarnings(st, res, std::string("Export ") + kind);

  std::vector<std::string> paths;
  auto written = res.find("paths");
  if (written != res.end() && written->is_array()) {
    for (const auto &path : *written)
      if (path.is_string() && !path.get_ref<const std::string &>().empty())
        paths.push_back(path.get<std::string>());
  }
  if (paths.empty()) {
    st.toast(ui::ToastLevel::Warn, std::string("No ") + kind + " files were written");
    st.addLog("[warn] [ecad] export " + std::string(kind) + " wrote no paths");
    return false;
  }

  std::string list;
  for (const std::string &path : paths) {
    if (!list.empty())
      list += ", ";
    list += path;
    st.addLog("[ecad] export " + std::string(kind) + " wrote " + path);
  }
  if (announce)
    st.toast(ui::ToastLevel::Info, std::string("Exported ") + kind + ": " + list);
  return true;
}

// Save via the same KiCad-aware per-tab path as the File menu.
void ecadSaveDoc(AppState &st) {
  for (int i = 0; i < (int)st.docTabs.size(); ++i) {
    if (!st.ecadDocId.empty() && st.docTabs[i].ecadDocId == st.ecadDocId) {
      ui::saveDocTab(st, i, false);
      return;
    }
  }
  st.toast(ui::ToastLevel::Warn, "Open an electronics document to save");
}

// Poll on the async lane; a clean changed file is reloaded by ecad_tree itself.
void ecadHubTick(AppState &st) {
  if (st.activeWorkspace != AppState::Workspace::Electronics &&
      !st.activeTabIsEcad() && !st.wantEcad) return;
  ui::anim::requestFramesAt(1.0f);
  const char *shotKind = st.wantEcad ? std::getenv("CHUDWARE_ECAD_KIND") : nullptr;
  if (shotKind && std::strcmp(shotKind, "missing") == 0) {
    st.ecadKicadStatus = {{"found", false}};
    st.ecadKicadStatusKnown = true;
    ui::ecadSetKicadStatus(st.ecadKicadStatus);
    return;
  }
  if (!st.workerStarted) return;
  static bool statusPending = false;
  static std::chrono::steady_clock::time_point nextStatus{};
  const auto now = std::chrono::steady_clock::now();
  if (!st.ecadKicadStatusKnown && !statusPending && now >= nextStatus) {
    statusPending = true;
    st.rpc.call("ecad_kicad_status", nlohmann::json::object(),
                [&st](const core::RpcResult &r) {
      statusPending = false;
      if (!r.ok || !r.result.is_object() || r.result.contains("code")) {
        st.addLog("[ecad] KiCad status failed: " +
                  (r.err.empty() ? jstr(r.result, "message") : r.err));
        nextStatus = std::chrono::steady_clock::now() + std::chrono::seconds(2);
        return;
      }
      st.ecadKicadStatus = r.result;
      st.ecadKicadStatusKnown = true;
      ui::ecadSetKicadStatus(st.ecadKicadStatus);
    }, 15000);
  }
  if (st.activeTab < 0 || st.activeTab >= (int)st.docTabs.size()) return;
  const auto &active = st.docTabs[st.activeTab];
  if (active.ecadDocId.empty()) return;
  static bool pending = false;
  static std::chrono::steady_clock::time_point next{};
  const auto pollNow = std::chrono::steady_clock::now();
  if (pending || pollNow < next) return;
  pending = true;
  next = pollNow + std::chrono::seconds(1);
  const std::string id = active.ecadDocId;
  st.rpc.call("ecad_tree", {{"doc_id", id}}, [&st, id](const core::RpcResult &r) {
    pending = false;
    if (!r.ok || !r.result.is_object() || r.result.contains("code")) {
      if (!r.err.empty()) st.addLog("[ecad] live sync: " + r.err);
      return;
    }
    for (auto &tab : st.docTabs) {
      if (tab.ecadDocId != id) continue;
      const int revision = r.result.value("revision", -1);
      if (revision >= 0 && revision < tab.ecadRevision) return;
      const bool revisionChanged = revision >= 0 && revision != tab.ecadRevision;
      if (revision >= 0) tab.ecadRevision = revision;
      tab.ecadSessionDirty = r.result.value("dirty", tab.ecadSessionDirty);
      tab.ecadFileChanged = r.result.value("file_changed", false);
      tab.ecadKicadLocked = r.result.value("kicad_locked", false);
      if (r.result.contains("path") && r.result["path"].is_string()) {
        const std::string path = r.result["path"].get<std::string>();
        if (path != tab.path) {
          tab.path = path;
          if (!path.empty()) tab.title = std::filesystem::u8path(path).filename().u8string();
        }
      }
      if (r.result.contains("project") && r.result["project"].is_string()) {
        const std::string project = r.result["project"].get<std::string>();
        if (project != tab.ecadProjectPath) {
          tab.ecadProjectPath = project;
          tab.ecadProjectSchPath.clear();
          tab.ecadProjectPcbPath.clear();
        }
      }
      st.syncEcadProjectFiles(tab.ecadProjectPath);
      ui::ecadSetFileState(id, tab.path, tab.dirty || tab.ecadSessionDirty,
                           tab.ecadFileChanged, tab.ecadKicadLocked);
      if (st.activeTab >= 0 && st.activeTab < (int)st.docTabs.size() &&
          st.docTabs[st.activeTab].ecadDocId == id) {
        st.ecadRevision = tab.ecadRevision;
        if ((revisionChanged || (tab.ecadFileChanged && !tab.ecadSessionDirty)) &&
            r.result.contains("tree"))
          ui::ecadEditorLoadResponse(r.result);
      }
      return;
    }
  });
}

#ifndef __EMSCRIPTEN__
namespace {
struct EcadRecoveryState {
  std::string session = std::to_string(
      std::chrono::system_clock::now().time_since_epoch().count());
  std::map<std::string, int> committed;
  std::map<std::string, std::filesystem::path> snapshots;
  std::map<std::string, std::filesystem::path> sources;
  std::map<std::string, int> saved;
  std::future<int> write;
  std::string writingDoc;
  std::filesystem::path writingPath;
  std::chrono::steady_clock::time_point next{};
  bool recovered = false;
};

EcadRecoveryState &ecadRecoveryState() {
  static EcadRecoveryState state;
  return state;
}

unsigned long ecadProcessId() {
#ifdef _WIN32
  return GetCurrentProcessId();
#else
  return static_cast<unsigned long>(getpid());
#endif
}

std::filesystem::path ecadRecoveryDir() {
  if (const char *p = std::getenv("CHUDWARE_ECAD_AUTOSAVE_DIR"); p && *p)
    return p;
  const char *base = std::getenv("APPDATA");
  return (base && *base ? std::filesystem::path(base)
                        : std::filesystem::temp_directory_path()) /
         "Chudware" / "ecad_autosave";
}


bool ecadProcessAlive(unsigned long pid) {
  if (!pid) return false;
#ifdef _WIN32
  HANDLE process = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, pid);
  if (!process) return GetLastError() == ERROR_ACCESS_DENIED;
  DWORD code = 0;
  const bool alive = GetExitCodeProcess(process, &code) && code == STILL_ACTIVE;
  CloseHandle(process);
  return alive;
#else
  return kill(static_cast<pid_t>(pid), 0) == 0 || errno == EPERM;
#endif
}
void ecadRemoveRecovery(const std::filesystem::path &snapshot, std::error_code &ec) {
  std::filesystem::remove(snapshot, ec);
  if (ec) return;
  auto sidecar = snapshot;
  sidecar += ".recovery.json";
  std::filesystem::remove(sidecar, ec);
}

void ecadRecoverySavedImpl(const std::string &docId, int savedRevision) {
  auto &state = ecadRecoveryState();
  auto saved = state.saved.find(docId);
  if (saved == state.saved.end()) state.saved[docId] = savedRevision;
  else saved->second = std::max(saved->second, savedRevision);
  auto snapshot = state.snapshots.find(docId);
  auto revision = state.committed.find(docId);
  if (!(state.write.valid() && state.writingDoc == docId) &&
      snapshot != state.snapshots.end() && revision != state.committed.end() &&
      revision->second <= savedRevision) {
    std::error_code ec;
    ecadRemoveRecovery(snapshot->second, ec);
    if (!ec) {
      state.snapshots.erase(snapshot);
      state.committed.erase(revision);
    }
  }
  auto source = state.sources.find(docId);
  if (source != state.sources.end() && savedRevision >= 0) {
    std::error_code ec;
    ecadRemoveRecovery(source->second, ec);
    if (!ec) state.sources.erase(source);
  }
}


void ecadRecoveryTick(AppState &st) {
  auto &state = ecadRecoveryState();
  if (!st.workerStarted || (engine::daemonMode() && !st.daemonEverConnected)) return;
  if (!state.recovered) {
    state.recovered = true;
    std::error_code ec;
    const auto dir = ecadRecoveryDir();
    if (std::filesystem::exists(dir, ec)) {
      struct Candidate { int revision; std::string snapshot, name; };
      std::map<std::pair<unsigned long, std::string>, std::vector<Candidate>> candidates;
      for (std::filesystem::directory_iterator it(dir, ec), end; !ec && it != end; it.increment(ec)) {
        nlohmann::json payload;
        const std::string sidecar = it->path().u8string();
        const std::string suffix = ".recovery.json";
        if (sidecar.size() <= suffix.size() ||
            sidecar.compare(sidecar.size() - suffix.size(), suffix.size(), suffix) != 0)
          continue;
        const std::string snapshot = sidecar.substr(0, sidecar.size() - suffix.size());
        const auto ext = std::filesystem::u8path(snapshot).extension().u8string();
        if (ext != ".kicad_sch" && ext != ".kicad_pcb") continue;
        try {
          std::ifstream input(it->path());
          input >> payload;
          if (!payload.is_object() || !payload.value("human_dirty", false) ||
              ecadProcessAlive(payload.value("pid", 0UL)) ||
              payload.value("snapshot", std::string()) != snapshot) continue;
          const auto key = std::make_pair(payload.value("pid", 0UL),
                                          payload.value("doc_id", std::string()));
          if (key.second.empty()) continue;
          candidates[key].push_back({payload.value("revision", -1), snapshot,
                                      payload.value("name", std::string("Recovered ECAD"))});
        } catch (...) { continue; }
      }
      for (auto &entry : candidates) {
        auto &snapshots = entry.second;
        std::sort(snapshots.begin(), snapshots.end(), [](const Candidate &a, const Candidate &b) {
          return a.revision != b.revision ? a.revision > b.revision : a.snapshot > b.snapshot;
        });
        for (size_t i = 0; i < snapshots.size(); ++i) {
          nlohmann::json opened;
          std::string error;
          if (!st.worker.call("ecad_open", {{"path", snapshots[i].snapshot}}, opened, error) ||
              !opened.is_object() || opened.contains("code")) {
            st.addLog("[warn] [ecad] recovery could not open " + snapshots[i].snapshot +
                      ": " + (error.empty() ? jstr(opened, "message") : error));
            continue;
          }
          if (!ecadAddResponseTab(st, opened, snapshots[i].name)) continue;
          auto &tab = st.docTabs[st.activeTab];
          if (!tab.path.empty()) tab.title = std::filesystem::u8path(tab.path).filename().u8string();
          tab.ecadSessionDirty = true;
          st.markEcadTabDirty(tab.ecadDocId);
          ui::ecadSetFileState(tab.ecadDocId, tab.path, true,
                               tab.ecadFileChanged, tab.ecadKicadLocked);
          state.sources[tab.ecadDocId] = snapshots[i].snapshot;
          for (size_t older = i + 1; older < snapshots.size(); ++older) {
            std::error_code removeError;
            ecadRemoveRecovery(snapshots[older].snapshot, removeError);
          }
          break;
        }
      }
    }
  }
  if (state.write.valid()) {
    if (state.write.wait_for(std::chrono::seconds(0)) != std::future_status::ready) return;
    const int revision = state.write.get();
    if (revision >= 0) {
      auto previous = state.snapshots.find(state.writingDoc);
      if (previous != state.snapshots.end() && previous->second != state.writingPath) {
        std::error_code ec;
        ecadRemoveRecovery(previous->second, ec);
      }
      state.committed[state.writingDoc] = revision;
      state.snapshots[state.writingDoc] = state.writingPath;
      auto old = state.sources.find(state.writingDoc);
      if (old != state.sources.end() && old->second != state.writingPath) {
        std::error_code ec;
        ecadRemoveRecovery(old->second, ec);
        if (!ec) state.sources.erase(old);
      }
      auto saved = state.saved.find(state.writingDoc);
      if (saved != state.saved.end())
        ecadRecoverySavedImpl(state.writingDoc, saved->second);
    } else {
      std::error_code ec;
      ecadRemoveRecovery(state.writingPath, ec);
      st.addLog("[err] ECAD recovery snapshot failed for " + state.writingDoc);
    }
  }
  const auto now = std::chrono::steady_clock::now();
  if (now < state.next) return;
  state.next = now + std::chrono::seconds(2);
  for (const auto &tab : st.docTabs) {
    if (tab.kind != AppState::DocTab::TabKind::Schematic &&
        tab.kind != AppState::DocTab::TabKind::PCB) continue;
    if (!tab.dirty) {
      for (auto *files : {&state.snapshots, &state.sources}) {
        auto it = files->find(tab.ecadDocId);
        if (it != files->end()) {
          std::error_code ec;
          ecadRemoveRecovery(it->second, ec);
          if (!ec) files->erase(it);
        }
      }
      state.committed.erase(tab.ecadDocId);
      continue;
    }
    const int rev = tab.ecadRevision;
    if (rev < 0) continue;
    auto committed = state.committed.find(tab.ecadDocId);
    if (committed != state.committed.end() && committed->second == rev) continue;
    const auto path = ecadRecoveryDir() /
        ("gui-" + std::to_string(ecadProcessId()) + "-" + state.session + "-" +
         tab.ecadDocId + "-" + std::to_string(rev) + "-" +
         std::to_string(now.time_since_epoch().count()) +
         (tab.kind == AppState::DocTab::TabKind::Schematic ? ".kicad_sch" : ".kicad_pcb"));
    nlohmann::json metadata = {{"pid", ecadProcessId()}, {"doc_id", tab.ecadDocId},
                               {"name", tab.title}, {"path", tab.path},
                               {"recovery_dir", ecadRecoveryDir().u8string()}};
    state.writingDoc = tab.ecadDocId;
    state.writingPath = path;
    auto *worker = &st.worker;
    const std::string docId = tab.ecadDocId;
    state.write = std::async(std::launch::async, [worker, docId, path, metadata]() {
      nlohmann::json result;
      std::string err;
      if (!worker->call("ecad_save", {{"doc_id", docId}, {"path", path.u8string()},
                                      {"recovery", metadata}}, result, err) ||
          result.contains("code"))
        return -1;
      return result.value("saved_revision", -1);
    });
    break;
  }
}

void ecadRecoveryShutdown(const AppState &st) {
  auto &state = ecadRecoveryState();
  if (state.write.valid()) {
    const int revision = state.write.get();
    if (revision >= 0) {
      auto previous = state.snapshots.find(state.writingDoc);
      if (previous != state.snapshots.end() && previous->second != state.writingPath) {
        std::error_code ec;
        ecadRemoveRecovery(previous->second, ec);
      }
      state.committed[state.writingDoc] = revision;
      state.snapshots[state.writingDoc] = state.writingPath;
      auto old = state.sources.find(state.writingDoc);
      if (old != state.sources.end() && old->second != state.writingPath) {
        std::error_code ec;
        ecadRemoveRecovery(old->second, ec);
        if (!ec) state.sources.erase(old);
      }
      auto saved = state.saved.find(state.writingDoc);
      if (saved != state.saved.end())
        ecadRecoverySavedImpl(state.writingDoc, saved->second);
    }
  }
  for (const auto &tab : st.docTabs) {
    if ((tab.kind == AppState::DocTab::TabKind::Schematic ||
         tab.kind == AppState::DocTab::TabKind::PCB) && !tab.dirty) {
      auto saved = state.saved.find(tab.ecadDocId);
      if (saved != state.saved.end())
        ecadRecoverySavedImpl(tab.ecadDocId, saved->second);
    }
  }
}
} // namespace
#endif

bool ecadAddProject(AppState &st, const nlohmann::json &res) {
  const std::string project = jstr(res, "project");
  if (project.empty()) {
    st.toast(ui::ToastLevel::Error, "KiCad project response has no project path");
    return false;
  }
  bool opened = false;
  for (const char *kind : {"sch", "pcb"}) {
    auto it = res.find(kind);
    if (it == res.end() || !it->is_object()) continue;
    nlohmann::json doc = *it;
    if (jstr(doc, "path").empty()) {
      auto path = std::filesystem::u8path(project);
      path.replace_extension(std::string(kind) == "sch" ? ".kicad_sch" : ".kicad_pcb");
      doc["path"] = path.u8string();
    }
    const std::string file = jstr(doc, "path");
    int existing = -1;
    for (int i = 0; i < (int)st.docTabs.size(); ++i)
      if (!st.docTabs[i].ecadDocId.empty() &&
          std::filesystem::u8path(st.docTabs[i].path).lexically_normal() ==
              std::filesystem::u8path(file).lexically_normal()) {
        existing = i;
        break;
      }
    if (existing >= 0) {
      const std::string redundantId = jstr(doc, "doc_id");
      if (!redundantId.empty() && redundantId != st.docTabs[existing].ecadDocId)
        st.rpc.call("ecad_close", {{"doc_id", redundantId}}, [](const core::RpcResult &) {});
      st.docTabs[existing].ecadProjectPath = project;
      st.switchToTab(existing);
      opened = true;
    } else if (ecadAddResponseTab(st, doc)) {
      st.docTabs[st.activeTab].ecadProjectPath = project;
      opened = true;
    }
  }
  st.syncEcadProjectFiles(project);
  if (opened) st.activeWorkspace = AppState::Workspace::Electronics;
  if (!opened) st.toast(ui::ToastLevel::Warn, "The KiCad project contains no schematic or board");
  return opened;
}

void ecadOpenPath(AppState &st, const std::string &path) {
  if (path.empty()) return;
  std::string ext = std::filesystem::u8path(path).extension().u8string();
  std::transform(ext.begin(), ext.end(), ext.begin(),
                 [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
  if (ext != ".kicad_pro") {
    for (int i = 0; i < (int)st.docTabs.size(); ++i)
      if (!st.docTabs[i].ecadDocId.empty() && !st.docTabs[i].path.empty() &&
          std::filesystem::u8path(st.docTabs[i].path).lexically_normal() ==
              std::filesystem::u8path(path).lexically_normal()) {
        st.switchToTab(i);
        st.activeWorkspace = AppState::Workspace::Electronics;
        st.wantFocusEcad = 6;
        st.pushRecentFile(path);
        return;
      }
  }
  nlohmann::json res;
  if (!ecadCall(st, ext == ".kicad_pro" ? "ecad_open_project" : "ecad_open",
                {{"path", path}}, res)) return;
  bool opened = false;
  if (ext == ".kicad_pro") {
    opened = ecadAddProject(st, res);
  } else {
    ecadReportWarnings(st, res, "Open");
    if (ecadResponseSucceeded(st, res, "Open"))
      opened = ecadAddResponseTab(st, res, std::filesystem::u8path(path).filename().u8string());
  }
  if (!opened) return;
  st.wantFocusEcad = 6;
  st.toast(ui::ToastLevel::Info, "Opened " + path);
  st.addLog("[ecad] opened " + path);
  st.pushRecentFile(path);
}

void ecadOpenDoc(AppState &st) {
  const std::string path = ui::openFileDialog(
      "KiCad and legacy electronics files\0*.kicad_pro;*.kicad_sch;*.kicad_pcb;*.ecad+;*.ecad;*.json\0"
      "KiCad project (*.kicad_pro)\0*.kicad_pro\0"
      "KiCad schematic (*.kicad_sch)\0*.kicad_sch\0"
      "KiCad board (*.kicad_pcb)\0*.kicad_pcb\0"
      "All files (*.*)\0*.*\0");
  if (!path.empty()) ecadOpenPath(st, path);
}

struct Ecad3dLinks {
  std::map<std::string, std::string> partByBoard;
};

Ecad3dLinks &ecad3dLinks() {
  static Ecad3dLinks links;
  return links;
}

std::string ecadSafeStem(const std::string &value, const char *fallback) {
  std::string stem;
  stem.reserve(value.size());
  for (unsigned char c : value) {
    if (c == ' ')
      stem.push_back('_');
    else if (std::isalnum(c) || c == '_' || c == '-')
      stem.push_back(static_cast<char>(c));
  }
  return stem.empty() ? std::string(fallback) : stem;
}

int cadTabForDoc(const AppState &st, const std::string &docId) {
  for (int i = 0; i < (int)st.docTabs.size(); ++i) {
    const auto &tab = st.docTabs[i];
    if ((tab.kind == AppState::DocTab::TabKind::Part ||
         tab.kind == AppState::DocTab::TabKind::Assembly) &&
        tab.docId == docId)
      return i;
  }
  return -1;
}

bool ecadBoardTabOpen(const AppState &st, const std::string &docId) {
  return std::any_of(st.docTabs.begin(), st.docTabs.end(),
                     [&](const AppState::DocTab &tab) {
                       return tab.kind == AppState::DocTab::TabKind::PCB &&
                              tab.ecadDocId == docId;
                     });
}

std::string ecadBoardPartStem(const AppState &st,
                              const std::string &boardDocId) {
  std::string title = ui::ecadEditorMirror().name();
  for (const auto &tab : st.docTabs) {
    if (tab.kind == AppState::DocTab::TabKind::PCB &&
        tab.ecadDocId == boardDocId) {
      title = tab.displayTitle();
      break;
    }
  }
  std::string tag = ecadSafeStem(boardDocId, "board");
  if (tag.size() > 10)
    tag.erase(0, tag.size() - 10);
  return ecadSafeStem(title, "Board") + "_PCB_" + tag;
}

// The headline integration: export the current board to a board-specific STEP,
// then activate the CAD part document minted (or updated) by ecad_to_3d.
void ecadTo3d(AppState &st) {
  if (!ecadRequireKind(st, "pcb", "Board to 3D"))
    return;
  if (st.activeTab < 0 || st.activeTab >= (int)st.docTabs.size()) return;
  if (st.docTabs[st.activeTab].path.empty() &&
      ui::saveDocTab(st, st.activeTab, false) != ui::TabSaveResult::Saved)
    return;

  const std::string boardDocId = st.ecadDocId;
  auto &links = ecad3dLinks().partByBoard;
  for (auto it = links.begin(); it != links.end();) {
    if (!ecadBoardTabOpen(st, it->first))
      it = links.erase(it);
    else
      ++it;
  }

  std::string intoDocId;
  int partTab = -1;
  auto linked = links.find(boardDocId);
  if (linked != links.end()) {
    partTab = cadTabForDoc(st, linked->second);
    if (partTab >= 0)
      intoDocId = linked->second;
    else
      links.erase(linked); // the user closed the 3D tab: mint afresh
  }

  const std::string partName = ecadBoardPartStem(st, boardDocId);
  nlohmann::json params = {{"doc_id", boardDocId}};
  if (!intoDocId.empty()) params["into_doc_id"] = intoDocId;
  nlohmann::json res;
  if (!ecadCall(st, "ecad_to_3d", params, res)) return;
  const std::string stepPath = jstr(res, "step_path");
  const std::string partDocId = jstr(res, "part_doc_id");
  if (stepPath.empty()) {
    st.toast(ui::ToastLevel::Error, "KiCad did not return the board STEP path");
    return;
  }
  if (partDocId.empty()) {
    links.erase(boardDocId);
    ecadReportWarnings(st, res, "Board to 3D");
    if (!res.contains("warnings") || res["warnings"].empty())
      st.toast(ui::ToastLevel::Warn,
               "Board STEP written to " + stepPath +
                   ", but opening it as a CAD part failed");
    st.addLog("[warn] [ecad] board STEP written but CAD import failed: " +
              stepPath);
    return;
  }

  links[boardDocId] = partDocId;
  partTab = cadTabForDoc(st, partDocId);
  if (partTab >= 0)
    st.switchToTab(partTab);
  else
    st.clearViewportForNewDoc();

  std::string aicmPath;
  std::string err;
  const bool activated =
      st.docMirror.activate(st.worker, partDocId, aicmPath, err);
  if (!activated) {
    links.erase(boardDocId);
    st.toast(ui::ToastLevel::Warn,
             "Board STEP written to " + stepPath +
                 ", but the imported CAD part could not be activated: " + err);
    st.addLog("[warn] [ecad] board part activation failed: " + err);
    return;
  }

  if (partTab < 0) {
    st.addDocTab(/*isAssembly=*/false);
    partTab = st.activeTab;
  }
  if (partTab >= 0 && partTab < (int)st.docTabs.size())
    st.docTabs[partTab].title = partName;

  std::string loadError;
  if (aicmPath.empty() || !st.loadDocMesh(aicmPath, loadError)) {
    const std::string reason =
        aicmPath.empty() ? std::string("the part returned no mesh")
                         : loadError;
    st.toast(ui::ToastLevel::Warn,
             "Board imported, but its 3D preview could not be loaded: " +
                 reason);
    st.addLog("[warn] [ecad] board part mesh load failed: " + reason);
    return;
  }

  ecadReportWarnings(st, res, "Board to 3D");
  st.wantFocusViewport =
      6; // surface the 3D view (force for a few frames so it sticks)
  st.toast(ui::ToastLevel::Info,
           "Board imported into " + partName +
               " - design the housing around it");
  st.addLog("[ecad] board " + boardDocId + " -> 3D part " + partDocId +
            ": " + stepPath);
}


bool ecadPersistGenerated(AppState &st, const std::string &docId,
                          const std::string &title) {
  for (auto &tab : st.docTabs) {
    if (tab.ecadDocId != docId)
      continue;
    if (tab.dirty) {
      st.toast(ui::ToastLevel::Error, "Cannot automatically save human ECAD edits");
      return false;
    }
    auto path = std::filesystem::u8path(tab.path);
    if (path.empty()) {
      if (st.workspaceDir.empty()) {
        st.toast(ui::ToastLevel::Error, "Choose a working directory for the KiCad project");
        return false;
      }
      std::string error;
      const std::string base = ecadSafeStem(title, "ECAD");
      std::string stem = base;
      std::filesystem::path dir = std::filesystem::u8path(st.workspaceDir) / stem;
      for (unsigned n = 2;; ++n) {
        const auto info = chudware::fsBackend().stat(dir.u8string(), error);
        if (!error.empty()) break;
        if (!info.exists) break;
        stem = base + "_" + std::to_string(n);
        dir = std::filesystem::u8path(st.workspaceDir) / stem;
      }
      if (!error.empty() || !chudware::fsBackend().mkdir(dir.u8string(), error)) {
        st.toast(ui::ToastLevel::Error, "Cannot create KiCad project directory: " + error);
        return false;
      }
      path = dir / (stem + (tab.kind == AppState::DocTab::TabKind::Schematic
                               ? ".kicad_sch" : ".kicad_pcb"));
    }
    nlohmann::json saved;
    if (!ecadCall(st, "ecad_save", {{"doc_id", docId}, {"path", path.u8string()}}, saved))
      return false;
    tab.path = saved.value("path", path.u8string());
    tab.title = std::filesystem::u8path(tab.path).filename().u8string();
    auto project = std::filesystem::u8path(tab.path);
    project.replace_extension(".kicad_pro");
    tab.ecadProjectPath = project.u8string();
    st.syncEcadProjectFiles(tab.ecadProjectPath);
    tab.ecadSessionDirty = false;
    tab.ecadFileChanged = false;
    tab.ecadKicadLocked = false;
    ui::ecadSetFileState(docId, tab.path, tab.dirty, false, false);
    st.addLog("[ecad] AI work saved: " + tab.path);
    return true;
  }
  return false;
}

// AI-design an ECAD schematic from a natural-language request (the launchpad /
// "AI Design" command): run the orchestrator (ecad_design) on the worker, then
// load the produced schematic into the editor.
bool ecadAiDesign(AppState &st, const std::string &request) {
  if (request.empty())
    return false;
  st.toast(ui::ToastLevel::Info, "AI: designing \"" + request + "\" ...");
  st.addLog("[ecad] AI design: " + request);
  nlohmann::json res;
  if (!ecadCall(st, "ecad_design", {{"request", request}}, res))
    return false;
  ecadReportWarnings(st, res, "AI design");
  if (!ecadAddResponseTab(st, res, "AI Schematic"))
    return false;
  if (!ecadPersistGenerated(st, st.ecadDocId, "AI_Schematic"))
    return false;
  const std::string recipe = jstr(res, "recipe_used");
  if (!ecadResponseSucceeded(st, res, "AI design")) {
    if (!recipe.empty())
      st.addLog("[ecad] incomplete AI design used recipe=" + recipe);
    return false;
  }
  st.wantFocusEcad = 6;
  st.toast(ui::ToastLevel::Info,
           recipe.empty() ? "AI design ready"
                          : ("AI design ready (recipe: " + recipe + ")"));
  st.addLog("[ecad] AI design ready: " + st.ecadDocId +
            (recipe.empty() ? std::string() : " recipe=" + recipe));
  return true;
}

// One-click "AI Board": NL request -> AI designs a schematic -> a board is
// created, populated from the schematic's netlist (real footprints + pads +
// bound nets), given an auto-sized rectangular outline, and autorouted. The
// finished board is loaded into the ECAD editor. Each step is a blocking worker
// RPC mirroring ecadAiDesign's shape; every ops-response is adopted into the
// editor mirror so st.ecadDocId/revision track the board and the canvas
// reflects each stage.
void ecadAiBoard(AppState &st, const std::string &request) {
  if (request.empty())
    return;
  st.toast(ui::ToastLevel::Info, "AI Board: designing \"" + request + "\" ...");
  st.addLog("[ecad] AI board: " + request);

  // a. Design and directly adopt the full schematic ops-response. The
  // schematic remains available as its own real tab after the board is built.
  nlohmann::json res;
  if (!ecadCall(st, "ecad_design", {{"request", request}}, res))
    return;
  ecadReportWarnings(st, res, "AI board schematic");
  if (!ecadAddResponseTab(st, res, "AI Board Schematic"))
    return;
  if (!ecadPersistGenerated(st, st.ecadDocId, "AI_Board"))
    return;
  if (!ecadResponseSucceeded(st, res, "AI board schematic")) {
    const std::string recipe = jstr(res, "recipe_used");
    if (!recipe.empty())
      st.addLog("[ecad] incomplete AI board schematic used recipe=" + recipe);
    return;
  }
  const std::string schId = st.ecadDocId;
  auto boardPath = std::filesystem::u8path(st.docTabs[st.activeTab].path);
  boardPath.replace_extension(".kicad_pcb");
  st.addLog("[ecad] AI board: schematic " + schId);

  // b. Create and tab the empty board before adopting it, so the panel's
  // per-document state resolver loads the response into the board tab.
  nlohmann::json pcbRes;
  if (!ecadCall(st, "ecad_new", {{"kind", "pcb"}}, pcbRes) ||
      !ecadAddResponseTab(st, pcbRes, "AI Board"))
    return;
  const std::string pcbId = st.ecadDocId;
  st.docTabs[st.activeTab].path = boardPath.u8string();

  // c. Populate the board from the schematic netlist and adopt that returned
  // board response directly.
  nlohmann::json upd;
  if (!ecadCall(st, "ecad_update_from_sch",
                {{"board_doc_id", pcbId}, {"sch_doc_id", schId}}, upd))
    return;
  ecadReportWarnings(st, upd, "Update board");
  if (!ecadResponseSucceeded(st, upd, "Update board"))
    return;
  ecadAdopt(st, upd);
  if (!ecadPersistGenerated(st, pcbId, "AI_Board"))
    return;

  // d. Auto-outline: axis-aligned bbox over every footprint pad's world
  // position (inflated by the
  //    pad half-extent so pads sit inside the edge), padded by ~5mm, applied as
  //    a 4-line rectangle.
  {
    const auto &fps = ui::ecadEditorMirror().footprints();
    bool any = false;
    float minX = 1e30f, minY = 1e30f, maxX = -1e30f, maxY = -1e30f;
    for (const auto &fp : fps) {
      for (const auto &pad : fp.pads) {
        const float hx = 0.5f * pad.w, hy = 0.5f * pad.h;
        minX = std::min(minX, pad.pos.x - hx);
        maxX = std::max(maxX, pad.pos.x + hx);
        minY = std::min(minY, pad.pos.y - hy);
        maxY = std::max(maxY, pad.pos.y + hy);
        any = true;
      }
      if (fp.pads.empty()) { // footprint with no parsed pads -> fall back to
                             // its origin
        minX = std::min(minX, fp.pos.x);
        maxX = std::max(maxX, fp.pos.x);
        minY = std::min(minY, fp.pos.y);
        maxY = std::max(maxY, fp.pos.y);
        any = true;
      }
    }
    if (any) {
      const float m = 5.0f; // edge margin (mm)
      minX -= m;
      minY -= m;
      maxX += m;
      maxY += m;
      nlohmann::json shapes = nlohmann::json::array(
          {{{"type", "line"}, {"p1", {minX, minY}}, {"p2", {maxX, minY}}},
           {{"type", "line"}, {"p1", {maxX, minY}}, {"p2", {maxX, maxY}}},
           {{"type", "line"}, {"p1", {maxX, maxY}}, {"p2", {minX, maxY}}},
           {{"type", "line"}, {"p1", {minX, maxY}}, {"p2", {minX, minY}}}});
      nlohmann::json ops = nlohmann::json::array();
      ops.push_back(
          {{"op", "pcb_set_outline"}, {"closed", true}, {"shapes", shapes}});
      if (!ecadApplyOps(st, ops) || !ecadPersistGenerated(st, pcbId, "AI_Board"))
        return;
      st.addLog("[ecad] AI board: outline " +
                std::to_string((int)(maxX - minX)) + "x" +
                std::to_string((int)(maxY - minY)) + "mm");
    } else {
      st.addLog("[ecad] AI board: no footprints - skipping outline");
    }
  }

  // e. Autoroute the ratsnest and adopt the returned board response directly.
  nlohmann::json route;
  if (!ecadCall(st, "ecad_autoroute", {{"doc_id", pcbId}}, route))
    return;
  ecadReportWarnings(st, route, "Autoroute");
  if (!ecadResponseSucceeded(st, route, "Autoroute"))
    return;
  ecadAdopt(st, route);
  if (!ecadPersistGenerated(st, pcbId, "AI_Board"))
    return;

  st.wantFocusEcad = 6; // surface the ECAD tab so the routed board is visible
  st.toast(ui::ToastLevel::Info,
           "AI board ready - Board to 3D builds the enclosure");
  st.addLog("[ecad] AI board ready: " + pcbId);
}

// Native circuit simulation (ecad_simulate): build an analysis study from
// st.ecadSimAnalysis (0=Transient, 1=AC), run it away from the render thread,
// and stash the results payload so the ECAD plot panel renders the waveforms.
// Only schematic docs simulate.
struct EcadSimRunState {
  bool running = false;
  bool cancelRequested = false;
  std::string jobId;
  std::string docId;
  int analysis = 0;
};

EcadSimRunState &ecadSimRunState() {
  static EcadSimRunState state;
  return state;
}

std::string nextEcadSimJobId() {
  static unsigned long long sequence = 0;
  const auto tick = std::chrono::duration_cast<std::chrono::microseconds>(
                        std::chrono::steady_clock::now().time_since_epoch())
                        .count();
  return "ecad_sim_" + std::to_string(tick) + "_" +
         std::to_string(++sequence);
}

bool ecadSimSourceStillActive(const AppState &st,
                              const std::string &docId) {
  if (st.activeTab >= 0 && st.activeTab < (int)st.docTabs.size())
    return st.docTabs[st.activeTab].kind == AppState::DocTab::TabKind::Schematic &&
           st.docTabs[st.activeTab].ecadDocId == docId;
  return st.wantEcad && st.ecadDocId == docId;
}

bool ecadSimShotActive(const AppState &st) {
  const char *kind = std::getenv("CHUDWARE_ECAD_KIND");
  return st.wantEcad && kind && std::strcmp(kind, "sim") == 0;
}

void ecadSimulate(AppState &st, const std::string &requestedDoc = {}, int requestedAnalysis = -1) {
  const std::string source = requestedDoc.empty() ? st.ecadDocId : requestedDoc;
  const bool exists = std::any_of(st.docTabs.begin(), st.docTabs.end(), [&](const AppState::DocTab &tab) {
    return tab.kind == AppState::DocTab::TabKind::Schematic && tab.ecadDocId == source;
  });
  if (source.empty() || (!exists && !(st.wantEcad && ecadActiveKind(st) == "sch"))) {
    st.toast(ui::ToastLevel::Warn, "Simulation requires an open schematic");
    return;
  }
  const int selectedAnalysis = requestedAnalysis < 0 ? st.ecadSimAnalysis : requestedAnalysis;
  auto &run = ecadSimRunState();
  if (run.running) {
    st.toast(ui::ToastLevel::Info, "A circuit simulation is already running");
    return;
  }

  nlohmann::json study;
  if (selectedAnalysis == 1) {
    study = {{"analyses",
              {{{"type", "ac"},
                {"sweep", "dec"},
                {"points", 20},
                {"fstart", 10.0},
                {"fstop", 1e6}}}}};
  } else {
    study = {
        {"analyses", {{{"type", "tran"}, {"tstep", 5e-5}, {"tstop", 5e-3}}}}};
  }

  run.running = true;
  run.cancelRequested = false;
  run.jobId = nextEcadSimJobId();
  run.docId = source;
  run.analysis = selectedAnalysis;
  const std::string docId = run.docId;
  const std::string jobId = run.jobId;
  const int analysis = run.analysis;
  st.status = analysis == 1 ? "Running circuit AC simulation..."
                            : "Running circuit transient simulation...";
  st.statusKind = 0;
  st.addLog("[ecad] simulate " +
            std::string(analysis == 1 ? "ac" : "tran") + " started (" +
            jobId + ")");

  st.rpc.call(
      "ecad_simulate",
      {{"doc_id", docId}, {"study", study}, {"job_id", jobId}},
      [&st, docId, jobId, analysis](const core::RpcResult &r) {
        auto &finished = ecadSimRunState();
        if (!finished.running || finished.jobId != jobId)
          return;
        finished.running = false;
        finished.cancelRequested = false;

        const bool sourceActive = ecadSimSourceStillActive(st, docId);
        const bool cancelled =
            r.ok && r.result.is_object() &&
            r.result.value("cancelled", false);
        if (cancelled) {
          st.addLog("[ecad] simulation cancelled (" + jobId + ")");
          if (sourceActive) {
            st.status = "Circuit simulation cancelled";
            st.statusKind = 1;
            st.toast(ui::ToastLevel::Info, "Circuit simulation cancelled");
          }
          return;
        }

        if (!r.ok || !r.result.is_object() ||
            r.result.contains("code") ||
            !r.result.value("ok", true)) {
          const std::string error =
              r.ok && r.result.is_object()
                  ? r.result.value(
                        "error",
                        r.result.value(
                            "message",
                            r.result.value("hint",
                                           std::string("simulation failed"))))
                  : (r.err.empty() ? std::string("simulation returned no result")
                                   : r.err);
          st.addLog("[err] [ecad] simulate " + jobId + ": " + error);
          if (sourceActive) {
            st.status = "Circuit simulation failed";
            st.statusKind = 2;
            st.toast(ui::ToastLevel::Error, "Simulate: " + error);
          }
          return;
        }

        const nlohmann::json results = r.result.contains("results")
            ? r.result["results"] : nlohmann::json::object();
        bool retained = false;
        for (auto &tab : st.docTabs) {
          if (tab.kind == AppState::DocTab::TabKind::Schematic && tab.ecadDocId == docId) {
            tab.ecadSimResults = results;
            tab.ecadSimAnalysis = analysis;
            retained = true;
            break;
          }
        }
        if (!sourceActive) {
          st.addLog(retained ? "[ecad] simulation completed for background schematic " + docId
                             : "[ecad] simulation source was closed: " + docId);
          return;
        }
        st.ecadSimResults = results;
        const bool converged = jbool(r.result, "converged");
        if (converged) {
          st.status = "Circuit simulation complete";
          st.statusKind = 0;
          st.toast(ui::ToastLevel::Info,
                   analysis == 1 ? "Simulated (AC)"
                                 : "Simulated (transient)");
        } else {
          st.status = "Circuit simulation needs review";
          st.statusKind = 1;
          st.toast(ui::ToastLevel::Warn,
                   "Simulation ran, but convergence warnings need review");
        }
        std::string line =
            "[ecad] simulate " +
            std::string(analysis == 1 ? "ac" : "tran") +
            (converged ? " converged" : " not converged");
        if (r.result.contains("convergence"))
          line += " " + r.result["convergence"].dump();
        if (r.result.contains("ms") && !r.result["ms"].is_null())
          line += " " + r.result["ms"].dump() + "ms";
        st.addLog(line);
        if (!converged && r.result.contains("log") &&
            r.result["log"].is_array() && !r.result["log"].empty())
          st.addLog("[ecad] sim log: " + r.result["log"].back().dump());
        if (ecadSimShotActive(st)) {
          std::printf("[ecad-shot] sim results present=%d\n",
                      (int)(st.ecadSimResults.is_object() &&
                            !st.ecadSimResults.empty()));
          std::fflush(stdout);
        }
      },
      24 * 60 * 60 * 1000);
}

void ecadCancelSimulation(AppState &st) {
  auto &run = ecadSimRunState();
  if (!run.running || run.cancelRequested || run.jobId.empty())
    return;
  run.cancelRequested = true;
  st.status = "Stopping circuit simulation...";
  st.statusKind = 1;

  nlohmann::json res;
  std::string err;
  // WorkerClient intentionally lets C++ sim_cancel bypass the mutex held by
  // the long ecad_simulate call, so this control RPC cannot queue behind the
  // work it is meant to stop.
  const bool called =
      st.worker.call("sim_cancel", {{"job_id", run.jobId}}, res, err, 10000);
  const bool accepted =
      called && res.is_object() && !res.contains("code") &&
      res.value("ok", false) && res.value("cancelled", false);
  if (accepted) {
    st.addLog("[ecad] cancellation requested (" + run.jobId + ")");
    return;
  }

  run.cancelRequested = false;
  const std::string reason =
      !called ? (err.empty() ? std::string("cancel request failed") : err)
              : res.value("message",
                          std::string("solver is still starting; try again"));
  st.status = "Circuit simulation still running";
  st.statusKind = 1;
  st.toast(ui::ToastLevel::Warn, "Could not cancel simulation: " + reason);
  st.addLog("[warn] [ecad] sim_cancel " + run.jobId + ": " + reason);
}


struct EcadCommandDialogs {
  bool aiPromptRequested = false;
  bool projectPromptRequested = false;
  char aiRequest[1024]{};
  char projectName[256] = "Electronics";
};

EcadCommandDialogs &ecadCommandDialogs() {
  static EcadCommandDialogs dialogs;
  return dialogs;
}

void ecadRequestAiPrompt(AppState &st) {
  auto &dialogs = ecadCommandDialogs();
  std::snprintf(dialogs.aiRequest, sizeof(dialogs.aiRequest), "%s",
                st.ecadAiRequest.c_str());
  dialogs.aiPromptRequested = true;
  st.wantFocusEcad = 6;
}


void ecadUpdateBoardFromSchematic(AppState &st) {
  int schTab = -1;
  int boardTab = -1;
  if (st.activeTab < 0 || st.activeTab >= (int)st.docTabs.size() ||
      st.docTabs[st.activeTab].ecadDocId.empty()) {
    st.toast(ui::ToastLevel::Warn, "Select a KiCad schematic or board first");
    return;
  }
  const std::string project = st.docTabs[st.activeTab].ecadProjectPath;
  for (int i = (int)st.docTabs.size() - 1; i >= 0; --i) {
    if (st.docTabs[i].ecadProjectPath != project) continue;
    if (schTab < 0 &&
        st.docTabs[i].kind == AppState::DocTab::TabKind::Schematic)
      schTab = i;
    if (boardTab < 0 && st.docTabs[i].kind == AppState::DocTab::TabKind::PCB)
      boardTab = i;
  }
  if (st.activeTab >= 0 && st.activeTab < (int)st.docTabs.size()) {
    if (st.docTabs[st.activeTab].kind ==
        AppState::DocTab::TabKind::Schematic)
      schTab = st.activeTab;
    else if (st.docTabs[st.activeTab].kind ==
             AppState::DocTab::TabKind::PCB)
      boardTab = st.activeTab;
  }
  if (schTab < 0 || boardTab < 0) {
    st.toast(ui::ToastLevel::Warn,
             "Update Board requires an open schematic and board tab");
    return;
  }
  const std::string schId = st.docTabs[schTab].ecadDocId;
  const std::string boardId = st.docTabs[boardTab].ecadDocId;
  if (st.activeTab != boardTab)
    st.switchToTab(boardTab);
  const int before = st.ecadRevision;
  nlohmann::json res;
  if (!ecadCall(st, "ecad_update_from_sch",
                {{"sch_doc_id", schId}, {"board_doc_id", boardId}}, res))
    return;
  ecadReportWarnings(st, res, "Update board");
  if (!ecadResponseSucceeded(st, res, "Update board"))
    return;
  ecadAdopt(st, res);
  if (st.ecadRevision > before)
    st.markEcadTabDirty(boardId);
  st.toast(ui::ToastLevel::Info, "Board updated from schematic");
  st.addLog("[ecad] updated board " + boardId + " from schematic " + schId);
}


void ecadDrawCommandDialogs(AppState &st) {
  auto &dialogs = ecadCommandDialogs();
  ImGuiViewport *vp = ImGui::GetMainViewport();
  const ImVec2 center(vp->Pos.x + vp->Size.x * 0.5f,
                      vp->Pos.y + vp->Size.y * 0.5f);

  if (dialogs.aiPromptRequested) {
    ImGui::OpenPopup("AI Schematic Design##ecad_ai_design");
    dialogs.aiPromptRequested = false;
  }
  ImGui::SetNextWindowPos(center, ImGuiCond_Appearing, ImVec2(0.5f, 0.5f));
  bool aiOpen = true;
  if (ImGui::BeginPopupModal("AI Schematic Design##ecad_ai_design", &aiOpen,
                             ImGuiWindowFlags_AlwaysAutoResize |
                                 ImGuiWindowFlags_NoSavedSettings)) {
    ImGui::TextUnformatted(
        "Describe the circuit to create as a new schematic document.");
    ImGui::InputTextMultiline(
        "##ecad_ai_request", dialogs.aiRequest, sizeof(dialogs.aiRequest),
        ImVec2(ui::metrics::px(520.0f), ui::metrics::px(120.0f)));
    const bool empty = dialogs.aiRequest[0] == '\0';
    if (empty)
      ImGui::BeginDisabled();
    if (ImGui::Button("Design schematic",
                      ImVec2(ui::metrics::px(160.0f), 0.0f))) {
      st.ecadAiRequest = dialogs.aiRequest;
      st.ecadAiGo = true;
      ImGui::CloseCurrentPopup();
    }
    if (empty)
      ImGui::EndDisabled();
    ImGui::SameLine(0.0f, ui::metrics::cpMenuRowGap());
    if (ImGui::Button("Cancel", ImVec2(ui::metrics::px(88.0f), 0.0f)))
      ImGui::CloseCurrentPopup();
    ImGui::EndPopup();
  }

  if (dialogs.projectPromptRequested) {
    ImGui::OpenPopup("New KiCad Project##ecad_project");
    dialogs.projectPromptRequested = false;
  }
  ImGui::SetNextWindowPos(center, ImGuiCond_Appearing, ImVec2(0.5f, 0.5f));
  bool projectOpen = true;
  if (ImGui::BeginPopupModal("New KiCad Project##ecad_project", &projectOpen,
                             ImGuiWindowFlags_AlwaysAutoResize | ImGuiWindowFlags_NoSavedSettings)) {
    ImGui::Text("Working directory: %s", st.workspaceDir.empty() ? "(choose one first)" : st.workspaceDir.c_str());
    ImGui::SetNextItemWidth(ui::metrics::px(320.0f));
    ImGui::InputText("Project name", dialogs.projectName, sizeof(dialogs.projectName));
    const std::string name = dialogs.projectName;
    const bool valid = !st.workspaceDir.empty() && !name.empty() && name != "." && name != ".." &&
                       name.find_first_of("/\\") == std::string::npos &&
                       std::none_of(name.begin(), name.end(), [](unsigned char c) { return c < 32; });
    if (!valid) ImGui::BeginDisabled();
    if (ImGui::Button("Create", ImVec2(ui::metrics::px(96.0f), 0.0f))) {
      const auto dir = std::filesystem::u8path(st.workspaceDir) / std::filesystem::u8path(name);
      nlohmann::json res;
      if (ecadCall(st, "ecad_new_project", {{"dir", dir.u8string()}, {"name", name}}, res) &&
          ecadAddProject(st, res)) {
        st.pushRecentFile(jstr(res, "project"));
        st.toast(ui::ToastLevel::Info, "Created KiCad project " + name);
        ImGui::CloseCurrentPopup();
      }
    }
    if (!valid) ImGui::EndDisabled();
    ImGui::SameLine(0.0f, ui::metrics::cpMenuRowGap());
    if (ImGui::Button("Cancel", ImVec2(ui::metrics::px(88.0f), 0.0f)))
      ImGui::CloseCurrentPopup();
    ImGui::EndPopup();
  }
  auto &sim = ecadSimRunState();
  if (sim.running && !ecadSimShotActive(st)) {
    ImGui::SetNextWindowPos(center, ImGuiCond_Always, ImVec2(0.5f, 0.5f));
    ImGui::SetNextWindowSize(ImVec2(ui::metrics::px(390.0f), 0.0f),
                             ImGuiCond_Always);
    if (ImGui::Begin("Circuit Simulation##ecad_sim_progress", nullptr,
                     ImGuiWindowFlags_AlwaysAutoResize |
                         ImGuiWindowFlags_NoSavedSettings |
                         ImGuiWindowFlags_NoCollapse |
                         ImGuiWindowFlags_NoResize |
                         ImGuiWindowFlags_NoMove)) {
      ImGui::TextUnformatted(sim.cancelRequested
                                 ? "Stopping the circuit solver..."
                                 : "Circuit simulation is running...");
      ImGui::TextDisabled("The editor remains responsive while it solves.");
      ImGui::Spacing();
      if (sim.cancelRequested)
        ImGui::BeginDisabled();
      if (ImGui::Button(sim.cancelRequested ? "Stopping..."
                                            : "Cancel simulation",
                        ImVec2(ui::metrics::px(150.0f), 0.0f)))
        ecadCancelSimulation(st);
      if (sim.cancelRequested)
        ImGui::EndDisabled();
    }
    ImGui::End();
  }

}

void ecadRunKicadCheck(AppState &st) {
  if (st.activeTab < 0 || st.activeTab >= (int)st.docTabs.size()) return;
  const auto &tab = st.docTabs[st.activeTab];
  const std::string id = tab.ecadDocId;
  const int revision = tab.ecadRevision;
  if (id.empty()) return;
  st.rpc.call("ecad_kicad_check", {{"doc_id", id}},
              [&st, id, revision](const core::RpcResult &r) {
    if (!r.ok || !r.result.is_object() || r.result.contains("code")) {
      const std::string error = r.err.empty() ? jstr(r.result, "message") : r.err;
      st.toast(ui::ToastLevel::Error, "KiCad check failed: " + error);
      return;
    }
    nlohmann::json result = r.result;
    result["_source_revision"] = revision;
    ui::ecadSetCheckResult(id, result);
    st.addLog("[ecad] KiCad " + jstr(result, "kind") + " completed for " + id);
  }, core::kLongEngineCallTimeoutMs);
}

void ecadReloadFromKicad(AppState &st) {
  if (st.activeTab < 0 || st.activeTab >= (int)st.docTabs.size()) return;
  const std::string id = st.docTabs[st.activeTab].ecadDocId;
  if (id.empty()) return;
  st.rpc.call("ecad_reload", {{"doc_id", id}, {"discard", true}},
              [&st, id](const core::RpcResult &r) {
    if (!r.ok || !r.result.is_object() || r.result.contains("code")) {
      st.toast(ui::ToastLevel::Error, "Reload failed: " +
               (r.err.empty() ? jstr(r.result, "message") : r.err));
      return;
    }
    for (auto &tab : st.docTabs) {
      if (tab.ecadDocId != id) continue;
      const int revision = r.result.value("revision", -1);
      if (revision >= 0 && revision < tab.ecadRevision) return;
      tab.ecadRevision = revision;
      tab.dirty = false;
      tab.lastHumanEditRevision = -1;
      tab.ecadSessionDirty = false;
      tab.ecadFileChanged = false;
      tab.ecadKicadLocked = r.result.value("kicad_locked", tab.ecadKicadLocked);
      ui::ecadSetFileState(id, tab.path, false, false, tab.ecadKicadLocked);
#ifndef __EMSCRIPTEN__
      ecadRecoverySavedImpl(id, revision);
#endif
      if (st.activeTab >= 0 && st.activeTab < (int)st.docTabs.size() &&
          st.docTabs[st.activeTab].ecadDocId == id) {
        st.ecadRevision = revision;
        ui::ecadEditorLoadResponse(r.result);
      }
      st.toast(ui::ToastLevel::Info, "Reloaded the KiCad file");
      return;
    }
  });
}

void ecadLaunchKicad(AppState &st, const std::string &cmd) {
#ifdef __EMSCRIPTEN__
  (void)cmd;
  st.toast(ui::ToastLevel::Warn, "Opening KiCad requires the desktop app");
#else
  if (st.activeTab < 0 || st.activeTab >= (int)st.docTabs.size()) return;
  const auto &active = st.docTabs[st.activeTab];
  if (active.ecadDocId.empty()) return;
  struct Launch {
    std::string activeId, project, command;
    std::vector<std::string> ids;
    size_t pending = 0;
    bool failed = false;
  };
  auto launch = std::make_shared<Launch>();
  launch->activeId = active.ecadDocId;
  launch->project = active.ecadProjectPath;
  launch->command = cmd;
  for (const auto &tab : st.docTabs) {
    if (tab.ecadDocId == launch->activeId ||
        (cmd == "ecad_kicad_open_project" && !launch->project.empty() &&
         tab.ecadProjectPath == launch->project && !tab.ecadDocId.empty()))
      launch->ids.push_back(tab.ecadDocId);
  }
  launch->pending = launch->ids.size();
  for (const std::string &id : launch->ids) {
    st.rpc.call("ecad_tree", {{"doc_id", id}}, [&st, launch, id](const core::RpcResult &r) {
      if (!r.ok || !r.result.is_object() || r.result.contains("code")) {
        launch->failed = true;
        st.toast(ui::ToastLevel::Error, "Could not check KiCad document before opening: " +
                 (r.err.empty() ? jstr(r.result, "message") : r.err));
      } else {
        for (auto &tab : st.docTabs) {
          if (tab.ecadDocId != id) continue;
          const int revision = r.result.value("revision", -1);
          if (revision >= 0 && revision < tab.ecadRevision) {
            launch->failed = true;
            st.toast(ui::ToastLevel::Warn, "The electronics document changed; open KiCad again.");
            break;
          }
          const bool changed = revision >= 0 && revision != tab.ecadRevision;
          if (revision >= 0) tab.ecadRevision = revision;
          tab.ecadSessionDirty = r.result.value("dirty", tab.ecadSessionDirty);
          tab.ecadFileChanged = r.result.value("file_changed", false);
          tab.ecadKicadLocked = r.result.value("kicad_locked", false);
          if (r.result.contains("path") && r.result["path"].is_string()) {
            tab.path = r.result["path"].get<std::string>();
            if (!tab.path.empty()) tab.title = std::filesystem::u8path(tab.path).filename().u8string();
          }
          if (r.result.contains("project") && r.result["project"].is_string()) {
            const std::string project = r.result["project"].get<std::string>();
            if (project != tab.ecadProjectPath) {
              tab.ecadProjectPath = project;
              tab.ecadProjectSchPath.clear();
              tab.ecadProjectPcbPath.clear();
            }
          }
          st.syncEcadProjectFiles(tab.ecadProjectPath);
          ui::ecadSetFileState(id, tab.path, tab.dirty || tab.ecadSessionDirty,
                               tab.ecadFileChanged, tab.ecadKicadLocked);
          if (changed && st.activeTab >= 0 && st.activeTab < (int)st.docTabs.size() &&
              st.docTabs[st.activeTab].ecadDocId == id) {
            st.ecadRevision = tab.ecadRevision;
            ui::ecadEditorLoadResponse(r.result);
          }
          break;
        }
      }
      if (--launch->pending != 0 || launch->failed) return;
      int activeIndex = -1;
      for (const auto &saveId : launch->ids) {
        int index = -1;
        for (int i = 0; i < (int)st.docTabs.size(); ++i)
          if (st.docTabs[i].ecadDocId == saveId) { index = i; break; }
        if (index < 0) continue;
        if (saveId == launch->activeId) activeIndex = index;
        const auto &tab = st.docTabs[index];
        if (tab.dirty || tab.ecadSessionDirty || tab.path.empty())
          if (ui::saveDocTab(st, index, false) != ui::TabSaveResult::Saved) return;
      }
      if (activeIndex < 0) return;
      const auto &tab = st.docTabs[activeIndex];
      const bool project = launch->command == "ecad_kicad_open_project";
      const char *key = project ? "kicad" :
          launch->command == "ecad_kicad_open_sch" ? "eeschema" : "pcbnew";
      const std::string executable = jstr(st.ecadKicadStatus, key);
      const std::string file = project ? tab.ecadProjectPath : tab.path;
      if (file.empty()) {
        st.toast(ui::ToastLevel::Warn, "Save the KiCad project before opening it");
        return;
      }
      if (!platform::launchKicad(executable, file))
        st.toast(ui::ToastLevel::Error, "Could not launch KiCad for " + file);
    });
  }
#endif
}

// All editor mutations are handled by KiCad; these are project-hub actions.
void ecadDispatch(AppState &st, const std::string &cmd) {
  if (cmd == "ecad_ai_design") { ecadRequestAiPrompt(st); return; }
  if (cmd == "ecad_new_project") {
    ecadCommandDialogs().projectPromptRequested = true;
    st.wantFocusEcad = 6;
    return;
  }
  if (cmd == "ecad_open") { ecadOpenDoc(st); return; }
  if (cmd == "ecad_save") { ecadSaveDoc(st); return; }
  if (cmd == "ecad_save_as") {
    ui::saveDocTab(st, st.activeTab, true);
    return;
  }
  if (cmd == "ecad_kicad_open_sch" || cmd == "ecad_kicad_open_pcb" ||
      cmd == "ecad_kicad_open_project") { ecadLaunchKicad(st, cmd); return; }
  if (cmd == "ecad_reload") { ecadReloadFromKicad(st); return; }
  if (cmd == "ecad_run_erc" || cmd == "ecad_run_drc") {
    if (ecadRequireKind(st, cmd == "ecad_run_erc" ? "sch" : "pcb", "KiCad check"))
      ecadRunKicadCheck(st);
    return;
  }
  if (cmd == "ecad_simulate") {
    if (ecadRequireKind(st, "sch", "Simulation")) {
      st.ecadSimGo = true;
      st.ecadSimRequestedDocId = st.ecadDocId;
      st.ecadSimRequestedAnalysis = st.ecadSimAnalysis;
      st.wantFocusEcad = 4;
    }
    return;
  }
  if (cmd == "ecad_autoroute") {
    if (ecadRequireKind(st, "pcb", "Autoroute"))
      ecadDocRpc(st, "ecad_autoroute", "Autoroute complete");
    return;
  }
  if (cmd == "ecad_fill_zones") {
    if (ecadRequireKind(st, "pcb", "Fill zones"))
      ecadDocRpc(st, "ecad_fill_zones", "Copper zones filled");
    return;
  }
  if (cmd == "ecad_to_3d") { ecadTo3d(st); return; }
  if (cmd == "pcb_update_from_sch") { ecadUpdateBoardFromSchematic(st); return; }
  if (cmd == "ecad_export_fab") {
    if (!ecadRequireKind(st, "pcb", "Fabrication export") ||
        st.activeTab < 0 || st.activeTab >= (int)st.docTabs.size()) return;
    const std::string path = st.docTabs[st.activeTab].path;
    if (path.empty()) {
      st.toast(ui::ToastLevel::Warn, "Save the board before exporting fabrication files");
      return;
    }
    const std::string dir = (std::filesystem::u8path(path).parent_path() / "fab").u8string();
    for (const char *kind : {"gerber", "drill", "cpl", "bom"})
      if (!ecadExport(st, kind, "pcb", dir, false)) return;
    st.toast(ui::ToastLevel::Info, "Fabrication files exported to " + dir);
    return;
  }
  if (cmd == "ecad_export_step") { ecadExport(st, "step", "pcb"); return; }
  if (cmd == "ecad_export_bom") {
    const std::string kind = ecadActiveKind(st);
    if (!kind.empty()) ecadExport(st, "bom", kind.c_str());
    return;
  }
  if (cmd == "sch_export_netlist") { ecadExport(st, "netlist", "sch"); return; }
  if (cmd == "sch_export_pdf") { ecadExport(st, "pdf", "sch"); return; }
  st.addLog("[warn] [ecad] unhandled command id: " + cmd);
  st.toast(ui::ToastLevel::Warn, "Unhandled ECAD command: " + cmd);
}

} // namespace

#ifndef __EMSCRIPTEN__
void ecadRecoverySaved(const std::string &docId, int savedRevision) {
  ecadRecoverySavedImpl(docId, savedRevision);
}
#endif

// W16: apply the param-less sm_flat_pattern feature (Sheet Metal "Flatten") +
// reload the mesh. One-click feature (no PM form) — add_feature then
// loadDocMesh from the ops-response, like the feature-tree op helper. The
// DocMirror listener refreshes the FeatureManager automatically.
static void applyFlatPattern(AppState &st) {
  if (!st.docMirror.hasDoc()) {
    st.toast(ui::ToastLevel::Info, "Open a sheet metal part first");
    return;
  }
  nlohmann::json ops =
      nlohmann::json::array({{{"op", "add_feature"},
                              {"feature",
                               {{"type", "sm_flat_pattern"},
                                {"name", "Flat-Pattern"},
                                {"params", nlohmann::json::object()}}}}});
  ++st.rebuildsInFlight;
  st.docMirror.applyOpsAsync(
      st.rpc, st.worker, ops, [&st](const core::OpsResponse &r, bool current) {
        --st.rebuildsInFlight;
        if (!current || !st.isActiveMirrorDoc())
          return;
        if (r.ok && !r.aicm_path.empty()) {
          st.loadDocMeshAsync(r.aicm_path);
          st.toast(ui::ToastLevel::Info, "Flat pattern created");
        } else if (!r.ok) {
          st.toast(ui::ToastLevel::Warn, r.error_message.empty()
                                             ? std::string("Flatten failed")
                                             : r.error_message);
        }
      });
}

// W14: export the ACTIVE sheet-metal part's flat pattern to DXF/DWG/PDF
// (outline on layer OUTLINE, bend lines on layer BEND). One-click Sheet Metal
// command (no PM form) — picks a path (native Save-As, or cwd fallback when
// dialogs are disabled) then calls export_flat_dxf via DocMirror. The engine
// computes the flat (fresh compile) and errors cleanly if the part has no flat
// pattern.
static void exportFlatDxf(AppState &st) {
  if (!st.docMirror.hasDoc()) {
    st.toast(ui::ToastLevel::Info, "Open a sheet metal part first");
    return;
  }
  // Suggested file name from the doc title.
  std::string title =
      st.docMirror.tree().is_object()
          ? st.docMirror.tree().value("name", std::string("part"))
          : std::string("part");
  std::string base;
  for (char c : title) {
    if (c == ' ')
      base += '_';
    else if (std::isalnum((unsigned char)c) || c == '_' || c == '-')
      base += c;
  }
  if (base.empty())
    base = "part";
  base += "_flat";

  const bool dialogsDisabled = std::getenv("CHUDWARE_NO_FILE_DIALOGS") != nullptr;
  std::string path;
  if (dialogsDisabled) {
    path = (std::filesystem::current_path() / (base + ".dxf")).string();
  } else {
    const char *filter = "DXF (*.dxf)\0*.dxf\0AutoCAD DWG (*.dwg)\0*.dwg\0PDF "
                         "(*.pdf)\0*.pdf\0All files (*.*)\0*.*\0";
    path = ui::saveFileDialog(filter, "dxf", (base + ".dxf").c_str());
    if (path.empty())
      return; // canceled
  }
  // Format follows the chosen extension (export_flat_dxf accepts dxf|dwg|pdf;
  // defaults to the ext).
  std::string fmt = "dxf";
  if (path.size() >= 4) {
    std::string tail = path.substr(path.size() - 4);
    for (auto &ch : tail)
      ch = (char)std::tolower((unsigned char)ch);
    if (tail == ".pdf")
      fmt = "pdf";
    else if (tail == ".dwg")
      fmt = "dwg";
  }

  std::string err;
  std::string written = st.docMirror.exportFlat(st.worker, path, fmt, err);
  if (!written.empty()) {
    st.addLog("[sm] exported flat " + fmt + ": " + written);
    st.toast(ui::ToastLevel::Info, "Exported " + written);
    ui::afterFileWritten(written);
  } else {
    st.addLog("[sm] export flat failed: " + err);
    st.toast(ui::ToastLevel::Warn,
             err.empty() ? std::string("Export Flat DXF failed") : err);
  }
}

// ---------------------------------------------------------------------------
// App-level ribbon command handlers. These were inline blocks in
// dispatchCmd's if-chain; they stay here (app-level policy) and are registered
// into the CommandRouter (app_commands.cpp) through RibbonCommandHooks, in the
// original chain order.
// ---------------------------------------------------------------------------

// Wave 4C/4D: Edit-menu copy / cut / paste. The menu bar fires these ids; they
// route exactly like the global Ctrl+C/X/V keys (sketch first when active, else
// the FeatureManager). They are registered AFTER the sketch onRibbonCommand
// hook so an armed sketch tool doesn't swallow them — these ids are never
// sketch-tool ids, so onRibbonCommand returns false for them.
static bool cmdEditCopy(AppState &st, const std::string & /*cmd*/) {
  ui::SketchMode &sketchMode = *st.sketchMode;
  if (sketchMode.active()) {
    if (sketchMode.sketchHasSelection())
      sketchMode.sketchCopySelection(st);
  } else
    ui::featureCopySelection(st);
  return true;
}
static bool cmdEditCut(AppState &st, const std::string & /*cmd*/) {
  // Sketch mode has no cut (Delete removes entities); cut applies to the
  // FeatureManager.
  ui::SketchMode &sketchMode = *st.sketchMode;
  if (!sketchMode.active())
    ui::featureCutSelection(st);
  return true;
}
static bool cmdEditPaste(AppState &st, const std::string & /*cmd*/) {
  ui::SketchMode &sketchMode = *st.sketchMode;
  if (sketchMode.active() &&
      st.clipboard.kind == AppState::Clipboard::Kind::SketchEntities)
    sketchMode.sketchPasteBegin(st);
  else if (st.clipboard.kind == AppState::Clipboard::Kind::Feature)
    ui::featurePasteSelection(st);
  return true;
}
// Wave-5-fill: Edit > Delete / Select All. featureDeleteSelection applies a
// DocMirror op immediately; that is safe here because the dispatcher runs
// BEFORE the FeatureManager tree walk this frame, so no tree-json iteration is
// live. Both stand down while a sketch is active (SketchMode owns the
// sketch-entity Del / marquee); with no sketch they act on the FeatureManager
// selection.
static bool cmdEditDelete(AppState &st, const std::string & /*cmd*/) {
  ui::SketchMode &sketchMode = *st.sketchMode;
  if (!sketchMode.active())
    ui::featureDeleteSelection(st);
  return true;
}
static bool cmdEditSelectAll(AppState &st, const std::string & /*cmd*/) {
  ui::SketchMode &sketchMode = *st.sketchMode;
  if (!sketchMode.active())
    ui::featureSelectAll(st);
  return true;
}

// DRAWINGS: "Make Drawing" creates a drawing + standard views from the active
// part and opens it as a Drawing tab (guarded; toasts if no part is open).
static bool cmdMakeDrawing(AppState &st, const std::string &cmd) {
  if (cmd == "make_drawing") {
    st.makeDrawing();
    return true;
  }
  return false;
}

// Appearance tab: Display group (cheap — the viewport applies st.displayStyle
// each frame).
static bool cmdDisplayStyle(AppState &st, const std::string &cmd) {
  int ds = -1;
  if (cmd == "display_shaded")
    ds = 0;
  else if (cmd == "display_shaded_edges")
    ds = 1;
  else if (cmd == "display_hlr")
    ds = 2;
  else if (cmd == "display_hlv")
    ds = 3;
  else if (cmd == "display_wireframe")
    ds = 4;
  if (ds < 0)
    return false;
  st.displayStyle = ds;
  return true;
}

// Appearance tab: Material group (set_appearance on body "b1").
static bool cmdAppearance(AppState &st, const std::string &cmd) {
  if (cmd == "appr_color") {
    st.requestAppearanceEditor = true;
    return true;
  }
  if (cmd == "appr_clear") {
    applyModelAppearance(st, nullptr, 0.0f, 0.5f);
    return true;
  }
  int mi = -1;
  if (cmd == "appr_mat_plastic")
    mi = 0;
  else if (cmd == "appr_mat_aluminum")
    mi = 1;
  else if (cmd == "appr_mat_steel")
    mi = 2;
  else if (cmd == "appr_mat_brass")
    mi = 3;
  else if (cmd == "appr_mat_dark")
    mi = 4;
  if (mi < 0)
    return false;
  const ui::MaterialPreset &m = ui::kMaterials[mi];
  applyModelAppearance(st, m.rgb, m.metallic, m.roughness);
  return true;
}

// Sheet Metal one-clicks (W16 Flatten / W14 flat-pattern DXF) — no PM form.
static bool cmdFlatPattern(AppState &st, const std::string &cmd) {
  if (cmd == "sm_flat_pattern") {
    applyFlatPattern(st);
    return true;
  }
  return false;
}
static bool cmdFlatDxf(AppState &st, const std::string &cmd) {
  if (cmd == "sm_export_flat_dxf") {
    exportFlatDxf(st);
    return true;
  }
  return false;
}

// Wave-5-fill / F4: File > Export <fmt> — export the ACTIVE part/assembly's
// bodies to a format export_file accepts ("step"|"iges"|"brep"|"sat"|"stl"|"obj"|
// "ply"|"3mf"|"gltf"|"glb"|"dxf"|"dwg"; the registry is core/doc/formats.cpp
// format_registry(), reported by the doc_formats RPC). Picks a path (native
// Save-As, or a cwd fallback when dialogs are disabled) then calls export_file
// via DocMirror. Mirrors exportFlatDxf's shape. Called only from the GL-thread
// request pump (the menu sets st.requestExportFormat) so the native dialog
// never re-enters an ImGui menu walk.
static void exportFile3d(AppState &st, const std::string &fmt) {
  if (!st.docMirror.hasDoc()) {
    st.toast(ui::ToastLevel::Info, "Open a part or assembly first");
    return;
  }
  // Per-format extension + native-dialog filter.
  const char *ext = "step";
  const char *filter = "STEP (*.step)\0*.step;*.stp\0All files (*.*)\0*.*\0";
  if (fmt == "iges") {
    ext = "iges";
    filter = "IGES (*.iges)\0*.iges;*.igs\0All files (*.*)\0*.*\0";
  } else if (fmt == "stl") {
    ext = "stl";
    filter = "STL (*.stl)\0*.stl\0All files (*.*)\0*.*\0";
  } else if (fmt == "3mf") {
    ext = "3mf";
    filter = "3MF (*.3mf)\0*.3mf\0All files (*.*)\0*.*\0";
  } else if (fmt == "brep") {
    ext = "brep";
    filter = "BREP (*.brep)\0*.brep\0All files (*.*)\0*.*\0";
  } else if (fmt == "sat") {
    ext = "sat";
    filter = "ACIS SAT (*.sat)\0*.sat\0All files (*.*)\0*.*\0";
  } else if (fmt == "obj") {
    ext = "obj";
    filter = "OBJ (*.obj)\0*.obj\0All files (*.*)\0*.*\0";
  } else if (fmt == "ply") {
    ext = "ply";
    filter = "PLY (*.ply)\0*.ply\0All files (*.*)\0*.*\0";
  } else if (fmt == "gltf") {
    ext = "gltf";
    filter = "glTF (*.gltf)\0*.gltf\0All files (*.*)\0*.*\0";
  } else if (fmt == "glb") {
    ext = "glb";
    filter = "glTF binary (*.glb)\0*.glb\0All files (*.*)\0*.*\0";
  } else if (fmt == "dxf") {
    ext = "dxf";
    filter = "AutoCAD DXF (*.dxf)\0*.dxf\0All files (*.*)\0*.*\0";
  } else if (fmt == "dwg") {
    ext = "dwg";
    filter = "AutoCAD DWG (*.dwg)\0*.dwg\0All files (*.*)\0*.*\0";
  } else if (fmt == "scad") {
    ext = "scad";
    filter = "OpenSCAD (*.scad)\0*.scad\0All files (*.*)\0*.*\0";
  }

  // Suggested base name from the doc title (alnum/_/-, spaces -> _).
  std::string title =
      st.docMirror.tree().is_object()
          ? st.docMirror.tree().value("name", std::string("part"))
          : std::string("part");
  std::string base;
  for (char c : title) {
    if (c == ' ')
      base += '_';
    else if (std::isalnum((unsigned char)c) || c == '_' || c == '-')
      base += c;
  }
  if (base.empty())
    base = "part";

  const bool dialogsDisabled = std::getenv("CHUDWARE_NO_FILE_DIALOGS") != nullptr;
  std::string path;
  if (dialogsDisabled) {
    path = (std::filesystem::current_path() / (base + "." + ext)).string();
  } else {
    path = ui::saveFileDialog(filter, ext, (base + "." + ext).c_str());
    if (path.empty())
      return; // canceled
  }

  // Tessellated formats carry the dialog's tessellation choice (DXF/DWG write
  // solids as ACIS and use it only for curves ACIS cannot express); B-rep
  // formats (incl. SAT) ignore it. ASCII is STL-only.
  const bool meshFmt = (fmt == "stl" || fmt == "obj" || fmt == "ply" ||
                        fmt == "3mf" || fmt == "gltf" || fmt == "glb" ||
                        fmt == "dxf" || fmt == "dwg");
  const double linDef = meshFmt ? (double)st.exportTess.linDeflection : 0.05;
  const double angDef = meshFmt ? (double)st.exportTess.angDeflectionDeg *
                                      3.14159265358979 / 180.0
                                : 0.35;
  const bool relDef = meshFmt && st.exportTess.relative;
  const bool asciiStl = fmt == "stl" && st.exportTess.ascii;

  std::string err;
  nlohmann::json exportResult;
  std::string written =
      st.docMirror.exportFile(st.worker, st.docMirror.docId(), path, fmt, err,
                              linDef, angDef, relDef, asciiStl, &exportResult);
#ifdef __EMSCRIPTEN__
  if (!written.empty() && fmt == "scad" &&
      !chudware::fsDownloadScadProject(path, exportResult.dump(), err))
    written.clear();
#endif
  if (!written.empty()) {
    st.addLog("[export] " + fmt + ": " + written);
    st.toast(ui::ToastLevel::Info, "Exported " + written);
#ifdef __EMSCRIPTEN__
    if (fmt != "scad") ui::afterFileWritten(written);
#else
    ui::afterFileWritten(written);
#endif
  } else {
    st.addLog("[export] " + fmt + " failed: " + err);
    st.toast(ui::ToastLevel::Warn,
             err.empty() ? ("Export " + fmt + " failed") : err);
  }
}

// ---- Wave 3A item 5: OS file drop (Windows Explorer -> our window)
// -------------------------- GLFW delivers OS file drops to a static callback.
// We reach AppState via the window user pointer (set once in run_gui after
// AppState is constructed) and route the FIRST dropped path with a known
// CAD/ECAD extension to the GL-thread request pump (st.requestOpenPath),
// exactly like the File Explorer's double-click / drag-to-viewport. Routing CAD
// vs ECAD by extension is done by the pump.
//
// Behavior on a MULTI-file drop: open the first valid file and toast that the
// rest were ignored (the pump is single-shot per frame; opening a doc swaps the
// active tab, so silently opening N docs would be surprising). Unknown
// extensions are skipped; if none are openable we toast.
//
// We CHAIN the ImGui-GLFW backend's drop callback (captured at install time) so
// ImGui still sees the drop too (it does nothing harmful with it, but chaining
// keeps the backend contract intact).
static GLFWdropfun g_prevDropCallback = nullptr;

static bool dropPathHasKnownExt(const std::string &path) {
  std::string ext = std::filesystem::u8path(path).extension().u8string();
  std::transform(ext.begin(), ext.end(), ext.begin(),
                 [](unsigned char c) { return (char)std::tolower(c); });
  // F4: every importable 3D format (see core/doc/formats.cpp format_registry()
  // / doc_formats RPC) plus the ECAD/KiCad family, the AutoCAD DXF/DWG lane and
  // ACIS .sat/.sab. The GL-thread pump branches CAD vs ECAD by extension.
  return ext == ".scad" || ext == ".draw+" || ext == ".step+" || ext == ".step" ||
         ext == ".stp" || ext == ".iges" || ext == ".igs" ||
         ext == ".brep" || ext == ".stl" || ext == ".3mf" ||
         ext == ".obj" || ext == ".ply" || ext == ".gltf" || ext == ".glb" ||
         ext == ".3dm" || ext == ".dxf" || ext == ".dwg" || ext == ".sat" ||
         ext == ".splat" || ext == ".spz" || ext == ".ksplat" ||
         ext == ".mp4" || ext == ".mov" || ext == ".webm" ||
         ext == ".mkv" || ext == ".avi" ||
         ext == ".sab" || ext == ".sldprt" ||
         ext == ".sldasm" || ext == ".x_t" ||
         ext == ".x_b" || // experimental (SW COM)
         ext == ".ecad+" || ext == ".ecad" || ext == ".kicad_sch" ||
         ext == ".kicad_pcb" || ext == ".kicad_pro";
}

static void onFileDrop(GLFWwindow *win, int count, const char **paths) {
  if (g_prevDropCallback)
    g_prevDropCallback(win, count, paths); // keep ImGui's backend in the loop
  AppState *stp = static_cast<AppState *>(glfwGetWindowUserPointer(win));
  if (!stp || count <= 0 || !paths)
    return;
#ifdef __EMSCRIPTEN__
  // Web: GLFW/browser "paths" are not host filesystem paths the daemon can
  // open. Canvas HTML5 drops are handled by chudware_install_canvas_drop +
  // drainWebFileDrop.
  (void)stp;
  (void)paths;
  return;
#else
  AppState &st = *stp;
  std::string first;
  int known = 0;
  for (int i = 0; i < count; ++i) {
    if (!paths[i])
      continue;
    if (dropPathHasKnownExt(paths[i])) {
      ++known;
      if (first.empty())
        first = paths[i];
    }
  }
  if (first.empty()) {
    st.toast(ui::ToastLevel::Warn, "Drop a CAD, ECAD, splat or video file to open it");
    return;
  }
  // Route the first valid file through the same deferred pump the menu/Explorer
  // use.
  st.requestOpenPath = first;
  st.addLog("[dnd] OS drop -> " + first);
  if (known > 1)
    st.toast(ui::ToastLevel::Info,
             "Opening " + std::filesystem::path(first).filename().string() +
                 " (dropped " + std::to_string(known) +
                 " files; opening the first)");
#endif
}

#ifdef __EMSCRIPTEN__
// Browser canvas drop → upload into workspace → requestOpenPath (ASYNCIFY-safe
// drain).
struct WebDropPending {
  volatile int ready = 0; // 0=empty, 1=pending, -1=error toast
  char name[512] = {0};
  uint8_t *data = nullptr;
  size_t len = 0;
  char err[256] = {0};
};
static WebDropPending g_webDrop;

extern "C" EMSCRIPTEN_KEEPALIVE void
chudware_web_drop_done(int ok, const char *name, const uint8_t *data, int len,
                     const char *errMsg) {
  if (g_webDrop.data) {
    free(g_webDrop.data);
    g_webDrop.data = nullptr;
    g_webDrop.len = 0;
  }
  if (ok && data && len > 0 && name && name[0]) {
    g_webDrop.data = static_cast<uint8_t *>(malloc(static_cast<size_t>(len)));
    if (!g_webDrop.data) {
      std::strncpy(g_webDrop.err, "out of memory", sizeof(g_webDrop.err) - 1);
      g_webDrop.ready = -1;
      return;
    }
    std::memcpy(g_webDrop.data, data, static_cast<size_t>(len));
    g_webDrop.len = static_cast<size_t>(len);
    std::strncpy(g_webDrop.name, name, sizeof(g_webDrop.name) - 1);
    g_webDrop.name[sizeof(g_webDrop.name) - 1] = '\0';
    g_webDrop.err[0] = '\0';
    g_webDrop.ready = 1;
  } else {
    g_webDrop.name[0] = '\0';
    if (errMsg && errMsg[0]) {
      std::strncpy(g_webDrop.err, errMsg, sizeof(g_webDrop.err) - 1);
      g_webDrop.err[sizeof(g_webDrop.err) - 1] = '\0';
    } else {
      g_webDrop.err[0] = '\0';
    }
    g_webDrop.ready = -1;
  }
}

// JS library (chudware_web_dialogs.js) — installs dragover/drop on the canvas.
extern "C" void chudware_install_canvas_drop(void);

static void drainWebFileDrop(AppState &st) {
  if (g_webDrop.ready == 0)
    return;
  if (g_webDrop.ready < 0) {
    if (g_webDrop.err[0])
      st.toast(ui::ToastLevel::Warn, g_webDrop.err);
    else
      st.toast(ui::ToastLevel::Warn, "Drop a CAD or ECAD file to open it");
    g_webDrop.ready = 0;
    return;
  }
  // ready == 1
  std::string name = g_webDrop.name;
  std::vector<uint8_t> bytes;
  if (g_webDrop.data && g_webDrop.len > 0)
    bytes.assign(g_webDrop.data, g_webDrop.data + g_webDrop.len);
  if (g_webDrop.data) {
    free(g_webDrop.data);
    g_webDrop.data = nullptr;
    g_webDrop.len = 0;
  }
  g_webDrop.ready = 0;
  g_webDrop.name[0] = '\0';

  if (name.empty() || bytes.empty()) {
    st.toast(ui::ToastLevel::Warn, "Drop a CAD or ECAD file to open it");
    return;
  }
  if (!dropPathHasKnownExt(name)) {
    st.toast(ui::ToastLevel::Warn, "Drop a CAD or ECAD file to open it");
    return;
  }
  st.toast(ui::ToastLevel::Info, "Uploading " + name + "\xe2\x80\xa6");
  std::string upErr;
  // Upload into workspace root (same plumbing as openFileDialog's pickFile
  // path).
  std::string abs =
      chudware::fsUpload(/*dir=*/"", name, bytes.data(), bytes.size(), upErr);
  if (abs.empty()) {
    st.addLog("[dnd] web drop upload failed: " + upErr);
    st.toast(ui::ToastLevel::Error,
             "Upload failed: " +
                 (upErr.empty() ? std::string("unknown") : upErr));
    return;
  }
  st.requestOpenPath = abs;
  st.addLog("[dnd] web drop -> " + abs);
}
#endif

#ifndef __EMSCRIPTEN__
struct NativeStartup {
  bool firstFramePresented = false;
  bool daemonConnectedMarked = false;
  bool daemonBootstrapMarked = false;
  bool workerStartedMarked = false;
  bool readinessPending = false;
  bool engineReady = false;
  std::chrono::steady_clock::time_point nextReadyPoll{};
  bool starterOpenPending = false;
  bool starterPresented = false;
  bool interactiveMarked = false;
};
static NativeStartup s_nativeStartup;
#endif

// Offer a disposable starter only once, after the native engine is ready and
// bootstrap has checked crash recovery. Readiness RPCs run on the async lane;
// the GUI never waits for hostd's cold engine load. Web keeps its existing gate.
static void ensureStarterDoc(AppState &st, bool shot) {
  if (shot || st.starterTried)
    return;
  if (st.docMirror.hasDoc() || !st.docTabs.empty() || st.designStarted) {
#ifndef __EMSCRIPTEN__
    // Seeing any document permanently cancels the offer, even if it is closed
    // before the engine finishes starting.
    st.starterTried = true;
#endif
    return;
  }
#ifndef __EMSCRIPTEN__
  if (!st.requestOpenPath.empty() || st.rebuildsInFlight > 0) {
    st.starterTried = true;
    return;
  }
#endif
  if (!st.workerStarted)
    return;
  // Never drop a blank starter next to parts the daemon is offering for crash
  // recovery.
  if (!st.recoveryPrompt.empty() || st.recoveryModalOpen || st.recoveryInFlight)
    return;
#ifdef __EMSCRIPTEN__
  // Web: workerStarted is synthetic (set at boot to unlock the UI); the daemon
  // WS may not be connected yet, so a doc_new here would fail with a transient
  // "worker not started" toast (the exact boot error users hit). Wait for the
  // first successful bootstrap.
  if (engine::daemonMode() && !st.daemonEverConnected)
    return;
#else
  st.startupPhase = "Opening starter part\xe2\x80\xa6";
  if (!s_nativeStartup.engineReady) {
    if (!engine::daemonMode()) {
      // A successfully started legacy worker / in-process fallback is ready.
      s_nativeStartup.engineReady = true;
      startup_trace::mark("engine ready");
    } else {
      // Do not mint a part before bootstrap has checked the recovery entries.
      if (!st.daemonEverConnected)
        return;
      const auto now = std::chrono::steady_clock::now();
      if (s_nativeStartup.readinessPending ||
          now < s_nativeStartup.nextReadyPoll)
        return;
      s_nativeStartup.readinessPending = true;
      st.rpc.call(
          "daemon_status", nlohmann::json::object(),
          [&st](const core::RpcResult &r) {
            s_nativeStartup.readinessPending = false;
            s_nativeStartup.nextReadyPoll =
                std::chrono::steady_clock::now() +
                std::chrono::milliseconds(250);
            if (!r.ok || !r.result.is_object() || r.result.contains("code"))
              return;
            const std::string state =
                r.result.value("state", std::string());
            if (state == "ready") {
              s_nativeStartup.engineReady = true;
              startup_trace::mark("engine ready");
            } else if (state == "failed") {
              st.starterTried = true;
              st.startupPhase.clear();
              st.status = "Engine failed to load";
              st.statusKind = 2;
              st.toast(ui::ToastLevel::Error, st.status);
              st.addLog("[startup] engine failed to load in hostd");
            }
          },
          5000);
      return;
    }
  }
  // A sync mint must not queue behind another call's WorkerClient mutex.
  // Startup metadata pollers stay deferred while startupPhase is set.
  if (st.rpc.busy())
    return;
#endif
  static int s_readyFrame = -1;
  if (s_readyFrame < 0)
    s_readyFrame = ImGui::GetFrameCount();
  if (ImGui::GetFrameCount() < s_readyFrame + 1) {
    st.startupPhase = "Opening starter part\xe2\x80\xa6";
    return;
  }
  st.starterTried = true;
  st.startupPhase = "Opening starter part\xe2\x80\xa6";
  ui::createNewDoc(
      st,
      0); // New Part — same path as the chooser (incl. per-doc session setup)
  if (st.activeTab >= 0 && st.activeTab < (int)st.docTabs.size()) {
    st.docTabs[st.activeTab].isStarter = true;
#ifndef __EMSCRIPTEN__
    st.wantFocusViewport = 6;
    s_nativeStartup.starterOpenPending = true;
    st.addLog("[startup] opened default starter part");
#endif
  }
#ifdef __EMSCRIPTEN__
  st.wantFocusViewport = 6;
#endif
  st.startupPhase.clear();
#ifdef __EMSCRIPTEN__
  st.addLog("[startup] opened default starter part");
#endif
}

// Save one stable tab identity at a time. A dialog cancellation, failed RPC, or
// later human edit prevents quit; no queued save is treated as complete.
struct SaveAllRun : std::enable_shared_from_this<SaveAllRun> {
  struct Key { AppState::DocTab::TabKind kind; std::string id; };
  AppState &st;
  bool forQuit;
  std::vector<Key> tabs;
  size_t next = 0;
  int saved = 0;
  std::function<void(bool, int)> done;

  SaveAllRun(AppState &state, bool quit, std::function<void(bool, int)> callback)
      : st(state), forQuit(quit), done(std::move(callback)) {}

  static std::string idFor(const AppState::DocTab &tab) {
    using K = AppState::DocTab::TabKind;
    if (tab.kind == K::Drawing) return tab.drawingDocId;
    if (tab.kind == K::Schematic || tab.kind == K::PCB) return tab.ecadDocId;
    return tab.docId;
  }

  void advance() {
    while (next < tabs.size()) {
      const Key key = tabs[next++];
      int index = -1;
      for (int i = 0; i < (int)st.docTabs.size(); ++i)
        if (st.docTabs[i].kind == key.kind && idFor(st.docTabs[i]) == key.id) {
          index = i;
          break;
        }
      if (index < 0) continue;
      const auto &tab = st.docTabs[index];
      if ((forQuit && !tab.dirty) || (!tab.dirty && tab.path.empty()))
        continue;
      auto self = shared_from_this();
      ui::saveDocTab(st, index, false, [self](ui::TabSaveResult result) {
        if (result == ui::TabSaveResult::Saved) ++self->saved;
        if (result == ui::TabSaveResult::Cancelled || result == ui::TabSaveResult::Failed) {
          self->done(false, self->saved);
          return;
        }
        self->advance();
      });
      return;
    }
    bool clean = !forQuit || !st.hasPendingHumanMutations();
    if (forQuit)
      for (const auto &tab : st.docTabs) if (tab.dirty) clean = false;
    done(clean, saved);
  }
};

static void saveAllTabs(AppState &st, bool forQuit, std::function<void(bool, int)> done) {
  auto run = std::make_shared<SaveAllRun>(st, forQuit, std::move(done));
  // Saving the source first also captures its embedded drawings. Standalone
  // drawings are subsequently saved to their own paths.
  for (int drawings = 0; drawings < 2; ++drawings)
    for (const auto &tab : st.docTabs) {
      const bool drawing = tab.kind == AppState::DocTab::TabKind::Drawing;
      if (drawing != (drawings != 0) || (forQuit && !tab.dirty) ||
          (!tab.dirty && tab.aiHandoff && !tab.userAdopted)) continue;
      const std::string id = SaveAllRun::idFor(tab);
      if (!id.empty()) run->tabs.push_back({tab.kind, id});
    }
  run->advance();
}

// "Recover" in the orphan-recovery prompt: reopen the entries ONE AT A TIME on the async
// primary lane. The old synchronous recovery_open{all} parked the GL thread for the whole
// batch — 28 crash survivors including a large assembly froze the window for half an hour.
// Each completion queues the next, so the attach/tree calls a recovered tab's mirror issues
// get FIFO slots between opens and every tab comes alive as soon as its own doc is ready.
struct OrphanRecoveryRun {
  std::vector<AppState::RecoveryEntry> entries;
  size_t next = 0;
  int opened = 0;
  std::vector<std::string> failed;
};

static void recoverNextOrphan(AppState &st, std::shared_ptr<OrphanRecoveryRun> run) {
  if (run->next >= run->entries.size()) {
    st.recoveryInFlight = false;
    if (run->failed.empty()) {
      st.toast(ui::ToastLevel::Info, "Recovered " + std::to_string(run->opened) +
                                         " part" + (run->opened == 1 ? "" : "s"));
    } else {
      std::string names;
      for (const auto &n : run->failed)
        names += (names.empty() ? "" : ", ") + n;
      st.toast(ui::ToastLevel::Warn,
               "Recovered " + std::to_string(run->opened) + " of " +
                   std::to_string(run->entries.size()) + " parts — " + names +
                   " could not be reopened (they remain recoverable next launch)");
    }
    return;
  }
  const AppState::RecoveryEntry entry = run->entries[run->next++];
  st.rpc.callPrimary(
      "recovery_open", nlohmann::json{{"id", entry.id}},
      [&st, run, entry](const core::RpcResult &r) {
        std::vector<std::string> ids;
        bool diskConflict = false;
        nlohmann::json observedStamp;
        if (r.ok && r.result.is_object() && r.result.contains("opened") &&
            r.result["opened"].is_array()) {
          for (const auto &o : r.result["opened"]) {
            if (o.is_object() && o.contains("doc_id") && o["doc_id"].is_string()) {
              ids.push_back(o["doc_id"].get<std::string>());
              diskConflict = diskConflict || o.value("disk_conflict", false);
              if (o.value("disk_conflict", false))
                observedStamp = o.value("observed_stamp", nlohmann::json());
            }
          }
        }
        if (ids.empty()) {
          run->failed.push_back(entry.name);
          st.addLog("[recovery] " + entry.name + " not reopened: " +
                    (r.ok ? r.result.value("message", std::string("no document"))
                          : r.err));
        } else {
          core::mirrorRecoveredDocs(st, ids, entry.humanDirty, 0, entry.path,
                                    diskConflict, observedStamp);
          run->opened += static_cast<int>(ids.size());
        }
        recoverNextOrphan(st, run);
      },
      core::kLongEngineCallTimeoutMs);
}

namespace ui {
namespace {
struct ReferenceFetch {
  std::string docId, id, key;
  uint64_t generation = 0;
  int revision = -1;
  size_t offset = 0, total = 0;
  std::shared_ptr<Mesh> mesh = std::make_shared<Mesh>();
};

struct ReferenceMirror {
  MeshReferenceView view;
  int revision = -1;
  uint64_t generation = 0;
  bool listing = false;
  std::unordered_set<std::string> fetching;
  std::unordered_map<std::string, std::shared_ptr<Mesh>> cache;
  std::deque<std::string> cacheOrder;
};
ReferenceMirror& referenceMirror() {
  static ReferenceMirror mirror;
  return mirror;
}

std::string referenceKey(const nlohmann::json& ref) {
  return render::meshReferenceKey(ref);
}

void trimReferenceCache(ReferenceMirror& m) {
  std::unordered_set<std::string> activeKeys;
  for (const auto& ref : m.view.refs) activeKeys.insert(referenceKey(ref));
  const auto bytes = [](const Mesh& mesh) {
    return (mesh.positions.capacity() + mesh.normals.capacity()) * sizeof(float) +
           mesh.indices.capacity() * sizeof(uint32_t);
  };
  size_t totalBytes = 0;
  for (const auto& entry : m.cache) totalBytes += bytes(*entry.second);
  size_t candidates = m.cacheOrder.size();
  constexpr size_t kMaxCachedBytes = size_t(256) * 1024 * 1024;
  while ((m.cacheOrder.size() > 6 || totalBytes > kMaxCachedBytes) && candidates--) {
    std::string key = std::move(m.cacheOrder.front());
    m.cacheOrder.pop_front();
    if (activeKeys.count(key)) {
      m.cacheOrder.push_back(std::move(key));
    } else {
      auto it = m.cache.find(key);
      if (it != m.cache.end()) {
        totalBytes -= bytes(*it->second);
        m.cache.erase(it);
      }
    }
  }
}

bool referenceActive(const AppState& st, const std::string& doc) {
  return st.isActiveMirrorDoc() && st.activeTab >= 0 &&
      st.docTabs[st.activeTab].kind == AppState::DocTab::TabKind::Part &&
      st.docMirror.docId() == doc;
}

std::string referenceRpcError(const core::RpcResult& r, const char* fallback) {
  if (r.result.is_object()) {
    for (const char* key : {"message", "error", "reason"}) {
      const auto it = r.result.find(key);
      if (it != r.result.end() && it->is_string() && !it->get_ref<const std::string&>().empty())
        return it->get<std::string>();
    }
  }
  return r.err.empty() ? std::string(fallback) : r.err;
}

void publishReferenceMeshes(AppState& st) {
  auto& m = referenceMirror();
  if (!st.renderer) return;
  std::vector<Renderer::MeshReference> meshes;
  meshes.reserve(m.view.refs.size());
  for (const auto& ref : m.view.refs) {
    if (!ref.is_object()) continue;
    const std::string key = referenceKey(ref);
    auto found = m.cache.find(key);
    if (found == m.cache.end()) continue;
    Renderer::MeshReference item;
    item.id = ref.value("id", std::string());
    item.key = key;
    item.mesh = found->second;
    item.visible = ref.value("visible", true);
    item.opacity = std::clamp(ref.value("opacity", 0.4f), 0.0f, 1.0f);
    const auto color = ref.find("color");
    if (color != ref.end() && color->is_array() && color->size() == 3 &&
        (*color)[0].is_number() && (*color)[1].is_number() && (*color)[2].is_number()) {
      for (size_t i = 0; i < 3; ++i)
        item.color[i] = std::clamp((*color)[i].get<float>(), 0.0f, 1.0f);
    }
    const auto bbox = ref.find("bbox_mm");
    if (bbox != ref.end() && bbox->is_object() && bbox->contains("min") && bbox->contains("max") &&
        (*bbox)["min"].is_array() && (*bbox)["max"].is_array() &&
        (*bbox)["min"].size() == 3 && (*bbox)["max"].size() == 3) {
      item.hasBounds = true;
      for (size_t i = 0; i < 3; ++i) {
        if (!(*bbox)["min"][i].is_number() || !(*bbox)["max"][i].is_number()) {
          item.hasBounds = false;
          break;
        }
        item.bboxMin[i] = (*bbox)["min"][i].get<float>();
        item.bboxMax[i] = (*bbox)["max"][i].get<float>();
        if (!std::isfinite(item.bboxMin[i]) || !std::isfinite(item.bboxMax[i]))
          item.hasBounds = false;
      }
    }
    meshes.push_back(std::move(item));
  }
  st.renderer->setMeshReferences(std::move(meshes));
}

void referenceFailure(AppState& st, const std::shared_ptr<ReferenceFetch>& fetch,
                      const std::string& message) {
  auto& m = referenceMirror();
  if (fetch->generation != m.generation || m.view.docId != fetch->docId) return;
  m.fetching.erase(fetch->key);
  m.view.loading = m.listing || !m.fetching.empty();
  m.view.error = message;
  st.addLog("[reference] " + message);
}

void fetchReferencePage(AppState& st, const std::shared_ptr<ReferenceFetch>& fetch) {
  st.rpc.call("doc_mesh_ref_mesh",
              {{"doc_id", fetch->docId}, {"id", fetch->id},
               {"expected_revision", fetch->revision}, {"offset_tris", fetch->offset},
               {"max_tris", 10000}},
              [&st, fetch](const core::RpcResult& r) {
    auto& m = referenceMirror();
    if (fetch->generation != m.generation || m.view.docId != fetch->docId ||
        !referenceActive(st, fetch->docId)) return;
    if (!r.ok || !r.result.is_object() || !r.result.value("ok", false)) {
      const std::string code = r.result.is_object()
          ? r.result.value("code", std::string()) : std::string();
      if (code == "stale_revision" || r.err.find("stale_revision") != std::string::npos) {
        m.fetching.erase(fetch->key);
        m.revision = -1;
        refreshMeshReferences(st, fetch->docId);
      } else {
        referenceFailure(st, fetch, referenceRpcError(r, "Could not load reference mesh"));
      }
      return;
    }
    try {
      render::appendMeshReferencePage(*fetch->mesh, r.result, fetch->offset, fetch->total);
      if (fetch->offset < fetch->total) {
        fetchReferencePage(st, fetch);
        return;
      }
      m.cache[fetch->key] = fetch->mesh;
      m.cacheOrder.push_back(fetch->key);
      trimReferenceCache(m);
      m.fetching.erase(fetch->key);
      m.view.loading = m.listing || !m.fetching.empty();
      publishReferenceMeshes(st);
    } catch (const std::exception& e) {
      referenceFailure(st, fetch, std::string("Invalid reference mesh: ") + e.what());
    }
  });
}
}  // namespace

const MeshReferenceView& meshReferenceView() { return referenceMirror().view; }

bool meshReferenceLoaded(const std::string& id) {
  const auto& m = referenceMirror();
  for (const auto& ref : m.view.refs)
    if (ref.is_object() && ref.value("id", std::string()) == id)
      return m.cache.count(referenceKey(ref)) != 0;
  return false;
}

void tickMeshReferences(AppState& st) {
  auto& m = referenceMirror();
  const std::string doc = st.workerStarted && referenceActive(st, st.docMirror.docId())
      ? st.docMirror.docId() : std::string();
  if (doc == m.view.docId && (!doc.empty() && m.revision == st.docMirror.revision())) return;
  if (doc.empty() && m.view.docId.empty()) return;
  const bool switched = doc != m.view.docId;
  ++m.generation;
  m.fetching.clear();
  m.listing = false;
  m.revision = doc.empty() ? -1 : st.docMirror.revision();
  if (switched) {
    m.view.docId = doc;
    m.view.refs = nlohmann::json::array();
    m.view.error.clear();
    trimReferenceCache(m);
    publishReferenceMeshes(st);
  }
  if (doc.empty()) { m.view.loading = false; return; }
  m.view.loading = m.listing = true;
  const uint64_t generation = m.generation;
  st.rpc.call("doc_mesh_ref_list", {{"doc_id", doc}}, [&st, doc, generation](const core::RpcResult& r) {
    auto& current = referenceMirror();
    if (current.generation != generation || !referenceActive(st, doc)) return;
    current.listing = false;
    current.view.loading = false;
    if (!r.ok || !r.result.is_object() || !r.result.value("ok", false) ||
        !r.result.contains("refs") || !r.result["refs"].is_array()) {
      current.view.error = referenceRpcError(r, "Could not list scan references");
      st.addLog("[reference] " + current.view.error);
      return;
    }
    current.view.error.clear();
    current.view.refs = r.result["refs"];
    const int revision = r.result.value("revision", current.revision);
    publishReferenceMeshes(st);
    for (const auto& ref : current.view.refs) {
      if (!ref.is_object()) continue;
      const std::string key = referenceKey(ref);
      const std::string id = ref.value("id", std::string());
      const size_t triangles = ref.value("triangles", size_t(0));
      if (key.empty() || id.empty() || !triangles || current.cache.count(key) ||
          !current.fetching.insert(key).second) continue;
      auto fetch = std::make_shared<ReferenceFetch>();
      fetch->docId = doc; fetch->id = id; fetch->key = key;
      fetch->generation = generation; fetch->revision = revision; fetch->total = triangles;
      fetchReferencePage(st, fetch);
    }
    current.view.loading = !current.fetching.empty();
  });
}

void refreshMeshReferences(AppState& st, const std::string& docId) {
  auto& m = referenceMirror();
  if (m.view.docId == docId) m.revision = -1;
  st.rpc.call("doc_tree", {{"doc_id", docId}}, [&st, docId](const core::RpcResult& r) {
    if (!referenceActive(st, docId) || !r.ok || !r.result.is_object()) return;
    if (r.result.value("revision", -1) < st.docMirror.revision()) return;
    st.docMirror.ingestTreeResponse(r.result);
  });
}

void meshReferenceMutationCompleted(AppState& st, const std::string& docId, int revision) {
  // Tree-only native transactions advance the document revision without a new AICM.
  // Invalidate parked revision identities so tab switching cannot restore an older tree.
  st.markHumanEdit(docId, revision);
  for (auto& tab : st.docTabs) {
    if (tab.docId != docId) continue;
    tab.meshCacheInvalidated = true;
    if (tab.session) tab.session->cachedAicmPath.clear();
  }
  if (st.renderer) st.renderer->discardCachedMeshes(docId);
  refreshMeshReferences(st, docId);
}

void mutateMeshReference(AppState& st, const char* method, nlohmann::json params) {
  auto& m = referenceMirror();
  if (m.view.writing || !st.workerStarted || !referenceActive(st, st.docMirror.docId())) return;
  const std::string doc = st.docMirror.docId();
  params["doc_id"] = doc;
  m.view.error.clear();
  m.view.writing = true;
  st.beginHumanMutation(doc);
  st.rpc.call(method, std::move(params), [&st, doc](const core::RpcResult& r) {
    st.endHumanMutation(doc);
    auto& current = referenceMirror();
    current.view.writing = false;
    if (!r.ok || !r.result.is_object() || !r.result.value("ok", false)) {
      const std::string message = referenceRpcError(r, "Scan reference operation failed");
      if (current.view.docId == doc) current.view.error = message;
      st.toast(ToastLevel::Error, message);
      return;
    }
    meshReferenceMutationCompleted(st, doc, r.result.value("revision", 0));
  });
}
}  // namespace ui

// TEMP DIAGNOSTIC: forward decl for the vendor-wizard headless repro in
// run_gui.
namespace ui {
namespace parts {
void startLoginWizard(AppState &, const std::string &);
}
} // namespace ui

#ifdef __EMSCRIPTEN__
#include <GLFW/emscripten_glfw3.h>
// Shell owns the safe-area/visualViewport CSS rectangle; GLFW owns the
// DPR-scaled backing store and keeps its logical window in CSS pixels.
static void wasmSetupCanvasResize(GLFWwindow *win) {
  emscripten_glfw_make_canvas_resizable(win, "#app-surface", nullptr);
}

// ---- WASM mobile control scheme
// ---------------------------------------------------------------
// Narrow-viewport detection + on-screen HUD hooks (shell.html) + multitouch
// camera gestures. Desktop / wide viewports keep byte-identical behaviour;
// everything here is EMSCRIPTEN-only.
static bool g_wasmMobile = false;
static constexpr float kWasmMobileWidthPx = 900.0f;

// Pending HUD commands (posted from EMSCRIPTEN_KEEPALIVE exports on the browser
// thread; drained once per frame on the main loop so we never touch
// ImGui/AppState off-thread).
enum class WasmHudCmd : int {
  None = 0,
  SetDragOrbit,
  SetDragPan,
  ZoomIn,
  ZoomOut,
  Undo,
  ToggleFiles,
  ToggleChat,
};
static volatile int g_wasmHudCmd = 0; // WasmHudCmd

static bool wasmIsNarrowViewport(GLFWwindow *win) {
  int w = 0, h = 0;
  if (win)
    glfwGetWindowSize(win, &w, &h);
  if (w <= 0 || h <= 0) {
    double cssW = 0.0, cssH = 0.0;
    if (emscripten_get_element_css_size("#canvas", &cssW, &cssH) ==
        EMSCRIPTEN_RESULT_SUCCESS) {
      w = (int)(cssW + 0.5);
      h = (int)(cssH + 0.5);
    }
  }
  (void)h;
  // Also treat coarse-pointer devices as mobile even when the window is wide
  // (tablets landscape).
  int coarse = EM_ASM_INT({
    try {
      if (window.matchMedia && window.matchMedia('(pointer: coarse)').matches)
        return 1;
    } catch (e) {
    }
    return 0;
  });
  return (w > 0 && (float)w <= kWasmMobileWidthPx) || coarse != 0;
}

static void wasmApplyMobileLayout(AppState &st, bool mobile) {
  if (mobile == g_wasmMobile)
    return;
  static bool desktopTree = true, desktopChat = true;
  static bool desktopTreeCollapsed = false, desktopChatCollapsed = false;
  g_wasmMobile = mobile;
  if (mobile) {
    desktopTree = st.showFeatureTree;
    desktopChat = st.showAIPanel;
    desktopTreeCollapsed = st.settings.fmCollapsed;
    desktopChatCollapsed = st.settings.copilotCollapsed;
    st.showFeatureTree = false;
    st.showAIPanel = false;
  } else {
    st.showFeatureTree = desktopTree;
    st.showAIPanel = desktopChat;
    st.settings.fmCollapsed = desktopTreeCollapsed;
    st.settings.copilotCollapsed = desktopChatCollapsed;
    ui::resetOverlayColumns();
  }
}

static void wasmDrainHudCommands(AppState &st) {
  int cmd = g_wasmHudCmd;
  g_wasmHudCmd = 0;
  if (cmd == 0)
    return;
  switch ((WasmHudCmd)cmd) {
  case WasmHudCmd::SetDragOrbit:
    st.vpDragMode = 0;
    st.toast(ui::ToastLevel::Info, "Orbit mode");
    break;
  case WasmHudCmd::SetDragPan:
    st.vpDragMode = 1;
    st.toast(ui::ToastLevel::Info, "Pan mode");
    break;
  case WasmHudCmd::ZoomIn: {
    st.zoom *= 1.15f;
    const float maxZoom = st.renderer ? st.renderer->maxSceneZoom() : 10.0f;
    if (st.zoom > maxZoom)
      st.zoom = maxZoom;
    break;
  }
  case WasmHudCmd::ZoomOut: {
    st.zoom *= (1.0f / 1.15f);
    if (st.zoom < 0.1f)
      st.zoom = 0.1f;
    break;
  }
  case WasmHudCmd::Undo: {
    if (st.docMirror.hasDoc() && st.workerStarted) {
      auto r = st.docMirror.undo(st.worker);
      if (r.ok) {
        st.addLog("[doc] undo  rev=" + std::to_string(r.revision));
        if (!r.aicm_path.empty()) {
          std::string le;
          st.loadDocMesh(r.aicm_path, le);
        }
      } else {
        st.toast(ui::ToastLevel::Warn,
                 r.error_message.empty() ? "Nothing to undo" : r.error_message);
      }
    } else {
      st.toast(ui::ToastLevel::Info, "Nothing to undo");
    }
    break;
  }
  case WasmHudCmd::ToggleFiles:
  case WasmHudCmd::ToggleChat: {
    const bool files = (WasmHudCmd)cmd == WasmHudCmd::ToggleFiles;
    const bool show = !(files ? st.showFeatureTree : st.showAIPanel);
    if (files) {
      st.showFeatureTree = show;
      if (show) st.settings.fmCollapsed = false;
      if (g_wasmMobile) st.showAIPanel = false;
    } else {
      st.showAIPanel = show;
      if (show) st.settings.copilotCollapsed = false;
      if (g_wasmMobile) st.showFeatureTree = false;
    }
    if (show) ui::resetOverlayColumns();
    break;
  }
  default:
    break;
  }
}

// Browser events can arrive during ASYNCIFY yields. Queue them until the next
// NewFrame rather than mutating ImGui while a widget is running.
struct WasmTextEvent {
  std::string text;
  ImGuiKey key = ImGuiKey_None;
};
static std::deque<WasmTextEvent> g_wasmTextEvents;
static ImGuiKey g_wasmTextKeyDown = ImGuiKey_None;

static void wasmDrainTextInput() {
  ImGuiIO &io = ImGui::GetIO();
  // Moving focus to the browser IME is still focus inside this application.
  // GLFW reports canvas blur; without the matching focus event NewFrame drops
  // the first text batch while clearing input for the apparent focus loss.
  if (EM_ASM_INT({
        return document.hasFocus() &&
               document.activeElement === document.getElementById('mobile-input');
      }))
    io.AddFocusEvent(true);
  if (g_wasmTextKeyDown != ImGuiKey_None) {
    io.AddKeyEvent(g_wasmTextKeyDown, false);
    g_wasmTextKeyDown = ImGuiKey_None;
    return; // Release gets its own frame, including repeated Backspace.
  }
  if (g_wasmTextEvents.empty()) return;
  const auto &event = g_wasmTextEvents.front();
  if (event.key != ImGuiKey_None) {
    io.AddKeyEvent(event.key, true);
    g_wasmTextKeyDown = event.key;
  } else {
    io.AddInputCharactersUTF8(event.text.c_str());
  }
  // Text must be consumed before a following Enter can submit the widget.
  g_wasmTextEvents.pop_front();
}

static void wasmPublishTextInput() {
  const auto &ime = ImGui::GetCurrentContext()->PlatformImeData;
  const ImVec2 origin = ImGui::GetMainViewport()->Pos;
  const auto &context = *ImGui::GetCurrentContext();
  bool scrollable = false;
  const bool movingWindow = context.ActiveIdWindow &&
                           context.ActiveId == context.ActiveIdWindow->MoveId;
  // A list/menu button press must be cancellable into a scroll. Text editing
  // and directional drag/slider widgets retain their own gesture.
  const bool buttonPress = context.ActiveIdSource == ImGuiInputSource_Mouse &&
                           context.ActiveIdUsingNavDirMask == 0 &&
                           context.InputTextState.ID != context.ActiveId;
  if (context.ActiveId == 0 || movingWindow || buttonPress) {
    for (ImGuiWindow *window = context.HoveredWindow; window; window = window->ParentWindow) {
      if (window->ScrollMax.y > 0.0f &&
          (window->Flags & (ImGuiWindowFlags_ChildWindow | ImGuiWindowFlags_Popup)) &&
          !(window->Flags & ImGuiWindowFlags_NoScrollWithMouse)) {
        scrollable = true;
        break;
      }
    }
  }
  EM_ASM({
    window.__chudwareTouchScroll = !!$4;
    if (window.chudwareTextInput)
      window.chudwareTextInput(!!$0, $1, $2, $3);
  }, ImGui::GetIO().WantTextInput, ime.InputPos.x - origin.x,
     ime.InputPos.y - origin.y, ime.InputLineHeight, scrollable);
}

extern "C" {

EMSCRIPTEN_KEEPALIVE void chudware_mobile_set_drag_mode(int mode) {
  g_wasmHudCmd =
      (mode == 1) ? (int)WasmHudCmd::SetDragPan : (int)WasmHudCmd::SetDragOrbit;
}
EMSCRIPTEN_KEEPALIVE void chudware_mobile_zoom_step(int dir) {
  g_wasmHudCmd =
      (dir >= 0) ? (int)WasmHudCmd::ZoomIn : (int)WasmHudCmd::ZoomOut;
}
EMSCRIPTEN_KEEPALIVE void chudware_mobile_undo(void) {
  g_wasmHudCmd = (int)WasmHudCmd::Undo;
}
EMSCRIPTEN_KEEPALIVE void chudware_mobile_toggle_panel(int panel) {
  if (panel == 0 || panel == 1)
    g_wasmHudCmd = (int)(panel == 0 ? WasmHudCmd::ToggleFiles : WasmHudCmd::ToggleChat);
}
EMSCRIPTEN_KEEPALIVE void chudware_mobile_text(const char *utf8) {
  if (utf8 && *utf8)
    g_wasmTextEvents.push_back({utf8, ImGuiKey_None});
}
EMSCRIPTEN_KEEPALIVE void chudware_mobile_key(int key) {
  constexpr ImGuiKey keys[] = {ImGuiKey_Backspace, ImGuiKey_Enter, ImGuiKey_Escape};
  if (key >= 0 && key < 3)
    g_wasmTextEvents.push_back({{}, keys[key]});
}

} // extern "C"
#endif

// RE-40: GL content of the Reverse-Engineering 3D preview slots. kit::Viewport3DPreview queues
// this as a draw-list callback at the rect it reserves, so it runs INSIDE ImGui's draw pass in
// window order: windows drawn later (Settings, modals, popups, tooltips) cover it. It runs with
// ImGui's render state bound; the kit resets that state after each call.
static void paintRevEngPreviewSlot(ui::reveng::kit::PreviewSlot slot, const ImRect &rect,
                                   const ImRect &clip) {
  using ui::reveng::kit::PreviewSlot;
  const ImDrawData *dd = ImGui::GetDrawData();
  if (!dd) return;
  core::framestats::Scope timing("reverse_eng");
  ui::reveng::RevEngState &rs = ui::reveng::S();
  const ImVec2 org = dd->DisplayPos, fbs = dd->FramebufferScale;
  const int dw = (int)(dd->DisplaySize.x * fbs.x);
  const int dh = (int)(dd->DisplaySize.y * fbs.y);
  // Map an ImGui screen rect (top-left origin, logical px) onto the GL
  // viewport (bottom-left origin, framebuffer px). Returns false when the
  // rect is degenerate or fully off-screen.
  auto toFramebuffer = [&](const ImRect &r, int &x, int &y, int &w, int &h) {
    x = (int)((r.Min.x - org.x) * fbs.x);
    w = (int)((r.Max.x - r.Min.x) * fbs.x);
    h = (int)((r.Max.y - r.Min.y) * fbs.y);
    y = dh - (int)((r.Max.y - org.y) * fbs.y); // flip Y
    return w > 1 && h > 1 && x < dw && y < dh && x + w > 0 && y + h > 0;
  };
  int vx = 0, vy = 0, vw = 0, vh = 0, cx = 0, cy = 0, cw = 0, ch = 0;
  if (!toFramebuffer(rect, vx, vy, vw, vh) || !toFramebuffer(clip, cx, cy, cw, ch))
    return;
  glViewport(vx, vy, vw, vh);
  glEnable(GL_SCISSOR_TEST);
  glScissor(std::max(0, cx), std::max(0, cy), std::min(dw, cx + cw) - std::max(0, cx),
            std::min(dh, cy + ch) - std::max(0, cy));
  // ImGui clears color only. Stale default-framebuffer depth must not
  // occlude this independent preview; never clear outside its slot.
  GLboolean previousDepthMask = GL_TRUE;
  glGetBooleanv(GL_DEPTH_WRITEMASK, &previousDepthMask);
  glDepthMask(GL_TRUE);
  const GLfloat clearDepth = 1.0f;
  glClearBufferfv(GL_DEPTH, 0, &clearDepth);
  glDepthMask(previousDepthMask);

  // Mesh and built-document slots use the same shaded CAD thumbnail pass
  // as Active Work, cached until the artifact or viewport size changes.
  struct ReThumbnail {
    unsigned int tex = 0;
    int width = 0, height = 0;
    uint64_t revision = 0;
    bool attempted = false;
  };
  static ReThumbnail meshThumb, parametricThumb;
  static GLuint reReadFbo = 0;
  auto drawDocument = [&](const std::shared_ptr<render::DocMesh> &doc, uint64_t revision,
                          ReThumbnail &thumb, std::string &error) {
    if (!doc || doc->empty()) return;
    const int w = std::clamp(vw, 16, 2048);
    const int h = std::clamp(vh, 16, 2048);
    if (!thumb.attempted || thumb.width != w || thumb.height != h ||
        thumb.revision != revision) {
      thumb.width = w; thumb.height = h; thumb.revision = revision;
      thumb.attempted = true;
      std::string drawError;
      if (!render::renderDocThumbnail(*doc, w, h, render::DocThumbStyle{}, thumb.tex,
                                      drawError))
        error = drawError.empty() ? "Cannot render document preview." : drawError;
      else
        error.clear();
    }
    if (!thumb.tex || !error.empty()) return;
    GLint previousRead = 0;
    glGetIntegerv(GL_READ_FRAMEBUFFER_BINDING, &previousRead);
    if (!reReadFbo) glGenFramebuffers(1, &reReadFbo);
    glBindFramebuffer(GL_READ_FRAMEBUFFER, reReadFbo);
    glFramebufferTexture2D(GL_READ_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, thumb.tex, 0);
    if (glCheckFramebufferStatus(GL_READ_FRAMEBUFFER) == GL_FRAMEBUFFER_COMPLETE)
      glBlitFramebuffer(0, h, w, 0, vx, vy, vx + vw, vy + vh, GL_COLOR_BUFFER_BIT, GL_LINEAR);
    glFramebufferTexture2D(GL_READ_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, 0, 0);
    glBindFramebuffer(GL_READ_FRAMEBUFFER, previousRead);
  };

  switch (slot) {
  case PreviewSlot::Splat: // RE-27: 3DGS splat (Brush export)
    if (rs.splatLoaded && rs.splat.hasSplats()) {
      const Renderer::CameraXform cam = ui::reveng::gaussianPreviewCamera(vw, vh);
      rs.splat.draw(cam);
      if (rs.stage == ui::reveng::Stage::Scale && rs.splatPickMarks.hasPoints())
        rs.splatPickMarks.draw(cam);
    }
    break;
  case PreviewSlot::SparseCloud: // RE-21: sparse point cloud + camera frusta
    if (rs.rePoints.hasPoints() || rs.rePoints.hasFrusta())
      rs.rePoints.draw(ui::reveng::sparsePreviewCamera(vw, vh));
    break;
  case PreviewSlot::Mesh:
    drawDocument(rs.meshPreview, rs.meshPreviewRevision, meshThumb, rs.meshPreviewError);
    break;
  case PreviewSlot::Parametric:
    drawDocument(rs.parametricPreview, rs.parametricPreviewRevision, parametricThumb,
                 rs.parametricPreviewError);
    break;
  case PreviewSlot::None:
  case PreviewSlot::Deviation: // painted by ImGui (dim_review paintErrorField)
    break;
  }
}

int run_gui(const char *screenshotPath, const char *openPath) {
  core::framestats::ReportOnExit frameStatsReport;
  startup_trace::mark("run_gui entry");
#ifndef __EMSCRIPTEN__
  s_nativeStartup = {};
#endif
  WASM_BOOT_LOG("[wasm-boot] run_gui enter\n");
  const bool shot = screenshotPath != nullptr;
#ifdef __EMSCRIPTEN__
  if (!glfwInit())
    return 1;
#elif !defined(_WIN32)
  // Native snap/tear-off for the borderless custom chrome is delegated through
  // X11's EWMH move/resize protocol. Prefer X11 (or XWayland) because Wayland
  // intentionally exposes no client-side API for starting those compositor grabs;
  // fall back to the default platform with the manual, non-snapping chrome path.
  glfwInitHint(GLFW_PLATFORM, GLFW_PLATFORM_X11);
  if (!glfwInit()) {
    glfwInitHint(GLFW_PLATFORM, GLFW_ANY_PLATFORM);
    if (!glfwInit())
      return 1;
  }
#else
  if (!glfwInit())
    return 1;
#endif
#ifdef __EMSCRIPTEN__
  // CLIENT_API must be set before version hints (contrib.glfw3 / WebGL).
  glfwWindowHint(GLFW_CLIENT_API, GLFW_OPENGL_ES_API);
  glfwWindowHint(GLFW_CONTEXT_VERSION_MAJOR, 3);
  glfwWindowHint(GLFW_CONTEXT_VERSION_MINOR, 0);
  // Default-framebuffer antialias fallback (contrib.glfw3 maps GLFW_SAMPLES ->
  // WebGL context attribute `antialias`). Scene AA is the offscreen MSAA FBO in
  // renderer.cpp; this still smooths any default-FB draws (ImGui) if the
  // browser honors it.
  glfwWindowHint(GLFW_SAMPLES, 4);
  // Do not request CORE_PROFILE — that is desktop-GL and confuses WebGL.
#else
  glfwWindowHint(GLFW_CONTEXT_VERSION_MAJOR, 3);
  glfwWindowHint(GLFW_CONTEXT_VERSION_MINOR, 3);
  glfwWindowHint(GLFW_OPENGL_PROFILE, GLFW_OPENGL_CORE_PROFILE);
#ifdef __APPLE__
  // macOS refuses any core profile >= 3.2 without this: NSGL only exposes 3.2+
  // through the forward-compatible path. Without it glfwCreateWindow returns
  // null below and run_gui() exits 1. Same hint as makeContext() in main.cpp.
  glfwWindowHint(GLFW_OPENGL_FORWARD_COMPAT, GL_TRUE);
#endif
#endif
  if (!shot)
    glfwWindowHint(GLFW_MAXIMIZED, GLFW_TRUE);
#if !defined(_WIN32) && !defined(__EMSCRIPTEN__)
  // Borderless: the app draws its own title bar; chrome::tick handles
  // move/resize. (Windows creates a normal window and strips the frame later
  // via the WndProc in chrome::install.)
  glfwWindowHint(GLFW_DECORATED, GLFW_FALSE);
#endif
  // Dev/test hook: CHUDWARE_HIDDEN=1 creates the window unmapped so
  // headless shot runs never enter the compositor's present path (on an
  // unattended desktop the compositor can starve a new window's swaps
  // indefinitely — observed as multi-minute drmSyncobjTimelineWait stalls). GL
  // rendering + glReadPixels work as normal on the hidden window's framebuffer.
  // Shot-mode only.
  if (shot && std::getenv("CHUDWARE_HIDDEN"))
    glfwWindowHint(GLFW_VISIBLE, GLFW_FALSE);
  // Stable window class / app_id so the desktop shell (KDE/GNOME) matches the
  // window to the installed .desktop entry (StartupWMClass=chudware) and shows
  // the app icon in the taskbar instead of a generic placeholder. Without this,
  // GLFW's WM_CLASS falls back to the window title, which may match no .desktop
  // entry. Harmless on Windows (no-op hints). Must match chudware.desktop.
  glfwWindowHintString(GLFW_X11_CLASS_NAME, "chudware");
  glfwWindowHintString(GLFW_X11_INSTANCE_NAME, "chudware");
  glfwWindowHintString(GLFW_WAYLAND_APP_ID, "chudware");
  // CHUDWARE_WIN_SIZE=WxH: explicit window geometry for managed/headless sessions
  // (chudware-web streams a fixed-size Xvfb display with no WM, so maximize hints
  // are no-ops there — the window must be created at display size and at 0,0).
  int winW = shot ? 1340 : 1440, winH = shot ? 838 : 900;
  bool winSized = false;
  if (const char *ws = std::getenv("CHUDWARE_WIN_SIZE")) {
    int w = 0, h = 0;
    if (std::sscanf(ws, "%dx%d", &w, &h) == 2 && w >= 640 && h >= 480 &&
        w <= 16384 && h <= 16384) {
      winW = w;
      winH = h;
      winSized = true;
    }
  }
  GLFWwindow *win = glfwCreateWindow(winW, winH, "Chudware", nullptr, nullptr);
#ifdef __EMSCRIPTEN__
  WASM_BOOT_LOG("[wasm-boot] glfwCreateWindow %s\n", win ? "ok" : "FAIL");
#endif
  if (!win) {
    glfwTerminate();
    return 1;
  }
  startup_trace::mark("GLFW window created");
  if (winSized)
    glfwSetWindowPos(win, 0, 0);
  glfwMakeContextCurrent(win);
  glfwSwapInterval(1);
#ifndef __EMSCRIPTEN__
  if (!gladLoadGL((GLADloadfunc)glfwGetProcAddress))
    return 1;
#endif
#ifdef __EMSCRIPTEN__
  std::fprintf(stderr, "[wasm-boot] GL context current\n");
  wasmSetupCanvasResize(win);
#endif
#ifndef __EMSCRIPTEN__
  engine::GuiConnection guiConnection(ui::anim::wakeLoop);
#endif

  // CEF (when the cef backend is selected) is initialised early in main(),
  // before GLFW — see main.cpp. Nothing to do here.

#ifdef __EMSCRIPTEN__
  const std::string assets = "/assets";
#else
  const std::string assets =
      exeDir() + "/assets"; // '/' is valid on Windows + POSIX
#endif
  setWindowIcon(win, assets + "/logo_icon.png");

  IMGUI_CHECKVERSION();
  ImGui::CreateContext();
  ImGuiIO &io = ImGui::GetIO();
  io.ConfigFlags |= ImGuiConfigFlags_DockingEnable;
  // GUI rework layout schema. The old imgui.ini can contain dock IDs/window
  // placements from pre-rework names, which restores the FeatureManager into
  // the center and leaves Viewport as a floating orphan. Use a versioned ini so
  // stale dock layouts cannot override buildDefaultLayout. v4 introduced the
  // full-bleed overlay columns; v5 moved CAM into a bottom dock split. v6 removes
  // that split entirely: Manufacture keeps the native 3D viewport edge to edge,
  // with its job/output controls in the pinned left column. Rolling the schema
  // prevents a saved v5 "CAM" dock node from permanently stealing viewport space.
  // The ini is also the FeatureManager column's width persistence; the Copilot column is
  // NoResize and persists its width as Settings::copilotWidth ("copilot_width").
  static std::string iniPath = appdataAppDir() + "/imgui_gui_v6.ini";
  io.IniFilename = shot ? nullptr : iniPath.c_str();

#ifdef __EMSCRIPTEN__
  // GLFW's browser content scale is DPR. ImGui's display/mouse space is CSS
  // pixels, so DPR must affect only the framebuffer, never fonts or metrics.
  float scale = 1.0f;
#else
  float xs = 1.0f, ys = 1.0f;
  glfwGetWindowContentScale(win, &xs, &ys);
  float scale = shot ? 1.0f : (xs > ys ? xs : ys);
#endif
  // CHUDWARE_SCALE overrides the content scale (incl. in screenshot mode) so
  // HiDPI layout can be reproduced/verified headlessly. Clamped to a sane
  // range.
  if (const char *envScale = std::getenv("CHUDWARE_SCALE")) {
    float v = (float)std::atof(envScale);
    if (v >= 0.5f && v <= 4.0f)
      scale = v;
  }

  // Theme: honour CHUDWARE_THEME env override, then persisted setting, then LIGHT
  // default (since 2026-07-14). Applied once here; the settings load at
  // first frame re-applies the persisted choice, and the View-menu toggle calls
  // applyTheme() again.
  {
    const char *envTheme = std::getenv("CHUDWARE_THEME");
    ui::Theme initTheme = ui::Theme::Light;
    if (envTheme && std::strcmp(envTheme, "dark") == 0)
      initTheme = ui::Theme::Dark;
    ui::applyTheme(initTheme, scale);
  }
  ui::Fonts fonts = ui::loadFonts(assets, scale);
  startup_trace::mark("fonts loaded");
  WASM_BOOT_LOG("[wasm-boot] loadFonts done\n");
  ImGui_ImplGlfw_InitForOpenGL(win, true);
#ifdef __EMSCRIPTEN__
  ImGui_ImplOpenGL3_Init("#version 300 es");
  std::fprintf(stderr, "[wasm-boot] ImGui_ImplOpenGL3_Init done\n");
#else
  ImGui_ImplOpenGL3_Init("#version 330");
  {
    // Device objects alone do not force drivers such as llvmpipe to JIT the
    // blend/vertex-state variant. Submit one unswapped startup pixel as well.
    ImGui_ImplOpenGL3_CreateDeviceObjects();
    ImDrawList warmDraw(ImGui::GetDrawListSharedData());
    warmDraw._ResetForNewFrame();
    warmDraw.PushClipRect(ImVec2(0, 0), ImVec2(1, 1));
    warmDraw.AddImage(io.Fonts->TexID, ImVec2(0, 0), ImVec2(1, 1));
    // Font/icon/viewport textures can have different power-of-two axes. Warm
    // the four clamp/linear sampler shapes, not only the font-atlas variant.
    GLuint warmTextures[4]{};
    glGenTextures(4, warmTextures);
    const unsigned char pixels[3 * 3 * 4]{};
    for (int i = 0; i < 4; ++i) {
      glBindTexture(GL_TEXTURE_2D, warmTextures[i]);
      glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, 2 + (i & 1), 2 + ((i >> 1) & 1),
                   0, GL_RGBA, GL_UNSIGNED_BYTE, pixels);
      glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
      glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
      glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
      glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
      warmDraw.AddImage((ImTextureID)(intptr_t)warmTextures[i], ImVec2(0, 0), ImVec2(1, 1));
    }
    ImDrawData warmData;
    warmData.Valid = true;
    warmData.DisplaySize = ImVec2(1, 1);
    warmData.FramebufferScale = ImVec2(1, 1);
    warmData.AddDrawList(&warmDraw);
    ImGui_ImplOpenGL3_RenderDrawData(&warmData);
    glFinish();
    glDeleteTextures(4, warmTextures);
  }
#endif
  ui::reveng::kit::setPreviewSlotPainter(&paintRevEngPreviewSlot);

  // Borderless window: strip the OS title bar/frame and drive our own
  // (drag/resize/snap stay native). Must be installed after the ImGui GLFW
  // backend so our WndProc is on top.
  static chrome::HitLayout hitLayout;
  hitLayout.resizeBorder = (int)(6 * scale);
  chrome::install(win, &hitLayout);

  Tex wordmark = loadTexture(assets + "/logo_wordmark.png");
  Tex wordmarkDark = loadTexture(assets + "/logo_wordmark_dark.png");
  Tex logoIcon = loadTexture(assets + "/logo_icon.png");

  std::string rerr;
  Renderer renderer;
  renderer.init(960, 720, rerr);
  startup_trace::mark("renderer init");
  // Viewport visuals driven by palette roles so they switch with the theme.
  // These are cheap uniform/state sets re-pushed every frame in the main loop
  // (see below) so a runtime theme toggle takes effect immediately; this
  // initial push covers the first headless frame.
  {
    float top3[3] = {ui::palette.viewportTop[0], ui::palette.viewportTop[1],
                     ui::palette.viewportTop[2]};
    float mid3[3] = {ui::palette.viewportMid[0], ui::palette.viewportMid[1],
                     ui::palette.viewportMid[2]};
    float bot3[3] = {ui::palette.viewportBottom[0],
                     ui::palette.viewportBottom[1],
                     ui::palette.viewportBottom[2]};
    renderer.setBackground3(top3, mid3, bot3);
    renderer.setEdgeStyle(ui::palette.viewportEdge[0],
                          ui::palette.viewportEdge[1],
                          ui::palette.viewportEdge[2], 1.6f * ui::uiScale);
    renderer.setDefaultPartColor(ui::palette.partDefault[0],
                                 ui::palette.partDefault[1],
                                 ui::palette.partDefault[2]);
    renderer.setGroundShadow(
        ui::palette.groundShadow[0], ui::palette.groundShadow[1],
        ui::palette.groundShadow[2], ui::palette.groundShadow[3]);
    renderer.setPlaneColors(ui::palette.planeFill, ui::palette.planeOutline);
  }

  // C14: load icon atlases (stb_image; must be after GL context is live).
  ui::loadIcons(assets);
  startup_trace::mark("icons loaded");
  WASM_BOOT_LOG("[wasm-boot] loadIcons done\n");

  // W1-B: create the GLFW standard cursors once (consumed per-frame via
  // st.requestCursor).
  ui::initCursors(win);

  // C17: the ribbon callback (PM dispatcher) is registered after AppState is
  // created so it can capture &st and the PropertyManager (see below). C14's
  // default printf handler is replaced.

  // Topology picking (C15): id pass + overlays live for the GUI's lifetime;
  // AppState borrows them. They stay inert until a doc mesh (AICM) is loaded.
  // Highlight colors track theme roles.
  render::IdPass idPass;
  render::Overlays overlays;
  {
    // C37-C feature-highlight wash follows the theme (dimmer selection tint)
    // instead of the baked default; re-pushed every frame below so theme
    // switches track automatically.
    float feat[4];
    ui::Palette::withAlpha(ui::palette.selectionHighlight, feat, 0.30f);
    overlays.setColors(ui::palette.hoverHighlight,
                       ui::palette.selectionHighlight, feat);
  }

  // ---------- application state (bundles what were run_gui locals) ----------
  AppState st;
  st.mfgMirror = std::make_shared<ui::MfgMirror>();
  st.win = win;
  st.shot = shot;
  st.scale = scale;
  // "Open with" / file association (task 05): a document handed in on the
  // command line is queued on the same deferred open pump the menu / OS
  // file-drop use. The pump holds an open request pending until the worker is
  // up (it only drops save/export when busy), so this opens cleanly even though
  // the worker hasn't started yet at this point.
  if (openPath && openPath[0]) {
    st.requestOpenPath = openPath;
    st.addLog(std::string("[open-with] launch arg -> ") + openPath);
  }
  st.assets = assets;
  st.fonts = fonts;
  st.wordmark = wordmark;
  st.wordmarkDark = wordmarkDark;
  st.logoIcon = logoIcon;
  st.renderer = &renderer;
  st.hitLayout = &hitLayout;
  st.idPass = &idPass;
  st.overlays = &overlays;

  // Wave 3A item 5: OS file drop. Expose AppState to the static drop callback
  // via the GLFW window user pointer (unused elsewhere), then install
  // onFileDrop. glfwSetDropCallback returns the previously-installed callback
  // (ImGui's GLFW backend registered one during InitForOpenGL); we chain to it
  // from onFileDrop so the backend contract is preserved.
  glfwSetWindowUserPointer(win, &st);
  g_prevDropCallback = glfwSetDropCallback(win, onFileDrop);
#ifdef __EMSCRIPTEN__
  chudware_install_canvas_drop();
#endif

  // Multi-document FALLBACK singletons. PropertyManager / SketchMode /
  // EvaluateDialogs are now owned PER-DOCUMENT on each DocSession;
  // st.propertyManager / sketchMode / evaluateDialogs are re-seated at the
  // active document's instances on every tab switch (reseatActiveSubsystems).
  // These run_gui-owned objects are the FALLBACK used at the empty start state
  // and on a Drawing / ECAD tab (no CAD document active). st.* default to them
  // so every borrow site is valid before the first document opens. NOTE: the
  // dispatcher + frame loop below go through st.propertyManager / st.sketchMode
  // / st.evaluateDialogs (NOT these locals) so they act on the ACTIVE document.
  ui::PropertyManager propertyManager;
  st.propertyManager = &propertyManager;
  st.fallbackPropertyManager = &propertyManager;

  // C19: SimClient (worker #2, single shared solver) + Evaluate dialogs — owned
  // here. The per-document SimManagers live on each DocSession; the fallback
  // below covers the empty start state / non-CAD tabs.
  core::SimClient simClient;
  st.simClient = &simClient;
  ui::SimManager fallbackSimManager;
  st.fallbackSimManager = &fallbackSimManager;
  st.simManager = &fallbackSimManager;
  ui::EvaluateDialogs evaluateDialogs; // fallback Measure/Mass/Section dialogs
                                       // (per-doc otherwise)
  st.evaluateDialogs = &evaluateDialogs;
  st.fallbackEvaluate = &evaluateDialogs;

  // C18: SketchMode — owned here, borrowed via st.sketchMode (opaque void*).
  ui::SketchMode sketchMode;
  st.sketchMode = &sketchMode;
  st.fallbackSketchMode = &sketchMode;

  // 3D sketcher (sketch3d features) — app-wide singleton (guards its doc id
  // itself).
  ui::Sketch3DMode sketch3dMode;
  st.sketch3dMode = &sketch3dMode;

  // Configurations panel (SolidWorks ConfigurationManager) — owned here. The
  // Configurations left-pane tab renders it (ConfigPanel::drawEmbedded) via
  // st.configPanel; there is no standalone dock window for it anymore.
  ui::ConfigPanel configPanel;
  st.configPanel = &configPanel;

  // Design Table panel (SolidWorks editable config grid) — owned here, drawn
  // each frame.
  ui::DesignTablePanel designTablePanel;

  // Weldment Cut List panel (SolidWorks Weldment Cut List) — owned here, drawn
  // each frame.
  ui::CutListPanel cutListPanel;

  // --- C20b: AsmPanels — owned here, borrowed via st.asmPanels (opaque void*).
  // ---
  ui::AsmPanels asmPanels;
  st.asmPanels = &asmPanels;
  // --- end C20b ---

  // C17/C18: SINGLE ribbon-command dispatcher. The ribbon callback and
  // st.ribbonDispatch (used by the shortcut bar) route through
  // the SAME lambda so the two paths can never drift apart — they were
  // previously hand-duplicated (Wave 1B unification).
  //   - App-level dispatch checks shared availability before side effects,
  //     then handles prof_toggle and the stub log.
  //   - every command FAMILY (sketch, edit_*, dwg_, display_/appr_,
  //   asm_, ecad_/sch_/pcb_,
  //     cam_, reveng_, PM forms, sm_, sim_, eval_) lives in an ordered rule
  //     table built by buildRibbonCommandRouter (app_commands.cpp);
  //     registration order reproduces the original if-chain exactly, including
  //     its fall-through cases.
  auto dispatchCmd = [&st](const char *cmd_id) {
    static const CommandRouter router = buildRibbonCommandRouter(
        {cmdEditCopy, cmdEditCut, cmdEditPaste, cmdEditDelete, cmdEditSelectAll,
         cmdMakeDrawing, cmdDisplayStyle, cmdAppearance, cmdFlatPattern,
         cmdFlatDxf, ecadDispatch});
    if (!cmd_id || !*cmd_id)
      return;
    const ui::CommandContext context = ui::commandContext(st);
    const ui::CommandAvailability availability = ui::commandAvailability(cmd_id, context);
    if (!availability.visible || !availability.enabled) {
      st.toast(ui::ToastLevel::Info,
               availability.reason ? availability.reason : "Command unavailable in this context");
      return;
    }
    // Multi-document: handlers resolve the PropertyManager / SketchMode /
    // EvaluateDialogs of the ACTIVE document through the AppState& they are
    // handed (st.* are re-seated on every tab switch), so no per-call shadowing
    // locals are needed here anymore. Evaluate > Profiler is global and
    // document-independent.
    if (std::strcmp(cmd_id, "prof_toggle") == 0) {
      st.showProfilerWindow = true;
      return;
    }
    // Family routing lives in the ordered rule table
    // (app_commands.cpp). Built once; the rules are stateless and resolve every
    // subsystem through the AppState& at call time. The app-level family
    // handlers (cmdEdit*/cmdDisplayStyle/cmdAppearance/...) stay in this file
    // and are handed over via RibbonCommandHooks.
    // Family routing uses the ordered router built above.
    if (router.dispatch(st, cmd_id, context))
      return;
    // Commands without a handler log a stub line so they stay observable until
    // later cards wire them.
    std::printf("[ribbon] %s (no PM form)\n", cmd_id);
    std::fflush(stdout);
    st.addLog(std::string("[ribbon] ") + cmd_id + " (no PM form yet)");
  };
  ui::setRibbonCallback(dispatchCmd);
  // C25: expose the same dispatcher to UI widgets (shortcut bar, gesture
  // wheel).
  st.ribbonDispatch = dispatchCmd;

  loadSettings(st.settings);
  startup_trace::mark("settings loaded");
  st.orthographic =
      st.settings
          .orthographic; // live projection mode mirrors the persisted setting
  // Viewport display toggles, View-menu panel visibility and the sketch defaults come back as the
  // user left them. Screenshot/automation runs keep the built-in defaults (and never persist).
  if (!shot) {
    st.seedViewPrefsFromSettings();
    sketchMode.applyPreferences(st.settings.sketchShowGrid,
                                st.settings.sketchAutoRelations);
  }
  // Liquid glass v2.1: install + resolve the persisted appearance config before
  // the first frame (idempotent; re-pushed every frame in the loop below so
  // runtime mutations self-heal).
  ui::setGlassConfig(st.settings.glassCfg);
  // User accent pair: writes the palette + accent-derived ImGuiCol slots and
  // retints the baked ribbon atlas. Ordered after loadIcons (line ~1939) so the
  // atlas is decoded and the first retint actually lands.
  ui::setAccentColors(st.settings.accentPrimary, st.settings.accentSecondary);
  // editor buffer for the system-prompt override (Settings > Agent)
  std::strncpy(st.spBuf.data(), st.settings.systemPrompt.c_str(),
               st.spBuf.size() - 1);

  // Seed live Explorer/Project Hub state from persisted Settings. Recents are
  // mirrored here once, then pushRecentFile/pushRecentProject keep both copies
  // synchronized.
  st.recentFiles = st.settings.recentFiles;
  st.recentProjects = st.settings.recentProjects;
  st.workspaceDir = st.settings.workspaceDir;
  ui::ensureWorkspaceDir(st);

  // Re-apply theme from persisted settings (env override already applied above;
  // if a saved setting differs, the persisted value wins unless env was set).
  {
    const char *envTheme = std::getenv("CHUDWARE_THEME");
    if (!envTheme) {
      // No env override — use the saved setting.
      ui::Theme t =
          (st.settings.theme == "light") ? ui::Theme::Light : ui::Theme::Dark;
      if (t != ui::activeTheme)
        ui::applyTheme(t, scale);
    }
  }
#ifdef __EMSCRIPTEN__
  wasmApplyMobileLayout(st, wasmIsNarrowViewport(win));
#endif

  // Ordinary viewport/Measure picks resolve off the GL thread. Editing wells keep
  // their established routing, but never wait behind a busy async engine call.
  st.selection.setObserver([&st]() {
    // Viewport -> FeatureManager: light + scroll to the newest pick's owning row NOW, from the
    // mirror's owner map. This used to wait for the doc_resolve_pick round-trip and ran only on
    // its success path, so a busy FIFO, a revision drift or a resolve error left the tree unsynced.
    for (auto it = st.selection.items().rbegin(); it != st.selection.items().rend(); ++it) {
      if (!it->pending_resolve || it->resolve_request != 0 || !it->hit.valid()) continue;
      ui::featureTreeRevealPick(st, it->hit);
      break;
    }
    if (!st.docMirror.hasDoc() || !st.workerStarted) {
      for (auto &item : st.selection.mutableItems()) {
        if (!item.pending_resolve) continue;
        item.pending_resolve = false;
        item.resolution_error = "No connected document is available to resolve this selection.";
      }
      return;
    }
    bool simPickConsumed =
        false; // a sim face pick was routed into a fixture/load well this pass
    // Picks a PropertyManager well consumed (added, toggled off or rejected) and stale deferred
    // picks. The well draws its own picks, so a pick it does not hold must not stay highlighted
    // as selected: an orange edge beside an empty Edges well read as a pick the form lost.
    std::vector<render::PickHit> released;
    for (auto &item : st.selection.mutableItems()) {
      if (!item.pending_resolve || item.resolve_request != 0 || !item.hit.valid())
        continue;
      item.resolution_error.clear();
      const char *kindStr = item.hit.kind == render::PickKind::Face   ? "face"
                            : item.hit.kind == render::PickKind::Edge ? "edge"
                            : item.hit.kind == render::PickKind::Vertex
                                ? "vertex"
                                : nullptr;
      if (!kindStr) {
        item.pending_resolve = false;
        item.resolution_error = "This topology kind cannot be resolved.";
        continue;
      }
      // A deferred pick's topology ids belong to the revision it was clicked on.
      if (item.deferred_revision >= 0 && item.deferred_revision != st.docMirror.revision()) {
        item.pending_resolve = false;
        item.resolution_error = "The document changed while resolving this pick. Select it again.";
        st.toast(ui::ToastLevel::Info, item.resolution_error);
        released.push_back(item.hit);
        continue;
      }
      const bool routedPick =
          (st.propertyManager && st.propertyManager->active()) ||
          (st.asmPanels && (st.asmPanels->mateFormActive() || st.asmPanels->patternFormActive())) ||
          (st.simManager && st.simManager->wantsViewportFacePick(st));
      if (!routedPick) {
        static uint64_t nextPickRequest = 0; // GL-thread only; never recycled across tabs
        const uint64_t request = item.resolve_request = ++nextPickRequest;
        const auto hit = item.hit;
        const std::string docId = st.docMirror.docId();
        const int revision = st.docMirror.revision();
        st.rpc.call("doc_resolve_pick",
            {{"doc_id", docId}, {"revision", revision}, {"body_id", hit.body_id},
             {"kind", kindStr}, {"topo_id", hit.topo_id}},
            [&st, request, hit, docId, revision](const core::RpcResult& reply) {
              std::vector<core::SelItem>* items = nullptr;
              int currentRevision = -1;
              const bool active = st.docMirror.docId() == docId;
              if (active) {
                items = &st.selection.mutableItems();
                currentRevision = st.docMirror.revision();
              } else {
                for (auto& tab : st.docTabs) {
                  if (tab.docId != docId || !tab.session) continue;
                  items = &tab.session->selItems;
                  currentRevision = tab.session->cachedRevision;
                  break;
                }
              }
              if (!items) return; // originating document was closed
              for (auto& selected : *items) {
                if (selected.resolve_request != request || !(selected.hit == hit)) continue;
                selected.pending_resolve = false;
                selected.resolve_request = 0;
                selected.selector.clear();
                if (currentRevision != revision) {
                  selected.resolution_error = "The document changed while resolving this pick. Select it again.";
                } else if (!reply.ok) {
                  selected.resolution_error = reply.err.empty()
                      ? "The engine could not resolve this selection." : reply.err;
                } else if (reply.result.contains("code")) {
                  selected.resolution_error = reply.result.value("message",
                      std::string("The selected topology could not be resolved."));
                } else if (!reply.result.contains("selector") ||
                           !reply.result["selector"].is_object()) {
                  selected.resolution_error = "The selected topology no longer exists. Select it again.";
                } else {
                  selected.selector = reply.result["selector"].dump();
                  selected.resolution_error.clear();
                  // Assembly: a component-pattern instance's seed row is only known from here, and
                  // so is the owning feature inside the component's part (owner_feature).
                  if (active && reply.result.contains("component") &&
                      reply.result["component"].is_string())
                    ui::featureTreeNoteAssemblyPick(
                        st, hit, reply.result["component"].get<std::string>(),
                        reply.result.value("owner_feature", std::string()));
                }
                if (!selected.resolution_error.empty())
                  st.addLog("[selection] " + selected.resolution_error);
                break;
              }
            });
        continue;
      }
      if (st.rpc.busy()) {
        // The routed paths resolve synchronously and must not block the GL thread behind queued
        // engine work. Keep the pick pending; the per-frame retry resolves it once the queue is
        // idle. Dropping it lost clicks whenever anything was queued, even the open form's own
        // once-per-second edit_hold renewal.
        if (item.deferred_revision < 0) item.deferred_revision = st.docMirror.revision();
        continue;
      }
      std::string err;
      // C20b: while the Mate form is open, assembly picks resolve via the asm
      // path (doc_resolve_pick returns {selector, component}) and route into
      // the active well.
      if (st.asmPanels) {
        auto *ap = st.asmPanels;
        if (ap->mateFormActive()) {
          std::string comp;
          std::string asel =
              st.docMirror.resolvePickAsm(st.worker, item.hit.body_id, kindStr,
                                          item.hit.topo_id, comp, err);
          item.pending_resolve = false;
          item.resolution_error = asel.empty() ? err : std::string();
          if (!asel.empty()) {
            ui::featureTreeNoteAssemblyPick(st, item.hit, comp);
            item.selector = asel;
            item.pending_resolve = false;
            try {
              nlohmann::json selObj = nlohmann::json::parse(asel);
              ap->routeAsmPick(selObj, comp, item.hit.body_id);
            } catch (...) {
            }
          }
          continue; // handled (or failed) — skip the part-doc resolve path
        }
        // Component pattern form: a pick selects the seed COMPONENT (not a
        // face/edge).
        if (ap->patternFormActive()) {
          std::string comp;
          item.selector = st.docMirror.resolvePickAsm(st.worker, item.hit.body_id, kindStr,
                                                      item.hit.topo_id, comp, err);
          item.pending_resolve = false;
          item.resolution_error = item.selector.empty() ? err : std::string();
          if (!comp.empty()) {
            ui::featureTreeNoteAssemblyPick(st, item.hit, comp);
            ap->routeAsmPatternPick(comp);
          }
          continue; // handled — skip the part-doc resolve path
        }
      }
      // SolidWorks "fillet a whole face": a FACE pick into an active
      // fillet/chamfer edge well expands into ALL of that face's edges. Resolve
      // each edge to a selector and route it, then we're done with this pick
      // (skip the normal single-element resolve below).
      if (item.hit.kind == render::PickKind::Face && st.propertyManager &&
          st.propertyManager->active() &&
          st.propertyManager->activeWellExpandsFaceToEdges()) {
        auto *pm = st.propertyManager;
        std::vector<uint32_t> eids = st.docMirror.faceEdges(
            st.worker, item.hit.body_id, item.hit.topo_id);
        int added = 0;
        for (uint32_t eid : eids) {
          std::string eerr;
          std::string esel = st.docMirror.resolvePick(
              st.worker, item.hit.body_id, "edge", eid, eerr);
          if (esel.empty())
            continue;
          try {
            nlohmann::json eobj = nlohmann::json::parse(esel);
            if (pm->routePick(eobj, (int)render::PickKind::Edge,
                              item.hit.body_id, eid))
              ++added;
          } catch (...) {
          }
        }
        st.addLog("[pm] face fillet: expanded face " +
                  std::to_string(item.hit.topo_id) + " -> " +
                  std::to_string(added) + " edge(s)");
        item.pending_resolve = false;
        item.selector = st.docMirror.resolvePick(st.worker, item.hit.body_id, kindStr,
                                                 item.hit.topo_id, err);
        item.resolution_error = item.selector.empty() ? err : std::string();
        released.push_back(item.hit); // the face's edges are in the well; the face is not
        continue;
      }
      // Body wells (boolean targets/tools): resolve the clicked element's
      // OWNING body (doc_resolve_pick kind="body" -> {kind:"body",
      // owner_feature:...}) instead of the face/edge/vertex it landed on, so
      // picking anywhere on a body selects that body.
      const char *resolveKind = kindStr;
      if (st.propertyManager && st.propertyManager->active() &&
          st.propertyManager->activeWellWantsBody())
        resolveKind = "body";
      std::string sel = st.docMirror.resolvePick(
          st.worker, item.hit.body_id, resolveKind, item.hit.topo_id, err);
      if (!sel.empty()) {
        item.selector = sel;
        item.pending_resolve = false;
        // C17: route the freshly resolved pick into the active PropertyManager
        // well (filtered by the well's allowed kinds). The well then owns the
        // pick's highlight, so the pick leaves the global selection.
        if (st.propertyManager) {
          auto *pm = st.propertyManager;
          if (pm->active()) {
            try {
              nlohmann::json selObj = nlohmann::json::parse(sel);
              // Feedback when a pick is rejected for the wrong topology (the
              // active well wants a different kind) — otherwise the click is
              // silently lost.
              const char *kn =
                  item.hit.kind == render::PickKind::Edge     ? "edge"
                  : item.hit.kind == render::PickKind::Face   ? "face"
                  : item.hit.kind == render::PickKind::Vertex ? "vertex"
                                                              : "?";
              bool routed = pm->routePick(selObj, (int)item.hit.kind,
                                          item.hit.body_id, item.hit.topo_id);
              st.addLog(std::string("[pm] click kind=") + kn +
                        " body=" + std::to_string(item.hit.body_id) +
                        " topo=" + std::to_string(item.hit.topo_id) +
                        " routed=" + (routed ? "1" : "0") +
                        " sel=" + sel.substr(0, 120));
              if (!routed) {
                std::string hint = pm->activeWellKindHint();
                if (!hint.empty())
                  st.toast(ui::ToastLevel::Info,
                           "This field takes " + hint +
                               " — that pick wasn't added");
              }
            } catch (...) {
            }
            released.push_back(item.hit);
          }
        }
        // Simulation studies: route a FACE pick into the armed fixture/load
        // well of the active study (only when the Simulation tab is up and no
        // PM form owns picks — the mutual-exclusion guard lives in
        // SimManager::wantsViewportFacePick).
        if (st.simManager && st.simManager->wantsViewportFacePick(st)) {
          if (item.hit.kind == render::PickKind::Face) {
            try {
              nlohmann::json selObj = nlohmann::json::parse(sel);
              if (st.simManager->routeFacePick(st, selObj, item.hit.body_id,
                                               item.hit.topo_id))
                simPickConsumed = true;
            } catch (...) {
            }
          } else {
            // The click resolved to an edge/vertex while a fixture/load well is
            // armed — tell the user instead of silently dropping it.
            st.toast(ui::ToastLevel::Info,
                     "Fixtures and loads take faces — click a model face");
          }
        }
      } else {
        item.pending_resolve = false;
        item.selector.clear();
        item.resolution_error = err.empty() ? "The selected topology could not be resolved." : err;
        st.addLog("[selection] " + item.resolution_error);
        if (st.propertyManager && st.propertyManager->active()) {
          st.toast(ui::ToastLevel::Info, item.resolution_error);
          released.push_back(item.hit);
        }
      }
    }
    if (!released.empty())
      st.selection.erase(released);
    // A routed sim face pick belongs to the fixture/load WELL (shown as the
    // green/red face wash), not the global selection. Clear st.selection so sim
    // picks never ACCUMULATE here and get scooped into the next well's capture
    // (the fixture→force carryover). Safe here: we're past the iteration, and
    // clear()'s re-entrant notify just re-runs this observer over an empty
    // list.
    if (simPickConsumed)
      st.selection.clear();
  });

  st.docMirror.setAcceptedOpsObserver(
      [&st](const core::OpsResponse &r, bool revisionChanged) {
        st.adoptCurrentOpsResponse(r, revisionChanged);
      });
  st.docMirror.setSupersededOpsObserver(
      [&st](const std::string &docId, int baseRevision) {
        st.catchUpSupersededOps(docId, baseRevision);
      });
  st.docMirror.setPendingOpsObserver([&st](const std::string &id, bool pending) {
    if (pending) st.beginHumanMutation(id); else st.endHumanMutation(id);
  });
  st.docMirror.setHumanMutationObserver([&st](const std::string &id, int revision) {
    st.markHumanEdit(id, revision);
  });

  // C16: Doc mirror listener — re-draws the tree whenever a snapshot is adopted.
  // (Tree reads st.docMirror.tree() directly; listener fires on the calling
  // thread.)
  st.docMirror.setListener([&st](bool) {
    // Nothing to do beyond triggering a redraw; ImGui redraws every frame so
    // the tree panel auto-updates from docMirror.tree() on the next frame.
    // We log the new revision for observability.
    st.addLog("[doc] tree updated  rev=" +
              std::to_string(st.docMirror.revision()));
    if (!st.isActiveMirrorDoc() &&
        std::any_of(st.docTabs.begin(), st.docTabs.end(), [&st](const auto &tab) {
          return !tab.docId.empty() && tab.docId == st.docMirror.docId();
        })) {
      st.parkInactiveMirrorTree();
      return;
    }
    // C38 (T1): this is the single central choke point — DocMirror::notify()
    // fires here after EVERY adopted tree (applyOps, resync, undo, redo, newPart)
    // once the rebuilt owner map is stored. Re-derive the feature highlight
    // from the fresh owner map so the tinted faces never go stale after a
    // topology rebuild retags them. (docUndo/docRedo also refresh explicitly,
    // which is redundant-but-harmless; this listener covers the ops/resync
    // paths.)
    ui::refreshFeatureHighlight(st);
    // Doc-driven reference auto-hide on EVERY tree mutation, not just mesh
    // commits. The interactive path reaches applyDocDrivenVisibility via
    // commitDocMeshFromParsed, but agent/worker-driven updates can refresh the
    // tree WITHOUT a mesh commit (end-of-run resync when the live preview
    // already landed the final revision; daemon-pushed restoreSnapshot with no
    // aicm), which left autoHiddenPlanes/hiddenFeatures stale and the
    // origin/planes/axes visible after the AI's first solid feature.
    // Idempotent, and honors the same user-toggle overrides (planesUserToggled
    // / axesUserToggled / originUserToggled / per-id sticky) as the interactive
    // path — a manual show still wins.
    st.applyDocDrivenVisibility(st.docMirror.tree());
    // Push feature datum geometry (axes/planes/points/csys) to the renderer so
    // CREATED reference geometry is visible — datums build no body, so the
    // engine ships them out-of-band in the ops-response. Tolerant of missing
    // fields / an engine that doesn't send "datums" (the array is then empty
    // and nothing is drawn).
    if (st.renderer) {
      auto v3 = [](const nlohmann::json &a, Vec3 dflt) -> Vec3 {
        if (a.is_array() && a.size() == 3 && a[0].is_number())
          return Vec3((float)a[0].get<double>(), (float)a[1].get<double>(),
                      (float)a[2].get<double>());
        return dflt;
      };
      std::vector<Renderer::RefDatum> rds;
      for (const auto &d : st.docMirror.refDatums()) {
        if (!d.is_object())
          continue;
        Renderer::RefDatum rd;
        const std::string k = d.value("kind", std::string());
        if (k == "axis") {
          rd.kind = Renderer::RefDatum::Kind::Axis;
          rd.origin = v3(d.value("point", nlohmann::json()), Vec3(0, 0, 0));
          rd.dir = v3(d.value("dir", nlohmann::json()), Vec3(0, 0, 1));
        } else if (k == "plane" || k == "csys") {
          rd.kind = (k == "csys") ? Renderer::RefDatum::Kind::Csys
                                  : Renderer::RefDatum::Kind::Plane;
          rd.origin = v3(d.value("origin", nlohmann::json()), Vec3(0, 0, 0));
          rd.dir = v3(d.value("normal", nlohmann::json()), Vec3(0, 0, 1));
          rd.xdir = v3(d.value("x_dir", nlohmann::json()), Vec3(1, 0, 0));
          rd.ydir = v3(d.value("y_dir", nlohmann::json()), Vec3(0, 1, 0));
        } else if (k == "point") {
          rd.kind = Renderer::RefDatum::Kind::Point;
          rd.origin = v3(d.value("point", nlohmann::json()), Vec3(0, 0, 0));
        } else if (k == "curve") {
          // A helix / spiral / spline path: a sampled world polyline that the
          // renderer draws and the path picker hit-tests as a sweep path.
          rd.kind = Renderer::RefDatum::Kind::Curve;
          if (d.contains("points") && d["points"].is_array()) {
            rd.points.reserve(d["points"].size());
            for (const auto &p : d["points"])
              rd.points.push_back(v3(p, Vec3(0, 0, 0)));
          }
          if (rd.points.size() < 2)
            continue; // nothing to draw / pick
        } else {
          continue;
        }
        rd.id = d.value("id", std::string());
        rd.name = d.value("name", std::string());
        rds.push_back(rd);
      }
      // Inject the synthetic world-origin point datum so the renderer
      // draws a point glyph at (0,0,0). Eye-toggle in the FeatureManager
      // hides/shows it via hiddenFeatures — same mechanism as every
      // user-created datum feature.
      {
        Renderer::RefDatum originPt;
        originPt.kind = Renderer::RefDatum::Kind::Point;
        originPt.origin = Vec3(0.0f, 0.0f, 0.0f);
        originPt.id = "point.origin";
        originPt.name = "Origin";
        rds.push_back(originPt);
      }
      // W2-ref: inject default X/Y/Z axes + auto plane-intersection axes
      // (GUI-synthetic, like point.origin). User axis features already present
      // in refDatums win on id. Default axis.x/y/z are PREDEFINED constants —
      // never "solved". Reserved plane-pair intersections ARE those three axes;
      // never emit axis.auto for reserved-only pairs. User-plane auto axes are
      // cached by plane-set fingerprint (skip O(P²) when unchanged).
      {
        std::unordered_set<std::string> occupied;
        for (const auto &rd : rds)
          if (!rd.id.empty())
            occupied.insert(rd.id);

        auto pushAxis = [&](const std::string &id, const std::string &name,
                            const Vec3 &origin, const Vec3 &dir) {
          if (occupied.count(id))
            return;
          Renderer::RefDatum ax;
          ax.kind = Renderer::RefDatum::Kind::Axis;
          ax.origin = origin;
          ax.dir = dir;
          ax.id = id;
          ax.name = name;
          rds.push_back(ax);
          occupied.insert(id);
        };

        // Reserved world axes through origin (contracts/doc_schema.md §5) —
        // constants.
        pushAxis("axis.x", "X Axis", Vec3(0, 0, 0), Vec3(1, 0, 0));
        pushAxis("axis.y", "Y Axis", Vec3(0, 0, 0), Vec3(0, 1, 0));
        pushAxis("axis.z", "Z Axis", Vec3(0, 0, 0), Vec3(0, 0, 1));

        // User planes only for the auto-axis fingerprint. Reserved
        // Front/Top/Right are pair partners for user planes but never pair with
        // each other as axis.auto (those three intersections are axis.x/y/z).
        struct PlaneSrc {
          std::string id;
          std::string name;
          Vec3 origin;
          Vec3 normal;
          bool reserved = false;
        };
        std::vector<PlaneSrc> userPlanes;
        for (const auto &d : st.docMirror.refDatums()) {
          if (!d.is_object() || d.value("kind", std::string()) != "plane")
            continue;
          const std::string pid = d.value("id", std::string());
          if (pid.empty() || pid == "plane.front" || pid == "plane.top" ||
              pid == "plane.right")
            continue;
          PlaneSrc ps;
          ps.id = pid;
          ps.name = d.value("name", pid);
          ps.origin = v3(d.value("origin", nlohmann::json()), Vec3(0, 0, 0));
          ps.normal = v3(d.value("normal", nlohmann::json()), Vec3(0, 0, 1));
          if (length(ps.normal) < 1e-9f)
            continue;
          ps.normal = normalize(ps.normal);
          userPlanes.push_back(std::move(ps));
        }

        // Fingerprint = sorted "id@ox,oy,oz,nx,ny,nz" joined with ';' for user
        // planes.
        std::vector<std::string> fpParts;
        fpParts.reserve(userPlanes.size());
        for (const auto &p : userPlanes) {
          char buf[192];
          std::snprintf(buf, sizeof(buf), "%s@%.6g,%.6g,%.6g,%.6g,%.6g,%.6g",
                        p.id.c_str(), (double)p.origin.x, (double)p.origin.y,
                        (double)p.origin.z, (double)p.normal.x,
                        (double)p.normal.y, (double)p.normal.z);
          fpParts.emplace_back(buf);
        }
        std::sort(fpParts.begin(), fpParts.end());
        std::string fp;
        for (size_t i = 0; i < fpParts.size(); ++i) {
          if (i)
            fp += ';';
          fp += fpParts[i];
        }

        if (fp != st.axisInjectPlaneFp) {
          // Recompute: reserved + user, skip reserved-only pairs and parallel
          // pairs.
          st.axisAutoCache.clear();
          std::vector<PlaneSrc> planes;
          planes.reserve(3 + userPlanes.size());
          planes.push_back(
              {"plane.front", "Front", Vec3(0, 0, 0), Vec3(0, -1, 0), true});
          planes.push_back(
              {"plane.top", "Top", Vec3(0, 0, 0), Vec3(0, 0, 1), true});
          planes.push_back(
              {"plane.right", "Right", Vec3(0, 0, 0), Vec3(1, 0, 0), true});
          for (const auto &up : userPlanes)
            planes.push_back(up);

          const float kParEps = 1e-6f;
          for (size_t i = 0; i < planes.size(); ++i) {
            for (size_t j = i + 1; j < planes.size(); ++j) {
              const PlaneSrc &a = planes[i];
              const PlaneSrc &b = planes[j];
              // Reserved∩reserved → axis.x/y/z; never emit axis.auto.
              if (a.reserved && b.reserved)
                continue;
              Vec3 dir = cross(a.normal, b.normal);
              const float dirLen = length(dir);
              if (dirLen < kParEps)
                continue; // parallel — no unique line
              dir = dir * (1.0f / dirLen);
              const float d1 = dot(a.normal, a.origin);
              const float d2 = dot(b.normal, b.origin);
              const float det2 = dirLen * dirLen;
              Vec3 pt = (cross(b.normal, dir * dirLen) * d1 +
                         cross(dir * dirLen, a.normal) * d2) *
                        (1.0f / det2);
              std::string idA = a.id, idB = b.id;
              if (idB < idA)
                std::swap(idA, idB);
              AppState::AutoAxisCache ent;
              ent.id = "axis.auto:" + idA + "|" + idB;
              // Name uses original a/b display names (not id-sorted).
              ent.name = "Axis " + a.name + " x " + b.name;
              ent.origin[0] = pt.x;
              ent.origin[1] = pt.y;
              ent.origin[2] = pt.z;
              ent.dir[0] = dir.x;
              ent.dir[1] = dir.y;
              ent.dir[2] = dir.z;
              st.axisAutoCache.push_back(std::move(ent));
            }
          }
          st.axisInjectPlaneFp = std::move(fp);
        }

        for (const auto &ent : st.axisAutoCache) {
          pushAxis(ent.id, ent.name,
                   Vec3(ent.origin[0], ent.origin[1], ent.origin[2]),
                   Vec3(ent.dir[0], ent.dir[1], ent.dir[2]));
        }

        // Rebuild axisDatumIds. Visibility is driven by hasSolidContent
        // (viewport_panel / applyDocDrivenVisibility) with per-id sticky
        // axesUserToggledIds. Defaults still honor aggregate axesUserToggled;
        // every axis.auto:* on a solid part is auto-hidden unless THAT id was
        // user-toggled (toggling X must not unlock plane-intersection clutter).
        std::unordered_set<std::string> prevAxisIds =
            std::move(st.axisDatumIds);
        st.axisDatumIds.clear();
        for (const auto &rd : rds) {
          if (rd.kind != Renderer::RefDatum::Kind::Axis || rd.id.empty())
            continue;
          st.axisDatumIds.insert(rd.id);
        }
        // treeHasSolidContent equivalent: any non-ref feature or assembly
        // component.
        bool solid = false;
        if (st.docMirror.hasDoc()) {
          const auto &tree = st.docMirror.tree();
          if (tree.is_object()) {
            auto tci = tree.find("components");
            if (tci != tree.end() && tci->is_array() && !tci->empty()) {
              solid = true;
            } else {
              auto tfi = tree.find("features");
              if (tfi != tree.end() && tfi->is_array()) {
                for (const auto &f : *tfi) {
                  if (!f.is_object() || f.value("suppressed", false))
                    continue;
                  const std::string ty = f.value("type", std::string());
                  if (ty.empty())
                    continue;
                  // Reference-only types leave axes visible
                  // (sketch/plane/axis/…).
                  if (ty == "sketch" || ty == "sketch3d" || ty == "ref_plane" ||
                      ty == "ref_axis" || ty == "ref_point" ||
                      ty == "ref_csys" || ty == "plane" ||
                      ty == "datum_plane" || ty == "axis" ||
                      ty == "datum_axis" || ty == "point" ||
                      ty == "datum_point" || ty == "csys" ||
                      ty == "coordinate_system")
                    continue;
                  solid = true;
                  break;
                }
              }
            }
          }
        }
        auto syncAxisHid = [&](const std::string &id) {
          if (id.empty())
            return;
          if (st.axesUserToggledIds.count(id))
            return; // per-id sticky
          const bool isAuto = id.rfind("axis.auto:", 0) == 0;
          // Defaults: only auto-manage while user has not taken aggregate
          // control.
          if (!isAuto && st.axesUserToggled)
            return;
          if (solid)
            st.hiddenFeatures.insert(id);
          else
            st.hiddenFeatures.erase(id);
        };
        for (const auto &id : st.axisDatumIds)
          syncAxisHid(id);
        // Defaults always tracked even if somehow missing from axisDatumIds.
        for (const char *def : {"axis.x", "axis.y", "axis.z"})
          syncAxisHid(def);
        // Rebuild display names when axis set membership changes (or first
        // fill).
        bool membershipChanged = st.axisDisplayNames.empty() ||
                                 st.axisDatumIds.size() != prevAxisIds.size();
        if (!membershipChanged) {
          for (const auto &id : st.axisDatumIds) {
            if (!prevAxisIds.count(id)) {
              membershipChanged = true;
              break;
            }
          }
        }
        if (membershipChanged) {
          st.axisDisplayNames.clear();
          for (const auto &rd : rds) {
            if (rd.kind != Renderer::RefDatum::Kind::Axis || rd.id.empty())
              continue;
            st.axisDisplayNames[rd.id] = rd.name.empty() ? rd.id : rd.name;
          }
        }
      }
      st.renderer->setRefDatums(std::move(rds));
      st.applyDatumVisibility(); // re-apply the FeatureManager eye hides to the
                                 // fresh datums
    }
    if (st.activeTab >= 0 && st.activeTab < (int)st.docTabs.size() &&
        st.docTabs[st.activeTab].docId == st.docMirror.docId())
      st.syncActiveTabTitle();
  });

  // W1-B (item 2/11): route otherwise-silent failures into the toast system.
  //  - WorkerClient transport errors (timeout / closed pipe) from ANY call site
  //  (77 of them),
  //    incl. ones owned by other modules — they all share this one client.
  //  - DocMirror stale_revision auto-resync — the user's action was discarded;
  //  tell them.
  st.worker.setErrorSink([&st](const std::string &method,
                               const std::string &err) {
#ifdef __EMSCRIPTEN__
    // Browser backend is daemon-only: Python-worker methods (fw_*, some
    // parts_*, etc.) hit "worker not started" on every boot probe. Don't toast
    // those — panels already degrade inline. Real daemon/CAD failures surface
    // via their own call sites.
    //
    // "not connected to hostd" (exact, no suffix — see ipc_client.cpp
    // reconnectLocked) fires only when the very first RPC (e.g. the
    // agent_list_models model-picker probe) races the async WebSocket
    // connect to hostd and loses; sockPath_ is set for the rest of the
    // session the instant that connect resolves, so this exact string can
    // never recur later. A genuine ongoing outage produces a DIFFERENT,
    // longer message ("...(reconnect failed: <reason>)") that this
    // intentionally does NOT match, so real connectivity problems still
    // toast.
    (void)method;
    if (err.find("worker not started") != std::string::npos ||
        err.find("WASM: worker pipe") != std::string::npos ||
        err == "not connected to hostd")
      return;
#endif
    // Stop/steer when hostd is already down is a quiet local finalize — log
    // only, no red Error toast.
    if (method == "agent_cancel" || method == "agent_steer" ||
        method == "agent_answer") {
      st.addLog("[worker] " + method + ": " + err);
      return;
    }
    // Dedupe: a dead/unreachable backend makes polling panels (fw_projects
    // every 4s, parts, plm) re-raise the SAME transport error forever — one
    // toast per unique method+error per 60s; repeats go to the log instead of
    // stacking red toasts.
    {
      static std::mutex dedupeMtx;
      static std::map<std::string, std::chrono::steady_clock::time_point>
          lastToast;
      const std::string key = method + "\x1f" + err;
      const auto now = std::chrono::steady_clock::now();
      std::lock_guard<std::mutex> lk(dedupeMtx);
      auto it = lastToast.find(key);
      if (it != lastToast.end() &&
          now - it->second < std::chrono::seconds(60)) {
        st.addLog("[worker] " + method + ": " + err);
        return;
      }
      lastToast[key] = now;
    }
    st.toast(ui::ToastLevel::Error, "Worker error (" + method + "): " + err);
  });
  st.docMirror.setStaleResyncCallback([&st]() {
    st.toast(ui::ToastLevel::Warn,
             "Model changed — action was discarded, try again");
  });

  // ---------- runtime + worker ----------
  // Wait for the final backend verdict before spawning the managed worker: it
  // must inherit the same daemon/in-process pin. Both native shots and interactive
  // launches paint while connect/spawn/hello runs on the connection worker.
  st.workerBootPending = true;
  st.startupPhase = "Starting engine\xe2\x80\xa6";
  st.status = "Starting engine...";
  st.statusKind = 0;

  // NOTE: the old "welcome model" (mounting_plate.stl into the bare renderer)
  // is gone — it contradicted the empty Part1 document (a mesh with no tree
  // features), occluded the plane-pick quads, and inflated the empty-scene
  // camera radius. Startup now shows the clean empty-doc state: origin planes +
  // triad, like a fresh SolidWorks part.

  std::strncpy(st.code.data(), kStarterCode, st.code.size() - 1);
  // Composer starts empty; placeholder lives in ui/copilot/composer.cpp. No
  // welcome Assistant seed bubble.

  const char *shotMode =
      std::getenv("CHUDWARE_SHOT"); // screenshot-only:
                                  // settings|chat|assembly|plan|ribbon|pmform
  // Headless captures are single-frame: a transient toast pinned over the top-right
  // occludes whatever panel the shot is verifying (the Copilot header, the ribbon's
  // first group, ...). Suppress toasts for EVERY --ui-screenshot capture unless
  // CHUDWARE_SHOT_KEEPTOAST=1 asks for them (used to verify the toast itself).
  if (shot && !(std::getenv("CHUDWARE_SHOT_KEEPTOAST") &&
                *std::getenv("CHUDWARE_SHOT_KEEPTOAST")))
    st.suppressToasts = true;
  st.wantSettings = shot && shotMode && std::strcmp(shotMode, "settings") == 0;
  st.wantChat = shot && shotMode &&
                (std::strcmp(shotMode, "chat") == 0 ||
                 std::strcmp(shotMode, "chat_evidence") == 0);
  // copilot_slash: slash-autocomplete regression — the composer holds a bare "/"
  // so the drop-up menu draws every frame (the detail-truncation loop once hung
  // the UI thread on exactly this draw), then a scripted Enter accepts the
  // highlighted suggestion so the final capture shows the filled composer.
  st.wantCopilotSlash =
      shot && shotMode && std::strcmp(shotMode, "copilot_slash") == 0;
  st.wantAssembly = shot && shotMode && std::strcmp(shotMode, "assembly") == 0;
  st.wantPlan = shot && shotMode && std::strcmp(shotMode, "plan") == 0;
  st.wantPick = shot && shotMode && std::strcmp(shotMode, "pick") == 0;
  st.wantRibbon = shot && shotMode && std::strcmp(shotMode, "ribbon") == 0;
#ifndef __EMSCRIPTEN__
  if (shot && shotMode && std::strcmp(shotMode, "update_available") == 0) {
    chudware::update::Snapshot update;
    update.state = chudware::update::State::Available;
    update.version = "1.2.0";
    update.message = "Update available \xe2\x80\x94 1.2.0";
    update.downloadUrl = "https://github.com/Hunter-124/Chudware/releases/tag/v1.2.0";
    chudware::update::globalUpdater().setTestSnapshot(std::move(update));
    std::printf("[update-shot] mode=%s chip=Update-available-1.2.0 auto_modal=0\n",
                shotMode);
    std::fflush(stdout);
  }
#endif
  st.wantTree = shot && shotMode && std::strcmp(shotMode, "tree") == 0;
  // Wave 3: explorer shot — force the tabbed left pane to the Explorer (File
  // Explorer) tab so the headless screenshot captures the workspace file tree
  // (otherwise it defaults to FeatureManager).
  if (shot && shotMode && std::strcmp(shotMode, "explorer") == 0)
    st.leftPaneTab = AppState::LP_Explorer;
  // Equations editor shot — seed two globals + open the dialog once the worker
  // is ready.
  st.wantEquations =
      shot && shotMode && std::strcmp(shotMode, "equations") == 0;
  if (st.wantEquations) {
    st.showFeatureTree = true;
  } // keep the FeatureManager (Equations node) visible
  // H2: daemon reconnect resilience — kill hostd, capture unreachable chip,
  // restart, capture tabs.
  st.wantDaemonH2 = shot && shotMode && std::strcmp(shotMode, "daemon_h2") == 0;
  if (st.wantDaemonH2) {
    st.showRibbon = true;
    st.showFeatureTree = true;
  }
  st.wantM1 = shot && shotMode &&
              std::strcmp(shotMode, "m1") == 0; // C17 integration scenario
  // Multi-document mid-edit scenario: open a PM form on Part A, switch to Part
  // B and measure, switch back — assert A's form/selection/hidden-body survive
  // and B is independent (DocSession).
  st.wantMultidoc = shot && shotMode && std::strcmp(shotMode, "multidoc") == 0;
  if (st.wantMultidoc) {
    st.showRibbon = true;
    st.showFeatureTree = true;
  }
  // Tab-switch perf harness: 2 real parts + many A<->B switches; logTabPerf
  // prints to stderr.
  st.wantTabPerf = shot && shotMode && std::strcmp(shotMode, "tabperf") == 0;
  if (st.wantTabPerf) {
    st.showRibbon = true;
    st.showFeatureTree = true;
  }
  // CHUDWARE_SHOT=doctabs: seed a long strip of fake AI-handoff tabs (most pulsing,
  // like a live agent swarm) so headless captures reproduce the doc-tab overflow +
  // glow state that only shows up mid-mission. Pure visual repro — no worker docs.
  if (shot && shotMode && std::strcmp(shotMode, "doctabs") == 0) {
    static const char *kNames[] = {
        "guide_shaped_batch", "sk",      "sk2",       "Bar",       "Obstacle",
        "SwingArm",           "dialect_dia", "pf_live_test", "cheatsheet_L22",
        "cheatsheet_L29",     "cheatsheet_L40", "link_dim_test", "ask_plm",
        "bracket_mount",      "gear_pair", "housing_v2", "cam_plate",
        "sensor_node",        "latch_arm", "rail_guide", "knob_dial",
        "flange_adapter",     "heat_sink", "pulley_40t"};
    for (size_t i = 0; i < sizeof(kNames) / sizeof(kNames[0]); ++i) {
      AppState::DocTab t;
      t.kind = AppState::DocTab::TabKind::Part;
      t.aiHandoff = true;
      t.handoffName = kNames[i];
      t.aiPulse = (i % 3 != 2) && !std::getenv("CHUDWARE_SHOT_NOPULSE"); // most tabs pulsing — the live-swarm look
      if (t.aiPulse) t.lastAgentEditAt = std::chrono::steady_clock::now(); // stamp so the 5s pulse-timeout tick doesn't clear the seed
      st.docTabs.push_back(std::move(t));
    }
    st.activeTab = 7;
  }
  st.wantEcad = shot && shotMode &&
                std::strcmp(shotMode, "ecad") == 0; // ECAD GUI shot
  if (st.wantEcad) {
    st.showPcbTools = true;
    st.showRibbon = true;
    st.ribbonTab = AppState::RT_PCB;
    // The scenario used to set only the ribbon tab, so the capture stayed on the Model
    // workspace and the ECAD shot verified the modelling tab with a PCB ribbon.
    st.activeWorkspace = AppState::Workspace::Electronics;
  } // PCB tab forward
  st.wantSketch = shot && shotMode &&
                  std::strcmp(shotMode, "sketch") == 0; // C18 sketch flow
  if (st.wantSketch) {
    st.showRibbon = true;
    st.ribbonTab = AppState::RT_Sketch;
  } // Sketch tab forward
  st.wantSketch3d = shot && shotMode &&
                    std::strcmp(shotMode, "sketch3d") == 0; // 3D sketcher flow
  if (st.wantSketch3d) {
    st.showRibbon = true;
    st.showFeatureTree = true;
  }
  st.wantFea =
      shot && shotMode && std::strcmp(shotMode, "fea") == 0; // C19 static study
  // Disclosure-matrix shots (Simulate workspace study picker).
  st.wantSimDisc = shot && shotMode &&
                   (std::strcmp(shotMode, "simdisc_mech") == 0 ||
                    std::strcmp(shotMode, "simdisc_ecad") == 0);
  if (st.wantSimDisc) {
    st.simDiscKind = std::strcmp(shotMode, "simdisc_ecad") == 0 ? 1 : 0;
    st.showSimulationTools = true;
    st.showRibbon = true;
    st.ribbonTab = AppState::RT_Simulation;
    st.leftPaneTab = AppState::LP_Simulation;
  }
  st.wantCam = shot && shotMode &&
               std::strcmp(shotMode, "cam") == 0; // CAM workspace scenario
  if (st.wantCam) {
    st.showRibbon = true;
    st.ribbonTab = AppState::RT_Manufacture;
  }
  st.wantFlow = shot && shotMode &&
                std::strcmp(shotMode, "flow") == 0; // external-flow study
  // W17: engineering-drawing scenario — part w/ a hole -> Make Drawing -> Model
  // Items + hole callout + a critical toleranced dim + title block, with the
  // contextual drawing ribbon shown.
  st.wantDrawing = shot && shotMode && std::strcmp(shotMode, "drawing") == 0;
  if (st.wantDrawing) {
    st.showRibbon = true;
    st.showFeatureTree = true;
    // Same omission as the ECAD scenario: without this the sheet renders inside the Model
    // workspace, so the Drawing workspace's own chrome was never in the capture.
    st.activeWorkspace = AppState::Workspace::Drawing;
  }
  st.wantEvalMass = shot && shotMode &&
                    std::strcmp(shotMode, "evalmass") == 0; // C19 mass props
  st.wantIconPipe = shot && shotMode &&
                    std::strcmp(shotMode, "iconpipe") == 0; // icon render
  if (st.wantFea || st.wantFlow) {
    st.showSimulationTools = true;
    st.showRibbon = true;
    st.ribbonTab = AppState::RT_Simulation;
    st.leftPaneTab = AppState::LP_Simulation;
  }
  // [workspace-tabs seam] workspaces shot — cycle all 8 workspace tabs, one
  // screenshot each.
  st.wantWorkspace =
      shot && shotMode && std::strcmp(shotMode, "workspaces") == 0;
  if (st.wantWorkspace) {
    st.showRibbon = true;
  }
  // CHUDWARE_SHOT=motion: kinematic motion study end-to-end (2-body asm + angle
  // mate + playback).
  st.wantMotion = shot && shotMode && std::strcmp(shotMode, "motion") == 0;
  if (st.wantMotion) {
    st.showRibbon = true;
    st.activeWorkspace = AppState::Workspace::Simulate;
  }
  // CHUDWARE_SHOT=aiws: open the AI workspace full-body hub for headless visual
  // verification.
  if (shot && shotMode && std::strcmp(shotMode, "aiws") == 0) {
    st.showRibbon = true;
    st.activeWorkspace = AppState::Workspace::AI;
  }
  // CHUDWARE_SHOT=firmware: open the Firmware workspace with the canned project +
  // build console + emulation UART log + GPIO trace fixture — headless visual
  // verification of the Firmware workspace (project visible, build console output, emulation
  // trace rendered). fw_status / fw_projects still hit the live worker, so the
  // backend rail shows the REAL doctor state.
  if (shot && shotMode && std::strcmp(shotMode, "firmware") == 0) {
    st.showRibbon = true;
    st.activeWorkspace = AppState::Workspace::Firmware;
    ui::firmware::seedFirmwareShotFixture();
  }
  // CHUDWARE_SHOT=reveng: open the Reverse Engineering full-body workspace shell
  // (RE-03) for headless visual verification of the new tab. Same inline flag
  // pattern as aiws — the generic per-frame shot capture grabs it (no scenario
  // state machine needed).
  if (shot && shotMode && std::strcmp(shotMode, "reveng") == 0) {
    st.showRibbon = true;
    st.activeWorkspace = AppState::Workspace::ReverseEng;
  }
  // Shared canned-session seeder for the reveng_* stage scenarios. Seeds one
  // rail entry, selects it, and pins the async-list throttle far-future so the
  // live list RPC can't wipe the canned state mid-shot. gallerySession is
  // pre-marked so the first-frame session-change reset doesn't clear the seeded
  // stage state.
  auto seedRevengCanned = [](const char *sid, const char *name,
                             const char *status, int images) {
    ui::reveng::RevEngState &rs = ui::reveng::S();
    ui::reveng::SessionSummary ss;
    ss.session_id = sid;
    ss.name = name;
    ss.status = status;
    ss.images = images;
    rs.sessions.push_back(ss);
    rs.selected = 0;
    rs.listedOnce = true;
    rs.lastListT = 1e9;    // block live list reload for the whole shot
    rs.gallerySession = 0; // skip the session-change state reset
  };
  // CHUDWARE_SHOT=reveng_review: open the RE workspace on the Review stage with a
  // canned confirm_dimensions payload + deviation result — headless visual
  // verification of RE-22.
  if (shot && shotMode && std::strcmp(shotMode, "reveng_review") == 0) {
    st.showRibbon = true;
    st.activeWorkspace = AppState::Workspace::ReverseEng;
    ui::reveng::seedCannedDimPayload();
    // Session id matches seedCannedDimPayload's ("re_canned00") so loadDims
    // keeps the canned rows instead of re-firing list_reveng_dims.
    seedRevengCanned("re_canned00", "Gear motor (canned)", "review", 11);
    ui::reveng::S().stage = ui::reveng::Stage::Review;
  }
  // CHUDWARE_SHOT=reveng_capture: open RE workspace on the Source stage (RE-19
  // capture suite
  // + webcam widget + image gallery) with a canned selected session so the
  // panels draw.
  if (shot && shotMode && std::strcmp(shotMode, "reveng_capture") == 0) {
    st.showRibbon = true;
    st.activeWorkspace = AppState::Workspace::ReverseEng;
    seedRevengCanned("re_canned_cap", "Bracket scan (canned)", "capturing", 3);
    ui::reveng::S().stage = ui::reveng::Stage::Source;
  }
  // CHUDWARE_SHOT=reveng_reference: the From-reference intake with a canned
  // existing-model search (demo rows, shot-only) and the Summary column pinned
  // open, so the search-first layout can be verified headlessly.
  if (shot && shotMode && std::strcmp(shotMode, "reveng_reference") == 0) {
    st.showRibbon = true;
    st.activeWorkspace = AppState::Workspace::ReverseEng;
    seedRevengCanned("re_canned_ref", "MG996R servo (canned)", "capturing", 0);
    auto &rs = ui::reveng::S();
    rs.sessions.back().lane = "reference";
    rs.stage = ui::reveng::Stage::Source;
    rs.sourceMode = ui::reveng::SourceMode::Reference;
    rs.laneChosenFor = "re_canned_ref";
    rs.propsIntent = ui::reveng::ColumnIntent::Expanded;
    std::snprintf(rs.ref.queryBuf, sizeof(rs.ref.queryBuf), "%s", "MG996R servo");
    rs.ref.searched = true;
    rs.ref.sitesSearched = 22;
    rs.ref.candidates = {
        {"Servo MG996R", "grabcad", "https://grabcad.com/library/servo-mg996r-10", "weak"},
        {"MG996R servo motor", "grabcad", "https://grabcad.com/library/mg996r-servo-motor-4", "weak"},
        {"MG996R/MG995/DS3115 Servo Base Bracket", "grabcad",
         "https://grabcad.com/library/mg996r-mg995-ds3115-servo-base-bracket-1", "weak"},
    };
  }
  // CHUDWARE_SHOT=reveng_scale: Scale stage with a canned session.
  if (shot && shotMode && std::strcmp(shotMode, "reveng_scale") == 0) {
    st.showRibbon = true;
    st.activeWorkspace = AppState::Workspace::ReverseEng;
    seedRevengCanned("re_canned_cap", "Bracket scan (canned)", "capturing", 12);
    ui::reveng::S().stage = ui::reveng::Stage::Scale;
  }
  // CHUDWARE_SHOT=reveng_recon: open the RE workspace on the Reconstruct stage
  // with a canned session + mid-run progress state — headless visual
  // verification of RE-21.
  if (shot && shotMode && std::strcmp(shotMode, "reveng_recon") == 0) {
    st.showRibbon = true;
    st.activeWorkspace = AppState::Workspace::ReverseEng;
    seedRevengCanned("re_canned_recon", "Bracket scan (canned)",
                     "reconstructing", 12);
    ui::reveng::S().stage = ui::reveng::Stage::Reconstruct;
    // Seed reconstruct progress at 45% (mid-dense phase).
    ui::reveng::RevEngEvent ev;
    ev.kind = "reconstruct_progress";
    ev.pct = 45;
    ev.msg = "dense";
    ui::reveng::feedRevEngEvent(ev);
  }
  // CHUDWARE_SHOT=reveng_model: Model stage with a canned session.
  if (shot && shotMode && std::strcmp(shotMode, "reveng_model") == 0) {
    st.showRibbon = true;
    st.activeWorkspace = AppState::Workspace::ReverseEng;
    seedRevengCanned("re_canned_model", "Bracket scan (canned)", "modeling",
                     12);
    ui::reveng::S().stage = ui::reveng::Stage::Model;
  }
  // CHUDWARE_SHOT=reveng_deliver: Deliver stage with a canned session.
  if (shot && shotMode && std::strcmp(shotMode, "reveng_deliver") == 0) {
    st.showRibbon = true;
    st.activeWorkspace = AppState::Workspace::ReverseEng;
    seedRevengCanned("re_canned_done", "Bracket scan (canned)", "done", 12);
    ui::reveng::S().stage = ui::reveng::Stage::Deliver;
  }
  // RE-40 note: the reveng_* family used to need its own workspace re-assert here. The
  // generic shot pin below (AppState::shotWorkspacePin) now covers every scenario.
  // CHUDWARE_SHOT=parts: capture the real Parts Library browse; optional
  // CHUDWARE_PARTS_SHOT_VIEW=grid|list and CHUDWARE_PARTS_SHOT_SELECT=<row>
  // choose the result view and open a real result's detail drawer.
  if (shot && shotMode && std::strcmp(shotMode, "parts") == 0) {
    st.showRibbon = true;
    st.activeWorkspace = AppState::Workspace::PartsLibrary;
    st.wantPartsShot = true;
  }
  // CHUDWARE_SHOT=git: open the PLM hub on one of its pages for headless
  // verification. `CHUDWARE_PLM_PAGE` picks the page, `CHUDWARE_PLM_PROJECT`
  // the project folder.
  const bool wantPlmShot = shot && shotMode && std::strcmp(shotMode, "git") == 0;
  const char *plmShotPage = wantPlmShot ? std::getenv("CHUDWARE_PLM_PAGE") : nullptr;
  const bool wantPlmGraphShot =
      plmShotPage && std::strcmp(plmShotPage, "graph") == 0;
  if (wantPlmShot) {
    st.showRibbon = true;
    st.activeWorkspace = AppState::Workspace::PLM;
    const char *pg = plmShotPage;
    if (pg && std::strcmp(pg, "planning") == 0)
      ui::plmSelectPlanningPage();
    else if (pg && std::strcmp(pg, "team") == 0)
      ui::plmSelectTeamPage();
    else if (pg && std::strcmp(pg, "overview") == 0)
      ui::plmSelectOverviewPage();
    else if (pg && std::strcmp(pg, "files") == 0)
      ui::plmSelectFilesPage();
    else if (pg && std::strcmp(pg, "graph") == 0)
      ui::plmSelectGraphPage();
    else if (pg && std::strcmp(pg, "compare") == 0)
      ui::plmSelectComparePage();
    else if (pg && std::strcmp(pg, "versions") == 0)
      ui::plmSelectVersionsPage();
    else if (pg && std::strcmp(pg, "releases") == 0)
      ui::plmSelectReleasesPage();
    else if (pg && std::strcmp(pg, "tour") == 0) {
      ui::plmSelectOverviewPage();
      ui::plmStartGuidedTour();
    } else
      ui::plmSelectGitPage();
    // CHUDWARE_PLM_PROJECT points the capture at a specific project folder.
    // Without it the shot falls back to the cwd, which is fine for the pages
    // that need no vault but useless for Compare (it needs real versions).
    if (const char *pd = std::getenv("CHUDWARE_PLM_PROJECT"); pd && *pd) {
      st.workspaceDir = pd;
    } else {
      std::error_code ec;
      auto cwd = std::filesystem::current_path(ec);
      st.workspaceDir = ec ? std::string(".") : cwd.string();
    }
  }
  // Seed the pin from the scenario's own selection (see AppState::shotWorkspacePin). Model is
  // not a pin target: scenarios that reach their workspace mid-run adopt it in the frame loop.
  if (shot && !st.wantWorkspace && st.activeWorkspace != AppState::Workspace::Model)
    st.shotWorkspacePin = (int)st.activeWorkspace;
  // C20b: assembly shot — author base/peg parts, new assembly, insert + mate +
  // interference.
  st.wantAsm = shot && shotMode && std::strcmp(shotMode, "asm") == 0;
  // CHUDWARE_MATEFORM: a lightweight Mate-form-only variant of the asm shot for
  // visual verification of the mate PM (Standard + Mechanical/Advanced sections
  // + the type-aware param block). It skips the heavy author/insert/mate state
  // machine and just opens the Mate form so the generic frame-6 capture grabs
  // it (mirrors the pmform scenario). CHUDWARE_MATETYPE=<type> preselects a
  // mechanical/advanced type (e.g. "gear") so its parameter field renders.
  const bool wantMateForm =
      st.wantAsm && std::getenv("CHUDWARE_MATEFORM") != nullptr;
  if (st.wantAsm) {
    st.showRibbon = true;
    st.ribbonTab = AppState::RT_Features; // Assembly tools live on Features tab
    if (wantMateForm) {
      st.wantAsm = false; // suppress the scripted scenario; just show the form
      asmPanels.handleCommand(
          st, "asm_mate"); // open the PM-guest mate form (left column)
      if (const char *mt = std::getenv("CHUDWARE_MATETYPE"))
        if (*mt)
          asmPanels.presetMateTypeForShot(mt);
    }
  }
  // ribbon shot: show Features tab by default; CHUDWARE_TAB=<0..12> selects which
  // tab to capture (0=Features 1=Sketch 2=Evaluate 3=Simulation 4=Assembly
  // 5=Sheet Metal 6=Weldments 7=PCB 8=Surfaces .. 11=Appearance 12=Manufacture)
  // for headless per-tab verification.
  if (st.wantRibbon) {
    st.showRibbon = true;
    st.ribbonTab = AppState::RT_Features;
    // Toasts are already suppressed for every headless capture (see the shotMode
    // block above). CHUDWARE_SHOT_KEEPTOAST=1 re-enables them AND seeds this one,
    // so the toast's own position/theming can be verified against the ribbon.
    if (!st.suppressToasts)
      st.toast(
          ui::ToastLevel::Info,
          "Chudware: live view of the AI's part — it updates as it builds");
    if (const char *tabEnv = std::getenv("CHUDWARE_TAB")) {
      int t = std::atoi(tabEnv);
      if (t >= 0 && t <= AppState::RT_Manufacture)
        st.ribbonTab = t;
    }
    // Named alias for CHUDWARE_TAB so in-context icon verification reads
    // self-documenting (e.g. CHUDWARE_RIBBON_TAB=appearance). Also unblocks
    // per-tab ribbon-glass captures.
    if (const char *tn = std::getenv("CHUDWARE_RIBBON_TAB")) {
      const std::string t = tn;
      int idx = -1;
      if (t == "features")
        idx = AppState::RT_Features;
      else if (t == "sketch")
        idx = AppState::RT_Sketch;
      else if (t == "evaluate")
        idx = AppState::RT_Evaluate;
      else if (t == "simulation")
        idx = AppState::RT_Simulation;
      else if (t == "assembly")
        idx = AppState::RT_Features; // Assembly tab retired -> Features
      else if (t == "sheetmetal")
        idx = AppState::RT_SheetMetal;
      else if (t == "weldments")
        idx = AppState::RT_Weldments;
      else if (t == "pcb")
        idx = AppState::RT_PCB;
      else if (t == "surfaces")
        idx = AppState::RT_Surfaces;
      else if (t == "appearance")
        idx = AppState::RT_Appearance;
      else if (t == "manufacture")
        idx = AppState::RT_Manufacture;
      if (idx >= 0)
        st.ribbonTab = idx;
      else {
        std::fprintf(stderr, "CHUDWARE_RIBBON_TAB: unknown tab '%s'\n", tn);
      }
    }
  }
  // pmform shot: open a PropertyManager form headlessly for visual verification
  // of its field layout/sizing. CHUDWARE_PMCMD=<cmd_id> (e.g. "extrude",
  // "surf_loft") selects which form; the left pane auto-switches to the
  // PropertyManager tab when a form is active (see left_pane.cpp).
  // openForCommand is safe pre-worker: with no doc the checkpoint is skipped
  // and field init is pure.
  if (shot && shotMode && std::strcmp(shotMode, "pmform") == 0) {
    st.showRibbon = true;
    st.showFeatureTree =
        true; // ensure the left pane (which hosts the PM tab) is visible
    // Verification harness: optionally load a part into the viewport so
    // the docked panels sample a LIVE (light) backdrop — that regime (adaptive
    // DARK text over the light viewport) is where the dark-mode
    // black-on-black actually manifests; without a loaded doc the panes fall
    // back to the theme palette (light text) and hide the bug. Gated on
    // CHUDWARE_PMBOX=1 so the default pmform shot is unchanged.
    if (const char *box = std::getenv("CHUDWARE_PMBOX");
        box && *box && std::strcmp(box, "0") != 0) {
      std::string aicm;
      const char *candidates[] = {
          "contracts/fixtures/box.aicm",
          "../contracts/fixtures/box.aicm",
          "../../contracts/fixtures/box.aicm",
      };
      for (const char *c : candidates) {
        FILE *f = std::fopen(c, "rb");
        if (f) {
          std::fclose(f);
          aicm = c;
          break;
        }
      }
      std::string lerr;
      if (!aicm.empty() && st.loadDocMesh(aicm, lerr)) {
        st.yaw = 35;
        st.pitch = 28;
        st.zoom = 1.0f; // iso so the box fills the viewport
      } else {
        std::fprintf(stderr, "pmform: CHUDWARE_PMBOX load failed (%s): %s\n",
                     aicm.empty() ? "not found" : aicm.c_str(), lerr.c_str());
      }
    }
    if (const char *cmd = std::getenv("CHUDWARE_PMCMD"))
      if (*cmd && !st.propertyManager->openForCommand(st, cmd))
        std::fprintf(stderr, "pmform: no form for command '%s'\n", cmd);
    // CHUDWARE_PMSET="key=value;key2=value2" presets fields so type-gated rows
    // render (e.g. "type=face" to show a fillet's face-set wells,
    // "end=up_to_surface", "thin=true").
    if (const char *setEnv = std::getenv("CHUDWARE_PMSET")) {
      std::string spec = setEnv;
      size_t pos = 0;
      while (pos < spec.size()) {
        size_t semi = spec.find(';', pos);
        std::string tok = spec.substr(pos, semi - pos);
        size_t eq = tok.find('=');
        if (eq != std::string::npos)
          st.propertyManager->presetFieldForShot(tok.substr(0, eq),
                                                 tok.substr(eq + 1));
        if (semi == std::string::npos)
          break;
        pos = semi + 1;
      }
    }
  }
  // wantTree doc smoke runs in the frame loop once the worker is ready (see
  // below).
  if (st.wantPick) {
    // C15 picking demo/verification: load the golden box AICM, assert its
    // topology counts, and (in the frame loop) simulate a center-screen pick +
    // highlight. The AICM path can be overridden via CHUDWARE_AICM; otherwise we
    // probe the repo fixture relative to the exe.
    std::string aicm;
    if (const char *env = std::getenv("CHUDWARE_AICM"))
      aicm = env;
    if (aicm.empty()) {
      const char *candidates[] = {
          "docs\\contracts\\fixtures\\box.aicm",
          "..\\docs\\contracts\\fixtures\\box.aicm",
          "..\\..\\docs\\contracts\\fixtures\\box.aicm",
          "..\\..\\..\\docs\\contracts\\fixtures\\box.aicm",
          "..\\..\\..\\..\\docs\\contracts\\fixtures\\box.aicm",
      };
      for (const char *c : candidates) {
        FILE *f = std::fopen(c, "rb");
        if (f) {
          std::fclose(f);
          aicm = c;
          break;
        }
      }
    }
    std::string lerr;
    if (aicm.empty() || !st.loadDocMesh(aicm, lerr)) {
      std::fprintf(stderr, "pick: failed to load box.aicm (%s): %s\n",
                   aicm.empty() ? "not found" : aicm.c_str(), lerr.c_str());
    } else {
      // Assert + print golden counts (mesh_format.md §4): 6 faces / 12 edges /
      // 8 verts.
      render::DocMesh probe;
      std::string perr;
      render::loadAicmFile(aicm, probe, perr);
      const auto &b = probe.bodies.front();
      std::printf("aicm: bodies=%zu name=%s verts=%u tris=%u faces=%zu "
                  "edges=%zu brep_verts=%zu\n",
                  probe.bodies.size(), b.name.c_str(), b.n_verts, b.n_tris,
                  b.faces.size(), b.edges.size(), b.brep_verts.size());
      if (probe.bodies.size() != 1 || b.faces.size() != 6 ||
          b.edges.size() != 12 || b.brep_verts.size() != 8 || b.n_verts != 24 ||
          b.n_tris != 12) {
        std::fprintf(stderr, "pick: golden box.aicm counts mismatch!\n");
      }
      st.yaw = 35;
      st.pitch = 28;
      st.zoom = 1.0f; // iso so a face faces the camera
    }
  }
  if (st.wantPlan) {
    // The moment plan mode shines: the plan card is drafted and visible while
    // the first code pass is still being written.
    ChatMsg u;
    u.fromUser = true;
    u.text = "an M3 hex standoff, 30mm tall";
    st.chat.push_back(u);
    ChatMsg a;
    a.running = true;
    a.liveStatus = "Build plan ready - writing the CAD code...";
    a.plan.present = true;
    a.plan.name = "hex standoff";
    a.plan.summary =
        "extruded hex prism with a through bore, chamfered top rim";
    PlanPart pp;
    pp.name = "standoff";
    pp.purpose = "spaces a PCB off its mounting plate";
    pp.dims = "across_flats 10, height 30, bore_radius 1.7";
    pp.features = {"M3 clearance bore r=1.7 through - cut a Cylinder",
                   "1mm chamfer on the top rim - chamfer(top_edges, length=1)"};
    a.plan.parts.push_back(pp);
    // A thinking box that has ALREADY overflowed the streaming height cap, so the shot
    // exercises the case the box exists for: once it clamps at copilotProseStreamCapH it
    // must keep the NEWEST thoughts in view, not freeze on the opening sentence.
    ActivityItem think;
    think.rowKind = 2;
    think.stage = "thought:stream";
    think.title = "Thinking";
    think.status = "running";
    think.streaming = true;
    think.expanded = true;
    for (int i = 1; i <= 14; ++i)
      think.detail += "Step " + std::to_string(i) +
                      ": the chamfer tips dip under the FDM minimum wall, so the rim "
                      "needs to come back before the bore is cut. ";
    think.detail += "LAST-LINE-MARKER";
    a.activity.push_back(think);
    st.chat.push_back(a);
  }
  if (st.wantAssembly) {
    // Demo a 3-part fit-test: base plate, lid above it, peg through both (clash
    // on the peg). Populates the viewport parts panel + fit readout and a chat
    // exchange so --ui-screenshot shows the assembly UI.
    const std::string asmDir = appdataAppDir() + "/cache/screenshot_asm/";
    st.asmStl = asmDir + "screenshot_asm_assembly.stl";
    // Per-part STLs (generated once for the demo) so the viewport shows the
    // bodies color-coded; missing files just fall back to the combined mesh.
    st.asmParts = {{"base", asmDir + "screenshot_asm_base.stl",
                    "40.0 x 30.0 x 8.0 mm", 9600.0},
                   {"lid", asmDir + "screenshot_asm_lid.stl",
                    "40.0 x 30.0 x 8.0 mm", 9600.0},
                   {"peg", asmDir + "screenshot_asm_peg.stl",
                    "6.0 x 6.0 x 30.0 mm", 848.2}};
    st.asmFit.present = true;
    st.asmFit.ok = false;
    st.asmFit.summary =
        "2 interfering pair(s); largest base<->peg = 226.2 mm^3";
    st.asmFit.interferences = {{"base", "peg", 226.2}, {"lid", "peg", 226.2}};
    st.asmFit.clearances = {{"base", "lid", 10.0}};
    st.showPartMesh(
        -1); // show the combined assembly mesh if its STL was pre-generated
    ChatMsg u;
    u.fromUser = true;
    u.text = "a base plate, a lid 10mm above it, and a peg through both";
    st.chat.push_back(u);
    ChatMsg a;
    a.text = "Built a 3-part assembly. The peg passes through both plates - "
             "tell me if that "
             "press-fit is intended, or I'll trim it to clear.";
    st.chat.push_back(a);
  }
  // CHUDWARE_SHOT=chat_context: a sent message carrying context-picker chips (a face + a
  // feature) as the latest bubble, plus pending chips above the composer (an edge + a sketch).
  // All are bound to the shot's open document once its tab has a hostd doc id.
  const char* shotModeEnv = std::getenv("CHUDWARE_SHOT");
  const bool shotChatContext =
      shot && shotModeEnv && std::strcmp(shotModeEnv, "chat_context") == 0;
  int chatContextBoundAt = -1;  // frame the chat_context chips were bound to a live doc
  bool chatContextCreateTried = false;
  auto shotContextChip = [](const char* kind, const char* id, const char* label,
                            const char* type, nlohmann::json selector) {
    core::ContextRef r;
    r.kind = kind;
    r.id = id;
    r.label = label;
    r.type = type;
    r.selector = std::move(selector);
    if (r.selector.is_object()) {
      r.owner = "f1";
      r.ownerName = "Standoff";
    }
    return r;
  };
  const std::vector<core::ContextRef> shotPendingChips = {
      shotContextChip("edge", "E5c1d2e", "Edge of Standoff", "",
                      {{"kind", "edge"}, {"surf", "circle"}, {"centroid", {0, 0, 30}},
                       {"axis", {0, 0, 1}}, {"radius", 5.5}, {"area_or_len", 34.558},
                       {"sid", "E5c1d2e"}}),
      shotContextChip("sketch", "f2", "Hex profile", "sketch", nullptr)};
  if (shotChatContext) {
    ChatMsg u;
    u.fromUser = true;
    u.text = "make this face 5 mm thicker and match the chamfer";
    u.contextRefs = {
        shotContextChip("face", "F3a9e71", "Face of Standoff", "",
                        {{"kind", "face"}, {"surf", "plane"}, {"centroid", {0, 0, 22}},
                         {"axis", {0, 0, 1}}, {"normal", {0, 0, 1}}, {"side", "top"},
                         {"area_or_len", 95.3}, {"sid", "F3a9e71"}}),
        shotContextChip("feature", "f3", "Top chamfer", "chamfer", nullptr)};
    st.chat.push_back(u);
    ChatMsg a;
    a.text = "Thickened the top face of Standoff by 5 mm and re-applied the 1 mm chamfer.";
    st.chat.push_back(a);
  }
  if (st.wantChat && std::strcmp(shotMode, "chat_evidence") == 0) {
    ChatMsg u;
    u.fromUser = true;
    u.text = "Render the mounting bracket and inspect its bore geometry.";
    st.chat.push_back(std::move(u));
    ChatMsg live;
    live.running = true;
    live.ts = chatNowEpoch() - 95.0;
    st.chat.push_back(std::move(live));
  } else if (st.wantChat) {
    ChatMsg u;
    u.fromUser = true;
    u.text = "make it 30mm tall with a chamfered top edge";
    st.chat.push_back(u);
    ChatMsg a;
    a.text = "Raised the standoff to 30mm and chamfered the top rim. Ask for "
             "another tweak "
             "and I'll revise this same part.";
    // Transparent activity timeline — what the AI did, expandable. Mirrors the
    // live orchestrator stages so the screenshot exercises the new inline
    // activity surface.
    {
      ActivityItem i0;
      i0.stage = "intake";
      i0.title = "Understanding the request";
      i0.status = "pass";
      i0.reason = "Edit the open hex standoff: 30mm tall + chamfered top.";
      i0.detail = "User wants the existing part taller with a chamfered top "
                  "rim; this is an "
                  "edit to the open document, not a new part.";
      i0.durationMs = 900;
      a.activity.push_back(i0);
      ActivityItem i1;
      i1.stage = "plan";
      i1.title = "Planning the build";
      i1.status = "pass";
      i1.reason = "Change total_h 22->30, add 1mm chamfer on top edges.";
      i1.durationMs = 1400;
      a.activity.push_back(i1);
      ActivityItem i2;
      i2.stage = "edit";
      i2.title = "Editing the model";
      i2.status = "pass";
      i2.opsCount = 3;
      i2.durationMs = 2600;
      i2.detail = "Set height=30; selected top perimeter edges; applied "
                  "chamfer length=1.0.";
      a.activity.push_back(i2);
      ActivityItem i3;
      i3.stage = "review";
      i3.title = "Reviewing the result";
      i3.status = "pass";
      i3.reason = "Rebuilds clean; height and chamfer match.";
      i3.durationMs = 1100;
      a.activity.push_back(i3);
    }
    // The plan-mode card the agent drafts before writing code.
    a.plan.present = true;
    a.plan.name = "hex standoff";
    a.plan.summary =
        "extruded hex prism with a through bore, chamfered top rim";
    {
      PlanPart pp;
      pp.name = "standoff";
      pp.purpose = "spaces a PCB off its mounting plate";
      pp.dims = "across_flats 10, height 30, bore_radius 1.7";
      pp.features = {
          "M3 clearance bore r=1.7 through - cut a Cylinder",
          "1mm chamfer on the top rim - chamfer(top_edges, length=1)"};
      a.plan.parts.push_back(pp);
    }
    // Typed transparency rows (M12-M14) + the per-call META trailer, so the shot
    // exercises the chip ellipsize, the finished-thinking auto-collapse, the
    // tool-row timestamp/cache cluster and the bubble meta-row cache suffix.
    a.cacheReadTok = 41200;
    {
      ActivityItem chip;
      chip.rowKind = 1;
      chip.stage = "ctx";
      chip.title = "Interference check pre op with a deliberately long rule label";
      chip.reason = "rule";
      chip.ctxTokens = 232;
      chip.status = "done";
      a.activity.push_back(chip);
      // Finished thinking box: expanded in the seed (as the live stream leaves
      // it) — the finished-turn clamp must collapse it to the one-line preview.
      ActivityItem think;
      think.rowKind = 2;
      think.stage = "thought:stream";
      think.title = "Thinking";
      think.status = "done";
      think.expanded = true;   // stream left it open; turn end must auto-close
      think.detail = "The user wants a taller standoff with a chamfered rim. "
                     "Plan: raise total_h, then chamfer the top edge ring.";
      a.activity.push_back(think);
      ActivityItem tool;
      tool.rowKind = 3;
      tool.stage = "tool:seed1";
      tool.title = "add_features";
      tool.reason = "standoff";
      tool.status = "done";
      tool.durationMs = 1840;
      tool.detail = std::string("{\"height\": 30, \"chamfer\": 1.0}") +
                    "\n\x1e" "META\x1e\n" + std::to_string((long long)chatNowEpoch()) +
                    "\t41300";
      a.activity.push_back(tool);
      // A failed call with the ERR trailer + ts/cache META.
      ActivityItem toolFail;
      toolFail.rowKind = 3;
      toolFail.stage = "tool:seed2";
      toolFail.title = "chamfer";
      toolFail.reason = "top edges";
      toolFail.status = "fail";
      toolFail.durationMs = 220;
      toolFail.detail = std::string("{\"radius\": 1.0}") +
                        "\n\x1e" "ERR\x1e\nchamfer() got an unexpected keyword "
                        "argument 'radius'" +
                        "\n\x1e" "META\x1e\n" +
                        std::to_string((long long)chatNowEpoch()) + "\t1200";
      a.activity.push_back(toolFail);
      // Ops-JSON output box with an over-long summary: must ellipsize to the row.
      ActivityItem ops;
      ops.rowKind = 2;
      ops.stage = "stream:out";
      ops.title = "Building ops\xe2\x80\xa6";
      ops.status = "done";
      ops.opsCount = 2;
      ops.reason = "1747 bytes \xc2\xb7 mission_standoff_chamfer_top_edge_ring_extra_long";
      ops.detail = "[{\"op\":\"extrude\"},{\"op\":\"chamfer\"}]";
      a.activity.push_back(ops);
    }
    st.chat.push_back(a);
    // Second turn: a live multi-agent run, so the agent overview above the composer shows a
    // plan with progress, a running / finished / failed sub-agent and inter-agent messages. The
    // bubble is only the shell; its rows, the plan and the agent trace are produced at frame 1
    // by replaying the worker's own event stream (see the frame-1 shot block).
    ChatMsg u2;
    u2.fromUser = true;
    u2.text = "Add four M3 mounting bosses for the sensor board and check clearance "
              "against the enclosure lid.";
    st.chat.push_back(u2);
    ChatMsg live;
    live.running = true;
    live.ts = chatNowEpoch() - 95.0;   // header elapsed reads as a run in progress
    st.chat.push_back(live);
  }

  // The four chat seeds above run BEFORE the startup session restore, which assigns over
  // st.chat — so on any machine with a saved conversation store the seeded turns were silently
  // discarded and the shot captured the user's own transcript instead (and on an EMPTY store the
  // restore left the hero state up). Keep a copy and re-apply it at frame 1, after the restore,
  // so a chat shot always shows the scenario it names.
  const std::vector<ChatMsg> shotChatSeed = st.chat;

#ifndef __EMSCRIPTEN__
  // Scripted screenshot/test scenarios
  // (CHUDWARE_SHOT=m1/sketch/asm/fea/evalmass/activity): the six step machines +
  // their per-step backbuffer captures, extracted to ui::ScenarioRunner
  // (ui/scenarios.cpp). tick() advances the active scenario before each frame;
  // endFrame() handles the captures and the scenario-driven swap+continue /
  // swap+break at loop bottom.
  ui::ScenarioRunner scenarios;
#endif

  auto desiredThemeForFrame = [&st]() {
    static const char *envTheme = std::getenv("CHUDWARE_THEME");
    if (envTheme) {
      if (std::strcmp(envTheme, "light") == 0)
        return ui::Theme::Light;
      if (std::strcmp(envTheme, "dark") == 0)
        return ui::Theme::Dark;
    }
    return (st.settings.theme == "light") ? ui::Theme::Light : ui::Theme::Dark;
  };
  auto styleColorMatchesRole = [](const ImVec4 &c, const float *role) {
    return std::fabs(c.x - role[0]) < 0.006f &&
           std::fabs(c.y - role[1]) < 0.006f &&
           std::fabs(c.z - role[2]) < 0.006f &&
           std::fabs(c.w - role[3]) < 0.006f;
  };
  auto ensureFrameTheme = [&]() {
    const ui::Theme desired = desiredThemeForFrame();
    const ImVec4 *colors = ImGui::GetStyle().Colors;
    const bool staleColors =
        !styleColorMatchesRole(colors[ImGuiCol_WindowBg],
                               ui::palette.windowBg) ||
        !styleColorMatchesRole(colors[ImGuiCol_ChildBg], ui::palette.panelBg) ||
        !styleColorMatchesRole(colors[ImGuiCol_FrameBg], ui::palette.frameBg);
    if (desired != ui::activeTheme || staleColors)
      ui::applyTheme(desired, scale);
  };

  // Headless capture with no engine: every scripted scenario (and every async
  // shot that waits on frame 99999) gates on workerStarted, so once startup has
  // tried the worker, the in-process core AND the one automatic setup, and all
  // of them failed, the capture can never happen. Report it and exit non-zero
  // instead of rendering frames forever.
  int shotExitCode = 0;
  auto shotEngineUnavailable = [&]() {
    return shot && !st.workerStarted && !st.workerBootPending &&
           st.autoSetupTried && !st.setupRunning.load() && !st.setupDone.load();
  };
  auto failShotNoEngine = [&]() {
    const char *scenario = std::getenv("CHUDWARE_SHOT");
    std::fprintf(stderr,
                 "ui screenshot FAILED: no engine could start (%s; worker: %s); "
                 "CHUDWARE_SHOT=%s cannot run\n",
                 st.status.c_str(), st.werr.empty() ? "-" : st.werr.c_str(),
                 scenario ? scenario : "(none)");
    std::fflush(stderr);
    shotExitCode = 1;
  };

  // Enable exact full-run capture before the first recorded frame, including setup.
  if (shot && shotMode && std::strcmp(shotMode, "frameperf") == 0) {
    const char *repo = std::getenv("CHUDWARE_REPO");
    core::framestats::setOutputPath(std::string(repo ? repo : ".") +
                                    "/scratch/frameperf/framestats.json");
  }

  st.rpc.setWakeCallback(ui::anim::wakeLoop);
  st.authRpc.setWakeCallback(ui::anim::wakeLoop);
  st.rpc.setPrimaryCaller([](const std::string &method, const nlohmann::json &params,
                            nlohmann::json &result, std::string &err, int timeoutMs,
                            const std::atomic<bool> &cancelled) {
    if (cancelled.load()) {
      err = "aborted";
      return false;
    }
    if (auto *client = engine::client())
      return client->call(method, params, result, err, timeoutMs, {}, &cancelled);
    err = "primary engine connection is unavailable";
    return false;
  });
  int postInputFrames = 0;
  bool quitAfterPendingEdits = false;
  WASM_BOOT_LOG("[wasm-boot] entering main loop\n");
  for (;;) {
    if (quitAfterPendingEdits && !st.hasPendingHumanMutations()) {
      quitAfterPendingEdits = false;
      glfwSetWindowShouldClose(win, GLFW_TRUE);
    }
    // Unsaved-changes guard on exit, at the loop HEAD so it sees every close
    // request before the loop can end: the title-bar X and File > Quit set
    // glfwSetWindowShouldClose mid-frame (after any check in the body, so a
    // mid-body guard let them quit silently and discard unsaved parts), the
    // window manager / Alt+F4 during event polling. If any document has unsaved
    // edits and the user hasn't already confirmed, cancel the close and raise
    // the exit confirmation modal. A clean session (or an already-confirmed
    // quit) falls straight through and the loop exits.
    if (glfwWindowShouldClose(win)) {
      if (!shot && st.hasPendingHumanMutations()) {
        glfwSetWindowShouldClose(win, GLFW_FALSE);
        quitAfterPendingEdits = true;
        // A newly accepted edit must be assessed after it settles, even if a
        // previous save/discard decision was already confirmed.
        st.quitConfirmed = false;
      } else if (!shot && !st.quitConfirmed) {
        const bool anyDirty = std::any_of(st.docTabs.begin(), st.docTabs.end(),
                                          [](const auto &t) { return t.dirty; });
        // In-flight copilot runs get their own prompt before the dirty-doc modal.
        if (st.anyRunActive()) {
          glfwSetWindowShouldClose(win, GLFW_FALSE);
          st.wantQuitAgentConfirm = true;
        } else if (anyDirty) {
          glfwSetWindowShouldClose(win, GLFW_FALSE);
          st.wantQuitConfirm = true;
        }
      }
      if (glfwWindowShouldClose(win))
        break;
    }
    core::framestats::Frame frameTiming;
#ifdef __EMSCRIPTEN__
    const bool engineTickAllowed = true;
#else
    // Connection startup is asynchronous. Bootstrap and the legacy handoff
    // check still wait until the interactive native shell has been swapped.
    const bool engineTickAllowed = shot || s_nativeStartup.firstFramePresented;
#endif
#ifdef __EMSCRIPTEN__
    static int s_wasmFrames = 0;
    ++s_wasmFrames;
    if (s_wasmFrames <= 3) {
      WASM_BOOT_LOG("[wasm-boot] frame %d\n", s_wasmFrames);
      if (s_wasmFrames == 3)
        wasmBootReady();
    }
    // The GLFW port normally refreshes its content scale through a resolution
    // media-query event. Browser zoom / moving between monitors usually fires it,
    // but some Chromium device-scale changes do not. A CSS-size-only check below
    // then leaves the old DPR backing store in place indefinitely: canvas CSS
    // pixels, GLFW framebuffer metrics and ImGui draw coordinates diverge.
    // Reuse the port's scale-change path (which updates all three) rather than
    // setting canvas.width ourselves and leaving GLFW's framebuffer size stale.
    const double browserDpr = emscripten_get_device_pixel_ratio();
    float glfwDpr = 1.0f, glfwDprY = 1.0f;
    glfwGetWindowContentScale(win, &glfwDpr, &glfwDprY);
    if (std::isfinite(browserDpr) && browserDpr > 0.0 &&
        std::fabs(browserDpr - glfwDpr) > 0.01) {
      EM_ASM({ GLFW3.onScaleChange(); });
    }
    // Self-healing CSS-size sync: fullscreen and visualViewport changes can
    // resize the displayed canvas without changing the GLFW logical size.
    // The port's setCanvasSize supplies the DPR-scaled backing store.
    // A 1px deadband avoids subpixel resize thrash.
    {
      double cssW = 0.0, cssH = 0.0;
      if (emscripten_get_element_css_size("#canvas", &cssW, &cssH) ==
          EMSCRIPTEN_RESULT_SUCCESS) {
        const int tW = (int)(cssW + 0.5), tH = (int)(cssH + 0.5);
        int winW = 0, winH = 0;
        glfwGetWindowSize(win, &winW, &winH);
        const int dW = tW > winW ? tW - winW : winW - tW;
        const int dH = tH > winH ? tH - winH : winH - tH;
        if (tW > 0 && tH > 0 && (dW > 1 || dH > 1))
          glfwSetWindowSize(win, tW, tH);
      }
    }
    // Mobile layout detect (narrow / coarse pointer). HUD + multitouch drain
    // AFTER the ASYNCIFY warmup frames so we never double-apply multitouch
    // deltas.
    {
      const bool mobile = wasmIsNarrowViewport(win);
      wasmApplyMobileLayout(st, mobile);
    }
    // Canvas sizing follows #app-surface, including software-keyboard changes.
    // First frames: minimal paint only — full UI path is too deep for ASYNCIFY
    // until the rewind stack has settled after a few yields.
    if (s_wasmFrames <= 2) {
      {
        core::framestats::Scope timing("event_wait");
        glfwPollEvents();
        core::framestats::markWait(timing.elapsedMs());
      }
      {
        core::framestats::Scope timing("imgui_new_frame");
        ImGui_ImplOpenGL3_NewFrame();
        ImGui_ImplGlfw_NewFrame();
        ImGui::NewFrame();
      }
      ImGui::SetNextWindowPos(ImVec2(20, 20), ImGuiCond_Always);
      ImGui::Begin("Chudware WASM", nullptr,
                   ImGuiWindowFlags_AlwaysAutoResize |
                       ImGuiWindowFlags_NoCollapse);
      ImGui::Text("Chudware WASM boot — frame %d", s_wasmFrames);
      ImGui::Text("ASYNCIFY warmup (full UI next)");
      ImGui::End();
      {
        core::framestats::Scope timing("imgui_render");
        ImGui::Render();
      }
      int dw = 0, dh = 0;
      glfwGetFramebufferSize(win, &dw, &dh);
      glViewport(0, 0, dw, dh);
      glClearColor(0.07f, 0.09f, 0.11f, 1.0f);
      glClear(GL_COLOR_BUFFER_BIT);
      {
        core::framestats::Scope timing("imgui_draw");
        ImGui_ImplOpenGL3_RenderDrawData(ImGui::GetDrawData());
      }
      swapFrameBuffers(win);
      emscripten_sleep(0);
      continue;
    }
    wasmDrainHudCommands(st);
#endif
    // Interactive motion polls; ambient painters renew deadlines without pinning vsync.
    // Consume requests unconditionally so a separate busy source cannot leave stale flags.
    {
      ImGuiContext *ctx = ImGui::GetCurrentContext();
      if (ctx->InputEventsQueue.Size > 0)
        postInputFrames = 3;
      const bool wantFrames = ui::anim::consumeFrameRequest();
      if (st.simClient && st.simClient->busy())
        ui::anim::requestFramesAt(15.0f);
      const double ambientWait = ui::anim::consumeAmbientWait();
      const bool busy = wantFrames || postInputFrames > 0 || st.animActive ||
                        st.rebuildsInFlight > 0 || st.rpc.interactionBusy();
      core::framestats::Scope timing("event_wait");
#ifdef __EMSCRIPTEN__
      (void)ambientWait;
      (void)busy;
      core::framestats::setIdle(false); // browser waits stall PROXY_TO_PTHREAD + contrib.glfw3
      glfwPollEvents();
#else
      const bool hidden = !shot && (chrome::iconified(win) ||
                                   !glfwGetWindowAttrib(win, GLFW_VISIBLE));
      double waitSeconds = ambientWait;
      bool poll = busy || shot || !engineTickAllowed;
#ifdef CHUDWARE_ENABLE_CEF
      const int cefDueMs = ui::cef::msUntilDue();
      if (cefDueMs >= 0)
        waitSeconds = std::min(waitSeconds, static_cast<double>(cefDueMs) * 0.001);
#endif
      // A minimized swap is not guaranteed to block, even if an animation/job is active.
      if (hidden) {
        poll = false;
        waitSeconds = 0.25;
      }
      core::framestats::setIdle(!poll);
      if (poll || waitSeconds <= 0.0)
        glfwPollEvents();
      else
        glfwWaitEventsTimeout(waitSeconds);
#endif
      core::framestats::markWait(timing.elapsedMs());
      // Empty events posted by background workers wake delivery, not three vsync frames.
      // Input (including ImGui's trickled press/release queue) does need settle frames.
      if (ctx->InputEventsQueue.Size > 0)
        postInputFrames = 3;
      else if (postInputFrames > 0)
        --postInputFrames;
      ui::anim::beginFrame();
    }
#ifndef __EMSCRIPTEN__
    // A second launch stood down and handed us its turn (single-instance guard, main.cpp). The
    // user double-clicked expecting a window, so surface this one. Restore ONLY when iconified:
    // glfwRestoreWindow also un-maximizes, and the app starts maximized. glfwFocusWindow is a
    // focus STEAL, which many window managers refuse — RequestWindowAttention then flashes the
    // taskbar entry instead of silently doing nothing.
    if (guiConsumeActivationRequest()) {
      if (glfwGetWindowAttrib(win, GLFW_ICONIFIED))
        glfwRestoreWindow(win);
      glfwShowWindow(win);
      glfwFocusWindow(win);
      glfwRequestWindowAttention(win);
      // ...and if that launch was an "Open with", adopt its document instead of swallowing it.
      // Same deferred open pump the launch-arg path and the File menu use.
      if (std::string doc = guiTakeActivationDoc(); !doc.empty()) {
        st.requestOpenPath = doc;
        st.addLog("[open-with] second launch -> " + doc);
      }
    }
#endif
#ifdef CHUDWARE_ENABLE_CEF
    {
      core::framestats::Scope timing("cef");
      ui::cef::pump_if_due(); // drive CEF's message loop when its deadline is due
    }
    {
      static const char *dbgVendor = std::getenv("CHUDWARE_DEBUG_CEF_VENDOR");
      static long dbgFrame = 0;
      if (dbgVendor && dbgVendor[0]) {
        ++dbgFrame;
        if (dbgFrame == 120) {
          st.activeWorkspace = AppState::Workspace::PartsLibrary;
          ui::parts::startLoginWizard(st, dbgVendor);
        }
        if (dbgFrame == 6000)
          break;
      }
    }
#endif

    // Borderless-window chrome: drag / edge-resize / double-click-maximize off
    // the same title-bar HitLayout the GUI publishes. No-op on Windows (the
    // WndProc handles it).
    {
      core::framestats::Scope timing("chrome");
      chrome::tick(win, &hitLayout);
    }

    if (st.setupDone.exchange(false)) {
      bool ok;
      std::string msg;
      {
        std::lock_guard<std::mutex> lk(st.setupMtx);
        ok = st.setupOk;
        msg = st.setupMsg;
      }
      if (ok) {
        st.addLog("[ok] " + msg);
        st.startupPhase = "Warming up kernel\xe2\x80\xa6";
        st.status = "Warming up kernel...";
        st.tryStartWorker();
        if (st.workerStarted && (st.starterTried || st.docMirror.hasDoc() ||
                                 !st.docTabs.empty() || st.designStarted))
          st.startupPhase.clear();
      } else {
        st.addLog("[err] " + msg);
        st.status = "Setup needed";
        st.statusKind = 2;
        st.startupPhase.clear();
      }
    }

    // Start the native worker only after a real presentation, not a guessed
    // frame count. Keep the browser's existing warmup-frame behavior.
    if (st.workerBootPending) {
#ifdef __EMSCRIPTEN__
      static int s_bootDeferFrames = 0;
      ++s_bootDeferFrames;
      const bool bootReady = s_bootDeferFrames >= 2;
#else
      const bool bootReady = (shot || s_nativeStartup.firstFramePresented) &&
                             !engine::connectionPending();
#endif
      if (bootReady) {
        st.startupPhase = "Warming up kernel\xe2\x80\xa6";
        st.status = "Warming up kernel...";
        st.statusKind = 0;
        st.tryStartWorker();
      }
    }
    if (st.pollWorkerStartup()) {
      if (st.setupRunning.load()) {
        st.startupPhase = "Provisioning runtime\xe2\x80\xa6";
      } else if (st.workerStarted) {
        if (st.starterTried || st.docMirror.hasDoc() || !st.docTabs.empty() ||
            st.designStarted)
          st.startupPhase.clear();
        else
          st.startupPhase = "Opening starter part\xe2\x80\xa6";
      } else {
        st.startupPhase.clear();
      }
    }
#ifndef __EMSCRIPTEN__
    if (st.workerStarted && !s_nativeStartup.workerStartedMarked) {
      s_nativeStartup.workerStartedMarked = true;
      startup_trace::mark("worker started");
    }
#endif

    // Clear the startup loading chip once the engine is ready and we are past
    // the starter-doc gate (starter created, skipped, or user already opened
    // something).
    if (!st.startupPhase.empty() && !st.workerBootPending &&
        !st.setupRunning.load() && st.workerStarted &&
        (st.starterTried || st.docMirror.hasDoc() || !st.docTabs.empty() ||
         st.designStarted)) {
      st.startupPhase.clear();
    }

    // C39: deliver finished async worker calls — their callbacks run HERE, on
    // the GL thread, before this frame's panels draw, so results land like any
    // other state.
    ui::ecadPrimeActiveApp(st);
    core::framestats::Scope rpcTiming("async_rpc");
    st.rpc.drain();
    st.authRpc.drain();   // sign-in results/events: their own queue, same GL-thread contract
    ui::tickMeshReferences(st);
    // Form-routed picks deferred behind queued engine work resolve once the queue is idle.
    if (!st.rpc.busy())
      st.selection.retryDeferred();
    // A chat context chip clicked for another tab selects its entity once that tab is live.
    ui::pumpContextRefFocus(st);
    // Manufacturing panels enqueue intent only. Dispatch after the prior frame's
    // callbacks drain, before hostd events update the read-only mirror.
    ui::dispatchManufactureRequests(st);
    rpcTiming.stop();
    // Commit mesh loads that finished disk-parse on the loader thread
    // (GL upload only here; stale gens dropped so keep-last-good stays on
    // screen).
    {
      core::framestats::Scope timing("mesh_loads");
      st.drainMeshLoads();
    }
#ifdef __EMSCRIPTEN__
    // Browser canvas drop → fsUpload → requestOpenPath (set by
    // chudware_web_drop_done).
    drainWebFileDrop(st);
#endif
    // Drain document refreshes and shared-engine lifecycle events without changing
    // the current tab; in-process cache refreshes use the same routing.
    if (engineTickAllowed) {
      core::framestats::Scope timing("engine_events");
      core::engineEventsTick(st);
    }
    if (engineTickAllowed)
      ecadHubTick(st);
#ifndef __EMSCRIPTEN__
    if (engineTickAllowed)
      ecadRecoveryTick(st);
    if (engineTickAllowed) {
      if (!s_nativeStartup.daemonConnectedMarked) {
        if (auto *c = engine::client(); c && c->connected()) {
          s_nativeStartup.daemonConnectedMarked = true;
          startup_trace::mark("daemon connected");
        }
      }
      if (st.daemonEverConnected && !s_nativeStartup.daemonBootstrapMarked) {
        s_nativeStartup.daemonBootstrapMarked = true;
        startup_trace::mark("daemon bootstrap done");
      }
    }
#endif
    // Multi-document: release closed-tab sessions whose async work (PM preview
    // / sketch / eval callbacks that captured `this`) has now fully drained —
    // safe to destroy once rpc is idle.
    st.sweepRetiredSessions();

    // Live MCP handoff: if the external Chudware MCP server (the engine Claude
    // drives) published a new part, open/refresh it in the viewport so the user
    // watches the AI build. Self-guards and no-ops the vast majority of frames
    // (a single mtime stat). In daemon mode the live preview arrives as
    // doc.changed events with the mesh path (engineEventsTick above) — the
    // .step+ file handoff is legacy only, so skip the poller entirely (no
    // second source of truth).
    if (engineTickAllowed && !engine::daemonMode())
      ui::pollAiCadHandoff(st);

    // C48: click telemetry for interaction debugging — only when
    // CHUDWARE_DEBUG_INPUT is set (pairs with CHUDWARE_LOG_FILE). Logs what ImGui
    // thinks is under every click.
    {
      static const bool dbgInput = std::getenv("CHUDWARE_DEBUG_INPUT") != nullptr;
      if (dbgInput && ImGui::IsMouseClicked(ImGuiMouseButton_Left)) {
        ImGuiContext *g = ImGui::GetCurrentContext();
        char b[256];
        std::snprintf(b, sizeof(b),
                      "[dbg] click (%.0f,%.0f) win=%s item=0x%08X",
                      ImGui::GetIO().MousePos.x, ImGui::GetIO().MousePos.y,
                      g->HoveredWindow ? g->HoveredWindow->Name : "(none)",
                      g->HoveredIdPreviousFrame);
        st.addLog(b);
      }
    }

    // Drain streamed agent progress into the running assistant message of the
    // conversation that OWNS each run, so every conversation updates live.
    // Runs are keyed by conversation id: a background conversation's run
    // streams into its own (parked) transcript and never into whatever is on
    // screen.
    core::framestats::Scope agentTiming("agent_events");
    // Resume at the first unserviced run when the shared budget expires.
    // Keep an index rather than an iterator because run slots can change
    // between frames.
    static size_t nextAgentRun = 0;
    const size_t agentRunCount = st.runs.size();
    auto agentRunIt = st.runs.begin();
    if (agentRunCount != 0) {
      nextAgentRun %= agentRunCount;
      for (size_t skipped = 0; skipped < nextAgentRun; ++skipped)
        ++agentRunIt;
    }
    const auto agentDeadline = std::chrono::steady_clock::now() +
                               std::chrono::milliseconds(2);
    bool servicedAgentRun = false;
    for (size_t visited = 0; visited < agentRunCount;
         ++visited, ++agentRunIt,
         nextAgentRun = (nextAgentRun + 1) % agentRunCount) {
      if (agentRunIt == st.runs.end())
        agentRunIt = st.runs.begin();
      auto &[runConvId, runPtr] = *agentRunIt;
      if (!runPtr)
        continue;
      JobState &job = *runPtr;
      // Stop discards normal rendering; maintenance can still invalidate its
      // owner's context when the outcome is uncertain or committed.
      if (job.stopRequested.load() && !job.isCompaction) {
        std::lock_guard<std::mutex> lk(job.evMtx);
        job.events.clear();
        continue;
      }
      if (!agentEventsPending(job))
        continue;
      if (servicedAgentRun &&
          std::chrono::steady_clock::now() >= agentDeadline) {
        ui::anim::requestFrames();
        break;
      }
      std::vector<ChatMsg> *msgs = conversationMsgs(st, runConvId);
      ChatMsg *active = nullptr;
      if (msgs) {
        for (auto it = msgs->rbegin(); it != msgs->rend(); ++it)
          if (!it->fromUser && it->running) {
            active = &(*it);
            break;
          }
      }
      // Preview trackers are document-scoped; live status is conversation-scoped.
      // A background run still streams into its own bubble.
      const bool isViewedDoc =
          !job.docKey.empty() && job.docKey == st.chatDocKey;
      // The context meter is a property of the CONVERSATION, not of a document: a general-lane
      // turn that only reads files or answers a question carries no docKey at all, so gating it
      // on isViewedDoc pinned the meter at zero for exactly the long chats where context
      // pressure matters most. Conversation::lastContextTokens is already per-session, which
      // says the same thing. Scope it to the conversation the user is looking at.
      const bool isViewedConv = runConvId == st.activeConvId();
      Conversation *parkedConv = nullptr;
      if (!isViewedConv)
        for (auto &conv : st.conversations)
          if (conv.id == runConvId) {
            parkedConv = &conv;
            break;
          }
      AgentEvent e;
      size_t drained = 0;
      servicedAgentRun = true;
      // At least one event advances even if setup used the remaining budget.
      while (drained < 16 &&
             (drained == 0 || std::chrono::steady_clock::now() < agentDeadline) &&
             takeAgentEvent(job, e)) {
        ++drained;
        if (job.stopRequested.load() && e.kind != "orch_agent.context")
          continue;
        // --- route orchestrator events to the Agent Workflow DAG store ---
        if (e.kind.rfind("orch_", 0) == 0) {
          std::string orchKind = e.kind.substr(5); // strip "orch_"
          try {
            if (!e.data)
              continue;
            const nlohmann::json &data = *e.data;
            // Phase 2 (M9/M10): feed the owning conversation's Agent Workflow
            // DAG store. Keyed by runConvId, so every chat builds its OWN
            // execution trace and two concurrent runs never blank or hijack
            // each other's; the Agent Hub renders the viewed conversation's.
            // Otherwise ungated — it must capture agent.* span events even
            // when no chat bubble is active (a subagent branch can outlive the
            // trunk turn). GL-thread only; the stores are plain statics in
            // ai_workspace.cpp.
            ui::feedAgentDagEvent(runConvId, orchKind, data);
            ui::activeWorkFeedEvent(runConvId, orchKind, data);
            // Fold the event into the running turn's inline activity timeline
            // so the conversation transparently shows what the AI is
            // doing/thinking. ask_user questions/answers become question
            // cards instead; an answer can resolve a batch in an OLDER
            // bubble (a composer reply split the turn), so it needs msgs.
            bool steerEvent = false;
            const bool askEvent =
                msgs && (feedAskEvent(*msgs, active, orchKind, data) ||
                         (steerEvent = feedSteerEvent(*msgs, orchKind, data)));
            // A steer the agent just read marks a point of the run: the transcript closes the
            // live message there and opens a fresh one under the steer's bubble. The vector
            // grew, so every pointer into it is stale — re-find the live message, and the rest
            // of this batch streams below the bubble.
            if (steerEvent && data.contains("steer_id") && data["steer_id"].is_string() &&
                ui::placeSteerInTranscript(st, *msgs, data["steer_id"].get<std::string>())) {
              active = nullptr;
              for (auto it = msgs->rbegin(); it != msgs->rend(); ++it)
                if (!it->fromUser && it->running) {
                  active = &(*it);
                  break;
                }
            }
            if (!askEvent && active)
              feedTurnActivity(*active, orchKind, data);
            // Context meter. THREE sources, one per lane — do not collapse:
            //   * agent.context (live Python general lane): the authoritative
            //     feed. Carries {used, window} straight from the provider's
            //     usage record, emitted after every provider completion, so
            //     the meter tracks the CURRENT context size live. `used` is
            //     assigned, never max()'d: a compaction genuinely shrinks the
            //     context and a ratcheting meter would lie afterwards.
            //     `window` is only stored when > 0 so a provider that cannot
            //     report one never wipes a good earlier value.
            //   * agent_call (C++ agent lane): context_tokens_est, the real
            //     per-call size. Peak-tracked because that lane emits an
            //     estimate per call with no shrink semantics.
            //   * agent_output (C++ agent lane): tokens.in fallback — C++
            //     agent_output usually has tokens:null, so agent_call is
            //     preferred, but honour a real number when one arrives.
            // The meter belongs to the run's conversation; preview trackers belong to the
            // currently viewed document.
            //
            // ONE reset point: run_started opens every run on both lanes (the
            // live Python general lane emits it first, then thinking/tool rows
            // and assistant deltas), so the meter can never show the previous
            // run's numbers.
            //
            // agent.context also carries `window_source` ("user" | "catalog" |
            // "probe" | "heuristic" | ""), i.e. HOW the harness arrived at
            // `window`. It is handed to the composer's meter because a
            // heuristic value is a hardcoded per-provider default and must not
            // outrank the curated model catalog the GUI already has: the
            // harness reports 200000 for claude-opus-5 with source
            // "heuristic" while the catalog row for that same id says
            // 1000000. An absent/empty key is treated as heuristic, so an
            // older worker degrades safely.
            if (orchKind == "run_started") {
              ui::storeConversationContext(st.lastContextTokens, st.conversations,
                                           isViewedConv, runConvId, 0);
              ui::storeConversationWindow(st.lastContextWindow, st.lastContextWindowSource,
                                          st.conversations, isViewedConv, runConvId, 0, {});
            } else if (orchKind == "agent.context") {
              if (data.contains("used") &&
                  (data["used"].is_null() || data["used"].is_number_integer())) {
                int stored = -1;  // OMP has no post-compaction occupancy yet.
                if (data["used"].is_number_integer()) {
                  const int used = data["used"].get<int>();
                  if (used >= 0)
                    stored = data.value("source", std::string()) == "estimate"
                                 ? ui::storedContextEstimate(used) : ui::storedContextMeasurement(used);
                  else
                    continue;
                }
                ui::storeConversationContext(st.lastContextTokens, st.conversations,
                                             isViewedConv, runConvId, stored);
              }
              if (data.contains("window") && data["window"].is_number_integer()) {
                const int win = data["window"].get<int>();
                if (win > 0)
                  ui::storeConversationWindow(
                      st.lastContextWindow, st.lastContextWindowSource, st.conversations,
                      isViewedConv, runConvId, win, data.value("window_source", std::string()));
              }
            } else if (orchKind == "agent.session") {
              // The Omp resume key, sent as the turn starts. Persist it now: the old
              // post-turn doctor capture never ran when the daemon died mid-turn
              // (2026-09-27, c217), leaving a 2-hour session unresumable.
#ifdef __EMSCRIPTEN__
              const auto handle = cellAgentSessionHandle(runConvId);
              if (!handle.empty()) {
                ui::OmpSessionLink &link = ui::ompSessionLinkEnsure(runConvId);
                link.sessionId = handle;
                link.sessionFile.clear();
                ui::persistChatHistory(st);
              }
#else
              const std::string file = data.value("omp_session_file", std::string());
              if (!file.empty()) {
                ui::OmpSessionLink &link = ui::ompSessionLinkEnsure(runConvId);
                if (link.sessionFile != file) {
                  link.sessionFile = file;
                  const std::string sid = data.value("omp_session_id", std::string());
                  if (!sid.empty())
                    link.sessionId = sid;
                  ui::persistChatHistory(st);
                }
              }
#endif
            } else if (orchKind == "agent_call") {
              int est = 0;
              if (data.contains("context_tokens_est") &&
                  data["context_tokens_est"].is_number_integer())
                est = data["context_tokens_est"].get<int>();
              if (est <= 0)
                est = data.value("context_tokens_est", 0);
              if (est > 0)
                ui::storeConversationContextPeak(st.lastContextTokens, st.conversations,
                                                 isViewedConv, runConvId, est);
            } else if (orchKind == "agent_output" &&
                       data.contains("tokens") &&
                       data["tokens"].is_object()) {
              int in = data["tokens"].value("in", 0);
              if (in > 0)
                ui::storeConversationContext(st.lastContextTokens, st.conversations,
                                             isViewedConv, runConvId, in);
            } else if (orchKind == "part_named") {
              // Naming stage: stash the AI-chosen part name for the
              // run-finish path (title refresh + auto-save). Result part_name
              // is a backup. Per-run data — correct for a background run too.
              std::string pn = data.value("name", std::string());
              if (!pn.empty()) {
                std::lock_guard<std::mutex> lk(job.mtx);
                job.partName = std::move(pn);
              }
            } else if (orchKind == "run_note") {
              // Agent self-persistence (file-first create / milestone re-save
              // / memory-stage rename) happens in-daemon, so the host's
              // doc_save PLM hook never sees it. Record those saves into
              // project history here — plmAutoRecordSave self-gates (worker
              // up, auto_commit on, path inside the PLM project folder) and
              // is async/non-blocking. Ungated: PLM history is per-document
              // bookkeeping, not viewport state.
              const std::string note = data.value("note", std::string());
              if (note == "file_created" || note == "milestone_saved" ||
                  note == "file_renamed") {
                const std::string p = data.value("path", std::string());
                if (!p.empty())
                  ui::plmAutoRecordSave(st, p);
              }
            } else if (orchKind == "doc.preview") {
              // A preview can only be paired with the exact tree revision on screen.
              const std::string prevDocId =
                  data.value("doc_id", std::string());
              const int prevRev = data.value("revision", 0);
              const std::string aicm = data.value("aicm_path", std::string());
              const bool midEdit =
                  (st.sketchMode && st.sketchMode->active()) ||
                  (st.sketch3dMode && st.sketch3dMode->active()) ||
                  (st.propertyManager && st.propertyManager->active());
              // Match the run's target: active DocMirror doc (launch always
              // targets the then-active doc; switching away must not
              // clobber).
              if (!midEdit && !prevDocId.empty() && st.isActiveMirrorDoc() &&
                  prevDocId == st.docMirror.docId() &&
                  prevRev == st.docMirror.revision() && !aicm.empty() &&
                  (st.committedMeshDocId != prevDocId || st.committedMeshRevision != prevRev)) {
                st.loadDocMeshAsync(aicm, false, true, true, true);
                const std::string phase =
                    "Building\xe2\x80\xa6 rev " + std::to_string(prevRev);
                if (isViewedConv)
                  st.livePhase = phase;
                else if (parkedConv)
                  parkedConv->viewLivePhase = phase;
                if (active)
                  active->liveStatus = phase;
              }
            }
          } catch (...) {
          }
          // Also update the compact status line in the chat
          const bool terminalNote =
              e.data && ((orchKind == "sub_run" &&
                          e.data->value("status", std::string()) != "running" &&
                          e.data->value("status", std::string()) != "started") ||
                         orchKind == "run_finished" ||
                         (orchKind == "status" &&
                          (e.data->value("phase", std::string()) == "error" ||
                           e.data->value("phase", std::string()) == "failed" ||
                           e.data->value("phase", std::string()) == "cancelled")));
          if (!e.notes.empty() && !terminalNote) {
            if (isViewedConv)
              st.agentV2Status = e.notes;
            else if (parkedConv)
              parkedConv->viewAgentV2Status = e.notes;
            if (active && active->running)
              active->liveStatus = e.notes;
          }
        }
      }
      if (agentEventsPending(job))
        ui::anim::requestFrames();
    }

    // A finished run belongs to ITS conversation. Walk every run slot and route
    // each completed run's final text / plan gate / error card into the
    // conversation it was launched from. Viewport and tab state is document-scoped;
    // chat status belongs to the conversation that owns the run.
    for (auto &[runConvId, runPtr] : st.runs) {
      // Finalization must follow every queued stream event, even across frames.
      if (!runPtr || agentEventsPending(*runPtr) || !runPtr->ready.exchange(false))
        continue;
      JobState &job = *runPtr;
      std::lock_guard<std::mutex> lk(job.mtx);
      const bool isViewedDoc = !job.runDocId.empty()
          ? st.isActiveMirrorDoc() && st.docMirror.docId() == job.runDocId
          : (!job.docKey.empty() && job.docKey == st.chatDocKey);
      const bool isViewedConv = runConvId == st.activeConvId();
      Conversation *parkedConv = nullptr;
      if (!isViewedConv)
        for (auto &conv : st.conversations)
          if (conv.id == runConvId) {
            parkedConv = &conv;
            break;
          }
      std::vector<ChatMsg> *msgsPtr = conversationMsgs(st, runConvId);
      // The turn is over: a question it asked can no longer be answered.
      if (msgsPtr) {
        closeOpenAsks(*msgsPtr);
        // A steer the finished run never confirmed did not reach the agent. A Stop's
        // own outcome (carried / delivered) lands with the cancel response instead.
        if (!job.stopRequested.load() && job.stopReason != "cancelled")
          settleOpenSteers(*msgsPtr);
      }
      auto setStatus = [&](const char *text, int kind) {
        if (isViewedConv) {
          st.status = text;
          st.statusKind = kind;
        } else if (parkedConv) {
          parkedConv->viewStatus = text;
          parkedConv->viewStatusKind = kind;
        }
      };
      if (isViewedConv) {
        st.livePhase.clear();
        st.agentV2Status.clear();
      } else if (parkedConv) {
        parkedConv->viewLivePhase.clear();
        parkedConv->viewAgentV2Status.clear();
      }
      if (job.isCompaction) {
        // Maintenance never adopts/refreshes documents, invents answer prose, or asks for a plan.
        // isViewedConv also controls the receipt's conversation-owned context.
        ChatMsg* compactionMessage = nullptr;
        if (msgsPtr) for (auto it = msgsPtr->rbegin(); it != msgsPtr->rend(); ++it)
          if (!it->fromUser && it->compactionRun) {
            compactionMessage = &*it;
            break;
          }
        const bool committed = ui::applyCommittedCompactionReceipt(
            job.compactionTurn, st.lastContextTokens, st.conversations,
            isViewedConv, runConvId, compactionMessage);
        const std::string outcome = jstr(job.compactionTurn, "outcome");
        const bool stopped = !committed && (outcome == "cancelled" ||
            (outcome.empty() && (job.stopRequested.load() || job.stopReason == "cancelled")));
        const bool skipped = !committed &&
                             (outcome == "skipped" || (outcome.empty() && job.stopReason == "skipped"));
        const nlohmann::json& compaction =
            job.compactionTurn.contains("compaction") &&
                    job.compactionTurn["compaction"].is_object()
                ? job.compactionTurn["compaction"] : job.compactionTurn;
        std::string notice;
        if (stopped) {
          notice = "Compaction stopped.";
        } else if (skipped) {
          notice = "Compaction skipped; the context was not changed.";
        } else if (committed) {
          notice = compactionNotice(compaction);
        } else {
          std::string reason = !job.errorDetail.empty() ? job.errorDetail
                               : !job.error.empty() ? job.error
                               : job.stopReason.empty() ? "Compaction failed" : job.stopReason;
          if (reason.find("worker_lane_busy") != std::string::npos ||
              reason.find("worker lane busy") != std::string::npos)
            reason = "Available when the current run finishes.";
          notice = "Couldn't compact this conversation: " + reason;
          if (isViewedConv) st.toast(ui::ToastLevel::Warn, notice);
          ui::restoreUnsentForConversation(st, runConvId, job.docKey, job.compactionCommand);
        }
        setStatus(stopped ? "Compaction stopped."
                          : skipped ? "Compaction skipped."
                          : committed ? "Context compacted." : "Compaction failed.",
                  committed ? 0 : 1);
        // A resumed maintenance session remains live, including when no agent.session event landed.
#ifdef __EMSCRIPTEN__
        if (const std::string handle = cellAgentSessionHandle(runConvId); !handle.empty()) {
          auto& link = ui::ompSessionLinkEnsure(runConvId);
          link.sessionId = handle;
          link.sessionFile.clear();
          link.runtimeLive = true;
        }
#else
        if (const std::string file = jstr(job.compactionTurn, "omp_session_file");
            !file.empty()) {
          auto& link = ui::ompSessionLinkEnsure(runConvId);
          link.sessionFile = file;
          const std::string sessionId = jstr(job.compactionTurn, "omp_session_id");
          if (!sessionId.empty()) link.sessionId = sessionId;
          link.runtimeLive = true;
        }
#endif
        if (job.compactionTurn.contains("omp_runtime_live") &&
            job.compactionTurn["omp_runtime_live"].is_boolean())
          ui::ompSessionLinkEnsure(runConvId).runtimeLive =
              job.compactionTurn["omp_runtime_live"].get<bool>();
        if (compactionMessage) {
          ui::settleCompactionMessage(*compactionMessage, stopped ? "cancelled"
                                        : skipped || committed ? "interrupted" : "failed");
          ActivityItem* row = nullptr;
          for (auto& item : compactionMessage->activity)
            if (item.rowKind == 17 && item.stage.find("compaction:") != std::string::npos) {
              row = &item;
              break;
            }
          if (!row) {
            ActivityItem item;
            item.rowKind = 17;
            item.stage = "subrun:compaction:" + job.runId;
            compactionMessage->activity.push_back(std::move(item));
            row = &compactionMessage->activity.back();
          }
          row->status = committed ? "done" : stopped ? "cancelled" : skipped ? "skipped" : "fail";
          row->title = committed ? "Context compacted" : stopped ? "Compaction stopped"
                       : skipped ? "Compaction skipped" : "Context compaction failed";
          row->reason.clear();
          row->streaming = false;
          ++row->revision;
          compactionMessage->errKind = ERR_NONE;
          compactionMessage->errorHuman.clear();
          compactionMessage->errorFull.clear();
          compactionMessage->planApprovePending = false;
          compactionMessage->text = std::move(notice);
          ++compactionMessage->revision;
        }
        job.stopRequested = false;
        job.runId.clear();
        st.agentV2Running = st.anyRunActive();
        st.addLog(stopped ? "[..] /compact stopped"
                         : skipped ? "[..] /compact skipped"
                         : committed ? "[..] /compact ok" : "[..] /compact failed");
        ui::persistChatHistory(st);
        continue;
      }
      if (job.stopRequested.load() || job.stopReason == "cancelled") {
        // The user stopped this run (or engine returned cancel_result after
        // agent_cancel). Discard quietly: leave the chat message as the Stop
        // handler finalized it, and skip the doc rebuild/resync and any new-tab
        // adoption. Do NOT resurrect an error card.
        job.stopRequested = false;
        st.agentV2Running = st.anyRunActive();
        setStatus("Stopped.", 1);
        // Ensure any still-running assistant bubble is settled as Stopped (not
        // error).
        if (msgsPtr) {
          for (auto it = msgsPtr->rbegin(); it != msgsPtr->rend(); ++it) {
            if (!it->fromUser && it->running) {
              it->running = false;
              it->liveStatus = "Stopped";
              it->errKind = ERR_NONE;
              break;
            }
          }
        }
        // NOTE: job.mtx is already held by `lk` above — re-locking here
        // self-deadlocked the GL thread (or aborted via EDEADLK) the moment a
        // stopped run actually returned status=cancelled.
        job.runId.clear();
        job.error.clear();
        job.errorDetail.clear();
        job.errorHuman.clear();
        st.addLog("[..] run result discarded (stopped by user)");
        // The success path persists at the end of its branch (line ~4693, under this same
        // lock); this one never did, so a stopped run's settled transcript lived only in
        // memory until some later unrelated save. "I stopped it and closed the app" must
        // not be the one way to lose a conversation.
        ui::persistChatHistory(st);
      } else {
        // --- C22b: if this was a v2 run, resync the DocMirror tree + reload
        // mesh --- The worker already rebuilt the doc; call doc_rebuild to
        // refresh the tree and get the latest aicm_path, then load it into the
        // viewport.
        const bool wasV2 = st.agentV2Running;
        // Ground truth, ungated: another conversation may still be working, and
        // a background run finishing must not lie about that either way.
        st.agentV2Running = st.anyRunActive();
        // Everything below mutates singular viewport / tab / auto-save state,
        // so it only runs when this run's document is the one on screen.
        if (isViewedDoc) {
          // plan_ready mutates nothing — skip doc_activate / mesh reload.
          if (!job.planReady && st.docMirror.hasDoc() && job.error.empty()) {
            // C39: a forced rebuild can take tens of seconds on a heavy doc —
            // the old sync call froze the window right after the agent
            // finished. Queue async; the callback (a later frame's drain, GL
            // thread) resyncs and reloads. doc_activate: the agent's final
            // apply_ops left the engine's read-cache compile warm, so this
            // serves the tree + mesh path without recompiling. When live
            // previews already landed the final revision, mesh is current —
            // skip the sticky "Refreshing model..." pill (still resync the
            // tree).
            const int finalRev = job.finalRevision;
            const bool meshAlreadyCurrent =
                finalRev > 0 && st.committedMeshDocId == st.docMirror.docId() &&
                st.committedMeshRevision == finalRev;
            if (isViewedConv) {
              if (!meshAlreadyCurrent)
                st.livePhase = "Refreshing model...";
              else
                st.livePhase.clear();
            } else if (parkedConv) {
              parkedConv->viewLivePhase = meshAlreadyCurrent ? "" : "Refreshing model...";
            }
            // Multi-document: capture the doc this rebuild targets. If the user
            // switches to another tab before the async result drains, the
            // callback must NOT resync / reload the FOREGROUND doc with this
            // (background) doc's result — the foreground mirror + mesh would be
            // corrupted. The backgrounded doc picks up the fresh tree
            // + mesh when its tab is reactivated (switchToTab re-activates +
            // reloads).
            const std::string rebuildDocId = st.docMirror.docId();
            ++st.rebuildsInFlight;
            st.rpc.call(
                "doc_activate", {{"doc_id", rebuildDocId}, {"origin", "system"}},
                [&st, rebuildDocId, runConvId, finalRev,
                 meshAlreadyCurrent](const core::RpcResult &r) {
                  --st.rebuildsInFlight;
                  if (runConvId == st.activeConvId()) {
                    if (st.livePhase == "Refreshing model...")
                      st.livePhase.clear();
                  } else {
                    for (auto &conv : st.conversations)
                      if (conv.id == runConvId && conv.viewLivePhase == "Refreshing model...") {
                        conv.viewLivePhase.clear();
                        break;
                      }
                  }
                  if (!st.isActiveMirrorDoc() || st.docMirror.docId() != rebuildDocId) {
                    core::requireDocMesh(rebuildDocId, finalRev);
                    st.addLog("[v2] doc_rebuild result for " + rebuildDocId +
                              " ignored — active doc changed");
                    return;
                  }
                  if (r.ok && r.result.is_object() && !r.result.contains("code")) {
                    const int revision = r.result.value("revision", -1);
                    if (revision >= 0 && revision < st.docMirror.revision())
                      return;
                    std::string aicm;
                    bool usableMesh = false;
                    if (r.result.contains("tess") && r.result["tess"].is_object() &&
                        !r.result.contains("mesh_error") &&
                        !r.result["tess"].contains("mesh_error")) {
                      const auto &tess = r.result["tess"];
                      if (tess.contains("aicm_path") && tess["aicm_path"].is_string()) {
                        aicm = tess["aicm_path"].get<std::string>();
                        usableMesh = !aicm.empty() ||
                            (tess.contains("bodies") && tess["bodies"].is_array() &&
                             tess["bodies"].empty());
                      }
                    }
                    const bool haveMesh = meshAlreadyCurrent && revision == st.committedMeshRevision;
                    auto admitMesh = [&st, rebuildDocId](int rev, const std::string &mesh,
                                                         bool usable) {
                      if (!usable) {
                        core::requireDocMesh(rebuildDocId, rev);
                      } else if (!mesh.empty()) {
                        st.loadDocMeshAsync(mesh);
                      } else {
                        st.unloadDocMesh();
                        st.committedMeshDocId = rebuildDocId;
                        st.committedMeshRevision = rev;
                        core::meshCatchupFinished(rebuildDocId, rev, true);
                      }
                    };
                    if (!r.result.contains("tree") || !r.result["tree"].is_object()) {
                      // Old activation responses need a tree read, but never a
                      // synchronous resync from this GL-thread completion.
                      st.rpc.callPrimary("doc_tree", {{"doc_id", rebuildDocId}},
                          [&st, rebuildDocId, revision, haveMesh, usableMesh,
                           aicm, admitMesh](const core::RpcResult &tree) {
                        if (!tree.ok || st.docMirror.docId() != rebuildDocId ||
                            tree.result.value("revision", -1) < st.docMirror.revision())
                          return;
                        if (!st.docMirror.ingestTreeResponse(tree.result))
                          return;
                        st.syncActiveTabTitle();
                        if (!haveMesh && revision == st.docMirror.revision())
                          admitMesh(revision, aicm, usableMesh);
                        else if (!haveMesh)
                          core::requireDocMesh(rebuildDocId, st.docMirror.revision());
                        st.addLog("[v2] asynchronous tree refresh after run: rev=" +
                                  std::to_string(st.docMirror.revision()));
                      }, 5000);
                      return;
                    }
                    if (!st.docMirror.ingestTreeResponse(r.result))
                      return;
                    st.syncActiveTabTitle();
                    // C40: doc_activate is an ops-response — the mesh path
                    // lives at tess.aicm_path. Skip reload when live previews
                    // already showed this revision (meshAlreadyCurrent).
                    if (!haveMesh) admitMesh(revision, aicm, usableMesh);
                    st.addLog(
                        "[v2] doc_activate after run: rev=" +
                        std::to_string(st.docMirror.revision()) +
                        (haveMesh ? " (mesh already current)" : ""));
                  } else {
                    core::requireDocMesh(rebuildDocId, finalRev);
                    st.addLog("[err] doc_activate after v2 run: " + r.err);
                    st.toast(
                        ui::ToastLevel::Error,
                        "Agent run finished but the model couldn't refresh");
                  }
                },
                60000);
          }
        }
        // --- end C22b ---
        std::string assistant;
        // The actionable one-liner (error_human) and the WHOLE raw failure text.
        // Both ride onto the assistant ChatMsg: headline + expandable "Error details" body.
        std::string assistantErrHuman, assistantErrFull;
        int assistantErr = ERR_NONE;
        if (!job.error.empty() && job.errorIsCad) {
          // The CAD script didn't build. That's normal territory for an AI
          // design loop (it usually self-corrects mid-run) — report it calmly,
          // details on demand, and don't dress a modeling problem up as an app
          // crash.
          std::string cause = errSummary(job.error);
          setStatus("Build didn't converge", 1);
          st.addLog("[cad] " + cause);
          assistantErr = ERR_CAD;
          if (job.kind == JOB_BUILD) {
            st.buildErrSummary = cause;
            st.buildErrFull = job.error;
          } else {
            // errSummary() clips the trace; keep the whole thing on the card so
            // "Error details" reaches it without hunting through the timeline.
            assistantErrFull = job.error;
            assistant =
                "I couldn't get this design to build cleanly - the CAD code "
                "kept "
                "failing.\nLast error: " +
                cause +
                "\nThis is a modeling problem, "
                "not an app problem. Try rephrasing or simplifying the request "
                "(each iteration's Error details has the full trace).";
          }
        } else if (!job.error.empty() && job.stopReason == "turn_budget") {
          // The agent was still working when the turn's time budget ran out and the
          // worker aborted the run (services/agent/omp_host.py). That is not an app
          // failure: no red "System error", no error toast — say what happened and how
          // to go on.
          setStatus("Turn time limit reached", 1);
          st.addLog("[agent] " + job.error);
          assistantErr = ERR_CAD;
          assistantErrFull = job.stopReason + " — " + job.error;
          assistant = job.error;
        } else if (!job.error.empty()) {
          setStatus("System error", 2);
          // Prefer the structured provider detail when present (longer, more
          // actionable).
          std::string full =
              !job.errorDetail.empty() ? job.errorDetail : job.error;
          if (!job.stopReason.empty() &&
              full.find(job.stopReason) == std::string::npos)
            full = job.stopReason + " — " + full;
          // The 400-char cut feeds the log line, the toast and the collapsed
          // card prose ONLY. `full` is kept whole on the message (same deal as
          // st.buildErrFull for CAD build errors) so the card's "Error details"
          // box can show every byte instead of losing the tail forever.
          size_t cut = full.size() > 400 ? 400 : full.size();
          while (cut > 0 && cut < full.size() &&
                 ((unsigned char)full[cut] & 0xC0) == 0x80)
            --cut; // never split a UTF-8 sequence
          std::string e = full.substr(0, cut);
          if (cut < full.size())
            e += "\xe2\x80\xa6";
          assistantErrFull = full;
          assistantErrHuman = job.errorHuman;
          st.addLog("[err] " + e);
          st.toast(ui::ToastLevel::Error,
                   "System error: " + errSummary(full)); // W1-B
          assistantErr = ERR_SYSTEM;
          // A missing Python module means the venv is incomplete — auto-repair
          // once.
          bool missingModule =
              full.find("ModuleNotFoundError") != std::string::npos ||
              full.find("No module named") != std::string::npos;
          if (missingModule && !st.importRepairTried &&
              !st.setupRunning.load()) {
            st.importRepairTried = true;
            st.addLog("[..] runtime module missing — auto-repairing (use "
                      "Chudware > Repair runtime if this persists)...");
            st.startSetup(true);
            assistant = "A required module is missing from the runtime — "
                        "repairing now (may take a minute). Try again once the "
                        "console shows setup complete.";
          } else {
            assistant = "Something went wrong on the app side (your design is "
                        "fine):\n" +
                        e;
            // Point at Settings only for credential-shaped failures; other
            // errors (connection refused, model missing) carry their own
            // guidance.
            // Match against the WHOLE text, not the collapsed excerpt — the
            // provider's status code often sits past the 400-char cut.
            std::string low = full;
            for (auto &c : low)
              c = (char)std::tolower((unsigned char)c);
            static const char *kAuthMarks[] = {
                "401",         "403",       "unauthorized", "forbidden",
                "api key",     "api_key",   "apikey",       "authentication",
                "invalid key", "credential"};
            for (const char *mark : kAuthMarks) {
              if (low.find(mark) != std::string::npos) {
                assistant += "\n(This looks like an authentication problem - "
                             "set your provider/API key in Settings.)";
                break;
              }
            }
            static const char *kRateMarks[] = {"429", "rate limit", "rate_limit",
                                               "too many requests"};
            for (const char *mark : kRateMarks) {
              if (low.find(mark) != std::string::npos) {
                assistant += "\n(The provider is rate-limiting you - wait a "
                             "moment and resend, or switch to a different model "
                             "in Settings.)";
                break;
              }
            }
            static const char *kContextMarks[] = {
                "context_length_exceeded", "maximum context length",
                "prompt is too long", "too many tokens"};
            for (const char *mark : kContextMarks) {
              if (low.find(mark) != std::string::npos) {
                assistant += "\n(This turn outgrew the model's context window - "
                             "start a new session or trim the attached files / "
                             "history, then try again.)";
                break;
              }
            }
          }
        } else if (!job.stl.empty()) {
          Mesh m;
          std::string e;
          if (loadBinarySTL(job.stl, m, e)) {
            renderer.setMesh(m);
            setStatus("Done", 0);
            st.modelInfo = job.info;
            st.buildErrSummary.clear();
            st.addLog("[ok] built  " + job.info);
            // Refresh the parts panel for the new model (cleared for single
            // parts so the panel disappears), then re-load assemblies
            // color-coded per part.
            st.asmStl = job.stl;
            st.asmParts = job.parts;
            st.asmFit = job.fit;
            st.selectedPart = -1;
            if (job.isAssembly)
              st.showPartMesh(-1);
            if (job.isAssembly && job.fit.present) {
              assistant = "Done - " + job.info + ".\n" +
                          (job.fit.ok ? "Fit OK - " : "Fit issue - ") +
                          job.fit.summary +
                          "\nSee the Viewport (parts list + fit readout). Ask "
                          "for a tweak and I'll revise.";
              st.addLog((job.fit.ok ? "[ok] fit: " : "[err] fit: ") +
                        job.fit.summary);
            } else {
              assistant =
                  "Done - " + job.info +
                  ".\nSee the Viewport. Ask for a tweak and I'll revise.";
            }
            if ((job.kind == JOB_DESIGN || job.kind == JOB_CHAT) &&
                !job.converged)
              assistant +=
                  "\n(Stopped at the iteration cap - say \"keep going\" "
                  "and I'll keep refining this same design.)";
          } else {
            setStatus("STL load failed", 2);
            st.addLog("[err] " + e);
            assistant = "Built it, but couldn't load the mesh: " + e;
            assistantErr = ERR_SYSTEM;
          }
        } else if (wasV2 && job.planReady && job.error.empty()) {
          // User-facing plan gate (manual Plan / advisor escalate
          // needs_user_decision). Agent-decided plans never land here — they
          // execute after in-core advisor OK.
          const bool escalate = (job.stopReason == "needs_user_decision");
          setStatus(escalate ? "Plan needs your decision"
                             : "Plan ready — need edits, or start execution?",
                    0);
          if (!job.missionVerdict.empty())
            assistant = job.missionVerdict;
        } else if (wasV2 && job.isMission && job.missionPartial &&
                   job.error.empty()) {
          // status=="partial": some tasks built, at least one failed. Geometry
          // is real — keep the doc, show warning-accent card (NOT error, NOT
          // full done).
          setStatus("Partially complete", 1); // warning
          int n = job.missionTasksCompleted;
          int m = job.missionTasksTotal > 0
                      ? job.missionTasksTotal
                      : (n + job.missionTasksFailed);
          std::string reason = !job.errorDetail.empty() ? job.errorDetail
                                                           : job.stopReason;
          if (reason.empty())
            reason = "unknown";
          char head[320];
          if (m > 0)
            std::snprintf(
                head, sizeof(head),
                "Partially complete — %d/%d tasks built (first failure: %s)", n,
                m, reason.c_str());
          else
            std::snprintf(head, sizeof(head),
                          "Partially complete (first failure: %s)",
                          reason.c_str());
          // Prefer composed head as the card body; append mission verdict if it
          // adds more.
          assistant = head;
          if (!job.missionVerdict.empty() &&
              job.missionVerdict.find("Partially complete") ==
                  std::string::npos) {
            assistant += "\n\n";
            assistant += job.missionVerdict;
          } else if (!job.missionVerdict.empty() &&
                     job.missionVerdict != assistant) {
            // missionVerdict already has the head-shaped text — use it.
            assistant = job.missionVerdict;
          }
          assistantErr = ERR_PARTIAL;
          st.addLog(std::string("[mission] partial: ") + head);
        } else if (wasV2 && job.isMission &&
                   !job.missionVerdict.empty() && job.error.empty()) {
          // Mission run: the report/verdict prose IS the assistant's reply (it
          // already narrates what was built + the analysis conclusion). The
          // doc rebuild was handled above; we just surface the
          // verdict as the bubble text.
          setStatus("Done", 0);
          assistant = job.missionVerdict;
        } else if (wasV2 && job.error.empty()) {
          // C40: a v2 orchestrator run has no job.stl — the doc itself was
          // built. Without this branch the assistant bubble finished EMPTY (the
          // run looked dead even when it succeeded). Summarize from the run
          // result; the tree/viewport refresh lands via the async doc_rebuild
          // above.
          if (job.converged) {
            setStatus("Done", 0);
            if (job.generalLane) {
              // The general agent never enters the geometry pipeline, so the
              // built-parametric-features boilerplate below would be a claim
              // nobody made: print the turn's own handoff instead, and say
              // plainly when it produced no account of itself.
              assistant = job.agentSummary.empty()
                              ? std::string(
                                    "The agent finished without reporting what it "
                                    "did, and no geometry was built. Nothing was "
                                    "changed - try rephrasing the request.")
                              : job.agentSummary;
            } else
              assistant = "Done - I built this as parametric features; see the "
                          "FeatureManager "
                          "tree and the viewport. Switch to \"Edit part\" to "
                          "ask for changes, "
                          "or edit any feature directly (double-click / "
                          "right-click it).";
          } else if (job.generalLane && !job.agentSummary.empty()) {
            // Same reason the converged branch above prefers agentSummary: the
            // general lane touched NO geometry, so the boilerplate below is a
            // false claim ("whatever was built so far is in the FeatureManager
            // tree" when nothing was built), and the turn's own handoff is the
            // only real account of why it stopped. Before this, agentSummary was
            // captured by jobs.cpp and then rendered only on success, so a model
            // that declined the work with a paragraph of reasoning and three
            // offered alternatives surfaced as "I stopped before fully
            // finishing (blocked)" and every word of it was discarded
            // (live 2026-08-27, pharm-press tooling request).
            setStatus("Stopped early", 1);
            assistant = job.agentSummary;
            assistantErrHuman = job.errorHuman;
          } else {
            setStatus("Stopped early", 1);
            assistantErr = ERR_CAD;
            // Prefer errorDetail (provider HTTP body / auth message) so the
            // card never falls back to "unknown reason" when stopReason was
            // dropped.
            std::string why =
                !job.errorDetail.empty()
                    ? job.errorDetail
                    : (job.stopReason.empty() ? "unknown reason"
                                                 : job.stopReason);
            if (!job.stopReason.empty() && !job.errorDetail.empty() &&
                job.stopReason != job.errorDetail)
              why = job.stopReason + " — " + job.errorDetail;
            assistantErrHuman = job.errorHuman;
            assistantErrFull = why;
            assistant = "I stopped before fully finishing (" + why +
                        "). Whatever was built "
                        "so far is in the FeatureManager tree - check the "
                        "Activity panel "
                        "for the step-by-step log, or rephrase and try again.";
          }
        }
        if (job.kind == JOB_DESIGN || job.kind == JOB_CHAT) {
          ChatMsg *active = nullptr;
          if (msgsPtr) {
            for (auto it = msgsPtr->rbegin(); it != msgsPtr->rend(); ++it)
              if (!it->fromUser && it->running) {
                active = &(*it);
                break;
              }
          }
          if (active) {
            active->running = false;
            active->liveStatus.clear();
            active->errKind = assistantErr;
            // Headline + untruncated raw text for the error card. The streamed
            // run_finished event may already have set errorHuman — don't wipe
            // it with an empty final-result value.
            if (!assistantErrHuman.empty())
              active->errorHuman = assistantErrHuman;
            if (!assistantErrFull.empty())
              active->errorFull = assistantErrFull;
            active->resultPartial =
                job.missionPartial && (assistantErr == ERR_PARTIAL);
            // User-facing plan gate only (manual Plan / needs_user_decision).
            if (job.planReady && assistantErr == ERR_NONE) {
              active->planApprovePending = true;
              if (active->planApproveRequest.empty()) {
                for (auto it = msgsPtr->rbegin(); it != msgsPtr->rend(); ++it) {
                  if (it->fromUser && !it->text.empty()) {
                    active->planApproveRequest = it->text;
                    break;
                  }
                }
              }
              // Seed plan section + escalate reason from payload.
              if (!job.planReadyJson.empty()) {
                try {
                  auto stash = nlohmann::json::parse(job.planReadyJson);
                  if (stash.contains("reason") && stash["reason"].is_string())
                    active->planApproveReason =
                        stash["reason"].get<std::string>();
                  if (!(active->planState.present || active->plan.present)) {
                    nlohmann::json planObj = nlohmann::json::object();
                    if (stash.contains("plan") && stash["plan"].is_object())
                      planObj = stash["plan"];
                    else if (stash.contains("plan") &&
                             stash["plan"].is_string()) {
                      if (assistant.empty())
                        assistant = stash["plan"].get<std::string>();
                    }
                    if (!planObj.empty()) {
                      if (planObj.contains("shape") ||
                          planObj.contains("items"))
                        feedTurnActivity(*active, "plan_state", planObj);
                      else
                        feedTurnActivity(*active, "plan", planObj);
                    }
                    if (!(active->planState.present || active->plan.present) &&
                        stash.contains("tasks") && stash["tasks"].is_array()) {
                      nlohmann::json ms = nlohmann::json::object();
                      ms["request"] = active->planApproveRequest;
                      ms["tasks"] = stash["tasks"];
                      feedTurnActivity(*active, "mission_started", ms);
                    }
                  }
                } catch (...) {
                }
              }
            } else if (!job.planReady && active->planApprovePending) {
              // Run finished without a user gate — drop any leftover draft flag
              // (e.g. Plan mode that failed before plan_ready).
              active->planApprovePending = false;
              active->planApproveReason.clear();
            }
            // Replace (never append) so a streamed waterfall isn't duplicated.
            // Prefer authoritative final text when present; keep streamed text
            // if the job produced no summary (non-v2 / empty assistant). On
            // plan_ready: never dump raw JSON; only soft summary / existing
            // stream.
            if (!assistant.empty() && assistantErr != ERR_NONE &&
                !active->text.empty()) {
              // A failure after streamed prose: APPEND. The timeline already drew
              // that prose inline and the error card renders only what m.text
              // carries beyond it (timelineProseTail); replacing m.text broke that
              // prefix, so the tail came back empty and the failure card was never
              // drawn — a 2 h turn ended with nothing but a red status-bar word.
              active->text += "\n\n" + assistant;
            } else if (!assistant.empty()) {
              // If stream already equals or contains the final prose, still
              // replace with the authoritative final so partial streams
              // finalize cleanly.
              active->text = assistant;
            }
            // else: leave active->text as-is (streamed deltas or empty)
          }
          // No misdelivery fallback: when this run's OWN conversation has no
          // running bubble left, the answer has nowhere legitimate to land.
          // Appending it to whatever conversation happens to be visible is
          // exactly the cross-conversation delivery bug this routing kills.
        }
        // Persist finished transcript so reopen/switch keeps assistant text +
        // artifacts.
        ui::persistChatHistory(st);
      } // end else (run not stopped by the user)
    } // end per-run finished-result routing
    agentTiming.stop();

    // C16 tree shot: run the doc smoke once the worker is ready (fires the
    // first time workerStarted is true after a wantTree shot). Synchronous
    // calls are OK here since we defer the screenshot to frame >= 10 (giving
    // extra frames for the tree to repaint).
    if (st.wantTree && !st.treeShotDone && st.workerStarted) {
      st.treeShotDone = true;
      std::string err;
      if (st.docMirror.newPart(st.worker, err)) {
        std::printf("doc_new: doc_id=%s rev=%d\n", st.docMirror.docId().c_str(),
                    st.docMirror.revision());
        std::fflush(stdout);
        nlohmann::json boxOps = nlohmann::json::array();
        boxOps.push_back(
            {{"op", "add_feature"},
             {"feature",
              {{"type", "box"},
               {"name", "Box1"},
               {"params",
                {{"length", {{"value", 50.0}, {"expr", nullptr}}},
                 {"width", {{"value", 40.0}, {"expr", nullptr}}},
                 {"height", {{"value", 30.0}, {"expr", nullptr}}}}}}}});
        auto r1 = st.docMirror.applyOps(st.worker, boxOps);
        std::printf("add_box: ok=%d rev=%d aicm=%s\n", (int)r1.ok, r1.revision,
                    r1.aicm_path.c_str());
        std::fflush(stdout);
        if (r1.ok && !r1.aicm_path.empty()) {
          std::string le;
          st.loadDocMesh(r1.aicm_path, le);
        }
        // Headless appearance smoke (CHUDWARE_APPR): apply a vivid colour so a
        // screenshot confirms the GL renderer reflects a set_appearance colour
        // (not the default grey).
        if (std::getenv("CHUDWARE_APPR")) {
          float orange[3] = {0.95f, 0.45f, 0.10f};
          applyModelAppearance(st, orange, 0.6f, 0.35f);
          std::printf("appr: applied orange\n");
          std::fflush(stdout);
        }
        std::string selStr, serr;
        selStr = st.docMirror.resolvePick(st.worker, 1, "face", 1, serr);
        std::printf("resolve_pick: selector=%s err=%s\n", selStr.c_str(),
                    serr.c_str());
        std::fflush(stdout);
        nlohmann::json edgeRef;
        try {
          edgeRef = nlohmann::json::parse(
              R"({"kind":"edge","surf":"line","centroid":[0,0,15],"axis":[0,0,1],"area_or_len":30,"owner_feature":null,"hint_index":0})");
        } catch (...) {
        }
        if (!edgeRef.is_null()) {
          nlohmann::json fo = nlohmann::json::array();
          fo.push_back(
              {{"op", "add_feature"},
               {"feature",
                {{"type", "fillet"},
                 {"name", "Fillet1"},
                 {"params", {{"radius", {{"value", 3.0}, {"expr", nullptr}}}}},
                 {"refs", {{"edges", nlohmann::json::array({edgeRef})}}}}}});
          auto r2 = st.docMirror.applyOps(st.worker, fo);
          std::printf("add_fillet: ok=%d rev=%d err=%s\n", (int)r2.ok,
                      r2.revision, r2.error_message.c_str());
          std::fflush(stdout);
          if (r2.ok && !r2.aicm_path.empty()) {
            std::string le;
            st.loadDocMesh(r2.aicm_path, le);
          }
        }
        auto ru = st.docMirror.undo(st.worker);
        std::printf("undo: ok=%d rev=%d features=%d\n", (int)ru.ok, ru.revision,
                    (int)(ru.tree.is_object() && ru.tree.contains("features") &&
                                  ru.tree["features"].is_array()
                              ? ru.tree["features"].size()
                              : 0));
        std::fflush(stdout);
        if (ru.ok && !ru.aicm_path.empty()) {
          std::string le;
          st.loadDocMesh(ru.aicm_path, le);
        }
        auto rr = st.docMirror.redo(st.worker);
        std::printf("redo: ok=%d rev=%d features=%d\n", (int)rr.ok, rr.revision,
                    (int)(rr.tree.is_object() && rr.tree.contains("features") &&
                                  rr.tree["features"].is_array()
                              ? rr.tree["features"].size()
                              : 0));
        std::fflush(stdout);
        if (rr.ok && !rr.aicm_path.empty()) {
          std::string le;
          st.loadDocMesh(rr.aicm_path, le);
        }
        st.wantFocusViewport = 6;
      } else {
        std::printf("tree-shot: doc_new failed: %s\n", err.c_str());
        std::fflush(stdout);
      }
    }

    // H2: daemon_h2 shot — seed a part, kill hostd, capture the sticky
    // unreachable chip, respawn hostd, re-bootstrap + open a part so tabs
    // repopulate, capture. Wall-clock phases (not frame counts): hostd
    // kill/spawn takes real seconds.
    if (st.wantDaemonH2 && st.workerStarted) {
      const double now = ImGui::GetTime();
      auto killHostd = []() {
#ifndef _WIN32
        // Instance-scoped hard kill. --stop drain leaves client sockets open
        // until the process fully exits (and may refuse while busy), so the
        // GUI never sees HUP. Kill the daemon_pid from hello for a real
        // mid-session reset — still scoped to THIS instance (never pkill -x).
        long pid = 0;
        if (auto *c = engine::client()) {
          pid = c->serverInfo().value("daemon_pid", 0L);
        }
        if (pid > 0) {
          std::printf("[daemon_h2] SIGKILL hostd pid=%ld\n", pid);
          std::fflush(stdout);
          ::kill((pid_t)pid, SIGKILL);
        } else {
          // Fallback: instance-keyed --stop --force.
          std::string bin = chudware_ipc::resolveHostdBin("");
          if (bin.empty())
            bin = "core/build-occt-linux/bin/chudware-hostd";
          std::string cmd = "\"" + bin + "\" --stop --force 2>/dev/null";
          int rc = std::system(cmd.c_str());
          (void)rc;
        }
        // Drop the instance sock so reconnect doesn't race a half-dead peer.
        std::string bin = chudware_ipc::resolveHostdBin("");
        if (bin.empty())
          bin = "core/build-occt-linux/bin/chudware-hostd";
        const std::string sock =
            chudware_ipc::socketPath(chudware_ipc::instanceKey(bin));
        if (!sock.empty())
          ::unlink(sock.c_str());
#endif
      };
      auto spawnHostd = []() -> bool {
        std::string err;
        // Prefer the same resolve path EngineClient uses so we hit the right
        // binary.
        std::string bin = chudware_ipc::resolveHostdBin("");
        if (bin.empty()) {
          // Dev-tree fallback next to the known layout.
          bin = "core/build-occt-linux/bin/chudware-hostd";
        }
        return chudware_ipc::spawnDetached(bin, "", err);
      };
      if (st.daemonH2Phase == 0) {
        // Seed a live part so the pre-kill session has a real doc tab.
        std::string err;
        if (st.docMirror.newPart(st.worker, err)) {
          nlohmann::json boxOps = nlohmann::json::array();
          boxOps.push_back(
              {{"op", "add_feature"},
               {"feature",
                {{"type", "box"},
                 {"name", "Box1"},
                 {"params",
                  {{"length", {{"value", 40.0}, {"expr", nullptr}}},
                   {"width", {{"value", 30.0}, {"expr", nullptr}}},
                   {"height", {{"value", 20.0}, {"expr", nullptr}}}}}}}});
          auto r1 = st.docMirror.applyOps(st.worker, boxOps);
          if (r1.ok && !r1.aicm_path.empty()) {
            std::string le;
            st.loadDocMesh(r1.aicm_path, le);
          }
          std::printf("[daemon_h2] seeded doc=%s tabs=%zu\n",
                      st.docMirror.docId().c_str(), st.docTabs.size());
        } else {
          std::printf("[daemon_h2] seed newPart failed: %s\n", err.c_str());
        }
        std::fflush(stdout);
        killHostd();
        st.daemonH2Phase = 1;
        st.daemonH2PhaseT = now;
        std::printf(
            "[daemon_h2] killed hostd; waiting for unreachable notice\n");
        std::fflush(stdout);
      } else if (st.daemonH2Phase == 1) {
        // Wait until the sticky flag latches, or time out after ~8s and
        // force-capture.
        if (st.daemonUnreachable || (now - st.daemonH2PhaseT) > 8.0) {
          std::printf("[daemon_h2] unreachable=%d (%.1fs) — capturing notice\n",
                      (int)st.daemonUnreachable, now - st.daemonH2PhaseT);
          std::fflush(stdout);
          st.daemonH2Phase = 2;
          st.daemonH2PhaseT = now;
        }
      } else if (st.daemonH2Phase == 2) {
        // Hold the unreachable chip on screen, then mark a mid-shot capture
        // (taken after ImGui::Render where dw/dh exist) and respawn hostd.
        if ((now - st.daemonH2PhaseT) > 1.2) {
          // Latch notice capture for the end of this frame (backbuffer is
          // fully drawn only after ImGui::Render below). -1 = write notice PNG.
          st.daemonH2CaptureFrame = -1;
          bool ok = spawnHostd();
          std::printf(
              "[daemon_h2] respawn hostd ok=%d (notice capture pending)\n",
              (int)ok);
          std::fflush(stdout);
          st.daemonH2Phase = 3;
          st.daemonH2PhaseT = now;
        }
      } else if (st.daemonH2Phase == 3) {
        // Wait for reconnect + bootstrap, then open a fresh part so the tab
        // strip is visibly populated in the post-reconnect capture.
        if (!st.daemonUnreachable && (now - st.daemonH2PhaseT) > 1.5) {
          std::string err;
          if (st.docMirror.newPart(st.worker, err)) {
            std::printf("[daemon_h2] post-reconnect doc=%s tabs=%zu\n",
                        st.docMirror.docId().c_str(), st.docTabs.size());
          } else {
            std::printf("[daemon_h2] post-reconnect newPart failed: %s\n",
                        err.c_str());
          }
          std::fflush(stdout);
          st.daemonH2Phase = 4;
          st.daemonH2PhaseT = now;
          if (st.daemonH2CaptureFrame <= 0)
            st.daemonH2CaptureFrame = st.frame + 8;
        } else if ((now - st.daemonH2PhaseT) > 20.0) {
          std::printf("[daemon_h2] reconnect timeout unreachable=%d\n",
                      (int)st.daemonUnreachable);
          std::fflush(stdout);
          st.daemonH2Phase = 4;
          st.daemonH2PhaseT = now;
          if (st.daemonH2CaptureFrame <= 0)
            st.daemonH2CaptureFrame = st.frame + 8;
        }
      }
    }

    // Equations shot: create a part, seed two globals via eq_set (width=20,
    // height=width*1.5), then open the Equations editor so the dialog renders
    // with rows (mirrors the wantTree flow). Runs once the worker is ready; the
    // deferred screenshot (frame >= 10) gives the dialog a few frames to
    // repaint with the seeded rows.
    if (st.wantEquations && !st.equationsShotDone && st.workerStarted) {
      st.equationsShotDone = true;
      std::string err;
      if (st.docMirror.newPart(st.worker, err)) {
        auto r1 = st.docMirror.applyOps(
            st.worker,
            nlohmann::json::array(
                {{{"op", "eq_set"}, {"name", "width"}, {"expr", "20"}}}));
        std::printf("eq_set width: ok=%d rev=%d err=%s\n", (int)r1.ok,
                    r1.revision, r1.error_message.c_str());
        auto r2 = st.docMirror.applyOps(
            st.worker, nlohmann::json::array({{{"op", "eq_set"},
                                               {"name", "height"},
                                               {"expr", "width*1.5"}}}));
        std::printf("eq_set height: ok=%d rev=%d err=%s\n", (int)r2.ok,
                    r2.revision, r2.error_message.c_str());
        std::fflush(stdout);
        if (r2.ok && !r2.aicm_path.empty()) {
          std::string le;
          st.loadDocMesh(r2.aicm_path, le);
        }
        st.showEquations = true;
      } else {
        std::printf("equations-shot: doc_new failed: %s\n", err.c_str());
        std::fflush(stdout);
      }
    }

    auto seedEcadShotFixture = [&](nlohmann::json &out, std::string &path) -> bool {
      path = (std::filesystem::temp_directory_path() /
          ("chudware-ecad-shot-" + std::to_string(std::chrono::steady_clock::now().time_since_epoch().count()) +
           ".kicad_pcb")).string();
      std::ofstream fixture(path, std::ios::binary);
      fixture << R"KICAD((kicad_pcb (version 20221018) (generator "chudware")
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (net 0 "") (net 1 "GND") (net 2 "SIG"))
)KICAD";
      fixture.close();
      if (!fixture) return false;
      std::string err;
      const bool ok = st.worker.call("ecad_open", {{"path", path}}, out, err, 60000);
      std::printf("[ecad-shot] fixture_open ok=%d%s %s\n", (int)ok,
                  (out.is_object() && out.contains("code")) ? " (code-err)" : "", err.c_str());
      std::fflush(stdout);
      return ok && !(out.is_object() && out.contains("code"));
    };

    // ECAD GUI shot — seed a deterministic routed PCB through the native worker
    // RPCs. Only the explicit AI variants use a provider.
    if (st.wantEcad && !st.ecadShotDone && st.workerStarted &&
        (!engine::daemonMode() || st.daemonEverConnected)) {
      st.ecadShotDone = true;
      auto ecall = [&](const char *m, const nlohmann::json &p,
                       nlohmann::json &out) -> bool {
        std::string e;
        bool ok = st.worker.call(m, p, out, e, 60000);
        const bool hasCode = out.is_object() && out.contains("code");
        const std::string detail = hasCode ? out.dump() : e;
        std::printf("[ecad-shot] %s ok=%d%s %s\n", m, (int)ok,
                    hasCode ? " (code-err)" : "", detail.c_str());
        std::fflush(stdout);
        return ok && !(out.is_object() && out.contains("code"));
      };
      const char *kindEnv = std::getenv("CHUDWARE_ECAD_KIND");
      const bool wantProject = kindEnv && std::strcmp(kindEnv, "project") == 0;
      const bool wantChecks = kindEnv && std::strcmp(kindEnv, "checks") == 0;
      const bool wantMissing = kindEnv && std::strcmp(kindEnv, "missing") == 0;
      const bool wantPcb =
          !kindEnv || std::strcmp(kindEnv, "pcb") == 0 || wantProject || wantChecks ||
          std::strcmp(kindEnv, "hidefront") == 0 || std::strcmp(kindEnv, "netselect") == 0;
      const bool want3d = kindEnv && std::strcmp(kindEnv, "3d") == 0;
      const bool wantAi = kindEnv && std::strcmp(kindEnv, "ai") == 0;
      const bool wantHideFront = kindEnv && std::strcmp(kindEnv, "hidefront") == 0;
      const bool wantNetSelect = kindEnv && std::strcmp(kindEnv, "netselect") == 0;
      const bool wantSim = kindEnv && std::strcmp(kindEnv, "sim") == 0;
      const bool wantAiBoard = kindEnv && std::strcmp(kindEnv, "aiboard") == 0;
      const bool wantAiBoard3d = kindEnv && std::strcmp(kindEnv, "aiboard3d") == 0;
      const bool wantViolZoom = kindEnv && std::strcmp(kindEnv, "violzoom") == 0;
      nlohmann::json r;
      std::string showId;
      if (wantAiBoard || wantAiBoard3d) {
        // Wave U capstone: drive the full one-click flow (design -> populate ->
        // outline -> autoroute). ecadAiBoard adopts the finished board into the
        // editor mirror itself, so there is no showId to tree-load below; the
        // screenshot captures the routed PCB.
        ecadAiBoard(st, "5V resistive divider with 2.5V output");
        std::printf(
            "[ecad-shot] aiboard: doc=%s footprints=%d tracks(adopted)\n",
            st.ecadDocId.c_str(),
            (int)ui::ecadEditorMirror().footprints().size());
        if (wantAiBoard3d) {
          // Wave X: continue the seamless chain into the CAD 3D viewport --
          // export the AI-designed board to STEP and import it as a solid part
          // the user can build a housing around. ecadTo3d surfaces the Viewport
          // (wantFocusViewport). ecadAiBoard just requested ECAD focus; cancel
          // it so only the Viewport wins this frame (in real use these are
          // separate clicks, so the focus never contends).
          ecadTo3d(st);
          st.wantFocusEcad = 0;
          std::printf("[ecad-shot] aiboard3d: exported board -> CAD 3D part\n");
        }
        std::fflush(stdout);
      } else if (wantAi || wantMissing) {
        st.wantFocusEcad = 6;
      } else if (wantViolZoom) {
        // Seed real unconnected schematic pins for KiCad ERC navigation.
        if (ecall("ecad_new", {{"kind", "sch"}}, r)) {
          const std::string id = jstr(r, "doc_id");
          const int rev = jint(r, "revision");
          nlohmann::json ops = nlohmann::json::array();
          ops.push_back({{"op", "sch_add_symbol"},
                         {"libid", "Simulation_SPICE:VDC"},
                         {"pos", {10, 42}},
                         {"value", "5"}});
          ops.push_back({{"op", "sch_add_symbol"},
                         {"libid", "Device:R"},
                         {"pos", {32, 42}},
                         {"value", "10k"}});
          ops.push_back({{"op", "sch_add_symbol"},
                         {"libid", "Device:R"},
                         {"pos", {32, 22}},
                         {"value", "10k"}});
          ops.push_back({{"op", "sch_add_symbol"},
                         {"libid", "Device:C"},
                         {"pos", {54, 22}},
                         {"value", "100n"}});
          ops.push_back({{"op", "sch_add_symbol"},
                         {"libid", "power:GND"},
                         {"pos", {32, 6}}});
          ops.push_back(
              {{"op", "sch_add_wire"}, {"pts", {{10, 32}, {32, 32}}}});
          ops.push_back({{"op", "sch_add_wire"},
                         {"pts", {{32, 32}, {54, 32}, {54, 26}}}});
          ecall("ecad_apply_ops",
                {{"doc_id", id}, {"base_revision", rev}, {"ops", ops}}, r);
          showId = id;
        }
      } else if (wantPcb || want3d) {
        std::string fixturePath;
        if (seedEcadShotFixture(r, fixturePath)) {
          const std::string pcbId = jstr(r, "doc_id");
          const int rev = jint(r, "revision");
          nlohmann::json ops = nlohmann::json::array();
          ops.push_back(
              {{"op", "pcb_set_outline"},
               {"closed", true},
               {"shapes",
                nlohmann::json::array(
                    {{{"type", "line"}, {"p1", {0, 0}}, {"p2", {48, 0}}},
                     {{"type", "line"}, {"p1", {48, 0}}, {"p2", {48, 32}}},
                     {{"type", "line"}, {"p1", {48, 32}}, {"p2", {0, 32}}},
                     {{"type", "line"}, {"p1", {0, 32}}, {"p2", {0, 0}}}})}});
          const auto twoPadDef = [](const char *fpid) {
            return nlohmann::json{
                {"fpid", fpid},
                {"pads",
                 nlohmann::json::array(
                     {{{"id", "p1"},
                       {"number", "1"},
                       {"pos", {-1.0, 0.0}},
                       {"size", {1.4, 1.4}},
                       {"shape", "roundrect"},
                       {"roundrect_ratio", 0.25},
                       {"type", "smd"},
                       {"layers", {"F.Cu", "F.Mask", "F.Paste"}}},
                      {{"id", "p2"},
                       {"number", "2"},
                       {"pos", {1.0, 0.0}},
                       {"size", {1.4, 1.4}},
                       {"shape", "roundrect"},
                       {"roundrect_ratio", 0.25},
                       {"type", "smd"},
                       {"layers", {"F.Cu", "F.Mask", "F.Paste"}}}})}};
          };
          ops.push_back({{"op", "pcb_add_footprint"},
                         {"fpid", "Resistor_SMD:R_0603"},
                         {"def", twoPadDef("Resistor_SMD:R_0603")},
                         {"pads",
                          nlohmann::json::array(
                              {{{"id", "p1"},
                                {"number", "1"},
                                {"pos", {-1.0, 0.0}},
                                {"size", {1.4, 1.4}},
                                {"shape", "roundrect"},
                                {"roundrect_ratio", 0.25},
                                {"type", "smd"},
                                {"layers", {"F.Cu", "F.Mask", "F.Paste"}},
                                {"net", "GND"}},
                               {{"id", "p2"},
                                {"number", "2"},
                                {"pos", {1.0, 0.0}},
                                {"size", {1.4, 1.4}},
                                {"shape", "roundrect"},
                                {"roundrect_ratio", 0.25},
                                {"type", "smd"},
                                {"layers", {"F.Cu", "F.Mask", "F.Paste"}},
                                {"net", "SIG"}}})},
                         {"pos", {12, 20}},
                         {"ref", "R1"},
                         {"value", "10k"}});
          ops.push_back({{"op", "pcb_add_footprint"},
                         {"fpid", "Capacitor_SMD:C_0603"},
                         {"def", twoPadDef("Capacitor_SMD:C_0603")},
                         {"pads",
                          nlohmann::json::array(
                              {{{"id", "p1"},
                                {"number", "1"},
                                {"pos", {-1.0, 0.0}},
                                {"size", {1.4, 1.4}},
                                {"shape", "roundrect"},
                                {"roundrect_ratio", 0.25},
                                {"type", "smd"},
                                {"layers", {"F.Cu", "F.Mask", "F.Paste"}},
                                {"net", "GND"}},
                               {{"id", "p2"},
                                {"number", "2"},
                                {"pos", {1.0, 0.0}},
                                {"size", {1.4, 1.4}},
                                {"shape", "roundrect"},
                                {"roundrect_ratio", 0.25},
                                {"type", "smd"},
                                {"layers", {"F.Cu", "F.Mask", "F.Paste"}},
                                {"net", "SIG"}}})},
                         {"pos", {36, 20}},
                         {"ref", "C1"},
                         {"value", "100n"}});
          ops.push_back({{"op", "pcb_add_footprint"},
                         {"fpid", "Resistor_SMD:R_0603"},
                         {"def", twoPadDef("Resistor_SMD:R_0603")},
                         {"pads",
                          nlohmann::json::array(
                              {{{"id", "p1"},
                                {"number", "1"},
                                {"pos", {-1.0, 0.0}},
                                {"size", {1.4, 1.4}},
                                {"shape", "roundrect"},
                                {"roundrect_ratio", 0.25},
                                {"type", "smd"},
                                {"layers", {"F.Cu", "F.Mask", "F.Paste"}},
                                {"net", "SIG"}},
                               {{"id", "p2"},
                                {"number", "2"},
                                {"pos", {1.0, 0.0}},
                                {"size", {1.4, 1.4}},
                                {"shape", "roundrect"},
                                {"roundrect_ratio", 0.25},
                                {"type", "smd"},
                                {"layers", {"F.Cu", "F.Mask", "F.Paste"}},
                                {"net", "SIG"}}})},
                         {"pos", {24, 10}},
                         {"value", "1k"}});
          ops.push_back({{"op", "pcb_add_track"},
                         {"net", "GND"},
                         {"layer", "F.Cu"},
                         {"width", 0.4},
                         {"p1", {11, 20}},
                         {"p2", {35, 20}}});
          ops.push_back({{"op", "pcb_add_track"},
                         {"net", "SIG"},
                         {"layer", "F.Cu"},
                         {"width", 0.1},
                         {"p1", {13, 20.5}},
                         {"p2", {23, 10}}});
          ops.push_back({{"op", "pcb_add_via"},
                         {"net", "SIG"},
                         {"at", {23, 10}},
                         {"diameter", 0.8},
                         {"drill", 0.4},
                         {"layers", {"F.Cu", "B.Cu"}}});
          ops.push_back({{"op", "pcb_add_track"},
                         {"net", "SIG"},
                         {"layer", "B.Cu"},
                         {"width", 0.4},
                         {"p1", {23, 10}},
                         {"p2", {37, 20}}});
          if (ecall("ecad_apply_ops",
                    {{"doc_id", pcbId}, {"base_revision", rev}, {"ops", ops}},
                    r)) {
            const auto dir = std::filesystem::temp_directory_path() /
                ("chudware-kicad-shot-" +
                 std::to_string(std::chrono::steady_clock::now().time_since_epoch().count()));
            std::error_code ec;
            std::filesystem::create_directories(dir, ec);
            const auto board = dir / "Fixture.kicad_pcb";
            nlohmann::json saved;
            if (!ec && ecall("ecad_save", {{"doc_id", pcbId}, {"path", board.u8string()}}, saved)) {
              if (wantProject || wantChecks) {
                nlohmann::json project;
                if (ecall("ecad_open_project", {{"path", (dir / "Fixture.kicad_pro").u8string()}}, project) &&
                    ecadAddProject(st, project))
                  showId = st.ecadDocId;
              } else {
                showId = pcbId;
              }
            }
          }
        }
        std::error_code fixtureError;
        std::filesystem::remove(fixturePath, fixtureError);
        if (showId.empty()) {
          std::fprintf(stderr, "[ecad-shot] PCB fixture creation or project opening failed\n");
          shotExitCode = 1;
          break;
        }
      } else if (wantSim) {
        // Build a reproducible RC low-pass from real KiCad symbols and wires,
        // then persist it just as a generated project before running AC.
        const bool created = ecall("ecad_new", {{"kind", "sch"}, {"name", "RC Low-pass"}}, r);
        const std::string id = created ? jstr(r, "doc_id") : std::string();
        const int rev = created ? jint(r, "revision") : 0;
        const nlohmann::json ops = nlohmann::json::array({
          {{"op", "sch_add_symbol"}, {"libid", "Simulation_SPICE:VDC"}, {"ref", "V1"},
           {"value", "0"}, {"pos", {0, 0}}},
          {{"op", "sch_add_symbol"}, {"libid", "Device:R"}, {"ref", "R1"},
           {"value", "1k"}, {"pos", {25.4, 5.08}}, {"angle", 90}},
          {{"op", "sch_add_symbol"}, {"libid", "Device:C"}, {"ref", "C1"},
           {"value", "159n"}, {"pos", {50.8, 0}}},
          {{"op", "sch_add_wire"}, {"pts", {{0, 5.08}, {21.59, 5.08}}}},
          {{"op", "sch_add_wire"}, {"pts", {{29.21, 5.08}, {50.8, 5.08}, {50.8, 3.81}}}},
          {{"op", "sch_add_wire"}, {"pts", {{50.8, -3.81}, {50.8, -10.16},
                                              {0, -10.16}, {0, -5.08}}}},
          {{"op", "sch_add_power"}, {"net", "GND"}, {"pos", {0, -5.08}}},
          {{"op", "sch_add_label"}, {"text", "OUT"}, {"pos", {50.8, 5.08}}}
        });
        if (!id.empty() &&
            ecall("ecad_apply_ops", {{"doc_id", id}, {"base_revision", rev}, {"ops", ops}}, r)) {
          r["doc_id"] = id;
          if (ecadAddResponseTab(st, r, "RC Low-pass") &&
              ecadPersistGenerated(st, id, "RC_Lowpass"))
            showId = id;
        }
        if (showId.empty()) {
          std::fprintf(stderr, "[ecad-shot] RC schematic creation or save failed\n");
          shotExitCode = 1;
          break;
        }
      } else if (!wantHideFront && !wantNetSelect) {
        // Provider-backed path retained only for explicit non-default variants.
        if (ecadAiDesign(st, "5V resistive divider with 2.5V output"))
          showId = st.ecadDocId;
        else {
          std::fprintf(stderr, "[ecad-shot] AI schematic creation or save failed\n");
          shotExitCode = 1;
          break;
        }
      }
      const bool loaded = !showId.empty() && ecall("ecad_tree", {{"doc_id", showId}}, r);
      if (!showId.empty() && !loaded) {
        std::fprintf(stderr, "[ecad-shot] failed to read fixture tree\n");
        shotExitCode = 1;
        break;
      }
      if (loaded) {
        r["doc_id"] = showId;
        if (st.activeTab < 0 || st.activeTab >= (int)st.docTabs.size() ||
            st.docTabs[st.activeTab].ecadDocId != showId) {
          ecadAddResponseTab(st, r);
        } else {
          ecadAdopt(st, r);
        }
        if (wantHideFront)
          ui::ecadEditorMirror().setLayerVisible("F.Cu", false);
        if (wantNetSelect)
          ui::ecadEditorHighlightNet("SIG");
        std::printf(
            "[ecad-shot] showing %s rev=%d footprints=%d tracks=%d vias=%d "
            "nets=%d drc=%d front_visible=%d sig_visible=%d\n",
            showId.c_str(), st.ecadRevision,
            (int)ui::ecadEditorMirror().footprints().size(),
            (int)ui::ecadEditorMirror().tracks().size(),
            (int)ui::ecadEditorMirror().vias().size(),
            (int)ui::ecadEditorMirror().nets().size(),
            (int)ui::ecadEditorMirror().drc().size(),
            (int)ui::ecadEditorMirror().layerVisible("F.Cu"),
            (int)ui::ecadEditorMirror().netVisible("SIG"));
        if (wantChecks || wantViolZoom) {
          nlohmann::json check;
          if (ecall("ecad_kicad_check", {{"doc_id", showId}}, check)) {
            check["_source_revision"] = st.ecadRevision;
            ui::ecadSetCheckResult(showId, check);
            ui::ecadEditorShowChecks();
          }
          else {
            std::fprintf(stderr, "[ecad-shot] KiCad check failed\n");
            shotExitCode = 1;
            break;
          }
        }
        if (wantViolZoom) {
          // Zoom-to-violation: pan/zoom the canvas to the first located ERC
          // violation and flash the marker — exactly what clicking a violation
          // row does.
          const bool ok = ui::ecadEditorFocusFirstViolation();
          st.wantFocusEcad = 6;
          std::printf(
              "[ecad-shot] violzoom: focused first located violation = %d\n",
              (int)ok);
        }
        if (want3d) {
          ecadTo3d(st);
          st.wantFocusEcad = 0;
        }
        if (wantSim) {
          st.ecadSimAnalysis = 1;
          // Queue the simulation off the render thread. The ECAD shot capture
          // gate below waits explicitly for ecadSimRunState().running to clear,
          // so the plotted curve is fully settled before the PNG is written.
          ecadSimulate(st);
          std::printf("[ecad-shot] sim queued\n");
        }

        std::fflush(stdout);
      }
    }

#ifndef __EMSCRIPTEN__
    // ---------- scripted scenarios
    // (CHUDWARE_SHOT=m1/sketch/asm/fea/evalmass/activity) ---------- Frame-driven
    // state machines that issue scripted ops through the REAL worker, drive the
    // panels, and request per-step screenshots; extracted to ui::ScenarioRunner
    // (ui/scenarios.cpp).
    const bool pointerShot =
        st.shot && shotMode &&
        (std::strcmp(shotMode, "siminteractive") == 0 ||
         std::strcmp(shotMode, "uxquality") == 0 ||
         std::strcmp(shotMode, "agentlock") == 0 ||
         std::strcmp(shotMode, "scad_edit") == 0 ||
         std::strcmp(shotMode, "parts_catalog") == 0 ||
         std::strcmp(shotMode, "simaudit") == 0 ||
         std::strcmp(shotMode, "pipedraw") == 0 ||
         std::strcmp(shotMode, "hydraulics3d") == 0 ||
         std::strcmp(shotMode, "chat_inline") == 0 ||
         std::strcmp(shotMode, "text_contrast") == 0 ||
         std::strcmp(shotMode, "composer_spell") == 0 ||
         std::strcmp(shotMode, "copilot_ui") == 0 ||
         std::strcmp(shotMode, "copilot_showcase") == 0 ||
         std::strcmp(shotMode, "settings_routes") == 0 ||
         std::strcmp(shotMode, "model_settings") == 0 ||
         std::strcmp(shotMode, "dwgopen") == 0 ||
         std::strcmp(shotMode, "frameperf") == 0);
    if (!pointerShot) {
      core::framestats::Scope timing("scenario");
      scenarios.tick(st);
    }
#endif

    // Keep theme state coherent across runtime toggles, screenshot scenarios,
    // and any accidental style leak. This runs before NewFrame so docked panes
    // cannot inherit a stale light surface while the saved/active theme says
    // dark.
    ensureFrameTheme();

    // Global "something is running" signal for the status bar, viewport panel
    // and Settings modal — deliberately ANY run, not just the viewed one.
    bool busy = st.anyRunActive();

    core::framestats::Scope newFrameTiming("imgui_new_frame");
    ImGui_ImplOpenGL3_NewFrame();
    ImGui_ImplGlfw_NewFrame();
#ifdef __EMSCRIPTEN__
    wasmDrainTextInput();
#endif
    newFrameTiming.stop();
#ifndef __EMSCRIPTEN__
    // Headless-capture hygiene, same reasoning as the toast suppression above: the
    // window opens under wherever the real cursor happens to sit, so a --ui-screenshot
    // frame can catch a ribbon/button tooltip parked over the very chrome the shot is
    // meant to show (the Manufacture captures kept eating the "Simulate" tooltip).
    // Pointer-driven simulation shots enqueue their input after the platform
    // backend; other captures keep the cursor outside the window.
    if (st.shot && !pointerShot)
      ImGui::GetIO().AddMousePosEvent(-FLT_MAX, -FLT_MAX);
    if (pointerShot) {
      core::framestats::Scope timing("scenario");
      scenarios.tick(st);
      ensureFrameTheme();
      busy = st.anyRunActive();
    }
#endif
    {
      core::framestats::Scope timing("imgui_new_frame");
      ImGui::NewFrame();
    }

    // Liquid glass v2.1: per-frame Settings -> resolved-glass push. Idempotent
    // (exact-match early-out in setGlassConfig => free), so any code path that
    // mutates st.settings.glassCfg (the Appearance editor, MCP, tests) is
    // live-applied without extra plumbing.
    ui::setGlassConfig(st.settings.glassCfg);

    // Viewport highlight colors track the live palette (theme switches
    // included) — setColors just stores three float[4]s, so the per-frame push
    // is free.
    if (st.overlays) {
      float featHl[4];
      ui::Palette::withAlpha(ui::palette.selectionHighlight, featHl, 0.30f);
      st.overlays->setColors(ui::palette.hoverHighlight,
                             ui::palette.selectionHighlight, featHl);
    }

    // ---------- W1-B (item 4): global Undo / Redo keys ----------
    // Ctrl+Z = undo, Ctrl+Y / Ctrl+Shift+Z = redo. Routed through the SAME
    // AppState helper the FeatureManager toolbar buttons use (mesh refresh +
    // owner map + confirmation toast). Guards:
    //   - skip while a text field has focus (the field owns Ctrl+Z for its own
    //   edit history),
    //   - skip while a sketch is active (sketch mode owns in-sketch undo in a
    //   later wave),
    //   - skip the read-only ECAD hub so it cannot undo a parked mechanical document,
    //   - skip Drawing / Schematic / PCB tabs and any moment the live DocMirror
    //   is not the
    //     active tab's doc (tab switch parks the previous part behind the
    //     sheet; async rebind can lag activeTab). docUndo/docRedo also refuse
    //     those cases as a second gate.
    {
      core::framestats::Scope timing("input");
      const bool canDoc = st.workerStarted;
      const bool textFocus = io.WantTextInput;
      const bool inSketchNow = st.sketchMode->active() ||
                               (st.sketch3dMode && st.sketch3dMode->active());
      bool ecadFocused = false;
      if (ImGuiContext *g = ImGui::GetCurrentContext()) {
        for (ImGuiWindow *w = g->NavWindow; w; w = w->ParentWindow)
          if (std::strcmp(w->Name, "ECAD") == 0) {
            ecadFocused = true;
            break;
          }
      }
      const bool activeCad =
          !st.activeTabIsDrawing() && !st.activeTabIsEcad() &&
          st.activeTab >= 0 && st.activeTab < (int)st.docTabs.size() &&
          st.docTabs[st.activeTab].docId == st.docMirror.docId() &&
          st.docMirror.hasDoc();
      // The open Hydraulics panel (Simulate) owns Ctrl+Z/Ctrl+Y for its circuit history
      // (HydraulicPanel::undo/redo, handled by its canvas and 3D scene); never also undo the
      // CAD document underneath it.
      const bool hydraulicsOwnsUndo =
          st.activeWorkspace == AppState::Workspace::Simulate && st.simManager &&
          st.simManager->hydraulic().isOpen();
      if (canDoc && !textFocus && !st.showWeb && !inSketchNow && !ecadFocused &&
          !hydraulicsOwnsUndo &&
          !(st.propertyManager && st.propertyManager->active()) &&
          activeCad && (io.KeyCtrl || io.KeySuper)) {
        if (io.KeyShift && ImGui::IsKeyPressed(ImGuiKey_Z, false)) {
          st.docRedo();
        } else if (ImGui::IsKeyPressed(ImGuiKey_Z, false)) {
          st.docUndo();
        } else if (ImGui::IsKeyPressed(ImGuiKey_Y, false)) {
          st.docRedo();
        }
      }
      // While the in-app browser overlay owns the screen it is the "text
      // field": its page/forms swallow typing, but a CEF OSR view does not set
      // io.WantTextInput, so guard the app's Ctrl-shortcuts on !st.showWeb too
      // — otherwise Ctrl+N/P/O/S get hijacked out from under the browser (where
      // they mean new-tab/print/open/save).
      const bool appShortcutsOk = !textFocus && !st.showWeb;

      // Ctrl+N creates a new part directly. The old "+" chooser lived in the
      // removed document strip; full document-type creation is still available
      // from File > New.
      if (appShortcutsOk && (io.KeyCtrl || io.KeySuper) && !io.KeyShift &&
          ImGui::IsKeyPressed(ImGuiKey_N, false))
        ui::createNewDoc(st, 0);

      // Ctrl+P: open the unified command + settings search.
      if (appShortcutsOk && (io.KeyCtrl || io.KeySuper) && !io.KeyShift &&
          ImGui::IsKeyPressed(ImGuiKey_P, false))
        st.showCommandSearch = true;

      // [workspace-tabs seam] Ctrl+1..Ctrl+8 switch the title-bar workspace
      // (matches tab order). The shortcut bar advertises these. Guards: text
      // input owns digits while typing. Deliberately NOT gated on ecadFocused:
      // the ECAD window is focused for the whole Electronics stay, and its
      // accelerator space has no Ctrl+digit bindings — gating here made the
      // workspace keys dead exactly where users need an exit.
      if (!textFocus && (io.KeyCtrl || io.KeySuper) && !io.KeyShift &&
          !io.KeyAlt) {
        using WS = AppState::Workspace;
        static constexpr struct {
          ImGuiKey key;
          WS ws;
        } kWsKeys[] = {
            {ImGuiKey_1, WS::Model},        {ImGuiKey_2, WS::Drawing},
            {ImGuiKey_3, WS::Simulate},     {ImGuiKey_4, WS::Electronics},
            {ImGuiKey_5, WS::Manufacture},  {ImGuiKey_6, WS::PLM},
            {ImGuiKey_7, WS::PartsLibrary}, {ImGuiKey_8, WS::AI},
            {ImGuiKey_9, WS::ReverseEng},
        };
        // Ctrl+digit stays live while the Web overlay is up so it doubles as an
        // exit (switch workspace AND drop the browser), mirroring a
        // workspace-tab click.
        for (const auto &k : kWsKeys)
          if (ImGui::IsKeyPressed(k.key, false)) {
            st.activeWorkspace = k.ws;
            st.showWeb = false;
            break;
          }
      }

      // Esc closes the Web overlay (back to modeling), unless a text field owns
      // the key.
      if (st.showWeb && !textFocus &&
          ImGui::IsKeyPressed(ImGuiKey_Escape, false))
        st.showWeb = false;

      // ---- Wave 3: Ctrl+O / Ctrl+S / Ctrl+Shift+S file shortcuts ----
      // Set request flags consumed by the GL-thread file pump below (after the
      // input block) so the native dialog opens with the doc swap off the ImGui
      // input path. Text fields retain their own shortcuts; the hub uses the shared save path.
      if (appShortcutsOk && (io.KeyCtrl || io.KeySuper)) {
        if (!io.KeyShift && ImGui::IsKeyPressed(ImGuiKey_O, false))
          st.requestOpenDialog = true;
        else if (io.KeyShift && ImGui::IsKeyPressed(ImGuiKey_S, false))
          st.requestSaveAs = true;
        else if (!io.KeyShift && ImGui::IsKeyPressed(ImGuiKey_S, false))
          st.requestSave = true;
      }

      // ---- Wave 4C/4D + Wave 3: global Ctrl+C / Ctrl+X / Ctrl+V copy/paste
      // routing ---- Priority: Explorer tab active -> filesystem file
      // copy/paste (Wave 3). Otherwise, when a sketch is active
      // SketchMode::drawOverlay owns Ctrl+C/V (it has the (u,v) context), so
      // the global router stands down; with no sketch active it routes to the
      // FeatureManager selection (Wave 4C/4D). Guards mirror the undo block:
      // skip while a text field or the ECAD window owns input. Ctrl+Z/Y are
      // handled above; we never touch them.
      if (appShortcutsOk && !ecadFocused && (io.KeyCtrl || io.KeySuper) &&
          !io.KeyShift) {
        if (ui::fileExplorerActive(st)) {
          // Explorer-tab FILE copy/paste — no worker needed, so not gated on
          // canDoc.
          if (ImGui::IsKeyPressed(ImGuiKey_C, false))
            ui::fileExplorerCopySelection(st);
          else if (ImGui::IsKeyPressed(ImGuiKey_V, false))
            ui::fileExplorerPaste(st);
          // Ctrl+X on files intentionally unsupported (no filesystem "cut"
          // semantics).
        } else if (canDoc && !inSketchNow) {
          // FeatureManager / left-pane selection copy/cut/paste.
          if (ImGui::IsKeyPressed(ImGuiKey_C, false))
            ui::featureCopySelection(st);
          else if (ImGui::IsKeyPressed(ImGuiKey_X, false))
            ui::featureCutSelection(st);
          else if (ImGui::IsKeyPressed(ImGuiKey_V, false))
            ui::featurePasteSelection(st);
        }
        // When inSketchNow (and not Explorer): no-op — SketchMode::drawOverlay
        // handles Ctrl+C/V.
      }
    }

    // ---- Wave 3: file request pump (open/save/recent) ----
    // Drains the one-shot flags the File menu (title_bar.cpp) and the Ctrl+O/S
    // shortcuts set. Runs once per frame on the GL thread, OUTSIDE the ImGui
    // input/menu walk, so the native dialog + DocMirror swap never re-enters a
    // draw. Open paths are dispatched by extension: CAD -> featureTreeOpenPath,
    // ECAD/KiCad -> ecadOpenPath. Each open/save records Recent. Document
    // operations use the engine's normal per-document serialization.
    if (st.requestOpenDialog || !st.requestOpenPath.empty() || st.requestSave ||
        st.requestSaveAs || st.requestSaveAll ||
        !st.requestExportFormat.empty()) {
      core::framestats::Scope timing("file_requests");
      const bool fileReady = st.workerStarted;
      if (!fileReady) {
        // Drop save/export/recent requests (need a doc); keep open requests
        // pending until ready.
        if (st.requestSave || st.requestSaveAs || st.requestSaveAll ||
            !st.requestExportFormat.empty())
          st.toast(ui::ToastLevel::Info, "Busy — try again in a moment");
        st.requestSave = st.requestSaveAs = st.requestSaveAll = false;
        st.requestExportFormat.clear();
      } else {
        if (st.requestOpenDialog) {
          st.requestOpenDialog = false;
          if (st.activeWorkspace == AppState::Workspace::Electronics) {
            ecadOpenDoc(st);
          } else {
            std::string opened = ui::featureTreeOpenDialog(st);
            if (!opened.empty()) st.pushRecentFile(opened);
          }
        }
        if (!st.requestOpenPath.empty()) {
          std::string path = st.requestOpenPath;
          st.requestOpenPath.clear();
          std::string ext = std::filesystem::u8path(path).extension().u8string();
          std::transform(ext.begin(), ext.end(), ext.begin(),
                         [](unsigned char c) { return (char)std::tolower(c); });
          const bool isDrawing = (ext == ".draw+");
          const bool isEcad = (ext == ".ecad+" || ext == ".ecad" ||
                               ext == ".kicad_sch" || ext == ".kicad_pcb" ||
                               ext == ".kicad_pro");
          if (isDrawing) {
            if (ui::featureTreeOpenDrawingPath(st, path))
              st.pushRecentFile(path);
          } else if (isEcad) {
            ecadOpenPath(st, path); // records Recent itself
          } else if (ui::featureTreeOpenPath(st, path)) {
            // ITEM 13: queue Zoom-to-Fit for THIS doc's first real display,
            // even if the viewport isn't drawn this frame (e.g. Parts Library
            // workspace focused).
            if (st.docMirror.hasDoc()) {
              const std::string id = st.docMirror.docId();
              if (!id.empty())
                st.fitPendingDocIds.insert(id);
            }
            // Keep legacy flag for any path that still keys on it; viewport
            // migrates it.
            if (!st.handoffQuietOpen)
              st.fitViewOnNextMeshCommit = true;
          }
        }
        // Save by tab identity; neither individual nor batch saves change focus.
        if (st.requestSave || st.requestSaveAs) {
          const bool asSave = st.requestSaveAs;
          st.requestSave = st.requestSaveAs = false;
          ui::saveDocTab(st, st.activeTab, asSave);
        }
        if (st.requestSaveAll) {
          st.requestSaveAll = false;
          saveAllTabs(st, false, [&st](bool ok, int count) {
            st.toast(ok ? ui::ToastLevel::Info : ui::ToastLevel::Warn,
                     "Save All: " + std::to_string(count) + " saved" +
                         (ok ? "" : "; stopped before all documents were saved"));
          });
        }
        // Wave-5-fill: Export <fmt> — native Save-As + DocMirror::exportFile
        // for the active doc.
        if (!st.requestExportFormat.empty()) {
          std::string fmt = st.requestExportFormat;
          st.requestExportFormat.clear();
          exportFile3d(st, fmt);
        }
      } // fileReady
    }

    // Re-push palette-derived viewport visuals every frame so a runtime theme
    // toggle (from the title bar) takes effect immediately. Cheap uniform/state
    // sets; no GPU allocation.
    {
      float top3[3] = {ui::palette.viewportTop[0], ui::palette.viewportTop[1],
                       ui::palette.viewportTop[2]};
      float mid3[3] = {ui::palette.viewportMid[0], ui::palette.viewportMid[1],
                       ui::palette.viewportMid[2]};
      float bot3[3] = {ui::palette.viewportBottom[0],
                       ui::palette.viewportBottom[1],
                       ui::palette.viewportBottom[2]};
      renderer.setBackground3(top3, mid3, bot3);
      renderer.setEdgeStyle(ui::palette.viewportEdge[0],
                            ui::palette.viewportEdge[1],
                            ui::palette.viewportEdge[2], 1.6f * ui::uiScale);
      renderer.setDefaultPartColor(ui::palette.partDefault[0],
                                   ui::palette.partDefault[1],
                                   ui::palette.partDefault[2]);
      // W4-K: gate on showGroundShadow so the HUD hide/show toggle actually
      // kills the shadow. (Previously this unconditional full-alpha push every
      // frame clobbered viewport_panel's gated value before renderOrbit ran, so
      // the toggle had no visible effect.)
      float gsA = st.showGroundShadow ? ui::palette.groundShadow[3] : 0.0f;
      renderer.setGroundShadow(ui::palette.groundShadow[0],
                               ui::palette.groundShadow[1],
                               ui::palette.groundShadow[2], gsA);
      renderer.setRealView(
          st.showRealView); // C30-C: RealView environment reflections
      renderer.setPlaneColors(ui::palette.planeFill, ui::palette.planeOutline);
    }

    if (shot && st.frame == 1) {
      if (st.wantSettings)
        st.openSettings = true;
      if (st.wantChat || st.wantAssembly || st.wantPlan || st.wantCopilotSlash ||
          shotChatContext) {
        st.designStarted = true; // show the conversation takeover
        // Re-apply the seed the restore clobbered (see shotChatSeed).
        if (!shotChatSeed.empty() && st.chat.size() != shotChatSeed.size())
          st.chat = shotChatSeed;
      }
      // Seed AFTER the startup session load (frame 0 clears the composer while
      // restoring the active conversation / prompt draft).
      if (st.wantCopilotSlash)
        std::snprintf(st.prompt.data(), st.prompt.size(), "/");
      if (shotChatContext) st.pendingContextRefs = shotPendingChips;
    }
    // chat shot: replay a live multi-agent run into the seeded running bubble through the SAME
    // two sinks the event drain feeds (feedAgentDagEvent -> agent trace / overview roster,
    // feedTurnActivity -> transcript rows + plan), with the event kinds and payload keys the
    // worker sends (orch_<kind> with the prefix stripped). Runs once the restore has settled on
    // a conversation id. The run is marked live with a sentinel run id no worker event can bind
    // to (runForEvent requires an exact run-id match).
    if (shot && st.wantChat && st.frame >= 1) {
      static bool s_chatRunSeeded = false;
      const std::string convId = st.activeConvId();
      if (!s_chatRunSeeded && !convId.empty() && !st.chat.empty() &&
          !st.chat.back().fromUser && st.chat.back().running) {
        s_chatRunSeeded = true;
        ChatMsg &live = st.chat.back();
        auto emit = [&](const char *kind, nlohmann::json data) {
          ui::feedAgentDagEvent(convId, kind, data);
          feedTurnActivity(live, kind, data);
        };
        const std::string model = st.settings.model[0] ? st.settings.model : "default";
        // Replay real mapped tool-result evidence supplied by a runtime smoke. Keep the
        // production feed/trace sinks and render collapsed tools exactly as a live turn.
        if (std::strcmp(shotMode, "chat_evidence") == 0) {
          // A tall capture keeps both real images and the hub visible without clipping
          // earlier evidence out of this smoke's auto-scrolled transcript.
          glfwSetWindowSize(win, 1340, 1600);
          const char* fixture = std::getenv("CHUDWARE_CHAT_EVIDENCE");
          std::ifstream input(fixture ? fixture : "");
          nlohmann::json events;
          try {
            input >> events;
            if (!events.is_array() || events.empty())
              throw std::runtime_error("expected a nonempty mapped-event array");
            for (const auto& event : events) {
              const std::string kind = event.at("kind").get<std::string>();
              emit(kind.c_str(), event.at("data"));
            }
            const nlohmann::json savedTrace = ui::agentDagToJson(convId);
            ui::agentDagFromJson(convId, savedTrace);
            std::vector<ui::AgentTraceAgent> agents;
            ui::agentTraceSubagents(st, convId, agents);
            for (const auto& event : events) {
              const auto& data = event.at("data");
              const std::string expected = data.value("name", std::string());
              const std::string id = data.value("subagent_id", std::string());
              if (expected.empty() || id.empty()) continue;
              const auto found = std::find_if(agents.begin(), agents.end(),
                  [&](const ui::AgentTraceAgent& agent) { return agent.id == id; });
              if (found == agents.end() || found->name != expected)
                throw std::runtime_error("restored task name differs for " + id);
            }
            for (const auto& agent : agents)
              std::printf("chat_evidence: restored agent %s name=%s parent=%s status=%s\n",
                          agent.id.c_str(), agent.name.c_str(), agent.parent.c_str(),
                          agent.status.c_str());
            if (std::getenv("CHUDWARE_CHAT_EVIDENCE_EXPANDED"))
              for (auto& row : live.activity) {
                if (row.rowKind == 3) {
                  row.expanded = true;
                  row.userToggled = true;
                }
              }
            std::printf("chat_evidence: replayed %zu mapped events\n", events.size());
          } catch (const std::exception& error) {
            std::fprintf(stderr, "chat_evidence: %s\n", error.what());
            std::fflush(nullptr);
            core::framestats::flushReport();
            std::_Exit(EXIT_FAILURE);
          }
        } else {
        emit("run_started", {{"run_id", "shot-chat-run"}, {"model", model}});
        emit("plan_state",
             {{"shape", "checklist"},
              {"title", "Sensor board mounting"},
              {"items",
               {{{"id", "bosses:1"}, {"title", "Read the board's hole pattern"},
                 {"status", "done"}},
                {{"id", "bosses:2"}, {"title", "Sketch the boss layout on the floor"},
                 {"status", "done"}},
                {{"id", "bosses:3"}, {"title", "Extrude four M3 bosses with pilot holes"},
                 {"status", "running"}},
                {{"id", "bosses:4"}, {"title", "Check clearance against the enclosure lid"},
                 {"status", "pending"}},
                {{"id", "bosses:5"}, {"title", "Update the drawing and BOM"},
                 {"status", "pending"}}}}});
        emit("agent.tool_call", {{"id", "main-1"}, {"tool_name", "inspect_part"},
                                 {"target", "enclosure base"}, {"ok", true},
                                 {"latency_ms", 640},
                                 {"args_preview", "{\"detail\": \"holes\"}"}});
        emit("agent.spawn", {{"subagent_id", "datasheet"}, {"id", "spawn-datasheet"},
                             {"role", "Datasheet reader"},
                             {"brief", "Pull the sensor board's hole pattern"},
                             {"spawn_kind", "task"}});
        emit("agent.spawn", {{"subagent_id", "geometry"}, {"id", "spawn-geometry"},
                             {"role", "Geometry"},
                             {"brief", "Model the four bosses and pilot holes"},
                             {"spawn_kind", "task"}});
        emit("agent.spawn", {{"subagent_id", "clearance"}, {"id", "spawn-clearance"},
                             {"role", "Clearance check"},
                             {"brief", "Check boss height against the lid ribs"},
                             {"spawn_kind", "task"}});
        for (const char *sub : {"datasheet", "geometry", "clearance"})
          emit("run_started", {{"subagent_id", sub}, {"run_id", std::string("shot-") + sub},
                               {"model", model}});
        // Datasheet reader: one read, a message to Geometry, done.
        emit("agent.tool_call", {{"subagent_id", "datasheet"}, {"id", "ds-1"},
                                 {"tool_name", "read_document"},
                                 {"target", "SHT45 sensor board datasheet"}, {"ok", true},
                                 {"latency_ms", 1820}});
        emit("agent.message",
             {{"from", "datasheet"}, {"to", "geometry"}, {"channel", "irc"},
              {"message_id", "shot-msg-1"},
              {"text", "Hole pattern is 21.0 x 15.5 mm with 2.7 mm holes. M3 bosses with "
                       "2.5 mm pilots line up."}});
        emit("sub_run", {{"subagent_id", "datasheet"}, {"id", "datasheet"},
                         {"purpose", "Datasheet reader"}, {"status", "done"},
                         {"detail", "Hole pattern 21.0 x 15.5 mm"}});
        // Clearance check: the lid is not open, so it fails.
        emit("agent.tool_call",
             {{"subagent_id", "clearance"}, {"id", "cl-1"}, {"tool_name", "measure_clearance"},
              {"target", "enclosure lid ribs"}, {"ok", false}, {"latency_ms", 410},
              {"error_preview", "The enclosure lid is not open, so there is nothing to "
                                "measure against."}});
        emit("sub_run", {{"subagent_id", "clearance"}, {"id", "clearance"},
                         {"purpose", "Clearance check"}, {"status", "failed"},
                         {"detail", "The enclosure lid is not open in this session."}});
        // Geometry: two finished calls and one in flight (its last step on the roster row).
        emit("agent.tool_call", {{"subagent_id", "geometry"}, {"id", "geo-1"},
                                 {"tool_name", "sketch_points"}, {"target", "base floor"},
                                 {"ok", true}, {"latency_ms", 930}});
        emit("agent.tool_call", {{"subagent_id", "geometry"}, {"id", "geo-2"},
                                 {"tool_name", "extrude"}, {"target", "boss sketch"},
                                 {"ok", true}, {"latency_ms", 2100}});
        emit("agent.message",
             {{"from", "geometry"}, {"channel", "board"}, {"message_id", "shot-msg-2"},
              {"text", "Bosses are placed on the datasheet pattern; pilot holes next. The "
                       "clearance check needs the lid open."}});
        emit("status", {{"subagent_id", "geometry"}, {"phase", "tool"},
                        {"tool_call_id", "geo-3"}, {"tool_name", "add_hole"}});
        }
        std::shared_ptr<JobState> job = st.runFor(convId);
        {
          std::lock_guard<std::mutex> lk(job->mtx);
          job->convId = convId;
          job->runId = "shot-chat-run";
        }
        job->running = true;
      }
    }
    // chat_context: bind the seeded chips to the shot's open document as soon as its tab has a
    // hostd doc id, creating a fresh part for the shot when none is open, so they render live.
    if (shotChatContext && st.frame >= 1 && chatContextBoundAt < 0) {
      const bool haveDoc = st.activeTab >= 0 && st.activeTab < (int)st.docTabs.size() &&
                           !st.docTabs[(size_t)st.activeTab].docId.empty();
      if (!haveDoc && !chatContextCreateTried && st.frame >= 3 && st.workerStarted) {
        chatContextCreateTried = true;
        std::string err;
        const bool ok = st.docMirror.newPart(st.worker, err);
        if (ok) {
          st.clearViewportForNewDoc();
          st.addDocTab(/*isAssembly=*/false);
        } else {
          std::fprintf(stderr, "[chat_context] new part failed: %s\n", err.c_str());
        }
      }
      if (haveDoc) {
        chatContextBoundAt = st.frame;
        const auto &tab = st.docTabs[(size_t)st.activeTab];
        auto stamp = [&tab](core::ContextRef &r) {
          r.docId = tab.docId;
          r.docName = tab.title;
          r.docPath = tab.path;
        };
        for (auto &r : st.pendingContextRefs) stamp(r);
        for (auto &m : st.chat)
          for (auto &r : m.contextRefs) stamp(r);
        std::fprintf(stderr, "[chat_context] chips bound to %s (%s) at frame %d\n",
                     tab.docId.c_str(), tab.title.c_str(), st.frame);
      }
    }
    // copilot_slash: with the menu open (composer holds "/"), inject Enter on
    // frame 5 (release on 6) so the highlighted suggestion is accepted — the
    // final capture verifies the fill ("/compact ") landed in the composer.
    if (shot && st.wantCopilotSlash) {
      if (st.frame == 5) io.AddKeyEvent(ImGuiKey_Enter, true);
      if (st.frame == 6) io.AddKeyEvent(ImGuiKey_Enter, false);
      if (st.frame == 7)
        std::fprintf(stderr, "[copilot_slash] composer after fill: '%s'\n",
                     st.prompt.data());
    }
    // Screenshot verification captures after a few frames; keep the settings
    // popup request alive long enough that ImGui opens it even if the first
    // frame is consumed by layout init.
    if (shot && st.wantSettings && st.frame <= 5)
      st.openSettings = true;

    // ---------- custom title bar (our own; the OS title bar is stripped)
    // ----------
    ImGuiViewport *vp = ImGui::GetMainViewport();
    const bool maximized = chrome::maximized(win);

    float barH = 0.0f;
    {
      core::framestats::Scope timing("title_bar");
      ui::drawTitleBar(st, maximized, barH);
    }

    // Window title mirrors the active document + dirty marker
    // (SolidWorks-style). Cheap change-detection so glfwSetWindowTitle only
    // fires when the string actually changes.
    {
      std::string title;
      if (st.activeTab >= 0 && st.activeTab < (int)st.docTabs.size()) {
        const auto &tab = st.docTabs[st.activeTab];
        title = tab.title.empty() ? "Untitled" : tab.title;
        if (tab.dirty)
          title += "*";
        title += " \xe2\x80\x94 Chudware"; // " — Chudware"
      } else {
        title = "Chudware";
      }
      static std::string lastTitle;
      if (title != lastTitle) {
        lastTitle = title;
        glfwSetWindowTitle(win, title.c_str());
      }
    }

    // ---------- C14: ribbon bar (drawn between title bar and dockspace)
    // ---------- syncIconTheme keeps texture selection in sync with the active
    // theme.
    core::framestats::Scope ribbonTiming("ribbon");
    ui::syncIconTheme();
    // C34: SolidWorks behavior — entering a sketch flips the ribbon to the
    // Sketch tab, exiting returns to Features. Edge-triggered so the user can
    // still switch tabs freely while sketching.
    {
      static bool s_wasInSketch = false;
      static bool s_wasInSketch3d = false;
      const bool nowInSketch = st.sketchMode->active();
      const bool nowInSketch3d = st.sketch3dMode && st.sketch3dMode->active();
      if ((nowInSketch && !s_wasInSketch) ||
          (nowInSketch3d && !s_wasInSketch3d))
        st.ribbonTab = AppState::RT_Sketch;
      else if (!nowInSketch && s_wasInSketch && !nowInSketch3d)
        st.ribbonTab = AppState::RT_Features;
      else if (!nowInSketch3d && s_wasInSketch3d && !nowInSketch)
        st.ribbonTab = AppState::RT_Features;
      s_wasInSketch = nowInSketch;
      s_wasInSketch3d = nowInSketch3d;
    }
    // C40: accent-highlight the armed sketch tool's ribbon button. When the
    // profiler is running (and no sketch tool is armed) highlight the Profiler
    // button instead, so its ON state is visible at a glance.
    const char *activeCmd = st.sketchMode->activeToolCmd();
    // Armed-but-awaiting-pick: "New Sketch" was clicked and we're waiting on a
    // plane/face pick (no sketch tool active yet, so activeToolCmd() above
    // returns ""). Highlight the New Sketch button itself so the armed state is
    // visible, matching every other armed-tool convention.
    if ((!activeCmd || !*activeCmd) && st.sketchPlanePickActive)
      activeCmd = "sketch_new";
    if (st.profilerRunning && (!activeCmd || !*activeCmd))
      activeCmd = "prof_toggle";
    ui::setRibbonActiveCmd(activeCmd);
    // Sketch tab swaps Exit Sketch <-> New Sketch depending on whether a sketch
    // is being edited.
    ui::setRibbonSketchActive(st.sketchMode->active());
    // 3D sketch: swaps the Sketch tab's command source to the 3D tool set (see
    // ribbon.cpp).
    ui::setRibbonSketch3DActive(st.sketch3dMode && st.sketch3dMode->active());
    // [workspace-tabs seam] Drive the per-workspace ribbon filter from the
    // active title-bar workspace so Model shows modeling tools, Simulate shows
    // sim tools, etc.
    ui::setRibbonWorkspace(static_cast<int>(st.activeWorkspace));
    ui::setRibbonCommandContext(ui::commandContext(st));
    // The Drawing workspace always shows the drawing tabs (View Layout / Annotation), with or
    // without a drawing open, so it never lands on an unrelated modeling tab.
    const bool drawingRibbon = st.activeTabIsDrawing() ||
                               st.activeWorkspace == AppState::Workspace::Drawing;
    if (drawingRibbon && st.ribbonTab != AppState::RT_ViewLayout &&
        st.ribbonTab != AppState::RT_Annotation)
      st.ribbonTab = AppState::RT_ViewLayout;
    float ribbonH =
        ui::drawRibbon(st.ribbonTab, st.showRibbon, st.showSimulationTools,
                       st.showPcbTools, barH, drawingRibbon);

    // Electronics: surface the PCB DRC/unrouted summary in the ribbon band's
    // trailing dead space (the board canvas gave up its bottom results strip to
    // fill the full height — see ecad_panel.cpp). No-op for schematic docs /
    // when no board is open.
    if (st.activeWorkspace == AppState::Workspace::Electronics &&
        ribbonH > 0.0f)
      ui::drawEcadRibbonStatus(st, vp->Pos.y + barH, ribbonH);

    // ---------- Manufacture Offline/Live mode strip ----------
    // Safety-critical mode indication is CHROME, not an overlay: it draws in its
    // own full-width strip directly under the ribbon and its height is folded
    // into dockTopY below, so the dockspace, the doc-tab strip and the pinned
    // columns all start beneath it. It used to float over the 3D viewport (and
    // collided with the doc tabs, which anchor to the same band top).
    // manufactureModeBarHeight() returns 0 outside the Manufacture workspace.
    const float mfgBarH = ui::manufactureModeBarHeight(st);
    if (mfgBarH > 0.0f)
      ui::drawManufactureModeBar(st, vp->Pos.x, vp->Pos.y + barH + ribbonH,
                                 vp->Size.x, mfgBarH);

    ribbonTiming.stop();

    // ---------- default starter document ----------
    // Open straight into a fresh part on launch (non-shot) so the app is
    // immediately usable.
    ensureStarterDoc(st, shot);

    // ---------- active document reconciliation ----------
    // The browser/SolidWorks-style document tabs are rendered by the central
    // Viewport/ECAD window that owns the view area, so the strip stays
    // contained in that view window.
    st.ensureActiveTab();

    // ---------- dockspace host (between the title bar+ribbon and the status
    // bar) ----------
    core::framestats::Scope layoutTiming("layout");
    const float statusH = ImGui::GetTextLineHeight() + 8.0f * scale;
    const float dockTopY = barH + ribbonH + mfgBarH;
#ifdef __EMSCRIPTEN__
    const float mobileHudH = g_wasmMobile ? 56.0f : 0.0f;
#else
    const float mobileHudH = 0.0f;
#endif
    // Publish the full viewport overlay band BEFORE any panel draws —
    // drawLeftPane (layout.cpp) and the Copilot column read it via
    // ui::overlayBand(). Manufacture no longer owns a separate bottom CAM dock,
    // so every viewport workspace uses the same uninterrupted band.
    const float bandBottom = vp->Pos.y + vp->Size.y - statusH - mobileHudH;
    ui::setOverlayBand(vp->Pos.x, vp->Pos.y + dockTopY, vp->Pos.x + vp->Size.x,
                       bandBottom);
    ImGui::SetNextWindowPos(ImVec2(vp->Pos.x, vp->Pos.y + dockTopY));
    ImGui::SetNextWindowSize(
        ImVec2(vp->Size.x, std::max(1.0f, vp->Size.y - dockTopY - statusH - mobileHudH)));
    ImGui::SetNextWindowViewport(vp->ID);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowRounding, 0);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowBorderSize, 0);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(0, 0));
    ImGuiWindowFlags hostFlags =
        ImGuiWindowFlags_NoTitleBar | ImGuiWindowFlags_NoCollapse |
        ImGuiWindowFlags_NoResize | ImGuiWindowFlags_NoMove |
        ImGuiWindowFlags_NoBringToFrontOnFocus | ImGuiWindowFlags_NoNavFocus |
        ImGuiWindowFlags_NoDocking;
    ImGui::Begin("##DockHost", nullptr,
                 hostFlags | ImGuiWindowFlags_NoBackground);
    ImGui::PopStyleVar(3);
    // Versioned dockspace ID pairs with the versioned ini. Changing this
    // intentionally forces one clean DockBuilder layout after major window/dock
    // identity changes.
    ImGuiID dockId = ImGui::GetID(
        "CHUDWAREDockV6"); // V6: overlay layout — splitless central node
    if ((!st.layoutInit && ImGui::DockBuilderGetNode(dockId) == nullptr) ||
        st.resetLayout) {
      buildDefaultLayout(dockId,
                         ImVec2(vp->Size.x, std::max(1.0f, vp->Size.y - dockTopY - statusH - mobileHudH)));
      st.layoutInit = true;
      st.resetLayout = false;
      // "Reset layout" also restores the pinned glass columns' default
      // width/collapse state (one-shot flags consumed by drawLeftPane / the
      // Copilot column on their next draw).
      ui::resetOverlayColumns();
      // Electronics owns the central ECAD editor. Every other normal workspace,
      // including Manufacture, uses the native viewport and should focus it after
      // rebuilding the dock layout.
      if (!st.wantEcad)
        st.wantFocusViewport = 6;
    }
    // W2 full-bleed: with PassthruCentralNode AND a window docked in the
    // central node, ImGui paints an OPAQUE ImGuiCol_WindowBg rect over the
    // WHOLE dockspace (imgui.cpp DockNodeUpdate: the transparent "central node
    // hole" is punched only when the central node is EMPTY, but the Viewport
    // window is docked there). That backdrop sits above the global background
    // draw list and hides the full-bleed 3D image composited there. Push a
    // transparent WindowBg just for the DockSpace() call so the dockspace
    // backdrop is see-through and the 3D shows; every docked window still
    // paints its own background (the glass FeatureManager stays translucent) on
    // top.
    ImGui::PushStyleColor(ImGuiCol_WindowBg, ImVec4(0, 0, 0, 0));
    ImGui::PushStyleColor(ImGuiCol_DockingEmptyBg, ImVec4(0, 0, 0, 0));
    // Overlay layout: the default layout has NO dock splits, so ImGui draws no
    // splitter bars. Belt-and-braces for user-created splits (a window dragged
    // into a dockspace edge): make any splitter invisible around ONLY this
    // DockSpace call (DockingSeparatorSize keeps its default so the invisible
    // hit zone still resizes). ui_theme's Separator roles stay live for normal
    // ImGui::Separator() everywhere else.
    ImGui::PushStyleColor(ImGuiCol_Separator, ImVec4(0, 0, 0, 0));
    ImGui::PushStyleColor(ImGuiCol_SeparatorHovered, ImVec4(0, 0, 0, 0));
    ImGui::PushStyleColor(ImGuiCol_SeparatorActive, ImVec4(0, 0, 0, 0));
    ImGui::DockSpace(dockId, ImVec2(0, 0),
                     ImGuiDockNodeFlags_PassthruCentralNode);
    ImGui::PopStyleColor(5);
    ImGui::End();
    layoutTiming.stop();

    // ---------- status bar ----------
    core::framestats::Scope statusTiming("status_bar");
#ifdef __EMSCRIPTEN__
    ImGuiViewport statusViewport;
    statusViewport.ID = vp->ID;
    statusViewport.Pos = vp->Pos;
    statusViewport.Size = ImVec2(vp->Size.x, vp->Size.y - mobileHudH);
    ui::drawStatusBar(st, &statusViewport, statusH, busy, hostFlags);
#else
    ui::drawStatusBar(st, vp, statusH, busy, hostFlags);
#endif
    statusTiming.stop();

    // ---------- panels (pinned glass columns + floating dialogs over the
    // full-width 3D) ---- The SolidWorks-faithful tabbed LEFT column (Explorer
    // / FeatureManager / PropertyManager / Configurations). drawLeftPane routes
    // the active tab body and auto- switches to the PropertyManager tab while a
    // command form is active. We still tick the PM so the debounced live
    // preview fires; the container draws the PM form body when active. A live
    // PropertyManager forces the left pane open so its form is visible, but the
    // preview tick is independent of that pane state to avoid invisible active
    // commands. Multi-document: the ACTIVE document's PropertyManager
    // (st.propertyManager is re-seated on every tab switch). A backgrounded
    // doc's PM is NOT ticked here, so its debounced preview pauses while parked
    // and resumes when its tab is reactivated.

    // [workspace-tabs seam] Derive per-frame display flags from
    // st.activeWorkspace. These are LOCAL to this frame — they never write
    // persistent state (no st.leftPaneTab mutation, no clamping that traps the
    // user). Full-body workspaces (PLM, PartsLibrary, AI) suppress the left
    // pane and viewport in favour of their own full-screen layout; the docked
    // Copilot column is always drawn so it stays available in non-AI workspaces
    // exactly as today.
    // Every CHUDWARE_SHOT scenario re-asserts its own workspace here: startup doc restore
    // runs activateTab, which pulls any workspace that cannot host a 3D part back to Model
    // (core/doc_tabs.cpp), so a full-body scenario captured the modelling tab and "passed"
    // while verifying nothing. This was hand-patched for parts, then reveng, then PLM, and
    // still bit ECAD, the AI workspace and Drawing — three scenarios exiting 0 having
    // photographed the wrong surface. One pin covers the class (AppState::shotWorkspacePin).
    // The pin ADOPTS any non-Model workspace the run selects and only ever restores it when
    // something has yanked us back to Model. A blunt "force the setup workspace every frame"
    // pin broke the other direction: fea / flow / simdisc_* select their workspace DURING the
    // run (they set only the ribbon and left-pane tab up front), so pinning Model at setup
    // pinned them to Model forever - the Simulation Manager scenarios then captured the
    // Feature Manager. Model is never a pin target for exactly that reason.
    // The catalog scenario deliberately transitions from Parts Library to the placed part.
    if (shot && !st.wantWorkspace && !(shotMode && std::strcmp(shotMode, "parts_catalog") == 0)) {
      if (st.activeWorkspace != AppState::Workspace::Model)
        st.shotWorkspacePin = (int)st.activeWorkspace;
      else if (st.shotWorkspacePin >= 0)
        st.activeWorkspace = (AppState::Workspace)st.shotWorkspacePin;
    }

    using WS = AppState::Workspace;
    const WS ws = st.activeWorkspace;
    // RE-40: release the webcam fd + preview/thumbnail GL textures the moment
    // the user leaves the Reverse Engineering tab. drawRevEngWorkspace simply
    // stops being called on a workspace switch, so without this the /dev/video
    // handle stayed open and blocked every other app on the machine from using
    // the camera. Tracked on the transition edge so it fires exactly once, no
    // matter which of the many activeWorkspace assignment sites caused it.
    {
      static WS prevWs = ws;
      if (prevWs == WS::ReverseEng && ws != WS::ReverseEng)
        ui::revengWorkspaceTeardown();
      prevWs = ws;
    }
    // The "Web" chip's full-window in-app browser overlay (title_bar.cpp
    // toggles st.showWeb). It is NOT a workspace — activeWorkspace stays put
    // underneath — so it composes with any workspace and toggles straight back
    // to modeling. While up it owns the whole central band.
    const bool webActive = st.showWeb;
    // Workspaces (or the Web overlay) that own the ENTIRE central region — no
    // left pane, no viewport, no cam/ecad. Folding webActive in suppresses all
    // of those in one place; the full-body workspace renderers below are gated
    // on !webActive separately.
    const bool wsFullBody =
        webActive || (ws == WS::PLM || ws == WS::PartsLibrary || ws == WS::AI ||
                      ws == WS::ReverseEng || ws == WS::Firmware);
    // Effective left-pane tab override (derived this frame; never written to
    // st). Simulate forces the Simulation tab so the sim tree shows.
    // Non-Simulate workspaces must NOT show the sim tree: clamp LP_Simulation
    // back to FeatureManager so the sim tab never leaks into Model/Drawing/etc.
    const int effectiveLeftTab =
        (ws == WS::Simulate) ? ui::kLeftPaneSimulation
                             : (st.leftPaneTab == ui::kLeftPaneSimulation
                                    ? ui::kLeftPaneFeatureManager
                                    : st.leftPaneTab);
    // Manufacture is one integrated workspace: job/output controls live in the
    // pinned left column, ribbon commands mutate that job, and every geometric
    // preview is drawn in the native viewport. There is no separate CAM window.
    // Electronics keeps ECAD as its primary body. Outside that workspace,
    // discard a stray transient show request when no ECAD document owns it so
    // the 2D editor never bleeds over Manufacture/Model/etc.
    if (ws != WS::Electronics && st.showPcbTools && st.ecadDocId.empty())
      st.showPcbTools = false;
    // The ECAD editor canvas + PCB Manager belong to the Electronics workspace
    // ONLY — an open board no longer bleeds its 2D canvas over the 3D viewport
    // in Model/etc. The doc-tab strip drives the coupling the
    // other way: activating a PCB/Schematic tab switches the workspace to
    // Electronics (AppState::switchToTab), so a board is always reachable.
    const bool wsShowEcad = (ws == WS::Electronics);

    // ---- Electronics workspace-entry edge (hoisted ABOVE drawLeftPane) ----
    // Entering Electronics never creates a document: with no ECAD document
    // open, the editor shows its launchpad (New PCB / New Schematic / Open /
    // AI design) and the manager column its empty state, the same way the
    // other workspaces present an empty context. wantFocusEcad's actual
    // SetNextWindowFocus stays below, right before drawEcadEditor draws —
    // window-focus calls that fire before their window's Begin() are a no-op.
    //
    // ecadPrimeActiveApp points the ECAD state() resolver at this AppState
    // before any ECAD RPC response can be adopted this frame (e.g. an open
    // pumped before drawEcadEditor/drawEcadManagerBody have ever run), so the
    // response lands in the active tab's own state, not the process fallback.
    ui::ecadPrimeActiveApp(st);
    static WS ecadPrevWsForInit = WS::Model;
    if (ws != ecadPrevWsForInit) {
      core::framestats::Scope timing("workspace_transition");
      // Leaving a workspace: discard its untouched auto-created starter doc
      // (an untouched starter Part) so a blank initial document doesn't
      // linger. Never closes the last tab.
      st.discardWorkspaceStarter(ecadPrevWsForInit);
      // discardWorkspaceStarter -> closeDocTab -> neighbor switchToTab can flip
      // the workspace (a Part neighbor pulls to Model); re-assert the workspace
      // the user actually picked.
      st.activeWorkspace = ws;
      if (ws == WS::Electronics) {
        st.wantFocusEcad = 2; // pop the ECAD tab forward for a couple of frames
      } else if (ecadPrevWsForInit == WS::Electronics) {
        st.wantFocusEcad = 0;     // cancel any pending grab from the stay
        st.wantFocusViewport = 2; // bring the 3D view back to the central node
      }
      ecadPrevWsForInit = ws;
    }

    // ---- workspace crossfade -------------------------------------------------
    // Switching workspaces was a channel flip: the outgoing body simply stopped
    // being drawn and the incoming one appeared fully formed on the next frame.
    // Fading the central body in over kBase costs a fifth of a second and is
    // most of the difference between "an app" and "a program". Only the BODY
    // transitions — the title bar, ribbon and status bar stay put, so the chrome
    // reads as a fixed frame around content that changed.
    //
    // Alpha only, deliberately no vertical lift: every full-body block below
    // positions its own window from the overlay band, so a lift would have to be
    // threaded through five separate SetNextWindowPos calls for a fraction of the
    // effect.
    //
    // The push is popped unconditionally after the body section; there is no
    // early return anywhere between the two (verified), and a headless capture
    // snaps straight to 1 so a shot can never photograph a half-faded panel.
    float wsFadeAlpha = 1.0f;
    {
      static WS wsFadeFrom = ws;
      static double wsFadeT0 = -1.0;
      if (wsFadeFrom != ws) {
        wsFadeFrom = ws;
        wsFadeT0 = ImGui::GetTime();
      }
      if (!shot && wsFadeT0 > 0.0) {
        const float t =
            (float)((ImGui::GetTime() - wsFadeT0) / (double)ui::anim::kBase);
        if (t >= 1.0f) {
          wsFadeT0 = -1.0;
        } else {
          wsFadeAlpha = ui::anim::easeOut(t);
          ui::anim::requestFrames();
        }
      }
    }
    ImGui::PushStyleVar(ImGuiStyleVar_Alpha,
                        ImGui::GetStyle().Alpha * wsFadeAlpha);

    if (!wsFullBody) {
      // ---- normal viewport workspaces (Model, Drawing, Simulate, Electronics,
      // Manufacture) ---- Electronics used to suppress this column (the ECAD
      // panel drew its own embedded left sub-panel instead) — that's what left
      // the doc-tab strip anchored at the band's far-left edge instead of
      // matching the Model tab (drawLeftPane never ran, so it never published
      // this frame's overlayColumnEdge(0)). drawLeftPane now hosts the ECAD
      // "PCB/Schematic Manager" body itself (layout.cpp's Electronics branch,
      // mirroring the Simulate/ Manufacture single-tree pattern) so the SAME
      // pinned column serves every workspace.
      const bool showLeftPane = true;
      if (st.propertyManager->active()) {
        core::framestats::Scope timing("property_manager");
        st.propertyManager->tick(st);
        if (showLeftPane)
          st.showFeatureTree = true;
      }
      if (st.showFeatureTree && showLeftPane) {
        core::framestats::Scope timing(st.propertyManager->active()
                                          ? "property_manager" : "feature_tree");
        // Pass the effective tab as a temporary override via the AppState
        // field; restore after. drawLeftPane reads st.leftPaneTab directly, so
        // we temporarily set it per-frame.
        const int savedTab = st.leftPaneTab;
        if (effectiveLeftTab != savedTab)
          st.leftPaneTab = effectiveLeftTab;
        ui::drawLeftPane(st, [&st](const std::string &fid) {
          const auto& tree = st.docMirror.tree();
          if (tree.contains("features") && tree["features"].is_array()) {
            for (const auto& f : tree["features"]) {
              if (f.is_object() && f.value("id", std::string()) == fid &&
                  f.value("type", std::string()) == "scad") {
                ui::openScadEditor(st, st.docMirror.docId(), fid);
                return;
              }
            }
          }
          st.propertyManager->openForEdit(st, fid);
        });
        if (effectiveLeftTab != savedTab)
          st.leftPaneTab = savedTab;
      }
    }

    // W3-H: unified AI Copilot pane (input-at-top + conversation; embeds the
    // run tree inline while a v2 run is active). Drawn as the docked Copilot
    // column on the right edge in every workspace EXCEPT the AI workspace,
    // where the full-body AI hub (drawAiWorkspace, below) hosts its own
    // expanded chat on the same st.chat state — drawing both would double the
    // conversation panel.
    if (ws != WS::AI && !webActive) {
      core::framestats::Scope timing("copilot");
      // Copilot busy/enabled are PER CONVERSATION: a run in some other
      // conversation must never lock the composer of the one on screen.
      const bool chatBusy = st.convRunning(st.activeConvId());
      ui::drawAIPanel(st, chatBusy, st.workerStarted && !chatBusy);
    }
    // Autosave safety net: the debounced chat_history flush used to live ONLY inside
    // drawAIPanel / the AI workspace, so it stopped entirely whenever the Copilot column
    // wasn't drawn (View > hide Copilot, the Web overlay, or an ImGui-clipped window). A long
    // agent run in any of those states persisted nothing until the user reopened the panel —
    // a crash lost the whole run. Run it unconditionally here; it no-ops until the session is
    // bound (st.chatDocKey) and is debounced to ~1 Hz, so double-calling with the panel's own
    // hook is free.
    ui::maybePersistChatHistoryDebounced(st, /*force=*/false);
    if (!wsFullBody) {
      // ---- 3D/2D viewport (Model, Drawing, Simulate, Manufacture) ----
      // Electronics uses the ECAD editor as the full central body — skip the 3D
      // viewport so the ECAD dock window wins the central node without fighting
      // the Viewport tab. Manufacture (cam-workspace overhaul): the 3D viewport
      // IS the central body — the CAM job tree lives in the left column
      // (drawLeftPane -> drawCamTreeBody), the CAM actions live on the
      // Manufacture ribbon, and generated toolpaths draw over the part via the
      // camActiveBackplot viewport overlay ("Show in 3D").
      const bool skipViewport = (ws == WS::Electronics);
      if (!skipViewport) {
        // Focusing a dock window closes popups above it, including first-run and recovery modals.
        // Keep the request pending until the user finishes the popup.
        if (st.wantFocusViewport > 0 &&
            !ImGui::IsPopupOpen(nullptr, ImGuiPopupFlags_AnyPopupId | ImGuiPopupFlags_AnyPopupLevel)) {
          ImGui::SetNextWindowFocus();
          st.wantFocusViewport--;
        }
        {
          core::framestats::Scope timing("viewport");
          ui::drawViewportPanel(st, io, busy);
        }
      }
    }
    // Manufacture owns no floating operator surface any more: Devices / Run /
    // Attention live in the pinned left column (layout.cpp) and the Offline/Live
    // mode strip is chrome under the ribbon (above). This call only bootstraps
    // the mirror's list RPCs and hosts the approval dialog.
    if (!webActive && ws == WS::Manufacture) {
      core::framestats::Scope timing("manufacture");
      ui::drawManufacturePanel(st);
      ui::camDrawPostConfirm(st);
    }

    if (!wsFullBody) {
      // ---- ECAD editor (Electronics workspace or when an ECAD doc is open)
      // ---- The workspace-entry edge trigger (arming wantFocusEcad /
      // wantFocusViewport) runs ABOVE drawLeftPane — see the
      // hoisted block above. This must stay edge-triggered: SetNextWindowFocus
      // -> FocusWindow steals ImGui's ActiveId from widgets in every other
      // window, so a continuous per-frame grab would cancel any click outside
      // the ECAD window before its release could land — title-bar workspace
      // tabs included. That was the "stuck on the Electronics tab" input
      // lockup.
      if (st.wantFocusEcad > 0) {
        // Surfaced by an explicit ecad command from another workspace: the
        // editor now lives ONLY in the Electronics workspace (wsShowEcad
        // above), so switch there to reveal it instead of the old showPcbTools
        // overlay (which no longer un-hides the canvas).
        if (ws != WS::Electronics)
          st.activeWorkspace = WS::Electronics;
        ImGui::SetNextWindowFocus();
        st.wantFocusEcad--;
      }
      if (wsShowEcad) {
        core::framestats::Scope timing("ecad");
        ui::drawEcadEditor(st); // ECAD 2D editor
      }
    }


    // ---- full-body workspace renderers (PLM / PartsLibrary / AI) ----
    // These take the whole central region. They use drawViewportPanel's dock
    // slot (the ImGui window ID "Viewport" is NOT opened by them — they draw
    // directly into the overlay band area as floating non-docked glass panels,
    // same pattern as drawLeftPane).
    if (!webActive && (ws == WS::PLM || ws == WS::PartsLibrary)) {
      core::framestats::Scope timing(ws == WS::PLM ? "plm" : "parts_library");
      // PLM / Parts Library: full-body workspaces. The body functions emit
      // widgets directly (no Begin of their own), so host them in ONE
      // borderless window spanning the whole central band — otherwise ImGui
      // dumps them into its fallback "Debug" window (a small box top-left,
      // the reported symptom). The band already stops left of the docked
      // Copilot column, so the body fills the central region and Copilot stays
      // visible.
      const ui::OverlayBand &band = ui::overlayBand();
      if (band.valid) {
        const float m = 12.0f;
        // Stop at the docked Copilot column's LEFT edge (published each frame,
        // slot 1) so the 2D workspace body never renders under the Copilot.
        // Falls back to the band's right edge when the Copilot isn't up.
        float rightX = ui::overlayColumnEdge(1);
        if (rightX <= band.minX + m)
          rightX = band.maxX;
        // ImGuiCond_Always: override any stale collapsed/size state from
        // imgui.ini so the workspace always fills the full band on first open.
        ImGui::SetNextWindowPos(ImVec2(band.minX + m, band.minY + m),
                                ImGuiCond_Always);
        ImGui::SetNextWindowSize(ImVec2((rightX - band.minX) - 2.0f * m,
                                        (band.maxY - band.minY) - 2.0f * m),
                                 ImGuiCond_Always);
        ImGui::Begin(
            ws == WS::PLM ? "##PLMWorkspace" : "##PartsWorkspace", nullptr,
            ImGuiWindowFlags_NoTitleBar | ImGuiWindowFlags_NoResize |
                ImGuiWindowFlags_NoMove | ImGuiWindowFlags_NoCollapse |
                ImGuiWindowFlags_NoBringToFrontOnFocus |
                ImGuiWindowFlags_NoNav | ImGuiWindowFlags_NoSavedSettings);
        if (ws == WS::PLM)
          ui::drawPlmWorkspace(st);
        else
          ui::drawPartsLibraryWorkspace(st);
        ImGui::End();
      }
    } else if (!webActive && ws == WS::AI) {
      core::framestats::Scope timing("ai_workspace");
      // AI workspace: full-body AI hub spanning the whole central band —
      // [sessions + sub-agents rail] | [expanded Copilot chat / project DAG] |
      // [AI settings]. Unlike PLM/Parts this fills to band.maxX (drawAIPanel's
      // docked column is suppressed for WS::AI above, so there is no right
      // column to reserve space for). Same borderless full-region window
      // pattern as drawPlmWorkspace / drawPartsLibraryWorkspace.
      const ui::OverlayBand &band = ui::overlayBand();
      if (band.valid) {
        const float m = 12.0f;
        ImGui::SetNextWindowPos(ImVec2(band.minX + m, band.minY + m),
                                ImGuiCond_Always);
        ImGui::SetNextWindowSize(ImVec2((band.maxX - band.minX) - 2.0f * m,
                                        (band.maxY - band.minY) - 2.0f * m),
                                 ImGuiCond_Always);
        ImGui::Begin("##AIWorkspace", nullptr,
                     ImGuiWindowFlags_NoTitleBar | ImGuiWindowFlags_NoResize |
                         ImGuiWindowFlags_NoMove | ImGuiWindowFlags_NoCollapse |
                         ImGuiWindowFlags_NoBringToFrontOnFocus |
                         ImGuiWindowFlags_NoNav |
                         ImGuiWindowFlags_NoSavedSettings);
        ui::drawAiWorkspace(st);
        ImGui::End();
      }
    } else if (!webActive && ws == WS::ReverseEng) {
      core::framestats::Scope timing("reverse_eng");
      // RE-ANCHOR(reveng): full-body Reverse Engineering workspace
      // (RE-03/RE-19/RE-21).
      const ui::OverlayBand &band = ui::overlayBand();
      if (band.valid) {
        const float m = 12.0f;
        float rightX = ui::overlayColumnEdge(1);
        if (rightX <= band.minX + m)
          rightX = band.maxX;
        ImGui::SetNextWindowPos(ImVec2(band.minX + m, band.minY + m),
                                ImGuiCond_Always);
        ImGui::SetNextWindowSize(ImVec2((rightX - band.minX) - 2.0f * m,
                                        (band.maxY - band.minY) - 2.0f * m),
                                 ImGuiCond_Always);
        ImGui::Begin("##RevEngWorkspace", nullptr,
                     ImGuiWindowFlags_NoTitleBar | ImGuiWindowFlags_NoResize |
                         ImGuiWindowFlags_NoMove | ImGuiWindowFlags_NoCollapse |
                         ImGuiWindowFlags_NoBringToFrontOnFocus |
                         ImGuiWindowFlags_NoNav |
                         ImGuiWindowFlags_NoSavedSettings);
        ui::drawRevEngWorkspace(st);
        ImGui::End();
      }
    } else if (!webActive && ws == WS::Firmware) {
      core::framestats::Scope timing("firmware");
      // Full-body Firmware workspace (project rail |
      // build/emulation/trace | fw_status-gated backend rail). Same borderless
      // central-band host as PLM/RevEng.
      const ui::OverlayBand &band = ui::overlayBand();
      if (band.valid) {
        const float m = 12.0f;
        float rightX = ui::overlayColumnEdge(1);
        if (rightX <= band.minX + m)
          rightX = band.maxX;
        ImGui::SetNextWindowPos(ImVec2(band.minX + m, band.minY + m),
                                ImGuiCond_Always);
        ImGui::SetNextWindowSize(ImVec2((rightX - band.minX) - 2.0f * m,
                                        (band.maxY - band.minY) - 2.0f * m),
                                 ImGuiCond_Always);
        ImGui::Begin("##FirmwareWorkspace", nullptr,
                     ImGuiWindowFlags_NoTitleBar | ImGuiWindowFlags_NoResize |
                         ImGuiWindowFlags_NoMove | ImGuiWindowFlags_NoCollapse |
                         ImGuiWindowFlags_NoBringToFrontOnFocus |
                         ImGuiWindowFlags_NoNav |
                         ImGuiWindowFlags_NoSavedSettings);
        ui::drawFirmwareWorkspace(st);
        ImGui::End();
      }
    }
    // End of the central body: pop the crossfade alpha pushed above. The Web
    // overlay below is NOT part of the transition — it composes over whatever
    // workspace is underneath and must never inherit a fade.
    ImGui::PopStyleVar();

    // ---- "Web" chip overlay: full-window in-app browser over the central band
    // ---- Drawn LAST in the central-body section and only when active, so it's
    // the single central renderer for the frame (viewport / left pane /
    // full-body workspaces above are all suppressed via wsFullBody|webActive) —
    // one body owner = no focus/z fights.
    if (webActive) {
      core::framestats::Scope timing("web");
      ui::drawWebBrowserPanel(st);
    }

    // App-owned ECAD command dialogs remain reachable regardless of whether
    // the editor is empty or already showing a document.
    core::framestats::Scope dialogsTiming("dialogs");
    ecadDrawCommandDialogs(st);

    // Hub actions share the ribbon's availability and dispatch path.
    if (!st.ecadPanelCmd.empty()) {
      const std::string c = st.ecadPanelCmd;
      const std::string origin = st.ecadPanelDocId;
      const int analysis = st.ecadPanelSimAnalysis;
      st.ecadPanelCmd.clear();
      st.ecadPanelDocId.clear();
      if (c == "ecad_simulate" && !origin.empty()) {
        st.ecadSimRequestedDocId = origin;
        st.ecadSimRequestedAnalysis = analysis;
        st.ecadSimGo = true;
      } else if (!origin.empty() &&
                 (st.activeTab < 0 || st.activeTab >= (int)st.docTabs.size() ||
                  st.docTabs[st.activeTab].ecadDocId != origin)) {
        st.toast(ui::ToastLevel::Warn, "Electronics selection changed; choose the action again.");
      } else if (st.ribbonDispatch) {
        st.ribbonDispatch(c.c_str());
      }
    }
    if (st.ecadAiGo) {
      st.ecadAiGo = false;
      ecadAiDesign(st, st.ecadAiRequest);
    } // Wave P
    if (st.ecadAiBoardGo) {
      st.ecadAiBoardGo = false;
      ecadAiBoard(st, st.ecadAiRequest);
    } // Wave U
    if (st.ecadSimGo) {
      st.ecadSimGo = false;
      const std::string source = std::move(st.ecadSimRequestedDocId);
      st.ecadSimRequestedDocId.clear();
      ecadSimulate(st, source, st.ecadSimRequestedAnalysis);
    } // native simulator

    // ---------- C19: simulation (active doc) + evaluate dialogs ----------
    // The active document's SimManager drains its own SimClient job
    // (owner-gated) and loads result overlays. Its tree + setup forms render in
    // the left pane's Simulation tab (drawLeftPane).
    if (st.simManager) {
      core::framestats::Scope timing("simulation");
      st.simManager->tick(st);
    }
    st.evaluateDialogs->draw(
        st); // active doc's Measure/Mass/Section windows (per-doc; re-seated)

    // ---------- C20b: assembly panels (Insert / Mate / Explode / Interference
    // / BOM) ----------
    asmPanels.draw(st);

    // ---------- "Create New" document chooser (tiled previews + Open existing)
    // ---------- Hosted here at app scope (not inside the Viewport window) so
    // the doc-strip "+" / Ctrl+N raise it regardless of which central document
    // — part, drawing, or ECAD — is forward.
    ui::drawNewDocChooser(st);

    // ---------- agent-work confirmation on app exit ----------
    // Raised from the main-loop close guard (st.wantQuitAgentConfirm) when the
    // user tries to quit with a copilot run in flight. Runs execute daemon-side,
    // so "Continue in background" just lets the quit proceed — hostd keeps the
    // run (its tray icon shows the work) and exits on its own once the work
    // finishes. "Stop agent & quit" cancels each exact browser run handle (native:
    // empty run_id cancels all live runs). Either choice then falls through to
    // the dirty-doc prompt below if any user-owned docs are unsaved, or exits.
    {
      static bool s_quitAgentModalOpen = false;
      if (st.wantQuitAgentConfirm) {
        st.wantQuitAgentConfirm = false;
        s_quitAgentModalOpen = true;
      }
      if (s_quitAgentModalOpen)
        ImGui::OpenPopup("Agent work in progress##appquitagent");
      ImGui::SetNextWindowPos(
          ImVec2(vp->Pos.x + vp->Size.x * 0.5f, vp->Pos.y + vp->Size.y * 0.5f),
          ImGuiCond_Appearing, ImVec2(0.5f, 0.5f));
      bool openAgent = true;
      if (ImGui::BeginPopupModal("Agent work in progress##appquitagent",
                                 &openAgent,
                                 ImGuiWindowFlags_AlwaysAutoResize |
                                     ImGuiWindowFlags_NoSavedSettings)) {
        int nActive = 0;
        for (const auto &[convId, run] : st.runs)
          if (run && run->running.load())
            ++nActive;
        ImGui::Text("%d agent run%s still in progress.", nActive,
                    nActive == 1 ? " is" : "s are");
        ImGui::Spacing();
        ImGui::TextUnformatted(
            "Continue in background keeps the work running in the\n"
            "background daemon (tray icon); results land in the document.");
        ImGui::Spacing();
        // After the agent choice: dirty user docs still get the save prompt,
        // otherwise the quit proceeds.
        auto proceedQuit = [&st, win] {
          bool anyDirty = st.hasPendingHumanMutations();
          for (const auto &t : st.docTabs)
            if (t.dirty) {
              anyDirty = true;
              break;
            }
          if (anyDirty)
            st.wantQuitConfirm = true;
          else {
            st.quitConfirmed = true;
            glfwSetWindowShouldClose(win, GLFW_TRUE);
          }
        };
        const float bw = 190.0f * scale;
        if (ImGui::Button("Continue in background", ImVec2(bw, 0))) {
          st.quitAgentContinues = true;
          s_quitAgentModalOpen = false;
          ImGui::CloseCurrentPopup();
          proceedQuit();
        }
        ImGui::SameLine();
        if (ImGui::Button("Stop agent & quit", ImVec2(bw, 0))) {
#ifdef __EMSCRIPTEN__
          for (const auto& entry : st.runs)
            if (entry.second && entry.second->running.load())
              ui::requestStopRunFor(st, entry.first);
#else
          nlohmann::json res;
          std::string err;
          st.worker.callAgentCancel("", res, err);
#endif
          s_quitAgentModalOpen = false;
          ImGui::CloseCurrentPopup();
          proceedQuit();
        }
        ImGui::SameLine();
        if (ImGui::Button("Cancel", ImVec2(90.0f * scale, 0))) {
          s_quitAgentModalOpen = false;
          ImGui::CloseCurrentPopup();
        }
        ImGui::EndPopup();
      }
      if (!openAgent)
        s_quitAgentModalOpen = false; // dismissed via the title-bar X = Cancel
    }

    // ---------- unsaved-changes confirmation on app exit ----------
    // Raised from the main-loop close guard (st.wantQuitConfirm) when the user
    // tries to quit with dirty documents. Save All & Quit saves every dirty doc
    // then exits (cancelled Save-As keeps the app open); Quit Without Saving
    // discards; Cancel returns. quitConfirmed latches the approved choices so
    // the re-issued close request falls through the guard to the real exit.
    {
      static bool s_quitModalOpen = false;
      static bool s_quitSavePending = false;
      if (st.wantQuitConfirm) {
        st.wantQuitConfirm = false;
        s_quitModalOpen = true;
      }
      if (s_quitModalOpen)
        ImGui::OpenPopup("Quit Chudware?##appquit");
      ImGui::SetNextWindowPos(
          ImVec2(vp->Pos.x + vp->Size.x * 0.5f, vp->Pos.y + vp->Size.y * 0.5f),
          ImGuiCond_Appearing, ImVec2(0.5f, 0.5f));
      bool open = true;
      if (ImGui::BeginPopupModal("Quit Chudware?##appquit", s_quitSavePending ? nullptr : &open,
                                 ImGuiWindowFlags_AlwaysAutoResize |
                                     ImGuiWindowFlags_NoSavedSettings)) {
        auto userOwnedDirty = [](const AppState::DocTab &t) { return t.dirty; };
        int n = 0;
        for (const auto &t : st.docTabs)
          if (userOwnedDirty(t))
            ++n;
        ImGui::Text("%d document%s with unsaved changes:", n,
                    n == 1 ? "" : "s");
        ImGui::Spacing();
        for (const auto &t : st.docTabs)
          if (userOwnedDirty(t))
            ImGui::BulletText("%s", t.displayTitle().c_str());
        ImGui::Spacing();
        ImGui::TextUnformatted("Save before quitting?");
        ImGui::Spacing();
        const float bw = 150.0f * scale;
        const bool waitForEdits = st.hasPendingHumanMutations();
        const bool controlsDisabled = s_quitSavePending || waitForEdits;
        if (waitForEdits) ImGui::TextUnformatted("Waiting for pending edits...");
        if (controlsDisabled) ImGui::BeginDisabled();
        if (ImGui::Button("Save All & Quit", ImVec2(bw, 0))) {
          s_quitSavePending = true;
          saveAllTabs(st, true, [&st, win](bool ok, int) {
            s_quitSavePending = false;
            if (!ok) return;
            st.quitConfirmed = true;
            s_quitModalOpen = false;
            glfwSetWindowShouldClose(win, GLFW_TRUE);
          });
          if (!s_quitModalOpen) ImGui::CloseCurrentPopup();
        }
        ImGui::SameLine();
        if (ImGui::Button("Quit Without Saving",
                          ImVec2(bw + 30.0f * scale, 0))) {
          st.quitConfirmed = true;
          glfwSetWindowShouldClose(win, GLFW_TRUE);
          s_quitModalOpen = false;
          ImGui::CloseCurrentPopup();
        }
        ImGui::SameLine();
        if (ImGui::Button("Cancel", ImVec2(90.0f * scale, 0))) {
          s_quitModalOpen = false;
          ImGui::CloseCurrentPopup();
        }
        if (controlsDisabled) ImGui::EndDisabled();
        ImGui::EndPopup();
      }
      if (!open)
        s_quitModalOpen = false; // dismissed via the title-bar X = Cancel
    }

    // ---------- orphan recovery prompt (daemon mode) ----------
    // Raised from the daemon bootstrap (st.recoveryPrompt) when the daemon
    // reports retired docs from a previous session awaiting explicit recovery.
    // Recover reopens them one at a time in the background (recoverNextOrphan);
    // Discard drops them daemon-side; Later just dismisses — the daemon keeps
    // the entries, so the prompt returns on the next app start.
    {
      // Saved display tier -> every open part/assembly doc (restart restore + docs opened later).
      st.syncDisplayQuality();
      // User-owned viewport toggles / modeless View windows -> Settings (saved only when changed).
      st.persistViewPrefs();
      if (std::string configWarning = takeConfigCorruptionWarning();
          !configWarning.empty())
        st.toast(ui::ToastLevel::Warn, std::move(configWarning));
      static std::vector<AppState::RecoveryEntry> s_recoveryEntries;
      // Never raise it during a headless capture: this modal is centred and app-modal, so it
      // covered the very panel every CHUDWARE_SHOT scenario exists to photograph (the reveng_*
      // captures were all screenshotting this dialog). Shot runs are throwaway processes and
      // must not answer recover/discard on the user's behalf, so just leave the entries alone —
      // "Later" is the daemon-side no-op and this is equivalent to it.
      if (!st.recoveryPrompt.empty() && shot) st.recoveryPrompt.clear();
      if (!st.recoveryPrompt.empty()) {
        s_recoveryEntries = std::move(st.recoveryPrompt);
        st.recoveryPrompt.clear();
        st.recoveryModalOpen = true;
      }
      if (st.recoveryModalOpen)
        ImGui::OpenPopup("Recover unsaved parts?##recovery");
      ImGui::SetNextWindowPos(
          ImVec2(vp->Pos.x + vp->Size.x * 0.5f, vp->Pos.y + vp->Size.y * 0.5f),
          ImGuiCond_Appearing, ImVec2(0.5f, 0.5f));
      bool ropen = true;
      if (ImGui::BeginPopupModal("Recover unsaved parts?##recovery", &ropen,
                                 ImGuiWindowFlags_AlwaysAutoResize |
                                     ImGuiWindowFlags_NoSavedSettings)) {
        const size_t n = s_recoveryEntries.size();
        ImGui::Text("%zu unsaved part%s from a previous session:", n,
                    n == 1 ? "" : "s");
        ImGui::Spacing();
        // Name every part: a bare count gave no way to tell real work from an
        // agent's scratch copies before committing to reopen all of them.
        const float rowH = ImGui::GetTextLineHeightWithSpacing();
        if (ImGui::BeginChild("##recovery_names",
                              ImVec2(360.0f * scale,
                                     rowH * static_cast<float>(std::min<size_t>(n, 10)) +
                                         ImGui::GetStyle().WindowPadding.y * 2.0f),
                              ImGuiChildFlags_Borders)) {
          for (const auto &e : s_recoveryEntries)
            ImGui::BulletText("%s", e.name.c_str());
        }
        ImGui::EndChild();
        ImGui::Spacing();
        ImGui::TextUnformatted("Recover them now, or discard them for good?");
        ImGui::Spacing();
        const float bw = 110.0f * scale;
        if (ImGui::Button("Recover", ImVec2(bw, 0))) {
          if (engine::client() && !s_recoveryEntries.empty()) {
            auto run = std::make_shared<OrphanRecoveryRun>();
            run->entries = std::move(s_recoveryEntries);
            st.recoveryInFlight = true;
            st.toast(ui::ToastLevel::Info,
                     "Recovering " + std::to_string(run->entries.size()) +
                         " part" + (run->entries.size() == 1 ? "" : "s") +
                         " in the background — each tab opens when it is ready");
            recoverNextOrphan(st, std::move(run));
          }
          s_recoveryEntries.clear();
          st.recoveryModalOpen = false;
          ImGui::CloseCurrentPopup();
        }
        ImGui::SameLine();
        if (ImGui::Button("Discard", ImVec2(bw, 0))) {
          if (auto *c = engine::client()) {
            nlohmann::json r;
            std::string e;
            nlohmann::json ids = nlohmann::json::array();
            for (const auto &entry : s_recoveryEntries) ids.push_back(entry.id);
            if (!c->call("recovery_discard", nlohmann::json{{"ids", ids}}, r, e))
              st.toast(ui::ToastLevel::Error, "Discard failed: " + e);
          }
          s_recoveryEntries.clear();
          st.recoveryModalOpen = false;
          ImGui::CloseCurrentPopup();
        }
        ImGui::SameLine();
        if (ImGui::Button("Later", ImVec2(90.0f * scale, 0))) {
          s_recoveryEntries.clear();
          st.recoveryModalOpen = false;
          ImGui::CloseCurrentPopup();
        }
        ImGui::EndPopup();
      }
      if (!ropen) {
        s_recoveryEntries.clear();
        st.recoveryModalOpen = false; // title-bar X = Later
      }
    }

    // ---------- mesh-export tessellation options ----------
    // Raised by File > Export of a mesh format. Lets the user pick a quality
    // preset or fine-tune the chord/normal deflection, shows a live triangle
    // estimate, then sets requestExportFormat so the GL-thread pump runs the
    // actual Save-As + export.
    {
      auto &d = st.exportTess;
      // Preset -> (linear mm, angular deg). Custom keeps whatever is in the
      // fields.
      auto applyPreset = [&](int p) {
        if (p == 0) {
          d.linDeflection = 0.2f;
          d.angDeflectionDeg = 30.0f;
        } else if (p == 1) {
          d.linDeflection = 0.05f;
          d.angDeflectionDeg = 20.0f;
        } else if (p == 2) {
          d.linDeflection = 0.01f;
          d.angDeflectionDeg = 12.0f;
        }
        d.estimateDirty = true;
      };
      if (d.open)
        ImGui::OpenPopup("Export mesh options##exporttess");
      ImGui::SetNextWindowPos(
          ImVec2(vp->Pos.x + vp->Size.x * 0.5f, vp->Pos.y + vp->Size.y * 0.5f),
          ImGuiCond_Appearing, ImVec2(0.5f, 0.5f));
      ImGui::SetNextWindowSize(ImVec2(420.0f * scale, 0.0f),
                               ImGuiCond_Appearing);
      bool eopen = true;
      if (ImGui::BeginPopupModal("Export mesh options##exporttess", &eopen,
                                 ImGuiWindowFlags_AlwaysAutoResize |
                                     ImGuiWindowFlags_NoSavedSettings)) {
        std::string fmtUpper = d.fmt;
        for (char &c : fmtUpper)
          c = (char)std::toupper((unsigned char)c);
        ImGui::Text("Tessellation quality for %s", fmtUpper.c_str());
        if (d.fmt == "dxf" || d.fmt == "dwg")
          ImGui::TextDisabled("Solids and surfaces are written as ACIS 3D solids "
                              "AutoCAD can edit; 2D profiles keep their lines and arcs.");
        ImGui::Separator();
        ImGui::Spacing();

        ImGui::TextUnformatted("Quality");
        ImGui::SetNextItemWidth(-FLT_MIN);
        static const char *kPreset[4] = {"Coarse (fast, small file)",
                                         "Standard",
                                         "Fine (smooth, large file)", "Custom"};
        if (ImGui::BeginCombo(
                "##tesspreset",
                kPreset[d.preset < 0 || d.preset > 3 ? 1 : d.preset])) {
          for (int i = 0; i < 4; ++i)
            if (ImGui::Selectable(kPreset[i], d.preset == i)) {
              d.preset = i;
              if (i != 3)
                applyPreset(i);
            }
          ImGui::EndCombo();
        }

        ImGui::Spacing();
        if (ImGui::Checkbox("Advanced", &d.advanced)) {
        }
        if (d.advanced) {
          ImGui::Indent();
          float lin = d.linDeflection, angDeg = d.angDeflectionDeg;
          ImGui::TextUnformatted("Linear deflection (max chord deviation)");
          ImGui::SetNextItemWidth(-FLT_MIN);
          if (ImGui::InputFloat("##lindef", &lin, 0.0f, 0.0f,
                                d.relative ? "%.4g (fraction)" : "%.4g mm")) {
          }
          if (ImGui::IsItemDeactivatedAfterEdit() && lin > 0.0f) {
            d.linDeflection = lin;
            d.preset = 3;
            d.estimateDirty = true;
          }
          ImGui::TextUnformatted("Angular deflection (max normal deviation)");
          ImGui::SetNextItemWidth(-FLT_MIN);
          if (ImGui::SliderFloat("##angdef", &angDeg, 5.0f, 45.0f,
                                 "%.0f deg")) {
          }
          if (ImGui::IsItemDeactivatedAfterEdit()) {
            d.angDeflectionDeg = angDeg;
            d.preset = 3;
            d.estimateDirty = true;
          }
          if (ImGui::Checkbox("Relative deflection (scale with feature size)",
                              &d.relative))
            d.estimateDirty = true;
          if (d.fmt == "stl")
            ImGui::Checkbox("ASCII STL (default binary)", &d.ascii);
          ImGui::Unindent();
        }

        // Live triangle/vertex estimate.
        ImGui::Spacing();
        if (d.estimateDirty) {
          d.estimateDirty = false;
          std::string eerr;
          long long tris = 0, verts = 0;
          const double angRad =
              (double)d.angDeflectionDeg * 3.14159265358979 / 180.0;
          d.estimateValid = st.docMirror.exportEstimate(
              st.worker, st.docMirror.docId(), (double)d.linDeflection, angRad,
              d.relative, tris, verts, eerr);
          d.estTris = tris;
          d.estVerts = verts;
        }
        if (d.estimateValid)
          ImGui::TextDisabled("Estimated: %lld triangles, %lld vertices",
                              d.estTris, d.estVerts);
        else
          ImGui::TextDisabled("Estimate unavailable");

        ImGui::Spacing();
        ImGui::Separator();
        const float bw = 110.0f * scale;
        if (ImGui::Button("Export...", ImVec2(bw, 0))) {
          d.open = false;
          st.requestExportFormat =
              d.fmt; // GL pump runs Save-As + export with d.*
          ImGui::CloseCurrentPopup();
        }
        ImGui::SameLine();
        if (ImGui::Button("Cancel", ImVec2(bw, 0))) {
          d.open = false;
          ImGui::CloseCurrentPopup();
        }
        ImGui::EndPopup();
      }
      if (!eopen)
        d.open = false; // dismissed via the title-bar X
    }

    // ---------- Equations / Global Variables editor (modeless; gated by
    // st.showEquations) ---------- Opened from the FeatureManager "Equations"
    // node (double-click) or View > Equations. Lists doc.equations.globals;
    // add/edit -> eq_set, delete -> eq_delete, then reloads the mesh.
    ui::drawEquationsDialog(st);
    ui::drawScadEditor(st);

    // Configurations now lives inside the left manager's ConfigurationManager
    // tab, matching the commercial CAD mental model instead of floating as a
    // separate task-pane window.

    // ---------- Design Table panel (SolidWorks editable config grid)
    // ---------- Editable spreadsheet over the active doc's configurations:
    // rows=configs, columns=driven params + per-feature suppression. Cell edits
    // write per-config overrides via the same config_* RPCs; editing the active
    // row updates the viewport. No-op unless the flag is set.
    designTablePanel.draw(st);

    // ---------- Weldment Cut List panel (SolidWorks Weldment Cut List)
    // ---------- Read-only table of the active part's weld_member structural
    // members grouped by profile + length into rows with quantities. No-op
    // unless st.showCutListPanel.
    cutListPanel.draw(st);
    dialogsTiming.stop();

    // ---------- C15 pick-shot driver: simulate a center-screen pick ----------
    // The viewport panel rendered the id buffer for this frame's camera; pick
    // its center, apply the selection (highlight shows from the next frame),
    // and print the PickHit once so verification is scriptable. Runs every
    // frame so the camera has settled by capture.
    if (st.wantPick && st.pickReady) {
      const Renderer::CameraXform &cam = renderer.camera();
      int fw = cam.ssWidth / renderer.supersample();
      int fh = cam.ssHeight / renderer.supersample();
      render::PickHit center = idPass.pickFinal(fw / 2, fh / 2, fw, fh);
      st.hover = render::PickHit{};
      st.selection.selectOne(center);
      st.pickStatus = st.pickLabel(center);
      if (!st.pickPrinted) {
        const char *kindName = center.kind == render::PickKind::Face   ? "face"
                               : center.kind == render::PickKind::Edge ? "edge"
                               : center.kind == render::PickKind::Vertex
                                   ? "vertex"
                                   : "none";
        std::printf("pick: body=%u kind=%s id=%u\n", center.body_id, kindName,
                    center.topo_id);
        std::fflush(stdout);
        st.pickPrinted = true;
      }
    }

    // ---------- modals ----------
    core::framestats::Scope modalsTiming("dialogs");
    ui::drawSettingsModal(st, busy);

    // Provider brand icons: drain the requests queued by every drawProviderLogo() call this
    // frame (composer model chip, Settings rows, …). This MUST live in the always-drawn frame
    // body — hosting it inside the Settings > Providers tab meant the pump only ran while that
    // tab was open, so the copilot's chip showed the fallback badge until the user went looking
    // at provider settings. GL-thread only, which the main loop already guarantees.
    ui::providerLogosTick(st);

    // ---------- in-app profiler window + unified command/settings search
    // ----------
    ui::drawProfilerWindow(st);
    ui::drawCommandSearch(st);
    modalsTiming.stop();

    // ---------- click empty space (any panel whitespace) to deselect
    // everything ---------- The viewport's own full-window input surface
    // handles empty-3D clicks; this covers the panels: a left click with
    // nothing hovered/active and no open popup clears the selection.
    if (ImGui::IsMouseClicked(ImGuiMouseButton_Left) &&
        !ImGui::IsAnyItemHovered() && !ImGui::IsAnyItemActive() &&
        !ImGui::IsPopupOpen(nullptr, ImGuiPopupFlags_AnyPopupId))
      st.clearSelection();

    // ---------- splash notifications (top-left glass; click to copy + clear)
    // ----------
    {
      core::framestats::Scope timing("toasts");
      ui::drawToasts(st);
    }
#ifdef CHUDWARE_ENABLE_CEF
    ui::CefWebView::finishFrame();
#endif

    // ---------- W1-B: consume this frame's cursor request (item 3) ----------
    // After all panels have had a chance to call st.requestCursor(); suspends
    // ImGui cursor management and sets the GLFW cursor when a non-default
    // cursor was requested, else restores.
    ui::applyCursorRequest(st, win);

    {
      core::framestats::Scope timing("imgui_render");
      ImGui::Render();
    }
#ifdef __EMSCRIPTEN__
    wasmPublishTextInput();
#endif
    int dw, dh;
    glfwGetFramebufferSize(win, &dw, &dh);
    glViewport(0, 0, dw, dh);
    glClearColor(ui::palette.windowBg[0], ui::palette.windowBg[1],
                 ui::palette.windowBg[2], 1.0f);
    glClear(GL_COLOR_BUFFER_BIT);
    {
      core::framestats::Scope timing("imgui_draw");
      ui::adaptCopilotTextToBackdrop(ImGui::GetDrawData());
      ImGui_ImplOpenGL3_RenderDrawData(ImGui::GetDrawData());
    }

#ifndef __EMSCRIPTEN__
    // Scripted-scenario per-step captures + frame-loop control
    // (m1/sketch/asm/fea/evalmass/ activity): endFrame() writes any pending
    // step screenshot from the backbuffer, mirrors the final step to
    // screenshotPath, swaps buffers, and reports whether the scenario cut this
    // loop iteration short (was the inline swap+continue) or ended the loop
    // (swap+break).
    {
      core::framestats::Scope timing("capture");
      ui::ScenarioRunner::FrameAction act =
          scenarios.endFrame(st, dw, dh, screenshotPath);
      if (act == ui::ScenarioRunner::FrameAction::Break)
        break;
      if (act == ui::ScenarioRunner::FrameAction::Continue) {
        if (shotEngineUnavailable()) {
          failShotNoEngine();
          break;
        }
        continue;
      }
    }
#endif

    // tree shot needs extra frames for the tree to repaint after async doc load
    // wantTree but !treeShotDone: keep waiting (up to 30 frames) for the worker
    // to start wantTree and treeShotDone: take the screenshot at frame 10 (tree
    // has repainted)
    int shotFrame = st.wantTree ? (st.treeShotDone ? 10 : 30) : 6;
    // equations shot: wait (up to 30 frames) for the worker, then capture at
    // frame 10 once the two globals are seeded and the dialog has repainted
    // with its rows.
    if (st.wantEquations)
      shotFrame = st.equationsShotDone ? 10 : 30;
    if (st.wantEcad) {
      if (!st.ecadShotDone || !st.ecadKicadStatusKnown) {
        static const auto started = std::chrono::steady_clock::now();
        if (std::chrono::steady_clock::now() - started > std::chrono::seconds(90)) {
          std::fprintf(stderr, "[ecad-shot] timed out waiting for fixture and KiCad status\n");
          shotExitCode = 1;
          break;
        }
        shotFrame = 99999; // wait for the fixture and resolved installation state
      }
      else if (ecadSimShotActive(st) && ecadSimRunState().running)
        shotFrame = 99999; // deterministic sim shot: wait for async completion
      else if (ecadSimShotActive(st) &&
               (!st.ecadSimResults.is_object() || st.ecadSimResults.empty())) {
        std::fprintf(stderr, "[ecad-shot] circuit simulation produced no results: %s\n",
                     st.status.c_str());
        shotExitCode = 1;
        break;
      }
      else if (st.wantFocusViewport > 0 || st.wantFocusEcad > 0)
        shotFrame = 99999; // let focus settle
      else
        shotFrame = 10; // then capture
    }
    // cam shot: wait for the worker + the build/load scenario, then give the
    // CAM tab a few frames to come forward (camLoadJob re-requests focus for
    // ~10 frames) before capturing.
    if (st.wantCam)
      shotFrame = st.camShotDone ? 60 : 99999;
    // simdisc: keep the loop alive until the scenario forced the study picker
    // open, then give the popup a few frames to draw before capturing.
    if (st.wantSimDisc)
      shotFrame = st.simDiscShotDone ? 30 : 99999;
    // PLM shot. The graph page needs its cad_graph_get payload plus ~2s (120
    // frames) for the force layout to settle; every OTHER page just needs its
    // async fetches to land. This used to key on the WORKSPACE alone, so any
    // non-graph page sat at 99999 forever waiting for a graph that never loads
    // — the capture never happened. (It went unnoticed because startup doc
    // restore yanked the workspace back to Model, so the branch never ran and
    // the scenario silently photographed the modelling tab instead.)
    if (wantPlmShot && st.activeWorkspace == AppState::Workspace::PLM) {
      static int plmShotAt = -1;
      if (wantPlmGraphShot) {
        if (plmShotAt < 0 && ui::plmGraphLoaded()) plmShotAt = st.frame + 120;
      } else if (plmShotAt < 0 && st.frame > 20 && !ui::plmBusy()) {
        plmShotAt = st.frame + 10;   // settled: a few frames to repaint
      }
      // Hard ceiling so a wedged fetch fails as a visible spinner shot rather
      // than hanging the harness forever.
      shotFrame = plmShotAt > 0 ? plmShotAt : (st.frame >= 1200 ? st.frame : 99999);
    }
    // Parts Library: wait for catalog/search and visible previews, with a
    // wall-clock ceiling independent of the renderer's variable frame rate.
    if (shot && shotMode && std::strcmp(shotMode, "parts") == 0) {
      static int partsShotAt = -1;
      static const auto partsShotDeadline =
          std::chrono::steady_clock::now() + std::chrono::seconds(60);
      if (partsShotAt < 0 && st.frame > 6 && ui::partsLibrarySettled(st)) partsShotAt = st.frame + 8;
      shotFrame = partsShotAt > 0 ? partsShotAt :
          (std::chrono::steady_clock::now() >= partsShotDeadline ? st.frame :
           std::numeric_limits<int>::max());
    }
    // Explorer: capture once document previews have rendered (and any scripted open landed),
    // bounded so a wedged load still produces a picture.
    if (shot && shotMode && std::strcmp(shotMode, "explorer") == 0) {
      static int explorerShotAt = -1;
      if (explorerShotAt < 0 && st.frame > 6 && ui::fileExplorerShotSettled(st))
        explorerShotAt = st.frame + 12;
      shotFrame = explorerShotAt > 0 ? explorerShotAt : 1500;
    }
    // iconpipe: keep the loop alive (offscreen icon renders happen in
    // tick()) until every recipe is captured, then exit promptly — the --shot
    // backbuffer capture is a throwaway.
    // chat_context: capture a few frames after the chips bind to the live doc (ceiling 300).
    if (shotChatContext)
      shotFrame = chatContextBoundAt > 0 ? chatContextBoundAt + 8
                                         : (st.frame >= 300 ? st.frame : 99999);
    if (st.wantIconPipe)
      shotFrame = st.iconPipeShotDone ? 3 : 99999;
    // H2 daemon_h2: wall-clock scenario. captureFrame==-1 => write the notice
    // PNG this frame (hostd just respawned, chip still sticky). captureFrame>0
    // => final post-reconnect shot at that absolute frame.
    if (st.wantDaemonH2) {
      if (st.daemonH2CaptureFrame == -1) {
        core::framestats::Scope timing("capture");
        const char *slash = std::strrchr(screenshotPath, '/');
        std::string midPath = slash ? std::string(screenshotPath, slash + 1) +
                                          "daemon_h2_notice.png"
                                    : std::string("daemon_h2_notice.png");
        std::vector<unsigned char> px(dw * dh * 4);
        glReadBuffer(GL_BACK);
        glReadPixels(0, 0, dw, dh, GL_RGBA, GL_UNSIGNED_BYTE, px.data());
        std::vector<unsigned char> flip(dw * dh * 4);
        for (int y = 0; y < dh; ++y)
          std::memcpy(&flip[(dh - 1 - y) * dw * 4], &px[y * dw * 4], dw * 4);
        stbi_write_png(midPath.c_str(), dw, dh, 4, flip.data(), dw * 4);
        std::printf("[daemon_h2] notice screenshot -> %s (unreachable=%d)\n",
                    midPath.c_str(), (int)st.daemonUnreachable);
        std::fflush(stdout);
        st.daemonH2CaptureFrame = 0; // clear sentinel; wait for phase 4 latch
      }
      shotFrame =
          (st.daemonH2CaptureFrame > 0) ? st.daemonH2CaptureFrame : 99999;
    }
    // copilot_slash: capture the open slash menu at frame 4 (before the scripted
    // Enter) as a mid PNG; the final capture at frame 8 shows the accepted fill.
    if (st.wantCopilotSlash) {
      if (st.frame == 4) {
        core::framestats::Scope timing("capture");
        const char *slash = std::strrchr(screenshotPath, '/');
        std::string midPath = slash ? std::string(screenshotPath, slash + 1) +
                                          "copilot_slash_menu.png"
                                    : std::string("copilot_slash_menu.png");
        std::vector<unsigned char> px(dw * dh * 4);
        glReadBuffer(GL_BACK);
        glReadPixels(0, 0, dw, dh, GL_RGBA, GL_UNSIGNED_BYTE, px.data());
        std::vector<unsigned char> flip(dw * dh * 4);
        for (int y = 0; y < dh; ++y)
          std::memcpy(&flip[(dh - 1 - y) * dw * 4], &px[y * dw * 4], dw * 4);
        stbi_write_png(midPath.c_str(), dw, dh, 4, flip.data(), dw * 4);
        std::printf("[copilot_slash] menu screenshot -> %s\n", midPath.c_str());
        std::fflush(stdout);
      }
      shotFrame = 8;
    }
    if (shotFrame >= 99999 && shotEngineUnavailable()) {
      failShotNoEngine();
      break;
    }
    if (shot && ++st.frame >= shotFrame) {
      core::framestats::Scope timing("capture");
      std::vector<unsigned char> px(dw * dh * 4);
      glReadBuffer(GL_BACK);
      glReadPixels(0, 0, dw, dh, GL_RGBA, GL_UNSIGNED_BYTE, px.data());
      std::vector<unsigned char> flip(dw * dh * 4);
      for (int y = 0; y < dh; ++y)
        std::memcpy(&flip[(dh - 1 - y) * dw * 4], &px[y * dw * 4], dw * 4);
      stbi_write_png(screenshotPath, dw, dh, 4, flip.data(), dw * 4);
      std::printf("ui screenshot -> %s (%dx%d)\n", screenshotPath, dw, dh);
      break;
    }
    swapFrameBuffers(win);
#ifndef __EMSCRIPTEN__
    if (!s_nativeStartup.firstFramePresented) {
      s_nativeStartup.firstFramePresented = true;
      startup_trace::mark("first frame presented");
    }
    if (s_nativeStartup.starterOpenPending) {
      s_nativeStartup.starterOpenPending = false;
      s_nativeStartup.starterPresented = true;
      startup_trace::mark("starter part open (doc visible)");
    } else if (s_nativeStartup.starterPresented &&
               !s_nativeStartup.interactiveMarked) {
      s_nativeStartup.interactiveMarked = true;
      startup_trace::mark("first interactive frame after starter");
    }
#endif
#ifdef __EMSCRIPTEN__
    // ASYNCIFY: yield to the browser event loop so WebSocket/timer callbacks
    // run.
    emscripten_sleep(0);
#endif
  }
  core::framestats::flushReport();
  // RPC-backed settings edits (skills/rules editors, model-assignment draft) must be handed to
  // the worker while the RPC layer is still up: st.rpc.shutdown() below aborts the in-flight call.
  if (!shot)
    ui::flushSettingsModalRpcEdits(st);
  core::shutdownEngineEvents();

  // Release any in-flight RPC before quit cleanup takes the daemon client's lock.
  if (std::getenv("CHUDWARE_DEBUG_SHUTDOWN"))
    std::fprintf(stderr, "[td] rpc.shutdown\n");
#ifndef __EMSCRIPTEN__
  ecadRecoveryShutdown(st);
  guiConnection.shutdown();
#endif
  st.rpc.shutdown(); // Abort the in-flight one before bounded cleanup.
  st.authRpc.shutdown();
  st.waitWorkerStartup();

  // Detach clean tabs without destroying agent work. Only an explicitly confirmed
  // quit-without-saving may discard a tab with outstanding human edits.
  if (!shot && engine::daemonMode()) {
    if (auto *c = engine::client()) {
      const auto deadline = std::chrono::steady_clock::now() +
                            std::chrono::seconds(10);
      for (const auto &t : st.docTabs) {
        if (t.docId.empty() || (t.kind != AppState::DocTab::TabKind::Part &&
                                t.kind != AppState::DocTab::TabKind::Assembly))
          continue;
        const auto remaining = std::chrono::duration_cast<std::chrono::milliseconds>(
                                   deadline - std::chrono::steady_clock::now()).count();
        if (remaining <= 0)
          break;
        const bool discardHumanEdits = t.dirty && st.quitConfirmed;
        nlohmann::json res;
        std::string err;
        const int timeoutMs = remaining > 3000 ? 3000 : static_cast<int>(remaining);
        if (!c->call(discardHumanEdits ? "doc_discard" : "detach_doc", {{"doc_id", t.docId}},
                     res, err, timeoutMs)) {
          st.addLog("[tabs] quit cleanup stopped: " + err);
          break; // A timed-out call would reconnect before the next one.
        }
      }
    }
  }

  // Debounced settings autosaves (system-prompt / role-prompt editors) commit here; the config
  // writer itself is drained by stopMeshLoader()'s flushConfigWrites() below.
  ui::flushSettingsModalEdits(st);
  ui::shutdownAgentControl(st);
  ui::flushChatPersistence(st); // drain transcript and draft writes before teardown
  ui::freeIcons(); // C14: release atlas textures while GL context is still live
  ui::destroyCursors(); // W1-B: free the GLFW standard cursors
  if (std::getenv("CHUDWARE_DEBUG_SHUTDOWN"))
    std::fprintf(stderr, "[td] stopMeshLoader\n");
  st.stopMeshLoader(); // join mesh thread before static dtors (avoid
                       // terminate)
  if (std::getenv("CHUDWARE_DEBUG_SHUTDOWN"))
    std::fprintf(stderr, "[td] worker.stop\n");
  st.worker.stop();
  if (std::getenv("CHUDWARE_DEBUG_SHUTDOWN"))
    std::fprintf(stderr, "[td] simClient.stop\n");
  simClient.stop(); // C19: shut down the second worker process
#ifdef CHUDWARE_ENABLE_CEF
  if (std::getenv("CHUDWARE_DEBUG_SHUTDOWN"))
    std::fprintf(stderr, "[td] cef.shutdown\n");
  ui::cef::shutdown(); // no-op unless CEF was initialised (browsers close in
                       // their dtors)
#endif
  if (std::getenv("CHUDWARE_DEBUG_SHUTDOWN"))
    std::fprintf(stderr, "[td] imgui/glfw shutdown\n");
  // RE-40: release the RE workspace's camera + GL textures while the context is
  // still current (must precede ImGui_ImplOpenGL3_Shutdown / glfwDestroyWindow).
  ui::revengWorkspaceTeardown();
  ui::shutdownBackdropText();
  ImGui_ImplOpenGL3_Shutdown();
  ImGui_ImplGlfw_Shutdown();
  ImGui::DestroyContext();
  glfwDestroyWindow(win);
  glfwTerminate();
  if (std::getenv("CHUDWARE_DEBUG_SHUTDOWN"))
    std::fprintf(stderr, "[td] done\n");
  return shotExitCode;
}
