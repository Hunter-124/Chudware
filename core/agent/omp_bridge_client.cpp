// core/agent/omp_bridge_client.cpp — see omp_bridge_client.h. POSIX fork+pipe child talking
// newline-delimited JSON-RPC to `bun run vendor/omp/bridge/src/bridge.ts`, structurally mirrored
// on core/hostd/worker_bridge.cpp (the existing Python-worker stdio bridge pattern).
#include "agent/omp_bridge_client.h"

#include <cstdlib>
#include <cstring>

#ifdef _WIN32

namespace chudware {
namespace agent {

std::atomic<bool> OmpBridgeClient::created_{false};

OmpBridgeClient& OmpBridgeClient::instance() {
    static OmpBridgeClient inst;
    created_.store(true, std::memory_order_relaxed);
    return inst;
}

OmpBridgeClient::~OmpBridgeClient() = default;

void OmpBridgeClient::stop() {}

void OmpBridgeClient::shutdownIfCreated() {
    if (created_.load(std::memory_order_relaxed)) instance().stop();
}

bool OmpBridgeClient::call(const std::string& /*method*/, const nlohmann::json& /*params*/,
                           nlohmann::json& /*result*/, std::string& err, int /*timeoutMs*/) {
    err = "omp bridge unavailable on Windows";
    return false;
}

}  // namespace agent
}  // namespace chudware

#else  // !_WIN32

#include <chrono>
#include <filesystem>
#include <thread>

#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <sys/prctl.h>
#include <sys/wait.h>
#include <unistd.h>

namespace chudware {
namespace agent {

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

// Walk upward from `start` looking for the vendor/omp/bridge/src/bridge.ts repo-root marker,
// so the bridge resolves whether the caller runs from a build dir, the repo root, or an
// installed AppDir. Mirrors the /proc/self/exe upward-walk convention used by
// host/src/bootstrap.cpp for locating the runtime payload.
std::string findRepoRootFrom(std::filesystem::path start) {
    std::error_code ec;
    for (int i = 0; i < 8 && !start.empty(); ++i) {
        if (std::filesystem::is_regular_file(
                start / "vendor" / "omp" / "bridge" / "src" / "bridge.ts", ec))
            return start.string();
        std::filesystem::path parent = start.parent_path();
        if (parent == start) break;
        start = parent;
    }
    return std::string();
}

std::string resolveRepoRoot() {
    // 1) explicit override — same convention worker_bridge.cpp / host/src/bootstrap.cpp use.
    if (const char* env = std::getenv("CHUDWARE_REPO"); env && *env) {
        std::string root(env);
        if (fileExists(root + "/vendor/omp/bridge/src/bridge.ts")) return root;
    }
    // 2) walk up from the running executable — works from any build/install layout.
    char buf[4096];
    ssize_t n = ::readlink("/proc/self/exe", buf, sizeof(buf) - 1);
    if (n > 0) {
        buf[n] = '\0';
        std::string found = findRepoRootFrom(std::filesystem::path(buf).parent_path());
        if (!found.empty()) return found;
    }
    // 3) walk up from the current working directory (dev runs, e.g. `./test_agent` from a
    // build dir, or ninja test targets run from the repo root).
    {
        std::error_code ec;
        std::string found = findRepoRootFrom(std::filesystem::current_path(ec));
        if (!found.empty()) return found;
    }
    // 4) last resort — the same fallback worker_bridge.cpp already uses for the Python worker.
    const std::string home = homeDir();
    return home.empty() ? std::string(".") : home + "/Repositories/CAD";
}

// bun resolution order: explicit override, common curl-installer/package-manager locations,
// else bare "bun" (PATH search via execvp inside the forked child).
std::string resolveBunExe() {
    if (const char* env = std::getenv("CHUDWARE_BUN"); env && *env) return std::string(env);
    const std::string home = homeDir();
    if (!home.empty() && fileExists(home + "/.bun/bin/bun")) return home + "/.bun/bin/bun";
    if (fileExists("/usr/local/bin/bun")) return "/usr/local/bin/bun";
    if (fileExists("/usr/bin/bun")) return "/usr/bin/bun";
    return "bun";
}

// The bridge's runtime deps live in <vendorOmp>/node_modules, which is gitignored — a fresh
// checkout or a clean build lacks them, and `bun run bridge.ts` then fails to resolve the
// workspace packages. Materialize them ONCE from the committed bun.lock (frozen) so the core
// is self-sufficient (no manual `bun install` step for providers to work). Best-effort +
// idempotent: a present node_modules short-circuits; on install failure we fall through and
// let spawn() surface bun's own diagnostic. Caller holds mtx_ (first-spawn resolvePaths).
void ensureBridgeDeps(const std::string& vendorOmpDir, const std::string& bunExe) {
    std::error_code ec;
    if (std::filesystem::is_directory(vendorOmpDir + "/node_modules", ec))
        return;  // already packed
    if (!std::filesystem::is_regular_file(vendorOmpDir + "/package.json", ec))
        return;  // no vendored workspace here — nothing to install
    pid_t pid = fork();
    if (pid < 0) return;
    if (pid == 0) {
        if (::chdir(vendorOmpDir.c_str()) != 0) _exit(127);
        // Install chatter -> stderr so it never contaminates a JSON-RPC stdout stream.
        dup2(STDERR_FILENO, STDOUT_FILENO);
        execlp(bunExe.c_str(), bunExe.c_str(), "install", "--frozen-lockfile", (char*)nullptr);
        _exit(127);
    }
    int status = 0;
    waitpid(pid, &status, 0);
}

}  // namespace

std::atomic<bool> OmpBridgeClient::created_{false};

OmpBridgeClient& OmpBridgeClient::instance() {
    static OmpBridgeClient inst;
    created_.store(true, std::memory_order_relaxed);
    return inst;
}

void OmpBridgeClient::shutdownIfCreated() {
    if (created_.load(std::memory_order_relaxed)) instance().stop();
}

OmpBridgeClient::~OmpBridgeClient() { stop(); }

void OmpBridgeClient::stop() {
    std::lock_guard<std::mutex> lk(mtx_);
    if (!running_ && process_ < 0 && childStdinWr_ < 0 && childStdoutRd_ < 0) return;
    // bridge.ts exits on stdin EOF — its `for await (const line of rl)` loop simply ends and
    // main() returns — so unlike worker_bridge's Python worker there is no explicit shutdown
    // method to write first; closing the write end is the whole signal.
    if (childStdinWr_ >= 0) {
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

void OmpBridgeClient::reap() {
    // Caller holds mtx_. Tear down without the graceful-EOF wait (child already dead or the
    // pipe is broken).
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
            ::kill(process_, SIGKILL);
            waitpid(process_, nullptr, 0);
        }
        process_ = -1;
    }
    readBuf_.clear();
    running_ = false;
}

bool OmpBridgeClient::resolvePaths(std::string& err) {
    if (pathsResolved_) return true;
    bunExe_ = resolveBunExe();
    bridgeScript_ = envOr("CHUDWARE_OMP_BRIDGE", resolveRepoRoot() + "/vendor/omp/bridge/src/bridge.ts");
    if (!fileExists(bridgeScript_)) {
        err = "OMP bridge script not found: " + bridgeScript_ +
              " (set CHUDWARE_OMP_BRIDGE to the script path, or CHUDWARE_REPO to a checkout "
              "containing vendor/omp/)";
        return false;
    }
    // <...>/vendor/omp/bridge/src/bridge.ts -> <...>/vendor/omp. Self-pack the workspace deps
    // on first use if they are absent (gitignored node_modules on a fresh checkout).
    ensureBridgeDeps(
        std::filesystem::path(bridgeScript_).parent_path().parent_path().parent_path().string(),
        bunExe_);
    pathsResolved_ = true;
    return true;
}

bool OmpBridgeClient::spawn(std::string& err) {
    // Caller holds mtx_. running_ must be false / fds closed.
    if (!resolvePaths(err)) return false;

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
        ::prctl(PR_SET_PDEATHSIG, SIGKILL);
        // Race guard: if hostd died between fork and prctl, PDEATHSIG never fires — re-check
        // the parent is still us.
        if (::getppid() != parentPid) _exit(1);
        // stdin <- inPipe[0], stdout -> outPipe[1]; stderr stays connected (bridge.ts logs its
        // own [omp-bridge] lines there — never mixed into the response stream).
        dup2(inPipe[0], STDIN_FILENO);
        dup2(outPipe[1], STDOUT_FILENO);
        ::close(inPipe[0]);
        ::close(inPipe[1]);
        ::close(outPipe[0]);
        ::close(outPipe[1]);
        // Close every other inherited fd (hostd's listen/client sockets etc.) so they never
        // leak into the bun child.
        ::close_range(3, ~0U, 0);
        execlp(bunExe_.c_str(), bunExe_.c_str(), "run", bridgeScript_.c_str(), (char*)nullptr);
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
    return true;
}

bool OmpBridgeClient::writeLine(const std::string& line) {
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

bool OmpBridgeClient::readLine(std::string& line, int timeoutMs) {
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
        int pr = ::poll(&pfd, 1, 2);  // 2 ms tick — matches worker_bridge.cpp / worker_client.cpp
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

bool OmpBridgeClient::callOnce(const std::string& method, const json& params, json& result,
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
            // Distinguish child death (EOF / reaped) from a live slow call so the outer retry
            // only respawns on a dead child.
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
            continue;  // ignore non-JSON noise (should not happen — stdout is response-only)
        }
        if (!j.contains("id") || j["id"].is_null()) continue;
        int rid = -1;
        try {
            rid = j["id"].get<int>();
        } catch (...) {
            continue;
        }
        if (rid != id) continue;

        if (j.value("ok", false)) {
            result = j.value("result", json::object());
            return true;
        }
        // ok:false — bridge.ts always sends a structured {message,status?} object, but tolerate
        // a bare string too (defensive, mirrors worker_bridge.cpp's same fallback).
        const auto it = j.find("error");
        if (it != j.end() && it->is_object()) {
            result = *it;
            err = it->value("message", std::string("omp bridge rpc error"));
            return false;
        }
        err = (it != j.end() && it->is_string()) ? it->get<std::string>()
                                                 : std::string("unknown omp bridge rpc error");
        result = json::object();
        return false;
    }
}

bool OmpBridgeClient::call(const std::string& method, const json& params, json& result,
                           std::string& err, int timeoutMs) {
    // Serialize the single stdio channel. Only this mutex is held — never a broader agent-run
    // lock — so other threads (if any) keep making progress.
    std::lock_guard<std::mutex> lk(mtx_);
    result = json::object();
    err.clear();
    if (timeoutMs <= 0) timeoutMs = 120000;

    // Child died (write fail/EOF) -> reap + respawn once, then fail. Timeouts and method-level
    // errors do NOT respawn.
    for (int attempt = 0; attempt < 2; ++attempt) {
        std::string attemptErr;
        if (callOnce(method, params, result, attemptErr, timeoutMs)) return true;

        const bool childDead =
            attemptErr == "write failed" || attemptErr == "closed waiting for response";
        // Method-level errors (ok:false with a structured {message,status} object) are final.
        const bool methodError = result.is_object() && result.contains("message");
        if (methodError || !childDead) {
            err = std::move(attemptErr);
            return false;
        }
        reap();
        err = std::move(attemptErr);
        if (attempt == 1) return false;
        // loop: respawn inside next callOnce
    }
    return false;
}

}  // namespace agent
}  // namespace chudware

#endif  // !_WIN32
