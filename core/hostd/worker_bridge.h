// Lazily-spawned, independent services-worker stdio lanes for hostd.
// Each lane owns a child, stdio channel, and mutex. A long Chudcode turn
// cannot serialize unrelated PLM/manufacturing/parts requests.
// Desktop GUI keeps its own WorkerClient; these lanes serve hostd only.
#pragma once

#include <atomic>
#include <functional>
#include <mutex>
#include <string>

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

    // Send {method, params} to the services worker; block until the matching
    // response (default 120 s — plm_overview on a large project is 10–30 s).
    //
    // Returns true on ok:true (result filled). Returns false on transport
    // failure or ok:false; err is always set on false, and result may hold a
    // structured {code,message,...} error envelope when the worker supplied one.
    bool call(const std::string& method, const nlohmann::json& params,
              nlohmann::json& result, std::string& err, int timeoutMs = 120000,
              const EventFn& onEvent = {});

    // Attempt cancel/steer through the authenticated worker side channel only
    // when a previously-created bridge has the exact requested run active.
    // Returns true only when control succeeded and result is filled. matched
    // distinguishes an active-run transport failure from an inactive request,
    // which the caller must leave on the ordinary core control path. This never
    // constructs or starts the worker.
    static bool controlActiveRun(const std::string& action,
                                 const nlohmann::json& params, bool& matched,
                                 nlohmann::json& result, std::string& err);

    // Shutdown of the child (idempotent). Called from ~WorkerBridge and daemon exit. Never
    // waits behind an in-flight call: a live agent turn is cancelled through the control lane,
    // then the child is terminated if it has not returned within a short grace.
    void stop();

    // Stop both lazily-created children without constructing an unused lane.
    static void shutdownIfCreated();

private:
    explicit WorkerBridge(bool agentLane) : agentLane_(agentLane) {}
    ~WorkerBridge();
    WorkerBridge(const WorkerBridge&) = delete;
    WorkerBridge& operator=(const WorkerBridge&) = delete;

#ifndef _WIN32
    bool spawn(std::string& err);
    void reap();  // close fds, waitpid; leaves running_=false
    bool writeLine(const std::string& line);
    bool readLine(std::string& line, int timeoutMs);
    // One attempt: ensure child, write request, read id-matched response.
    // On write-fail/EOF sets err and returns false (caller may reap+retry).
    bool callOnce(const std::string& method, const nlohmann::json& params,
                  nlohmann::json& result, std::string& err, int timeoutMs,
                  const EventFn& onEvent);
    bool fetchControlInfo(std::string& err);
    bool controlActiveRunImpl(const std::string& action,
                              const nlohmann::json& params, bool& matched,
                              nlohmann::json& result, std::string& err);
    void clearControlState();
    void clearActiveRun();

    int childStdinWr_ = -1;   // write -> child stdin
    int childStdoutRd_ = -1;  // read  <- child stdout
    pid_t process_ = -1;
    // Mirror of process_ readable WITHOUT mtx_: stop() must be able to end a child whose
    // in-flight call holds the lane for hours. Updated at every process_ assignment.
    std::atomic<pid_t> livePid_{-1};
    std::string readBuf_;
    int nextId_ = 1;
    bool running_ = false;
    // Resolved once on first spawn. Packaged builds use only the canonical adjacent worker;
    // developer builds retain CHUDWARE_WORKER_BIN / CHUDWARE_PYTHON source execution.
    std::string workerBin_;
    std::string repo_;
    bool compiledWorker_ = false;
    bool pathsResolved_ = false;
#endif
    struct ControlInfo {
        std::string transport;
        std::string endpoint;
        std::string token;
    };
    std::mutex controlMtx_;
    ControlInfo control_;
    std::string activeWorkerRunId_;

    const bool agentLane_;
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
