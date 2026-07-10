// Phase 4 (shared engine host daemon): the ONE seam that decides where the
// GUI's C++-engine calls execute.
//
// Two backends behind one call() contract (the CppCore::call signature):
//   * InProcCore  — today's behavior: core::cpp_core() dlopens libaicad into
//                   the GUI process. Default.
//   * DaemonConn  — aicad_ipc::EngineClient over the hostd unix socket
//                   (clientKind "gui"), selected ONCE at startup by
//                   AI_CAD_ENGINE_DAEMON=1. On connect/spawn failure we log
//                   loudly and fall back to InProcCore (same pattern as
//                   services/mcp/core_bridge.py), so the GUI never hard-breaks.
//
// Threading: engine::call() runs on the async_rpc worker thread (EngineClient
// serializes internally). engine::client()->drainEvents() is called from the
// GUI thread each frame (host/src/core/engine_events.cpp) — EngineClient is
// internally locked and drainEvents try-locks so a long-running solve on the
// worker thread never stalls the render loop.
#pragma once

#include <string>

#include <nlohmann/json.hpp>

namespace aicad_ipc { class EngineClient; }

namespace engine {

// True iff the daemon backend was selected AND connected at startup (resolved
// once, thread-safe). False = legacy in-process libaicad.
bool daemonMode();

// The live daemon connection (nullptr unless daemonMode()). GUI-thread event
// drain + daemon-only methods (attach_doc / edit_hold / subscribe) go here.
aicad_ipc::EngineClient* client();

// Route an engine call to the selected backend. Same contract as
// core::CppCore::call: true = rc==0 (result holds the payload); false = the
// method reported an error (structured {code,message} lands in `result`) or a
// transport failure (err only). Daemon extras handled here:
//   * {"code":"loading"} absorbed by polling daemon_status until ready.
//   * {"code":"read_only"} auto-upgrades the attach (attach_doc mode=rw) and
//     retries once — the "user edits a mirror tab" path, zero new UI.
bool call(const std::string& method, const nlohmann::json& params,
          nlohmann::json& result, std::string& err);

// Backend availability probes with InProcCore semantics (routing predicates in
// worker_client.cpp use these instead of core::cpp_core() directly). In daemon
// mode both report the connection state (the daemon owns the DLL).
bool available();
bool ensureLoaded(std::string& err);

}  // namespace engine
