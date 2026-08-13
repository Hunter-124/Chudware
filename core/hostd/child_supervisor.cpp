// POSIX backend (fork/parent-death guard/exec). Windows sibling: child_supervisor_win.cpp
// (Job Objects, KILL_ON_JOB_CLOSE).
#ifndef _WIN32

#include "child_supervisor.h"

#include <chrono>
#include <cstring>
#include <vector>

#include <fcntl.h>
#include <signal.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <unistd.h>

#include "hostd_log.h"
#include "security/integrity.h"
#include "security/runtime_guard.h"
#include "util/platform_paths.h"
#include "util/parent_death.h"

extern char** environ;

namespace hostd {

using nlohmann::json;

static long long monoMs() {
    return std::chrono::duration_cast<std::chrono::milliseconds>(
               std::chrono::steady_clock::now().time_since_epoch())
        .count();
}

// pipe2(O_CLOEXEC) is Linux-only. Darwin has to set the flag after the fact, so
// there is a window in which a concurrent fork+exec elsewhere in the daemon
// (worker_bridge, omp bridge) could inherit these ends; a leaked read end would
// keep the pipe open past exec and turn "exec failed?" into a blocking read. The
// atomic form is therefore kept wherever the kernel offers it, and the exec-report
// read below is the only consumer, bounded by the child's own exit either way.
static bool cloexecPipe(int fds[2]) {
#if defined(__linux__)
    return ::pipe2(fds, O_CLOEXEC) == 0;
#else
    if (::pipe(fds) != 0) return false;
    for (int i = 0; i < 2; ++i) {
        const int fl = ::fcntl(fds[i], F_GETFD);
        if (fl < 0 || ::fcntl(fds[i], F_SETFD, fl | FD_CLOEXEC) != 0) {
            ::close(fds[0]);
            ::close(fds[1]);
            return false;
        }
    }
    return true;
#endif
}

void ChildSupervisor::start() {
    std::lock_guard<std::mutex> lk(mtx_);
    if (started_) return;
    started_ = true;
    stopping_ = false;
    reaper_ = std::thread([this] { reaperLoop(); });
}

void ChildSupervisor::shutdown() {
    {
        std::lock_guard<std::mutex> lk(mtx_);
        if (!started_) return;
    }
    stopAll();
    {
        // Under mtx_: the reaper's idle cv_.wait re-checks stopping_ under this
        // same lock, so flipping it locked + notifying cannot lose the wakeup
        // (an unlocked write could land between the predicate check and the
        // block, leaving the reaper asleep forever).
        std::lock_guard<std::mutex> lk(mtx_);
        stopping_ = true;
    }
    cv_.notify_all();
    if (reaper_.joinable()) reaper_.join();
    std::lock_guard<std::mutex> lk(mtx_);
    started_ = false;
}

int ChildSupervisor::aliveCount() {
    std::lock_guard<std::mutex> lk(mtx_);
    return (int)children_.size();
}

void ChildSupervisor::statusInto(json& status) {
    std::lock_guard<std::mutex> lk(mtx_);
    status["children"] = (int)children_.size();
    json missions = json::array();
    for (auto& [id, c] : children_)
        if (!c.missionId.empty()) missions.push_back(c.missionId);
    status["missions"] = std::move(missions);
}

void ChildSupervisor::emitExited(const Child& c, int status) {
    int exitCode = WIFEXITED(status) ? WEXITSTATUS(status) : -1;
    int sig = WIFSIGNALED(status) ? WTERMSIG(status) : 0;
    log().line("child %s (pid %ld) exited: code=%d signal=%d mission=%s",
               c.childId.c_str(), (long)c.pid, exitCode, sig, c.missionId.c_str());
    if (!broadcast_) return;
    json evt = {{"event", "mission.exited"},
                {"data",
                 {{"child_id", c.childId},
                  {"pid", (long)c.pid},
                  {"exit_code", exitCode},
                  {"signal", sig},
                  {"mission_id", c.missionId},
                  {"doc_id", c.docId}}}};
    broadcast_(evt.dump());
}

bool ChildSupervisor::reapLocked(std::map<std::string, Child>::iterator it, int waitFlags) {
    // waitpid the REGISTERED pid only — a wait(-1) here would steal the
    // intermediate forks spawnDetached() self-reaps (see ipc_discovery.cpp).
    int status = 0;
    pid_t r = ::waitpid(it->second.pid, &status, waitFlags);
    if (r != it->second.pid) return false;  // still running (0) or ECHILD (-1)
    Child c = std::move(it->second);
    children_.erase(it);
    emitExited(c, status);
    return true;
}

void ChildSupervisor::reaperLoop() {
    std::unique_lock<std::mutex> lk(mtx_);
    while (!stopping_) {
        // Spawn handoff: fork children HERE (a daemon-lifetime thread) because
        // PDEATHSIG fires when the forking thread exits — see SpawnReq.
        while (!pendingSpawns_.empty()) {
            SpawnReq* req = pendingSpawns_.front();
            pendingSpawns_.pop_front();
            lk.unlock();
            doSpawn(*req);
            req->done.set_value();
            lk.lock();
        }
        const long long now = monoMs();
        for (auto it = children_.begin(); it != children_.end();) {
            auto cur = it++;
            // Grace enforcement for agent_stop: TERM was sent, deadline passed,
            // still alive -> KILL the whole group.
            if (cur->second.killDeadlineMs > 0 && now >= cur->second.killDeadlineMs) {
                ::kill(-cur->second.pgid, SIGKILL);
                cur->second.killDeadlineMs = 0;  // one KILL is enough
            }
            reapLocked(cur, WNOHANG);
        }
        if (children_.empty() && pendingSpawns_.empty()) {
            // Idle: nothing to reap and no kill deadline to enforce — sleep
            // until an event notifies instead of spinning at 200 ms forever.
            // Every mutation that ends the idle state notify_all()s under mtx_:
            // agent_spawn (pendingSpawns_ push), agent_stop (deadline arm),
            // doSpawn (children_ insert) and shutdown (stopping_, set locked).
            cv_.wait(lk, [this] {
                return stopping_.load() || !children_.empty() || !pendingSpawns_.empty();
            });
        } else {
            // Children live: the 200 ms cadence IS the reap / kill-deadline poll.
            cv_.wait_for(lk, std::chrono::milliseconds(200));
        }
    }
    // Shutting down: fail any spawn still queued so its caller doesn't hang.
    while (!pendingSpawns_.empty()) {
        SpawnReq* req = pendingSpawns_.front();
        pendingSpawns_.pop_front();
        req->isError = true;
        req->result = json{{"code", "draining"}, {"message", "daemon is shutting down"}};
        req->done.set_value();
    }
}

void ChildSupervisor::stopAll(int graceMs) {
    {
        std::lock_guard<std::mutex> lk(mtx_);
        if (children_.empty()) return;
        log().line("stopping %d agent child(ren): TERM, %d ms grace, KILL",
                   (int)children_.size(), graceMs);
        for (auto& [id, c] : children_) ::kill(-c.pgid, SIGTERM);
    }
    const long long deadline = monoMs() + graceMs;
    for (;;) {
        {
            std::lock_guard<std::mutex> lk(mtx_);
            for (auto it = children_.begin(); it != children_.end();) {
                auto cur = it++;
                reapLocked(cur, WNOHANG);
            }
            if (children_.empty()) return;
            if (monoMs() >= deadline) break;
        }
        ::usleep(50 * 1000);
    }
    // Grace expired: KILL and reap synchronously (blocking wait is safe now —
    // SIGKILL cannot be ignored).
    std::lock_guard<std::mutex> lk(mtx_);
    for (auto& [id, c] : children_) ::kill(-c.pgid, SIGKILL);
    for (auto it = children_.begin(); it != children_.end();) {
        auto cur = it++;
        reapLocked(cur, 0);
    }
}

bool ChildSupervisor::handleMethod(int clientId, const std::string& method,
                                   const json& params, json& result, bool& isError) {
    if (method == "agent_list") {
        std::lock_guard<std::mutex> lk(mtx_);
        json arr = json::array();
        const long long now = monoMs();
        for (auto& [id, c] : children_)
            arr.push_back({{"child_id", c.childId},
                           {"pid", (long)c.pid},
                           {"mission_id", c.missionId},
                           {"doc_id", c.docId},
                           {"spawner_client", c.spawnerClient},
                           {"log", c.logPath},
                           {"age_s", (now - c.spawnedMs) / 1000.0},
                           {"status", c.killDeadlineMs > 0 ? "stopping" : "running"}});
        result = json{{"children", std::move(arr)}};
        return true;
    }

    if (method == "agent_stop") {
        const std::string id = params.value("child_id", "");
        const bool force = params.value("force", false);
        std::lock_guard<std::mutex> lk(mtx_);
        auto it = children_.find(id);
        if (it == children_.end()) {
            // Idempotent: already reaped (or never existed) is not an error —
            // a double stop and a stop racing the reaper look identical.
            result = json{{"ok", true}, {"note", "no such child (already exited?)"}};
            return true;
        }
        if (force) {
            ::kill(-it->second.pgid, SIGKILL);
            it->second.killDeadlineMs = 0;
        } else {
            ::kill(-it->second.pgid, SIGTERM);
            if (it->second.killDeadlineMs == 0)
                it->second.killDeadlineMs = monoMs() + 5000;
        }
        cv_.notify_all();
        result = json{{"ok", true}, {"child_id", id}, {"signal", force ? "KILL" : "TERM"}};
        return true;
    }

    if (method != "agent_spawn") return false;

    // ---- agent_spawn: hand off to the reaper thread (PDEATHSIG, see SpawnReq)
    SpawnReq req;
    req.clientId = clientId;
    req.params = params;
    auto fut = req.done.get_future();
    {
        std::lock_guard<std::mutex> lk(mtx_);
        if (!started_ || stopping_) {
            isError = true;
            result = json{{"code", "draining"}, {"message", "daemon is shutting down"}};
            return true;
        }
        pendingSpawns_.push_back(&req);
    }
    cv_.notify_all();
    fut.wait();
    result = std::move(req.result);
    isError = req.isError;
    return true;
}

void ChildSupervisor::doSpawn(SpawnReq& req) {
    const int clientId = req.clientId;
    const json& params = req.params;
    json& result = req.result;
    bool& isError = req.isError;
    const auto executable = chudware::platform::executablePath();
    const auto integrity = chudware::security::verifyInstalledPayload(
        executable, chudware::security::VerifyMode::ProtectedOperation);
    if (!integrity.ok) {
        isError = true;
        result = json{{"code", "contained"},
                      {"message", "worker spawn blocked: protected_file_hash_mismatch"}};
        return;
    }
    const auto security = chudware::security::inspectAndPersistRuntime(
        chudware::security::SecurityCheckpoint::BeforeWorkerSpawn,
        integrity.root, integrity.files, chudware::security::runtimeGuardStateDir().string());
    if (!security.allowed()) {
        isError = true;
        result = json{{"code", "contained"},
                      {"message", "worker spawn blocked: " + security.denialReason}};
        return;
    }

    if (!params.contains("argv") || !params["argv"].is_array() || params["argv"].empty()) {
        isError = true;
        result = json{{"code", "bad_params"}, {"message", "agent_spawn needs a non-empty argv array"}};
        return;
    }
    std::vector<std::string> argvS;
    for (auto& a : params["argv"]) argvS.push_back(a.is_string() ? a.get<std::string>() : a.dump());

    // Child environment = daemon env + caller overrides (built BEFORE fork —
    // setenv after fork in a threaded process is not async-signal-safe).
    std::map<std::string, std::string> env;
    for (char** e = environ; *e; ++e) {
        const char* eq = strchr(*e, '=');
        if (eq) env[std::string(*e, eq - *e)] = eq + 1;
    }
    if (params.contains("env") && params["env"].is_object())
        for (auto& [k, v] : params["env"].items())
            env[k] = v.is_string() ? v.get<std::string>() : v.dump();
    std::vector<std::string> envS;
    for (auto& [k, v] : env) envS.push_back(k + "=" + v);

    std::string childId, logPath;
    {
        std::lock_guard<std::mutex> lk(mtx_);
        childId = "agent-" + std::to_string((long)::getpid()) + "-" + std::to_string(nextId_++);
    }
    if (!logDir_.empty()) {
        ::mkdir(logDir_.c_str(), 0700);  // stateDir()/logs usually exists already
        logPath = logDir_ + "/" + childId + ".log";
    }

    std::vector<char*> argv, envp;
    for (auto& s : argvS) argv.push_back(const_cast<char*>(s.c_str()));
    argv.push_back(nullptr);
    for (auto& s : envS) envp.push_back(const_cast<char*>(s.c_str()));
    envp.push_back(nullptr);

    // CLOEXEC pipe: a successful exec closes it silently; a failed exec writes
    // errno so the caller gets a synchronous clean error (and no table entry).
    int ep[2];
    if (!cloexecPipe(ep)) {
        isError = true;
        result = json{{"code", "spawn_failed"}, {"message", "pipe failed"}};
        return;
    }

    const pid_t parentPid = ::getpid();
    const pid_t pid = ::fork();
    if (pid < 0) {
        ::close(ep[0]);
        ::close(ep[1]);
        isError = true;
        result = json{{"code", "spawn_failed"}, {"message", "fork failed"}};
        return;
    }
    if (pid == 0) {
        // Child: async-signal-safe calls only until exec.
        ::setpgid(0, 0);
        // Parent-death linkage: PDEATHSIG on Linux, a kqueue babysitter on
        // Darwin. Never returns if the daemon already died.
        chudware::platform::guardParentDeath(parentPid);
        int devnull = ::open("/dev/null", O_RDONLY);
        if (devnull >= 0) ::dup2(devnull, 0);
        int logfd = logPath.empty() ? -1
                                    : ::open(logPath.c_str(),
                                             O_WRONLY | O_CREAT | O_APPEND, 0644);
        if (logfd >= 0) {
            ::dup2(logfd, 1);
            ::dup2(logfd, 2);
        }
#if defined(__linux__)
        ::execvpe(argv[0], argv.data(), envp.data());
#else
        // Darwin has no execvpe. Assigning `environ` then calling execvp() is the
        // documented equivalent — execvp hands the current `environ` to the new
        // image. A pointer store is async-signal-safe and this child has one
        // thread, so no one can observe the intermediate state.
        environ = envp.data();
        ::execvp(argv[0], argv.data());
#endif
        int e = errno;
        ssize_t ignored = ::write(ep[1], &e, sizeof e);
        (void)ignored;
        _exit(127);
    }

    // Parent. Mirror the child's setpgid (whoever runs first wins; both same).
    ::setpgid(pid, pid);
    ::close(ep[1]);
    int execErrno = 0;
    ssize_t n = ::read(ep[0], &execErrno, sizeof execErrno);  // EOF = exec succeeded
    ::close(ep[0]);
    if (n > 0) {
        ::waitpid(pid, nullptr, 0);  // reap the stillborn child — no zombie
        isError = true;
        result = json{{"code", "spawn_failed"},
                      {"message", std::string("exec failed: ") + strerror(execErrno) +
                                      " (" + argvS[0] + ")"}};
        return;
    }

    Child c;
    c.childId = childId;
    c.pid = pid;
    c.pgid = pid;
    c.missionId = params.value("mission_id", "");
    c.docId = params.value("doc_id", "");
    c.logPath = logPath;
    c.spawnerClient = clientId;
    c.spawnedMs = monoMs();
    {
        std::lock_guard<std::mutex> lk(mtx_);
        children_[childId] = c;
    }
    cv_.notify_all();
    log().line("spawned child %s pid=%ld argv0=%s mission=%s (client #%d)",
               childId.c_str(), (long)pid, argvS[0].c_str(), c.missionId.c_str(),
               clientId);
    result = json{{"child_id", childId}, {"pid", (long)pid}, {"pgid", (long)pid},
                  {"log", logPath}};

}

}  // namespace hostd

#endif  // !_WIN32
