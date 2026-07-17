// aicad-hostd server core: accept loop, per-client connection threads, the
// hello handshake, and raw passthrough of engine methods into the ONE loaded
// libaicad (via core::CppCore, the same loader every other consumer uses).
//
// Phase-1 scope (see plan): daemon-level methods hello/ping/daemon_status/
// daemon_drain; everything else goes straight to aicad_call. The doc registry
// (attach modes, events, edit holds) layers on top in Phase 3 — the per-conn
// write mutex and client table are already shaped for it.
#pragma once

#include <atomic>
#include <map>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include <nlohmann/json.hpp>

#include "../../services/ipc/ipc_transport.h"
#include "autosave.h"
#include "child_supervisor.h"
#include "doc_registry.h"
#include "engine_watchdog.h"

namespace hostd {

struct ClientConn {
    int id = 0;
    std::string kind;  // gui | mcp | agent | test (from hello)
    // Optional multi-user identity from hello params.user {id, display}.
    // Empty when the client did not send a user (local single-seat default).
    std::string userId;
    std::string userDisplay;
    long pid = 0;
    aicad_ipc::IpcConn conn;
    std::mutex writeMtx;  // response writes and (Phase 3) event pushes interleave
    std::thread thread;
};

class Server {
public:
    // engineState: 0=loading 1=ready 2=failed (set by the async engine loader).
    struct Status {
        std::atomic<int> engineState{0};
        std::string engineErr;                 // set once, before engineState=2
        std::atomic<int> clients{0};
        std::atomic<int> inflight{0};
        std::atomic<bool> draining{false};
        std::atomic<long long> lastActivityMs{0};  // monotonic ms of last disconnect/RPC
    };

    Server(aicad_ipc::IpcListener listener, std::string buildId);
    ~Server();

    // T-PERF10: close the listen fd without unlinking the sock, and return the
    // path so the caller can hand it to SingleInstance::setSockPathToUnlink.
    // Must be called before ~Server so the destructor does not race the unlink.
    std::string detachSocketForFlockRelease();

    // Phase 3: the autosave scheduler (recovery id_remap served in hello) and
    // the doc registry (attach modes, holds, events) plug in before run().
    void setAutosave(Autosave* a) { autosave_ = a; }
    DocRegistry& registry() { return registry_; }
    // Phase 5: supervised agent children (agent_spawn/agent_stop/agent_list).
    ChildSupervisor& children() { return children_; }
    // Phase 8: stale-binary self-drain backstop. The accept-loop tick stats the
    // hostd binary itself + the loaded libaicad every AICAD_HOSTD_STALECHECK_S
    // (default 60 s); an mtime change (dev rebuild, installer upgrade) flips the
    // daemon to draining so the next client respawns the new build.
    void setSelfBinary(std::string path) { selfBinary_ = std::move(path); }

    // Accept/dispatch until drained-and-idle (or stop()). lingerSecs: how long
    // the daemon must be idle (no clients, no inflight) before self-exit.
    void run(int lingerSecs);
    void stop() { stopping_ = true; }

    Status& status() { return status_; }

private:
    void clientLoop(std::shared_ptr<ClientConn> c);
    nlohmann::json dispatch(ClientConn& c, const std::string& method,
                            const nlohmann::json& params, bool& isError);
    void touch();
    bool idleExpired(int lingerSecs);
    void staleBinaryTick();
    // Engine-call watchdog: track the in-flight engine RPC per client thread (one at a time —
    // clientLoop is sequential) so a background thread can detect a wedged interactive rebuild
    // and self-heal via restart. See engine_watchdog.h for the policy.
    void beginInflight(int clientId, const std::string& method);
    void endInflight(int clientId);
    void watchdogLoop();
    // DISCARD an abandoned doc: close it in the engine and purge its autosave
    // artifacts (onDocClosed) so it never resurrects. Used by doc-GC (grace
    // expired, reason "gc") and explicit user discard (reason "discarded") —
    // neither is a crash, so nothing is preserved for recovery. Returns true once
    // registry state is dropped (false = still attached / engine loading).
    // `reason` labels the doc.closed event. See DocRegistry::tick + isReapable.
    bool gcCloseDoc(const std::string& docId, const char* reason = "gc");
    void sendEventTo(int clientId, const std::string& line);
    void sendEventToAll(const std::string& line);

    aicad_ipc::IpcListener listener_;
    std::string buildId_;
    Status status_;
    std::atomic<bool> stopping_{false};
    std::mutex clientsMtx_;
    std::map<int, std::shared_ptr<ClientConn>> clients_;
    int nextClientId_ = 1;
    DocRegistry registry_;
    ChildSupervisor children_;
    Autosave* autosave_ = nullptr;
    // Stale-binary watch state (accept-loop thread only — no locking needed).
    std::string selfBinary_;
    long long lastStaleCheckMs_ = 0;
    std::map<std::string, long long> watchBaselineMs_;  // path -> first-seen mtime
    // Engine-call watchdog state.
    struct InflightCall { std::string method; long long startMs = 0; };
    std::mutex inflightMtx_;
    std::map<int, InflightCall> inflight_;   // clientId -> current engine call (absent = idle)
    std::thread watchdog_;
    long long engineWatchdogMs_ = 180000;    // 0 = disabled (env AICAD_HOSTD_ENGINE_WATCHDOG_S)
};

}  // namespace hostd
