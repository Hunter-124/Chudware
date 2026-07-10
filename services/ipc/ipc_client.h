// EngineClient — a client connection to the shared engine-host daemon.
//
// Drop-in sibling of core::CppCore (services/worker/cpp_core.h): same blocking
// call() contract, so the GUI's routing seam (worker_client.cpp) and tests can
// swap backends without signature churn. Adds connect-or-spawn discovery and a
// queue of daemon push events (doc.changed etc., drained by the GUI per frame).
//
// Threading: call() is serialized by an internal mutex (one outstanding request
// per connection, matching the daemon's v1 contract). Event lines that arrive
// while waiting for a response are queued; drainEvents() also does a 0 ms poll
// so pushes arrive even when the client is idle.
#pragma once

#include <mutex>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

#include "ipc_transport.h"

namespace aicad_ipc {

class EngineClient {
public:
    struct Options {
        std::string hostdBin;   // spawn candidate; "" = env/discovery only
        std::string clientKind = "test";  // "gui" | "mcp" | "agent" | "test"
        int spawnBudgetMs = 15000;        // total connect-or-spawn budget
        bool allowSpawn = true;           // false = attach-only (never spawn)
    };

    // Connect to the daemon for this instance key, spawning one if absent (and
    // allowed). Performs the hello handshake. False + err on failure.
    bool connectOrSpawn(const Options& opts, std::string& err);

    bool connected() const { return conn_.valid() && !conn_.eof(); }
    void disconnect() { conn_.close(); }

    // The hello response: {proto, abi, engine_path, git_rev, daemon_pid, state, id_remap}.
    const nlohmann::json& serverInfo() const { return serverInfo_; }

    // Blocking JSON-RPC round trip; same contract as CppCore::call. Returns true
    // iff the daemon answered with "result"; err carries transport or RPC errors
    // ("error" payloads are also copied into result for callers that inspect them).
    bool call(const std::string& method, const nlohmann::json& params,
              nlohmann::json& result, std::string& err);

    // Move any queued push events out (non-blocking; also polls the socket).
    std::vector<nlohmann::json> drainEvents();

private:
    bool tryConnect(std::string& err);
    bool hello(const std::string& kind, std::string& err);
    // Read lines until a response with `id` arrives; queue event lines seen on the way.
    bool readResponse(int id, nlohmann::json& out, std::string& err);

    IpcConn conn_;
    std::mutex mtx_;                     // serialize call()s + event queue access
    std::vector<nlohmann::json> events_;
    nlohmann::json serverInfo_;
    std::string sockPath_;
    int nextId_ = 1;
};

}  // namespace aicad_ipc
