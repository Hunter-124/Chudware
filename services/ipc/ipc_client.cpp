#ifndef _WIN32

#include "ipc_client.h"

#include <chrono>
#include <thread>

#include <unistd.h>

#include "ipc_discovery.h"

namespace aicad_ipc {

using nlohmann::json;

bool EngineClient::tryConnect(std::string& err) {
    conn_ = connectTo(sockPath_, err);
    return conn_.valid();
}

bool EngineClient::hello(const std::string& kind, std::string& err) {
    json params = {{"client", kind},
                   {"pid", (long)::getpid()},
                   {"proto", 1}};
    json result;
    // Engine may still be loading; hello is a daemon-level method and answers
    // immediately with state=loading|ready — that's fine for the handshake.
    if (!call("hello", params, result, err)) return false;
    serverInfo_ = result;
    return true;
}

bool EngineClient::connectOrSpawn(const Options& opts, std::string& err) {
    const std::string key = instanceKey(opts.hostdBin);
    sockPath_ = socketPath(key);
    if (sockPath_.empty()) {
        err = "no runtime dir for hostd socket";
        return false;
    }
    std::string firstErr;
    if (tryConnect(firstErr)) return hello(opts.clientKind, err);

    if (!opts.allowSpawn) {
        err = "hostd not running (" + firstErr + ")";
        return false;
    }
    const std::string bin = resolveHostdBin(opts.hostdBin);
    if (bin.empty()) {
        err = "hostd binary not found (set AICAD_HOSTD_BIN); connect failed: " + firstErr;
        return false;
    }
    if (!spawnDetached(bin, "", err)) return false;

    // Retry-connect: 50 ms doubling capped at 500 ms, until the budget runs out.
    // The daemon binds BEFORE loading the engine, so this only needs to cover
    // process start + bind, not the (slow) OCCT load.
    const auto deadline =
        std::chrono::steady_clock::now() + std::chrono::milliseconds(opts.spawnBudgetMs);
    int backoff = 50;
    for (;;) {
        std::string e;
        if (tryConnect(e)) return hello(opts.clientKind, err);
        if (std::chrono::steady_clock::now() >= deadline) {
            err = "hostd spawn timed out: " + e;
            return false;
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(backoff));
        backoff = std::min(backoff * 2, 500);
    }
}

bool EngineClient::readResponse(int id, json& out, std::string& err) {
    std::string line;
    for (;;) {
        if (!conn_.readLine(line, -1)) {
            err = conn_.eof() ? "daemon connection closed" : "daemon read failed";
            return false;
        }
        json msg = json::parse(line, nullptr, /*allow_exceptions=*/false);
        if (msg.is_discarded()) continue;  // tolerate stray non-JSON lines
        if (msg.contains("event")) {
            events_.push_back(std::move(msg));
            continue;
        }
        if (msg.value("id", -1) == id) {
            out = std::move(msg);
            return true;
        }
        // A response for another id can't happen with one-outstanding-request;
        // drop it rather than deadlock if the daemon misbehaves.
    }
}

bool EngineClient::call(const std::string& method, const json& params, json& result,
                        std::string& err) {
    std::lock_guard<std::mutex> lk(mtx_);
    if (!conn_.valid() || conn_.eof()) {
        err = "not connected to hostd";
        return false;
    }
    const int id = nextId_++;
    json req = {{"id", id}, {"method", method}, {"params", params}};
    if (!conn_.writeLine(req.dump())) {
        err = "daemon write failed";
        return false;
    }
    json resp;
    if (!readResponse(id, resp, err)) return false;
    if (resp.contains("result")) {
        result = resp["result"];
        return true;
    }
    result = resp.value("error", json::object());
    err = result.is_object() ? result.value("message", result.dump()) : result.dump();
    return false;
}

std::vector<json> EngineClient::drainEvents() {
    // try_lock: the GUI thread drains per frame while call()s run on the async
    // worker thread; a long solve holding mtx_ must never stall the render loop.
    // Events arriving mid-call are queued by readResponse and picked up on the
    // first uncontended frame after it returns.
    std::unique_lock<std::mutex> lk(mtx_, std::try_to_lock);
    if (!lk.owns_lock()) return {};
    // Non-blocking poll for pushes that arrived while idle.
    std::string line;
    while (conn_.valid() && conn_.readLine(line, 0)) {
        json msg = json::parse(line, nullptr, false);
        if (!msg.is_discarded() && msg.contains("event")) events_.push_back(std::move(msg));
        // Orphan responses (no waiter) are dropped.
    }
    std::vector<json> out;
    out.swap(events_);
    return out;
}

}  // namespace aicad_ipc

#endif  // !_WIN32
