// core/agent/omp_bridge_client.cpp — see omp_bridge_client.h. Long-lived newline-delimited
// JSON-RPC sidecar transport. Packaged builds execute the canonical adjacent bridge directly;
// developer builds retain explicit overrides and the Bun source bridge. POSIX uses fork/pipe;
// Windows uses CreateProcessW with inherited stdio pipes.
#include "agent/omp_bridge_client.h"
#include "agent/providers.h"  // provider_cancel_flag() — abort in-flight bridge complete() on Stop

#include <algorithm>
#include <chrono>
#include <filesystem>
#include <fstream>
#include <thread>
#include <vector>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include "util/platform_paths.h"
#include "security/integrity.h"
#include "security/runtime_guard.h"

#ifdef _WIN32

#define WIN32_LEAN_AND_MEAN
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>

namespace chudware {
namespace agent {

using nlohmann::json;

namespace {

std::wstring widen(const std::string& value) {
    if (value.empty()) return std::wstring();
    const int count = MultiByteToWideChar(CP_UTF8, 0, value.data(), static_cast<int>(value.size()),
                                          nullptr, 0);
    if (count <= 0) return std::wstring();
    std::wstring out(static_cast<size_t>(count), L'\0');
    MultiByteToWideChar(CP_UTF8, 0, value.data(), static_cast<int>(value.size()), out.data(), count);
    return out;
}

bool fileExistsWin(const std::string& path) {
    const DWORD attrs = GetFileAttributesW(widen(path).c_str());
    return attrs != INVALID_FILE_ATTRIBUTES && !(attrs & FILE_ATTRIBUTE_DIRECTORY);
}

std::string winError(DWORD code) {
    char* msg = nullptr;
    const DWORD count = FormatMessageA(FORMAT_MESSAGE_ALLOCATE_BUFFER |
                                           FORMAT_MESSAGE_FROM_SYSTEM |
                                           FORMAT_MESSAGE_IGNORE_INSERTS,
                                       nullptr, code, 0, reinterpret_cast<char*>(&msg), 0, nullptr);
    std::string out = count && msg ? std::string(msg, count) : "error " + std::to_string(code);
    if (msg) LocalFree(msg);
    while (!out.empty() && (out.back() == '\r' || out.back() == '\n' || out.back() == '.'))
        out.pop_back();
    return out;
}

#ifdef CHUDWARE_DEVELOPER_BUILD
std::string findRepoRootWin(std::filesystem::path start) {
    std::error_code ec;
    for (int i = 0; i < 8 && !start.empty(); ++i) {
        if (std::filesystem::is_regular_file(
                start / "vendor" / "omp" / "bridge" / "src" / "bridge.ts", ec))
            return start.string();
        const std::filesystem::path parent = start.parent_path();
        if (parent == start) break;
        start = parent;
    }
    return std::string();
}

std::string resolveRepoRootWin() {
    if (const char* env = std::getenv("CHUDWARE_REPO"); env && *env) {
        if (fileExistsWin(std::string(env) + "/vendor/omp/bridge/src/bridge.ts")) return env;
    }
    std::vector<wchar_t> exe(32768);
    const DWORD count = GetModuleFileNameW(nullptr, exe.data(), static_cast<DWORD>(exe.size()));
    if (count > 0 && count < exe.size()) {
        const std::string found = findRepoRootWin(std::filesystem::path(exe.data()).parent_path());
        if (!found.empty()) return found;
    }
    std::error_code ec;
    const std::string found = findRepoRootWin(std::filesystem::current_path(ec));
    if (!found.empty()) return found;
    if (const char* home = std::getenv("USERPROFILE"); home && *home)
        return std::string(home) + "/Repositories/CAD";
    return ".";
}

std::string resolveBunExeWin() {
    if (const char* env = std::getenv("CHUDWARE_BUN"); env && *env) return env;
    if (const char* home = std::getenv("USERPROFILE"); home && *home) {
        const std::string candidate = std::string(home) + "/.bun/bin/bun.exe";
        if (fileExistsWin(candidate)) return candidate;
    }
    return "bun.exe";
}
#endif

bool readerReadLineWin(HANDLE pipe, std::string& buffer, std::string& line) {
    for (;;) {
        const size_t newline = buffer.find('\n');
        if (newline != std::string::npos) {
            line = buffer.substr(0, newline);
            buffer.erase(0, newline + 1);
            return true;
        }
        char chunk[4096];
        DWORD got = 0;
        if (!ReadFile(pipe, chunk, sizeof(chunk), &got, nullptr) || got == 0) return false;
        buffer.append(chunk, static_cast<size_t>(got));
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
        std::lock_guard<std::mutex> lock(mtx_);
        if (!running_ && !process_ && !job_ && !childStdinWr_ && !childStdoutRd_ &&
            !reader_.joinable())
            return;
        if (childStdinWr_) {
            CloseHandle(static_cast<HANDLE>(childStdinWr_));
            childStdinWr_ = nullptr;
        }
        if (process_ && WaitForSingleObject(static_cast<HANDLE>(process_), 2000) == WAIT_TIMEOUT) {
            TerminateProcess(static_cast<HANDLE>(process_), 1);
        }
    }
    if (reader_.joinable()) reader_.join();
    std::lock_guard<std::mutex> lock(mtx_);
    if (running_) reap();
}

void OmpBridgeClient::reap() {
    if (childStdinWr_) {
        CloseHandle(static_cast<HANDLE>(childStdinWr_));
        childStdinWr_ = nullptr;
    }
    if (childStdoutRd_) {
        CloseHandle(static_cast<HANDLE>(childStdoutRd_));
        childStdoutRd_ = nullptr;
    }
    if (process_) {
        HANDLE process = static_cast<HANDLE>(process_);
        if (WaitForSingleObject(process, 0) == WAIT_TIMEOUT) {
            TerminateProcess(process, 1);
            WaitForSingleObject(process, 2000);
        }
        CloseHandle(process);
        process_ = nullptr;
    }
    if (job_) {
        CloseHandle(static_cast<HANDLE>(job_));
        job_ = nullptr;
    }
    running_ = false;
    if (stderrPump_.joinable()) stderrPump_.join();
    failAllPending("omp bridge subprocess exited");
}
bool OmpBridgeClient::resolvePaths(std::string& err) {
    if (pathsResolved_) return true;
#ifdef CHUDWARE_DEVELOPER_BUILD
    if (const char* env = std::getenv("CHUDWARE_OMP_BRIDGE_BIN"); env && *env) {
        std::error_code ec;
        bridgeScript_ = std::filesystem::weakly_canonical(env, ec).string();
        if (ec) bridgeScript_ = env;
        compiledBridge_ = true;
    }
    if (bridgeScript_.empty()) {
        bunExe_ = resolveBunExeWin();
        if (const char* env = std::getenv("CHUDWARE_OMP_BRIDGE"); env && *env)
            bridgeScript_ = env;
        else
            bridgeScript_ = resolveRepoRootWin() + "/vendor/omp/bridge/src/bridge.ts";
    }
#else
    const std::string executable = platform::executablePath();
    if (executable.empty()) {
        err = "could not resolve the current executable path";
        return false;
    }
    std::error_code ec;
    bridgeScript_ = std::filesystem::weakly_canonical(
                        std::filesystem::path(executable).parent_path() /
                            "chudware-omp-bridge.exe",
                        ec)
                        .string();
    if (ec) {
        err = "could not canonicalize adjacent OMP bridge path";
        return false;
    }
    if (const char* env = std::getenv("CHUDWARE_OMP_BRIDGE_BIN"); env && *env) {
        std::error_code envEc;
        const auto supplied = std::filesystem::weakly_canonical(env, envEc);
        if (envEc || supplied != std::filesystem::path(bridgeScript_)) {
            err = "CHUDWARE_OMP_BRIDGE_BIN must name the adjacent packaged bridge";
            return false;
        }
    }
    compiledBridge_ = true;
#endif
    if (!fileExistsWin(bridgeScript_)) {
        err = std::string(compiledBridge_ ? "OMP bridge binary not found: "
                                          : "OMP bridge script not found: ") +
              bridgeScript_;
        return false;
    }
    if (compiledBridge_)
        bridgeCwd_ = std::filesystem::path(bridgeScript_).parent_path().string();
    else
        vendorOmpDir_ = std::filesystem::path(bridgeScript_)
                            .parent_path().parent_path().parent_path().string();
    pathsResolved_ = true;
    return true;
}

void OmpBridgeClient::provisionDeps() {
    // Release bridges are self-contained. Developer workspaces retain their existing Bun
    // install workflow; platform builders run the locked install before compiling releases.
}
namespace {
bool allowBridgeSpawn(std::string& err) {
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

bool OmpBridgeClient::spawn(std::string& err) {
    if (!allowBridgeSpawn(err)) return false;
    if (!resolvePaths(err)) return false;
    provisionDeps();

    SECURITY_ATTRIBUTES attrs{};
    attrs.nLength = sizeof(attrs);
    attrs.bInheritHandle = TRUE;
    HANDLE outRead = nullptr, outWrite = nullptr;
    HANDLE inRead = nullptr, inWrite = nullptr;
    HANDLE errRead = nullptr, errWrite = nullptr;
    auto closeIf = [](HANDLE& handle) {
        if (handle) CloseHandle(handle);
        handle = nullptr;
    };
    if (!CreatePipe(&outRead, &outWrite, &attrs, 0) ||
        !CreatePipe(&inRead, &inWrite, &attrs, 0) ||
        !CreatePipe(&errRead, &errWrite, &attrs, 0)) {
        const DWORD code = GetLastError();
        closeIf(outRead); closeIf(outWrite); closeIf(inRead); closeIf(inWrite);
        closeIf(errRead); closeIf(errWrite);
        err = "CreatePipe failed: " + winError(code);
        return false;
    }
    SetHandleInformation(outRead, HANDLE_FLAG_INHERIT, 0);
    SetHandleInformation(inWrite, HANDLE_FLAG_INHERIT, 0);
    SetHandleInformation(errRead, HANDLE_FLAG_INHERIT, 0);

    STARTUPINFOW startup{};
    startup.cb = sizeof(startup);
    startup.dwFlags = STARTF_USESTDHANDLES;
    startup.hStdInput = inRead;
    startup.hStdOutput = outWrite;
    startup.hStdError = errWrite;

    const std::string launcher = compiledBridge_ ? bridgeScript_ : bunExe_;
    std::wstring command = L"\"" + widen(launcher) + L"\"";
    if (!compiledBridge_) command += L" run \"" + widen(bridgeScript_) + L"\"";
    std::vector<wchar_t> commandBuffer(command.begin(), command.end());
    commandBuffer.push_back(L'\0');
    const std::wstring cwd = std::filesystem::path(compiledBridge_ ? bridgeCwd_ : vendorOmpDir_)
                                 .wstring();
    const std::wstring application = widen(launcher);

    HANDLE job = CreateJobObjectW(nullptr, nullptr);
    if (!job) {
        const DWORD code = GetLastError();
        closeIf(outRead); closeIf(outWrite); closeIf(inRead); closeIf(inWrite);
        closeIf(errRead); closeIf(errWrite);
        err = "CreateJobObjectW failed: " + winError(code);
        return false;
    }
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits{};
    limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
    if (!SetInformationJobObject(job, JobObjectExtendedLimitInformation, &limits,
                                 sizeof(limits))) {
        const DWORD code = GetLastError();
        CloseHandle(job);
        closeIf(outRead); closeIf(outWrite); closeIf(inRead); closeIf(inWrite);
        closeIf(errRead); closeIf(errWrite);
        err = "SetInformationJobObject failed: " + winError(code);
        return false;
    }

    PROCESS_INFORMATION process{};
    const BOOL created = CreateProcessW(
        application.empty() ? nullptr : application.c_str(), commandBuffer.data(), nullptr, nullptr,
        TRUE, CREATE_NO_WINDOW | CREATE_SUSPENDED, nullptr,
        cwd.empty() ? nullptr : cwd.c_str(), &startup, &process);
    const DWORD code = GetLastError();
    closeIf(outWrite);
    closeIf(inRead);
    closeIf(errWrite);
    if (!created) {
        CloseHandle(job);
        closeIf(outRead); closeIf(inWrite); closeIf(errRead);
        err = "Could not launch OMP bridge: " + winError(code);
        return false;
    }
    if (!AssignProcessToJobObject(job, process.hProcess)) {
        const DWORD assignCode = GetLastError();
        TerminateProcess(process.hProcess, 1);
        CloseHandle(process.hThread);
        CloseHandle(process.hProcess);
        CloseHandle(job);
        closeIf(outRead); closeIf(inWrite); closeIf(errRead);
        err = "AssignProcessToJobObject failed: " + winError(assignCode);
        return false;
    }
    if (ResumeThread(process.hThread) == static_cast<DWORD>(-1)) {
        const DWORD resumeCode = GetLastError();
        TerminateProcess(process.hProcess, 1);
        CloseHandle(process.hThread);
        CloseHandle(process.hProcess);
        CloseHandle(job);
        closeIf(outRead); closeIf(inWrite); closeIf(errRead);
        err = "ResumeThread failed: " + winError(resumeCode);
        return false;
    }
    CloseHandle(process.hThread);
    job_ = job;
    process_ = process.hProcess;
    childStdoutRd_ = outRead;
    childStdinWr_ = inWrite;
    {
        std::lock_guard<std::mutex> lock(stderrMtx_);
        stderrTail_.clear();
    }
    stderrPump_ = std::thread(&OmpBridgeClient::stderrLoop, this, errRead);
    running_ = true;
    return true;
}

void OmpBridgeClient::stderrLoop(void* raw) {
    HANDLE pipe = static_cast<HANDLE>(raw);
    char chunk[4096];
    for (;;) {
        DWORD got = 0;
        if (!ReadFile(pipe, chunk, sizeof(chunk), &got, nullptr) || got == 0) break;
        HANDLE stderrHandle = GetStdHandle(STD_ERROR_HANDLE);
        if (stderrHandle && stderrHandle != INVALID_HANDLE_VALUE) {
            DWORD wrote = 0;
            WriteFile(stderrHandle, chunk, got, &wrote, nullptr);
        }
        std::lock_guard<std::mutex> lock(stderrMtx_);
        stderrTail_.append(chunk, static_cast<size_t>(got));
        if (stderrTail_.size() > kMaxStderrTail)
            stderrTail_.erase(0, stderrTail_.size() - kMaxStderrTail);
    }
    CloseHandle(pipe);
}

std::string OmpBridgeClient::stderrSummary() {
    std::string tail;
    {
        std::lock_guard<std::mutex> lock(stderrMtx_);
        tail = stderrTail_;
    }
    std::vector<std::string> lines;
    size_t pos = 0;
    while (pos <= tail.size()) {
        const size_t newline = tail.find('\n', pos);
        std::string line = tail.substr(
            pos, newline == std::string::npos ? std::string::npos : newline - pos);
        while (!line.empty() && (line.back() == '\r' || line.back() == ' ')) line.pop_back();
        if (!line.empty()) lines.push_back(std::move(line));
        if (newline == std::string::npos) break;
        pos = newline + 1;
    }
    if (lines.empty()) return std::string();
    const size_t take = std::min<size_t>(3, lines.size());
    std::string out;
    for (size_t i = lines.size() - take; i < lines.size(); ++i) {
        if (!out.empty()) out += " | ";
        out += lines[i];
    }
    if (out.size() > kMaxStderrTail) out.resize(kMaxStderrTail);
    return out;
}

bool OmpBridgeClient::writeLine(const std::string& line) {
    if (!childStdinWr_) return false;
    const char* cursor = line.data();
    size_t remaining = line.size();
    while (remaining > 0) {
        DWORD wrote = 0;
        const DWORD chunk = static_cast<DWORD>(std::min<size_t>(remaining, 0xffffffffu));
        if (!WriteFile(static_cast<HANDLE>(childStdinWr_), cursor, chunk, &wrote, nullptr) ||
            wrote == 0)
            return false;
        cursor += wrote;
        remaining -= wrote;
    }
    return true;
}

bool OmpBridgeClient::ensureRunning(std::string& err) {
    std::lock_guard<std::mutex> lock(mtx_);
    if (running_) return true;
    if (reader_.joinable()) reader_.join();
    if (!spawn(err)) return false;
    reader_ = std::thread(&OmpBridgeClient::readerLoop, this, childStdoutRd_, process_);
    return true;
}

void OmpBridgeClient::readerLoop(void* rawPipe, void* rawProcess) {
    HANDLE pipe = static_cast<HANDLE>(rawPipe);
    std::string buffer;
    std::string line;
    while (readerReadLineWin(pipe, buffer, line)) {
        if (!line.empty()) dispatchResponse(line);
    }
    std::lock_guard<std::mutex> lock(mtx_);
    if (running_ && process_ == rawProcess) reap();
}
}  // namespace agent
}  // namespace chudware


#else  // !_WIN32

#include <algorithm>
#include <chrono>
#include <filesystem>
#include <fstream>
#include <thread>
#include <vector>

#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <sys/file.h>
#include <sys/wait.h>
#include <unistd.h>

#include "util/parent_death.h"

namespace chudware {
namespace agent {

using nlohmann::json;

namespace {
bool allowBridgeSpawn(std::string& err) {
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


std::string envOr(const char* key, const std::string& def) {
    if (const char* v = std::getenv(key); v && *v) return std::string(v);
    return def;
}

#ifdef CHUDWARE_DEVELOPER_BUILD
std::string homeDir() {
    if (const char* h = std::getenv("HOME"); h && *h) return std::string(h);
    return std::string();
}
#endif

bool fileExists(const std::string& p) {
    std::error_code ec;
    return std::filesystem::is_regular_file(p, ec);
}

#ifdef CHUDWARE_DEVELOPER_BUILD
// Walk upward from `start` looking for the vendor/omp/bridge/src/bridge.ts repo-root marker,
// so the bridge resolves whether the caller runs from a build dir, the repo root, or an
// installed AppDir. Mirrors the running-executable upward-walk convention used by
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
    if (const std::string exe = platform::executablePath(); !exe.empty()) {
        std::string found = findRepoRootFrom(std::filesystem::path(exe).parent_path());
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

// --- vendored bridge dependency provisioning ----------------------------------------
// The bridge's runtime deps live in <vendorOmp>/node_modules, which is gitignored — a fresh
// checkout or a clean build lacks them, and `bun run bridge.ts` then fails to resolve the
// workspace packages. Materialize them from the committed bun.lock (frozen) so the core is
// self-sufficient (no manual `bun install` step for providers to work).
//
// Presence alone is NOT a sufficient "already provisioned" test. A checkout that gets pulled
// forward keeps its OLD node_modules (git never touches it), so a lockfile bump silently
// leaves the tree half-upgraded: bun then aborts at import with e.g. "SyntaxError: Export
// named 'vectorIndexTopK' not found in module .../@oh-my-pi/pi-natives/native/index.js", the
// child dies before answering anything, and every provider RPC degrades to a bare transport
// error ("No providers reported by the bridge" in Settings -> Providers). That is exactly how
// the shared web daemon rotted. So we stamp the lockfile fingerprint we installed FROM next to
// node_modules and reinstall whenever the committed lock no longer matches it.

// FNV-1a over the provisioning inputs (bun.lock + package.json). Content-addressed rather
// than mtime-based: a git checkout rewrites mtimes for reasons that have nothing to do with
// the dependency set, and a same-second install would race an mtime comparison.
std::string bridgeDepsStamp(const std::string& vendorOmpDir) {
    static const char* kInputs[] = {"/bun.lock", "/bun.lockb", "/package.json"};
    uint64_t h = 1469598103934665603ull;
    bool any = false;
    for (const char* rel : kInputs) {
        std::ifstream in(vendorOmpDir + rel, std::ios::binary);
        if (!in) continue;
        any = true;
        char chunk[8192];
        while (in.read(chunk, sizeof(chunk)) || in.gcount() > 0) {
            const std::streamsize got = in.gcount();
            for (std::streamsize i = 0; i < got; ++i) {
                h ^= (unsigned char)chunk[i];
                h *= 1099511628211ull;
            }
            if (!in) break;
        }
    }
    if (!any) return std::string();
    char out[17];
    std::snprintf(out, sizeof(out), "%016llx", (unsigned long long)h);
    return std::string(out);
}

std::string readStampFile(const std::string& path) {
    std::ifstream in(path);
    std::string s;
    if (in) std::getline(in, s);
    while (!s.empty() && (s.back() == '\n' || s.back() == '\r' || s.back() == ' ')) s.pop_back();
    return s;
}

// True when node_modules exists AND was installed from the lockfile we are holding.
bool bridgeDepsCurrent(const std::string& vendorOmpDir, const std::string& stampPath,
                       const std::string& want) {
    std::error_code ec;
    if (!std::filesystem::is_directory(vendorOmpDir + "/node_modules", ec)) return false;
    return !want.empty() && readStampFile(stampPath) == want;
}

// Run `bun install --frozen-lockfile` in vendorOmpDir; true only on a clean exit.
bool runBridgeInstall(const std::string& vendorOmpDir, const std::string& bunExe) {
    std::string securityError;
    if (!allowBridgeSpawn(securityError)) return false;
    pid_t pid = fork();
    if (pid < 0) return false;
    if (pid == 0) {
        if (::chdir(vendorOmpDir.c_str()) != 0) _exit(127);
        // Install chatter -> stderr so it never contaminates a JSON-RPC stdout stream.
        dup2(STDERR_FILENO, STDOUT_FILENO);
        execlp(bunExe.c_str(), bunExe.c_str(), "install", "--frozen-lockfile", (char*)nullptr);
        _exit(127);
    }
    int status = 0;
    if (waitpid(pid, &status, 0) != pid) return false;
    return WIFEXITED(status) && WEXITSTATUS(status) == 0;
}

// `want` is the caller's already-computed lockfile fingerprint (bridgeDepsStamp). Best-effort
// throughout: on install failure we fall through and let spawn() surface bun's own diagnostic
// (captured from the child's stderr — see stderrLoop).
void ensureBridgeDeps(const std::string& vendorOmpDir, const std::string& bunExe,
                      const std::string& want) {
    std::error_code ec;
    if (!std::filesystem::is_regular_file(vendorOmpDir + "/package.json", ec))
        return;  // no vendored workspace here — nothing to install
    const std::string stampPath = vendorOmpDir + "/.chudware-bridge-deps";
    if (bridgeDepsCurrent(vendorOmpDir, stampPath, want)) return;

    // Every process sharing this checkout (hostd, the desktop GUI, tests) reaches the same
    // verdict at the same moment after a pull, and concurrent `bun install`s into one
    // node_modules corrupt it. Serialize on a lock file, then re-check — the winner's install
    // makes the others' work unnecessary.
    const std::string lockPath = vendorOmpDir + "/.chudware-bridge-install.lock";
    int lockFd = ::open(lockPath.c_str(), O_CREAT | O_RDWR | O_CLOEXEC, 0644);
    if (lockFd >= 0 && ::flock(lockFd, LOCK_EX) != 0) {
        ::close(lockFd);
        lockFd = -1;
    }
    if (lockFd >= 0 && bridgeDepsCurrent(vendorOmpDir, stampPath, want)) {
        ::close(lockFd);
        return;
    }
    // A lockfile/version cutover can REMOVE a nested package (for example the bridge's old
    // direct pi-utils@17 dependency becomes workspace:*). `bun install` is additive here: it
    // updates the root graph but does not reliably delete an obsolete
    // bridge/node_modules/@oh-my-pi/pi-utils, and Bun resolves that stale nested package before
    // the root workspace. The result is an upgrade-only import failure while clean checkouts
    // work — exactly the kind of "ghostly" split-brain behavior this stamp was meant to prevent.
    //
    // We are under the cross-process install lock and both paths are generated output at fixed
    // locations beneath the vendored workspace. Remove them before the frozen install so every
    // fingerprint change is a genuinely clean materialization, not an overlay on an old graph.
    std::filesystem::remove_all(vendorOmpDir + "/node_modules", ec);
    ec.clear();
    std::filesystem::remove_all(vendorOmpDir + "/bridge/node_modules", ec);
    ec.clear();

    const bool installed = runBridgeInstall(vendorOmpDir, bunExe);
    // Stamp only a clean install: a failed one must be retried by the next process/boot
    // rather than remembered as provisioned.
    if (installed && !want.empty()) {
        std::ofstream out(stampPath, std::ios::trunc);
        if (out) out << want << "\n";
    }
    if (lockFd >= 0) {
        ::flock(lockFd, LOCK_UN);
        ::close(lockFd);
    }
}
#endif

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
    // Join the stderr pump BEFORE failing anyone: the child's write end is gone now, so the
    // pump is at EOF and about to exit, and only after it does is stderrTail_ guaranteed to
    // hold the child's dying words. call() decorates the transport error with them.
    if (stderrPump_.joinable()) stderrPump_.join();
    // Whoever noticed the child is dead (a caller's failed write, or the reader thread's own
    // EOF detection) is the single place that guarantees no in-flight call() is left hanging.
    failAllPending("omp bridge subprocess exited");
}

bool OmpBridgeClient::resolvePaths(std::string& err) {
    if (pathsResolved_) return true;
#ifdef CHUDWARE_DEVELOPER_BUILD
    if (const char* env = std::getenv("CHUDWARE_OMP_BRIDGE_BIN"); env && *env) {
        std::error_code ec;
        bridgeScript_ = std::filesystem::weakly_canonical(env, ec).string();
        if (ec) bridgeScript_ = env;
        compiledBridge_ = true;
    }
    if (bridgeScript_.empty()) {
        bunExe_ = resolveBunExe();
        bridgeScript_ = envOr("CHUDWARE_OMP_BRIDGE",
                              resolveRepoRoot() + "/vendor/omp/bridge/src/bridge.ts");
    }
#else
    const std::string executable = platform::executablePath();
    if (executable.empty()) {
        err = "could not resolve the current executable path";
        return false;
    }
    std::error_code ec;
    bridgeScript_ = std::filesystem::weakly_canonical(
                        std::filesystem::path(executable).parent_path() /
                            "chudware-omp-bridge",
                        ec)
                        .string();
    if (ec) {
        err = "could not canonicalize adjacent OMP bridge path";
        return false;
    }
    if (const char* env = std::getenv("CHUDWARE_OMP_BRIDGE_BIN"); env && *env) {
        std::error_code envEc;
        const auto supplied = std::filesystem::weakly_canonical(env, envEc);
        if (envEc || supplied != std::filesystem::path(bridgeScript_)) {
            err = "CHUDWARE_OMP_BRIDGE_BIN must name the adjacent packaged bridge";
            return false;
        }
    }
    compiledBridge_ = true;
#endif
    if (!fileExists(bridgeScript_)) {
        err = std::string(compiledBridge_ ? "OMP bridge binary not found: "
                                          : "OMP bridge script not found: ") +
              bridgeScript_;
        return false;
    }
    if (compiledBridge_)
        bridgeCwd_ = std::filesystem::path(bridgeScript_).parent_path().string();
    else
        vendorOmpDir_ =
            std::filesystem::path(bridgeScript_).parent_path().parent_path().parent_path().string();
    pathsResolved_ = true;
    return true;
}

// Provision (or re-provision) the vendored workspace before each spawn, not once per process:
// a checkout pulled forward under a live daemon leaves node_modules behind its lockfile, and
// the resulting child dies at import on every call. Re-checking here lets the very next
// respawn repair it instead of requiring a daemon restart. `depsStamp_` keeps that cheap and
// stops a hopeless install from re-running on every 3-second provider poll: one attempt per
// distinct lockfile fingerprint per process. Caller holds mtx_.
void OmpBridgeClient::provisionDeps() {
#ifdef CHUDWARE_DEVELOPER_BUILD
    if (compiledBridge_ || vendorOmpDir_.empty()) return;
    const std::string want = bridgeDepsStamp(vendorOmpDir_);
    if (!want.empty() && want == depsStamp_) return;  // this lockfile already handled here
    ensureBridgeDeps(vendorOmpDir_, bunExe_, want);
    depsStamp_ = want;
#endif
}

bool OmpBridgeClient::spawn(std::string& err) {
    if (!allowBridgeSpawn(err)) return false;
    // Caller holds mtx_. running_ must be false / fds closed.
    if (!resolvePaths(err)) return false;
    provisionDeps();

    int outPipe[2], inPipe[2], errPipe[2];
    if (pipe(outPipe) != 0 || pipe(inPipe) != 0 || pipe(errPipe) != 0) {
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
        ::close(errPipe[0]);
        ::close(errPipe[1]);
        return false;
    }
    if (pid == 0) {
        // Child: async-signal-safe orphan-proofing first (same shape as
        // child_supervisor.cpp's doSpawn).
        ::setpgid(0, 0);
        // Parent-death linkage: PDEATHSIG on Linux, a kqueue babysitter on Darwin.
        // Never returns if hostd already died.
        platform::guardParentDeath(parentPid);
        // stdin <- inPipe[0], stdout -> outPipe[1], stderr -> errPipe[1]. bridge.ts logs its
        // own [omp-bridge] lines (and bun's import-time diagnostics) on stderr — never mixed
        // into the response stream. The pump thread tees them straight back out to OUR stderr,
        // so the journal sees exactly what it saw when stderr was inherited.
        dup2(inPipe[0], STDIN_FILENO);
        dup2(outPipe[1], STDOUT_FILENO);
        dup2(errPipe[1], STDERR_FILENO);
        ::close(inPipe[0]);
        ::close(inPipe[1]);
        ::close(outPipe[0]);
        ::close(outPipe[1]);
        ::close(errPipe[0]);
        ::close(errPipe[1]);
        // Close every other inherited fd (hostd's listen/client sockets etc.) so they never
        // leak into the bridge child.
        platform::closeInheritedFds(3);
        if (compiledBridge_ && !bridgeCwd_.empty() && ::chdir(bridgeCwd_.c_str()) != 0)
            _exit(127);
        if (compiledBridge_)
            execlp(bridgeScript_.c_str(), bridgeScript_.c_str(), (char*)nullptr);
        else
            execlp(bunExe_.c_str(), bunExe_.c_str(), "run", bridgeScript_.c_str(),
                   (char*)nullptr);
        _exit(127);
    }

    // Parent mirrors the child's setpgid (whoever runs first wins; both same), then keeps our
    // ends and closes the child's.
    ::setpgid(pid, pid);
    ::close(inPipe[0]);
    ::close(outPipe[1]);
    ::close(errPipe[1]);
    fcntl(outPipe[0], F_SETFL, O_NONBLOCK);
    process_ = pid;
    childStdoutRd_ = outPipe[0];
    childStdinWr_ = inPipe[1];
    {
        std::lock_guard<std::mutex> lk(stderrMtx_);
        stderrTail_.clear();  // this generation's diagnostics only
    }
    stderrPump_ = std::thread(&OmpBridgeClient::stderrLoop, this, errPipe[0]);
    running_ = true;
    return true;
}

// Background stderr pump for one generation: tees the child's stderr to ours (so hostd's
// journal keeps the [omp-bridge] lines it always had) while retaining the tail in memory.
// Without this, a child that dies during module resolution — the shape a stale vendored
// node_modules takes — reaches callers as a bare "omp bridge subprocess exited" and the whole
// diagnosis lives only in a log nobody running the GUI can see. Owns `fd` and closes it.
void OmpBridgeClient::stderrLoop(int fd) {
    char chunk[4096];
    for (;;) {
        ssize_t n = ::read(fd, chunk, sizeof(chunk));
        if (n > 0) {
            ssize_t off = 0;
            while (off < n) {
                ssize_t w = ::write(STDERR_FILENO, chunk + off, (size_t)(n - off));
                if (w <= 0) {
                    if (w < 0 && errno == EINTR) continue;
                    break;
                }
                off += w;
            }
            std::lock_guard<std::mutex> lk(stderrMtx_);
            stderrTail_.append(chunk, (size_t)n);
            if (stderrTail_.size() > kMaxStderrTail)
                stderrTail_.erase(0, stderrTail_.size() - kMaxStderrTail);
            continue;
        }
        if (n < 0 && errno == EINTR) continue;
        break;  // EOF (child gone) or unrecoverable read error
    }
    ::close(fd);
}

// Last few non-empty stderr lines of the most recent child, condensed onto one line for an
// error string. Empty when the child said nothing.
std::string OmpBridgeClient::stderrSummary() {
    std::string tail;
    {
        std::lock_guard<std::mutex> lk(stderrMtx_);
        tail = stderrTail_;
    }
    std::vector<std::string> lines;
    size_t pos = 0;
    while (pos <= tail.size()) {
        const size_t nl = tail.find('\n', pos);
        std::string ln = tail.substr(pos, nl == std::string::npos ? std::string::npos : nl - pos);
        while (!ln.empty() && (ln.back() == '\r' || ln.back() == ' ')) ln.pop_back();
        if (!ln.empty()) lines.push_back(std::move(ln));
        if (nl == std::string::npos) break;
        pos = nl + 1;
    }
    if (lines.empty()) return std::string();
    const size_t take = std::min<size_t>(3, lines.size());
    std::string out;
    for (size_t i = lines.size() - take; i < lines.size(); ++i) {
        if (!out.empty()) out += " | ";
        out += lines[i];
    }
    if (out.size() > kMaxStderrTail) out.resize(kMaxStderrTail);
    return out;
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

}  // namespace agent
}  // namespace chudware

#endif  // _WIN32

namespace chudware {
namespace agent {

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
    //
    // A transport failure carries no information on its own ("omp bridge subprocess exited" is
    // what the Providers tab used to render as an unexplained banner over an empty list), so
    // every transport-level `err` leaves here with the child's own last stderr lines appended —
    // bun's module-resolution SyntaxError, a missing runtime, an OOM kill.
    auto fail = [this](std::string& out, std::string attemptErr) {
        const std::string why = stderrSummary();
        out = why.empty() ? std::move(attemptErr) : std::move(attemptErr) + ": " + why;
        return false;
    };
    for (int attempt = 0; attempt < 2; ++attempt) {
        std::string attemptErr;
        bool childDead = false;
        if (callOnce(method, params, result, attemptErr, timeoutMs, sink, childDead)) return true;

        // Method-level errors (ok:false with a structured {message,status} object) are final,
        // and already say something useful — never bury them under bridge log noise.
        const bool methodError = result.is_object() && result.contains("message");
        if (methodError) {
            err = std::move(attemptErr);
            return false;
        }
        if (!childDead || streamed.load(std::memory_order_relaxed) || attempt == 1)
            return fail(err, std::move(attemptErr));
        // loop: ensureRunning() respawns inside the next callOnce attempt.
    }
    return false;
}

}  // namespace agent
}  // namespace chudware

