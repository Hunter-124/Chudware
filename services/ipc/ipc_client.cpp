#include "ipc_client.h"

#include <chrono>
#include <cstdio>
#include <filesystem>
#include <map>
#include <thread>

#ifdef _WIN32
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>

#include <process.h>  // _getpid
#else
#include <csignal>
#include <unistd.h>
#endif

#ifdef __EMSCRIPTEN__
#include <emscripten.h>            // emscripten_sleep
#include <emscripten/threading.h>  // emscripten_is_main_runtime_thread
#endif

#include "ipc_discovery.h"

namespace {
long selfPid() {
#ifdef _WIN32
    return (long)_getpid();
#else
    return (long)::getpid();
#endif
}

// True once `pid` no longer exists (drained daemon finished exiting).
bool pidGone(long pid) {
    if (pid <= 0) return true;
#ifdef _WIN32
    HANDLE h = OpenProcess(SYNCHRONIZE, FALSE, (DWORD)pid);
    if (!h) return true;
    CloseHandle(h);
    return false;
#else
    return ::kill((pid_t)pid, 0) == -1 && errno == ESRCH;
#endif
}

// Build id of the hostd binary ON DISK: `aicad-hostd --version` prints
// "aicad-hostd <git-describe id>". One subprocess per (path, mtime) — cached, so
// steady-state reconnects never exec anything. "" on any failure (callers treat
// unknown as not-stale). This is the client half of the version drain-replace:
// hello.git_rev (the RUNNING daemon's configure-time id) vs what a fresh spawn
// of the on-disk binary would report.
std::string diskBuildId(const std::string& bin) {
#ifdef __EMSCRIPTEN__
    (void)bin;
    return "";  // no hostd binary / no popen in the browser
#else
    if (bin.empty()) return "";
    std::error_code ec;
    const auto t = std::filesystem::last_write_time(bin, ec);
    const long long mtime =
        ec ? 0
           : std::chrono::duration_cast<std::chrono::milliseconds>(t.time_since_epoch())
                 .count();
    static std::mutex m;
    static std::map<std::string, std::pair<long long, std::string>> cache;
    {
        std::lock_guard<std::mutex> lk(m);
        auto it = cache.find(bin);
        if (it != cache.end() && it->second.first == mtime) return it->second.second;
    }
    std::string out;
#ifdef _WIN32
    FILE* f = _popen(("\"" + bin + "\" --version").c_str(), "r");
#else
    FILE* f = ::popen(("'" + bin + "' --version 2>/dev/null").c_str(), "r");
#endif
    if (f) {
        char buf[256] = {0};
        if (std::fgets(buf, sizeof buf, f)) out = buf;
#ifdef _WIN32
        _pclose(f);
#else
        ::pclose(f);
#endif
    }
    while (!out.empty() && (out.back() == '\n' || out.back() == '\r' || out.back() == ' '))
        out.pop_back();
    const size_t sp = out.find_last_of(" \t");
    if (sp != std::string::npos) out = out.substr(sp + 1);
    std::lock_guard<std::mutex> lk(m);
    cache[bin] = {mtime, out};
    return out;
#endif  // !__EMSCRIPTEN__
}
}  // namespace

namespace aicad_ipc {

using nlohmann::json;

bool EngineClient::tryConnect(std::string& err) {
    conn_ = connectTo(sockPath_, err);
    return conn_.valid();
}

bool EngineClient::hello(const std::string& kind, std::string& err) {
    json params = {{"client", kind},
                   {"pid", selfPid()},
                   {"proto", 1}};
    // Optional multi-user identity — only when the client set Options.userId.
    if (!userId_.empty())
        params["user"] = {{"id", userId_}, {"display", userDisplay_}};
    json result;
#ifdef __EMSCRIPTEN__
    // Browser: never block first paint forever if the gateway is slow/silent.
    // maintainConnection retries with backoff when hello fails.
    const int timeoutMs = 5000;
#else
    const int timeoutMs = (kind == "control") ? 2000 : -1;
#endif
    if (!call("hello", params, result, err, timeoutMs)) return false;
    serverInfo_ = result;
    return true;
}

bool EngineClient::connectOrSpawn(const Options& opts, std::string& err) {
    if (!connectInner(opts, err)) return false;
    if (!opts.allowSpawn) return true;  // control clients (--stop/--status) never drain

    // Version drain-replace (Phase 8): the running daemon predates the hostd
    // binary on disk -> ask it to drain (autosaves + exits), wait for exit, then
    // respawn/reconnect so everyone lands on the new build. A refusal
    // (mission_active) is a warn-and-continue: never kill running agent work.
    const std::string running = serverInfo_.value("git_rev", std::string());
    const std::string disk = diskBuildId(resolveHostdBin(opts.hostdBin));
    if (running.empty() || disk.empty() || running == "unknown" || disk == "unknown" ||
        running == disk)
        return true;
    nlohmann::json r;
    std::string e;
    if (!call("daemon_drain", nlohmann::json::object(), r, e)) {
        std::fprintf(stderr,
                     "[ipc] hostd on disk is %s but the running daemon is %s; drain "
                     "refused (%s) — continuing on the old daemon\n",
                     disk.c_str(), running.c_str(), e.c_str());
        return true;
    }
    std::fprintf(stderr,
                 "[ipc] hostd stale (running %s, on disk %s) — drained; respawning the "
                 "new build\n",
                 running.c_str(), disk.c_str());
    const long oldPid = serverInfo_.value("daemon_pid", 0L);
    disconnect();
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(30);
    while (!pidGone(oldPid) && std::chrono::steady_clock::now() < deadline)
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
    return connectInner(opts, err);
}

bool EngineClient::connectInner(const Options& opts, std::string& err) {
    const std::string key = instanceKey(opts.hostdBin);
    clientKind_ = opts.clientKind;
    userId_ = opts.userId;
    userDisplay_ = opts.userDisplay;
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

bool EngineClient::readResponse(int id, json& out, std::string& err, int timeoutMs,
                                const std::function<void(const json&)>& onEvent) {
    // One deadline for the WHOLE response wait: event lines arriving on the way
    // must not restart the clock.
    const auto deadline = std::chrono::steady_clock::now() +
                          std::chrono::milliseconds(timeoutMs >= 0 ? timeoutMs : 0);
    std::string line;
    for (;;) {
        int wait = -1;
        if (timeoutMs >= 0) {
            const auto left = std::chrono::duration_cast<std::chrono::milliseconds>(
                                  deadline - std::chrono::steady_clock::now())
                                  .count();
            wait = left > 0 ? (int)left : 0;
        }
        if (!conn_.readLine(line, wait)) {
            if (!conn_.eof() && timeoutMs >= 0) {
                // Timed out with the peer still alive: the response is still in
                // flight, so this connection is mid-stream and unusable — close
                // it; the next call() reconnects cleanly instead of reading a
                // stale reply. err-only (no {code} in result), same transport-
                // error shape as a connection failure, so callers toast it.
                err = "engine call timed out after " + std::to_string(timeoutMs) + "ms";
                conn_.close();
                return false;
            }
            err = conn_.eof() ? "daemon connection closed" : "daemon read failed";
            return false;
        }
        json msg = json::parse(line, nullptr, /*allow_exceptions=*/false);
        if (msg.is_discarded()) continue;  // tolerate stray non-JSON lines
        if (msg.contains("event")) {
            // Live agent-event streaming: fire onEvent NOW (mid-call) for agent.event so a
            // blocking agent run streams instead of buffering until it returns. Other events
            // (doc.*) still queue for the GUI-thread drainEvents().
            if (onEvent && msg.value("event", std::string()) == "agent.event") {
                try { onEvent(msg); } catch (...) {}
            } else {
                events_.push_back(std::move(msg));
            }
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

// Reconnect after a timeout/EOF invalidated the connection (mtx_ held).
// Connect-only, never spawns: a timed-out daemon is usually alive-but-busy, and
// hello is a daemon-level method that answers even mid-solve. C++ equivalent of
// daemon_bridge.py DaemonClient.call's reconnect-on-EOF.
bool EngineClient::reconnectLocked(std::string& err) {
    if (sockPath_.empty()) {  // connectOrSpawn never ran (or never succeeded)
        err = "not connected to hostd";
        return false;
    }
    conn_.close();
    std::string cerr;
    if (!tryConnect(cerr)) {
        err = "not connected to hostd (reconnect failed: " + cerr + ")";
        return false;
    }
    // Inline hello — hello() goes through call(), which would re-lock mtx_.
    // Bounded: a reconnect must never reintroduce an unbounded wait.
    const int id = nextId_++;
    json hparams = {{"client", clientKind_}, {"pid", selfPid()}, {"proto", 1}};
    if (!userId_.empty())
        hparams["user"] = {{"id", userId_}, {"display", userDisplay_}};
    json req = {{"id", id},
                {"method", "hello"},
                {"params", hparams}};
    if (!conn_.writeLine(req.dump())) {
        err = "daemon write failed (reconnect hello)";
        return false;
    }
    json resp;
    if (!readResponse(id, resp, err, /*timeoutMs=*/5000)) return false;
    if (!resp.contains("result")) {
        err = "hello failed on reconnect";
        conn_.close();
        return false;
    }
    serverInfo_ = resp["result"];
    // Signal subscribers (GUI event bootstrap) that per-connection daemon state
    // — event subscriptions above all — was just reset by this new connection.
    reconnectGen_.fetch_add(1, std::memory_order_relaxed);
    return true;
}

bool EngineClient::tryReconnect(std::string& err) {
    std::lock_guard<std::mutex> lk(mtx_);
    if (conn_.valid() && !conn_.eof()) return true;
    return reconnectLocked(err);
}

// Non-blocking twin of tryReconnect for the wasm render thread — see the header.
// try_to_lock: a failed acquire means a call() is mid-flight; return false so the
// frame loop skips this tick (keeping the browser event loop alive) instead of
// hard-blocking, which would deadlock against the WS onclose that must run on this
// same thread. Bootstraps sockPath_ so it also serves the very first connect.
bool EngineClient::tryReconnectNonBlocking(std::string& err, bool* deferred) {
    std::unique_lock<std::mutex> lk(mtx_, std::try_to_lock);
    if (!lk.owns_lock()) {
        err = "reconnect deferred (engine call in flight)";
        if (deferred) *deferred = true;
        return false;
    }
    if (conn_.valid() && !conn_.eof()) return true;
    if (sockPath_.empty()) {
        // First use: establish the instance key + socket path reconnectLocked needs.
        // The wasm transport ignores the path (it dials the gateway WS) but
        // reconnectLocked guards on a non-empty sockPath_. clientKind_ is "gui";
        // the gateway asserts the real per-user identity into the hello anyway.
        clientKind_ = "gui";
        sockPath_ = socketPath(instanceKey(std::string()));
        if (sockPath_.empty()) sockPath_ = "ws://engine";  // sentinel; path unused in wasm
    }
    return reconnectLocked(err);
}

bool EngineClient::pollAlive() {
    // try_lock: same rule as drainEvents — never block the GUI behind a long RPC.
    std::unique_lock<std::mutex> lk(mtx_, std::try_to_lock);
    if (!lk.owns_lock()) return conn_.valid() && !conn_.eof();
    if (!conn_.valid() || conn_.eof()) return false;
    // 0 ms poll: if the peer died, readLine sets eof_ on HUP/read-0.
    // Any push events that arrive are queued for the next drainEvents.
    std::string line;
    while (conn_.readLine(line, 0)) {
        json msg = json::parse(line, nullptr, false);
        if (!msg.is_discarded() && msg.contains("event")) events_.push_back(std::move(msg));
    }
    return conn_.valid() && !conn_.eof();
}

bool EngineClient::call(const std::string& method, const json& params, json& result,
                        std::string& err, int timeoutMs,
                        const std::function<void(const json&)>& onEvent) {
#ifdef __EMSCRIPTEN__
    // Browser MAIN thread: a hard mutex wait would busy-block the event loop, so the
    // AsyncRpc-worker call currently holding mtx_ can never receive its WS onmessage to
    // finish and release — permanent deadlock (the "drag the arrow → page freezes/crashes"
    // class: an interactive main-thread sync call — checkpoint/push_pull probe/revert —
    // contends mtx_ with an in-flight preview). Spin with emscripten_sleep so the event
    // loop keeps pumping; the worker drains and releases, then we acquire. Worker/other
    // threads (which do NOT own the event loop) still hard-block normally.
    std::unique_lock<std::mutex> lk(mtx_, std::defer_lock);
    if (emscripten_is_main_runtime_thread()) {
        while (!lk.try_lock()) emscripten_sleep(1);
    } else {
        lk.lock();
    }
#else
    std::lock_guard<std::mutex> lk(mtx_);
#endif
    if ((!conn_.valid() || conn_.eof()) && !reconnectLocked(err)) return false;
    const int id = nextId_++;
    json req = {{"id", id}, {"method", method}, {"params", params}};
    if (!conn_.writeLine(req.dump())) {
        err = "daemon write failed";
        return false;
    }
    json resp;
    if (!readResponse(id, resp, err, timeoutMs, onEvent)) return false;
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
