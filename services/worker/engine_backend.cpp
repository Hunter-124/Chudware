#include "engine_backend.h"

#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <mutex>
#include <thread>

#include "cpp_core.h"

#ifdef _WIN32
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#else
#include <unistd.h>
#endif

#include "../ipc/ipc_client.h"
#include "../ipc/ipc_discovery.h"

namespace engine {

using nlohmann::json;

namespace {

chudware_ipc::EngineClient* g_client = nullptr;  // non-null iff daemon mode won
bool g_daemon = false;
std::once_flag g_once;

chudware_ipc::EngineClient* g_runClient = nullptr;  // dedicated conn for long streaming runs
std::mutex g_runClientMtx;                          // guards g_runClient creation

// The long streaming agent methods stream for minutes and EngineClient::call holds that
// connection's mutex for the whole call. Running them on the shared g_client starved the
// GL thread — openMirrorTab's synchronous attach_doc, doc_discard on close, the
// Recover/Discard buttons all call g_client directly and block on its mutex until the run
// unwinds. That is the "freeze after Stop" hang: Stop cancels cooperatively but the run
// keeps holding g_client until it actually returns, so the UI stays wedged. Give the long
// run its OWN connection instead; hostd serves each connection on its own thread and its
// doc registry gates multi-writer overlap (same rationale as the cancel/steer side
// sockets), so concurrent edits are safe. Only one long run is live at a time
// (WorkerClient::callMtx_), so one reserved connection suffices.
//
// Both naming variants must match: the host RPC names (agent_run_v2 / agent_mission_v1)
// reach engine::call directly when the C++ agent is OFF, but with it ON WorkerClient's
// routeAgentToCpp translates them to the CORE names (agent_run / agent_mission_v1) before
// engine::call — miss either and the run silently falls back to the shared client.
bool isLongRunMethod(const std::string& method) {
    return method == "agent_run" || method == "agent_run_v2" ||
           method == "agent_mission" || method == "agent_mission_v1";
}

// Lazily open (and reuse) the dedicated long-run connection. nullptr on connect failure —
// the caller then falls back to the shared client (correctness over the anti-freeze
// optimization). EngineClient reconnects itself on EOF, so one connect is reused forever.
chudware_ipc::EngineClient* runClient() {
    std::lock_guard<std::mutex> lk(g_runClientMtx);
    if (g_runClient) return g_runClient;
    auto* c = new chudware_ipc::EngineClient();
    chudware_ipc::EngineClient::Options opts;
    opts.hostdBin = hostdBinHint();
    opts.clientKind = "gui";
    std::string err;
    if (!c->connectOrSpawn(opts, err)) {
        std::fprintf(stderr,
                     "[engine] run-connection connect failed (%s) — long runs share the "
                     "GUI connection\n",
                     err.c_str());
        delete c;
        return nullptr;
    }
    return g_runClient = c;
}

std::string selfExePath() {
#ifdef __EMSCRIPTEN__
    return std::string();
#elif defined(_WIN32)
    wchar_t buf[MAX_PATH];
    DWORD n = GetModuleFileNameW(nullptr, buf, MAX_PATH);
    if (n == 0 || n >= MAX_PATH) return std::string();
    int len = WideCharToMultiByte(CP_UTF8, 0, buf, (int)n, nullptr, 0, nullptr, nullptr);
    std::string out(len, '\0');
    WideCharToMultiByte(CP_UTF8, 0, buf, (int)n, out.data(), len, nullptr, nullptr);
    return out;
#else
    char buf[4096];
    ssize_t n = ::readlink("/proc/self/exe", buf, sizeof(buf) - 1);
    if (n <= 0) return std::string();
    buf[n] = 0;
    return std::string(buf);
#endif
}

}  // namespace

// hostd binary hint, mirroring services/mcp/daemon_bridge.py resolve_hostd_bin:
// $CHUDWARE_HOSTD_BIN is honored inside resolveHostdBin(); the hint covers
// exe-adjacent (installed layout: dist/Chudware stages both binaries together)
// then the repo dev-tree build.
std::string hostdBinHint() {
    namespace fs = std::filesystem;
    std::error_code ec;
#ifdef _WIN32
    const char* exeName = "chudware-hostd.exe";
    const char* devBuildDir = "build";  // core\build: the build.bat / MSVC preset dir
#else
    const char* exeName = "chudware-hostd";
    const char* devBuildDir = "build-occt-linux";
#endif
    const std::string self = selfExePath();
    if (!self.empty()) {
        // exe-adjacent
        fs::path cand = fs::path(self).parent_path() / exeName;
        if (fs::is_regular_file(cand, ec)) return cand.string();
        // repo dev tree: walk up from the exe looking for core/<build dir>.
        fs::path dir = fs::path(self).parent_path();
        for (int i = 0; i < 6 && !dir.empty(); ++i, dir = dir.parent_path()) {
            fs::path c = dir / "core" / devBuildDir / "bin" / exeName;
            if (fs::is_regular_file(c, ec)) return c.string();
            if (dir == dir.parent_path()) break;
        }
    }
    return std::string();
}

namespace {

// One-shot startup latency probe: N pings over the socket, logged to stderr so
// the number lands in the app log/console (Phase 4 deliverable: <0.5 ms/call).
void logPingLatency(chudware_ipc::EngineClient& c) {
    json r;
    std::string e;
    const int N = 200;
    const auto t0 = std::chrono::steady_clock::now();
    for (int i = 0; i < N; ++i)
        if (!c.call("ping", json::object(), r, e)) return;
    const double us = std::chrono::duration<double, std::micro>(
                          std::chrono::steady_clock::now() - t0).count() / N;
    std::fprintf(stderr, "[engine] daemon ping round-trip: %.1f us/call (%d calls)\n", us, N);
}

void initOnce() {
#ifdef __EMSCRIPTEN__
    // Browser client is ALWAYS daemon mode. Do NOT connect here — first paint
    // must not block on WebSocket open/hello. maintainConnection() connects
    // with backoff once the frame loop is running.
    auto* c = new chudware_ipc::EngineClient();
    g_client = c;
    g_daemon = true;
    std::fprintf(stderr, "[engine] WASM daemon mode armed (connect deferred)\n");
    return;
#else
    // Phase-8 flip: daemon mode is the DEFAULT. Only an explicit
    // CHUDWARE_ENGINE_DAEMON=0 forces the legacy in-process load (kept at least
    // one release; removal is a named N+1 task).
    const char* f = std::getenv("CHUDWARE_ENGINE_DAEMON");
    if (f && f[0] == '0') return;  // explicit opt-out -> legacy in-process
    auto* c = new chudware_ipc::EngineClient();
    chudware_ipc::EngineClient::Options opts;
    opts.hostdBin = hostdBinHint();
    opts.clientKind = "gui";
    std::string err;
    if (!c->connectOrSpawn(opts, err)) {
        // core_bridge.py-style loud warning + legacy fallback.
        std::fprintf(stderr,
                     "[engine] WARNING: daemon mode (default) but hostd connect failed (%s) "
                     "— falling back to in-process libchudware\n",
                     err.c_str());
        delete c;
        return;
    }
    g_client = c;
    g_daemon = true;
    std::fprintf(stderr, "[engine] daemon mode: connected to chudware-hostd (state=%s)\n",
                 c->serverInfo().value("state", std::string("?")).c_str());
    logPingLatency(*c);
#endif
}

// Absorb {"code":"loading"}: the daemon binds before the (slow) OCCT load and
// answers engine calls with a retriable loading error until ready. Mirror the
// Python bridge: poll daemon_status, bounded.
bool waitEngineReady(chudware_ipc::EngineClient& c, std::string& err) {
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(300);
    while (std::chrono::steady_clock::now() < deadline) {
        json st;
        std::string e;
        if (!c.call("daemon_status", json::object(), st, e)) { err = e; return false; }
        const std::string state = st.value("state", std::string());
        if (state == "ready") return true;
        if (state == "failed") { err = "engine failed to load in hostd"; return false; }
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }
    err = "engine load timed out in hostd";
    return false;
}

}  // namespace

bool daemonMode() {
    std::call_once(g_once, initOnce);
    return g_daemon;
}

chudware_ipc::EngineClient* client() {
    return daemonMode() ? g_client : nullptr;
}

// Inner dispatch: daemon socket round-trip (with loading-wait and ro-upgrade retries) or
// the in-process DLL call. Split out so the doc_activate compat shim below can re-enter.
static bool callDispatch(chudware_ipc::EngineClient* cl, const std::string& method,
                         const json& params, json& result, std::string& err, int timeoutMs,
                         const std::function<void(const json&)>& onEvent) {
    // timeoutMs only bounds the daemon round trip; the in-process DLL call is
    // synchronous and can't be interrupted (matches the pre-daemon behavior).
    if (!daemonMode()) return core::cpp_core().call(method, params, result, err);
    bool ok = cl->call(method, params, result, err, timeoutMs, onEvent);
    if (!ok && result.is_object()) {
        const std::string code = result.value("code", std::string());
        if (code == "loading") {
            std::string werr;
            if (waitEngineReady(*cl, werr))
                ok = cl->call(method, params, result, err, timeoutMs, onEvent);
            else if (err.empty())
                err = werr;
        } else if (code == "read_only" && params.is_object() && params.contains("doc_id")) {
            // The user edited a doc this client only VIEWS (an agent's mirror
            // tab): human steering wins — upgrade the attach to rw and retry
            // once. Documented Phase 4 choice: auto-upgrade over new UI.
            json ar;
            std::string ae;
            if (cl->call("attach_doc",
                         json{{"doc_id", params["doc_id"]}, {"mode", "rw"}}, ar, ae))
                ok = cl->call(method, params, result, err, timeoutMs);
        }
    }
    return ok;
}

bool call(const std::string& method, const json& params, json& result, std::string& err,
          int timeoutMs, const std::function<void(const json&)>& onEvent) {
    // Long streaming runs go on a dedicated connection so they never hold the shared GUI
    // client's call mutex (needed by the GL thread for attach_doc / doc_discard / event
    // drains) — otherwise a live run freezes the UI until it unwinds. Fall back to the
    // shared client if the dedicated connection can't be established.
    chudware_ipc::EngineClient* cl = g_client;
    if (daemonMode() && isLongRunMethod(method)) {
        if (auto* rc = runClient()) cl = rc;
    }
    bool ok = callDispatch(cl, method, params, result, err, timeoutMs, onEvent);
    // Compat shim: doc_activate (cheap tab-switch activation, cached-serve) is served by
    // this tree's engine, but a long-lived hostd daemon may predate it. On "unknown
    // method" fall back ONCE to the historical forced doc_rebuild — identical response
    // shape, just slower — so a new GUI against an old daemon degrades instead of breaking.
    if (!ok && method == "doc_activate") {
        const bool unknown =
            err.find("unknown method") != std::string::npos ||
            (result.is_object() && result.value("message", std::string())
                                        .find("unknown method") != std::string::npos);
        if (unknown) {
            err.clear();
            ok = callDispatch(g_client, "doc_rebuild", params, result, err, timeoutMs, {});
        }
    }
    return ok;
}

// Wake a blocked long run on its dedicated connection at teardown (mirror of
// EngineClient::abortPending on the shared client) so app quit / AsyncRpc shutdown never
// hangs on a multi-minute modeling call. No-op if the run connection was never opened.
// Safe from any thread.
void abortRunClient() {
    std::lock_guard<std::mutex> lk(g_runClientMtx);
    if (g_runClient) g_runClient->abortPending();
}

bool available() {
    if (daemonMode()) {
        // Reflect live socket health so callers don't pretend a wedged hostd is fine.
        return g_client && g_client->connected();
    }
    return core::cpp_core().available();
}

bool ensureLoaded(std::string& err) {
    if (daemonMode()) {
        if (g_client && g_client->connected()) return true;
        err = "engine daemon unreachable";
        return false;
    }
    return core::cpp_core().ensureLoaded(err);
}

// Backoff schedule for proactive GUI reconnect: 1s, 2s, 5s, then every 10s.
// First detection of a dead socket tries immediately (step 0 after a failed try
// schedules the 1s wait).
static int reconnectBackoffMs(int failedAttempts) {
    static const int kMs[] = {1000, 2000, 5000, 10000};
    if (failedAttempts <= 0) return kMs[0];
    if (failedAttempts >= 4) return kMs[3];
    return kMs[failedAttempts - 1];
}

DaemonConnState maintainConnection() {
    if (!daemonMode() || !g_client) return DaemonConnState::InProc;
    // Backoff state lives for the process lifetime; reset whenever the socket is
    // healthy so a call()-driven reconnect doesn't leave a stale nextTry delay.
    static int failedAttempts = 0;
    static std::chrono::steady_clock::time_point nextTry{};
    // pollAlive surfaces peer death (HUP/EOF) without waiting for the next RPC —
    // connected() alone stays true until something reads the dead socket.
    if (g_client->pollAlive()) {
        failedAttempts = 0;
        nextTry = {};
        return DaemonConnState::Connected;
    }
    // Dead socket: bounded reconnect (connect-only; no spawn — matches EngineClient
    // call()-driven reconnect).
    const auto now = std::chrono::steady_clock::now();
    if (nextTry.time_since_epoch().count() != 0 && now < nextTry)
        return DaemonConnState::Unreachable;

    std::string err;
#ifdef __EMSCRIPTEN__
    // Browser: the render thread MUST NOT hard-block acquiring the client mutex.
    // Under ASYNCIFY (no PROXY_TO_PTHREAD) the WebSocket onclose/onmessage
    // callbacks run on THIS thread; a lock_guard wait behind an AsyncRpc-worker
    // call stuck on a dead socket would stop those callbacks from ever firing,
    // wedging the tab. tryReconnectNonBlocking try_locks and, when a call is in
    // flight, sets `deferred` and returns false — that is NOT a failure: keep the
    // backoff counter untouched so we retry next frame the instant the lock frees
    // (the stuck call drains as soon as the event loop runs). It also does connect
    // + hello inline (no spawn) and bootstraps the socket path for the first connect.
    bool deferred = false;
    const bool ok = g_client->tryReconnectNonBlocking(err, &deferred);
    if (!ok && deferred) return DaemonConnState::Unreachable;  // skip tick, no backoff
#else
    const bool ok = g_client->tryReconnect(err);
#endif
    if (ok) {
        failedAttempts = 0;
        nextTry = {};
        std::fprintf(stderr, "[engine] daemon reconnected (gen=%llu)\n",
                     (unsigned long long)g_client->reconnectGeneration());
        return DaemonConnState::Connected;
    }
    ++failedAttempts;
    const int waitMs = reconnectBackoffMs(failedAttempts);
    nextTry = now + std::chrono::milliseconds(waitMs);
    if (failedAttempts == 1 || failedAttempts % 5 == 0) {
        std::fprintf(stderr,
                     "[engine] daemon unreachable (%s) — retry in %dms (attempt %d)\n",
                     err.c_str(), waitMs, failedAttempts);
    }
    return DaemonConnState::Unreachable;
}

}  // namespace engine
