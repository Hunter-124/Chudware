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

#include "worker_client.h"
#include "core/async_rpc.h"

#include <nlohmann/json.hpp>

#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <mutex>
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
        }
        cv_.notify_all();
        ::shutdown(listenFd_, SHUT_RDWR);
        ::close(listenFd_);
        acceptThread_.join();
        std::vector<int> fds;
        {
            std::lock_guard<std::mutex> lk(mtx_);
            fds = connFds_;
        }
        for (int fd : fds) ::shutdown(fd, SHUT_RDWR);
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

private:
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

    json handle(const std::string& method) {
        if (method == "hello") return {{"state", "ready"}, {"client_id", 1}};
        if (method == "worker_control_info")
            return {{"transport", "unix"},
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
            return {{"ok", true}, {"text", "done"}};
        }
        if (method == "subscription_accounts") return {{"accounts", json::array()}};
        if (method == "doc_activate")
            return {{"doc_id", "doc-cpp00000002"}, {"revision", 1}, {"tree", json::object()}};
        return json::object();
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
                const json reply = {{"id", req.value("id", 0)},
                                    {"result", handle(req.value("method", std::string()))}};
                const std::string out = reply.dump() + "\n";
                if (::write(fd, out.data(), out.size()) < 0) break;
            }
        }
        ::close(fd);
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
    int turnsStarted_ = 0;
    int activeTurns_ = 0;
    int maxConcurrent_ = 0;
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
        asyncLaneRunsDuringTurn(hostd);  // last: ~AsyncRpc aborts the process-wide connections
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
