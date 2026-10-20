// test_worker_long_run.cpp — ctest for WorkerClient's daemon-mode long-run lanes.
//
// Links the REAL worker_client.cpp + engine_backend.cpp + ipc client/transport + async_rpc.cpp
// against an in-process fake hostd: a private Unix socket (CHUDWARE_HOSTD_SOCKET, temp
// XDG_RUNTIME_DIR, CHUDWARE_HOSTD_BIN=/bin/true so nothing real can be spawned or contacted)
// that serves each connection on its own thread, like hostd.
//
// Regression under test (live, 2026-09-27): the GUI's agent job thread held
// WorkerClient::callMtx_ for the whole multi-hour agent_admin_turn. A worker-routed call on the
// single-FIFO st.rpc lane (subscription_*, chudcode_*, plm_* …) blocked on that mutex, and every
// later st.rpc call — the tab switch's doc_activate — waited behind it for the rest of the turn:
// clicking a tab showed "Loading" forever.
//
// Acceptance:
//   ✓ while an agent_admin_turn is in flight, a worker-routed call and a doc_activate queued
//     behind it on AsyncRpc both complete
//   ✓ two online turns on one client overlap on different hostd connections, and their
//     Stop/steer/answer calls reach hostd by run_id without fetching worker_control_info
//   ✓ native agent runs still serialize: the second reaches hostd only after the first returns
//   ✓ the agent connection pool admits eight calls, queues the ninth, and releases leases
//   ✓ a native sim_run_topology with timeoutMs=-1 stays pending across solver silence far
//     longer than the transport's 200 ms abort-poll slice (mesh/assembly/factorization emit
//     nothing for seconds), streams its progress live, then receives its result; a GUI
//     doc_activate on the same WorkerClient completes while it waits
//   ✓ while a sim_run_topology is held in flight, a worker-routed call and a doc_activate
//     queued behind it on AsyncRpc both complete (the solve must not hold callMtx_), and a
//     cancelled solve's structured partial reply reaches the caller intact
//   ✓ two topology runs on one client never overlap; an agent turn and a topology run on one
//     client do not wait for each other (independent lanes)
//   ✓ a sim_mesh preview on the GUI's client, and the doc_activate queued behind it, complete
//     while another client's topology solve holds the shared simulation lane
//
// EngineClient transport, against the same socket peer:
//   ✓ an unbounded wait still ends on peer EOF and, promptly, on abortPending()
//   ✓ a bounded wait is one deadline across a push stream, and the call after a timeout gets
//     its own reply on a fresh connection (never the stale one)
//   ✓ a discarding connection (the private simulation lane) delivers agent.event live to
//     onEvent yet retains nothing of thousands of unrelated pushes, mid-call or idle
//   ✓ a default connection still queues every unconsumed push for drainEvents(), in order
//   ✓ a held GUI background catalog query never blocks primary document calls or a second
//     background thread's query on the same EngineClient
//   ✓ background query EOF/timeout leaves primary liveness/generation intact and reconnects
//     only that side connection; disconnect wakes every active background call
//   ✓ a gateway id:null parse-error reply surfaces its exact structured refusal, invalidates
//     only that socket, and reconnects without throwing or replaying the rejected call;
//     malformed reply ids fail as protocol errors rather than being converted to integers
//
// engine_backend startup against a draining hostd (forked child: the backend latches once
// per process):
//   ✓ a `daemon_draining` hello refusal keeps daemon mode: no in-process fallback, engine
//     calls fail with the named refusal, daemonRefusal() carries the daemon's error object,
//     and the daemon.draining push is not queued for drainEvents()
//   ✓ maintainConnection() reports Unreachable while the daemon refuses, then connects once
//     it admits clients again, and the refusal clears
//
// Idle bursts larger than the socket buffer only finish sending while the client reads, so
// the idle checks consume through pollAlive/drainEvents CONCURRENTLY with the sender
// (consumeIdleBurst) instead of waiting for the send to finish first.

#include "worker_client.h"
#include "engine_backend.h"
#include "core/async_rpc.h"
#include "../ipc/ipc_client.h"

#include <nlohmann/json.hpp>

#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>
#include <sys/wait.h>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <filesystem>
#include <mutex>
#include <optional>
#include <string>
#include <thread>
#include <vector>

using nlohmann::json;
using Clock = std::chrono::steady_clock;

static int g_failures = 0;

#define CHECK(cond)                                                              \
    do {                                                                         \
        if (!(cond)) {                                                           \
            std::printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond);          \
            std::fflush(stdout);                                                 \
            ++g_failures;                                                        \
        }                                                                        \
    } while (0)

// ---- fake hostd --------------------------------------------------------------------------
class FakeHostd {
public:
    explicit FakeHostd(const std::string& path) : path_(path) {
        listenFd_ = ::socket(AF_UNIX, SOCK_STREAM, 0);
        sockaddr_un addr{};
        addr.sun_family = AF_UNIX;
        std::snprintf(addr.sun_path, sizeof addr.sun_path, "%s", path.c_str());
        ::unlink(path.c_str());
        if (::bind(listenFd_, reinterpret_cast<sockaddr*>(&addr), sizeof addr) != 0 ||
            ::listen(listenFd_, 16) != 0) {
            std::perror("fake hostd bind/listen");
            std::exit(2);
        }
        acceptThread_ = std::thread([this] { acceptLoop(); });
    }

    ~FakeHostd() {
        {
            std::lock_guard<std::mutex> lk(mtx_);
            stopping_ = true;
            releaseTurns_ = true;
            releaseSims_ = true;
        }
        cv_.notify_all();
        ::shutdown(listenFd_, SHUT_RDWR);
        ::close(listenFd_);
        acceptThread_.join();
        {
            // Under mtx_: serve() closes and forgets its fd under the same lock, so this never
            // shuts down an fd number the process already reused for something else.
            std::lock_guard<std::mutex> lk(mtx_);
            for (int fd : connFds_) ::shutdown(fd, SHUT_RDWR);
        }
        for (auto& t : connThreads_) t.join();
        ::unlink(path_.c_str());
    }

    // Block online turns and native agent runs until releaseTurns(); re-arm per test.
    void holdTurns() {
        std::lock_guard<std::mutex> lk(mtx_);
        releaseTurns_ = false;
    }
    void releaseTurns() {
        {
            std::lock_guard<std::mutex> lk(mtx_);
            releaseTurns_ = true;
        }
        cv_.notify_all();
    }
    bool waitTurnsStarted(int n, int timeoutMs) {
        std::unique_lock<std::mutex> lk(mtx_);
        return cv_.wait_for(lk, std::chrono::milliseconds(timeoutMs),
                            [&] { return turnsStarted_ >= n; });
    }
    int turnsStarted() {
        std::lock_guard<std::mutex> lk(mtx_);
        return turnsStarted_;
    }
    int maxConcurrentTurns() {
        std::lock_guard<std::mutex> lk(mtx_);
        return maxConcurrent_;
    }
    bool waitNativeAgentsStarted(int n, int timeoutMs) {
        return waitCount(nativeAgentsStarted_, n, timeoutMs);
    }
    int nativeAgentsStarted() {
        std::lock_guard<std::mutex> lk(mtx_);
        return nativeAgentsStarted_;
    }
    int maxConcurrentNativeAgents() {
        std::lock_guard<std::mutex> lk(mtx_);
        return maxConcurrentNativeAgents_;
    }
    int controlInfoRequests() {
        std::lock_guard<std::mutex> lk(mtx_);
        return controlInfoRequests_;
    }
    bool waitSimsStarted(int n, int timeoutMs) { return waitCount(simsStarted_, n, timeoutMs); }
    int simsStarted() {
        std::lock_guard<std::mutex> lk(mtx_);
        return simsStarted_;
    }
    bool waitProbesStarted(int n, int timeoutMs) { return waitCount(probesStarted_, n, timeoutMs); }
    int probesStarted() {
        std::lock_guard<std::mutex> lk(mtx_);
        return probesStarted_;
    }
    // Probe calls whose after_broadcasts burst (written after the reply) is fully sent.
    bool waitAfterPushesSent(int n, int timeoutMs) {
        return waitCount(afterPushesSent_, n, timeoutMs);
    }
    int afterPushesSent() {
        std::lock_guard<std::mutex> lk(mtx_);
        return afterPushesSent_;
    }
    // Hold every sim_run_topology that carries "hold":true until releaseSims().
    void holdSims() {
        std::lock_guard<std::mutex> lk(mtx_);
        releaseSims_ = false;
    }
    void releaseSims() {
        {
            std::lock_guard<std::mutex> lk(mtx_);
            releaseSims_ = true;
        }
        cv_.notify_all();
    }
    int activeSims() {
        std::lock_guard<std::mutex> lk(mtx_);
        return activeSims_;
    }
    int maxConcurrentSims() {
        std::lock_guard<std::mutex> lk(mtx_);
        return maxConcurrentSims_;
    }
    int activeTurns() {
        std::lock_guard<std::mutex> lk(mtx_);
        return activeTurns_;
    }
    int meshesServed() {
        std::lock_guard<std::mutex> lk(mtx_);
        return meshesServed_;
    }
    void holdMetadata() {
        std::lock_guard<std::mutex> lk(mtx_);
        releaseMetadata_ = false;
    }
    bool releaseMetadata() {
        std::unique_lock<std::mutex> lk(mtx_);
        releaseMetadata_ = true;
        cv_.notify_all();
        return cv_.wait_for(lk, std::chrono::seconds(5), [&] { return heldMetadata_ == 0; });
    }
    bool waitMetadataStarted(int n, int timeoutMs) {
        return waitCount(metadataStarted_, n, timeoutMs);
    }
    int metadataStarted() {
        std::lock_guard<std::mutex> lk(mtx_);
        return metadataStarted_;
    }
    // Refuse every hello like a draining hostd (Server::admitWhileDraining).
    void refuseHellos(bool refuse) {
        std::lock_guard<std::mutex> lk(mtx_);
        refuseHellos_ = refuse;
    }

private:
    bool waitCount(const int& counter, int n, int timeoutMs) {
        std::unique_lock<std::mutex> lk(mtx_);
        return cv_.wait_for(lk, std::chrono::milliseconds(timeoutMs),
                            [&] { return counter >= n; });
    }

    // A wait that teardown cuts short.
    void waitUnlessStopping(int ms) {
        if (ms <= 0) return;
        std::unique_lock<std::mutex> lk(mtx_);
        cv_.wait_for(lk, std::chrono::milliseconds(ms), [&] { return stopping_; });
    }

    static void push(int fd, const json& msg) {
        const std::string out = msg.dump() + "\n";
        (void)::send(fd, out.data(), out.size(), MSG_NOSIGNAL);
    }
    static json agentEvent(int i) {
        return {{"event", "agent.event"}, {"data", {{"kind", "sim.progress"}, {"i", i}}}};
    }
    static json docChanged(int i) {
        return {{"event", "doc.changed"}, {"data", {{"doc_id", "doc-cpp00000002"}, {"i", i}}}};
    }
    // Server::drainRefusal for a stale-binary drain.
    static json drainRefusal() {
        return {{"code", "daemon_draining"},
                {"reason", "stale_binary"},
                {"daemon_pid", static_cast<long>(::getpid())},
                {"clients", 1},
                {"active_clients", 1},
                {"children", 0},
                {"hard_stop", false},
                {"changed_path", "/opt/chudware/lib/libchudware.so"},
                {"message", "The engine changed on disk (libchudware.so). This daemon runs the "
                            "old engine and is draining."},
                {"hint", "Restart Chudware to load the new engine."}};
    }

    // Scripted call body: `events` agent.event (sim.progress) pushes interleaved with
    // `broadcasts` doc.changed pushes, interval_ms apart, then silent_ms of nothing — a solver
    // factorizing. False = hang up (`close`) instead of replying.
    bool script(int fd, const json& p) {
        const int events = p.value("events", 0);
        const int broadcasts = p.value("broadcasts", 0);
        const int intervalMs = p.value("interval_ms", 0);
        for (int i = 0; i < std::max(events, broadcasts); ++i) {
            if (i < events) push(fd, agentEvent(i));
            if (i < broadcasts) push(fd, docChanged(i));
            waitUnlessStopping(intervalMs);
        }
        waitUnlessStopping(p.value("silent_ms", 0));
        return !p.value("close", false);
    }

    void bump(int& counter) {
        {
            std::lock_guard<std::mutex> lk(mtx_);
            ++counter;
        }
        cv_.notify_all();
    }

    void acceptLoop() {
        for (;;) {
            const int fd = ::accept(listenFd_, nullptr, nullptr);
            if (fd < 0) return;
            std::lock_guard<std::mutex> lk(mtx_);
            if (stopping_) {
                ::close(fd);
                return;
            }
            connFds_.push_back(fd);
            const int clientId = nextClientId_++;
            connThreads_.emplace_back([this, fd, clientId] { serve(fd, clientId); });
        }
    }

    // nullopt = hang up without replying.
    std::optional<json> handle(int fd, int clientId, const std::string& method,
                               const json& params) {
        if (method == "hello")
            return json{{"state", "ready"}, {"client_id", clientId}};
        if (method == "worker_control_info") {
            bump(controlInfoRequests_);
            return json{{"transport", "unix"},
                        {"endpoint", "/nonexistent/chudware-test-control.sock"},
                        {"token", std::string(64, 'a')}};
        }
        if (method == "agent_admin_turn") {
            std::unique_lock<std::mutex> lk(mtx_);
            ++turnsStarted_;
            ++activeTurns_;
            const std::string runId = params.value("run_id", std::string());
            activeRunIds_.push_back(runId);
            if (activeTurns_ > maxConcurrent_) maxConcurrent_ = activeTurns_;
            cv_.notify_all();
            cv_.wait(lk, [&] { return releaseTurns_ || stopping_; });
            --activeTurns_;
            activeRunIds_.erase(std::find(activeRunIds_.begin(), activeRunIds_.end(), runId));
            return json{{"ok", true}, {"text", "done"}, {"client_id", clientId}};
        }
        if (method == "agent_run" || method == "agent_run_v2") {
            std::unique_lock<std::mutex> lk(mtx_);
            ++nativeAgentsStarted_;
            ++activeNativeAgents_;
            maxConcurrentNativeAgents_ = std::max(maxConcurrentNativeAgents_, activeNativeAgents_);
            cv_.notify_all();
            cv_.wait(lk, [&] { return releaseTurns_ || stopping_; });
            --activeNativeAgents_;
            return json{{"ok", true}, {"text", "done"}};
        }
        if (method == "agent_cancel" || method == "agent_steer" || method == "agent_answer") {
            std::lock_guard<std::mutex> lk(mtx_);
            const std::string runId = params.value("run_id", std::string());
            const bool active =
                std::find(activeRunIds_.begin(), activeRunIds_.end(), runId) != activeRunIds_.end();
            const char* flag = method == "agent_cancel" ? "cancelled" :
                               method == "agent_steer" ? "steered" : "answered";
            json reply{{"ok", true}, {"run_id", runId}, {flag, active}};
            if (params.contains("provider")) reply["provider"] = params["provider"];
            return reply;
        }
        if (method == "subscription_accounts") {
            {
                std::unique_lock<std::mutex> lk(mtx_);
                ++metadataStarted_;
                if (params.value("hold_metadata", false)) {
                    ++heldMetadata_;
                    cv_.notify_all();
                    // A broken client must fail this test, not wedge the whole suite:
                    // release automatically after 10 s if the main thread queued behind us.
                    cv_.wait_for(lk, std::chrono::seconds(10),
                                 [&] { return releaseMetadata_ || stopping_; });
                    --heldMetadata_;
                }
                cv_.notify_all();
            }
            if (!script(fd, params)) return std::nullopt;
            return json{{"accounts", json::array()},
                        {"tag", params.value("tag", 0)},
                        {"client_id", clientId}};
        }
        if (method == "doc_list")
            return json{{"docs", json::array()}, {"client_id", clientId}};
        if (method == "doc_new")
            return json{{"doc_id", "doc-cpp00000003"}, {"client_id", clientId}};
        if (method == "doc_activate")
            return json{{"doc_id", "doc-cpp00000002"}, {"revision", 1}, {"tree", json::object()}};
        if (method == "sim_mesh") {
            bump(meshesServed_);
            return json{{"ok", true}, {"mesh", "preview"}};
        }
        if (method == "sim_run_topology") {
            {
                std::unique_lock<std::mutex> lk(mtx_);
                ++simsStarted_;
                ++activeSims_;
                if (activeSims_ > maxConcurrentSims_) maxConcurrentSims_ = activeSims_;
                cv_.notify_all();
                if (params.value("hold", false))
                    cv_.wait(lk, [&] { return releaseSims_ || stopping_; });
            }
            const bool reply = script(fd, params);
            {
                std::lock_guard<std::mutex> lk(mtx_);
                --activeSims_;
            }
            if (!reply) return std::nullopt;
            // A solve stopped by sim_cancel answers with its partial design (hostd's error
            // envelope always carries code/message).
            if (params.value("reply_error", false))
                return json{{"code", "cancelled"},
                            {"message", "cancelled"},
                            {"termination", "cancelled"},
                            {"density", json::array({0.25, 0.75})}};
            return json{{"ok", true}, {"marker", "topology-done"}};
        }
        if (method == "transport_probe") {
            bump(probesStarted_);
            if (!script(fd, params)) return std::nullopt;
            return json{{"tag", params.value("tag", 0)}};
        }
        return json::object();
    }

    // Forget then close a connection fd under mtx_ (see ~FakeHostd).
    void closeConn(int fd) {
        std::lock_guard<std::mutex> lk(mtx_);
        connFds_.erase(std::remove(connFds_.begin(), connFds_.end(), fd), connFds_.end());
        ::close(fd);
    }

    void serve(int fd, int clientId) {
        std::string buf;
        char chunk[4096];
        for (;;) {
            const ssize_t n = ::read(fd, chunk, sizeof chunk);
            if (n <= 0) break;
            buf.append(chunk, static_cast<size_t>(n));
            size_t nl;
            while ((nl = buf.find('\n')) != std::string::npos) {
                const std::string line = buf.substr(0, nl);
                buf.erase(0, nl + 1);
                json req = json::parse(line, nullptr, false);
                if (!req.is_object()) continue;
                json params = req.value("params", json::object());
                if (!params.is_object()) params = json::object();
                const std::string method = req.value("method", std::string());
                bool refuse = false;
                {
                    std::lock_guard<std::mutex> lk(mtx_);
                    refuse = refuseHellos_ && method == "hello";
                }
                if (refuse) {
                    // Like hostd: the event, then the refusal for the hello id, then close.
                    push(fd, {{"event", "daemon.draining"}, {"reason", "stale_binary"}});
                    push(fd, {{"id", req.value("id", 0)}, {"error", drainRefusal()}});
                    closeConn(fd);
                    return;
                }
                if (params.contains("reply_frame")) {
                    // Exercise the real client parser with the peer's exact wire envelope.
                    push(fd, params["reply_frame"]);
                    continue;
                }
                const std::optional<json> result = handle(fd, clientId, method, params);
                if (!result) {
                    closeConn(fd);
                    return;
                }
                const char* key = params.value("reply_error", false) ? "error" : "result";
                const json reply = {{"id", req.value("id", 0)}, {key, *result}};
                const std::string out = reply.dump() + "\n";
                // MSG_NOSIGNAL: a client that already hung up must not SIGPIPE the test.
                if (::send(fd, out.data(), out.size(), MSG_NOSIGNAL) < 0) break;
                if (method == "transport_probe" && params.value("after_broadcasts", 0) > 0) {
                    // Pushes that reach an IDLE client: only pollAlive/drainEvents read them.
                    // Past the socket buffer each send blocks until the client reads.
                    for (int i = 0; i < params.value("after_broadcasts", 0); ++i)
                        push(fd, docChanged(i));
                    bump(afterPushesSent_);
                }
            }
        }
        closeConn(fd);
    }

    std::string path_;
    int listenFd_ = -1;
    std::thread acceptThread_;
    std::mutex mtx_;
    std::condition_variable cv_;
    std::vector<int> connFds_;
    std::vector<std::thread> connThreads_;
    bool stopping_ = false;
    bool refuseHellos_ = false;
    bool releaseTurns_ = true;
    bool releaseSims_ = true;
    bool releaseMetadata_ = true;
    int heldMetadata_ = 0;
    int metadataStarted_ = 0;
    int nextClientId_ = 1;
    int turnsStarted_ = 0;
    int activeTurns_ = 0;
    int maxConcurrent_ = 0;
    std::vector<std::string> activeRunIds_;
    int nativeAgentsStarted_ = 0;
    int activeNativeAgents_ = 0;
    int maxConcurrentNativeAgents_ = 0;
    int controlInfoRequests_ = 0;
    int simsStarted_ = 0;
    int activeSims_ = 0;
    int maxConcurrentSims_ = 0;
    int meshesServed_ = 0;
    int probesStarted_ = 0;
    int afterPushesSent_ = 0;
};

static json turnParams(const std::string& runId) {
    return {{"run_id", runId}, {"request", "build it"}, {"doc_id", ""}};
}

// The live bug: an hours-long turn on the job thread must not park the GUI's st.rpc lane.
static void asyncLaneRunsDuringTurn(FakeHostd& hostd) {
    WorkerClient w;
    hostd.holdTurns();
    const int before = hostd.turnsStarted();
    std::thread job([&] {
        json r;
        std::string e;
        w.call("agent_admin_turn", turnParams("run-lane"), r, e, 60000);
    });
    CHECK(hostd.waitTurnsStarted(before + 1, 5000));

    bool accountsDone = false, activateDone = false;
    bool accountsOk = false, activateOk = false;
    {
        core::AsyncRpc rpc(w);
        // FIFO: a worker-routed call first, then the tab switch's activation behind it.
        rpc.call("subscription_accounts", json::object(), [&](const core::RpcResult& r) {
            accountsDone = true;
            accountsOk = r.ok;
        }, 30000);
        rpc.call("doc_activate", json{{"doc_id", "doc-cpp00000002"}},
                 [&](const core::RpcResult& r) {
                     activateDone = true;
                     activateOk = r.ok && r.result.value("revision", 0) == 1;
                 }, 30000);
        const auto deadline = Clock::now() + std::chrono::seconds(5);
        while (!(accountsDone && activateDone) && Clock::now() < deadline) {
            rpc.drain();
            std::this_thread::sleep_for(std::chrono::milliseconds(5));
        }
        CHECK(accountsDone);
        CHECK(accountsOk);
        CHECK(activateDone);
        CHECK(activateOk);
        // Unblock before AsyncRpc teardown so a failing build still exits.
        hostd.releaseTurns();
        while (rpc.busy()) {
            rpc.drain();
            std::this_thread::sleep_for(std::chrono::milliseconds(5));
        }
        job.join();  // before ~AsyncRpc: its shutdown aborts the shared engine connections
    }
}

// Concurrent conversations lease different connections and keep hostd-routed control live.
static void onlineTurnsOverlap(FakeHostd& hostd) {
    WorkerClient w;
    hostd.holdTurns();
    const int before = hostd.turnsStarted();
    const int controlInfoBefore = hostd.controlInfoRequests();
    json results[2];
    bool ok[2] = {false, false};
    auto runTurn = [&](int index, const char* id) {
        std::string e;
        ok[index] = w.call("agent_admin_turn", turnParams(id), results[index], e, 60000);
    };
    std::thread first(runTurn, 0, "run-a");
    CHECK(hostd.waitTurnsStarted(before + 1, 5000));
    std::thread second(runTurn, 1, "run-b");
    CHECK(hostd.waitTurnsStarted(before + 2, 5000));
    CHECK(hostd.activeTurns() == 2);
    CHECK(hostd.maxConcurrentTurns() == 2);
    CHECK(hostd.controlInfoRequests() == controlInfoBefore);
    for (const char* id : {"run-a", "run-b"}) {
        json r;
        std::string e;
        const std::string provider = std::string(id) == "run-a" ? "anthropic" : "xai";
        CHECK(w.callAgentSteer(id, "continue", json::array(), "steer-test", r, e, provider));
        CHECK(r.value("steered", false));
        CHECK(r.value("run_id", std::string()) == id);
        CHECK(r.value("provider", std::string()) == provider);
        CHECK(w.callAgentAnswer(id, "batch-test", json{{"question", "yes"}}, false, r, e, provider));
        CHECK(r.value("answered", false));
        CHECK(r.value("run_id", std::string()) == id);
        CHECK(r.value("provider", std::string()) == provider);
        CHECK(w.callAgentCancel(id, r, e, provider));
        CHECK(r.value("cancelled", false));
        CHECK(r.value("run_id", std::string()) == id);
        CHECK(r.value("provider", std::string()) == provider);
    }
    hostd.releaseTurns();
    first.join();
    second.join();
    CHECK(ok[0] && ok[1]);
    CHECK(results[0].value("client_id", 0) != results[1].value("client_id", 0));
    CHECK(hostd.turnsStarted() == before + 2);
}

// Offline native agents keep their existing per-client serialization.
static void nativeAgentsNeverOverlap(FakeHostd& hostd) {
    WorkerClient w;
    hostd.holdTurns();
    const int before = hostd.nativeAgentsStarted();
    bool ok[2] = {false, false};
    auto runAgent = [&](int index) {
        json r;
        std::string e;
        ok[index] = w.call("agent_run_v2",
                           json{{"request", "build it"}, {"doc_id", "doc-cpp00000002"}},
                           r, e, 60000);
    };
    std::thread first(runAgent, 0);
    CHECK(hostd.waitNativeAgentsStarted(before + 1, 5000));
    std::thread second(runAgent, 1);
    CHECK(!hostd.waitNativeAgentsStarted(before + 2, 500));
    CHECK(hostd.maxConcurrentNativeAgents() == 1);
    hostd.releaseTurns();
    first.join();
    second.join();
    CHECK(ok[0] && ok[1]);
    CHECK(hostd.nativeAgentsStarted() == before + 2);
    CHECK(hostd.maxConcurrentNativeAgents() == 1);
}

// The pool is bounded even when hostd accepts more concurrent turns.
static void agentConnectionPoolIsBounded(FakeHostd& hostd) {
    WorkerClient w;
    hostd.holdTurns();
    const int before = hostd.turnsStarted();
    bool ok[9] = {};
    std::vector<std::thread> jobs;
    jobs.reserve(9);
    auto runTurn = [&](int index) {
        json r;
        std::string e;
        ok[index] = w.call("agent_admin_turn",
                           turnParams("run-pool-" + std::to_string(index)), r, e, 60000);
    };
    for (int i = 0; i < 8; ++i)
        jobs.emplace_back(runTurn, i);
    CHECK(hostd.waitTurnsStarted(before + 8, 5000));
    CHECK(hostd.activeTurns() == 8);
    jobs.emplace_back(runTurn, 8);
    CHECK(!hostd.waitTurnsStarted(before + 9, 500));
    CHECK(hostd.maxConcurrentTurns() == 8);
    hostd.releaseTurns();
    for (auto& job : jobs) job.join();
    for (bool succeeded : ok) CHECK(succeeded);
    CHECK(hostd.turnsStarted() == before + 9);
    CHECK(hostd.maxConcurrentTurns() == 8);
}

// Native topology solves wait with timeoutMs=-1 on engine_backend's dedicated simulation
// connection. Their mesh, assembly and sparse factorization phases legitimately push nothing for
// seconds, so a silent stretch past EngineClient's 200 ms abort-poll slice must not fail the call,
// the progress they do stream must still reach onEvent live through that discarding lane, and
// the GUI's other requests on the same WorkerClient must keep completing meanwhile.
static void silentTopologySolveStaysPending(FakeHostd& hostd) {
    constexpr int kSilentSolveMs = 650;
    constexpr int kProgress = 3;
    WorkerClient w;
    const int before = hostd.simsStarted();
    std::atomic<bool> simDone{false};
    std::atomic<int> progress{0};
    bool simOk = false;
    json simResult;
    std::string simErr;
    const auto start = Clock::now();
    Clock::time_point simEnd{};
    std::thread job([&] {
        simOk = w.call("sim_run_topology",
                       json{{"study", {{"type", "topology"}}},
                            {"events", kProgress},
                            {"silent_ms", kSilentSolveMs}},
                       simResult, simErr, -1, [&](const json& ev) {
                           if (ev.is_object() &&
                               ev.value("kind", std::string()) == "sim.progress")
                               ++progress;
                       });
        simEnd = Clock::now();
        simDone = true;
    });
    CHECK(hostd.waitSimsStarted(before + 1, 5000));

    json activated;
    std::string activateErr;
    const bool activateOk = w.call("doc_activate", json{{"doc_id", "doc-cpp00000002"}},
                                   activated, activateErr, 5000);
    CHECK(activateOk);
    CHECK(activated.is_object() && activated.value("revision", 0) == 1);
    CHECK(!simDone.load());  // the GUI call finished while the solve was still pending

    job.join();
    if (!simOk) std::printf("  sim_run_topology failed: %s\n", simErr.c_str());
    CHECK(simOk);
    CHECK(simResult.is_object() &&
          simResult.value("marker", std::string()) == "topology-done");
    CHECK(simEnd - start >= std::chrono::milliseconds(kSilentSolveMs));
    CHECK(progress.load() == kProgress);
}

static json heldTopologyParams(bool replyError = false) {
    json p = {{"study", {{"type", "topology"}}}, {"hold", true}};
    if (replyError) p["reply_error"] = true;
    return p;
}

// The topology twin of asyncLaneRunsDuringTurn. SimClient's job thread blocks in
// sim_run_topology for the whole optimization; a worker-routed call (not engine-bound, so the
// side channel never rescues it) and the tab switch's doc_activate queued behind it on the
// single-FIFO AsyncRpc lane must both finish while the solve is still running. The held solve is
// released regardless of the checks, so a failing build still exits.
static void asyncLaneRunsDuringTopology(FakeHostd& hostd) {
    WorkerClient w;
    hostd.holdSims();
    const int before = hostd.simsStarted();
    std::atomic<bool> simDone{false};
    bool simOk = false;
    json simResult;
    std::string simErr;
    std::thread job([&] {
        simOk = w.call("sim_run_topology", heldTopologyParams(/*replyError=*/true), simResult,
                       simErr, -1);
        simDone = true;
    });
    CHECK(hostd.waitSimsStarted(before + 1, 5000));

    bool accountsDone = false, activateDone = false;
    bool accountsOk = false, activateOk = false;
    {
        core::AsyncRpc rpc(w);
        rpc.call("subscription_accounts", json::object(), [&](const core::RpcResult& r) {
            accountsDone = true;
            accountsOk = r.ok && r.result.contains("accounts");
        }, 30000);
        rpc.call("doc_activate", json{{"doc_id", "doc-cpp00000002"}},
                 [&](const core::RpcResult& r) {
                     activateDone = true;
                     activateOk = r.ok && r.result.value("revision", 0) == 1;
                 }, 30000);
        const auto deadline = Clock::now() + std::chrono::seconds(5);
        while (!(accountsDone && activateDone) && Clock::now() < deadline) {
            rpc.drain();
            std::this_thread::sleep_for(std::chrono::milliseconds(5));
        }
        CHECK(accountsDone);
        CHECK(accountsOk);
        CHECK(activateDone);
        CHECK(activateOk);
        CHECK(!simDone.load());            // both finished while the solve was still running
        CHECK(hostd.activeSims() == 1);
        hostd.releaseSims();
        const auto drainDeadline = Clock::now() + std::chrono::seconds(10);
        while (rpc.busy() && Clock::now() < drainDeadline) {
            rpc.drain();
            std::this_thread::sleep_for(std::chrono::milliseconds(5));
        }
        job.join();  // before ~AsyncRpc: its shutdown aborts the shared engine connections
    }
    // A cancelled solve's structured reply keeps its partial design for SimClient.
    if (!simOk) std::printf("  held sim_run_topology failed: %s\n", simErr.c_str());
    CHECK(simOk);
    CHECK(simResult.is_object() && simResult.value("termination", std::string()) == "cancelled");
    CHECK(simResult.is_object() && simResult.contains("density") &&
          simResult["density"].size() == 2);
}

// The mesh preview twin. SimClient runs the solve on its OWN WorkerClient, while the Mesh
// preview goes through the GUI's st.rpc (st.worker). Both share the process's single simulation
// connection, so a preview that waited for that lane would park the GUI FIFO — and the
// doc_activate behind it — for the whole optimization. Both must finish while the solve runs.
static void meshPreviewDuringTopology(FakeHostd& hostd) {
    WorkerClient simWorker;
    WorkerClient gui;
    hostd.holdSims();
    const int before = hostd.simsStarted();
    const int meshesBefore = hostd.meshesServed();
    std::atomic<bool> simDone{false};
    bool simOk = false;
    std::thread job([&] {
        json r;
        std::string e;
        simOk = simWorker.call("sim_run_topology", heldTopologyParams(), r, e, -1);
        simDone = true;
    });
    CHECK(hostd.waitSimsStarted(before + 1, 5000));

    bool meshDone = false, activateDone = false;
    bool meshOk = false, activateOk = false;
    {
        core::AsyncRpc rpc(gui);
        rpc.call("sim_mesh", json{{"study", {{"type", "topology"}}}},
                 [&](const core::RpcResult& r) {
                     meshDone = true;
                     meshOk = r.ok && r.result.value("mesh", std::string()) == "preview";
                 }, 30000);
        rpc.call("doc_activate", json{{"doc_id", "doc-cpp00000002"}},
                 [&](const core::RpcResult& r) {
                     activateDone = true;
                     activateOk = r.ok && r.result.value("revision", 0) == 1;
                 }, 30000);
        const auto deadline = Clock::now() + std::chrono::seconds(5);
        while (!(meshDone && activateDone) && Clock::now() < deadline) {
            rpc.drain();
            std::this_thread::sleep_for(std::chrono::milliseconds(5));
        }
        CHECK(meshDone);
        CHECK(meshOk);
        CHECK(activateDone);
        CHECK(activateOk);
        CHECK(!simDone.load());  // both finished while the solve was still running
        CHECK(hostd.activeSims() == 1);
        hostd.releaseSims();
        const auto drainDeadline = Clock::now() + std::chrono::seconds(10);
        while (rpc.busy() && Clock::now() < drainDeadline) {
            rpc.drain();
            std::this_thread::sleep_for(std::chrono::milliseconds(5));
        }
        job.join();  // before ~AsyncRpc: its shutdown aborts the shared engine connections
    }
    CHECK(simOk);
    CHECK(hostd.meshesServed() == meshesBefore + 1);
    // With the lane idle again a preview runs as before (gui is aborted by ~AsyncRpc; the
    // engine connections it closed reconnect on the next call).
    json r;
    std::string e;
    CHECK(simWorker.call("sim_mesh", json{{"study", {{"type", "topology"}}}}, r, e, 5000));
    CHECK(r.is_object() && r.value("mesh", std::string()) == "preview");
    CHECK(hostd.meshesServed() == meshesBefore + 2);
}

// Same-kind serialization: a second topology run on one client reaches hostd only after the
// first returned.
static void topologyRunsNeverOverlap(FakeHostd& hostd) {
    WorkerClient w;
    hostd.holdSims();
    const int before = hostd.simsStarted();
    auto runSim = [&] {
        json r;
        std::string e;
        w.call("sim_run_topology", heldTopologyParams(), r, e, -1);
    };
    std::thread first(runSim);
    CHECK(hostd.waitSimsStarted(before + 1, 5000));
    std::thread second(runSim);
    CHECK(!hostd.waitSimsStarted(before + 2, 500));
    CHECK(hostd.maxConcurrentSims() == 1);
    hostd.releaseSims();
    first.join();
    second.join();
    CHECK(hostd.simsStarted() == before + 2);
    CHECK(hostd.maxConcurrentSims() == 1);
}

// Independent lanes: a topology run on one client starts while an agent turn on the same client
// is still in flight, and both are active together.
static void agentAndTopologyLanesAreIndependent(FakeHostd& hostd) {
    WorkerClient w;
    hostd.holdTurns();
    hostd.holdSims();
    const int turnsBefore = hostd.turnsStarted();
    const int simsBefore = hostd.simsStarted();
    std::thread turn([&] {
        json r;
        std::string e;
        w.call("agent_admin_turn", turnParams("run-lanes"), r, e, 60000);
    });
    CHECK(hostd.waitTurnsStarted(turnsBefore + 1, 5000));
    std::thread sim([&] {
        json r;
        std::string e;
        w.call("sim_run_topology", heldTopologyParams(), r, e, -1);
    });
    CHECK(hostd.waitSimsStarted(simsBefore + 1, 5000));
    CHECK(hostd.activeTurns() == 1);
    CHECK(hostd.activeSims() == 1);
    hostd.releaseTurns();
    hostd.releaseSims();
    turn.join();
    sim.join();
}

// ---- EngineClient transport ---------------------------------------------------------------
using chudware_ipc::EngineClient;

static bool connectProbe(EngineClient& c, bool discardUnconsumedEvents,
                          const char* clientKind = "test") {
    EngineClient::Options o;
    o.hostdBin = engine::hostdBinHint();
    o.clientKind = clientKind;
    o.allowSpawn = false;
    o.discardUnconsumedEvents = discardUnconsumedEvents;
    std::string e;
    if (c.connectOrSpawn(o, e)) return true;
    std::printf("  probe connect failed: %s\n", e.c_str());
    return false;
}

static int probeTag(const json& r) { return r.is_object() ? r.value("tag", 0) : 0; }
static int probeClientId(const json& r) {
    return r.is_object() ? r.value("client_id", 0) : 0;
}
static void uncorrelatedProtocolErrorsRetireConnection() {
    EngineClient c;
    CHECK(connectProbe(c, false));
    CHECK(c.serverInfo().value("state", std::string()) == "ready");
    const auto generation = c.reconnectGeneration();
    const json refusal = {{"code", -32700}, {"message", "invalid JSON request"}};
    json r;
    std::string e;
    CHECK(!c.call("transport_probe",
                  {{"reply_frame", {{"id", nullptr}, {"error", refusal}}}},
                  r, e, 5000));
    CHECK(r == refusal);
    CHECK(e == "invalid JSON request");
    CHECK(!c.connected());
    CHECK(c.reconnectGeneration() == generation);
    e.clear();
    CHECK(c.call("transport_probe", {{"tag", 71}}, r, e, 5000));
    CHECK(probeTag(r) == 71);
    CHECK(c.connected());
    CHECK(c.reconnectGeneration() == generation + 1);

    for (const auto& frame : {
             json{{"id", nullptr}, {"result", json::object()}},
             json{{"id", nullptr}, {"error", refusal}, {"result", json::object()}},
             json{{"id", "2"}, {"result", json::object()}},
             json{{"id", 2.0}, {"result", json::object()}},
             json{{"id", false}, {"result", json::object()}},
             json{{"result", json::object()}},
             json::array()}) {
        e.clear();
        CHECK(!c.call("transport_probe", {{"reply_frame", frame}}, r, e, 5000));
        CHECK(e == "daemon response has an invalid request id");
        CHECK(!c.connected());
        e.clear();
        CHECK(c.call("transport_probe", {{"tag", 72}}, r, e, 5000));
        CHECK(probeTag(r) == 72);
    }
}


// Server-side holds establish ordering; no sleep is used to guess whether a query started.
// The slow query stays gated until both independent calls have finished, then also spends
// 2 s silent. Time checks have a generous margin and are secondary to the ordering checks.
static void backgroundQueriesDoNotBlockPrimary(FakeHostd& hostd) {
    EngineClient c;
    CHECK(connectProbe(c, false, "gui"));
    const int primaryId = probeClientId(c.serverInfo());
    CHECK(primaryId != 0);
    hostd.holdMetadata();
    const int before = hostd.metadataStarted();
    std::atomic<bool> slowDone{false};
    bool slowOk = false;
    json slowResult;
    Clock::time_point slowEnd{};
    const auto started = Clock::now();
    std::thread slow([&] {
        std::string e;
        slowOk = c.call("subscription_accounts",
                        json{{"hold_metadata", true}, {"silent_ms", 2000}, {"tag", 1}},
                        slowResult, e, 15000);
        slowEnd = Clock::now();
        slowDone = true;
    });
    CHECK(hostd.waitMetadataStarted(before + 1, 5000));

    json r;
    std::string e;
    const auto primaryStarted = Clock::now();
    CHECK(c.call("doc_list", json::object(), r, e, 5000));
    CHECK(probeClientId(r) == primaryId);
    CHECK(c.call("doc_new", json{{"name", "startup"}}, r, e, 5000));
    CHECK(probeClientId(r) == primaryId);
    const auto primaryEnd = Clock::now();
    CHECK(primaryEnd - primaryStarted < std::chrono::milliseconds(1500));
    CHECK(!slowDone.load());

    bool secondOk = false;
    json secondResult;
    Clock::time_point secondEnd{};
    const auto secondStarted = Clock::now();
    std::thread second([&] {
        std::string err;
        secondOk = c.call("subscription_accounts", json{{"tag", 2}},
                          secondResult, err, 5000);
        secondEnd = Clock::now();
    });
    second.join();
    CHECK(secondOk);
    CHECK(probeTag(secondResult) == 2);
    CHECK(probeClientId(secondResult) != primaryId);
    CHECK(secondEnd - secondStarted < std::chrono::milliseconds(1500));
    CHECK(!slowDone.load());

    CHECK(hostd.releaseMetadata());
    slow.join();
    CHECK(slowOk);
    CHECK(probeTag(slowResult) == 1);
    CHECK(probeClientId(slowResult) != primaryId);
    CHECK(probeClientId(slowResult) != probeClientId(secondResult));
    CHECK(slowEnd > primaryEnd && slowEnd > secondEnd);
    CHECK(slowEnd - started >= std::chrono::milliseconds(2000));
}

// Keep failure and reconnect on the SAME background thread so it reuses the side slot.
// Pause between them while the primary thread checks that its own socket was untouched.
enum class BackgroundFailure { Eof, Timeout, ProtocolRefusal };

static void backgroundQueryFailureIsIsolated(BackgroundFailure failure) {
    EngineClient c;
    CHECK(connectProbe(c, false, "gui"));
    const int primaryId = probeClientId(c.serverInfo());
    CHECK(primaryId != 0);
    const auto generation = c.reconnectGeneration();
    std::mutex mtx;
    std::condition_variable cv;
    bool failed = false, resume = false;
    bool firstOk = false, failureOk = true, reconnectOk = false;
    json firstResult, failureResult, reconnectResult;
    std::string failureErr;
    std::thread background([&] {
        std::string err;
        firstOk = c.call("subscription_accounts", json{{"tag", 3}}, firstResult, err, 5000);
        json params;
        if (failure == BackgroundFailure::Eof) params = {{"close", true}};
        else if (failure == BackgroundFailure::Timeout)
            params = {{"silent_ms", 2000}, {"tag", 4}};
        else
            params = {{"reply_frame", {{"id", nullptr},
                        {"error", {{"code", -32700}, {"message", "invalid JSON request"}}}}}};
        failureOk = c.call("subscription_accounts", params, failureResult, failureErr,
                           failure == BackgroundFailure::Timeout ? 250 : 5000);
        {
            std::unique_lock<std::mutex> lk(mtx);
            failed = true;
            cv.notify_all();
            cv.wait(lk, [&] { return resume; });
        }
        reconnectOk = c.call("subscription_accounts", json{{"tag", 5}},
                             reconnectResult, err, 5000);
    });
    {
        std::unique_lock<std::mutex> lk(mtx);
        CHECK(cv.wait_for(lk, std::chrono::seconds(10), [&] { return failed; }));
    }
    CHECK(c.connected());
    CHECK(c.pollAlive());
    CHECK(c.reconnectGeneration() == generation);
    json r;
    std::string err;
    CHECK(c.call("doc_list", json::object(), r, err, 5000));
    CHECK(probeClientId(r) == primaryId);
    {
        std::lock_guard<std::mutex> lk(mtx);
        resume = true;
    }
    cv.notify_all();
    background.join();
    CHECK(firstOk);
    CHECK(!failureOk);
    if (failure == BackgroundFailure::Eof)
        CHECK(failureErr == "daemon connection closed");
    else if (failure == BackgroundFailure::Timeout)
        CHECK(failureErr.find("timed out") != std::string::npos);
    else {
        CHECK(failureErr == "invalid JSON request");
        CHECK(failureResult == json({{"code", -32700}, {"message", "invalid JSON request"}}));
    }
    CHECK(reconnectOk);
    CHECK(probeTag(reconnectResult) == 5);  // never the timed-out call's stale tag 4
    CHECK(probeClientId(firstResult) != primaryId);
    CHECK(probeClientId(reconnectResult) != primaryId);
    CHECK(probeClientId(firstResult) != probeClientId(reconnectResult));
    CHECK(c.connected());
    CHECK(c.reconnectGeneration() == generation);
}

static void backgroundQueryEofIsIsolated() {
    backgroundQueryFailureIsIsolated(BackgroundFailure::Eof);
}
static void backgroundQueryTimeoutIsIsolated() {
    backgroundQueryFailureIsIsolated(BackgroundFailure::Timeout);
}
static void backgroundProtocolRefusalIsIsolated() {
    backgroundQueryFailureIsIsolated(BackgroundFailure::ProtocolRefusal);
}

static void disconnectWakesAllBackgroundQueries(FakeHostd& hostd) {
    EngineClient c;
    CHECK(connectProbe(c, false, "gui"));
    hostd.holdMetadata();
    const int before = hostd.metadataStarted();
    bool ok[2] = {true, true};
    std::string err[2];
    Clock::time_point ended[2];
    auto query = [&](int i) {
        json r;
        ok[i] = c.call("subscription_accounts", json{{"hold_metadata", true}},
                       r, err[i], -1);
        ended[i] = Clock::now();
    };
    std::thread first(query, 0);
    std::thread second(query, 1);
    CHECK(hostd.waitMetadataStarted(before + 2, 5000));
    const auto disconnected = Clock::now();
    c.disconnect();
    first.join();
    second.join();
    CHECK(!c.connected());
    for (int i = 0; i < 2; ++i) {
        CHECK(!ok[i]);
        CHECK(err[i] == "aborted");
        CHECK(ended[i] - disconnected < std::chrono::milliseconds(1500));
    }
    CHECK(hostd.releaseMetadata());
}

// An unbounded wait outlives silence but still ends when the peer hangs up, and the next
// call reconnects.
static void unboundedWaitEndsOnEof() {
    EngineClient c;
    CHECK(connectProbe(c, false));
    json r;
    std::string e;
    const auto t0 = Clock::now();
    const bool ok = c.call("transport_probe", json{{"silent_ms", 450}, {"close", true}}, r, e, -1);
    const auto took = Clock::now() - t0;
    CHECK(!ok);
    if (e != "daemon connection closed") std::printf("  EOF wait err: %s\n", e.c_str());
    CHECK(e == "daemon connection closed");
    CHECK(took >= std::chrono::milliseconds(400));  // survived two silent poll slices
    CHECK(took < std::chrono::seconds(3));
    CHECK(!c.connected());
    r = json();
    CHECK(c.call("transport_probe", json{{"tag", 7}}, r, e, 5000));
    CHECK(probeTag(r) == 7);
}

// abortPending() wakes an unbounded wait at once — not at the end of a poll slice — and the
// connection is usable again afterwards.
static void unboundedWaitEndsOnAbort(FakeHostd& hostd) {
    EngineClient c;
    CHECK(connectProbe(c, false));
    const int before = hostd.probesStarted();
    std::atomic<bool> done{false};
    bool ok = true;
    std::string e;
    Clock::time_point end{};
    std::thread caller([&] {
        json r;
        ok = c.call("transport_probe", json{{"silent_ms", 5000}}, r, e, -1);
        end = Clock::now();
        done = true;
    });
    CHECK(hostd.waitProbesStarted(before + 1, 5000));
    std::this_thread::sleep_for(std::chrono::milliseconds(450));
    CHECK(!done.load());  // still pending across silent poll slices
    const auto abortAt = Clock::now();
    c.abortPending();
    caller.join();
    CHECK(!ok);
    if (e != "aborted") std::printf("  abort err: %s\n", e.c_str());
    CHECK(e == "aborted");
    CHECK(end - abortAt < std::chrono::milliseconds(150));
    json r;
    CHECK(c.call("transport_probe", json{{"tag", 8}}, r, e, 5000));
    CHECK(probeTag(r) == 8);
}

// A bounded wait is ONE deadline: a steady push stream does not extend it. The timed-out
// connection is dropped, so the next call reads its own reply, never the stale one.
static void boundedWaitIsOneDeadlineAcrossPushes() {
    EngineClient c;
    CHECK(connectProbe(c, false));
    json r;
    std::string e;
    const auto t0 = Clock::now();
    const bool ok = c.call("transport_probe",
                           json{{"broadcasts", 40}, {"interval_ms", 25}, {"tag", 1}}, r, e, 300);
    const auto took = Clock::now() - t0;
    CHECK(!ok);
    CHECK(e.find("timed out") != std::string::npos);
    CHECK(took >= std::chrono::milliseconds(290));
    CHECK(took < std::chrono::milliseconds(800));  // the 1 s stream did not keep it alive
    CHECK(!c.connected());
    r = json();
    CHECK(c.call("transport_probe", json{{"tag", 2}}, r, e, 5000));
    CHECK(probeTag(r) == 2);
}

// Consume an IDLE client's pushes through the frame-loop entry points (pollAlive, then
// drainEvents) WHILE the fake hostd is still writing its post-reply burst. Past the socket buffer
// the sender blocks until the client reads, so waiting for the send to finish before consuming
// would deadlock on backpressure. Stops after a full read pass that began once the burst was
// completely written (every byte is then queued on this socket, so that pass reads it all) and
// `expect` pushes were retained. `sent` reports whether the burst finished within the deadline.
static std::vector<json> consumeIdleBurst(EngineClient& c, FakeHostd& hostd, int sentTarget,
                                          size_t expect, bool& sent) {
    std::vector<json> got;
    const auto deadline = Clock::now() + std::chrono::seconds(10);
    for (;;) {
        sent = hostd.afterPushesSent() >= sentTarget;  // observed BEFORE this pass reads
        CHECK(c.pollAlive());
        for (json& m : c.drainEvents()) got.push_back(std::move(m));
        if (sent && got.size() >= expect) break;
        if (Clock::now() >= deadline) break;
        std::this_thread::sleep_for(std::chrono::milliseconds(2));
    }
    return got;
}

constexpr int kPushes = 3000;
constexpr int kIdlePushes = 500;

// The private simulation lane: agent.event reaches onEvent live, in order; nothing else is
// retained however many pushes arrive mid-call or while idle.
static void discardingLaneRetainsNothing(FakeHostd& hostd) {
    EngineClient c;
    CHECK(connectProbe(c, true));
    int live = 0;
    bool ordered = true;
    auto onEvent = [&](const json& ev) {
        if (!ev.is_object() || ev.value("kind", std::string()) != "sim.progress") return;
        if (ev.value("i", -1) != live) ordered = false;
        ++live;
    };
    const int sentBefore = hostd.afterPushesSent();
    json r;
    std::string e;
    CHECK(c.call("transport_probe",
                 json{{"events", kPushes},
                      {"broadcasts", kPushes},
                      {"after_broadcasts", kIdlePushes},
                      {"tag", 3}},
                 r, e, 30000, onEvent));
    CHECK(probeTag(r) == 3);
    CHECK(live == kPushes);
    CHECK(ordered);
    CHECK(c.drainEvents().empty());  // the mid-call broadcasts were never queued
    bool sent = false;
    const std::vector<json> idle = consumeIdleBurst(c, hostd, sentBefore + 1, 0, sent);
    CHECK(sent);          // all kIdlePushes were written while this client read and dropped them
    CHECK(idle.empty());  // nor the idle ones
    // agent.event with no consumer is unconsumed too.
    r = json();
    CHECK(c.call("transport_probe", json{{"events", 50}, {"broadcasts", 50}, {"tag", 4}}, r, e,
                 5000));
    CHECK(probeTag(r) == 4);
    CHECK(c.drainEvents().empty());
    CHECK(c.connected());
}

// Ordinary GUI/agent connections keep every unconsumed push for drainEvents().
static void defaultLaneStillBuffers(FakeHostd& hostd) {
    EngineClient c;
    CHECK(connectProbe(c, false));
    int live = 0;
    auto onEvent = [&](const json& ev) {
        if (ev.is_object() && ev.value("kind", std::string()) == "sim.progress") ++live;
    };
    const int sentBefore = hostd.afterPushesSent();
    json r;
    std::string e;
    CHECK(c.call("transport_probe",
                 json{{"events", kPushes},
                      {"broadcasts", kPushes},
                      {"after_broadcasts", kIdlePushes},
                      {"tag", 5}},
                 r, e, 30000, onEvent));
    CHECK(probeTag(r) == 5);
    CHECK(live == kPushes);
    // Every mid-call broadcast is queued, in order; this drain may also pick up the start of
    // the post-reply burst, which continues the sequence from 0.
    std::vector<json> all = c.drainEvents();
    CHECK(all.size() >= static_cast<size_t>(kPushes));
    const size_t total = static_cast<size_t>(kPushes + kIdlePushes);
    bool sent = false;
    const size_t remaining = all.size() < total ? total - all.size() : 0;
    for (json& m : consumeIdleBurst(c, hostd, sentBefore + 1, remaining, sent))
        all.push_back(std::move(m));
    CHECK(sent);
    CHECK(all.size() == total);  // every mid-call and idle push, none lost, none extra
    bool inOrder = true;
    for (size_t i = 0; i < all.size(); ++i) {
        const json& m = all[i];
        const int expect = static_cast<int>(i < static_cast<size_t>(kPushes) ? i : i - kPushes);
        if (m.value("event", std::string()) != "doc.changed" || !m.contains("data") ||
            m["data"].value("i", -1) != expect)
            inOrder = false;
    }
    CHECK(inOrder);
    // Without onEvent, agent.event queues as before.
    r = json();
    CHECK(c.call("transport_probe", json{{"events", 5}, {"tag", 6}}, r, e, 5000));
    CHECK(probeTag(r) == 6);
    const std::vector<json> queued = c.drainEvents();
    CHECK(queued.size() == 5);
    CHECK(!queued.empty() && queued.front().value("event", std::string()) == "agent.event");
}

// Runs in a forked child: engine_backend selects its backend once per process, and this test
// must be the first to select it. A GUI that starts while an old hostd drains gets the named
// daemon_draining refusal. It must keep daemon mode: a private in-process engine would not
// see the documents of the agent or of any other client.
static void drainRefusalKeepsDaemonMode(FakeHostd& hostd) {
    hostd.refuseHellos(true);

    EngineClient probe;
    EngineClient::Options o;
    o.hostdBin = engine::hostdBinHint();
    std::string e;
    CHECK(!probe.connectOrSpawn(o, e));
    CHECK(e.find("daemon_draining") != std::string::npos);
    const json refusal = probe.helloError();
    CHECK(refusal.is_object() && refusal.value("code", std::string()) == "daemon_draining");
    CHECK(refusal.value("active_clients", -1) == 1);
    CHECK(!probe.connected());
    CHECK(probe.drainEvents().empty());  // the daemon.draining push is not queued

    CHECK(engine::daemonMode());
    CHECK(!engine::daemonUnavailable());
    CHECK(engine::daemonRefusal().value("reason", std::string()) == "stale_binary");
    json r;
    std::string ce;
    CHECK(!engine::call("doc_list", json::object(), r, ce, 5000));
    CHECK(ce.find("daemon_draining") != std::string::npos);
    std::string le;
    CHECK(!engine::ensureLoaded(le));
    CHECK(le.find("draining") != std::string::npos);
    CHECK(engine::maintainConnection() == engine::DaemonConnState::Unreachable);
    CHECK(!engine::daemonRefusal().is_null());

    // The old daemon exits and a daemon that admits clients takes its place. The 1 s backoff
    // rung after the refused attempt bounds the wait.
    hostd.refuseHellos(false);
    bool connected = false;
    const auto deadline = Clock::now() + std::chrono::seconds(5);
    while (!connected && Clock::now() < deadline) {
        connected = engine::maintainConnection() == engine::DaemonConnState::Connected;
        if (!connected) std::this_thread::sleep_for(std::chrono::milliseconds(50));
    }
    CHECK(connected);
    CHECK(engine::daemonRefusal().is_null());
    CHECK(engine::call("doc_list", json::object(), r, ce, 5000));
}

// The local-pipe regression executes this test binary as its private worker, using
// real stdio and Unix control transports without contacting a daemon or Python.
static int privateControlWorker() {
    const std::string path = std::getenv("CHUDWARE_TEST_PRIVATE_CONTROL_SOCKET");
    const std::string token(64, 'a');
    const int listener = ::socket(AF_UNIX, SOCK_STREAM, 0);
    sockaddr_un addr{};
    addr.sun_family = AF_UNIX;
    std::snprintf(addr.sun_path, sizeof addr.sun_path, "%s", path.c_str());
    if (::bind(listener, reinterpret_cast<sockaddr*>(&addr), sizeof addr) != 0 ||
        ::listen(listener, 8) != 0)
        return 2;
    std::mutex mutex;
    std::condition_variable cv;
    bool answered = false;
    std::atomic<bool> stopping{false};
    auto send = [](int fd, const json& value) {
        const std::string frame = value.dump() + "\n";
        (void)::send(fd, frame.data(), frame.size(), MSG_NOSIGNAL);
    };
    std::thread control([&] {
        while (!stopping.load()) {
            const int fd = ::accept(listener, nullptr, nullptr);
            if (fd < 0) break;
            std::string frame;
            char chunk[4096];
            while (frame.find('\n') == std::string::npos) {
                const auto count = ::recv(fd, chunk, sizeof chunk, 0);
                if (count <= 0) break;
                frame.append(chunk, static_cast<size_t>(count));
            }
            const json request = json::parse(frame);
            const auto& params = request["params"];
            const std::string action = request.value("action", std::string());
            const std::string failure = params.value(
                action == "steer" ? "text" : "batch_id", std::string());
            if (request.value("token", std::string()) != token) {
                send(fd, json{{"ok", false}});
            } else if (failure == "refused") {
                send(fd, json{{"ok", false}, {"error", {{"message", "control refused"}}}});
            } else if (failure == "malformed") {
                (void)::send(fd, "not-json\n", 9, MSG_NOSIGNAL);
            } else if (failure == "held") {
                std::this_thread::sleep_for(std::chrono::milliseconds(3000));
            } else if (failure == "closed") {
                // No response; the listener and endpoint remain alive.
            } else {
                send(fd, json{{"ok", true}, {"result", {{"answered", true}}}});
                {
                    std::lock_guard<std::mutex> lock(mutex);
                    answered = true;
                }
                cv.notify_all();
            }
            ::close(fd);
        }
    });
    int infoRequests = 0;
    std::string line;
    while (std::getline(std::cin, line)) {
        const json request = json::parse(line);
        const auto method = request.value("method", std::string());
        json result;
        if (method == "shutdown") break;
        if (method == "worker_control_info") {
            ++infoRequests;
            result = json{{"transport", "unix"}, {"endpoint", path}, {"token", token}};
        } else if (method == "agent_admin_turn") {
            std::cout << json{{"id", request["id"]}, {"event", "ready"},
                              {"data", {{"kind", "ready"}}}}.dump() << '\n' << std::flush;
            std::unique_lock<std::mutex> lock(mutex);
            cv.wait_for(lock, std::chrono::seconds(10), [&] { return answered; });
            result = json{{"answered", answered}, {"info_requests", infoRequests}};
        }
        std::cout << json{{"id", request["id"]}, {"ok", true},
                          {"result", result}}.dump() << '\n' << std::flush;
    }
    stopping = true;
    ::shutdown(listener, SHUT_RDWR);
    ::close(listener);
    control.join();
    ::unlink(path.c_str());
    return 0;
}

static void localPrivateFailuresPreserveEndpoint(const std::filesystem::path& dir,
                                                 const std::string& executable) {
    ::setenv("CHUDWARE_ENGINE_DAEMON", "0", 1);
    ::setenv("CHUDWARE_WORKER_BIN", executable.c_str(), 1);
    ::setenv("CHUDWARE_TEST_PRIVATE_CONTROL_SOCKET",
             (dir / "private-control.sock").string().c_str(), 1);
    WorkerClient worker;
    std::string error;
    if (!worker.start(error)) {
        CHECK(false);
        return;
    }
    std::mutex mutex;
    std::condition_variable cv;
    bool ready = false;
    json turnResult;
    std::string turnError;
    bool turnOk = false;
    std::thread turn([&] {
        turnOk = worker.call("agent_admin_turn", json{{"run_id", "private-run"}},
                             turnResult, turnError, -1, [&](const json&) {
            {
                std::lock_guard<std::mutex> lock(mutex);
                ready = true;
            }
            cv.notify_all();
        });
    });
    {
        std::unique_lock<std::mutex> lock(mutex);
        CHECK(cv.wait_for(lock, std::chrono::seconds(5), [&] { return ready; }));
    }
    for (bool answerAttempt : {false, true}) {
        for (const char* failure : {"refused", "malformed", "closed", "held"}) {
            json result;
            error.clear();
            const bool ok = answerAttempt
                ? worker.callAgentAnswer("private-run", failure, json{{"q", "PETG"}},
                                         false, result, error)
                : worker.callAgentSteer("private-run", failure, json::array(), "",
                                        result, error);
            CHECK(!ok);
            CHECK(error.find("endpoint unavailable") == std::string::npos);
            CHECK(error.find(std::string(64, 'a')) == std::string::npos);
        }
    }
    json answer;
    error.clear();
    const bool accepted = worker.callAgentAnswer(
        "private-run", "pending-question", json{{"material", "PETG"}}, false,
        answer, error);
    CHECK(accepted);
    CHECK(answer.value("answered", false));
    // On failure, wake the unbounded stdio reader rather than hanging the test.
    if (!accepted) worker.abortPending();
    turn.join();
    CHECK(turnOk);
    CHECK(turnResult.value("answered", false));
    CHECK(turnResult.value("info_requests", 0) == 1);
    worker.stop();
}

int main(int argc, char** argv) {
    if (argc == 3 && std::strcmp(argv[1], "--module") == 0 &&
        std::strcmp(argv[2], "worker") == 0)
        return privateControlWorker();
    namespace fs = std::filesystem;
    const fs::path dir = fs::temp_directory_path() /
                         ("chudware-test-long-run-" + std::to_string(::getpid()));
    fs::create_directories(dir);
    const std::string sock = (dir / "hostd.sock").string();
    // Everything that could resolve a real daemon points at private, harmless targets.
    ::setenv("XDG_RUNTIME_DIR", dir.string().c_str(), 1);
    ::setenv("CHUDWARE_HOSTD_SOCKET", sock.c_str(), 1);
    ::setenv("CHUDWARE_INSTANCE_KEY", ("test-long-run-" + std::to_string(::getpid())).c_str(), 1);
    ::setenv("CHUDWARE_HOSTD_BIN", "/bin/true", 1);
    ::setenv("CHUDWARE_ENGINE_DAEMON_RETRY_MS", "0", 1);
    ::unsetenv("CHUDWARE_ENGINE_DAEMON");
    ::unsetenv("CHUDWARE_RPC_TRACE");


    {
        const std::string executable = fs::canonical(argv[0]).string();
        std::fflush(stdout);
        const pid_t child = ::fork();  // backend selection is isolated before threads exist
        if (child == 0) {
            localPrivateFailuresPreserveEndpoint(dir, executable);
            std::fflush(stdout);
            ::_exit(g_failures ? 1 : 0);
        }
        int status = 0;
        CHECK(child > 0 && ::waitpid(child, &status, 0) == child && WIFEXITED(status) &&
              WEXITSTATUS(status) == 0);
    }
    {
        const std::string drainSock = (dir / "hostd-drain.sock").string();
        std::fflush(stdout);
        const pid_t child = ::fork();  // before any thread exists in this process
        if (child == 0) {
            ::setenv("CHUDWARE_HOSTD_SOCKET", drainSock.c_str(), 1);
            {
                FakeHostd hostd(drainSock);
                drainRefusalKeepsDaemonMode(hostd);
            }
            std::fflush(stdout);
            ::_exit(g_failures ? 1 : 0);
        }
        int status = 0;
        CHECK(child > 0 && ::waitpid(child, &status, 0) == child && WIFEXITED(status) &&
              WEXITSTATUS(status) == 0);
    }

    {
        FakeHostd hostd(sock);
        uncorrelatedProtocolErrorsRetireConnection();
        backgroundQueriesDoNotBlockPrimary(hostd);
        backgroundQueryEofIsIsolated();
        backgroundQueryTimeoutIsIsolated();
        backgroundProtocolRefusalIsIsolated();
        disconnectWakesAllBackgroundQueries(hostd);
        onlineTurnsOverlap(hostd);
        nativeAgentsNeverOverlap(hostd);
        agentConnectionPoolIsBounded(hostd);
        silentTopologySolveStaysPending(hostd);
        unboundedWaitEndsOnEof();
        unboundedWaitEndsOnAbort(hostd);
        boundedWaitIsOneDeadlineAcrossPushes();
        discardingLaneRetainsNothing(hostd);
        defaultLaneStillBuffers(hostd);
        topologyRunsNeverOverlap(hostd);
        agentAndTopologyLanesAreIndependent(hostd);
        // Last: each ~AsyncRpc aborts the process-wide connections (the next call reconnects).
        asyncLaneRunsDuringTopology(hostd);
        meshPreviewDuringTopology(hostd);
        asyncLaneRunsDuringTurn(hostd);
    }
    std::error_code ec;
    fs::remove_all(dir, ec);
    if (g_failures) {
        std::printf("test_worker_long_run: %d failure(s)\n", g_failures);
        return 1;
    }
    std::printf("test_worker_long_run: all passed\n");
    return 0;
}
