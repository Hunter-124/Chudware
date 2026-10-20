// Lazily-spawned, independent services-worker stdio lanes for hostd.
// Each lane owns a child, stdio channel, and mutex. A long Chudcode turn
// cannot serialize unrelated PLM/manufacturing/parts requests.
// Desktop GUI keeps its own WorkerClient; these lanes serve hostd only.
#pragma once

#include <atomic>
#include <cstddef>
#include <cstdint>
#include <functional>
#include <memory>
#include <mutex>
#include <string>
#include <utility>

#include <nlohmann/json.hpp>

#ifndef _WIN32
#include <sys/types.h>
#endif

namespace hostd {

class WorkerBridge {
public:
    static WorkerBridge& agent();
    static WorkerBridge& general();
    // Explicitly classify worker methods. Unknown methods use the general lane.
    static WorkerBridge& forMethod(const std::string& method);
    // Called synchronously for each matching streamed worker notification's
    // "data" payload while call() waits for the terminal response.
    using EventFn = std::function<void(const nlohmann::json&)>;

    // Route turns to independent agent workers, keeping each conversation single-flight.
    static bool dispatch(const std::string& method, const nlohmann::json& params,
                         nlohmann::json& result, std::string& err, int timeoutMs,
                         const EventFn& onEvent,
                         const nlohmann::json& ctx = nlohmann::json::object());

    // Send {method, params} to the services worker; block until the matching
    // response (default 120 s — plm_overview on a large project is 10–30 s; 0 means that
    // default). timeoutMs bounds the silence between response lines; < 0 waits without a
    // deadline (an agent turn with no caller budget) until the reply, child death or stop().
    //
    // Returns true on ok:true (result filled). Returns false on transport
    // failure or ok:false; err is always set on false, and result may hold a
    // structured {code,message,...} error envelope when the worker supplied one.
    bool call(const std::string& method, const nlohmann::json& params,
              nlohmann::json& result, std::string& err, int timeoutMs = 120000,
              const EventFn& onEvent = {},
              const nlohmann::json& ctx = nlohmann::json::object());

    // Attempt cancel/steer through the authenticated worker side channel only
    // when a previously-created bridge has the exact requested run active.
    // Returns true only when control succeeded and result is filled. matched
    // distinguishes an active-run transport failure from an inactive request,
    // which the caller must leave on the ordinary core control path. This never
    // constructs or starts the worker.
    static bool controlActiveRun(const std::string& action,
                                 const nlohmann::json& params, bool& matched,
                                 nlohmann::json& result, std::string& err,
                                 const nlohmann::json& ctx = nlohmann::json::object());

    // Retire only this actor/workspace's generations at or below the cutoff.
    // Newer generations must survive delayed or repeated authority updates.
    static void revokeActor(const std::string& actorKey,
                            const std::string& workspaceKey,
                            std::int64_t revokedThrough);

    // Shutdown of the child (idempotent). Called from ~WorkerBridge and daemon exit. Never
    // waits behind an in-flight call: a live agent turn is cancelled through the control lane,
    // then the child is terminated if it has not returned within a short grace.
    void stop();

    // Stop every lazily-created child without constructing an unused lane.
    static void shutdownIfCreated();

    struct Activity {
        std::uint64_t busy = 0, background = 0, unknown = 0;
    };
    // Snapshot already-created lanes only. Busy/reserved lanes never wait.
    static Activity activity();

private:
    explicit WorkerBridge(bool agentLane, bool turnWorker = false,
                          nlohmann::json identity = nlohmann::json::object())
        : identity_(std::move(identity)), agentLane_(agentLane), turnWorker_(turnWorker) {}
    ~WorkerBridge();
    WorkerBridge(const WorkerBridge&) = delete;
    WorkerBridge& operator=(const WorkerBridge&) = delete;


    struct AgentPool;
    static AgentPool& pool();
    struct ActorLanes;
    struct ActorRegistry;
    static ActorRegistry& actorRegistry();
    static std::shared_ptr<ActorLanes> actorLanes(const nlohmann::json& ctx,
                                                bool create, std::string& err);
    static bool isAgentMethod(const std::string& method);
    static void stopActorLanes(const std::shared_ptr<ActorLanes>& lanes);
    static void reapIdleExtras(AgentPool& state);
    static void releaseTurn(AgentPool& state, WorkerBridge* bridge);
    // Ask this worker to dispose its idle live runtime for one conversation (control
    // action "release") so another worker can resume it from its session file.
    bool releaseSession(const std::string& sessionId, std::string& err,
                        const nlohmann::json& ctx);
    bool spawn(std::string& err);
    void reap();  // close fds, waitpid; leaves running_=false
    bool childAlive();  // caller holds mtx_; does not discard buffered stdout
    bool transportReady() const;
    void terminateInFlightChild();  // safe without mtx_
    void stopChildLocked();  // graceful child shutdown; caller holds mtx_
    bool writeLine(const std::string& line);
    bool readLine(std::string& line, int timeoutMs, bool* malformed = nullptr,
                  std::size_t maxBytes = 64u * 1024u * 1024u);  // <0: no deadline
    bool activityLocked(std::uint64_t& active, int timeoutMs);  // never spawns/stops
    // One attempt: ensure child, write request, read id-matched response.
    // On write-fail/EOF sets err and returns false (caller may reap+retry).
    bool callOnce(const std::string& method, const nlohmann::json& params,
                  nlohmann::json& result, std::string& err, int timeoutMs,
                  const EventFn& onEvent, const nlohmann::json& ctx);
    bool fetchControlInfo(std::string& err, const nlohmann::json& ctx);
    bool controlActiveRunImpl(const std::string& action,
                              const nlohmann::json& params, bool& matched,
                              nlohmann::json& result, std::string& err,
                              const nlohmann::json& ctx);
    void clearControlState();
    void clearActiveRun();

#ifdef _WIN32
    void* childStdinWr_ = nullptr;   // HANDLE, write -> child stdin
    void* childStdoutRd_ = nullptr;  // HANDLE, read <- child stdout
    void* stdoutEvent_ = nullptr;   // one reusable OVERLAPPED completion event per lane
    void* process_ = nullptr;       // HANDLE
    void* childJob_ = nullptr;      // kill-on-close ownership of the entire worker tree
    // Protect handle teardown against stop() terminating an in-flight generation.
    std::mutex nativeMtx_;
#else
    int childStdinWr_ = -1;   // write -> child stdin
    int childStdoutRd_ = -1;  // read  <- child stdout
    pid_t process_ = -1;
#endif
    // Generation presence without the stdio lock: release must stay off busy stdio.
    std::atomic<long> livePid_{-1};
    std::string readBuf_;
    int nextId_ = 1;
    bool running_ = false;
    int pendingActivityId_ = -1;  // retained across bounded probe timeouts
    bool discardingLine_ = false;
    // Resolved once on first spawn. Packaged builds use only the canonical adjacent worker;
    // developer builds retain CHUDWARE_WORKER_BIN / CHUDWARE_PYTHON source execution.
    std::string workerBin_;
    std::string repo_;
    bool compiledWorker_ = false;
    bool pathsResolved_ = false;
    struct ControlInfo {
        std::string transport;
        std::string endpoint;
        std::string token;
    };
    std::mutex controlMtx_;
    ControlInfo control_;
    std::string activeWorkerRunId_;
    nlohmann::json activeCtx_ = nlohmann::json::object();

    const nlohmann::json identity_;

    const bool agentLane_;
    const bool turnWorker_;
    // Timed: a call waits for the lane no longer than its own timeout (see call()). A plain
    // lock let an agent-lane call queue behind an hours-long agent_admin_turn until hostd's
    // long-tier watchdog killed the whole daemon (2026-09-27, subscription_accounts).
    std::timed_mutex mtx_;
    // Set once stop() begins: an in-flight call that loses its child must not respawn one.
    std::atomic<bool> stopping_{false};
    static std::atomic<bool> agentCreated_;
    static std::atomic<bool> generalCreated_;
};

}  // namespace hostd
