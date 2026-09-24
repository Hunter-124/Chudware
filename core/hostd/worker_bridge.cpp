// hostd → services worker stdio bridge. Windows hostd remains unavailable.
#include "worker_bridge.h"

#include <cstdlib>
#include <cstring>

#include "hostd_log.h"
#include "security/integrity.h"
#include "security/runtime_guard.h"
#include "util/platform_paths.h"

namespace hostd {

std::atomic<bool> WorkerBridge::agentCreated_{false};
std::atomic<bool> WorkerBridge::generalCreated_{false};

WorkerBridge& WorkerBridge::agent() {
    static WorkerBridge bridge(true);
    agentCreated_.store(true, std::memory_order_release);
    return bridge;
}

WorkerBridge& WorkerBridge::general() {
    static WorkerBridge bridge(false);
    generalCreated_.store(true, std::memory_order_release);
    return bridge;
}

WorkerBridge& WorkerBridge::forMethod(const std::string& method) {
    // Reviewed against worker_main._METHODS registrations. These families share
    // Chudcode sessions, the agent prompt cache, subscription login progress,
    // benchmark subprocesses, or agent/subagent/DAG control state. The only
    // non-family exceptions are the control endpoint and trigger rule creation.
    // office_extract and all other registered worker families (PLM, printer,
    // parts, reveng, sim, deliverables, run_model, prompts) use general.
    static constexpr const char* agentPrefixes[] = {
        "agent_", "chudcode_", "session_", "subscription_", "skills_",
        "triggers_", "packs_", "ai_roles_", "model_rankings_", "bench_",
        "memory_", "subagent_", "dag_", "ai_prompt_", "ai_tool_",
    };
    if (method == "worker_control_info" || method == "rule_create")
        return agent();
    for (const char* prefix : agentPrefixes)
        if (method.compare(0, std::strlen(prefix), prefix) == 0)
            return agent();
    return general();
}

void WorkerBridge::shutdownIfCreated() {
    if (agentCreated_.load(std::memory_order_acquire)) agent().stop();
    if (generalCreated_.load(std::memory_order_acquire)) general().stop();
}

}  // namespace hostd

#ifdef _WIN32

namespace hostd {

WorkerBridge::~WorkerBridge() = default;

void WorkerBridge::stop() {}

bool WorkerBridge::call(const std::string& /*method*/, const nlohmann::json& /*params*/,
                        nlohmann::json& /*result*/, std::string& err, int /*timeoutMs*/,
                        const EventFn& /*onEvent*/) {
    err = "worker bridge unavailable on Windows";
    return false;
}

bool WorkerBridge::controlActiveRun(const std::string& /*action*/,
                                    const nlohmann::json& /*params*/, bool& matched,
                                    nlohmann::json& /*result*/, std::string& err) {
    matched = false;
    err = "worker bridge unavailable on Windows";
    return false;
}

}  // namespace hostd

#else  // !_WIN32
#include <algorithm>
#include <chrono>
#include <filesystem>
#include <stdexcept>
#include <thread>

#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <sys/socket.h>
#include <sys/un.h>
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

constexpr size_t kControlRequestLimit = 64 * 1024;
constexpr size_t kControlResponseLimit = 256 * 1024;
constexpr auto kControlTimeout = std::chrono::milliseconds(2500);

void wipeString(std::string& value) {
    volatile char* p = value.empty() ? nullptr : value.data();
    for (size_t i = 0; i < value.size(); ++i) p[i] = 0;
    value.clear();
}

bool validControlToken(const std::string& token) {
    return token.size() == 64 &&
           std::all_of(token.begin(), token.end(), [](unsigned char c) {
               return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f');
           });
}

struct SensitiveLine {
    std::string value;
    ~SensitiveLine() { wipeString(value); }
};

bool validControlInfo(const std::string& transport, const std::string& endpoint,
                      const std::string& token) {
    if (!validControlToken(token) || transport != "unix" || endpoint.empty() ||
        endpoint.front() != '/' || endpoint.size() > 103)
        return false;
    return std::none_of(endpoint.begin(), endpoint.end(), [](unsigned char c) {
        return c < 0x20 || c == 0x7f;
    });
}

int remainingMs(const std::chrono::steady_clock::time_point deadline) {
    const auto left = std::chrono::duration_cast<std::chrono::milliseconds>(
                          deadline - std::chrono::steady_clock::now())
                          .count();
    if (left <= 0) return 0;
    return static_cast<int>(std::min<long long>(left, 2500));
}

bool waitSocket(int fd, short events,
                const std::chrono::steady_clock::time_point deadline) {
    for (;;) {
        const int timeout = remainingMs(deadline);
        if (timeout <= 0) return false;
        pollfd pfd{fd, events, 0};
        const int rc = ::poll(&pfd, 1, timeout);
        if (rc > 0) return (pfd.revents & events) != 0;
        if (rc == 0) return false;
        if (errno != EINTR) return false;
    }
}

bool sendControlRequest(const std::string& transport, const std::string& endpoint,
                        const std::string& token, const std::string& action,
                        const json& params, json& result, std::string& err) {
    result = json::object();
    err.clear();
    if (!validControlInfo(transport, endpoint, token) ||
        (action != "cancel" && action != "steer" && action != "status") ||
        !params.is_object()) {
        err = "worker control information is invalid";
        return false;
    }
    SensitiveLine request;
    std::string paramsText;
    try {
        paramsText = params.dump();
        request.value.reserve(token.size() + action.size() + paramsText.size() + 40);
        request.value = "{\"token\":\"";
        request.value += token;
        request.value += "\",\"action\":\"";
        request.value += action;
        request.value += "\",\"params\":";
        request.value += paramsText;
        request.value += "}\n";
    } catch (...) {
        err = "worker control params are not serializable";
        return false;
    }
    if (request.value.size() > kControlRequestLimit) {
        err = "worker control request exceeded limit";
        return false;
    }

    const int fd = ::socket(AF_UNIX, SOCK_STREAM, 0);
    if (fd < 0) {
        err = "worker control socket failed";
        return false;
    }
    fcntl(fd, F_SETFD, FD_CLOEXEC);
    const int flags = fcntl(fd, F_GETFL, 0);
    if (flags < 0 || fcntl(fd, F_SETFL, flags | O_NONBLOCK) != 0) {
        ::close(fd);
        err = "worker control socket setup failed";
        return false;
    }

    const auto deadline = std::chrono::steady_clock::now() + kControlTimeout;
    sockaddr_un address{};
    address.sun_family = AF_UNIX;
    std::memcpy(address.sun_path, endpoint.data(), endpoint.size());
    address.sun_path[endpoint.size()] = '\0';
    const int connected =
        ::connect(fd, reinterpret_cast<const sockaddr*>(&address), sizeof(address));
    if (connected != 0) {
        if (errno != EINPROGRESS || !waitSocket(fd, POLLOUT, deadline)) {
            ::close(fd);
            err = "worker control connect failed";
            return false;
        }
        int socketError = 0;
        socklen_t errorSize = sizeof(socketError);
        if (::getsockopt(fd, SOL_SOCKET, SO_ERROR, &socketError, &errorSize) != 0 ||
            socketError != 0) {
            ::close(fd);
            err = "worker control connect failed";
            return false;
        }
    }

    size_t sent = 0;
    while (sent < request.value.size()) {
        if (!waitSocket(fd, POLLOUT, deadline)) {
            ::close(fd);
            err = "worker control send timed out";
            return false;
        }
#ifdef MSG_NOSIGNAL
        const ssize_t count =
            ::send(fd, request.value.data() + sent, request.value.size() - sent,
                   MSG_NOSIGNAL);
#else
        const ssize_t count =
            ::send(fd, request.value.data() + sent, request.value.size() - sent, 0);
#endif
        if (count > 0) {
            sent += static_cast<size_t>(count);
            continue;
        }
        if (count < 0 && (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR))
            continue;
        ::close(fd);
        err = "worker control send failed";
        return false;
    }

    std::string response;
    for (;;) {
        const size_t newline = response.find('\n');
        if (newline != std::string::npos) {
            if (newline > kControlResponseLimit) {
                ::close(fd);
                err = "worker control response exceeded limit";
                return false;
            }
            response.resize(newline);
            break;
        }
        if (response.size() > kControlResponseLimit) {
            ::close(fd);
            err = "worker control response exceeded limit";
            return false;
        }
        if (!waitSocket(fd, POLLIN, deadline)) {
            ::close(fd);
            err = "worker control response timed out";
            return false;
        }
        char chunk[4096];
        const ssize_t count = ::recv(fd, chunk, sizeof(chunk), 0);
        if (count > 0) {
            response.append(chunk, static_cast<size_t>(count));
            continue;
        }
        if (count < 0 && (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR))
            continue;
        ::close(fd);
        err = "worker control response closed";
        return false;
    }
    ::close(fd);

    if (response.find(token) != std::string::npos) {
        err = "worker control response exposed authentication material";
        return false;
    }
    json decoded;
    try {
        decoded = json::parse(response);
    } catch (...) {
        err = "worker control response was invalid";
        return false;
    }
    if (!decoded.is_object()) {
        err = "worker control response was invalid";
        return false;
    }
    const auto ok = decoded.find("ok");
    if (ok == decoded.end() || !ok->is_boolean()) {
        err = "worker control response was invalid";
        return false;
    }
    if (!ok->get<bool>()) {
        err = "worker control request failed";
        return false;
    }
    const auto it = decoded.find("result");
    if (it == decoded.end() || !it->is_object()) {
        err = "worker control response was invalid";
        return false;
    }
    result = *it;
    return true;
}
}  // namespace

WorkerBridge::~WorkerBridge() { stop(); }

void WorkerBridge::clearActiveRun() {
    std::lock_guard<std::mutex> lk(controlMtx_);
    wipeString(activeWorkerRunId_);
}

void WorkerBridge::clearControlState() {
    std::lock_guard<std::mutex> lk(controlMtx_);
    wipeString(control_.token);
    wipeString(control_.endpoint);
    wipeString(control_.transport);
    wipeString(activeWorkerRunId_);
}

bool WorkerBridge::controlActiveRun(const std::string& action, const json& params,
                                    bool& matched, json& result, std::string& err) {
    matched = false;
    if (!agentCreated_.load(std::memory_order_acquire)) {
        err.clear();
        return false;
    }
    return agent().controlActiveRunImpl(action, params, matched, result, err);
}

bool WorkerBridge::controlActiveRunImpl(const std::string& action, const json& params,
                                        bool& matched, json& result, std::string& err) {
    matched = false;
    result = json::object();
    err.clear();
    if (!params.is_object()) return false;
    const auto runIt = params.find("run_id");
    if (runIt == params.end() || !runIt->is_string()) return false;
    const std::string runId = runIt->get<std::string>();
    if (runId.empty()) return false;

    std::lock_guard<std::mutex> lk(controlMtx_);
    if (activeWorkerRunId_ != runId) return false;
    matched = true;
    if (control_.token.empty()) {
        err = "worker control endpoint unavailable for active run";
        return false;
    }
    if (sendControlRequest(control_.transport, control_.endpoint, control_.token,
                           action, params, result, err))
        return true;

    // An endpoint/protocol failure invalidates the credential cache, but the
    // run remains active until its stdio call returns or child teardown.
    wipeString(control_.token);
    wipeString(control_.endpoint);
    wipeString(control_.transport);
    return false;
}

void WorkerBridge::stop() {
    stopping_.store(true, std::memory_order_release);
    std::unique_lock<std::mutex> lk(mtx_, std::try_to_lock);
    if (!lk.owns_lock()) {
        // A call holds the lane — an agent turn holds it for hours. Waiting here wedged the
        // daemon on tray Quit / SIGTERM mid-turn (2026-09-24): the accept loop was gone, the
        // worker's engine socket hung, and the turn's next CAD call blocked forever. Ask the
        // active run to cancel, give it a bounded moment to return, then end the child so
        // the blocked readLine sees EOF and releases the lane.
        std::string runId;
        {
            std::lock_guard<std::mutex> controlLock(controlMtx_);
            runId = activeWorkerRunId_;
        }
        if (!runId.empty()) {
            bool matched = false;
            json ignored;
            std::string cancelErr;
            controlActiveRunImpl("cancel", json{{"run_id", runId}}, matched, ignored, cancelErr);
        }
        const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(10);
        while (!lk.try_lock() && std::chrono::steady_clock::now() < deadline)
            std::this_thread::sleep_for(std::chrono::milliseconds(50));
        if (!lk.owns_lock()) {
            const pid_t pid = livePid_.load(std::memory_order_acquire);
            if (pid > 0) {
                log().line("worker_bridge: stopping with a call in flight; terminating pid %d",
                           (int)pid);
                ::kill(pid, SIGTERM);
            }
            lk.lock();
        }
    }
    clearControlState();
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
        livePid_.store(-1, std::memory_order_release);
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
        livePid_.store(-1, std::memory_order_release);
    }
    readBuf_.clear();
    running_ = false;
    clearControlState();
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

    int outPipe[2] = {-1, -1}, inPipe[2] = {-1, -1};
    if (pipe(outPipe) != 0 || pipe(inPipe) != 0) {
        err = std::string("pipe() failed: ") + std::strerror(errno);
        for (int fd : outPipe) if (fd >= 0) ::close(fd);
        for (int fd : inPipe) if (fd >= 0) ::close(fd);
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
        // This role belongs to the spawned child, not the hostd environment:
        // desktop/standalone workers continue to start with their full lifecycle.
        if (::setenv("CHUDWARE_HOSTD_WORKER_LANE",
                     agentLane_ ? "agent" : "general", 1) != 0)
            _exit(127);
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
    livePid_.store(pid, std::memory_order_release);
    childStdoutRd_ = outPipe[0];
    childStdinWr_ = inPipe[1];
    readBuf_.clear();
    running_ = true;
    log().line("worker_bridge: spawned %s services.worker_main pid=%d launcher=%s repo=%s",
               agentLane_ ? "agent" : "general", (int)pid,
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
    constexpr size_t kMaxWorkerLine = 64u * 1024u * 1024u;
    size_t scanFrom = 0;
    for (;;) {
        size_t nl = readBuf_.find('\n', scanFrom);
        if ((nl != std::string::npos && nl > kMaxWorkerLine) ||
            (nl == std::string::npos && readBuf_.size() > kMaxWorkerLine))
            return false;
        scanFrom = readBuf_.size();
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
                livePid_.store(-1, std::memory_order_release);
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
            ::close(childStdoutRd_);
            childStdoutRd_ = -1;
            return false;  // EOF
        } else if (errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR) {
            ::close(childStdoutRd_);
            childStdoutRd_ = -1;
            return false;
        }
    }
}

bool WorkerBridge::fetchControlInfo(std::string& err) {
    {
        std::lock_guard<std::mutex> lk(controlMtx_);
        if (!control_.token.empty()) return true;
    }

    json info;
    std::string infoErr;
    if (!callOnce("worker_control_info", json::object(), info, infoErr, 5000, {})) {
        err = infoErr.empty() ? "worker control information unavailable" : infoErr;
        return false;
    }

    const auto wipeInfoToken = [&info] {
        if (!info.is_object()) return;
        const auto token = info.find("token");
        if (token != info.end() && token->is_string())
            wipeString(token->get_ref<std::string&>());
    };
    ControlInfo next;
    try {
        if (!info.is_object()) throw std::runtime_error("invalid");
        next.transport = info.at("transport").get<std::string>();
        next.endpoint = info.at("endpoint").get<std::string>();
        next.token = info.at("token").get<std::string>();
    } catch (...) {
        wipeInfoToken();
        wipeString(next.token);
        err = "worker control information is invalid";
        return false;
    }
    wipeInfoToken();
    if (!validControlInfo(next.transport, next.endpoint, next.token)) {
        wipeString(next.token);
        wipeString(next.endpoint);
        wipeString(next.transport);
        err = "worker control information is invalid";
        return false;
    }
    {
        std::lock_guard<std::mutex> lk(controlMtx_);
        wipeString(control_.token);
        control_ = std::move(next);
    }
    return true;
}

bool WorkerBridge::callOnce(const std::string& method, const json& params, json& result,
                            std::string& err, int timeoutMs, const EventFn& onEvent) {
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
            if (readBuf_.size() > 64u * 1024u * 1024u) {
                err = "worker response line exceeds 64 MiB";
                reap();  // framing is lost; next call must spawn a fresh worker
                return false;
            }
            // Distinguish child death (EOF / reaped) from a live slow call so
            // the outer retry only respawns on a dead child.
            if (process_ < 0 || childStdoutRd_ < 0) {
                err = "closed waiting for response";
            } else {
                int status = 0;
                pid_t r = waitpid(process_, &status, WNOHANG);
                if (r == process_ || (r < 0 && errno == ECHILD)) {
                    process_ = -1;
                    livePid_.store(-1, std::memory_order_release);
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

        // Matching streamed progress is delivered once, before the terminal response.
        if (j.contains("event") && j.contains("data") && !j.contains("ok")) {
            if (onEvent) {
                try {
                    onEvent(j["data"]);
                } catch (...) {
                    // Event delivery is transparency only; it must not poison the
                    // worker transport or turn an eventual success into failure.
                }
            }
            continue;
        }

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
                        std::string& err, int timeoutMs, const EventFn& onEvent) {
    // Only this lane's stdio mutex is held; an agent turn cannot serialize
    // clients using the independent general-worker channel.
    std::lock_guard<std::mutex> lk(mtx_);
    result = json::object();
    err.clear();
    if (timeoutMs <= 0) timeoutMs = 120000;
    if (stopping_.load(std::memory_order_acquire)) {
        err = "worker bridge stopping";
        return false;
    }

    const bool longAgentTurn = method == "agent_admin_turn";
    std::string runId;
    if (longAgentTurn && params.is_object()) {
        const auto runIt = params.find("run_id");
        if (runIt != params.end() && runIt->is_string()) runId = runIt->get<std::string>();
    }
    if (longAgentTurn && runId.empty()) {
        err = "agent_admin_turn requires a non-empty run_id";
        return false;
    }

    // Child died (write fail/EOF) → reap + respawn once, then fail.
    // Timeouts and method-level errors do NOT respawn.
    for (int attempt = 0; attempt < 2; ++attempt) {
        std::string attemptErr;
        result = json::object();

        // The authenticated side channel must be known before an online turn
        // starts. Otherwise stdio would be occupied by an uncancellable call.
        if (longAgentTurn && !fetchControlInfo(attemptErr)) {
            const bool childDead =
                attemptErr == "write failed" || attemptErr == "closed waiting for response";
            if (!childDead) {
                err = std::move(attemptErr);
                return false;
            }
            log().line("worker_bridge: child lost before %s; %s", method.c_str(),
                       attempt == 0 ? "respawning once" : "giving up");
            reap();
            err = std::move(attemptErr);
            if (attempt == 1 || stopping_.load(std::memory_order_acquire)) return false;
            continue;
        }

        if (longAgentTurn) {
            std::lock_guard<std::mutex> controlLock(controlMtx_);
            activeWorkerRunId_ = runId;
        }
        const bool ok =
            callOnce(method, params, result, attemptErr, timeoutMs, onEvent);
        clearActiveRun();
        const bool childDead =
            attemptErr == "write failed" || attemptErr == "closed waiting for response";
        const bool methodError = result.is_object() && result.contains("code");
        if (ok) return true;

        if (methodError || !childDead) {
            err = std::move(attemptErr);
            return false;
        }
        log().line("worker_bridge: child lost during %s (%s); %s", method.c_str(),
                   attemptErr.c_str(), attempt == 0 ? "respawning once" : "giving up");
        reap();
        err = std::move(attemptErr);
        if (attempt == 1 || stopping_.load(std::memory_order_acquire)) return false;
        // loop: respawn inside fetchControlInfo/callOnce
    }
    return false;
}

}  // namespace hostd

#endif  // !_WIN32
