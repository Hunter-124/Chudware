#include "worker_client.h"

#if defined(_WIN32)
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include "../ipc/ipc_windows_local.h"
#elif !defined(__EMSCRIPTEN__)
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <sys/wait.h>
#include <unistd.h>

#include <cerrno>
#include <filesystem>
#include <thread>
#include <vector>
#endif
#include <chrono>
#include <cstring>

#include "../ipc/ipc_client.h"
#include <cstdio> // T-G9 parity trace (CHUDWARE_RPC_TRACE) file append
#include <cstdlib>
#include <functional> // T-G9 parity trace secret redaction
#include <cwchar> // _wcsnicmp for the Win32 child env block
#include <string>
#include <shared_mutex>
#include <vector>
#include <unordered_map>
#if defined(_WIN32)
#include <filesystem>
#endif
#include "security/integrity.h"
#include "security/runtime_guard.h"
#include "util/platform_paths.h"

#include "bootstrap.h"
#include "cpp_core.h" // P16: legacy in-process libchudware (InProcCore backend)
#include "engine_backend.h" // Phase 4: InProcCore vs DaemonConn seam (CHUDWARE_ENGINE_DAEMON)

using nlohmann::json;

namespace {

struct LiveRoute {
  std::string path;
  std::string remoteId;
  std::string itemId;
  json participant;
};

std::mutex g_liveRoutesMtx;
std::shared_mutex g_liveDispatchMtx;
std::unordered_map<std::string, LiveRoute> g_liveRoutes;
// Serializes daemon-mode simulation-lane calls (engine::RunLane::Sim) across EVERY
// WorkerClient in the process: engine_backend has ONE simulation connection per process, so a
// solve on SimClient's client and a sim_mesh preview on the GUI's client contend for the same
// lane. Holding this instead of callMtx_ keeps a solve from parking the client's other calls,
// and lets a preview see a busy lane (try_lock) rather than queue behind the solve.
std::mutex g_simLaneMtx;

std::string liveString(const json &value, const char *key) {
  if (!value.is_object())
    return {};
  const auto it = value.find(key);
  return (it != value.end() && it->is_string()) ? it->get<std::string>()
                                                 : std::string();
}

std::string livePath(const json &params) {
  std::string value = liveString(params, "path");
  return value.empty() ? liveString(params, "project_dir") : value;
}

std::string liveRemoteId(const json &params) {
  std::string value = liveString(params, "remote_id");
  if (value.empty())
    value = liveString(params, "remote");
  return value.empty() ? std::string("origin") : value;
}

std::string liveItemId(const json &params) {
  std::string value = liveString(params, "item_id");
  return value.empty() ? liveString(params, "item") : value;
}

json liveParticipant(const json &params) {
  if (!params.is_object())
    return nullptr;
  const auto participant = params.find("participant");
  if (participant != params.end() && !participant->is_null())
    return *participant;

  const std::string id = liveString(params, "participant_id");
  std::string display = liveString(params, "participant_display");
  if (display.empty())
    display = liveString(params, "display_name");
  if (id.empty() && display.empty())
    return nullptr;

  json value = {{"id", id}, {"display", display}, {"account_id", nullptr}};
  const auto account = params.find("account_id");
  if (account != params.end() && !account->is_null())
    value["account_id"] = *account;
  return value;
}

bool sameLiveIdentity(const LiveRoute &route, const std::string &path,
                      const std::string &remoteId,
                      const std::string &itemId) {
  return route.path == path && route.remoteId == remoteId &&
         route.itemId == itemId;
}

bool findLiveRoute(const json &params, LiveRoute &route) {
  if (!params.is_object())
    return false;
  const auto id = params.find("doc_id");
  if (id == params.end() || !id->is_string())
    return false;
  std::lock_guard<std::mutex> lock(g_liveRoutesMtx);
  const auto found = g_liveRoutes.find(id->get<std::string>());
  if (found == g_liveRoutes.end())
    return false;
  route = found->second;
  return true;
}

bool successfulLiveEnvelope(const json &result) {
  if (!result.is_object())
    return false;
  const auto ok = result.find("ok");
  return ok != result.end() && ok->is_boolean() && ok->get<bool>();
}

void registerLiveRoute(const json &params, const json &result) {
  const std::string localDocId = liveString(result, "local_doc_id");
  if (localDocId.empty())
    return;

  LiveRoute route;
  route.path = livePath(params);
  route.remoteId = liveString(result, "remote_id");
  if (route.remoteId.empty())
    route.remoteId = liveRemoteId(params);
  route.itemId = liveString(result, "item_id");
  if (route.itemId.empty())
    route.itemId = liveItemId(params);
  const auto returnedParticipant =
      result.is_object() ? result.find("participant") : result.end();
  route.participant =
      (result.is_object() && returnedParticipant != result.end())
          ? *returnedParticipant
          : liveParticipant(params);

  const std::string requestedDocId = liveString(params, "local_doc_id");
  std::lock_guard<std::mutex> lock(g_liveRoutesMtx);
  for (auto it = g_liveRoutes.begin(); it != g_liveRoutes.end();) {
    if ((!requestedDocId.empty() && it->first == requestedDocId) ||
        sameLiveIdentity(it->second, route.path, route.remoteId, route.itemId)) {
      if (route.participant.is_null())
        route.participant = it->second.participant;
      it = g_liveRoutes.erase(it);
    } else {
      ++it;
    }
  }
  g_liveRoutes[localDocId] = std::move(route);
}

void removeLiveRoutes(const json &params) {
  const std::string path = livePath(params);
  const std::string remoteId = liveRemoteId(params);
  const std::string itemId = liveItemId(params);
  std::lock_guard<std::mutex> lock(g_liveRoutesMtx);
  for (auto it = g_liveRoutes.begin(); it != g_liveRoutes.end();) {
    if (sameLiveIdentity(it->second, path, remoteId, itemId))
      it = g_liveRoutes.erase(it);
    else
      ++it;
  }
}

json liveMethodError(const json &response) {
  std::string code = liveString(response, "reason");
  if (code.empty())
    code = "live_proposal_refused";
  std::string message = liveString(response, "hint");
  if (message.empty())
    message = code;
  return json{{"code", code}, {"message", message}, {"detail", response}};
}

// In-process live event streaming (desktop InProcCore path): bridge cpp_core's
// thread-local C event sink to the current call's EventFn so agent-run events
// reach the Copilot/timeline AS THEY HAPPEN instead of a batched replay after
// the (multi-minute) run. Daemon mode streams via hostd's agent.event push
// (engine_events) instead, so this in-proc sink is used only when
// !daemonMode().
thread_local WorkerClient::EventFn t_inproc_event_sink;
void inprocEventThunk(const char *ev_json, void *) {
  if (!t_inproc_event_sink || !ev_json)
    return;
  try {
    t_inproc_event_sink(nlohmann::json::parse(ev_json));
  } catch (...) {
  }
}
// Run an in-process agent engine::call with live streaming to onEvent.
// `streamedLive` reports whether events fired live (so the caller skips the
// batched replay to avoid doubles). RAII-clears the sink even if engine::call
// throws.
bool callAgentStreaming(const std::string &coreMethod, const json &p,
                        json &cppResult, std::string &cerr, int timeoutMs,
                        const WorkerClient::EventFn &onEvent,
                        bool &streamedLive) {
  if (engine::daemonMode()) {
    // Daemon: hostd pushes {"event":"agent.event","data":<ev>} and EngineClient
    // hands our router the PAYLOAD (`data`) live, mid-call, on this worker
    // thread. `fired` tells us whether the daemon actually streamed (a
    // same-build daemon does) so an OLD daemon that only returns the batched
    // result["events"] still falls back to the caller's replay.
    bool fired = false;
    auto router = [&](const json &ev) {
      if (onEvent && ev.is_object() && !ev.empty()) {
        fired = true;
        onEvent(ev);
      }
    };
    bool ok = engine::call(coreMethod, p, cppResult, cerr, timeoutMs, router);
    streamedLive = fired;
    return ok;
  }
  // In-process: the cpp_core thread-local sink fires each event live ->
  // onEvent.
  streamedLive = static_cast<bool>(onEvent) && core::cpp_core().hasEventSink();
  struct Guard {
    bool on;
    ~Guard() {
      if (on) {
        core::cpp_core().setEventSink(nullptr, nullptr);
        t_inproc_event_sink = {};
      }
    }
  } guard{streamedLive};
  if (streamedLive) {
    t_inproc_event_sink = onEvent;
    core::cpp_core().setEventSink(&inprocEventThunk, nullptr);
  }
  return engine::call(coreMethod, p, cppResult, cerr, timeoutMs);
}

// --- Per-document routing to the pure-C++ core (libchudware) --------------------
// A document CREATED in the C++ core gets a "doc-cpp..." id, so every
// subsequent doc_* call carrying that id routes to the C++ core too — the id
// prefix IS the commit-at-doc_new marker, giving an all-or-nothing-per-document
// binding with no separate routing state. The C++ doc engine is the only
// document backend (R5a/T-RF21; parity-complete since 2026-06-19), so the
// native agent seam below drives the same doc-cpp sessions.

// Methods that MINT a new document (vs. operate on an existing one). hostd
// attaches a minted document rw to the connection that minted it, so callImpl
// never sends these over a short-lived side connection. No services worker
// serves them: a core failure surfaces to the caller.
bool isMintingMethod(const std::string &method, const json &params) {
  if (method == "doc_new" || method == "doc_open")
    return true;
  if (method == "import_file") {
    auto into = params.is_object() ? params.find("into") : params.end();
    const bool hasInto = params.is_object() && into != params.end() &&
                         into->is_string() && !into->get<std::string>().empty();
    return !hasInto; // import into a NEW doc mints; import into an existing doc
                     // does not
  }
  return false;
}
bool routeDocToCpp(const std::string &method, const json &params) {
  if (method == "doc_compile_cpp" || method == "doc_validate")
    return false; // diagnostics
  // The parametric standard-parts catalog lives ONLY in the C++ core:
  // parts_catalog_list is doc-independent (family/size listing); parts_make
  // mints a NEW doc-cpp part from the generator. There is no Python equivalent,
  // so always route to the core.
  if (method == "parts_catalog_list" || method == "parts_make")
    return true;
  // T-RF19: doc_formats (the import/export format matrix) is C++-only
  // (core/doc/formats).
  if (method == "doc_formats") {
    if (!engine::available()) {
      std::string lerr;
      if (!engine::ensureLoaded(lerr))
        return false; // can't load -> Python
    }
    return true;
  }
  // T-RF19 leaves 4-6:
  // hydraulic/piping/structural/tolerance/mechanical/materials/dfm ported to
  // core/ (dispatch in libchudware.cpp). Route the ported sets.
  static const char *const kR4e6[] = {
      // hydraulic (core/hydraulic)
      "hydraulic_new",
      "hydraulic_templates",
      "hydraulic_add_component",
      "hydraulic_add_hose",
      "hydraulic_size_cylinder",
      "hydraulic_size_pump",
      "hydraulic_size_accumulator",
      "hydraulic_valve_flow",
      "hydraulic_simulate",
      "hydraulic_size_motor",
      "pump_drive_size",
      "hydraulic_bom",
      "hydraulic_piping_handoff",
      "hydraulic_verify_piping",
      "hydraulic_fea_handoff",
      "hydraulic_verify_fea",
      // piping (core/piping)
      "piping_solve",
      "piping_report",
      "piping_operating_point",
      "piping_fluid",
      "piping_cfd_boundary",
      "piping_verify_cfd",
      "piping_route",
      "piping_edit",
      "piping_fitting_k",
      "piping_spec_check",
      "piping_bom",
      "vessel_size",
      "duct_size",
      // structural (core/structural)
      "struct_analyze",
      "struct_check",
      "struct_estimate",
      // tolerance / mechanical / materials / dfm
      "stackup_analyze",
      "gear_check",
      "bearing_life",
      "material_get",
      "material_density",
      "material_resolve_key",
      "material_fea_table",
      "material_fea_aliases",
      "material_cam_data",
      "material_cam_id",
      "material_cam_materials",
      "material_all",
      "dfm_known_processes",
      "dfm_get_profile",
      "dfm_resolve_process",
      "dfm_fit_clearance",
      "dfm_modeled_dim",
      "dfm_process_block_text",
      // parts placement (core/parts/place; callers pass `record` — hub FTS is
      // Python)
      "part_place",
      "part_import_file",
  };
  {
    bool ported = false;
    for (const char *p : kR4e6)
      if (method == p) {
        ported = true;
        break;
      }
    if (ported) {
      if (!engine::available()) {
        std::string lerr;
        if (!engine::ensureLoaded(lerr))
          return false; // can't load -> Python
      }
      return true;
    }
  }
  // T-RF18: sourcing/BOM/cost/spec-match ported to core/sourcing (dispatch in
  // libchudware.cpp with production seams). Route the ported set; the vendor
  // registry read methods and parts_hub stay Python until their cutover.
  if (method == "bom_rollup" || method == "bom_price" ||
      method == "request_quote" || method == "match_part_number" ||
      method == "vendor_part_fetch" || method == "spec_sheet_match" ||
      method == "list_spec_templates") {
    if (!engine::available()) {
      std::string lerr;
      if (!engine::ensureLoaded(lerr))
        return false; // can't load -> Python
    }
    return true;
  }
  // Agent RPCs are owned by the AGENT seam (routeAgentToCpp), which translates
  // the host names (agent_run_v2 -> agent_run, get_prompts_v2 ->
  // agent_get_prompts_v2). They also carry the active doc's doc_id, so without
  // this guard the doc seam below would greedily claim them by the doc-cpp
  // prefix and call the C++ core with the UNtranslated name -> "unknown method:
  // agent_run_v2". Never route agent_* / get_prompts_v2 here.
  if (method.rfind("agent_", 0) == 0 || method == "get_prompts_v2")
    return false;
  // A C++-backed doc carries a doc-cpp id -> EVERY call operating on it routes
  // to the C++ core for that doc's whole lifetime (even if the flag was since
  // toggled off). This is NOT limited to doc_* — config_* / weld_* / export_* /
  // etc. all carry the doc_id and belong to the same document. An unported
  // method then fails loud in the core (no Python doc to fall back to),
  // surfacing the gap rather than misrouting.
  if (params.is_object()) {
    // doc_id (most doc_* / config_* / export_*) or `into` (import_file appends
    // to a doc) binds the call to a C++-backed document. A part/assembly
    // carries a "doc-cpp" id; a 2D DRAWING session (drawing_new / drawing_add_*
    // / drawing_get / drawing_bom / export_drawing) carries a "dwg-cpp" id —
    // both route to the C++ core. eval_compare (WS-C reverse-engineering:
    // params {doc_a, doc_b, ...}, no "doc_id") is the one eval_* RPC that does
    // NOT carry a plain "doc_id" -- without doc_a/doc_b here it would silently
    // miss this C++-only RPC and fall through to the (nonexistent, in this
    // RPC's case) Python doc path. eval_interference/eval_feature_overlap/
    // doc_face_wires all use "doc_id" and are already covered by the generic
    // sniff below.
    for (const char *key : {"doc_id", "into", "doc_a", "doc_b"}) {
      auto it = params.find(key);
      if (it != params.end() && it->is_string()) {
        const std::string v = it->get<std::string>();
        if (v.rfind("doc-cpp", 0) == 0 || v.rfind("dwg-cpp", 0) == 0)
          return true;
      }
    }
    // drawing_new carries the SOURCE part by id (source_doc_id), not a doc_id
    // of its own yet — route it to C++ when the source part is C++-backed so
    // the new drawing is minted in (and stays bound to) the C++ core.
    auto sit = params.find("source_doc_id");
    if (sit != params.end() && sit->is_string() &&
        sit->get<std::string>().rfind("doc-cpp", 0) == 0)
      return true;
  }
  // doc_new / doc_open / import_file (no `into`) MINT a fresh doc in the C++
  // core, which handles PART and ASSEMBLY documents (P57b compile + P57d
  // resolve_pick assembly branch + P57e primitive cut/merge; cpp_asm_parity
  // 10/10 incl. the host pick->mate->seat path). A doc_open of a non-part/asm
  // container is declined by the core (code "unsupported") and that structured
  // error reaches the caller.
  if (method == "doc_new") {
    const std::string kind = params.is_object()
                                 ? params.value("kind", std::string("part"))
                                 : std::string("part");
    return kind.empty() || kind == "part" || kind == "assembly";
  }
  if (method == "doc_open")
    return true; // core declines non-parts with a structured error
  if (isMintingMethod(method, params))
    return true; // import_file minting a new doc
  // vendor_part_insert mutates/mints a doc by applying an `import` feature; it
  // now lives only in the C++ core (the Python doc engine is gone). Route it
  // there whenever it carries no doc-cpp id yet (the prefix check above already
  // catches the into/doc_id case).
  if (method == "vendor_part_insert")
    return true;
  return false;
}
// --- Native offline-agent routing ------------------------------------------------
// Online turns use agent_admin_turn/Chudcode. These methods retain deterministic native
// fallback behavior for C++ documents, plus read-only probes and control calls.

// Is the active document a C++-backed ("doc-cpp") session? For agent_run_v2 /
// agent_ask_v2 the host passes the active doc_id directly in params;
// agent_pm_assist_v1 carries it too. get_prompts_v2 has no doc and is always
// safe to route (read-only).
bool agentDocIsCpp(const std::string &method, const json &params) {
  if (method == "get_prompts_v2")
    return true; // doc-independent (prompt catalog)
  // The pre-flight clarify gate fires on a Design-new submit BEFORE any part
  // doc exists, so it is doc-independent like get_prompts_v2: always safe to
  // route to C++ (it only reads the request + optional live-session context).
  // This also loads the DLL lazily.
  if (method == "agent_clarify_v1")
    return true;
  // Cancel is process-wide registry keyed by run_id — no doc needed.
  if (method == "agent_cancel")
    return true;
  // Steer and the ask_user answer are the same per-run registry path as cancel
  // — no doc needed.
  if (method == "agent_steer" || method == "agent_answer")
    return true;
  if (!params.is_object())
    return false;
  auto it = params.find("doc_id");
  if (it == params.end() || !it->is_string())
    return false;
  return it->get<std::string>().rfind("doc-cpp", 0) == 0;
}

bool routeAgentToCpp(const std::string &method, const json &params) {
  // Model-availability probe is doc-independent and provider-diagnostic; route
  // it to the C++ core whenever the DLL loads (the host picker falls back to a
  // static catalog if the core is unavailable).
  if (method == "agent_list_models") {
    std::string lerr;
    return engine::ensureLoaded(lerr);
  }
  if (!(method == "agent_run_v2" || method == "agent_ask_v2" ||
        method == "agent_clarify_v1" || method == "agent_pm_assist_v1" ||
        method == "agent_simulate_v1" || method == "agent_mission_v1" ||
        method == "agent_cancel" || method == "agent_steer" ||
        method == "agent_answer" ||
        method == "get_prompts_v2"))
    return false; // everything else stays Python
  if (!agentDocIsCpp(method, params))
    return false; // only C++-backed docs (or doc-indep)
  // Ensure the core DLL is loadable before committing to the C++ path.
  // ensureLoaded is idempotent (loads once); if it can't load, fall through to
  // the Python worker so the agent never hard-breaks. For a doc-cpp doc the DLL
  // is already loaded (it minted the doc); for get_prompts_v2 this triggers the
  // lazy load on first use.
  std::string lerr;
  return engine::ensureLoaded(lerr);
}
// --- C++ SIM (FEA/flow) routing ---------------------------------------------
// Route the host's simulation RPCs (sim_mesh / sim_run_static / sim_run_adaptive /
// sim_run_modal / sim_run_fatigue / sim_run_thermal / sim_run_flow / sim_cancel) to the
// pure-C++ core (libchudware); FEA and flow run on the C++ core only
// (R5a/T-RF21). sim_cancel is C++-dispatched in core (T-RF16,
// core/sim/sim_cancel.cpp).
//
// The host's send-shape for sim_run_static/sim_run_modal is already the C++
// contract
// ({study:{step_path, density, material, fixtures, loads}} + n_modes for
// modal), so no params translation is needed here. The C++ result carries
// summary + overlay_aicm, which is exactly what sim_panel consumes (it never
// reads results_npz/solve_time_s, the two fields the C++ result omits) — so the
// result is byte-compatible for display.

// Native-only simulation RPCs: libchudware is their sole implementation (no
// services-worker/CalculiX/Python counterpart exists). Claim them even when the
// engine cannot load, so an unavailable engine surfaces as an error instead of
// a proxy to an unrelated services method of the same name.
bool isNativeOnlySimMethod(const std::string &method) {
  return method == "sim_run_buckling" || method == "sim_run_topology" ||
         method == "topology_capabilities" || method == "sim_netlist_to_circuit";
}

bool isTopologyMethod(const std::string &method) {
  return method == "sim_run_topology" || method == "topology_capabilities";
}

bool routeSimToCpp(const std::string &method, const json & /*params*/) {
  if (isNativeOnlySimMethod(method))
    return true;
  if (!(method == "sim_mesh" || method == "sim_run_static" ||
        method == "sim_run_adaptive" || method == "sim_run_modal" ||
        method == "sim_run_fatigue" ||
        method == "sim_run_thermal" || method == "sim_run_flow" ||
        method == "sim_run_pipe_flow" ||
        method == "sim_cancel"))
    return false; // sim_caps + everything else stays Python (services worker)
  if (!engine::available()) {
    // Lazily try to load the core DLL; if it can't load, fall through to Python
    // so sim never hard-breaks. ensureLoaded is idempotent (loads once).
    std::string lerr;
    if (!engine::ensureLoaded(lerr))
      return false;
  }
  return true;
}
// --- C++ ECAD routing -------------------------------------------------------
// ECAD document sessions live in the C++ core and mint "ecadcpp..." ids
// (core/ecad/session.cpp new_doc_id()). The id prefix binds implemented,
// document-scoped RPCs to that core session for the document's lifetime.
//
// The deleted Python ECAD worker and the live Python MCP tool surface are
// different backends. libchudware must claim only the ecad_* methods in its
// dispatch table; every other name must fall through to the generic hostd/MCP
// bridge. Previously the doc-id branch treated an "ecadcpp" prefix alone as
// sufficient and over-claimed every ecad_* method. That sent MCP-only tools such
// as ecad_thermal_handoff to libchudware's unknown-method fallthrough before the
// MCP bridge could serve them. Keep both the document-bound and documentless
// allowlists synchronized with the libchudware ECAD dispatch region.
bool cppEcadFlag() {
  // R5a (T-RF21): the CHUDWARE_CPP_ECAD escape hatch is removed — the C++ core is
  // the only ECAD backend on every platform (the Python ECAD worker is deleted
  // at closeout; Windows' Python default goes with it).
  return true;
}

bool routeEcadToCpp(const std::string &method, const json &params) {
  if (method.rfind("ecad_", 0) != 0)
    return false; // only ecad_* methods

  static const char *const kDocBound[] = {
      "ecad_apply_ops",       "ecad_undo",          "ecad_redo",
      "ecad_tree",            "ecad_rebuild",       "ecad_swap_doc",
      "ecad_close",           "ecad_describe",      "ecad_resolve_pick",
      "ecad_dfm",             "ecad_autoroute",     "ecad_route_diff_pair",
      "ecad_fill_zones",      "ecad_simulate",      "ecad_to_3d",
      "ecad_export",          "ecad_save",          "ecad_update_from_sch"};
  static const char *const kDocless[] = {
      "ecad_new",             "ecad_open",          "ecad_design",
      "ecad_symbols",         "ecad_load_symbol_library",
      "ecad_schema",          "ecad_list_ops",      "ecad_recipe",
      "ecad_closest_recipe",  "ecad_impedance"};

  // An ecadcpp id binds a call to the core only when libchudware implements
  // that document-scoped method. MCP-only ecad_* tools also carry doc_id; the
  // old prefix-only predicate swallowed them before the generic MCP bridge.
  bool docBound = false;
  for (const char *m : kDocBound)
    if (method == m) {
      docBound = true;
      break;
    }
  if (docBound && params.is_object()) {
    // board_doc_id/sch_doc_id are used by ecad_update_from_sch.
    for (const char *key : {"doc_id", "board_doc_id", "sch_doc_id"}) {
      auto it = params.find(key);
      if (it != params.end() && it->is_string() &&
          it->get<std::string>().rfind("ecadcpp", 0) == 0)
        return true;
    }
  }

  // Minting and documentless methods have no ecadcpp id to bind on, so claim
  // only the names present in libchudware. All other names deliberately fall
  // through to the live Python MCP tool surface.
  if (!cppEcadFlag())
    return false;
  bool claimed = false;
  for (const char *m : kDocless)
    if (method == m) {
      claimed = true;
      break;
    }
  if (!claimed)
    return false;
  if (!engine::available()) {
    std::string lerr;
    if (!engine::ensureLoaded(lerr))
      return false; // can't load -> Python
  }
  return true;
}
// --- C++ firmware routing (T-RF15) ------------------------------------------
// Route the ported fw_* methods to core/firmware (libchudware). DEFAULT ON on
// Linux/macOS (the platforms the C++ module is verified on);
// CHUDWARE_CPP_FIRMWARE overrides. The three emulator-leg methods (fw_sim_run /
// fw_power_profile / fw_sensor_scenario) stay Python until the Renode leg
// lands.
bool cppFirmwareFlag() {
  // R5a (T-RF21): the CHUDWARE_CPP_FIRMWARE escape hatch is removed —
  // core/firmware owns the fw_* surface (POSIX-verified; Windows path is
  // best-effort C++).
  return true;
}

bool routeFirmwareToCpp(const std::string &method, const json & /*params*/) {
  static const char *const kPorted[] = {
      "fw_new_project",     "fw_build",          "fw_sources",
      "fw_verify_board",    "fw_flash",          "fw_debug_start",
      "fw_debug_halt",      "fw_debug_read_mem", "fw_status",
      "fw_projects",        "fw_sim_run",        "fw_power_profile",
      "fw_sensor_scenario", // T-RF15 emulator leg
  };
  if (!cppFirmwareFlag())
    return false;
  bool ported = false;
  for (const char *p : kPorted)
    if (method == p) {
      ported = true;
      break;
    }
  if (!ported)
    return false; // unknown fw_* stays Python
  if (!engine::available()) {
    std::string lerr;
    if (!engine::ensureLoaded(lerr))
      return false; // can't load -> Python
  }
  return true;
}
#ifdef _WIN32
std::wstring widen(const std::string &s) {
  if (s.empty())
    return L"";
  int n = MultiByteToWideChar(CP_UTF8, 0, s.c_str(), (int)s.size(), nullptr, 0);
  std::wstring w(n, 0);
  MultiByteToWideChar(CP_UTF8, 0, s.c_str(), (int)s.size(), w.data(), n);
  return w;
}
bool fileExists(const std::string &p) {
  DWORD a = GetFileAttributesA(p.c_str());
  return a != INVALID_FILE_ATTRIBUTES && !(a & FILE_ATTRIBUTE_DIRECTORY);
}
std::string win32Error(DWORD code) {
  char *msg = nullptr;
  DWORD n = FormatMessageA(
      FORMAT_MESSAGE_ALLOCATE_BUFFER | FORMAT_MESSAGE_FROM_SYSTEM |
          FORMAT_MESSAGE_IGNORE_INSERTS,
      nullptr, code, MAKELANGID(LANG_NEUTRAL, SUBLANG_DEFAULT), (char *)&msg, 0,
      nullptr);
  std::string s =
      (n && msg) ? std::string(msg, n) : ("error " + std::to_string(code));
  if (msg)
    LocalFree(msg);
  while (!s.empty() &&
         (s.back() == '\n' || s.back() == '\r' || s.back() == '.'))
    s.pop_back();
  return s;
}

// CreateProcessW only reads a *wide* environment block when it is also given
// CREATE_UNICODE_ENVIRONMENT — the two go together or the child sees garbage.
// Copy the parent block, overriding (or inserting) one NAME=VALUE, and return a
// double-NUL-terminated wchar_t buffer suitable for lpEnvironment.
std::vector<wchar_t> childEnvBlockWith(const std::wstring &name,
                                       const std::wstring &value) {
  std::vector<wchar_t> out;
  const std::wstring prefix = name + L"=";
  wchar_t *parent = GetEnvironmentStringsW();
  if (parent) {
    for (wchar_t *p = parent; *p;) {
      const std::wstring entry(p);
      p += entry.size() + 1;
      // Drop any existing binding for `name` (Windows env names are
      // case-insensitive) so ours is the only one. Everything else, including
      // cmd's "=C:=..." per-drive entries, is copied verbatim.
      if (entry.size() >= prefix.size() &&
          _wcsnicmp(entry.c_str(), prefix.c_str(), prefix.size()) == 0)
        continue;
      out.insert(out.end(), entry.begin(), entry.end());
      out.push_back(0);
    }
    FreeEnvironmentStringsW(parent);
  }
  const std::wstring ours = prefix + value;
  out.insert(out.end(), ours.begin(), ours.end());
  out.push_back(0);
  out.push_back(0); // block terminator
  return out;
}
#else
bool fileExists(const std::string &p) {
  std::error_code ec;
  return std::filesystem::is_regular_file(p, ec);
}

#endif
#ifndef __EMSCRIPTEN__
std::string adjacentWorkerBinary(std::string &err) {
  const std::string executable = chudware::platform::executablePath();
  if (executable.empty()) {
    err = "Could not resolve the current executable path";
    return {};
  }
#ifdef _WIN32
  constexpr const char *name = "chudware-worker.exe";
#else
  constexpr const char *name = "chudware-worker";
#endif
  std::error_code ec;
  const std::filesystem::path candidate =
      std::filesystem::weakly_canonical(std::filesystem::path(executable).parent_path() / name, ec);
  if (ec) {
    err = "Could not canonicalize adjacent worker path";
    return {};
  }
  return candidate.string();
}

bool sameCanonicalPath(const std::string &left, const std::string &right) {
  std::error_code leftEc, rightEc;
  const auto canonicalLeft = std::filesystem::weakly_canonical(left, leftEc);
  const auto canonicalRight = std::filesystem::weakly_canonical(right, rightEc);
  return !leftEc && !rightEc && canonicalLeft == canonicalRight;
}
#if !defined(__EMSCRIPTEN__)
#ifndef _WIN32
using WorkerControlSocket = int;
constexpr WorkerControlSocket kInvalidWorkerControlSocket = -1;
void closeWorkerControlSocket(WorkerControlSocket socket) {
  if (socket != kInvalidWorkerControlSocket)
    ::close(socket);
}
int workerControlSocketError() { return errno; }
bool workerControlConnectPending(int error) {
  return error == EINPROGRESS || error == EAGAIN || error == EWOULDBLOCK;
}
bool workerControlWouldBlock(int error) {
  return error == EAGAIN || error == EWOULDBLOCK;
}
bool workerControlInterrupted(int error) { return error == EINTR; }
bool setWorkerControlNonblocking(WorkerControlSocket socket) {
  const int flags = fcntl(socket, F_GETFL, 0);
  const int descriptorFlags = fcntl(socket, F_GETFD, 0);
  return flags >= 0 && descriptorFlags >= 0 &&
         fcntl(socket, F_SETFL, flags | O_NONBLOCK) == 0 &&
         fcntl(socket, F_SETFD, descriptorFlags | FD_CLOEXEC) == 0;
}
bool ensureWorkerControlSockets(std::string &) { return true; }

class WorkerControlSocketHandle {
public:
  explicit WorkerControlSocketHandle(WorkerControlSocket socket)
      : socket_(socket) {}
  ~WorkerControlSocketHandle() { closeWorkerControlSocket(socket_); }
  WorkerControlSocketHandle(const WorkerControlSocketHandle &) = delete;
  WorkerControlSocketHandle &
  operator=(const WorkerControlSocketHandle &) = delete;
  WorkerControlSocket get() const { return socket_; }
  WorkerControlSocket release() {
    const WorkerControlSocket released = socket_;
    socket_ = kInvalidWorkerControlSocket;
    return released;
  }

private:
  WorkerControlSocket socket_;
};

using WorkerControlDeadline = std::chrono::steady_clock::time_point;
int workerControlRemainingMs(const WorkerControlDeadline &deadline) {
  const auto remaining = std::chrono::duration_cast<std::chrono::milliseconds>(
                             deadline - std::chrono::steady_clock::now())
                             .count();
  if (remaining <= 0)
    return 0;
  return remaining > 0x7fffffff ? 0x7fffffff : static_cast<int>(remaining);
}

bool waitWorkerControlSocket(WorkerControlSocket socket, bool writable,
                             const WorkerControlDeadline &deadline,
                             std::string &err) {
  for (;;) {
    const int timeoutMs = workerControlRemainingMs(deadline);
    if (timeoutMs <= 0) {
      err = "worker control request timed out";
      return false;
    }
    pollfd descriptor{};
    descriptor.fd = socket;
    descriptor.events = writable ? POLLOUT : POLLIN;
    const int ready = ::poll(&descriptor, 1, timeoutMs);
    if (ready > 0)
      return true;
    if (ready == 0) {
      err = "worker control request timed out";
      return false;
    }
    if (!workerControlInterrupted(workerControlSocketError())) {
      err = "worker control socket wait failed";
      return false;
    }
  }
}

bool connectWorkerControlSocket(
    const chudware::worker::detail::ControlInfo &info,
    const WorkerControlDeadline &deadline, WorkerControlSocket &connected,
    std::string &err) {
  if (!ensureWorkerControlSockets(err))
    return false;
  if (info.transport != chudware::worker::detail::ControlTransport::Unix) {
    err = "worker control transport is unavailable";
    return false;
  }
#if defined(SOCK_CLOEXEC)
  WorkerControlSocket socket = ::socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
#else
  WorkerControlSocket socket = ::socket(AF_UNIX, SOCK_STREAM, 0);
#endif
  if (socket == kInvalidWorkerControlSocket) {
    err = "worker control socket creation failed";
    return false;
  }
  WorkerControlSocketHandle guard(socket);
#if defined(__APPLE__)
  int noSigPipe = 1;
  if (setsockopt(socket, SOL_SOCKET, SO_NOSIGPIPE, &noSigPipe,
                 sizeof(noSigPipe)) != 0) {
    err = "worker control socket setup failed";
    return false;
  }
#endif
  if (!setWorkerControlNonblocking(socket)) {
    err = "worker control socket setup failed";
    return false;
  }
  sockaddr_un address{};
  address.sun_family = AF_UNIX;
  std::memcpy(address.sun_path, info.endpoint.c_str(), info.endpoint.size() + 1);
  const int status =
      ::connect(socket, reinterpret_cast<sockaddr *>(&address), sizeof(address));
  if (status != 0) {
    const int connectError = workerControlSocketError();
    if (!workerControlConnectPending(connectError) ||
        !waitWorkerControlSocket(socket, true, deadline, err))
      return false;
    int completionError = 0;
    socklen_t completionSize = sizeof(completionError);
    if (getsockopt(socket, SOL_SOCKET, SO_ERROR,
                   reinterpret_cast<char *>(&completionError),
                   &completionSize) != 0 ||
        completionError != 0) {
      err = "worker control connection failed";
      return false;
    }
  }
  connected = guard.release();
  return true;
}
#endif

bool exchangeWorkerControl(
    const chudware::worker::detail::ControlInfo &info, std::string &request,
    std::string &response, std::string &err) {
#ifdef _WIN32
  if (info.transport != chudware::worker::detail::ControlTransport::NamedPipe) {
    err = "worker control transport is unavailable";
    return false;
  }
  const bool ok = chudware_ipc::exchangeLocalPipe(
      info.endpoint, request, response,
      chudware::worker::detail::kControlResponseLimit,
      chudware::worker::detail::kControlTimeoutMs, err);
  chudware::worker::detail::secureClear(request);
  return ok;
#else
  const auto deadline = std::chrono::steady_clock::now() +
                        std::chrono::milliseconds(
                            chudware::worker::detail::kControlTimeoutMs);
  WorkerControlSocket socket = kInvalidWorkerControlSocket;
  if (!connectWorkerControlSocket(info, deadline, socket, err))
    return false;
  WorkerControlSocketHandle guard(socket);
  std::size_t sent = 0;
  while (sent < request.size()) {
    if (!waitWorkerControlSocket(socket, true, deadline, err))
      return false;
#ifdef MSG_NOSIGNAL
    const ssize_t wrote =
        ::send(socket, request.data() + sent, request.size() - sent,
               MSG_NOSIGNAL);
#else
    const ssize_t wrote =
        ::send(socket, request.data() + sent, request.size() - sent, 0);
#endif
    if (wrote > 0) {
      sent += static_cast<std::size_t>(wrote);
      continue;
    }
    const int sendError = workerControlSocketError();
    if (workerControlInterrupted(sendError) ||
        workerControlWouldBlock(sendError))
      continue;
    err = "worker control send failed";
    return false;
  }
  chudware::worker::detail::secureClear(request);

  response.clear();
  char chunk[4096];
  for (;;) {
    if (!waitWorkerControlSocket(socket, false, deadline, err))
      return false;
    const ssize_t received = ::recv(socket, chunk, sizeof(chunk), 0);
    if (received > 0) {
      const char *newline = static_cast<const char *>(
          std::memchr(chunk, '\n', static_cast<std::size_t>(received)));
      const std::size_t count =
          newline ? static_cast<std::size_t>(newline - chunk)
                  : static_cast<std::size_t>(received);
      if (response.size() + count >
          chudware::worker::detail::kControlResponseLimit) {
        err = "worker control response exceeded limit";
        return false;
      }
      response.append(chunk, count);
      if (newline)
        return true;
      continue;
    }
    if (received == 0) {
      err = "worker control connection closed";
      return false;
    }
    const int receiveError = workerControlSocketError();
    if (workerControlInterrupted(receiveError) ||
        workerControlWouldBlock(receiveError))
      continue;
    err = "worker control receive failed";
    return false;
  }
#endif
}
#endif

#endif
class ActiveWorkerRunMarker {
public:
  ActiveWorkerRunMarker(std::mutex &mutex, std::string &activeRunId,
                        std::string runId, bool engaged)
      : mutex_(mutex), activeRunId_(activeRunId), runId_(std::move(runId)),
        engaged_(engaged) {
    if (engaged_) {
      std::lock_guard<std::mutex> lock(mutex_);
      activeRunId_ = runId_;
    }
  }
  ~ActiveWorkerRunMarker() {
    if (engaged_) {
      std::lock_guard<std::mutex> lock(mutex_);
      if (activeRunId_ == runId_)
        activeRunId_.clear();
    }
  }
  ActiveWorkerRunMarker(const ActiveWorkerRunMarker &) = delete;
  ActiveWorkerRunMarker &operator=(const ActiveWorkerRunMarker &) = delete;

private:
  std::mutex &mutex_;
  std::string &activeRunId_;
  std::string runId_;
  bool engaged_;
};

} // namespace
void WorkerClient::clearWorkerControlInfo() {
  std::lock_guard<std::mutex> lock(workerControlMtx_);
  chudware::worker::detail::secureClear(workerControl_.token);
  workerControl_ = {};
}

void WorkerClient::clearWorkerControlState() {
  std::lock_guard<std::mutex> lock(workerControlMtx_);
  chudware::worker::detail::secureClear(workerControl_.token);
  workerControl_ = {};
  activeWorkerRunId_.clear();
}

bool WorkerClient::installWorkerControlInfo(json &info, std::string &err) {
  WorkerControlInfo parsed;
#ifdef _WIN32
  constexpr bool windows = true;
#else
  constexpr bool windows = false;
#endif
  if (!chudware::worker::detail::parseControlInfo(info, windows, parsed, err)) {
    auto token = info.is_object() ? info.find("token") : info.end();
    if (info.is_object() && token != info.end() && token->is_string())
      chudware::worker::detail::secureClear(
          token->get_ref<std::string &>());
    return false;
  }
  std::lock_guard<std::mutex> lock(workerControlMtx_);
  chudware::worker::detail::secureClear(workerControl_.token);
  workerControl_ = std::move(parsed);
  return true;
}

bool WorkerClient::ensureWorkerControlInfo(std::string &err) {
  {
    std::lock_guard<std::mutex> lock(workerControlMtx_);
    if (workerControl_.transport !=
            chudware::worker::detail::ControlTransport::None &&
        !workerControl_.endpoint.empty() && !workerControl_.token.empty())
      return true;
  }
  json info;
  if (!callWorkerPipe("worker_control_info", json::object(), info, err, 5000,
                      {}))
    return false;
  if (!installWorkerControlInfo(info, err)) {
    clearWorkerControlInfo();
    return false;
  }
  return true;
}

bool WorkerClient::callWorkerControl(const std::string &action,
                                     const std::string &runId,
                                     const json &params, bool &matched,
                                     json &result, std::string &err) {
  matched = false;
  // Daemon turns belong to hostd's worker pool, not this local pipe endpoint.
  if (engine::daemonMode())
    return false;
  WorkerControlInfo info;
  {
    std::lock_guard<std::mutex> lock(workerControlMtx_);
    if (!chudware::worker::detail::matchesActiveRun(activeWorkerRunId_, runId))
      return false;
    matched = true;
    if (workerControl_.transport ==
            chudware::worker::detail::ControlTransport::None ||
        workerControl_.endpoint.empty() || workerControl_.token.empty()) {
      err = "worker control endpoint unavailable for active run";
      return false;
    }
    info = workerControl_;
  }

  std::string request;
  struct SecretGuard {
    std::string &token;
    std::string &frame;
    ~SecretGuard() {
      chudware::worker::detail::secureClear(token);
      chudware::worker::detail::secureClear(frame);
    }
  } secrets{info.token, request};
  if (!chudware::worker::detail::buildControlFrame(info.token, action, params,
                                                   request, err))
    return false;
#ifdef __EMSCRIPTEN__
  err = "worker control transport is unavailable";
  return false;
#else
  std::string response;
  if (exchangeWorkerControl(info, request, response, err) &&
      chudware::worker::detail::parseControlResponse(response, info.token,
                                                     result, err))
    return true;

  // Private connections are independent of the worker's endpoint lifetime. A
  // refusal, timeout, or malformed/closed response does not prove replacement.
  // Keep the endpoint until worker teardown so the next answer/Stop can use it
  // without fetching control info behind the active turn on stdio.
  return false;
#endif
}


WorkerClient::~WorkerClient() { stop(); }

namespace {
bool allowWorkerSpawn(std::string& err) {
  const auto executable = chudware::platform::executablePath();
  const auto integrity = chudware::security::verifyInstalledPayload(
      executable, chudware::security::VerifyMode::ProtectedOperation);
  if (!integrity.ok) {
    err = "worker spawn blocked: protected_file_hash_mismatch";
    return false;
  }
  const auto decision = chudware::security::inspectAndPersistRuntime(
      chudware::security::SecurityCheckpoint::BeforeWorkerSpawn,
      integrity.root, integrity.files, chudware::security::runtimeGuardStateDir().string());
  if (decision.allowed()) return true;
  err = "worker spawn blocked: " + decision.denialReason;
  return false;
}
}  // namespace
bool WorkerClient::start(std::string &err, const std::string &module) {
  clearWorkerControlState();
#ifdef __EMSCRIPTEN__
  (void)module;
  err = "WASM: Python worker not available (daemon mode only)";
  return false;
#else
  if (!allowWorkerSpawn(err)) return false;
  std::string repo;
  std::string launcher;
  bool compiledWorker = false;
#ifdef CHUDWARE_DEVELOPER_BUILD
  if (const char *env = std::getenv("CHUDWARE_WORKER_BIN"); env && *env) {
    if (module != "services.worker_main") {
      err = "Compiled worker does not provide module: " + module;
      return false;
    }
    std::error_code ec;
    launcher = std::filesystem::weakly_canonical(env, ec).string();
    if (ec) launcher = env;
    compiledWorker = true;
  } else {
    launcher = runtimePython();
  }
  repo = appDir();
#else
  if (module != "services.worker_main") {
    err = "Packaged worker does not provide module: " + module;
    return false;
  }
  launcher = adjacentWorkerBinary(err);
  if (launcher.empty()) return false;
  if (const char *env = std::getenv("CHUDWARE_WORKER_BIN"); env && *env &&
      !sameCanonicalPath(env, launcher)) {
    err = "CHUDWARE_WORKER_BIN must name the adjacent packaged worker";
    return false;
  }
  repo = std::filesystem::path(launcher).parent_path().string();
  compiledWorker = true;
#endif
  readBuf_.clear(); // fresh process => fresh stream (matters when restarting
                    // after a repair)

  if (!fileExists(launcher)) {
    err = compiledWorker ? "Compiled worker not found: " + launcher
                         : "Python runtime not installed.\nExpected: " + launcher +
                               "\nUse Chudware \xe2\x96\xb8 Repair runtime to install it.";
    return false;
  }

  // Pin the child to the SAME engine backend this process resolved, always
  // explicitly. The two sides default DIFFERENTLY: engine_backend falls back to
  // the in-process libchudware when hostd is unreachable, while the Python
  // core_bridge treats an unset CHUDWARE_ENGINE_DAEMON as daemon mode
  // unconditionally. Leaving it unset is exactly how the GUI ended up on its
  // private in-process core while the agent worker it had just spawned talked to
  // hostd: every document the agent built then lived in an engine the GUI could
  // not see — no doc tab, no viewport geometry, nothing in the AI chat's
  // Artifacts, which instead showed only what the user had opened locally.
  const char *enginePin = engine::daemonMode() ? "1" : "0";

#ifdef _WIN32
  SECURITY_ATTRIBUTES sa{};
  sa.nLength = sizeof(sa);
  sa.bInheritHandle = TRUE;

  HANDLE outRd = nullptr, outWr = nullptr, inRd = nullptr, inWr = nullptr;
  if (!CreatePipe(&outRd, &outWr, &sa, 0) ||
      !CreatePipe(&inRd, &inWr, &sa, 0)) {
    err = "CreatePipe failed";
    return false;
  }
  // The parent's ends must not be inherited by the child.
  SetHandleInformation(outRd, HANDLE_FLAG_INHERIT, 0);
  SetHandleInformation(inWr, HANDLE_FLAG_INHERIT, 0);

  STARTUPINFOW si{};
  si.cb = sizeof(si);
  si.dwFlags = STARTF_USESTDHANDLES;
  si.hStdInput = inRd;
  si.hStdOutput = outWr;
  si.hStdError =
      GetStdHandle(STD_ERROR_HANDLE); // worker diagnostics -> host console

  std::wstring cmd = L"\"" + widen(launcher) + L"\"";
  if (compiledWorker)
    cmd += L" --module worker";
  else
    cmd += L" -m " + widen(module);
  std::wstring cwd = widen(repo);
  std::vector<wchar_t> cmdbuf(cmd.begin(), cmd.end());
  cmdbuf.push_back(0);

  // Explicit wide env block (see childEnvBlockWith): lpEnvironment must be
  // paired with CREATE_UNICODE_ENVIRONMENT, otherwise CreateProcessW reads the
  // wide bytes as ANSI.
  std::vector<wchar_t> envBlock =
      childEnvBlockWith(L"CHUDWARE_ENGINE_DAEMON", widen(enginePin));

  PROCESS_INFORMATION pi{};
  BOOL ok = CreateProcessW(nullptr, cmdbuf.data(), nullptr, nullptr, TRUE,
                           CREATE_NO_WINDOW | CREATE_UNICODE_ENVIRONMENT,
                           envBlock.data(),
                           cwd.empty() ? nullptr : cwd.c_str(), &si, &pi);
  DWORD gle = GetLastError();
  CloseHandle(outWr);
  CloseHandle(inRd);
  if (!ok) {
    err = "Could not launch worker (" + win32Error(gle) + ").\n" + launcher;
    CloseHandle(outRd);
    CloseHandle(inWr);
    return false;
  }
  CloseHandle(pi.hThread);
  process_ = pi.hProcess;
  childStdoutRd_ = outRd;
  childStdinWr_ = inWr;
  running_ = true;
  workerModule_ = module;
  poisoned_ = false;
  return true;
#else
  // POSIX: outPipe = child stdout -> parent; inPipe = parent -> child stdin.
  int outPipe[2], inPipe[2];
  if (pipe(outPipe) != 0 || pipe(inPipe) != 0) {
    err = std::string("pipe() failed: ") + std::strerror(errno);
    return false;
  }
  pid_t pid = fork();
  if (pid < 0) {
    err = std::string("fork failed: ") + std::strerror(errno);
    ::close(outPipe[0]);
    ::close(outPipe[1]);
    ::close(inPipe[0]);
    ::close(inPipe[1]);
    return false;
  }
  if (pid == 0) {
    // child: stdin <- inPipe[0], stdout -> outPipe[1]; stderr stays (host
    // console).
    dup2(inPipe[0], STDIN_FILENO);
    dup2(outPipe[1], STDOUT_FILENO);
    ::close(inPipe[0]);
    ::close(inPipe[1]);
    ::close(outPipe[0]);
    ::close(outPipe[1]);
    if (!repo.empty() &&
        chdir(repo.c_str()) != 0) { /* fall through; exec from cwd */
    }
    // Single-threaded child, post-fork: safe to mutate the environment here,
    // and doing it here (rather than in the parent) keeps the override out of
    // the parent's own later getenv() decisions.
    setenv("CHUDWARE_ENGINE_DAEMON", enginePin, 1);
    if (compiledWorker)
      execlp(launcher.c_str(), launcher.c_str(), "--module", "worker", (char *)nullptr);
    else
      execlp(launcher.c_str(), launcher.c_str(), "-m", module.c_str(), (char *)nullptr);
    _exit(127); // exec failed
  }
  // parent: keep our ends, close the child's.
  ::close(inPipe[0]);
  ::close(outPipe[1]);
  fcntl(outPipe[0], F_SETFL,
        O_NONBLOCK); // non-blocking reads so readLine can poll
  process_ = pid;
  childStdoutRd_ = outPipe[0];
  childStdinWr_ = inPipe[1];
  running_ = true;
  workerModule_ = module;
  poisoned_ = false;
  return true;
#endif // _WIN32
#endif // __EMSCRIPTEN__
}

void WorkerClient::stop() {
  clearWorkerControlState();
#ifdef __EMSCRIPTEN__
  running_ = false;
  return;
#elif defined(_WIN32)
  if (childStdinWr_) {
    DWORD wrote = 0;
    const char *bye = "{\"id\":0,\"method\":\"shutdown\"}\n";
    WriteFile(childStdinWr_, bye, (DWORD)strlen(bye), &wrote, nullptr);
    CloseHandle(childStdinWr_);
    childStdinWr_ = nullptr;
  }
  if (process_) {
    WaitForSingleObject(process_, 2000);
    TerminateProcess(process_, 0);
    CloseHandle(process_);
    process_ = nullptr;
  }
  if (childStdoutRd_) {
    CloseHandle(childStdoutRd_);
    childStdoutRd_ = nullptr;
  }
#else
  if (childStdinWr_ >= 0) {
    const char *bye = "{\"id\":0,\"method\":\"shutdown\"}\n";
    ssize_t w = ::write(childStdinWr_, bye, std::strlen(bye));
    (void)w;
    ::close(childStdinWr_);
    childStdinWr_ = -1;
  }
  if (process_ >= 0) {
    // give the worker ~2s to exit on its own, then SIGTERM and reap.
    bool reaped = false;
    for (int i = 0; i < 200; ++i) {
      int status = 0;
      pid_t r = waitpid(process_, &status, WNOHANG);
      if (r == process_ || (r < 0 && errno == ECHILD)) {
        reaped = true;
        break;
      }
      std::this_thread::sleep_for(std::chrono::milliseconds(10));
    }
    if (!reaped) {
      ::kill(process_, SIGTERM);
      waitpid(process_, nullptr, 0);
    }
    process_ = -1;
  }
  if (childStdoutRd_ >= 0) {
    ::close(childStdoutRd_);
    childStdoutRd_ = -1;
  }
#endif
  running_ = false;
  poisoned_ = false;
}

bool WorkerClient::terminatePoisonedWorker(std::string &err) {
  clearWorkerControlState();
  bool terminated = true;
#ifdef __EMSCRIPTEN__
  running_ = false;
#elif defined(_WIN32)
  // A timed-out request may still be mutating state. Do not enqueue the normal
  // graceful shutdown behind it: break the poisoned stream and terminate now.
  if (childStdinWr_) {
    CloseHandle(childStdinWr_);
    childStdinWr_ = nullptr;
  }
  if (process_) {
    if (WaitForSingleObject(process_, 0) != WAIT_OBJECT_0) {
      if (!TerminateProcess(process_, 1)) {
        err = "could not terminate timed-out worker (" +
              win32Error(GetLastError()) + ")";
        terminated = false;
      } else if (WaitForSingleObject(process_, 2000) != WAIT_OBJECT_0) {
        err = "timed-out worker did not terminate within 2 seconds";
        terminated = false;
      }
    }
    if (terminated) {
      CloseHandle(process_);
      process_ = nullptr;
    }
  }
  if (childStdoutRd_) {
    CloseHandle(childStdoutRd_);
    childStdoutRd_ = nullptr;
  }
#else
  // Closing stdin prevents another request from entering the poisoned stream.
  // SIGTERM normally stops the Python worker immediately; bound that wait and
  // escalate so a stuck handler cannot outlive its timed-out caller.
  if (childStdinWr_ >= 0) {
    ::close(childStdinWr_);
    childStdinWr_ = -1;
  }
  if (process_ >= 0) {
    bool reaped = false;
    if (::kill(process_, SIGTERM) != 0 && errno != ESRCH) {
      err = std::string("could not terminate timed-out worker: ") +
            std::strerror(errno);
      terminated = false;
    }
    if (terminated) {
      for (int i = 0; i < 25; ++i) {
        int status = 0;
        pid_t r = waitpid(process_, &status, WNOHANG);
        if (r == process_ || (r < 0 && errno == ECHILD)) {
          reaped = true;
          break;
        }
        if (r < 0 && errno != EINTR) {
          err = std::string("could not reap timed-out worker: ") +
                std::strerror(errno);
          terminated = false;
          break;
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
      }
    }
    if (terminated && !reaped) {
      if (::kill(process_, SIGKILL) != 0 && errno != ESRCH) {
        err = std::string("could not kill timed-out worker: ") +
              std::strerror(errno);
        terminated = false;
      } else {
        for (;;) {
          pid_t r = waitpid(process_, nullptr, 0);
          if (r == process_ || (r < 0 && errno == ECHILD)) {
            reaped = true;
            break;
          }
          if (r < 0 && errno == EINTR)
            continue;
          err = std::string("could not reap killed worker: ") +
                std::strerror(errno);
          terminated = false;
          break;
        }
      }
    }
    if (terminated && reaped)
      process_ = -1;
  }
  if (childStdoutRd_ >= 0) {
    ::close(childStdoutRd_);
    childStdoutRd_ = -1;
  }
#endif
  readBuf_.clear();
  running_ = false;
  return terminated;
}

bool WorkerClient::restartPoisonedWorker(std::string &err) {
  if (!poisoned_)
    return true;
  if (!terminatePoisonedWorker(err))
    return false;
  if (abort_.load()) {
    err = "aborted";
    return false;
  }
  const std::string module = workerModule_;
  if (!start(err, module))
    return false;
  poisoned_ = false;
  return true;
}

void WorkerClient::abortPending() {
  abort_.store(true);
  // Wake a blocked daemon agent_run so AsyncRpc::shutdown / app quit cannot
  // hang on a multi-minute modeling call. In-proc path still relies on
  // agent_cancel (libchudware cooperative flag) which requestStopRun already
  // fired.
  if (engine::daemonMode()) {
    if (auto *c = engine::client())
      c->abortPending();
    // The long agent/mission run now streams on its own dedicated connection
    // (engine_backend runClient) — abort that too, or a blocked run pins quit.
    engine::abortRunClient();
  }
}

WorkerClient::ReadLineResult WorkerClient::readLine(std::string &line,
                                                    int timeoutMs) {
#ifdef __EMSCRIPTEN__
  (void)line;
  (void)timeoutMs;
  return ReadLineResult::Closed;
#elif defined(_WIN32)
  ULONGLONG start = GetTickCount64();
  for (;;) {
    if (abort_.load())
      return ReadLineResult::Closed; // C39: teardown — stop polling immediately
    size_t nl = readBuf_.find('\n');
    if (nl != std::string::npos) {
      line = readBuf_.substr(0, nl);
      readBuf_.erase(0, nl + 1);
      return ReadLineResult::Line;
    }
    if (timeoutMs >= 0 && (int)(GetTickCount64() - start) > timeoutMs)
      return ReadLineResult::Timeout;

    DWORD avail = 0;
    if (!PeekNamedPipe(childStdoutRd_, nullptr, 0, nullptr, &avail, nullptr))
      return ReadLineResult::Closed;
    if (avail == 0) {
      if (WaitForSingleObject(process_, 0) == WAIT_OBJECT_0) {
        // process exited; drain whatever is buffered one last time
        if (!PeekNamedPipe(childStdoutRd_, nullptr, 0, nullptr, &avail,
                           nullptr) ||
            avail == 0)
          return ReadLineResult::Closed;
      } else {
        // W1-B (item 11): tighter poll. For the first ~2ms after the request
        // was written, yield with Sleep(0) (no enforced minimum sleep) so a
        // fast doc_* round-trip returns with sub-millisecond latency; past
        // that, fall back to Sleep(1) to avoid burning a core while waiting on
        // a slow call. Measurable win across the 77 RPC call sites with no
        // architectural change (still a synchronous busy-poll).
        ULONGLONG waited = GetTickCount64() - start;
        if (waited < 2)
          Sleep(0);
        else
          Sleep(1);
        continue;
      }
    }
    std::string chunk(avail, 0);
    DWORD got = 0;
    if (!ReadFile(childStdoutRd_, chunk.data(), avail, &got, nullptr) ||
        got == 0)
      return ReadLineResult::Closed;
    readBuf_.append(chunk.data(), got);
  }
#else
  auto start = std::chrono::steady_clock::now();
  for (;;) {
    if (abort_.load())
      return ReadLineResult::Closed; // C39: teardown — stop polling immediately
    size_t nl = readBuf_.find('\n');
    if (nl != std::string::npos) {
      line = readBuf_.substr(0, nl);
      readBuf_.erase(0, nl + 1);
      return ReadLineResult::Line;
    }
    auto elapsed = std::chrono::duration_cast<std::chrono::milliseconds>(
                       std::chrono::steady_clock::now() - start)
                       .count();
    if (timeoutMs >= 0 && (int)elapsed > timeoutMs)
      return ReadLineResult::Timeout;

    struct pollfd pfd;
    pfd.fd = childStdoutRd_;
    pfd.events = POLLIN;
    pfd.revents = 0;
    int pr =
        ::poll(&pfd, 1, 2); // 2ms tick (matches the Windows fast-poll cadence)
    if (pr < 0) {
      if (errno == EINTR)
        continue;
      return ReadLineResult::Closed;
    }
    if (pr == 0) { // no data this tick — detect a child exit, else keep polling
      int status = 0;
      pid_t r = (process_ >= 0) ? waitpid(process_, &status, WNOHANG) : -1;
      if (r == process_ || (r < 0 && errno == ECHILD)) {
        process_ = -1; // reaped here so stop() won't re-wait
        char chunk[4096];
        ssize_t got =
            ::read(childStdoutRd_, chunk, sizeof(chunk)); // final drain
        if (got > 0) {
          readBuf_.append(chunk, (size_t)got);
          continue;
        }
        return ReadLineResult::Closed;
      }
      continue;
    }
    char chunk[4096];
    ssize_t got = ::read(childStdoutRd_, chunk, sizeof(chunk));
    if (got > 0) {
      readBuf_.append(chunk, (size_t)got);
    } else if (got == 0) {
      return ReadLineResult::Closed; // EOF: pipe closed
    } else if (errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR) {
      return ReadLineResult::Closed;
    }
  }
#endif // platform
}

bool WorkerClient::liveDocJoined(const std::string &docId) const {
  std::shared_lock<std::shared_mutex> order(g_liveDispatchMtx);
  std::lock_guard<std::mutex> lock(g_liveRoutesMtx);
  return g_liveRoutes.find(docId) != g_liveRoutes.end();
}

void WorkerClient::clearLiveDocRoute(const std::string &docId) {
  std::unique_lock<std::shared_mutex> order(g_liveDispatchMtx);
  std::lock_guard<std::mutex> lock(g_liveRoutesMtx);
  g_liveRoutes.erase(docId);
}

void WorkerClient::clearLiveDocRoutes() {
  std::unique_lock<std::shared_mutex> order(g_liveDispatchMtx);
  std::lock_guard<std::mutex> lock(g_liveRoutesMtx);
  g_liveRoutes.clear();
}

bool WorkerClient::callWithLiveRouting(const std::string &method,
                                       const json &params, json &result,
                                       std::string &err, int timeoutMs,
                                       const EventFn &onEvent) {
  LiveRoute route;
  // Linearize route publication/removal with both synchronous calls and
  // AsyncRpc workers. Ordinary document calls may run concurrently; join and
  // leave wait for all earlier doc_apply_ops calls and publish their route
  // change before any later one can choose a backend.
  std::shared_lock<std::shared_mutex> docOrder;
  std::unique_lock<std::shared_mutex> lifecycleOrder;
  if (method == "doc_apply_ops")
    docOrder = std::shared_lock<std::shared_mutex>(g_liveDispatchMtx);
  else if (method == "plm_live_join" || method == "plm_live_leave")
    lifecycleOrder = std::unique_lock<std::shared_mutex>(g_liveDispatchMtx);

  if (method == "doc_apply_ops" && findLiveRoute(params, route)) {
    json proposalParams = {
        {"path", route.path},
        {"remote_id", route.remoteId},
        {"item_id", route.itemId},
        {"participant", route.participant},
        {"ops", nullptr},
    };
    if (params.is_object()) {
      const auto ops = params.find("ops");
      if (ops != params.end())
        proposalParams["ops"] = *ops;
    }

    json proposalResult;
    if (!callImpl("plm_live_propose", proposalParams, proposalResult, err,
                  timeoutMs, onEvent))
      return false;

    // A protocol-2 method error from the generic worker seam is already in the
    // shape DocMirror expects. A PLM contract refusal is a plain
    // {ok:false,reason,hint} envelope, so adapt it without ever falling through
    // to the local doc engine.
    if (proposalResult.is_object() && proposalResult.contains("code")) {
      result = std::move(proposalResult);
      err = result.value("message", std::string("live proposal failed"));
      return true;
    }
    if (!successfulLiveEnvelope(proposalResult)) {
      result = liveMethodError(proposalResult);
      err = result.value("message", std::string("live proposal failed"));
      return true;
    }

    const auto opsResponse = proposalResult.find("ops_response");
    if (opsResponse == proposalResult.end() || !opsResponse->is_object()) {
      result = json{{"code", "live_protocol_error"},
                    {"message",
                     "The live proposal response omitted its ops response."},
                    {"detail", proposalResult}};
      err = result["message"].get<std::string>();
      return true;
    }
    result = *opsResponse;
    return true;
  }

  const bool ok =
      callImpl(method, params, result, err, timeoutMs, onEvent);
  if (!ok)
    return false;
  if (method == "plm_live_join" && successfulLiveEnvelope(result))
    registerLiveRoute(params, result);
  else if (method == "plm_live_leave" && successfulLiveEnvelope(result))
    removeLiveRoutes(params);
  return true;
}

// T-G9 parity trace: with CHUDWARE_RPC_TRACE=<file> set, every call() appends one
// JSONL record {method, params, ok, result|err}. This is the single funnel all
// panel RPCs pass through regardless of routing (Python worker subprocess,
// in-process libchudware doc/CAM core, ECAD/agent C++ paths), so tools/gui_parity/
// can capture byte-comparable request/ response streams from BOTH GUI binaries
// (host links services/worker directly too). Env unset (the default, incl. all
// production runs) = the wrapper is a tail call.
bool WorkerClient::call(const std::string &method, const json &params,
                        json &result, std::string &err, int timeoutMs,
                        const EventFn &onEvent) {
  static const char *tracePath = std::getenv("CHUDWARE_RPC_TRACE");
  if (!tracePath || !*tracePath)
    return callWithLiveRouting(method, params, result, err, timeoutMs, onEvent);

  const bool ok =
      callWithLiveRouting(method, params, result, err, timeoutMs, onEvent);
  try {
    // Redact credential-bearing fields recursively (agent_list_models et al
    // carry api_key/base_url auth material) — trace files must never hold live
    // secrets.
    std::function<void(json &)> redact = [&](json &node) {
      if (node.is_object()) {
        for (auto &[k, v] : node.items()) {
          if (k == "api_key" || k == "apikey" || k == "token" ||
              k == "secret" || k == "password" || k == "authorization")
            v = "<redacted>";
          else
            redact(v);
        }
      } else if (node.is_array()) {
        for (auto &v : node)
          redact(v);
      }
    };
    json rec = {{"method", method}, {"params", params}, {"ok", ok}};
    if (ok)
      rec["result"] = result;
    else
      rec["err"] = err;
    redact(rec);
    static std::mutex
        traceMtx; // two instances (doc + sim worker) share the file
    std::lock_guard<std::mutex> tlk(traceMtx);
    FILE *f = std::fopen(tracePath, "ab");
    if (f) {
      const std::string line = rec.dump() + "\n";
      std::fwrite(line.data(), 1, line.size(), f);
      std::fclose(f);
    }
  } catch (...) { /* tracing must never break the app */
  }
  return ok;
}

bool WorkerClient::callAgentCancel(const std::string &runId, json &result,
                                   std::string &err, const std::string &provider) {
  json params{{"run_id", runId}};
  if (!provider.empty()) params["provider"] = provider;
  bool matched = false;
  if (callWorkerControl("cancel", runId, params, matched, result, err))
    return true;
  if (matched)
    return false;
  return call("agent_cancel", params, result, err, 5000);
}

bool WorkerClient::callAgentSteer(const std::string &runId,
                                  const std::string &text,
                                  const json &attachments,
                                  const std::string &steerId, json &result,
                                  std::string &err, const std::string &provider) {
  json params{{"run_id", runId}, {"text", text}};
  if (!provider.empty()) params["provider"] = provider;
  if (attachments.is_array() && !attachments.empty())
    params["attachments"] = attachments;
  if (!steerId.empty())
    params["steer_id"] = steerId;
  bool matched = false;
  if (callWorkerControl("steer", runId, params, matched, result, err))
    return true;
  if (matched)
    return false;
  return call("agent_steer", params, result, err, 5000);
}

bool WorkerClient::callAgentAnswer(const std::string &runId,
                                   const std::string &batchId,
                                   const json &answers, bool declined,
                                   json &result, std::string &err,
                                   const std::string &provider) {
  json params{{"run_id", runId}, {"batch_id", batchId}};
  if (!provider.empty()) params["provider"] = provider;
  if (declined)
    params["declined"] = true;
  else
    params["answers"] = answers.is_object() ? answers : json::object();
  bool matched = false;
  if (callWorkerControl("answer", runId, params, matched, result, err))
    return true;
  if (matched)
    return false;
  return call("agent_answer", params, result, err, 5000);
}

bool WorkerClient::callViaSideClient(const std::string &method,
                                     const json &params, json &result,
                                     std::string &err, int timeoutMs,
                                     const EventFn &onEvent) {
  if (abort_.load()) { // C39: app is tearing down — intentional, no errorSink
                       // toast
    err = "aborted";
    return false;
  }
  // Short-lived private connection to the daemon (no spawn: a call is already
  // in flight on the shared client, so hostd is up). Same pattern as the
  // agent_cancel/agent_steer/agent_answer side channel in callImpl below.
  // hostdBin hint is REQUIRED: an empty hint hashes "default" in instanceKey(),
  // which is a different socket than the running daemon's (hostd binds
  // instanceKey(selfPath())) — the connect then fails ENOENT mid-session.
  chudware_ipc::EngineClient side;
  chudware_ipc::EngineClient::Options opts;
  opts.clientKind = "gui";
  opts.hostdBin = engine::hostdBinHint();
  opts.allowSpawn = false;
  std::string cerr;
  if (!side.connectOrSpawn(opts, cerr)) {
    // The daemon died between the in-flight call and now — fail fast with a
    // clear error rather than queueing behind a run that will never return.
    err = cerr.empty() ? "engine daemon unreachable" : cerr;
    if (errorSink_)
      errorSink_(method, err);
    return false;
  }
  if (side.call(method, params, result, cerr, timeoutMs, onEvent))
    return true;
  // Same response contract as the engine branches in callImpl: a structured
  // method error round-trips in `result` and returns true (the RPC succeeded,
  // the METHOD reported an error); only transport failures return false.
  if (result.is_object() && !result.empty()) {
    err =
        result.value("message", cerr.empty() ? std::string("rpc error") : cerr);
    return true;
  }
  if (result.is_string()) {
    err = result.get<std::string>();
    return false;
  }
  err = cerr.empty() ? std::string("engine channel error") : cerr;
  if (errorSink_)
    errorSink_(method, err);
  return false;
}

bool WorkerClient::callImpl(const std::string &method, const json &params,
                            json &result, std::string &err, int timeoutMs,
                            const EventFn &onEvent) {
  // Agent control and an in-process C++ sim_cancel must reach the engine WHILE
  // their corresponding long call is blocked on this WorkerClient. The normal
  // callMtx_ serialization makes a cancellation queued behind the work it is
  // supposed to stop. Keep Python-worker sim_cancel serialized: its stdio pipe
  // cannot safely carry concurrent requests.
  const bool isAgentControl =
      (method == "agent_cancel" || method == "agent_steer" ||
       method == "agent_answer");
#ifdef __EMSCRIPTEN__
  // Browser controls must share the cell admission guard, not native side sockets.
  if (isAgentControl)
    return engine::call(method, params, result, err, timeoutMs, onEvent);
#endif
  const bool isCppSimCancel =
      (method == "sim_cancel" && routeSimToCpp(method, params));
  auto *sharedClient = engine::daemonMode() ? engine::client() : nullptr;
  // Document-free GUI background queries own a private EngineClient connection.
  // Holding the stdio/document mutex here would reintroduce the startup freeze
  // before the independent socket is reached (notably doc_new behind model probes).
  const bool backgroundQuery =
      sharedClient && sharedClient->usesBackgroundConnection(method, params);
  const bool daemonAgentTurn =
      method == "agent_admin_turn" && engine::daemonMode();
  const bool bypassCallMutex =
      isAgentControl || isCppSimCancel || backgroundQuery || daemonAgentTurn;
  const engine::RunLane lane = engine::runLane(method);
  const bool isLongRun = lane != engine::RunLane::None;
  // Dedicated daemon connections keep long calls off the GUI document socket.
  // Native runs use longRunMtx_ and simulations use g_simLaneMtx, independently.
  // Online daemon turns take neither lane mutex nor callMtx_: each leases its
  // own agent connection and control goes through hostd by run_id.
  // Pipe mode still holds callMtx_ because stdio carries one request at a time.
  // Native runs whose private connection cannot open also keep callMtx_ for
  // their shared-connection fallback.
  const bool daemonLongRun = isLongRun && !daemonAgentTurn &&
                             engine::daemonMode() && engine::runConnectionReady(lane);
  std::mutex &laneMtx =
      lane == engine::RunLane::Sim ? g_simLaneMtx : longRunMtx_;
  std::unique_lock<std::mutex> lk(daemonLongRun ? laneMtx : callMtx_,
                                  std::defer_lock);
  if (isCppSimCancel && engine::daemonMode()) {
    // Keep cancellation on an independent socket even if a solve's dedicated
    // connection was unavailable; it must never queue behind the work it stops.
    return callViaSideClient(method, params, result, err, timeoutMs, onEvent);
  }
  if (daemonLongRun && lane == engine::RunLane::Sim &&
      engine::isSimLanePreview(method) && !lk.try_lock()) {
    // A mesh preview arrives on the GUI's single-FIFO st.rpc lane while a solve
    // (SimClient's own WorkerClient, possibly hours of topology optimization)
    // holds the process's simulation lane. Waiting for the lane parked that
    // whole FIFO — and every doc_activate behind it — for the entire solve, so
    // take the same short-lived side connection engine-bound calls use. Only the
    // preview does this: solves still wait for the lane, one at a time.
    return callViaSideClient(method, params, result, err, timeoutMs, onEvent);
  }
  if (!bypassCallMutex && !isLongRun && engine::daemonMode()) {
    // Engine-bound call arriving while another call holds callMtx_ — e.g. a
    // slow doc_rebuild: the GUI thread makes config_list / doc_tree / pick
    // calls on normal interaction, and blocking it behind that call froze the
    // whole app (2026-07-21 SIGABRT crash, then behind an agent_run). In daemon mode
    // hostd serves each connection on its own thread, so sidestep the wait
    // with a short-lived side connection. Excluded (keep blocking on the shared
    // connection): minting calls (doc_new/doc_open/import_file,
    // ecad_new/ecad_open — hostd attaches a minted document rw to the
    // connection that minted it, and a side connection closes as soon as the
    // call returns), agent-seam methods (routeAgentToCpp translates host RPC
    // names to core names — agent_ask_v2→agent_answer_question etc. — which the
    // side channel does not do), and pipe-bound methods (plm_*, git_*,
    // most sim_* — rare on the GUI thread).
    const bool engineBound =
        (method.rfind("cam_", 0) == 0 || routeSimToCpp(method, params) ||
         routeEcadToCpp(method, params) || routeFirmwareToCpp(method, params) ||
         (routeDocToCpp(method, params) && !isMintingMethod(method, params))) &&
        method != "ecad_new" && method != "ecad_open";
    if (engineBound && !lk.try_lock())
      return callViaSideClient(method, params, result, err, timeoutMs, onEvent);
  }
  if (!bypassCallMutex && !lk.owns_lock())
    lk.lock();
  if (abort_.load()) { // C39: app is tearing down — intentional, no errorSink
                       // toast
    err = "aborted";
    return false;
  }
  // Native agent fallback. Online top-level turns arrive as agent_admin_turn and do not enter
  // this block; libchudware restricts agent_run_v2 to offline-safe local providers.
  if (routeAgentToCpp(method, params)) {
    std::string cerr;
    json cppResult;
    bool ok = false;
    if (method == "agent_run_v2") {
      // {request, doc_id, provider?, model?, reasoning_effort?, ...} ->
      // agent_run. The C++ core resolves the provider from config.json unless
      // the GUI pins a provider NAME (or full descriptor) + model/effort
      // overrides (picker O1). CRITICAL: those top-level override fields MUST
      // be forwarded — dropping them makes the picker decorative and leaves
      // ambient config.json/env in charge.
      json p = {{"request", params.value("request", std::string())},
                {"doc_id", params.value("doc_id", std::string())}};
      for (const char *k : {"provider", "model", "reasoning_effort",
                            "attachments", "context_refs"}) {
        auto it = params.find(k);
        if (it != params.end())
          p[k] = *it;
      }
      // Pass through any run options the host may carry
      // (budgets/run_id/control).
      json options = json::object();
      for (const char *k : {"options", "budgets", "run_id", "control"}) {
        auto it = params.find(k);
        if (it != params.end()) {
          if (std::string(k) == "options" && it->is_object())
            options = *it;
          else
            options[k] = *it;
        }
      }
      if (!options.empty())
        p["options"] = options;
      bool streamedLive = false;
      ok = callAgentStreaming("agent_run", p, cppResult, cerr, timeoutMs,
                              onEvent, streamedLive);
      // Batched replay ONLY when events did NOT stream live (old engine w/o the
      // sink export). When streamedLive, onEvent already fired per-event during
      // the run — replaying the batched cppResult["events"] would double every
      // card.
      if (!streamedLive && ok && onEvent && cppResult.is_object() &&
          cppResult.contains("events") && cppResult["events"].is_array()) {
        for (const auto &ev : cppResult["events"])
          onEvent(ev);
      }
    } else if (method == "agent_mission_v1") {
      // {request, doc_id, provider?, model?, ...} -> agent_mission_v1. Same O1
      // passthrough as agent_run_v2 so the picker beats ambient config.
      json p = {{"request", params.value("request", std::string())},
                {"doc_id", params.value("doc_id", std::string())}};
      for (const char *k : {"provider", "model", "reasoning_effort",
                            "attachments", "context_refs"}) {
        auto it = params.find(k);
        if (it != params.end())
          p[k] = *it;
      }
      json options = json::object();
      for (const char *k : {"options", "budgets", "run_id", "control"}) {
        auto it = params.find(k);
        if (it != params.end()) {
          if (std::string(k) == "options" && it->is_object())
            options = *it;
          else
            options[k] = *it;
        }
      }
      if (!options.empty())
        p["options"] = options;
      bool streamedLive = false;
      ok = callAgentStreaming("agent_mission_v1", p, cppResult, cerr, timeoutMs,
                              onEvent, streamedLive);
      if (!streamedLive && ok && onEvent && cppResult.is_object() &&
          cppResult.contains("events") && cppResult["events"].is_array()) {
        for (const auto &ev : cppResult["events"])
          onEvent(ev);
      }
    } else if (method == "agent_ask_v2") {
      // {question, doc_id, provider?, model?, ...} -> agent_answer_question.
      json p = {{"question", params.value("question", std::string())},
                {"doc_id", params.value("doc_id", std::string())}};
      for (const char *k : {"provider", "model", "reasoning_effort"}) {
        auto it = params.find(k);
        if (it != params.end())
          p[k] = *it;
      }
      ok = engine::call("agent_answer_question", p, cppResult, cerr, timeoutMs);
    } else if (method == "agent_clarify_v1") {
      // {request, doc_id, provider?, model?, ...} -> agent_clarify.
      json p = {{"request", params.value("request", std::string())},
                {"doc_id", params.value("doc_id", std::string())}};
      for (const char *k : {"provider", "model", "reasoning_effort"}) {
        auto it = params.find(k);
        if (it != params.end())
          p[k] = *it;
      }
      ok = engine::call("agent_clarify", p, cppResult, cerr, timeoutMs);
    } else if (method == "agent_pm_assist_v1") {
      // The host's PM payload (doc_id, intent, form_schema, current,
      // selected_refs) IS the params object the C++ assist expects under
      // "params".
      json p = {{"params", params},
                {"doc_id", params.value("doc_id", std::string())}};
      ok = engine::call("agent_pm_assist", p, cppResult, cerr, timeoutMs);
      // C++ returns {validated:{...}}; the host consumes the assist result
      // object directly (patches/activate_well/...). Unwrap so the caller sees
      // the same shape the Python agent_pm_assist_v1 returns.
      if (ok && cppResult.is_object() && cppResult.contains("validated"))
        cppResult = cppResult["validated"];
    } else if (method == "get_prompts_v2") {
      // -> agent_get_prompts_v2 {} (C++ default/env resolves the prompts dir).
      ok = engine::call("agent_get_prompts_v2", json::object(), cppResult, cerr,
                        timeoutMs);
    } else if (method == "agent_list_models") {
      // {provider, base_url?, api_key?} -> {ok, models:[...]} (pass-through
      // probe).
      ok =
          engine::call("agent_list_models", params, cppResult, cerr, timeoutMs);
    } else if (method == "agent_simulate_v1") {
      // {doc_id, request} -> {ok, answer, drag_n, lift_n, ...}: run a
      // wind-tunnel CFD on the current geometry (pass-through; the core parses
      // the flow condition + runs LBM).
      ok =
          engine::call("agent_simulate_v1", params, cppResult, cerr, timeoutMs);
    } else if (method == "agent_steer" || method == "agent_answer") {
      // Mid-run steering / ask_user answer: steer queues `text` for the run to
      // consume at the next stage/attempt boundary; answer resolves the run's
      // pending question batch. Same transport shape as agent_cancel:
      // in-process calls libchudware directly; daemon mode uses a SHORT-LIVED
      // second EngineClient so the control call never queues behind the
      // in-flight agent_run on the shared socket (hostd routes it to the
      // active worker run's control lane).
      if (engine::daemonMode()) {
        chudware_ipc::EngineClient side;
        chudware_ipc::EngineClient::Options opts;
        opts.clientKind = "gui";
        opts.hostdBin =
            engine::hostdBinHint(); // same instance key as the live daemon
        opts.allowSpawn = false;    // never spawn a new hostd for control
        std::string cerr2;
        if (side.connectOrSpawn(opts, cerr2)) {
          ok = side.call(method, params, cppResult, cerr, 5000);
        } else {
          cerr = cerr2.empty() ? method + ": daemon unreachable" : cerr2;
          ok = false;
        }
      } else if (method == "agent_answer") {
        // In-process there is no hostd to route to the worker's control lane,
        // and libchudware has no question registry: callAgentAnswer already
        // tried the direct worker control socket, so no live run owns it.
        cppResult = json{{"ok", false},
                         {"run_id", params.value("run_id", std::string())},
                         {"batch_id", params.value("batch_id", std::string())},
                         {"answered", false},
                         {"reason", "no active agent run for this question"}};
        ok = true;
      } else {
        ok = engine::call(method, params, cppResult, cerr, timeoutMs);
      }
    } else if (method == "agent_cancel") {
      // Best-effort stop. In-process: call libchudware directly (no mutex
      // contention with the blocked agent_run). Daemon: open a SHORT-LIVED
      // second EngineClient so we don't queue behind the in-flight agent_run on
      // the shared socket.
      if (engine::daemonMode()) {
        chudware_ipc::EngineClient side;
        chudware_ipc::EngineClient::Options opts;
        opts.clientKind = "gui";
        opts.hostdBin =
            engine::hostdBinHint(); // same instance key as the live daemon
        opts.allowSpawn = false;    // never spawn a new hostd for cancel
        std::string cerr2;
        if (side.connectOrSpawn(opts, cerr2)) {
          ok = side.call("agent_cancel", params, cppResult, cerr, 5000);
        } else {
          cerr = cerr2.empty() ? "cancel: daemon unreachable" : cerr2;
          ok = false;
        }
      } else {
        ok = engine::call("agent_cancel", params, cppResult, cerr, timeoutMs);
      }
    }
    if (ok) {
      result = std::move(cppResult);
      return true;
    }
    // C++ reported a structured error: surface it like the doc path (round-trip
    // OK).
    if (cppResult.is_object() && cppResult.contains("code")) {
      result = cppResult;
      err = cppResult.value("message", std::string("cpp-agent error"));
      return true;
    }
    // Hard failure (a C++ throw, not a structured {code} envelope). The v2
    // agent RPCs (agent_run_v2 / ask / clarify / pm_assist / simulate) are
    // C++-ONLY — the Python worker deleted them (engine/worker.py: "MIGRATED TO
    // THE C++ CORE"), so falling through to it would ALWAYS return the
    // misleading "unknown method: agent_run_v2" and bury the real cause (e.g. a
    // provider/config error). Surface the actual C++ error instead.
    // (agent_list_models is the one exception: it has a host-side static
    // catalog fallback, so we let it fall through and degrade gracefully.)
    if (method != "agent_list_models") {
      err = cerr.empty() ? ("cpp-agent (" + method + ") failed") : cerr;
      if (errorSink_)
        errorSink_(method, err);
      return false;
    }
    // else: fall through to the Python worker path below (list_models tolerates
    // failure).
  }
  // C++ SIM (FEA/flow) routing. The host already sends the C++ {study:{...}}
  // contract verbatim, so params pass through unchanged; the result carries
  // summary + overlay_aicm (what sim_panel reads).
  if (routeSimToCpp(method, params)) {
    std::string cerr;
    json cppResult;
    // The native topology solve streams sim.progress (phase + per-iteration
    // metrics) live to onEvent: daemon mode over hostd's agent.event push on
    // engine_backend's dedicated simulation run connection, in-process through
    // the thread-local cpp_core event sink. Its result carries no batched event
    // copy, so there is nothing to replay. Other sim methods keep their
    // existing non-streaming call.
    bool callOk = false;
    if (method == "sim_run_topology") {
      bool streamedLive = false;
      callOk = callAgentStreaming(method, params, cppResult, cerr, timeoutMs,
                                  onEvent, streamedLive);
    } else {
      callOk = engine::call(method, params, cppResult, cerr, timeoutMs);
    }
    if (callOk) {
      result =
          std::move(cppResult); // ok: result holds {summary, overlay_aicm, ...}
      return true;
    }
    // The core reported a STRUCTURED error envelope ({code,message,detail}).
    // Surface it the same way the doc/agent seam does: deliver it in `result`
    // and return true (the round-trip succeeded, the METHOD reported an error).
    // SimClient::launch reads result.contains("code") to mark the job failed
    // with the message.
    if (cppResult.is_object() && cppResult.contains("code")) {
      result = std::move(cppResult);
      err = result.value("message", std::string("cpp-sim error"));
      return true;
    }
    // The core returned a bare unknown-method error. FEA methods have no Python
    // fallback, so translate the dispatcher detail into the build option that
    // restores the capability.
    if (cppResult.is_object()) {
      const std::string cppErr = cppResult.value("error", std::string());
      if (cppErr.rfind("unknown method", 0) == 0) {
        if (isTopologyMethod(method))
          err = "This engine build has no native topology optimizer, so '" +
                method + "' is unavailable. Update the engine to a build "
                         "that dispatches it.";
        else
          err = "This engine build has no FEA mesher, so '" + method +
                "' is unavailable. Rebuild the core with "
                "-DCHUDWARE_WITH_POLYMESH=ON.";
        if (errorSink_)
          errorSink_(method, err);
        return false;
      }
    }
    if (isNativeOnlySimMethod(method)) {
      // A native failure that still carries its payload — a cancelled or failed
      // topology run answers with its partial design, history and termination —
      // is a METHOD failure: hand the payload to the caller (SimClient keeps it)
      // normalized the way hostd normalizes engine errors (message + code always
      // present), so the daemon and in-process backends report it identically.
      // Only a payload-less failure is a transport error.
      if (cppResult.is_object() && !cppResult.empty()) {
        std::string message;
        for (const char *k : {"message", "error", "termination_detail"}) {
          const auto it = cppResult.find(k);
          if (it != cppResult.end() && it->is_string() &&
              !it->get_ref<const std::string &>().empty()) {
            message = it->get<std::string>();
            break;
          }
        }
        if (message.empty())
          message = !cerr.empty() ? cerr : method + " failed in the engine";
        const auto msgIt = cppResult.find("message");
        if (msgIt == cppResult.end() || !msgIt->is_string())
          cppResult["message"] = message;
        if (!cppResult.contains("code"))
          cppResult["code"] = "engine_error";
        result = std::move(cppResult);
        err = std::move(message);
        return true;
      }
      if (!cerr.empty())
        err = cerr;
      else if (method == "sim_run_buckling")
        err = "Engine linear buckling is unavailable";
      else if (method == "sim_netlist_to_circuit")
        err = "Native circuit conversion is unavailable";
      else
        err = "Engine topology optimization is unavailable";
      if (errorSink_)
        errorSink_(method, err);
      return false;
    }
    // Any other hard failure (DLL throw / load race after the available() check):
    // do NOT hard-break sim — fall through to the Python sim worker path below so
    // analysis keeps working.
  }
  // C++ ECAD routing is always enabled for the methods allowlisted above.
  // ecad_new/ecad_open mint "ecadcpp" documents; later document-bound calls
  // route here only when both that id prefix and an implemented method match.
  // MCP-only ecad_* methods deliberately skip this block and reach the generic
  // hostd/worker bridge below. No params translation is needed for claimed
  // calls: the host send-shape is the libchudware contract.
  if (routeEcadToCpp(method, params)) {
    std::string cerr;
    if (engine::call(method, params, result, cerr, timeoutMs))
      return true; // handled (rc==0); `result` holds {doc_id, revision, tree,
                   // ...}
    // Nonzero rc: a structured OpError carries {code,message,detail} in
    // `result` — including an ecad_new/ecad_open the core declined.
    if (result.is_object() && result.contains("code")) {
      err = result.value("message", std::string("cpp-ecad error"));
      return true; // structured error reported (round-trip OK)
    }
    // Hard failure on a claimed core RPC (minting included — no services worker
    // serves ECAD documents): surface the core failure rather than masking it
    // behind a different backend.
    err = "cpp-ecad (" + method + "): " + cerr;
    if (errorSink_)
      errorSink_(method, err);
    return false;
  }
  // CAM (computer-aided machining): every cam_* RPC lives ONLY in the C++ core
  // (libchudware); there is no Python CAM engine. Route them all to cpp_core and
  // report its result (an OpError surfaces as {code,message,detail}); never
  // fall back to Python.
  if (method.rfind("cam_", 0) == 0) {
    std::string cerr;
    if (engine::call(method, params, result, cerr, timeoutMs))
      return true; // handled (rc==0)
    if (result.is_object() && result.contains("code")) {
      err = result.value("message", std::string("cpp-cam error"));
      return true; // structured OpError round-tripped
    }
    err = "cpp-cam (" + method + "): " + cerr;
    if (errorSink_)
      errorSink_(method, err);
    return false;
  }
  // Firmware (fw_*): the whole surface lives in the C++ core (core/firmware,
  // dispatched in libchudware.cpp; hostd serves it in daemon mode). The Python
  // worker no longer registers fw_* methods — calls that reach the pipe come
  // back "unknown method". Route like cam_*: structured error envelopes
  // round-trip in `result`; hard failures surface via errorSink.
  if (routeFirmwareToCpp(method, params)) {
    std::string cerr;
    if (engine::call(method, params, result, cerr, timeoutMs))
      return true;
    if (result.is_object() && result.contains("code")) {
      err = result.value("message", std::string("cpp-firmware error"));
      return true; // structured OpError round-tripped
    }
    err = "cpp-firmware (" + method + "): " + cerr;
    if (errorSink_)
      errorSink_(method, err);
    return false;
  }
  // agent_cap_raw remains a pure native helper. It has no Python registration,
  // so route it directly to the loaded core and never fall back to the pipe.
  if (method == "agent_cap_raw") {
    std::string cerr;
    if (engine::call(method, params, result, cerr, timeoutMs))
      return true;
    if (result.is_object() && result.contains("code")) {
      err = result.value("message", std::string("agent_cap_raw error"));
      return true;
    }
    err = cerr.empty() ? "agent_cap_raw failed" : cerr;
    if (errorSink_)
      errorSink_(method, err);
    return false;
  }
  // P16: route C++-backed documents to the core (libchudware). All-or-nothing
  // per document, and the C++ core is the only document engine, so an unported
  // method or a declined mint surfaces a clear error rather than silently
  // misrouting. The diagnostic parity probe in doc_mirror is unaffected.
  if (routeDocToCpp(method, params)) {
    const bool minting = isMintingMethod(method, params);
    std::string cerr;
    if (engine::call(method, params, result, cerr, timeoutMs))
      return true; // handled (chudware_call rc==0); `result` holds the
                   // ops-response
    // Nonzero rc: a structured OpError carries {code,message,detail} in
    // `result` — including a MINTING call the core declined as "unsupported"
    // (e.g. doc_open of a non-part container), which created no doc.
    if (result.is_object() && result.contains("code")) {
      err = result.value("message", std::string("cpp-core error"));
      return true; // structured error reported (round-trip OK)
    }
    // Hard failure (DLL missing / a throw / doc_open: "no such file:
    // live.step+"). No services worker serves documents, so fail loudly with
    // the core's message instead of masking it behind "unknown method".
    err = minting ? (cerr.empty() ? ("cpp-core (" + method + ") failed") : cerr)
                  : ("cpp-core (" + method + "): " + cerr);
    if (errorSink_)
      errorSink_(method, err);
    return false;
  }
  // Fall-through methods (plm_*, git_*, ai_roles_*, subscription_status, bench_*,
  // triggers_*, skills_*, list_editable_prompts, provider accounts, and service-side provider
  // completions like Cursor) are served by the worker sidecar, NOT the C++ core. On the web
  // there is no local pipe; on desktop in DAEMON mode the GUI likewise has no local worker
  // child (tryStartWorker fell back to the in-process C++ core, running_==false), so in BOTH
  // cases route them over the engine channel — engine::call -> hostd, which bridges
  // engine-unknown methods to its lazily-spawned services worker. Without
  // this, desktop daemon mode hit the dead local pipe below and every such method failed with
  // "worker not started" even though hostd was up. Only a desktop build on the LEGACY
  // in-process backend (CHUDWARE_ENGINE_DAEMON=0) with a real local worker child takes the
  // pipe path below.
#if defined(__EMSCRIPTEN__)
  const bool routeViaEngine = true;
#else
  const bool routeViaEngine = engine::daemonMode();
#endif
  if (routeViaEngine) {
#if !defined(__EMSCRIPTEN__)
    if (method == "agent_admin_turn" &&
        (!params.is_object() ||
         params.value("run_id", std::string()).empty())) {
      err = "agent_admin_turn requires a non-empty run_id";
      return false;
    }
#endif
    std::string cerr;
    if (engine::call(method, params, result, cerr, timeoutMs, onEvent))
      return true;
    // Structured method error envelope (round-trip OK) — same contract as the pipe path
    // (object error -> result + true) and the doc/cam engine branches.
    if (result.is_object() && result.contains("code")) {
      err = result.value("message", std::string("rpc error"));
      return true;
    }
    if (result.is_object() && !result.empty()) {
      // Error object without code (EngineClient stores the error payload in result).
      err = result.value("message", cerr.empty() ? std::string("rpc error") : cerr);
      return true;
    }
    if (result.is_string()) {
      // Legacy plain-string method error -> false, no errorSink_ (pipe path contract).
      err = result.get<std::string>();
      return false;
    }
    // Transport / channel failure (result untouched or empty).
    err = cerr.empty() ? std::string("engine channel error") : cerr;
    if (errorSink_)
      errorSink_(method, err);
    return false;
  }
#if defined(__EMSCRIPTEN__)
  err = "local worker pipe is unavailable";
  return false;
#else
  return callWorkerPipe(method, params, result, err, timeoutMs, onEvent);
#endif
}

bool WorkerClient::callWorkerPipe(const std::string &method,
                                  const json &params, json &result,
                                  std::string &err, int timeoutMs,
                                  const EventFn &onEvent) {
#if !defined(__EMSCRIPTEN__)
  // Reap a worker child that died since start() (import crash, kill, OOM, …).
  // Without this, running_ stays true and every pipe write fails EPIPE —
  // polling panels (fw_projects, parts, plm) then re-raise the same transport
  // error forever (the "write failed" toast storm). markStarted() clients have
  // no child (process_ is -1/nullptr) and are untouched.
#ifdef _WIN32
  if (process_) {
    DWORD exitCode = 0;
    if (GetExitCodeProcess(process_, &exitCode) && exitCode != STILL_ACTIVE) {
      CloseHandle(process_);
      process_ = nullptr;
      if (childStdinWr_) {
        CloseHandle(childStdinWr_);
        childStdinWr_ = nullptr;
      }
      if (childStdoutRd_) {
        CloseHandle(childStdoutRd_);
        childStdoutRd_ = nullptr;
      }
      running_ = false;
      clearWorkerControlState();
    }
  }
#else
  if (process_ >= 0) {
    int wstatus = 0;
    pid_t wr = waitpid(process_, &wstatus, WNOHANG);
    if (wr == process_ || (wr < 0 && errno == ECHILD)) {
      process_ = -1;
      if (childStdinWr_ >= 0) {
        ::close(childStdinWr_);
        childStdinWr_ = -1;
      }
      if (childStdoutRd_ >= 0) {
        ::close(childStdoutRd_);
        childStdoutRd_ = -1;
      }
      running_ = false;
      clearWorkerControlState();
    }
  }
#endif
  if (poisoned_) {
    std::string restartErr;
    if (!restartPoisonedWorker(restartErr)) {
      err = "worker restart after timeout failed: " + restartErr;
      if (errorSink_)
        errorSink_(method, err);
      return false;
    }
  }
  if (!running_) {
    err = "worker not started";
    if (errorSink_)
      errorSink_(method, err); // W1-B: surface transport failures
    return false;
  }
  const bool workerAgentRun = method == "agent_admin_turn";
  std::string workerRunId;
  if (workerAgentRun) {
    workerRunId = params.is_object()
                      ? params.value("run_id", std::string())
                      : std::string();
    if (workerRunId.empty()) {
      err = "agent_admin_turn requires a non-empty run_id";
      return false;
    }
    if (!ensureWorkerControlInfo(err))
      return false;
  }
  ActiveWorkerRunMarker activeWorkerRun(
      workerControlMtx_, activeWorkerRunId_, std::move(workerRunId),
      workerAgentRun);
  int id = nextId_++;
  json req = {{"id", id}, {"method", method}, {"params", params}};
  std::string line = req.dump() + "\n";
#ifdef _WIN32
  DWORD wrote = 0;
  if (!WriteFile(childStdinWr_, line.data(), (DWORD)line.size(), &wrote,
                 nullptr)) {
#else
  if (::write(childStdinWr_, line.data(), line.size()) !=
      (ssize_t)line.size()) {
#endif
    err = "write failed";
    clearWorkerControlInfo();
    if (errorSink_)
      errorSink_(method, err); // W1-B
    return false;
  }
  for (;;) {
    std::string resp;
    const ReadLineResult readStatus = readLine(resp, timeoutMs);
    if (readStatus != ReadLineResult::Line) {
      clearWorkerControlInfo();
      if (abort_.load()) { // C39: teardown abort, not a worker failure
        err = "aborted";
        return false;
      }
      err = "timeout/closed waiting for response";
      if (readStatus == ReadLineResult::Timeout) {
        poisoned_ = true;
        std::fprintf(
            stderr,
            "worker request %d (%s) exceeded the local-pipe timeout; "
            "connection poisoned, discarding any late result and restarting "
            "before the next request\n",
            id, method.c_str());
        std::string restartErr;
        if (restartPoisonedWorker(restartErr)) {
          std::fprintf(stderr,
                       "worker restarted on a fresh local-pipe stream after "
                       "timed-out request %d\n",
                       id);
        } else {
          std::fprintf(stderr,
                       "worker restart after timed-out request %d failed: %s\n",
                       id, restartErr.c_str());
        }
      }
      if (errorSink_)
        errorSink_(method, err); // W1-B
      return false;
    }
    if (resp.empty())
      continue;
    json j;
    try {
      j = json::parse(resp);
    } catch (...) {
      continue; // ignore non-JSON noise on the channel
    }
    if (!j.contains("id") || j["id"].is_null() || j["id"].get<int>() != id)
      continue;
    // Streamed progress notification (no "ok"): deliver and keep waiting.
    if (j.contains("event") && !j.contains("ok")) {
      if (onEvent)
        onEvent(j.value("data", json::object()));
      continue;
    }
    if (j.value("ok", false)) {
      result = j.value("result", json::object());
      // P-session: doc_list must enumerate BOTH engines during the
      // mixed-cutover phase. The Python worker only knows its own docs; append
      // the C++ core's open sessions. Gated on available() — the DLL only loads
      // once a C++-backed doc exists, so when the cutover flag is off this is
      // byte-for-byte today's behavior (no C++ docs, no merge).
      if (method == "doc_list" && engine::available() && result.is_object() &&
          result.contains("docs") && result["docs"].is_array()) {
        json cppList;
        std::string cerr;
        if (engine::call("doc_list", json::object(), cppList, cerr,
                         timeoutMs) &&
            cppList.is_object() && cppList.contains("docs") &&
            cppList["docs"].is_array()) {
          for (auto &d : cppList["docs"])
            result["docs"].push_back(d);
        }
      }
      return true;
    }
    // Protocol-2 methods report errors as a structured envelope
    // {code,message,detail}. Deliver it in `result` (doc_mirror checks
    // result.contains("code")) and return true: the RPC round-trip succeeded,
    // the METHOD reported an error. Legacy methods use a plain string error and
    // keep the false-return contract. Transport failures stay false.
    {
      const auto it = j.find("error");
      if (it != j.end() && it->is_object()) {
        result = *it;
        err = it->value("message", std::string("rpc error"));
        return true;
      }
      err = (it != j.end() && it->is_string())
                ? it->get<std::string>()
                : std::string("unknown rpc error");
      return false;
    }
  }
#else
  (void)method;
  (void)params;
  (void)result;
  (void)timeoutMs;
  (void)onEvent;
  err = "local worker pipe is unavailable";
  return false;
#endif // !__EMSCRIPTEN__
}
