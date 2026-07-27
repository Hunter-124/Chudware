// core/agent/omp_bridge_client.cpp — see omp_bridge_client.h. POSIX fork+pipe child talking
// newline-delimited JSON-RPC to `bun run vendor/omp/bridge/src/bridge.ts`, structurally mirrored
// on core/hostd/worker_bridge.cpp (the existing Python-worker stdio bridge pattern), but with a
// background reader thread multiplexing responses instead of serializing request/response pairs
// behind one lock — see the design note at the top of omp_bridge_client.h.
#include "agent/omp_bridge_client.h"
#include "agent/providers.h"  // provider_cancel_flag() — abort in-flight bridge complete() on Stop

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
                           nlohmann::json& /*result*/, std::string& err, int /*timeoutMs*/,
                           OnEvent /*onEvent*/) {
    err = "omp bridge unavailable on Windows";
    return false;
}

}  // namespace agent
}  // namespace chudware

#else  // !_WIN32

#include <algorithm>
#include <chrono>
#include <filesystem>
#include <thread>
#include <vector>

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

// Blocking single-line reader used only by the background reader thread: pumps `fd` (this
// generation's child stdout, non-blocking) into `buf` until a full line is available, `fd`
// hits EOF/error, or `pid` is confirmed reaped. Not a class member — it needs nothing from
// `this`, and keeping it free keeps the per-generation read buffer a stack local instead of a
// shared field (only one thread — the reader — ever touches it).
bool readerReadLine(int fd, pid_t pid, std::string& buf, std::string& line) {
    for (;;) {
        size_t nl = buf.find('\n');
        if (nl != std::string::npos) {
            line = buf.substr(0, nl);
            buf.erase(0, nl + 1);
            return true;
        }
        struct pollfd pfd;
        pfd.fd = fd;
        pfd.events = POLLIN;
        pfd.revents = 0;
        int pr = ::poll(&pfd, 1, 2);  // 2 ms tick — matches worker_bridge.cpp / worker_client.cpp
        if (pr < 0) {
            if (errno == EINTR) continue;
            return false;
        }
        if (pr == 0) {
            int status = 0;
            pid_t r = waitpid(pid, &status, WNOHANG);
            if (r == pid || (r < 0 && errno == ECHILD)) {
                // Child exited — drain whatever is already sitting in the pipe once more
                // before declaring EOF (a final response may have raced the exit).
                char chunk[4096];
                ssize_t got = ::read(fd, chunk, sizeof(chunk));
                if (got > 0) {
                    buf.append(chunk, (size_t)got);
                    continue;
                }
                return false;
            }
            continue;
        }
        char chunk[4096];
        ssize_t got = ::read(fd, chunk, sizeof(chunk));
        if (got > 0) {
            buf.append(chunk, (size_t)got);
        } else if (got == 0) {
            return false;  // EOF
        } else if (errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR) {
            return false;
        }
    }
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
    {
        std::lock_guard<std::mutex> lk(mtx_);
        if (!running_ && process_ < 0 && childStdinWr_ < 0 && childStdoutRd_ < 0 &&
            !reader_.joinable())
            return;
        // bridge.ts exits on stdin EOF — its `for await (const line of rl)` loop simply ends
        // and main() returns — so unlike worker_bridge's Python worker there is no explicit
        // shutdown method to write first; closing the write end is the whole signal. The
        // reader thread notices the resulting stdout EOF and finishes the teardown (reap(),
        // which also fails any outstanding callers) itself.
        if (childStdinWr_ >= 0) {
            ::close(childStdinWr_);
            childStdinWr_ = -1;
        }
    }
    if (reader_.joinable()) reader_.join();
    // Safety net: if the reader thread never got to run for this generation (e.g. the child
    // died between spawn() and its first read), finish the teardown here.
    std::lock_guard<std::mutex> lk(mtx_);
    if (running_) reap();
}

void OmpBridgeClient::reap() {
    // Caller holds mtx_. Tear down without the graceful-EOF wait (child already dead, or the
    // pipe is broken, or stop() already closed stdin and is waiting on the reader thread).
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
    running_ = false;
    // Whoever noticed the child is dead (a caller's failed write, or the reader thread's own
    // EOF detection) is the single place that guarantees no in-flight call() is left hanging.
    failAllPending("omp bridge subprocess exited");
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

bool OmpBridgeClient::ensureRunning(std::string& err) {
    std::lock_guard<std::mutex> lk(mtx_);
    if (running_) return true;
    // Previous generation's reader (if any) has already exited by the time running_ went
    // false — reap() only flips it under this same mtx_, and only the reader thread or a
    // caller holding mtx_ ever calls reap(). Join it so exactly one reader thread ever exists
    // per child, then hand the new generation's fds to a fresh one.
    if (reader_.joinable()) reader_.join();
    if (!spawn(err)) return false;
    reader_ = std::thread(&OmpBridgeClient::readerLoop, this, childStdoutRd_, process_);
    return true;
}

void OmpBridgeClient::readerLoop(int fd, pid_t pid) {
    std::string buf;
    std::string line;
    for (;;) {
        if (!readerReadLine(fd, pid, buf, line)) {
            {
                std::lock_guard<std::mutex> lk(mtx_);
                // Only reap if this is still the live generation (nobody else — e.g. a
                // caller's failed write on the same generation — already did it).
                if (running_ && process_ == pid) reap();
            }
            return;
        }
        if (line.empty()) continue;
        dispatchResponse(line);
    }
}

void OmpBridgeClient::dispatchResponse(const std::string& line) {
    json j;
    try {
        j = json::parse(line);
    } catch (...) {
        return;  // ignore non-JSON noise (should not happen — stdout is response-only)
    }
    if (!j.contains("id") || j["id"].is_null()) return;
    int64_t rid = -1;
    try {
        rid = j["id"].get<int64_t>();
    } catch (...) {
        return;
    }

    // A notification frame carries "event" and no "ok": it belongs to a call that is still
    // running (more may follow, and its real response line is still coming), so unlike a
    // response it must NOT retire the Pending.
    const auto ev = j.find("event");
    const bool isEvent = !j.contains("ok") && ev != j.end() && ev->is_string();

    std::shared_ptr<Pending> p;
    {
        std::lock_guard<std::mutex> lk(pendingMtx_);
        auto it = pending_.find(rid);
        if (it == pending_.end()) return;  // unknown/expired id (timed out/cancelled) — drop
        p = it->second;
        if (!isEvent) pending_.erase(it);
    }

    if (isEvent) {
        // pendingMtx_ is already released; the sink runs under the Pending's OWN mutex instead.
        // That is deliberate: the waiting caller flips `detached` under that same mutex the
        // moment it stops waiting, so this check-then-invoke can never straddle a call() return
        // and hand a fragment to a sink whose stack frame is gone. The caller holds the mutex
        // only between its 50 ms cv ticks, so a delta never waits long for it.
        std::lock_guard<std::mutex> plk(p->mtx);
        if (p->done || p->detached || !p->onEvent) return;
        try {
            p->onEvent(j.value("data", json::object()), ev->get<std::string>());
        } catch (...) {
            // A throwing sink must never take down the reader thread — it owns every OTHER
            // in-flight call's response too.
        }
        return;
    }

    std::lock_guard<std::mutex> plk(p->mtx);
    if (j.value("ok", false)) {
        p->ok = true;
        p->result = j.value("result", json::object());
    } else {
        // ok:false — bridge.ts always sends a structured {message,status?} object, but
        // tolerate a bare string too (defensive, mirrors worker_bridge.cpp's same fallback).
        p->ok = false;
        const auto it = j.find("error");
        if (it != j.end() && it->is_object()) {
            p->result = *it;
            p->err = it->value("message", std::string("omp bridge rpc error"));
        } else {
            p->result = json::object();
            p->err = (it != j.end() && it->is_string()) ? it->get<std::string>()
                                                          : std::string("unknown omp bridge rpc error");
        }
    }
    p->done = true;
    p->cv.notify_all();
}

void OmpBridgeClient::failAllPending(const std::string& reason) {
    std::vector<std::shared_ptr<Pending>> victims;
    {
        std::lock_guard<std::mutex> lk(pendingMtx_);
        victims.reserve(pending_.size());
        for (auto& kv : pending_) victims.push_back(kv.second);
        pending_.clear();
    }
    for (auto& p : victims) {
        std::lock_guard<std::mutex> plk(p->mtx);
        if (p->done) continue;  // already satisfied concurrently — don't clobber a real result
        p->ok = false;
        p->err = reason;
        p->result = json::object();
        p->done = true;
        p->cv.notify_all();
    }
}

bool OmpBridgeClient::callOnce(const std::string& method, const json& params, json& result,
                               std::string& err, int timeoutMs, const OnEvent& onEvent,
                               bool& childDead) {
    childDead = false;
    if (!ensureRunning(err)) {
        childDead = true;
        return false;
    }

    const int64_t id = nextId_.fetch_add(1, std::memory_order_relaxed);
    auto pending = std::make_shared<Pending>();
    // Published before the map insert below, so the reader thread never observes a registered
    // Pending with a half-written sink.
    pending->onEvent = onEvent;
    {
        std::lock_guard<std::mutex> lk(pendingMtx_);
        pending_[id] = pending;
    }
    auto deregister = [this, id] {
        std::lock_guard<std::mutex> lk(pendingMtx_);
        pending_.erase(id);
    };

    json req = {{"id", id}, {"method", method}, {"params", params}};
    const std::string line = req.dump() + "\n";
    {
        // Serialize only the write of this one line — never held across the wait below, so
        // other threads' writes (and their subsequent waits) proceed independently.
        std::lock_guard<std::mutex> lk(mtx_);
        if (!running_ || !writeLine(line)) {
            if (running_) reap();  // dead child: fails every outstanding Pending, including ours
            deregister();          // no-op if reap()'s failAllPending already removed us
            {
                // Uniform invariant: callOnce never returns while its Pending can still reach
                // the caller's sink (deregister() alone would leave a reader thread that had
                // already grabbed the shared_ptr free to invoke it).
                std::lock_guard<std::mutex> plk(pending->mtx);
                pending->detached = true;
            }
            err = "write failed";
            childDead = true;
            return false;
        }
    }

    std::unique_lock<std::mutex> plk(pending->mtx);
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::milliseconds(timeoutMs);
    for (;;) {
        if (pending->done) break;
        // Cooperative cancel: agent_cancel flips the run's provider cancel flag. The bridge
        // complete() is a blocking round-trip (up to 300s) that the native curl xfer-abort path
        // can't reach — without this, Stop couldn't interrupt an in-flight LLM call routed
        // through the bun sidecar. Unlike the old serialized channel, cancelling here does NOT
        // kill the subprocess (that would abort every OTHER concurrently in-flight chat's
        // request too) — it just stops this caller from waiting; the eventual late response is
        // dropped by dispatchResponse's "unknown id" path once we deregister below.
        const std::atomic<bool>* cf = provider_cancel_flag();
        if (cf && cf->load(std::memory_order_acquire)) {
            pending->detached = true;
            plk.unlock();
            deregister();
            err = "cancelled";
            return false;
        }
        auto now = std::chrono::steady_clock::now();
        if (now >= deadline) {
            pending->detached = true;
            plk.unlock();
            deregister();
            err = "timeout waiting for response";
            return false;
        }
        const auto remaining = std::chrono::duration_cast<std::chrono::milliseconds>(deadline - now);
        const auto tick = std::min<std::chrono::milliseconds>(std::chrono::milliseconds(50), remaining);
        pending->cv.wait_for(plk, tick);
    }
    // Still holding the Pending's mutex: from here on the reader thread must not touch our
    // sink, whose captures are about to go out of scope with this frame.
    pending->detached = true;

    const bool ok = pending->ok;
    err = pending->err;
    result = pending->result;
    if (!ok) {
        // Distinguish "the whole transport died" (worth a respawn+retry) from a normal
        // ok:false method-level RPC error (final) using reap()'s/callOnce's own fail strings.
        childDead = (err == "omp bridge subprocess exited") || (err == "write failed");
    }
    return ok;
}

bool OmpBridgeClient::call(const std::string& method, const json& params, json& result,
                           std::string& err, int timeoutMs, OnEvent onEvent) {
    result = json::object();
    err.clear();
    if (timeoutMs <= 0) timeoutMs = 120000;

    // Wrap the caller's sink so the retry below can tell whether anything already reached it.
    // Mirrors the native streaming branch's `streamed_now` gate (providers.cpp): once a
    // fragment has been handed to the GUI, replaying the turn would duplicate it on screen, so
    // a dead-child respawn stops being a safe retry.
    std::atomic<bool> streamed{false};
    OnEvent sink;
    if (onEvent) {
        sink = [&streamed, &onEvent](const json& data, const std::string& event) {
            streamed.store(true, std::memory_order_relaxed);
            onEvent(data, event);
        };
    }

    // Child died (write fail/subprocess exit) -> reap + respawn once (inside the next
    // callOnce's ensureRunning), then fail. Timeouts, cancellation, method-level errors, and
    // anything that already streamed do NOT respawn — they're final for this call.
    for (int attempt = 0; attempt < 2; ++attempt) {
        std::string attemptErr;
        bool childDead = false;
        if (callOnce(method, params, result, attemptErr, timeoutMs, sink, childDead)) return true;

        // Method-level errors (ok:false with a structured {message,status} object) are final.
        const bool methodError = result.is_object() && result.contains("message");
        if (methodError || !childDead || streamed.load(std::memory_order_relaxed)) {
            err = std::move(attemptErr);
            return false;
        }
        err = std::move(attemptErr);
        if (attempt == 1) return false;
        // loop: ensureRunning() respawns inside the next callOnce attempt.
    }
    return false;
}

}  // namespace agent
}  // namespace chudware

#endif  // !_WIN32
