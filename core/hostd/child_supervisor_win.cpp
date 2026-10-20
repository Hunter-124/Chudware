// Windows backend of ChildSupervisor (POSIX sibling: child_supervisor.cpp).
// WRITTEN ON LINUX, NEVER COMPILED HERE — unverified until the manual checklist
// in docs/reference/PACKAGING_WINDOWS.md runs on a real box.
//
// Containment model (the PDEATHSIG analog):
//   * each child gets its OWN Job Object with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
//     and WITHOUT JOB_OBJECT_LIMIT_BREAKAWAY_OK, created BEFORE the child runs:
//     CreateProcessW(CREATE_SUSPENDED) -> AssignProcessToJobObject -> ResumeThread,
//     so there is no window where the child (or its own children) escapes;
//   * the job handle is owned by the Child record. Daemon exit — clean OR
//     SIGKILL-equivalent — closes the handle; the last handle drop fires
//     KILL_ON_JOB_CLOSE and the kernel kills the whole child tree. Orphans are
//     structurally impossible, same guarantee as PDEATHSIG+pgid on Linux.
//
// Graceful stop caveat (documented, not solved): Windows has no SIGTERM.
// GenerateConsoleCtrlEvent(CTRL_BREAK_EVENT, pid) only reaches processes
// sharing the caller's console — the daemon is DETACHED_PROCESS (no console),
// so it cannot deliver one. agent_stop{force:false} therefore just starts the
// grace timer and TerminateJobObject()s at the deadline; a cooperative child
// that wants a clean shutdown must watch for it by other means (its stdin
// closing, a file flag, an IPC message). Same for stopAll(): grace, then
// TerminateJobObject.
//
// Threading: the SpawnReq handoff-to-reaper architecture is kept even though
// Windows has no PDEATHSIG-binds-to-thread hazard — spawning from the
// daemon-lifetime reaper thread keeps the two backends' locking and lifetime
// story identical (and per-client threads still never block each other on a
// slow CreateProcessW).
#ifdef _WIN32

#include "child_supervisor.h"

#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>

#include <chrono>
#include <cwctype>
#include <vector>

#include "hostd_log.h"
#include "../../services/ipc/ipc_private.h"
#include "security/integrity.h"
#include "security/runtime_guard.h"
#include "util/platform_paths.h"

namespace hostd {

using nlohmann::json;

namespace {

// Local stand-ins for the POSIX wait flags reapLocked()'s signature carries.
constexpr int kNoHang = 1;   // WNOHANG analog: poll, don't block
constexpr int kBlocking = 0;

long long monoMs() {
    return std::chrono::duration_cast<std::chrono::milliseconds>(
               std::chrono::steady_clock::now().time_since_epoch())
        .count();
}

std::wstring widen(const std::string& s) {
    if (s.empty()) return std::wstring();
    int n = MultiByteToWideChar(CP_UTF8, 0, s.c_str(), (int)s.size(), nullptr, 0);
    std::wstring w(n, L'\0');
    MultiByteToWideChar(CP_UTF8, 0, s.c_str(), (int)s.size(), w.data(), n);
    return w;
}

// One argv element -> command-line token, per the MSVC CRT parsing rules
// ("Parsing C++ command-line arguments", MSDN): backslashes are literal unless
// they precede a double quote; N backslashes + quote -> 2N(+1) backslashes.
void appendQuoted(std::wstring& cmd, const std::wstring& arg) {
    if (!cmd.empty()) cmd += L' ';
    if (!arg.empty() && arg.find_first_of(L" \t\n\v\"") == std::wstring::npos) {
        cmd += arg;
        return;
    }
    cmd += L'"';
    size_t bs = 0;
    for (wchar_t c : arg) {
        if (c == L'\\') {
            ++bs;
            continue;
        }
        if (c == L'"') {
            cmd.append(bs * 2 + 1, L'\\');
            cmd += L'"';
        } else {
            cmd.append(bs, L'\\');
            cmd += c;
        }
        bs = 0;
    }
    cmd.append(bs * 2, L'\\');  // trailing backslashes before the closing quote
    cmd += L'"';
}

std::string lastErr(const char* what) {
    return std::string(what) + " failed (" + std::to_string(GetLastError()) + ")";
}

}  // namespace

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
    // `status` is the raw GetExitCodeProcess value; there is no signal notion.
    log().line("child %s (pid %lu) exited: code=%d mission=%s", c.childId.c_str(),
               c.pid, status, c.missionId.c_str());
    if (!broadcast_) return;
    try {
        json evt = {{"event", "mission.exited"},
                    {"data",
                     {{"child_id", c.childId},
                      {"pid", (long)c.pid},
                      {"exit_code", status},
                      {"signal", 0},
                      {"mission_id", c.missionId},
                      {"doc_id", c.docId}}}};
        broadcast_(evt.dump());
    } catch (...) { log().line("child exit event delivery failed"); }
}

bool ChildSupervisor::reapLocked(std::map<std::string, Child>::iterator it,
                                 int waitFlags) {
    Child& c = it->second;
    DWORD wait = WaitForSingleObject((HANDLE)c.process,
                                     waitFlags == kBlocking ? INFINITE : 0);
    if (wait != WAIT_OBJECT_0) return false;  // still running (or wait failed)
    DWORD code = (DWORD)-1;
    GetExitCodeProcess((HANDLE)c.process, &code);
    Child dead = std::move(it->second);
    children_.erase(it);
    CloseHandle((HANDLE)dead.process);
    // Last job-handle drop: with the process already exited this is a no-op
    // beyond freeing the job; with straggler grandchildren still inside, the
    // close fires KILL_ON_JOB_CLOSE and takes the rest of the tree with it.
    CloseHandle((HANDLE)dead.job);
    emitExited(dead, (int)code);
    return true;
}

void ChildSupervisor::reaperLoop() {
    std::unique_lock<std::mutex> lk(mtx_);
    while (!stopping_) {
        while (!pendingSpawns_.empty()) {
            SpawnReq* req = pendingSpawns_.front();
            pendingSpawns_.pop_front();
            lk.unlock();
            completeSpawn(*req);
            lk.lock();
        }
        const long long now = monoMs();
        for (auto it = children_.begin(); it != children_.end();) {
            auto cur = it++;
            // Grace enforcement for agent_stop{force:false}: no TERM analog was
            // (or could be) sent — the deadline IS the stop.
            if (cur->second.killDeadlineMs > 0 && now >= cur->second.killDeadlineMs) {
                TerminateJobObject((HANDLE)cur->second.job, /*exit code*/ 1);
                cur->second.killDeadlineMs = 0;
            }
            reapLocked(cur, kNoHang);
        }
        if (children_.empty() && pendingSpawns_.empty()) {
            // Idle: nothing to reap and no grace deadline to enforce — sleep
            // until an event notifies instead of spinning at 200 ms forever.
            // Every mutation that ends the idle state notify_all()s under mtx_:
            // agent_spawn (pendingSpawns_ push), agent_stop (deadline arm),
            // doSpawn (children_ insert) and shutdown (stopping_, set locked).
            cv_.wait(lk, [this] {
                return stopping_.load() || !children_.empty() || !pendingSpawns_.empty();
            });
        } else {
            // Children live: the 200 ms cadence IS the reap / grace-deadline poll.
            cv_.wait_for(lk, std::chrono::milliseconds(200));
        }
    }
    while (!pendingSpawns_.empty()) {
        SpawnReq* req = pendingSpawns_.front();
        pendingSpawns_.pop_front();
        completeSpawn(*req, true);
    }
}

void ChildSupervisor::stopAll(int graceMs) {
    // No graceful signal exists (see header comment): give cooperative children
    // the grace window to notice the daemon draining, then terminate the jobs.
    {
        std::lock_guard<std::mutex> lk(mtx_);
        if (children_.empty()) return;
        log().line("stopping %d agent child(ren): %d ms grace, then TerminateJobObject",
                   (int)children_.size(), graceMs);
    }
    const long long deadline = monoMs() + graceMs;
    for (;;) {
        {
            std::lock_guard<std::mutex> lk(mtx_);
            for (auto it = children_.begin(); it != children_.end();) {
                auto cur = it++;
                reapLocked(cur, kNoHang);
            }
            if (children_.empty()) return;
            if (monoMs() >= deadline) break;
        }
        Sleep(50);
    }
    std::lock_guard<std::mutex> lk(mtx_);
    for (auto& [id, c] : children_) TerminateJobObject((HANDLE)c.job, 1);
    for (auto it = children_.begin(); it != children_.end();) {
        auto cur = it++;
        reapLocked(cur, kBlocking);  // safe: the job was just terminated
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
            result = json{{"ok", true}, {"note", "no such child (already exited?)"}};
            return true;
        }
        if (force) {
            TerminateJobObject((HANDLE)it->second.job, 1);
            it->second.killDeadlineMs = 0;
        } else if (it->second.killDeadlineMs == 0) {
            // No SIGTERM analog on Windows (see header) — arm the grace timer;
            // the reaper TerminateJobObject()s at the deadline.
            it->second.killDeadlineMs = monoMs() + 5000;
        }
        cv_.notify_all();
        result = json{{"ok", true}, {"child_id", id},
                      {"signal", force ? "KILL" : "TERM"}};
        return true;
    }

    if (method != "agent_spawn") return false;
    if (!validateSpawn(params, result)) {
        isError = true;
        return true;
    }

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
    fut.get();
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

    std::vector<std::string> argvS;
    for (const auto& a : params["argv"])
        argvS.push_back(a.get<std::string>());

    std::wstring cmdline;
    for (auto& a : argvS) appendQuoted(cmdline, widen(a));
    std::vector<wchar_t> cmdBuf(cmdline.begin(), cmdline.end());
    cmdBuf.push_back(L'\0');  // CreateProcessW may modify lpCommandLine

    // Child environment = daemon env + caller overrides. Wide block, sorted
    // case-insensitively (the documented CreateProcessW requirement), joined as
    // NAME=value\0 ... \0\0. Keys are compared uppercased (env is
    // case-insensitive on Windows), overrides replace inherited values.
    std::map<std::wstring, std::wstring> env;  // key = UPPERCASED name -> "Name=value"
    auto upKey = [](std::wstring s) {
        for (auto& c : s) c = towupper(c);
        return s;
    };
    {
        wchar_t* block = GetEnvironmentStringsW();
        for (wchar_t* p = block; p && *p;) {
            std::wstring entry = p;
            p += entry.size() + 1;
            size_t eq = entry.find(L'=', 1);  // skip the odd "=C:=..." cwd entries' lead =
            if (eq == std::wstring::npos) continue;
            env[upKey(entry.substr(0, eq))] = entry;
        }
        if (block) FreeEnvironmentStringsW(block);
    }
    if (params.contains("env") && params["env"].is_object())
        for (auto& [k, v] : params["env"].items()) {
            std::wstring wk = widen(k);
            std::wstring wv = widen(v.get<std::string>());
            env[upKey(wk)] = wk + L"=" + wv;
        }
    std::wstring envBlock;  // std::map iteration = the sorted order required
    for (auto& [k, e] : env) {
        envBlock += e;
        envBlock.push_back(L'\0');
    }
    envBlock.push_back(L'\0');

    std::string childId, logPath;
    {
        std::lock_guard<std::mutex> lk(mtx_);
        childId = "agent-" + std::to_string((long)GetCurrentProcessId()) + "-" +
                  std::to_string(nextId_++);
    }
    if (!logDir_.empty()) {
        if (!chudware_ipc::privateDir(logDir_)) {
            isError = true;
            result = {{"code", "spawn_failed"}, {"message", "cannot create private child log directory"}};
            return;
        }
        logPath = logDir_ + "\\" + childId + ".log";
    }
    Child c;
    c.childId = childId;
    c.missionId = params.value("mission_id", "");
    c.docId = params.value("doc_id", "");
    c.logPath = logPath;
    c.spawnerClient = clientId;
    c.spawnedMs = monoMs();
    const std::string missionId = c.missionId;

    struct SpawnGuard {
        HANDLE job = nullptr, process = nullptr, thread = nullptr, input = INVALID_HANDLE_VALUE;
        FILE* log = nullptr;
        ~SpawnGuard() {
            if (process) {
                TerminateProcess(process, 1);
                WaitForSingleObject(process, INFINITE);
                CloseHandle(process);
            }
            if (thread) CloseHandle(thread);
            if (job) CloseHandle(job);
            if (input != INVALID_HANDLE_VALUE) CloseHandle(input);
            if (log) std::fclose(log);
        }
    } guard;

    // Job first (assign-before-resume): KILL_ON_JOB_CLOSE, breakaway NOT granted
    // to the child, so nothing it spawns can outlive it.
    HANDLE job = CreateJobObjectW(nullptr, nullptr);
    guard.job = job;
    if (!job) {
        isError = true;
        result = json{{"code", "spawn_failed"}, {"message", lastErr("CreateJobObjectW")}};
        return;
    }
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION jeli{};
    jeli.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
    if (!SetInformationJobObject(job, JobObjectExtendedLimitInformation, &jeli,
                                 sizeof jeli)) {
        isError = true;
        result = json{{"code", "spawn_failed"},
                      {"message", lastErr("SetInformationJobObject")}};
        return;
    }

    // stdio: stdin <- NUL, stdout/stderr -> per-child log (inheritable handles).
    SECURITY_ATTRIBUTES sa{};
    sa.nLength = sizeof sa;
    sa.bInheritHandle = TRUE;
    HANDLE hNul = CreateFileW(L"NUL", GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE,
                              &sa, OPEN_EXISTING, 0, nullptr);
    guard.input = hNul;
    guard.log = logPath.empty() ? nullptr : chudware_ipc::privateFile(logPath, true, true);
    HANDLE hLog = guard.log ? reinterpret_cast<HANDLE>(_get_osfhandle(_fileno(guard.log))) :
                             INVALID_HANDLE_VALUE;
    if (!logPath.empty() && (!guard.log ||
            !SetHandleInformation(hLog, HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT))) {
        isError = true;
        result = {{"code", "spawn_failed"}, {"message", "cannot create private child log"}};
        return;
    }

    STARTUPINFOW si{};
    si.cb = sizeof si;
    si.dwFlags = STARTF_USESTDHANDLES;
    si.hStdInput = hNul;
    si.hStdOutput = hLog;
    si.hStdError = hLog;
    PROCESS_INFORMATION pi{};
    const DWORD flags = CREATE_SUSPENDED | CREATE_NEW_PROCESS_GROUP |
                        CREATE_UNICODE_ENVIRONMENT | CREATE_NO_WINDOW;
    BOOL ok = CreateProcessW(nullptr, cmdBuf.data(), nullptr, nullptr,
                             /*inherit handles*/ TRUE, flags,
                             (LPVOID)envBlock.c_str(), nullptr, &si, &pi);
    guard.process = pi.hProcess;
    guard.thread = pi.hThread;
    const std::string createErr = ok ? "" : lastErr("CreateProcessW");
    if (!ok) {
        isError = true;
        result = json{{"code", "spawn_failed"},
                      {"message", createErr + " (" + argvS[0] + ")"}};
        return;
    }
    if (!AssignProcessToJobObject(job, pi.hProcess)) {
        // Containment is the point — refuse to run an uncontained child.
        const std::string aerr = lastErr("AssignProcessToJobObject");
        isError = true;
        result = json{{"code", "spawn_failed"}, {"message", aerr}};
        return;
    }
    if (ResumeThread(pi.hThread) == static_cast<DWORD>(-1)) {
        isError = true;
        result = {{"code", "spawn_failed"}, {"message", lastErr("ResumeThread")}};
        return;
    }

    c.process = pi.hProcess;
    c.job = job;
    c.pid = pi.dwProcessId;
    result = json{{"child_id", childId},
                  {"pid", (long)pi.dwProcessId},
                  {"pgid", (long)pi.dwProcessId},
                  {"log", logPath}};
    {
        std::lock_guard<std::mutex> lk(mtx_);
        children_.emplace(childId, std::move(c));
    }
    guard.process = nullptr;
    guard.job = nullptr;  // table owns both handles after all throwing work
    cv_.notify_all();
    log().line("spawned child %s pid=%lu argv0=%s mission=%s (client #%d)",
               childId.c_str(), pi.dwProcessId, argvS[0].c_str(),
               missionId.c_str(), clientId);
}

}  // namespace hostd

#endif  // _WIN32
