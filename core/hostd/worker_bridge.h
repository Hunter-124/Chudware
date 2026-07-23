// Lazily-spawned Python worker bridge for chudware-hostd.
//
// On web the browser talks only to hostd (via the gateway websocket). Methods the
// C++ engine does not implement (plm_*, git_*, ai_roles_*, provider accounts, …)
// live in `python -m services.worker_main`. This bridge owns ONE long-lived worker
// subprocess and forwards those engine-unknown RPCs over stdio JSON-RPC — the
// same protocol services/worker/worker_client.cpp uses on desktop.
//
// Desktop GUI behaviour is unchanged: it still owns its own WorkerClient. This
// path is the hostd fallback only.
//
// Threading: call() is safe from any client-dispatch thread; a single mutex
// serialises the one stdio channel. Callers must NOT hold a global/server lock
// across call() — a slow plm_overview (~10–30 s) must only block the requesting
// connection's thread.
#pragma once

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

    // Send {method, params} to the Python worker; block until the matching
    // response (default 120 s — plm_overview on a large project is 10–30 s).
    //
    // Returns true on ok:true (result filled). Returns false on transport
    // failure or ok:false; err is always set on false, and result may hold a
    // structured {code,message,...} error envelope when the worker supplied one.
    bool call(const std::string& method, const nlohmann::json& params,
              nlohmann::json& result, std::string& err, int timeoutMs = 120000);

    // Best-effort shutdown of the child (idempotent). Called from ~WorkerBridge.
    void stop();

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
                  nlohmann::json& result, std::string& err, int timeoutMs);

    int childStdinWr_ = -1;   // write -> child stdin
    int childStdoutRd_ = -1;  // read  <- child stdout
    pid_t process_ = -1;
    std::string readBuf_;
    int nextId_ = 1;
    bool running_ = false;
    // Resolved once on first spawn.
    std::string python_;
    std::string repo_;
    bool pathsResolved_ = false;
#endif

    std::mutex mtx_;
};

}  // namespace hostd
