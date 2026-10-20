#include "engine_backend.h"

#include <array>
#include <chrono>
#include <condition_variable>
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
#include "util/platform_paths.h"

namespace engine {

using nlohmann::json;

namespace {

chudware_ipc::EngineClient* g_client = nullptr;  // non-null iff daemon mode won
bool g_daemon = false;
// True only when daemon mode was WANTED (no explicit CHUDWARE_ENGINE_DAEMON=0)
// yet every connect attempt failed and we fell back to the in-process core. Never set
// by a daemon_draining refusal: that keeps daemon mode (see initOnce).
bool g_daemonUnavailable = false;
std::once_flag g_once;

struct AgentRunSlot {
    chudware_ipc::EngineClient* client = nullptr;
    bool busy = false;
};
std::array<AgentRunSlot, 8> g_agentRunClients{};
std::condition_variable g_agentRunCv;
size_t g_agentRunAbortGeneration = 0;
chudware_ipc::EngineClient* g_simRunClient = nullptr;
std::mutex g_runClientMtx;

// Streaming calls hold an EngineClient's mutex until they finish, so they stay off
// the GUI document connection. Each agent call leases a private connection: hostd
// runs online turns in separate workers and DocLock serializes same-doc batches.
// WorkerClient still serializes native agent runs and simulation solves.
// Both host and translated core method names must select the same lane.
// The simulation connection discards pushes not consumed live and never falls back
// to g_client, since a long solve would otherwise freeze GUI document calls.
bool isAgentLongRunMethod(const std::string& method) {
    return method == "agent_run" || method == "agent_run_v2" ||
           method == "agent_mission" || method == "agent_mission_v1" ||
           method == "agent_admin_turn";
}

bool isSimLongRunMethod(const std::string& method) {
    return method == "sim_mesh" || method == "sim_run_static" ||
           method == "sim_run_adaptive" || method == "sim_run_modal" ||
           method == "sim_run_fatigue" ||
           method == "sim_run_buckling" || method == "sim_run_thermal" ||
           method == "sim_run_flow" || method == "sim_run_pipe_flow" ||
           method == "sim_run_topology";
}

// Called with g_runClientMtx held. EngineClient reconnects itself after EOF.
chudware_ipc::EngineClient* createRunClient(bool discardUnconsumedEvents,
                                           std::string& err) {
    auto* c = new chudware_ipc::EngineClient();
    chudware_ipc::EngineClient::Options opts;
    opts.hostdBin = hostdBinHint();
    opts.clientKind = "gui";
    opts.discardUnconsumedEvents = discardUnconsumedEvents;
    if (!c->connectOrSpawn(opts, err)) {
        delete c;
        return nullptr;
    }
    return c;
}

chudware_ipc::EngineClient* openRunClient(chudware_ipc::EngineClient*& slot,
                                          bool discardUnconsumedEvents, std::string& err) {
    std::lock_guard<std::mutex> lk(g_runClientMtx);
    if (!slot) slot = createRunClient(discardUnconsumedEvents, err);
    return slot;
}

bool agentRunConnectionReady() {
    std::lock_guard<std::mutex> lk(g_runClientMtx);
    for (const auto& slot : g_agentRunClients)
        if (slot.client) return true;
    std::string err;
    g_agentRunClients.front().client = createRunClient(false, err);
    return g_agentRunClients.front().client != nullptr;
}

class AgentRunLease {
public:
    explicit AgentRunLease(bool acquire) {
        if (!acquire) return;
        std::unique_lock<std::mutex> lk(g_runClientMtx);
        const size_t generation = g_agentRunAbortGeneration;
        for (;;) {
            if (generation != g_agentRunAbortGeneration) {
                aborted_ = true;
                return;
            }
            AgentRunSlot* empty = nullptr;
            bool anyClient = false;
            for (auto& slot : g_agentRunClients) {
                if (!slot.client) {
                    if (!empty) empty = &slot;
                    continue;
                }
                anyClient = true;
                if (!slot.busy) {
                    slot.busy = true;
                    slot_ = &slot;
                    return;
                }
            }
            if (empty) {
                std::string err;
                empty->client = createRunClient(false, err);
                if (empty->client) {
                    empty->busy = true;
                    slot_ = empty;
                    return;
                }
                if (!anyClient) {
                    std::fprintf(stderr,
                                 "[engine] agent run-connection connect failed (%s) — long runs "
                                 "share the GUI connection\n", err.c_str());
                    return;
                }
            }
            g_agentRunCv.wait(lk);
        }
    }

    ~AgentRunLease() {
        if (!slot_) return;
        {
            std::lock_guard<std::mutex> lk(g_runClientMtx);
            slot_->busy = false;
        }
        g_agentRunCv.notify_one();
    }

    AgentRunLease(const AgentRunLease&) = delete;
    AgentRunLease& operator=(const AgentRunLease&) = delete;
    chudware_ipc::EngineClient* client() const { return slot_ ? slot_->client : nullptr; }
    bool aborted() const { return aborted_; }

private:
    AgentRunSlot* slot_ = nullptr;
    bool aborted_ = false;
};

// The simulation connection discards unconsumed pushes and has no shared fallback.
chudware_ipc::EngineClient* simRunClient(std::string& err) {
    return openRunClient(g_simRunClient, /*discardUnconsumedEvents=*/true, err);
}

// A draining hostd refuses every new non-control client with this hello error
// (Server::drainRefusal). It holds the instance lock until it exits, so no successor can
// start beside it, and a private in-process engine would not share its documents.
bool isDrainRefusal(const json& helloError) {
    if (!helloError.is_object()) return false;
    const auto code = helloError.find("code");
    return code != helloError.end() && code->is_string() && *code == "daemon_draining";
}

std::string jsonString(const json& o, const char* key) {
    const auto it = o.is_object() ? o.find(key) : o.end();
    return it != o.end() && it->is_string() ? it->get<std::string>() : std::string();
}

std::string selfExePath() { return chudware::platform::executablePath(); }

}  // namespace

RunLane runLane(const std::string& method) {
    if (isAgentLongRunMethod(method)) return RunLane::Agent;
    if (isSimLongRunMethod(method)) return RunLane::Sim;
    return RunLane::None;
}

bool isSimLanePreview(const std::string& method) { return method == "sim_mesh"; }

// Production accepts only the adjacent daemon selected and verified by resolveHostdBin().
// Developer builds additionally search the familiar repository build tree.
std::string hostdBinHint() {
    namespace fs = std::filesystem;
    std::error_code ec;
#if defined(_WIN32)
    const char* exeName = "chudware-hostd.exe";
    const char* devBuildDir = "build";  // core\build: the build.bat / MSVC preset dir
#elif defined(__APPLE__)
    const char* exeName = "chudware-hostd";
    // build.sh derives the dev tree name from uname; keep the two spellings in step.
#  if defined(__aarch64__) || defined(__arm64__)
    const char* devBuildDir = "build-occt-macos-arm64";
#  else
    const char* devBuildDir = "build-occt-macos-x86_64";
#  endif
#else
    const char* exeName = "chudware-hostd";
    const char* devBuildDir = "build-occt-linux";
#endif
    const std::string self = selfExePath();
    if (!self.empty()) {
        // exe-adjacent
        fs::path cand = fs::path(self).parent_path() / exeName;
        if (fs::is_regular_file(cand, ec)) return cand.string();
#ifdef CHUDWARE_DEVELOPER_BUILD
        // repo dev tree: walk up from the exe looking for core/<build dir>.
        fs::path dir = fs::path(self).parent_path();
        for (int i = 0; i < 6 && !dir.empty(); ++i, dir = dir.parent_path()) {
            fs::path c = dir / "core" / devBuildDir / "bin" / exeName;
            if (fs::is_regular_file(c, ec)) return c.string();
            if (dir == dir.parent_path()) break;
        }
#endif
    }
    return std::string();
}

namespace {

// Explicit diagnostic only: normal startup must not spend 200 synchronous round trips
// measuring the connection before the GUI can use it.
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

    // Transient-outage retry. hostd may be booting, restarting under systemd,
    // or draining a version mismatch at the exact moment the GUI starts. This
    // function runs under std::call_once, so its verdict is PERMANENT for the
    // process: maintainConnection() only reconnects once daemon mode has
    // already WON, and cannot rearm a legacy fallback. Giving up on the first
    // refused connect therefore stranded entire multi-hour sessions on a
    // private in-process engine — invisible to (and unable to see) every doc
    // living in the shared daemon. So retry with bounded backoff before
    // falling back. A draining daemon is the exception: see below.
    //
    // CHUDWARE_ENGINE_DAEMON_RETRY_MS (integer, total milliseconds) overrides
    // the retry budget; 0 disables retrying entirely and restores the old
    // single-attempt behaviour, which is what tests and the CHUDWARE_SHOT
    // harness want so startup stays instant when no hostd is expected.
    static const int kBackoffMs[] = {100, 250, 500, 1000, 1000};
    const size_t kBackoffN = sizeof(kBackoffMs) / sizeof(kBackoffMs[0]);
    int budgetMs = 0;
    for (size_t i = 0; i < kBackoffN; ++i) budgetMs += kBackoffMs[i];
    if (const char* rm = std::getenv("CHUDWARE_ENGINE_DAEMON_RETRY_MS")) {
        char* end = nullptr;
        const long v = std::strtol(rm, &end, 10);
        if (end != rm && v >= 0) budgetMs = static_cast<int>(v);
    }

    bool connected = false;
    bool draining = false;
    int spentMs = 0;
    for (size_t attempt = 0;; ++attempt) {
        if (c->connectOrSpawn(opts, err)) { connected = true; break; }
        // A draining hostd refuses by name and keeps refusing until it exits, so this
        // short ladder cannot outlast it. maintainConnection() waits for it instead.
        if (isDrainRefusal(c->helloError())) { draining = true; break; }
        if (attempt >= kBackoffN) break;
        const int waitMs = kBackoffMs[attempt];
        if (spentMs + waitMs > budgetMs) break;
        std::fprintf(stderr,
                     "[engine] hostd connect failed (%s) — retrying in %d ms "
                     "(attempt %zu)\n",
                     err.c_str(), waitMs, attempt + 2);
        std::this_thread::sleep_for(std::chrono::milliseconds(waitMs));
        spentMs += waitMs;
    }
    if (draining) {
        // Never fall back to the in-process engine on a drain refusal: this window would
        // then edit documents that the agent and every other client cannot see, for the
        // whole session. Keep daemon mode with a client that is not connected yet, as the
        // browser build does. maintainConnection() shows the refusal on the unreachable
        // chip, retries, and starts the new daemon after the old one exits.
        g_client = c;
        g_daemon = true;
        std::fprintf(stderr,
                     "[engine] %s — daemon mode stays on. Chudware connects when the "
                     "draining daemon exits. It does not start a private in-process "
                     "engine.\n",
                     err.c_str());
        return;
    }
    if (!connected) {
        // core_bridge.py-style loud warning + legacy fallback. Spell out the
        // consequence: this process now owns a PRIVATE engine, so anything the
        // agent (or another client) creates in the shared daemon simply does
        // not exist from here — no tab, no viewport, nothing in Artifacts.
        g_daemonUnavailable = true;
        std::fprintf(stderr,
                     "[engine] WARNING: daemon mode (default) but hostd connect failed "
                     "after %d ms of retries (%s) — falling back to in-process "
                     "libchudware. This process is now on a PRIVATE in-process engine: "
                     "documents created by a shared chudware-hostd (including any agent "
                     "worker attached to it) will NOT be visible here.\n",
                     spentMs, err.c_str());
        delete c;
        return;
    }
    g_client = c;
    g_daemon = true;
    std::fprintf(stderr, "[engine] daemon mode: connected to chudware-hostd (state=%s)\n",
                 c->serverInfo().value("state", std::string("?")).c_str());
    const char* pingBench = std::getenv("CHUDWARE_PING_BENCH");
    if (pingBench && pingBench[0] == '1' && pingBench[1] == '\0') logPingLatency(*c);
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

bool daemonUnavailable() {
    std::call_once(g_once, initOnce);  // same guard: safe to call at any time
    return g_daemonUnavailable;
}

chudware_ipc::EngineClient* client() {
    return daemonMode() ? g_client : nullptr;
}

// Inner dispatch: daemon socket round-trip (with loading-wait and ro-upgrade retries) or
// the in-process DLL call. Split out so the doc_activate compat shim below can re-enter.
static bool callDispatch(chudware_ipc::EngineClient* cl, const std::string& method,
                         const json& params, json& result, std::string& err, int timeoutMs,
                         const std::function<void(const json&)>& onEvent) {
#ifdef __EMSCRIPTEN__
    if (method != "hello" && method != "ping" &&
        (!cl || !webCellMethodAllowed(cl->serverInfo(), method))) {
        result = {{"code", "cell_refused"}, {"message", "Browser CAD and AI are unavailable"}};
        err = "Browser CAD and AI are unavailable";
        return false;
    }
    if (!cl) {
        result = {{"code", "cell_refused"}, {"message", "Browser cell is unavailable"}};
        err = "Browser cell is unavailable";
        return false;
    }
#endif
    // timeoutMs only bounds the daemon round trip; the in-process DLL call is
    // synchronous and can't be interrupted (matches the pre-daemon behavior).
#ifndef __EMSCRIPTEN__
    if (!daemonMode()) return core::cpp_core().call(method, params, result, err);
#endif
    bool ok = cl->call(method, params, result, err, timeoutMs, onEvent);
#ifndef __EMSCRIPTEN__
    if (!ok && result.is_object()) {
        const auto code = result.find("code");
        if (code != result.end() && *code == "loading") {
            std::string werr;
            if (waitEngineReady(*cl, werr))
                ok = cl->call(method, params, result, err, timeoutMs, onEvent);
            else if (err.empty())
                err = werr;
        } else if (code != result.end() && *code == "read_only" &&
                   params.is_object() && params.contains("doc_id")) {
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
#endif
    return ok;
}

bool call(const std::string& method, const json& params, json& result, std::string& err,
          int timeoutMs, const std::function<void(const json&)>& onEvent) {
    // Initialize daemon mode before copying g_client. A first short request reaches this
    // function before any other daemonMode() caller, so copying first would pass the
    // pre-initialization nullptr into callDispatch even though initOnce then armed daemon mode.
    // Long streaming runs go on a dedicated connection so they never hold the shared GUI
    // client's call mutex (needed by the GL thread for attach_doc / doc_discard / event
    // drains) — otherwise a live run freezes the UI until it unwinds. An agent run falls back
    // to the initialized shared client if its connection can't be established; a simulation
    // solve fails instead (see isSimLongRunMethod).
    const bool useDaemon = daemonMode();
    chudware_ipc::EngineClient* cl = useDaemon ? g_client : nullptr;
#ifdef __EMSCRIPTEN__
    // Refuse before acquiring a streaming lane: an unadvertised call must not
    // wait for another run or establish an extra connection.
    if (!cl || !webCellMethodAllowed(cl->serverInfo(), method))
        return callDispatch(cl, method, params, result, err, timeoutMs, onEvent);
#endif
    const RunLane lane = useDaemon ? runLane(method) : RunLane::None;
    AgentRunLease agentLease(lane == RunLane::Agent);
    if (agentLease.aborted()) {
        err = "aborted";
        return false;
    }
    if (lane == RunLane::Agent) {
        if (auto* rc = agentLease.client()) cl = rc;
    } else if (lane == RunLane::Sim) {
        std::string cerr;
        cl = simRunClient(cerr);
        if (!cl) {
            err = "simulation connection to hostd unavailable (" + cerr + ")";
            return false;
        }
    }
    bool ok = callDispatch(cl, method, params, result, err, timeoutMs, onEvent);
#ifndef __EMSCRIPTEN__
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
#endif
    return ok;
}

// Wake blocked long runs on their dedicated connections at teardown (mirror of
// EngineClient::abortPending on the shared client) so app quit / AsyncRpc shutdown never
// hangs on a multi-minute modeling call or simulation solve. No-op for a connection that was
// never opened. Safe from any thread.
void abortRunClient() {
    std::lock_guard<std::mutex> lk(g_runClientMtx);
    ++g_agentRunAbortGeneration;
    for (const auto& slot : g_agentRunClients)
        if (slot.client) slot.client->abortPending();
    g_agentRunCv.notify_all();
    if (g_simRunClient) g_simRunClient->abortPending();
}

bool runConnectionReady(RunLane lane) {
    if (!daemonMode()) return false;
    switch (lane) {
        case RunLane::Agent:
            return agentRunConnectionReady();
        case RunLane::Sim: {
            std::string err;
            return simRunClient(err) != nullptr;
        }
        case RunLane::None:
            break;
    }
    return false;
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
        const json refusal = daemonRefusal();
        err = refusal.is_null()
                  ? std::string("engine daemon unreachable")
                  : "engine daemon is draining: " + jsonString(refusal, "message");
        return false;
    }
    return core::cpp_core().ensureLoaded(err);
}

json daemonRefusal() {
    if (!daemonMode() || !g_client) return json();
    json refusal = g_client->helloError();
    return isDrainRefusal(refusal) ? refusal : json();
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

#ifndef __EMSCRIPTEN__
// Consecutive failed connect-only attempts before we conclude the daemon is
// really GONE (not just briefly restarting) and start one. On the 1s/2s ladder
// the third failure lands at t≈3s, so the respawn goes out then and the next
// (1s) tick reconnects: recovery in ~4s, still past any drain-replace or
// accept hiccup. With a hostd that dies on every start the doubling cooldown
// below caps this at 15 spawns/hour.
static const int kRespawnAfterFailures = 3;

// Cooldown before another respawn is allowed: 30s doubling to a 5-minute cap, so
// a hostd that dies on every start settles at ~12 spawns/hour instead of one per
// backoff tick. Deliberately NO hard cap on the escalation count: the cause is
// usually fixable mid-session (rebuild, freed RAM, repaired install) and a
// permanently disarmed GUI could never notice.
static int respawnCooldownSecs(int escalations) {
    int s = 30;
    for (int i = 1; i < escalations && s < 300; ++i) s *= 2;
    return s > 300 ? 300 : s;
}
#endif

DaemonConnState maintainConnection() {
    if (!daemonMode() || !g_client) return DaemonConnState::InProc;
    // Backoff state lives for the process lifetime; reset whenever the socket is
    // healthy so a call()-driven reconnect doesn't leave a stale nextTry delay.
    static int failedAttempts = 0;
    static std::chrono::steady_clock::time_point nextTry{};
    // Respawn escalation state, reset alongside the backoff: a connection that
    // actually came back proves the last spawn worked, so a later death starts
    // over at the 30s cooldown instead of inheriting the 5-minute one.
    static int respawns = 0;
    static std::chrono::steady_clock::time_point nextRespawn{};
    // pollAlive surfaces peer death (HUP/EOF) without waiting for the next RPC —
    // connected() alone stays true until something reads the dead socket.
    if (g_client->pollAlive()) {
        failedAttempts = 0;
        nextTry = {};
        respawns = 0;
        nextRespawn = {};
        return DaemonConnState::Connected;
    }
    // Dead socket: bounded reconnect (connect-only; no spawn — matches EngineClient
    // call()-driven reconnect). Spawning is escalated to separately, below.
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
        respawns = 0;
        nextRespawn = {};
        std::fprintf(stderr, "[engine] daemon reconnected (gen=%llu)\n",
                     (unsigned long long)g_client->reconnectGeneration());
        return DaemonConnState::Connected;
    }
    ++failedAttempts;
    int waitMs = reconnectBackoffMs(failedAttempts);
#ifndef __EMSCRIPTEN__
    // A draining hostd answered and refused: it is alive and holds the instance lock, so
    // a spawn now only loses that lock and arms the respawn cooldown. The refused
    // attempts still count: after the old daemon exits, the first failed connect spawns
    // the new one at once.
    const bool draining = isDrainRefusal(g_client->helloError());
    // Nothing above can ever bring back a hostd that actually DIED (crash,
    // SIGKILL, its own watchdog _Exit(70)): every reconnect path in EngineClient
    // is connect-only, and the desktop daemon has no OS supervisor on either
    // platform. Without this the GUI polls a dead socket forever behind a sticky
    // "unreachable" chip. So: start one ourselves, rate-limited.
    //
    // Spawn-and-let-the-ladder-reconnect rather than calling connectOrSpawn()
    // here, deliberately. hostd binds its socket BEFORE the (slow) OCCT load, so
    // the very next 1s tick normally connects — while connectOrSpawn() would
    // stall this frame-loop thread for its whole connect budget AND poke
    // EngineClient::conn_ from outside mtx_ (connectInner->tryConnect) while a
    // worker-thread call() may still be unwinding on the dead socket.
    if (!draining && failedAttempts >= kRespawnAfterFailures && now >= nextRespawn) {
        ++respawns;
        const int cooldownS = respawnCooldownSecs(respawns);
        nextRespawn = now + std::chrono::seconds(cooldownS);
        const std::string bin = chudware_ipc::resolveHostdBin(hostdBinHint());
        std::string serr;
        if (!bin.empty() && chudware_ipc::spawnDetached(bin, "", serr)) {
            waitMs = 1000;  // back to the fast rung: it should be listening within ~1s
            std::fprintf(stderr,
                         "[engine] hostd gone after %d failed reconnects — respawned "
                         "'%s' (escalation #%d; next respawn no sooner than %ds)\n",
                         failedAttempts, bin.c_str(), respawns, cooldownS);
        } else {
            std::fprintf(stderr,
                         "[engine] hostd gone and respawn failed (%s) — next respawn "
                         "in %ds\n",
                         bin.empty() ? "hostd binary not found (set CHUDWARE_HOSTD_BIN)"
                                     : serr.c_str(),
                         cooldownS);
        }
    }
#endif
    nextTry = now + std::chrono::milliseconds(waitMs);
    if (failedAttempts == 1 || failedAttempts % 5 == 0) {
        std::fprintf(stderr,
                     "[engine] daemon unreachable (%s) — retry in %dms (attempt %d)\n",
                     err.c_str(), waitMs, failedAttempts);
    }
    return DaemonConnState::Unreachable;
}

}  // namespace engine
