// core/agent/omp_bridge_client.h — long-lived bun bridge sidecar client
// (vendor/omp/bridge/src/bridge.ts).
//
// core/agent/providers.cpp::complete() forwards OMP-backed providers (see
// is_omp_bridge_provider() in providers.h) here instead of building a native libcurl/WinHTTP
// request. This owns ONE `bun run <repo>/vendor/omp/bridge/src/bridge.ts` child process and
// speaks the newline-delimited JSON-RPC protocol documented at the top of bridge.ts:
//   -> {"id": <int>, "method": <string>, "params": <object>}
//   <- {"id": <int>, "ok": true,  "result": <object>}
//   <- {"id": <int>, "ok": false, "error": {"message": <string>, "status"?: <number>}}
// stdout carries ONLY response lines; the child's own stderr logging is left connected to this
// process's stderr (nothing redirected/discarded).
//
// Mirrors core/hostd/worker_bridge.{h,cpp} (the C++ core's existing Python-worker stdio
// bridge) deliberately: a fork+pipe POSIX child, id-correlated blocking call(), one mutex
// serializing the single stdio channel, reap-and-respawn-once on a dead child. Windows is
// currently an "unavailable" stub, matching worker_bridge's own Windows branch — the bun
// bridge does not yet have a CreateProcess-based child transport.
#pragma once

#include <mutex>
#include <string>

#include <nlohmann/json.hpp>

#ifndef _WIN32
#include <sys/types.h>
#endif

namespace aicad {
namespace agent {

class OmpBridgeClient {
public:
    static OmpBridgeClient& instance();

    // Send {method, params} to the bun bridge; block until the matching response (default
    // 120 s, matching worker_bridge's default — a non-streaming `complete` round-trip through
    // a reasoning model can legitimately take a while; providers.cpp passes an explicit larger
    // timeout for that call).
    //
    // Returns true on ok:true (result filled with the bridge's `result` object). Returns false
    // on transport failure or ok:false; err is always set on false, and result carries the
    // bridge's structured {message,status?} error object when it supplied one.
    bool call(const std::string& method, const nlohmann::json& params, nlohmann::json& result,
              std::string& err, int timeoutMs = 120000);

    // Best-effort shutdown of the child (idempotent). Called from ~OmpBridgeClient.
    void stop();

private:
    OmpBridgeClient() = default;
    ~OmpBridgeClient();
    OmpBridgeClient(const OmpBridgeClient&) = delete;
    OmpBridgeClient& operator=(const OmpBridgeClient&) = delete;

#ifndef _WIN32
    // Resolve bunExe_/bridgeScript_ once (env overrides, else repo-root discovery). Caller
    // holds mtx_.
    bool resolvePaths(std::string& err);
    bool spawn(std::string& err);
    void reap();  // close fds, waitpid; leaves running_=false
    bool writeLine(const std::string& line);
    bool readLine(std::string& line, int timeoutMs);
    // One attempt: ensure child, write request, read id-matched response. On write-fail/EOF
    // sets err and returns false (caller may reap+retry).
    bool callOnce(const std::string& method, const nlohmann::json& params, nlohmann::json& result,
                  std::string& err, int timeoutMs);

    int childStdinWr_ = -1;   // write -> child stdin
    int childStdoutRd_ = -1;  // read  <- child stdout
    pid_t process_ = -1;
    std::string readBuf_;
    int nextId_ = 1;
    bool running_ = false;
    // Resolved once on first spawn. Defaults to a PATH-searched "bun" (or a common
    // curl-installer location) and <repo root>/vendor/omp/bridge/src/bridge.ts; both are
    // overridable (AI_CAD_BUN, AI_CAD_OMP_BRIDGE) for checkouts/installs that don't sit under
    // $HOME/Repositories/CAD or don't have bun on PATH.
    std::string bunExe_;
    std::string bridgeScript_;
    bool pathsResolved_ = false;
#endif

    std::mutex mtx_;
};

}  // namespace agent
}  // namespace aicad
