// test_worker_long_run.cpp — ctest for WorkerClient's daemon-mode long-run serialization.
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
//   ✓ two long runs on one client never overlap: the second reaches hostd only after the first
//     returned
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

#include <algorithm>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdio>
#include <cstdlib>
#include <cstring>
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

    // Block every agent_admin_turn until releaseTurns(); re-arm for the next test.
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
            connThreads_.emplace_back([this, fd] { serve(fd); });
        }
    }

    // nullopt = hang up without replying.
    std::optional<json> handle(int fd, const std::string& method, const json& params) {
        if (method == "hello") return json{{"state", "ready"}, {"client_id", 1}};
        if (method == "worker_control_info")
            return json{{"transport", "unix"},
                        {"endpoint", "/nonexistent/chudware-test-control.sock"},
                        {"token", std::string(64, 'a')}};
        if (method == "agent_admin_turn") {
            std::unique_lock<std::mutex> lk(mtx_);
            ++turnsStarted_;
            ++activeTurns_;
            if (activeTurns_ > maxConcurrent_) maxConcurrent_ = activeTurns_;
            cv_.notify_all();
            cv_.wait(lk, [&] { return releaseTurns_; });
            --activeTurns_;
            return json{{"ok", true}, {"text", "done"}};
        }
        if (method == "subscription_accounts") return json{{"accounts", json::array()}};
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

    void serve(int fd) {
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
                const std::optional<json> result = handle(fd, method, params);
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
    bool releaseTurns_ = true;
    bool releaseSims_ = true;
    int turnsStarted_ = 0;
    int activeTurns_ = 0;
    int maxConcurrent_ = 0;
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

// Two long runs on one client still serialize: the second reaches hostd only after the first.
static void longRunsNeverOverlap(FakeHostd& hostd) {
    WorkerClient w;
    hostd.holdTurns();
    const int before = hostd.turnsStarted();
    auto runTurn = [&](const char* id) {
        json r;
        std::string e;
        w.call("agent_admin_turn", turnParams(id), r, e, 60000);
    };
    std::thread first(runTurn, "run-a");
    CHECK(hostd.waitTurnsStarted(before + 1, 5000));
    std::thread second(runTurn, "run-b");
    // A second run that skipped the long-run mutex reaches the fake hostd within milliseconds.
    CHECK(!hostd.waitTurnsStarted(before + 2, 500));
    CHECK(hostd.maxConcurrentTurns() == 1);
    hostd.releaseTurns();
    first.join();
    second.join();
    CHECK(hostd.turnsStarted() == before + 2);
    CHECK(hostd.maxConcurrentTurns() == 1);
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

static bool connectProbe(EngineClient& c, bool discardUnconsumedEvents) {
    EngineClient::Options o;
    o.hostdBin = engine::hostdBinHint();
    o.clientKind = "test";
    o.allowSpawn = false;
    o.discardUnconsumedEvents = discardUnconsumedEvents;
    std::string e;
    if (c.connectOrSpawn(o, e)) return true;
    std::printf("  probe connect failed: %s\n", e.c_str());
    return false;
}

static int probeTag(const json& r) { return r.is_object() ? r.value("tag", 0) : 0; }

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

int main() {
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
        FakeHostd hostd(sock);
        longRunsNeverOverlap(hostd);
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
