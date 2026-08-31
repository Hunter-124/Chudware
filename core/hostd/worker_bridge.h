// Lazily-spawned services worker bridge for chudware-hostd.
//
// On web the browser talks only to hostd (via the gateway websocket). Methods the
// C++ engine does not implement (plm_*, git_*, ai_roles_*, provider accounts, …)
// live in the services worker. This bridge owns ONE long-lived subprocess and
// forwards those engine-unknown RPCs over stdio JSON-RPC — the same protocol
// services/worker/worker_client.cpp uses on desktop.
//
// Desktop GUI behaviour is unchanged: it still owns its own WorkerClient. This
// path is the hostd fallback only.
//
// Threading: call() is safe from any client-dispatch thread; a single mutex
// serialises the one stdio channel. Callers must NOT hold a global/server lock
// across call() — a slow plm_overview (~10–30 s) must only block the requesting
// connection's thread.
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
    static WorkerBridge& instance();
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

    // Best-effort shutdown of the child (idempotent). Called from ~WorkerBridge.
    void stop();

    // Stops the singleton's child WITHOUT constructing it if instance() was never called —
    // safe to call unconditionally from hostd's clean-shutdown tail after server.run()
    // returns (does NOT lazily spin up a worker process just to shut it down).
    static void shutdownIfCreated();

private:
    WorkerBridge() = default;
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

    std::mutex mtx_;

    // Set true the first time instance() runs; lets shutdownIfCreated() avoid constructing
    // the function-local static singleton just to no-op stop() on it.
    static std::atomic<bool> created_;
};

}  // namespace hostd
