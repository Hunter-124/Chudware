#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif
#include "util/subprocess.h"

#include <algorithm>
#include <chrono>
#include <cstring>
#include <map>
#include <string>
#include <vector>

#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#else
#include <cerrno>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <spawn.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>
#ifdef __APPLE__
#include <crt_externs.h>
#else
extern char** environ;
#endif
#endif

namespace chudware::util {
namespace {

bool has_nul(const std::string& s) { return s.find('\0') != std::string::npos; }

void append_output(ProcessResult& result, const char* bytes, std::size_t count,
                   std::size_t limit) {
    const std::size_t available = result.output.size() < limit ? limit - result.output.size() : 0;
    const std::size_t kept = std::min(count, available);
    result.output.append(bytes, kept);
    if (kept != count) result.output_truncated = true;
}

#ifdef _WIN32
std::wstring wide(const std::string& s) {
    if (s.empty()) return {};
    const int n = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, s.data(),
                                      static_cast<int>(s.size()), nullptr, 0);
    if (!n) return {};
    std::wstring out(n, L'\0');
    MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, s.data(), static_cast<int>(s.size()),
                        out.data(), n);
    return out;
}

// Windows command line parsing follows the backslash/quote rules of CommandLineToArgvW.
std::wstring quote(const std::wstring& arg) {
    std::wstring result = L"\"";
    std::size_t slashes = 0;
    for (wchar_t c : arg) {
        if (c == L'\\') { ++slashes; continue; }
        result.append(c == L'"' ? 2 * slashes + 1 : slashes, L'\\');
        slashes = 0;
        result.push_back(c);
    }
    result.append(2 * slashes, L'\\');
    result.push_back(L'"');
    return result;
}

struct Handle {
    HANDLE value = nullptr;
    ~Handle() { if (value && value != INVALID_HANDLE_VALUE) CloseHandle(value); }
};

struct Attributes {
    std::vector<unsigned char> storage;
    LPPROC_THREAD_ATTRIBUTE_LIST list = nullptr;
    ~Attributes() { if (list) DeleteProcThreadAttributeList(list); }
    bool init(HANDLE* handles, std::size_t count) {
        SIZE_T size = 0;
        InitializeProcThreadAttributeList(nullptr, 1, 0, &size);
        if (!size) return false;
        storage.resize(size);
        list = reinterpret_cast<LPPROC_THREAD_ATTRIBUTE_LIST>(storage.data());
        if (!InitializeProcThreadAttributeList(list, 1, 0, &size)) { list = nullptr; return false; }
        return UpdateProcThreadAttribute(list, 0, PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
                                         handles, count * sizeof(HANDLE), nullptr, nullptr);
    }
};

struct CaseLess {
    bool operator()(const std::wstring& a, const std::wstring& b) const {
        return _wcsicmp(a.c_str(), b.c_str()) < 0;
    }
};
#else
struct Fd {
    int value = -1;
    ~Fd() { if (value >= 0) ::close(value); }
};
#endif
}  // namespace

ProcessResult run_process(const std::vector<std::string>& argv, const ProcessOptions& options) {
    ProcessResult result;
    if (argv.empty() || argv[0].empty() || has_nul(options.cwd) || options.timeout.count() <= 0) {
        result.error = "invalid process arguments or deadline";
        return result;
    }
    for (const auto& arg : argv) {
        if (has_nul(arg)) { result.error = "process argument contains NUL"; return result; }
    }
    for (const auto& entry : options.env) {
        if (entry.first.empty() || entry.first.find('=') != std::string::npos ||
            has_nul(entry.first) || has_nul(entry.second)) {
            result.error = "invalid environment override";
            return result;
        }
    }
    const auto deadline = std::chrono::steady_clock::now() + options.timeout;
#ifdef _WIN32
    std::wstring command;
    for (const auto& arg : argv) {
        const auto w = wide(arg);
        if (!arg.empty() && w.empty()) { result.error = "invalid UTF-8 process argument"; return result; }
        if (!command.empty()) command.push_back(L' ');
        command += quote(w);
    }
    std::map<std::wstring, std::wstring, CaseLess> env;
    LPWCH inherited = GetEnvironmentStringsW();
    if (!inherited) { result.error = "GetEnvironmentStringsW failed"; return result; }
    for (const wchar_t* p = inherited; *p;) {
        std::wstring item(p);
        p += item.size() + 1;
        const auto equal = item.find(L'=', item[0] == L'=' ? 1 : 0);
        if (equal != std::wstring::npos) env[item.substr(0, equal)] = item.substr(equal + 1);
    }
    FreeEnvironmentStringsW(inherited);
    for (const auto& entry : options.env) {
        const auto key = wide(entry.first), value = wide(entry.second);
        if (key.empty() || (!entry.second.empty() && value.empty())) {
            result.error = "invalid UTF-8 environment override"; return result;
        }
        env[key] = value;
    }
    std::wstring block;
    for (const auto& entry : env) {
        block += entry.first + L"=" + entry.second;
        block.push_back(L'\0');
    }
    block.push_back(L'\0');
    if (env.empty()) block.push_back(L'\0');
    const auto cwd = wide(options.cwd);
    if (!options.cwd.empty() && cwd.empty()) { result.error = "invalid UTF-8 working directory"; return result; }

    SECURITY_ATTRIBUTES sa{sizeof(sa), nullptr, TRUE};
    Handle read_pipe, write_pipe, input;
    if (!CreatePipe(&read_pipe.value, &write_pipe.value, &sa, 0) ||
        !SetHandleInformation(read_pipe.value, HANDLE_FLAG_INHERIT, 0)) {
        result.error = "CreatePipe failed: " + std::to_string(GetLastError()); return result;
    }
    input.value = CreateFileW(L"NUL", GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE,
                              &sa, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (input.value == INVALID_HANDLE_VALUE) { result.error = "opening NUL failed"; return result; }
    HANDLE handles[] = {input.value, write_pipe.value};
    Attributes attributes;
    if (!attributes.init(handles, 2)) {
        result.error = "Cannot restrict inherited process handles"; return result;
    }
    STARTUPINFOEXW startup{};
    startup.StartupInfo.cb = sizeof(startup);
    startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES;
    startup.StartupInfo.hStdInput = input.value;
    startup.StartupInfo.hStdOutput = write_pipe.value;
    startup.StartupInfo.hStdError = write_pipe.value;
    startup.lpAttributeList = attributes.list;
    PROCESS_INFORMATION process{};
    if (!CreateProcessW(nullptr, command.data(), nullptr, nullptr, TRUE,
                        CREATE_NO_WINDOW | CREATE_UNICODE_ENVIRONMENT | CREATE_SUSPENDED |
                            EXTENDED_STARTUPINFO_PRESENT,
                        block.data(), cwd.empty() ? nullptr : cwd.c_str(), &startup.StartupInfo, &process)) {
        result.error = "CreateProcessW failed: " + std::to_string(GetLastError()); return result;
    }
    Handle child{process.hProcess}, thread{process.hThread};
    Handle job{CreateJobObjectW(nullptr, nullptr)};
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits{};
    limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
    if (!job.value ||
        !SetInformationJobObject(job.value, JobObjectExtendedLimitInformation, &limits, sizeof(limits)) ||
        !AssignProcessToJobObject(job.value, child.value)) {
        result.error = "Cannot attach process to a bounded job";
        TerminateProcess(child.value, 1);
        WaitForSingleObject(child.value, INFINITE);
        return result;
    }
    if (ResumeThread(thread.value) == static_cast<DWORD>(-1)) {
        result.error = "Cannot start process";
        TerminateJobObject(job.value, 1);
        WaitForSingleObject(child.value, INFINITE);
        return result;
    }
    CloseHandle(write_pipe.value); write_pipe.value = nullptr;
    bool exited = false;
    for (;;) {
        DWORD available = 0;
        if (PeekNamedPipe(read_pipe.value, nullptr, 0, nullptr, &available, nullptr)) {
            while (available) {
                char buf[8192]; DWORD n = 0;
                if (!ReadFile(read_pipe.value, buf, std::min<DWORD>(available, sizeof(buf)), &n, nullptr) || !n)
                    break;
                append_output(result, buf, n, options.max_output_bytes);
                available -= n;
            }
        }
        exited = WaitForSingleObject(child.value, 0) == WAIT_OBJECT_0;
        if (exited && available == 0) break;
        if (std::chrono::steady_clock::now() >= deadline) {
            result.timed_out = true;
            TerminateJobObject(job.value, 1);
            WaitForSingleObject(child.value, INFINITE);
            break;
        }
        Sleep(10);
    }
    DWORD code = 0;
    if (GetExitCodeProcess(child.value, &code) && code != STILL_ACTIVE)
        result.exit_code = static_cast<int>(code);
#else
    std::map<std::string, std::string> env;
#ifdef __APPLE__
    char** inherited = *_NSGetEnviron();
#else
    char** inherited = environ;
#endif
    for (char** p = inherited; p && *p; ++p) {
        std::string item(*p);
        const auto equal = item.find('=');
        if (equal != std::string::npos) env[item.substr(0, equal)] = item.substr(equal + 1);
    }
    for (const auto& entry : options.env) env[entry.first] = entry.second;
    std::vector<std::string> env_strings;
    env_strings.reserve(env.size());
    for (const auto& entry : env) env_strings.push_back(entry.first + "=" + entry.second);
    std::vector<char*> env_ptrs, args;
    for (auto& item : env_strings) env_ptrs.push_back(item.data());
    env_ptrs.push_back(nullptr);
    for (const auto& arg : argv) args.push_back(const_cast<char*>(arg.c_str()));
    args.push_back(nullptr);

    int pipe_fds[2];
#ifdef __linux__
    if (::pipe2(pipe_fds, O_CLOEXEC) != 0) { result.error = std::strerror(errno); return result; }
#else
    if (::pipe(pipe_fds) != 0) { result.error = std::strerror(errno); return result; }
#endif
    Fd reader{pipe_fds[0]}, writer{pipe_fds[1]};
    for (int* fd : {&reader.value, &writer.value}) {
        if (*fd <= STDERR_FILENO) {
            const int replacement = ::fcntl(*fd, F_DUPFD_CLOEXEC, STDERR_FILENO + 1);
            if (replacement < 0) { result.error = std::strerror(errno); return result; }
            ::close(*fd);
            *fd = replacement;
        }
        if (::fcntl(*fd, F_SETFD, FD_CLOEXEC) < 0) {
            result.error = std::strerror(errno); return result;
        }
    }
    const int flags = ::fcntl(reader.value, F_GETFL);
    if (flags < 0 || ::fcntl(reader.value, F_SETFL, flags | O_NONBLOCK) < 0) {
        result.error = std::strerror(errno); return result;
    }
    posix_spawn_file_actions_t actions;
    posix_spawnattr_t attr;
    int err = posix_spawn_file_actions_init(&actions);
    if (err) { result.error = std::strerror(err); return result; }
    err = posix_spawnattr_init(&attr);
    if (err) { posix_spawn_file_actions_destroy(&actions); result.error = std::strerror(err); return result; }
    auto action = [&](int rc) { if (!err && rc) err = rc; };
    action(posix_spawn_file_actions_addopen(&actions, STDIN_FILENO, "/dev/null", O_RDONLY, 0));
    action(posix_spawn_file_actions_adddup2(&actions, writer.value, STDOUT_FILENO));
    action(posix_spawn_file_actions_adddup2(&actions, writer.value, STDERR_FILENO));
    action(posix_spawn_file_actions_addclose(&actions, reader.value));
    action(posix_spawn_file_actions_addclose(&actions, writer.value));
    if (!options.cwd.empty()) action(posix_spawn_file_actions_addchdir_np(&actions, options.cwd.c_str()));
    action(posix_spawnattr_setflags(&attr, POSIX_SPAWN_SETPGROUP));
    action(posix_spawnattr_setpgroup(&attr, 0));
    pid_t pid = -1;
    if (!err) err = posix_spawnp(&pid, argv[0].c_str(), &actions, &attr, args.data(), env_ptrs.data());
    posix_spawnattr_destroy(&attr);
    posix_spawn_file_actions_destroy(&actions);
    if (err) { result.error = std::strerror(err); return result; }
    ::close(writer.value); writer.value = -1;
    bool exited = false, eof = false;
    int status = 0;
    while (!exited || !eof) {
        char buf[8192];
        for (;;) {
            const ssize_t n = ::read(reader.value, buf, sizeof(buf));
            if (n > 0) {
                append_output(result, buf, static_cast<std::size_t>(n), options.max_output_bytes);
                if (std::chrono::steady_clock::now() >= deadline) break;
                continue;
            }
            if (n == 0) eof = true;
            else if (errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR) {
                result.error = std::strerror(errno); eof = true;
            }
            if (n < 0 && errno == EINTR) continue;
            break;
        }
        if (!exited) {
            const pid_t done = ::waitpid(pid, &status, WNOHANG);
            if (done == pid) exited = true;
            else if (done < 0 && errno != EINTR) { result.error = std::strerror(errno); exited = true; }
        }
        if (exited && eof) break;
        if (std::chrono::steady_clock::now() >= deadline) {
            result.timed_out = true;
            ::kill(-pid, SIGKILL);
            if (!exited) while (::waitpid(pid, &status, 0) < 0 && errno == EINTR) {}
            break;
        }
        if (eof) ::poll(nullptr, 0, 20);
        else {
            pollfd fd{reader.value, POLLIN | POLLHUP, 0};
            ::poll(&fd, 1, 20);
        }
    }
    if (WIFEXITED(status)) result.exit_code = WEXITSTATUS(status);
    else if (WIFSIGNALED(status)) result.exit_code = 128 + WTERMSIG(status);
#endif
    return result;
}

}  // namespace chudware::util
