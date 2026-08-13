// hostd → services worker stdio bridge. Windows hostd remains unavailable.
#include "worker_bridge.h"

#include <cstdlib>
#include <cstring>

#include "hostd_log.h"
#include "security/integrity.h"
#include "security/runtime_guard.h"
#include "util/platform_paths.h"

#ifdef _WIN32

namespace hostd {

std::atomic<bool> WorkerBridge::created_{false};

WorkerBridge& WorkerBridge::instance() {
    static WorkerBridge inst;
    created_.store(true, std::memory_order_relaxed);
    return inst;
}

WorkerBridge::~WorkerBridge() = default;

void WorkerBridge::stop() {}

void WorkerBridge::shutdownIfCreated() {
    if (created_.load(std::memory_order_relaxed)) instance().stop();
}

bool WorkerBridge::call(const std::string& /*method*/, const nlohmann::json& /*params*/,
                        nlohmann::json& /*result*/, std::string& err, int /*timeoutMs*/) {
    err = "worker bridge unavailable on Windows";
    return false;
}

}  // namespace hostd

#else  // !_WIN32

#include <chrono>
#include <filesystem>
#include <thread>

#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <sys/wait.h>
#include <unistd.h>

#include "util/parent_death.h"
#include "util/platform_paths.h"

namespace hostd {

using nlohmann::json;

namespace {

std::string envOr(const char* key, const std::string& def) {
    if (const char* v = std::getenv(key); v && *v) return std::string(v);
    return def;
}

std::string homeDir() {
    if (const char* h = std::getenv("HOME"); h && *h) return std::string(h);
    return std::string();
}

bool fileExists(const std::string& p) {
    std::error_code ec;
    return std::filesystem::is_regular_file(p, ec);
}


std::string adjacentWorkerBinary(std::string& err) {
    const std::string executable = chudware::platform::executablePath();
    if (executable.empty()) {
        err = "could not resolve the current executable path";
        return {};
    }
#ifdef _WIN32
    constexpr const char* name = "chudware-worker.exe";
#else
    constexpr const char* name = "chudware-worker";
#endif
    std::error_code ec;
    const auto candidate = std::filesystem::weakly_canonical(
        std::filesystem::path(executable).parent_path() / name, ec);
    if (ec) {
        err = "could not canonicalize adjacent worker path";
        return {};
    }
    return candidate.string();
}

bool sameCanonicalPath(const std::string& left, const std::string& right) {
    std::error_code leftEc, rightEc;
    const auto canonicalLeft = std::filesystem::weakly_canonical(left, leftEc);
    const auto canonicalRight = std::filesystem::weakly_canonical(right, rightEc);
    return !leftEc && !rightEc && canonicalLeft == canonicalRight;
}
}  // namespace

std::atomic<bool> WorkerBridge::created_{false};

WorkerBridge& WorkerBridge::instance() {
    static WorkerBridge inst;
    created_.store(true, std::memory_order_relaxed);
    return inst;
}

void WorkerBridge::shutdownIfCreated() {
    if (created_.load(std::memory_order_relaxed)) instance().stop();
}

WorkerBridge::~WorkerBridge() { stop(); }

void WorkerBridge::stop() {
    std::lock_guard<std::mutex> lk(mtx_);
    if (!running_ && process_ < 0 && childStdinWr_ < 0 && childStdoutRd_ < 0) return;
    // Soft shutdown request, then reap (mirrors worker_client.cpp stop()).
    if (childStdinWr_ >= 0) {
        const char* bye = "{\"id\":0,\"method\":\"shutdown\"}\n";
        ssize_t w = ::write(childStdinWr_, bye, std::strlen(bye));
        (void)w;
        ::close(childStdinWr_);
        childStdinWr_ = -1;
    }
    if (process_ >= 0) {
        bool reaped = false;
        for (int i = 0; i < 200; ++i) {
            int status = 0;
            pid_t r = waitpid(process_, &status, WNOHANG);
            if (r == process_ || (r < 0 && errno == ECHILD)) {
                reaped = true;
                break;
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(10));
        }
        if (!reaped) {
            ::kill(process_, SIGTERM);
            waitpid(process_, nullptr, 0);
        }
        process_ = -1;
    }
    if (childStdoutRd_ >= 0) {
        ::close(childStdoutRd_);
        childStdoutRd_ = -1;
    }
    readBuf_.clear();
    running_ = false;
}

void WorkerBridge::reap() {
    // Caller holds mtx_. Tear down without the graceful shutdown write (child
    // already dead or pipe broken).
    if (childStdinWr_ >= 0) {
        ::close(childStdinWr_);
        childStdinWr_ = -1;
    }
    if (childStdoutRd_ >= 0) {
        ::close(childStdoutRd_);
        childStdoutRd_ = -1;
    }
    if (process_ >= 0) {
        int status = 0;
        pid_t r = waitpid(process_, &status, WNOHANG);
        if (r == 0) {
            ::kill(process_, SIGTERM);
            waitpid(process_, nullptr, 0);
        } else if (r < 0 && errno != ECHILD) {
            // best-effort
            ::kill(process_, SIGKILL);
            waitpid(process_, nullptr, 0);
        }
        process_ = -1;
    }
    readBuf_.clear();
    running_ = false;
}

namespace {
bool allowWorkerSpawn(std::string& err) {
    const auto executable = chudware::platform::executablePath();
    const auto integrity = chudware::security::verifyInstalledPayload(
        executable, chudware::security::VerifyMode::ProtectedOperation);
    if (!integrity.ok) {
        err = "worker spawn blocked: protected_file_hash_mismatch";
        return false;
    }
    const auto decision = chudware::security::inspectAndPersistRuntime(
        chudware::security::SecurityCheckpoint::BeforeWorkerSpawn,
        integrity.root, integrity.files, chudware::security::runtimeGuardStateDir().string());
    if (decision.allowed()) return true;
    err = "worker spawn blocked: " + decision.denialReason;
    return false;
}
}  // namespace
bool WorkerBridge::spawn(std::string& err) {
    // Caller holds mtx_. running_ must be false / fds closed.
    if (!allowWorkerSpawn(err)) return false;
    if (!pathsResolved_) {
#ifdef CHUDWARE_DEVELOPER_BUILD
        const std::string home = homeDir();
        if (const char* env = std::getenv("CHUDWARE_WORKER_BIN"); env && *env) {
            std::error_code ec;
            workerBin_ = std::filesystem::weakly_canonical(env, ec).string();
            if (ec) workerBin_ = env;
            compiledWorker_ = true;
        } else {
            workerBin_ = envOr("CHUDWARE_PYTHON", std::string());
            if (workerBin_.empty()) {
                const std::string venv =
                    home.empty() ? std::string()
                                 : home + "/.local/share/Chudware/runtime/venv/bin/python";
                if (!venv.empty() && fileExists(venv)) {
                    workerBin_ = venv;
                } else {
                    for (const char* cand :
                         {"/usr/bin/python3", "/usr/local/bin/python3", "/usr/bin/python"}) {
                        if (fileExists(cand)) { workerBin_ = cand; break; }
                    }
                    if (workerBin_.empty()) workerBin_ = "python3";
                }
            }
        }
        repo_ = compiledWorker_
                    ? std::filesystem::path(workerBin_).parent_path().string()
                    : envOr("CHUDWARE_REPO",
                            home.empty() ? std::string(".") : home + "/Repositories/CAD");
#else
        workerBin_ = adjacentWorkerBinary(err);
        if (workerBin_.empty()) return false;
        if (const char* env = std::getenv("CHUDWARE_WORKER_BIN"); env && *env &&
            !sameCanonicalPath(env, workerBin_)) {
            err = "CHUDWARE_WORKER_BIN must name the adjacent packaged worker";
            log().line("worker_bridge: spawn failed: %s", err.c_str());
            return false;
        }
        compiledWorker_ = true;
        repo_ = std::filesystem::path(workerBin_).parent_path().string();
#endif
        pathsResolved_ = true;
    }

    if (workerBin_.empty()) {
        err = "CHUDWARE_WORKER_BIN is required in packaged builds";
        log().line("worker_bridge: spawn failed: %s", err.c_str());
        return false;
    }

    // Packaged binaries must resolve to a real manifest-owned file. Developer-mode bare
    // interpreter names may still defer to execlp's PATH search.
    if ((compiledWorker_ || workerBin_.find('/') != std::string::npos) &&
        !fileExists(workerBin_)) {
        err = (compiledWorker_ ? "Compiled worker not found at " : "Python runtime not found at ") +
              workerBin_;
        log().line("worker_bridge: spawn failed: %s", err.c_str());
        return false;
    }

    int outPipe[2], inPipe[2];
    if (pipe(outPipe) != 0 || pipe(inPipe) != 0) {
        err = std::string("pipe() failed: ") + std::strerror(errno);
        return false;
    }

    const pid_t parentPid = ::getpid();
    pid_t pid = fork();
    if (pid < 0) {
        err = std::string("fork failed: ") + std::strerror(errno);
        ::close(outPipe[0]);
        ::close(outPipe[1]);
        ::close(inPipe[0]);
        ::close(inPipe[1]);
        return false;
    }
    if (pid == 0) {
        // Child: async-signal-safe orphan-proofing first (mirrors child_supervisor.cpp:310-315
        // in spirit).
        ::setpgid(0, 0);
        // Parent-death linkage: PDEATHSIG on Linux, a kqueue babysitter on
        // Darwin. Never returns if hostd already died.
        chudware::platform::guardParentDeath(parentPid);
        // stdin <- inPipe[0], stdout -> outPipe[1]; stderr stays (hostd log/console).
        dup2(inPipe[0], STDIN_FILENO);
        dup2(outPipe[1], STDOUT_FILENO);
        ::close(inPipe[0]);
        ::close(inPipe[1]);
        ::close(outPipe[0]);
        ::close(outPipe[1]);
        // Close every other inherited fd (hostd's listen/client sockets etc.) so they never
        // leak into the worker child.
        chudware::platform::closeInheritedFds(3);
        if (!repo_.empty() && chdir(repo_.c_str()) != 0) {
            // Fall through; imports or resource lookup will fail clearly.
        }
        if (compiledWorker_)
            execlp(workerBin_.c_str(), workerBin_.c_str(), "--module", "worker", (char*)nullptr);
        else
            execlp(workerBin_.c_str(), workerBin_.c_str(), "-m", "services.worker_main",
                   (char*)nullptr);
        _exit(127);
    }

    // Parent mirrors the child's setpgid (whoever runs first wins; both same), then keeps our
    // ends and closes the child's.
    ::setpgid(pid, pid);
    ::close(inPipe[0]);
    ::close(outPipe[1]);
    fcntl(outPipe[0], F_SETFL, O_NONBLOCK);
    process_ = pid;
    childStdoutRd_ = outPipe[0];
    childStdinWr_ = inPipe[1];
    readBuf_.clear();
    running_ = true;
    log().line("worker_bridge: spawned services.worker_main pid=%d launcher=%s repo=%s", (int)pid,
               workerBin_.c_str(), repo_.c_str());
    return true;
}

bool WorkerBridge::writeLine(const std::string& line) {
    if (childStdinWr_ < 0) return false;
    const char* p = line.data();
    size_t left = line.size();
    while (left > 0) {
        ssize_t n = ::write(childStdinWr_, p, left);
        if (n < 0) {
            if (errno == EINTR) continue;
            return false;
        }
        if (n == 0) return false;
        p += n;
        left -= (size_t)n;
    }
    return true;
}

bool WorkerBridge::readLine(std::string& line, int timeoutMs) {
    auto start = std::chrono::steady_clock::now();
    for (;;) {
        size_t nl = readBuf_.find('\n');
        if (nl != std::string::npos) {
            line = readBuf_.substr(0, nl);
            readBuf_.erase(0, nl + 1);
            return true;
        }
        auto elapsed = std::chrono::duration_cast<std::chrono::milliseconds>(
                           std::chrono::steady_clock::now() - start)
                           .count();
        if ((int)elapsed > timeoutMs) return false;

        struct pollfd pfd;
        pfd.fd = childStdoutRd_;
        pfd.events = POLLIN;
        pfd.revents = 0;
        int pr = ::poll(&pfd, 1, 2);  // 2 ms tick — matches worker_client.cpp
        if (pr < 0) {
            if (errno == EINTR) continue;
            return false;
        }
        if (pr == 0) {
            int status = 0;
            pid_t r = (process_ >= 0) ? waitpid(process_, &status, WNOHANG) : -1;
            if (r == process_ || (r < 0 && errno == ECHILD)) {
                process_ = -1;
                char chunk[4096];
                ssize_t got = ::read(childStdoutRd_, chunk, sizeof(chunk));
                if (got > 0) {
                    readBuf_.append(chunk, (size_t)got);
                    continue;
                }
                return false;  // EOF / dead child, buffer drained
            }
            continue;
        }
        char chunk[4096];
        ssize_t got = ::read(childStdoutRd_, chunk, sizeof(chunk));
        if (got > 0) {
            readBuf_.append(chunk, (size_t)got);
        } else if (got == 0) {
            return false;  // EOF
        } else if (errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR) {
            return false;
        }
    }
}

bool WorkerBridge::callOnce(const std::string& method, const json& params, json& result,
                            std::string& err, int timeoutMs) {
    if (!running_) {
        if (!spawn(err)) return false;
    }

    const int id = nextId_++;
    json req = {{"id", id}, {"method", method}, {"params", params}};
    const std::string line = req.dump() + "\n";
    if (!writeLine(line)) {
        err = "write failed";
        return false;
    }

    for (;;) {
        std::string resp;
        if (!readLine(resp, timeoutMs)) {
            // Distinguish child death (EOF / reaped) from a live slow call so
            // the outer retry only respawns on a dead child.
            if (process_ < 0 || childStdoutRd_ < 0) {
                err = "closed waiting for response";
            } else {
                int status = 0;
                pid_t r = waitpid(process_, &status, WNOHANG);
                if (r == process_ || (r < 0 && errno == ECHILD)) {
                    process_ = -1;
                    err = "closed waiting for response";
                } else {
                    err = "timeout waiting for response";
                }
            }
            return false;
        }
        if (resp.empty()) continue;
        json j;
        try {
            j = json::parse(resp);
        } catch (...) {
            continue;  // ignore non-JSON noise
        }
        if (!j.contains("id") || j["id"].is_null()) continue;
        // id may be number; tolerate int-or-int64
        int rid = -1;
        try {
            rid = j["id"].get<int>();
        } catch (...) {
            continue;
        }
        if (rid != id) continue;

        // Streamed progress notification (no "ok"): ignore and keep waiting.
        if (j.contains("event") && !j.contains("ok")) continue;

        if (j.value("ok", false)) {
            result = j.value("result", json::object());
            return true;
        }
        // ok:false — structured envelope preferred (protocol-2).
        {
            const auto it = j.find("error");
            if (it != j.end() && it->is_object()) {
                result = *it;
                err = it->value("message", std::string("rpc error"));
                return false;
            }
            err = (it != j.end() && it->is_string()) ? it->get<std::string>()
                                                     : std::string("unknown rpc error");
            result = json::object();
            return false;
        }
    }
}

bool WorkerBridge::call(const std::string& method, const json& params, json& result,
                        std::string& err, int timeoutMs) {
    // Serialize the single stdio channel. Only this mutex is held — never a
    // Server-wide lock — so other client threads keep dispatching engine RPCs.
    std::lock_guard<std::mutex> lk(mtx_);
    result = json::object();
    err.clear();
    if (timeoutMs <= 0) timeoutMs = 120000;

    // Child died (write fail/EOF) → reap + respawn once, then fail.
    // Timeouts and method-level errors do NOT respawn.
    for (int attempt = 0; attempt < 2; ++attempt) {
        std::string attemptErr;
        if (callOnce(method, params, result, attemptErr, timeoutMs)) return true;

        const bool childDead =
            attemptErr == "write failed" || attemptErr == "closed waiting for response";
        // Method-level errors (ok:false with structured code) are final.
        const bool methodError = result.is_object() && result.contains("code");
        if (methodError || !childDead) {
            err = std::move(attemptErr);
            return false;
        }
        log().line("worker_bridge: child lost during %s (%s); %s", method.c_str(),
                   attemptErr.c_str(), attempt == 0 ? "respawning once" : "giving up");
        reap();
        err = std::move(attemptErr);
        if (attempt == 1) return false;
        // loop: respawn inside next callOnce
    }
    return false;
}

}  // namespace hostd

#endif  // !_WIN32
