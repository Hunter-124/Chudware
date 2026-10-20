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

// Build id of the hostd binary ON DISK: `chudware-hostd --version` prints
// "chudware-hostd <git-describe id>". One subprocess per (path, mtime) — cached, so
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

// Runs `f` when the scope ends, on every return path.
template <class F>
struct OnExit {
    F f;
    ~OnExit() { f(); }
};
template <class F>
OnExit<F> onExit(F f) {
    return OnExit<F>{std::move(f)};
}
}  // namespace

namespace chudware_ipc {

using nlohmann::json;

bool EngineClient::usesBackgroundConnection(const std::string& method,
                                            const json& params) const {
    if (clientKind_ != "gui" || std::this_thread::get_id() == primaryThread_ ||
        (params.is_object() && params.contains("doc_id")))
        return false;
    // Server::clientLoop dispatches synchronously: sharing one socket would still
    // queue the GUI behind cold worker/provider calls even without a client mutex.
    // These exact names are document-free catalogs/status (worker_rpc.py's
    // subscription, Chudcode, skills and prompt handlers; fw_projects/status and
    // engine capability queries). Do NOT generalize to prefixes: unknown methods,
    // mints, document reads/edits and all registry/hold/subscription RPCs retain the
    // primary client identity, including its ro/CAS and edit-hold semantics.
    static constexpr const char* queries[] = {
        "ping", "daemon_status", "worker_control_info", "agent_list_models",
        "subscription_accounts", "subscription_status", "subscription_usage",
        "chudcode_provider_list", "chudcode_auth_state", "chudcode_models_list",
        "chudcode_session_stats", "chudcode_stt_status", "skills_list", "skills_get",
        "ai_prompt_baseline", "ai_tool_catalog", "fw_projects", "fw_status",
        "em_status", "sim_caps", "topology_capabilities", "topology_dfam"
    };
    for (const char* name : queries)
        if (method == name) return true;
    return false;
}

EngineClient* EngineClient::backgroundClient() {
    std::lock_guard<std::mutex> lk(backgroundMtx_);
    auto& slot = backgroundClients_[std::this_thread::get_id()];
    if (!slot) {
        slot = std::make_unique<EngineClient>();
        slot->sockPath_ = sockPath_;
        slot->clientKind_ = "gui-background";
        slot->userId_ = userId_;
        slot->userDisplay_ = userDisplay_;
        // The primary socket is the GUI's event subscriber. Hostd also broadcasts
        // on private sockets; retaining those duplicates would leak an idle queue.
        slot->discardEvents_ = true;
    }
    // No I/O under backgroundMtx_: each calling thread connects/handshakes/replies
    // on its own socket and mutex. A side EOF/timeout cannot invalidate the primary
    // socket, advance its reconnectGeneration or change the unreachable chip.
    // gui-background never pins an idle daemon like gui (Server::pinningClients).
    return slot.get();
}

void EngineClient::pollBackgroundClients() {
    std::unique_lock<std::mutex> lk(backgroundMtx_, std::try_to_lock);
    if (!lk.owns_lock()) return;
    for (const auto& entry : backgroundClients_) entry.second->pollAlive();
}

void EngineClient::closeConnection() {
    std::lock_guard<std::mutex> lk(socketMtx_);
    connected_.store(false, std::memory_order_release);
    conn_.close();
}

void EngineClient::abortPending() {
    abort_.store(true, std::memory_order_release);
    connected_.store(false, std::memory_order_release);
    {
        // shutdown wakes an active read without modifying its read buffer/handle.
        // The read owner closes it; idle sockets can be closed here immediately.
        std::lock_guard<std::mutex> lk(socketMtx_);
        conn_.shutdown();
    }
    {
        std::unique_lock<std::mutex> lk(mtx_, std::try_to_lock);
        if (lk.owns_lock()) closeConnection();
    }
    std::lock_guard<std::mutex> lk(backgroundMtx_);
    for (const auto& entry : backgroundClients_) entry.second->abortPending();
}

void EngineClient::disconnect() {
    // Drop the entire logical GUI client, not just its primary/event connection.
    abortPending();
}

bool EngineClient::tryConnect(std::string& err) {
    std::lock_guard<std::mutex> lk(socketMtx_);
    conn_ = connectTo(sockPath_, err);
    connected_.store(conn_.valid(), std::memory_order_release);
    if (conn_.valid()) {
        // Fresh socket after abortPending() — allow subsequent call()s again.
        abort_.store(false, std::memory_order_release);
        return true;
    }
    return false;
}

json EngineClient::helloError() const {
    std::lock_guard<std::mutex> lk(helloErrMtx_);
    return helloError_;
}

void EngineClient::setHelloError(json error) {
    std::lock_guard<std::mutex> lk(helloErrMtx_);
    helloError_ = std::move(error);
}

// hostd closes the connection after it refuses hello (a draining daemon refuses every
// new non-control client: Server::admitWhileDraining), so this connection ends too. err
// names the code, the message and the hint, in the same form as daemon_bridge.py.
void EngineClient::refuseHello(const json& error, std::string& err) {
    closeConnection();
    const auto text = [&error](const char* key) {
        const auto it = error.find(key);
        return it != error.end() && it->is_string() ? it->get<std::string>() : std::string();
    };
    const std::string code = text("code");
    const std::string message = text("message");
    const std::string hint = text("hint");
    err = "hostd refused hello (" + (code.empty() ? std::string("error") : code) + ")";
    if (!message.empty()) err += ": " + message;
    if (!hint.empty()) err += " " + hint;
}

bool EngineClient::hello(const std::string& kind, std::string& err, json& refusal) {
    json params = {{"client", kind},
                   {"pid", selfPid()},
                   {"proto", 1}};
    // Optional multi-user identity — only when the client set Options.userId.
    if (!userId_.empty())
        params["user"] = {{"id", userId_}, {"display", userDisplay_}};
    json result;
    // Bounded on every platform: a daemon that accepts but never answers hello must
    // fail the connect (callers fall back or retry) rather than wedge startup. Same
    // bound as reconnectLocked's inline hello; hostd answers hello even mid-solve.
    // Browser: never block first paint forever if the gateway is slow/silent —
    // maintainConnection retries with backoff when hello fails.
    const int timeoutMs = (kind == "control") ? 2000 : 5000;
    if (!call("hello", params, result, err, timeoutMs)) {
        // An error object is a refusal by the daemon; anything else failed on the transport.
        if (result.is_object()) {
            refusal = std::move(result);
            refuseHello(refusal, err);
        }
        return false;
    }
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
    // Publish this attempt's hello error (null unless the daemon refused) when it ends.
    json refusal;
    const auto publish = onExit([&] { setHelloError(std::move(refusal)); });
#ifdef CHUDWARE_DEVELOPER_BUILD
    const std::string keyBin = opts.hostdBin;
#else
    // Installed clients and hostd must derive the rendezvous key from the same
    // verified executable, even when the caller supplied no hint.
    const std::string keyBin = resolveHostdBin(opts.hostdBin);
    if (keyBin.empty()) {
        err = "canonical manifest-owned hostd binary unavailable";
        return false;
    }
#endif
    const std::string key = instanceKey(keyBin);
    sockPath_ = socketPath(key);
    clientKind_ = opts.clientKind;
    primaryThread_ = std::this_thread::get_id();
    userId_ = opts.userId;
    userDisplay_ = opts.userDisplay;
    discardEvents_ = opts.discardUnconsumedEvents;
    if (sockPath_.empty()) {
        err = "no runtime dir for hostd socket";
        return false;
    }
    std::string firstErr;
    if (tryConnect(firstErr)) return hello(opts.clientKind, err, refusal);

    if (!opts.allowSpawn) {
        err = "hostd not running (" + firstErr + ")";
        return false;
    }
    const std::string bin = resolveHostdBin(opts.hostdBin);
    if (bin.empty()) {
#ifdef CHUDWARE_DEVELOPER_BUILD
        err = "hostd binary not found (set CHUDWARE_HOSTD_BIN); connect failed: " + firstErr;
#else
        err = "canonical manifest-owned hostd binary unavailable; connect failed: " + firstErr;
#endif
        return false;
    }
#ifndef CHUDWARE_DEVELOPER_BUILD
    // Re-verify immediately before CreateProcess/exec. resolveHostdBin also verifies,
    // but this closes the discovery-to-spawn swap window for ordinary replacements.
    const std::string rechecked = resolveHostdBin(opts.hostdBin);
    if (rechecked.empty() || rechecked != bin) {
        err = "hostd binary changed after integrity verification";
        return false;
    }
#endif
    if (!spawnDetached(bin, "", err)) return false;

    // Retry-connect: 50 ms doubling capped at 500 ms, until the budget runs out.
    // The daemon binds BEFORE loading the engine, so this only needs to cover
    // process start + bind, not the (slow) OCCT load.
    const auto deadline =
        std::chrono::steady_clock::now() + std::chrono::milliseconds(opts.spawnBudgetMs);
    int backoff = 50;
    for (;;) {
        std::string e;
        if (tryConnect(e)) return hello(opts.clientKind, err, refusal);
        if (std::chrono::steady_clock::now() >= deadline) {
            err = "hostd spawn timed out: " + e;
            return false;
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(backoff));
        backoff = std::min(backoff * 2, 500);
    }
}

void EngineClient::retainEvent(json&& msg) {
    // hostd sends daemon.draining only to a new connection it refuses, just before the
    // hello error that carries the same reason (helloError()). Queueing it would only
    // replay one stale line per refused attempt after the next connect.
    const auto ev = msg.find("event");
    if (ev != msg.end() && ev->is_string() && *ev == "daemon.draining") return;
    if (!discardEvents_) events_.push_back(std::move(msg));
}

bool EngineClient::readResponse(int id, json& out, std::string& err, int timeoutMs,
                                const std::function<void(const json&)>& onEvent) {
    // One deadline for the WHOLE response wait: event lines arriving on the way
    // must not restart the clock.
    const auto deadline = std::chrono::steady_clock::now() +
                          std::chrono::milliseconds(timeoutMs >= 0 ? timeoutMs : 0);
    std::string line;
    for (;;) {
        if (abort_.load(std::memory_order_acquire)) {
            err = "aborted";
            closeConnection();
            return false;
        }
        int wait = -1;
        if (timeoutMs >= 0) {
            const auto left = std::chrono::duration_cast<std::chrono::milliseconds>(
                                  deadline - std::chrono::steady_clock::now())
                                  .count();
            wait = left > 0 ? (int)left : 0;
        } else {
            // Infinite timeout: still wake periodically so abortPending is observed.
            wait = 200;
        }
        if (!conn_.readLine(line, wait)) {
            // abortPending() half-closes the socket, which reads as EOF: report the abort.
            if (abort_.load(std::memory_order_acquire)) continue;
            if (conn_.eof() || !conn_.valid()) {
                closeConnection();
                err = "daemon connection closed";
                return false;
            }
            // A poll slice expired with the peer alive. An unbounded call keeps waiting:
            // a solver factorizing a large system legitimately pushes nothing for minutes,
            // and failing here abandoned a running native solve after 200 ms of silence.
            if (timeoutMs < 0) continue;
            // A bounded call has reached its deadline with the response still in
            // flight, so this connection is mid-stream and unusable — close it; the
            // next call() reconnects cleanly instead of reading a stale reply.
            // err-only (no {code} in result), same transport-error shape as a
            // connection failure, so callers toast it.
            err = "engine call timed out after " + std::to_string(timeoutMs) + "ms";
            closeConnection();
            return false;
        }
        json msg = json::parse(line, nullptr, /*allow_exceptions=*/false);
        if (msg.is_discarded()) continue;  // tolerate stray non-JSON lines
        if (msg.contains("event")) {
            // Live agent-event streaming: fire onEvent NOW (mid-call) for agent.event so a
            // blocking agent run streams instead of buffering until it returns. Other events
            // (doc.*) still queue for the GUI-thread drainEvents() — or are dropped on a
            // discarding lane that nothing drains.
            //
            // The callback receives the event PAYLOAD (`data`), never the {"event","data"}
            // envelope — the same shape WorkerClient's local-pipe path delivers, so a
            // consumer keying on data["kind"]/data["run_id"] works over BOTH transports.
            // Handing over the envelope instead cost the whole online chat lane its live
            // rendering: every mapped kind read as "" and no bubble, tool row or delta ever
            // appeared until the batched result landed minutes later.
            if (onEvent && msg.value("event", std::string()) == "agent.event") {
                const auto it = msg.find("data");
                try {
                    onEvent(it != msg.end() && it->is_object() ? *it : json::object());
                } catch (...) {}
            } else {
                retainEvent(std::move(msg));
            }
            continue;
        }
        const auto responseId = msg.find("id");
        if (responseId == msg.end() || !responseId->is_number_integer()) {
            // JSON-RPC parse/invalid-request errors have id:null because the peer
            // cannot correlate the rejected request. There is only one waiter on
            // this socket: surface that error, then retire the uncorrelated stream.
            // value("id", -1) throws on null (and aborts exception-disabled WASM).
            const auto error = msg.find("error");
            if (responseId != msg.end() && responseId->is_null() &&
                error != msg.end() && error->is_object() && !msg.contains("result")) {
                out = std::move(msg);
                closeConnection();
                return true;
            }
            err = "daemon response has an invalid request id";
            closeConnection();
            return false;
        }
        if (*responseId == id) {
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
    // Publish this attempt's hello error (null unless the daemon refused) when it ends.
    json refusal;
    const auto publish = onExit([&] { setHelloError(std::move(refusal)); });
    if (sockPath_.empty()) {  // connectOrSpawn never ran (or never succeeded)
        err = "not connected to hostd";
        return false;
    }
    closeConnection();
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
        closeConnection();
        err = "daemon write failed (reconnect hello)";
        return false;
    }
    json resp;
    if (!readResponse(id, resp, err, /*timeoutMs=*/5000)) return false;
    if (!resp.contains("result")) {
        refusal = resp.value("error", json());
        if (refusal.is_object()) {
            refuseHello(refusal, err);
        } else {
            refusal = json();
            err = "hello failed on reconnect";
            closeConnection();
        }
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
        primaryThread_ = std::this_thread::get_id();
        sockPath_ = socketPath(instanceKey(std::string()));
        if (sockPath_.empty()) sockPath_ = "ws://engine";  // sentinel; path unused in wasm
    }
    return reconnectLocked(err);
}

bool EngineClient::pollAlive() {
    // try_lock: same rule as drainEvents — never block the GUI behind a long RPC.
    std::unique_lock<std::mutex> lk(mtx_, std::try_to_lock);
    if (!lk.owns_lock()) return connected();
    if (!conn_.valid() || conn_.eof()) {
        connected_.store(false, std::memory_order_release);
        return false;
    }
    // 0 ms poll: if the peer died, readLine sets eof_ on HUP/read-0.
    // Any push events that arrive are queued for the next drainEvents.
    std::string line;
    while (conn_.readLine(line, 0)) {
        json msg = json::parse(line, nullptr, false);
        if (!msg.is_discarded() && msg.contains("event")) retainEvent(std::move(msg));
    }
    const bool alive = conn_.valid() && !conn_.eof();
    connected_.store(alive, std::memory_order_release);
    return alive;
}

bool EngineClient::call(const std::string& method, const json& params, json& result,
                        std::string& err, int timeoutMs,
                        const std::function<void(const json&)>& onEvent) {
    if (usesBackgroundConnection(method, params))
        return backgroundClient()->call(method, params, result, err, timeoutMs, onEvent);
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
        closeConnection();
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
    pollBackgroundClients();
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
        if (!msg.is_discarded() && msg.contains("event")) retainEvent(std::move(msg));
        // Orphan responses (no waiter) are dropped.
    }
    if (!conn_.valid() || conn_.eof())
        connected_.store(false, std::memory_order_release);
    std::vector<json> out;
    out.swap(events_);
    return out;
}

}  // namespace chudware_ipc
