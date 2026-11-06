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
#include <functional>
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
#include "worker_bridge.h"
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
    // OS-authenticated PID of a desktop `gui` hello (IpcConn::peerPid), else -1. A soft
    // stale-binary drain admits further `gui` connections from that live process.
    std::atomic<long> guiPeerPid{-1};
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

    Server(chudware_ipc::IpcListener listener, std::string buildId, std::string daemonStartId);
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
        if (a) {
            a->setInternalFilter([this](const std::string& id) { return registry_.isInternal(id); });
            a->setRegistryAnnotator([this](nlohmann::json& rows) { registry_.annotateDocList(rows, -1); });
            a->setCaptureGuard([this](const std::string& id, const std::function<void()>& capture) {
                auto guard = registry_.lockMutation(id);
                capture();
            });
        }
    }
    DocRegistry& registry() { return registry_; }
    // Phase 5: supervised agent children (agent_spawn/agent_stop/agent_list).
    ChildSupervisor& children() { return children_; }
    // Phase 8: stale-binary self-drain backstop. The accept-loop tick stats the
    // hostd binary itself + the loaded libchudware every CHUDWARE_HOSTD_STALECHECK_S
    // (default 60 s); an mtime change starts a soft drain so the new build
    // takes over once existing sessions finish.
    void setSelfBinary(std::string path) {
        selfBinary_ = std::move(path);
        staleBinaryTick(true);  // baseline before the accept loop and async engine load
    }
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
    // SIGTERM terminal; a soft stale-binary drain waits for current work and
    // admits only new MCP sockets from agent worker generations which already
    // owned a session when the drain began. `reason` names the cause in every
    // refusal and in daemon_status; a hard stop supersedes a soft drain.
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
    // Count of engine-side calls only (agent_admin_turn / doc_* via beginInflight).
    // Distinct from Status::inflight which counts every RPC including hello/status
    // and makes the tray show a permanent "1 in-flight" while any client polls.
    int engineInflightCount() const;


private:
    // `firstLine` is a request already read before the loop started (a hello
    // admitted during a drain); empty = none.
    void clientLoop(std::shared_ptr<ClientConn> c, std::string firstLine);
    nlohmann::json dispatch(ClientConn& c, const std::string& method,
                            const nlohmann::json& params, bool& isError);
    // Admit control, a previously-owned desktop agent worker, or another connection
    // of an already-connected GUI process on a soft stale drain. Other clients
    // receive a structured refusal before EOF.
    bool admitWhileDraining(chudware_ipc::IpcConn& conn, std::string& firstLine,
                            bool allowControl);
    // The structured `daemon_draining` error a refused client receives.
    nlohmann::json drainRefusal();
    void touch();
    bool idleExpired(int lingerSecs);
    void staleBinaryTick(bool initial = false);
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
    // Preserve authored abandoned work before closing, except on explicit
    // discard. Recheck attachment generation atomically before removal.
    bool gcCloseDoc(const std::string& docId, const char* reason = "gc",
                    int clientId = 0, const std::string& clientKind = "daemon",
                    const char* method = "gc");
    void sendEventTo(int clientId, const std::string& line);
    void sendEventToAll(const std::string& line);
    // Live agent-event streaming (transparency): push one emitted run event to a client as a
    // {"event":"agent.event","data":<ev>} notification. streamThunk is the C sink registered
    // with the engine (chudware_set_event_sink); its `ud` is a StreamCtx{this, clientId}.
    void sendAgentEvent(int clientId, const char* eventJson);
    static void streamThunk(const char* eventJson, void* ud);
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
    // Desktop agent_admin_turn calls in flight, by run_id. A turn streams only to its
    // requester; supersedeOrphanedTurns stops a conversation's turns whose requester is gone.
    // `requester` stays valid while its entry exists: the entry is erased on the requester's
    // own dispatch thread before that call returns.
    struct LiveTurn {
        std::string sessionId;
        ClientConn* requester = nullptr;
        nlohmann::json credentials;  // byok_* fields the worker's cancel matches against
    };
    std::mutex liveTurnsMtx_;
    std::map<std::string, LiveTurn> liveTurns_;
    void supersedeOrphanedTurns(const std::string& sessionId);

    chudware_ipc::IpcListener listener_;
    std::string buildId_;
    std::string daemonStartId_;
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
    // Pre-drain, daemon-owned desktop worker generations; protected by drainMtx_.
    std::vector<WorkerBridge::AgentPeer> drainAgentPeers_;
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
    std::atomic<std::uint64_t> nextAgentRunScope_{0};
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
