// chudware-hostd server core: accept loop, per-client connection threads, the
// hello handshake, and raw passthrough of engine methods into the ONE loaded
// libchudware (via core::CppCore, the same loader every other consumer uses).
//
// Phase-1 scope (see plan): daemon-level methods hello/ping/daemon_status/
// daemon_drain; everything else goes straight to chudware_call. The doc registry
// (attach modes, events, edit holds) layers on top in Phase 3 — the per-conn
// write mutex and client table are already shaped for it.
#pragma once

#include <algorithm>
#include <atomic>
#include <cstdint>
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
    std::string writerGroup;  // optional shared writer identity from hello
    // Optional multi-user identity from hello params.user {id, display}.
    // Empty when the client did not send a user (local single-seat default).
    std::string userId;
    std::string userDisplay;
    long pid = 0;
    bool gateway = false;
    bool helloDone = false;
    std::atomic<bool> controlOnly{false};
    std::atomic<bool> operatorControl{false};
    bool operatorListener = false;
    bool retireAfterReply = false;
    std::string actorKey;
    std::string actorHex;
    std::string right;
    long long epoch = -1;
    chudware_ipc::IpcConn conn;
    // Monotonic ms of this connection's last request. A socket that has been silent
    // longer than the idle window, holds no attachment and has nothing in flight is a
    // PARKED connection — see Server::pinningClients().
    std::atomic<long long> lastRpcMs{0};
    std::timed_mutex writeMtx;  // response writes and event pushes interleave
    std::atomic<bool> eventWriteFailureLogged{false};
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

    Server(chudware_ipc::IpcListener listener, std::string buildId);
    ~Server();

    // Close the listen fd without unlinking the sock, and return the
    // path so the caller can hand it to SingleInstance::setSockPathToUnlink.
    // Must be called before ~Server so the destructor does not race the unlink.
    std::string detachSocketForFlockRelease();

    // Phase 3: the autosave scheduler (recovery id_remap served in hello) and
    // the doc registry (attach modes, holds, events) plug in before run().
    // Also teaches the scheduler which docs are INTERNAL throwaways (the agent
    // preflight shadow clone) — the registry owns that flag, the autosave tier has
    // to honour it or a clone lands in the user's "Recover?" prompt.
    void setAutosave(Autosave* a) {
        autosave_ = a;
        if (a) a->setInternalFilter([this](const std::string& id) { return registry_.isInternal(id); });
    }
    DocRegistry& registry() { return registry_; }
    // Phase 5: supervised agent children (agent_spawn/agent_stop/agent_list).
    ChildSupervisor& children() { return children_; }
    // Phase 8: stale-binary self-drain backstop. The accept-loop tick stats the
    // hostd binary itself + the loaded libchudware every CHUDWARE_HOSTD_STALECHECK_S
    // (default 60 s); an mtime change (dev rebuild, installer upgrade) flips the
    // daemon to draining so the next client respawns the new build.
    void setSelfBinary(std::string path) { selfBinary_ = std::move(path); }
    // Must be configured before run(); the gateway fd is the sole principal authority.
    bool configureCell(std::string state, std::string work,
                       const std::string& policyPath, std::string& err);
    bool installCacheRefreshSink();
    void clearCacheRefreshSink();

    // Accept/dispatch until the daemon has nothing left to do (or stop()). lingerSecs: how
    // long it must stay unpinned (see pinningClients) before self-exit.
    void run(int lingerSecs);
    void stop() { stopping_ = true; }

    // Begin a wind-down. `deadlineSecs > 0` makes it a HARD stop: new connects are refused
    // and, once the deadline passes, whatever is still connected is disconnected and the
    // daemon exits regardless. That deadline is what makes `--stop` / the tray's Quit /
    // SIGTERM terminal; a soft drain (deadlineSecs == 0, used by the stale-binary watch)
    // only refuses new connects and waits for the current work to finish on its own.
    // `reason` names the cause in every refusal and in daemon_status: "stale_binary"
    // (`changedPath` = the file that changed), "stop_requested" (daemon_drain / --stop),
    // "quit" (tray Quit). A later drain replaces the reason; a changed path is kept.
    void beginDrain(int deadlineSecs, std::string reason, std::string changedPath = {});
    // Drain cause as passed to beginDrain ("" while not draining). Any thread.
    std::string drainReason() const;

    // Short idle grace used instead of lingerSecs once the last GUI client has
    // detached (CHUDWARE_HOSTD_GUI_EXIT_GRACE_S). The daemon exists to serve
    // the GUI, an ACTIVE non-GUI client, or in-flight agent work the user chose to
    // continue in the background — none of those needs the full linger after the
    // GUI is gone.
    void setGuiExitGrace(int secs) { guiExitGraceSecs_ = std::max(1, secs); }

    Status& status() { return status_; }
    // Count of engine-side calls only (agent_run / doc_* via beginInflight).
    // Distinct from Status::inflight which counts every RPC including hello/status
    // and makes the tray show a permanent "1 in-flight" while any client polls.
    int engineInflightCount() const;

    // True while any client has an agent_* RPC on the wire. Agent-origin docs are
    // protected from user-initiated purges (doc_close/doc_discard) while this holds,
    // so an agent's in-flight work is independent of the user's viewport tabs.
    bool agentWorkInFlight() const;

private:
    // `firstLine` is a request already read off the socket before the loop started (the
    // hello of a control client admitted during a drain); empty = none.
    void clientLoop(std::shared_ptr<ClientConn> c, std::string firstLine);
    nlohmann::json dispatch(ClientConn& c, const std::string& method,
                            const nlohmann::json& params, bool& isError);
    // A connect that arrives while the daemon drains. Reads the first request (bounded)
    // and admits only an operator control client (`--status`, `--stop`), returning its
    // request in `firstLine`. Every other client gets drainRefusal() as the answer to its
    // first request, then the connection closes. The client sees a named reason, never a
    // bare reset (ECONNRESET came from closing a socket with an unread hello).
    bool admitWhileDraining(chudware_ipc::IpcConn& conn, std::string& firstLine);
    // The structured `daemon_draining` error a refused client receives.
    nlohmann::json drainRefusal();
    void touch();
    bool idleExpired(int lingerSecs);
    void staleBinaryTick();
    // How many connected clients are a REASON to stay up, as opposed to merely connected.
    //
    // WHY this is not `status_.clients`: a client that connected, said hello and then went
    // silent (a crashed-but-not-closed tool, an editor's MCP server parked all day) held
    // `clients > 0` forever, and every exit path — idle-exit AND drain — waits on that
    // counter. One parked socket kept a daemon alive for 5.5 h in the "draining" state with
    // a tray icon the user could not get rid of. A GUI client always counts (the app being
    // open IS the reason the daemon exists); any other client counts only while it is
    // actually working: an engine call in flight, an attached document, or traffic within
    // the idle window. Anything else is parked, and parked sockets get disconnected on the
    // way out — clients respawn the daemon on their next call, which is the normal contract.
    int pinningClients(int idleWindowSecs);
    // shutdown() every live client connection so their reader threads unblock. Used by the
    // hard-drain deadline: a client that will not leave on its own is dropped.
    void disconnectAllClients();
    // Engine-call watchdog: track the in-flight engine RPC per client thread (one at a time —
    // clientLoop is sequential) so a background thread can detect an overdue engine call
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
    // Live agent-event streaming (transparency): push one emitted run event to a client as a
    // {"event":"agent.event","data":<ev>} notification. streamThunk is the C sink registered
    // with the engine (chudware_set_event_sink); its `ud` is a StreamCtx{this, clientId}.
    void sendAgentEvent(int clientId, const char* eventJson);
    static void streamThunk(const char* eventJson, void* ud);
    // Direct doc_new/doc_apply_ops calls nested inside an opaque agent RPC arrive here only
    // after the engine operation returns. Feed them through DocRegistry::postCall so the normal
    // lock-free snapshot + doc.opened/doc.changed fanout path remains the single implementation.
    static void docPublishThunk(const char* method, const char* paramsJson,
                                const char* resultJson, void* ud);
    static void cacheRefreshThunk(const char* docId, const char* responseJson, void* ud);
    struct CellActor {
        std::string key, hex, right;
        long long epoch = -1;
        bool revoked = false;
        std::shared_ptr<chudware_ipc::IpcListener> listener;
    };
    bool cell_ = false;
    std::string cellState_, cellWork_, workspaceKey_;
    nlohmann::json cellPolicy_;
    // Serialize authority transitions, never hold cellMtx_ while retiring workers.
    std::mutex principalMtx_;
    std::mutex cellMtx_;
    std::map<std::string, CellActor> actors_;
    std::map<std::string, std::string> handleOwners_;
    std::map<std::string, std::string> runOwners_;
    std::map<std::string, bool> connectionNonces_;
    nlohmann::json cellContext(const ClientConn& c);
    nlohmann::json cellHello(ClientConn& c, const nlohmann::json& params, bool& isError);
    nlohmann::json changePrincipal(ClientConn& c, const std::string& method,
                                   const nlohmann::json& params, bool& isError);
    void retireActorGeneration(const std::string& actor, long long revokedThrough,
                               const nlohmann::json& owner, int controlClient);
    bool cellAdmit(ClientConn& c, const std::string& method,
                   nlohmann::json& params, nlohmann::json& error);
    nlohmann::json cellOperator(ClientConn& c, const std::string& method,
                                const nlohmann::json& params, bool& isError);
    nlohmann::json cellCensus(bool commitExit = false, bool idleExit = false);
    void acceptClient(chudware_ipc::IpcConn conn, bool gateway,
                      const std::string& actor = {}, bool operatorListener = false);
    void rememberCellHandles(const ClientConn& c, const nlohmann::json& params,
                             const nlohmann::json& result);

    chudware_ipc::IpcListener listener_;
    std::string buildId_;
    Status status_;
    std::atomic<bool> stopping_{false};
    // True once the last "gui" client has detached (reset when a gui hello
    // lands). idleExpired() uses guiExitGraceSecs_ instead of the full linger
    // while set, so the daemon does not outlive the GUI without a reason.
    std::atomic<bool> guiDetached_{false};
    int guiExitGraceSecs_ = 10;
    // Monotonic ms after which a HARD drain stops waiting and drops whatever is still
    // connected. 0 = no deadline (soft drain, or not draining at all).
    std::atomic<long long> drainDeadlineMs_{0};
    // Why the daemon drains (see beginDrain). Written from the accept loop, a client
    // thread (daemon_drain) or the tray thread, so it has its own lock.
    mutable std::mutex drainMtx_;
    std::string drainReason_;
    std::string drainChangedPath_;
    std::mutex clientsMtx_;
    // Connection registration and maintenance closure share this authority.
    bool cellAdmissionClosed_ = false, cellExitWhenQuiescent_ = false, cellExiting_ = false;
    std::uint64_t cellGeneration_ = 0;
    std::atomic<int> controlRepliesPending_{0};
    std::mutex cellCensusMtx_;
    long long lastCellProbeMs_ = 0;  // accept loop only
    std::string cellId_;
    std::unique_ptr<chudware_ipc::IpcListener> operatorListener_;
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
    mutable std::mutex inflightMtx_;
    std::map<int, InflightCall> inflight_;   // clientId -> current engine call (absent = idle)
    std::thread watchdog_;
    // 90s for bounded non-modeling calls (engine_watchdog.h). Modeling/rebuild calls use
    // the separate long ceiling because their cost scales with document size.
    // 0 = disabled. Env override: CHUDWARE_HOSTD_ENGINE_WATCHDOG_S.
    long long engineWatchdogMs_ = 90000;
};

}  // namespace hostd
