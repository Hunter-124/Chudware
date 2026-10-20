#include "firmware/subprocess.h"

#include <algorithm>
#include <chrono>
#include <cstdlib>
#include <filesystem>
#include <string_view>

#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#else
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>
#ifdef __linux__
#include <sys/syscall.h>
#endif
extern char** environ;
#endif

namespace chudware::firmware {
namespace {
#ifndef _WIN32
struct PosixFd {
    int value = -1;
    ~PosixFd() { if (value >= 0) ::close(value); }
};

// Keep pipe/null descriptors away from stdio, including in headless hosts with
// a closed standard descriptor. Atomic CLOEXEC prevents cross-spawn pipe leaks.
bool prepare_fd(int& fd) {
    if (fd <= STDERR_FILENO) {
        const int moved = ::fcntl(fd, F_DUPFD_CLOEXEC, STDERR_FILENO + 1);
        if (moved == -1) return false;
        ::close(fd);
        fd = moved;
    }
    return ::fcntl(fd, F_SETFD, FD_CLOEXEC) != -1;
}

bool capture_pipe(int (&fds)[2]) {
#ifdef __linux__
    if (::pipe2(fds, O_CLOEXEC) != 0) return false;
#else
    if (::pipe(fds) != 0) return false;
#endif
    if (prepare_fd(fds[0]) && prepare_fd(fds[1])) return true;
    ::close(fds[0]);
    ::close(fds[1]);
    return false;
}
#endif
using Clock = std::chrono::steady_clock;

// Match Python subprocess.run(text=True) without allocating a second capture.
void normalize_newlines(std::string& s) {
    size_t dest = 0;
    for (size_t i = 0; i < s.size(); ++i) {
        if (s[i] == '\r') {
            if (i + 1 < s.size() && s[i + 1] == '\n') ++i;
            s[dest++] = '\n';
        } else {
            s[dest++] = s[i];
        }
    }
    s.resize(dest);
}

struct CaptureBudget {
    RunResult& result;
    size_t total_limit;
    size_t stream_limit;

    bool append(std::string& stream, const char* data, size_t size) {
        const size_t remaining = std::min(
            stream_limit - stream.size(),
            total_limit - result.stdout_text.size() - result.stderr_text.size());
        const size_t accepted = std::min(size, remaining);
        stream.append(data, accepted);
        if (accepted == size) return true;
        result.output_limit_exceeded = true;
        result.returncode = -1;
        return false;
    }

    void finish() {
        normalize_newlines(result.stdout_text);
        normalize_newlines(result.stderr_text);
        if (!result.output_limit_exceeded) return;
        constexpr std::string_view marker =
            "\nfirmware subprocess output limit exceeded; process tree terminated\n";
        const size_t marker_size = std::min(marker.size(), stream_limit);
        // Reserve the diagnostic inside both budgets, not in addition to them.
        result.stderr_text.resize(std::min(result.stderr_text.size(),
                                           stream_limit - marker_size));
        result.stdout_text.resize(std::min(result.stdout_text.size(),
            total_limit - result.stderr_text.size() - marker_size));
        result.stderr_text.append(marker.data(), marker_size);
    }
};

std::string resolve_executable(const std::string& name, const std::filesystem::path& cwd) {
    namespace fs = std::filesystem;
    if (name.empty()) return {};
    auto executable = [&](fs::path path) -> std::string {
        if (path.is_relative()) path = cwd / path;
        std::error_code ec;
        if (!fs::is_regular_file(path, ec)) return {};
#ifndef _WIN32
        if (::access(path.c_str(), X_OK) != 0) return {};
#endif
        return path.string();
    };
    if (name.find('/') != std::string::npos
#ifdef _WIN32
        || name.find('\\') != std::string::npos || fs::path(name).is_absolute()
#endif
    ) return executable(fs::path(name));
    const char* path_env = std::getenv("PATH");
    if (!path_env) return {};
    std::string_view paths(path_env);
#ifdef _WIN32
    constexpr char separator = ';';
    // CreateProcess directly executes binaries, never a shell script implicitly.
    constexpr std::string_view extensions[] = {"", ".exe", ".com"};
#else
    constexpr char separator = ':';
#endif
    size_t pos = 0;
    while (pos <= paths.size()) {
        const size_t next = paths.find(separator, pos);
        const auto dir = paths.substr(pos, next == std::string_view::npos ? next : next - pos);
        if (!dir.empty()) {
#ifdef _WIN32
            for (const auto ext : extensions) {
                auto found = executable(fs::path(dir) / (name + std::string(ext)));
                if (!found.empty()) return found;
            }
#else
            auto found = executable(fs::path(dir) / name);
            if (!found.empty()) return found;
#endif
        }
        if (next == std::string_view::npos) break;
        pos = next + 1;
    }
    return {};
}

#ifdef _WIN32
struct WinHandle {
    HANDLE value = nullptr;
    ~WinHandle() { if (value && value != INVALID_HANDLE_VALUE) CloseHandle(value); }
    void close() { if (value && value != INVALID_HANDLE_VALUE) CloseHandle(value); value = nullptr; }
};

std::wstring wide(const std::string& value) {
    if (value.empty()) return {};
    const int n = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, value.data(),
                                     static_cast<int>(value.size()), nullptr, 0);
    if (!n) return {};
    std::wstring result(n, L'\0');
    MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, value.data(),
                        static_cast<int>(value.size()), result.data(), n);
    return result;
}

// Microsoft CRT argv decoding: double backslashes before a quote or closing quote.
void append_argument(std::wstring& command, const std::wstring& arg) {
    if (!command.empty()) command += L' ';
    command += L'"';
    size_t slashes = 0;
    for (wchar_t c : arg) {
        if (c == L'\\') { ++slashes; continue; }
        command.append(slashes * (c == L'"' ? 2 : 1), L'\\');
        slashes = 0;
        if (c == L'"') command += L'\\';
        command += c;
    }
    command.append(slashes * 2, L'\\');
    command += L'"';
}

std::wstring environment_value(const wchar_t* name) {
    const DWORD size = GetEnvironmentVariableW(name, nullptr, 0);
    if (!size) return {};
    std::wstring value(size, L'\0');
    const DWORD written = GetEnvironmentVariableW(name, value.data(), size);
    if (!written || written >= size) return {};
    value.resize(written);
    return value;
}
#endif
}  // namespace

std::string which(const std::string& name) {
    std::error_code ec;
    const auto cwd = std::filesystem::current_path(ec);
    return ec ? std::string{} : resolve_executable(name, cwd);
}

RunResult run_capture(const std::vector<std::string>& argv, const std::string& cwd,
                      double timeout_s, bool minimal_env, std::size_t output_limit) {
    RunResult rr;
    if (argv.empty()) return rr;
    CaptureBudget budget{rr, output_limit, output_limit / 2};
    std::error_code ec;
    const auto workdir = cwd.empty() ? std::filesystem::current_path(ec)
                                   : std::filesystem::absolute(cwd, ec);
    if (ec) return rr;
    const std::string executable = resolve_executable(argv[0], workdir);
    if (executable.empty()) { rr.not_found = true; return rr; }
    const auto start = Clock::now();
    auto expired = [&] {
        return timeout_s > 0 && std::chrono::duration<double>(Clock::now() - start).count() >= timeout_s;
    };

#ifdef _WIN32
    const std::wstring program = wide(executable);
    const std::wstring directory = workdir.wstring();
    std::wstring command;
    for (const auto& arg : argv) {
        auto converted = wide(arg);
        if (!arg.empty() && converted.empty()) return rr;
        append_argument(command, converted);
    }
    if (program.empty()) return rr;
    std::wstring environment;
    if (minimal_env) {
        auto add = [&](const wchar_t* key, const std::wstring& value) {
            environment += key;
            environment += L'=';
            environment += value;
            environment += L'\0';
        };
        // Windows environment entries are case-insensitively sorted.
        add(L"HOME", directory);
        add(L"PATH", environment_value(L"PATH"));
        add(L"SystemRoot", environment_value(L"SystemRoot"));
        add(L"TMPDIR", directory);
        environment += L'\0';
    }
    SECURITY_ATTRIBUTES sa{sizeof(sa), nullptr, TRUE};
    WinHandle out_read, out_write, err_read, err_write, input, job;
    if (!CreatePipe(&out_read.value, &out_write.value, &sa, 0) ||
        !CreatePipe(&err_read.value, &err_write.value, &sa, 0) ||
        !SetHandleInformation(out_read.value, HANDLE_FLAG_INHERIT, 0) ||
        !SetHandleInformation(err_read.value, HANDLE_FLAG_INHERIT, 0)) return rr;
    input.value = CreateFileW(L"NUL", GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE,
                             &sa, OPEN_EXISTING, 0, nullptr);
    if (input.value == INVALID_HANDLE_VALUE) return rr;
    job.value = CreateJobObjectW(nullptr, nullptr);
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits{};
    limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
    if (!job.value || !SetInformationJobObject(job.value, JobObjectExtendedLimitInformation,
                                              &limits, sizeof(limits))) return rr;
    // Explicit handle allowlist avoids inheriting unrelated operator handles.
    STARTUPINFOEXW si{};
    si.StartupInfo.cb = sizeof(si);
    si.StartupInfo.dwFlags = STARTF_USESTDHANDLES;
    si.StartupInfo.hStdOutput = out_write.value;
    si.StartupInfo.hStdError = err_write.value;
    si.StartupInfo.hStdInput = input.value;
    SIZE_T attribute_size = 0;
    InitializeProcThreadAttributeList(nullptr, 1, 0, &attribute_size);
    std::vector<unsigned char> attributes(attribute_size);
    si.lpAttributeList = reinterpret_cast<LPPROC_THREAD_ATTRIBUTE_LIST>(attributes.data());
    if (!InitializeProcThreadAttributeList(si.lpAttributeList, 1, 0, &attribute_size)) return rr;
    HANDLE inherited[] = {out_write.value, err_write.value, input.value};
    const bool handles_ready = UpdateProcThreadAttribute(si.lpAttributeList, 0,
        PROC_THREAD_ATTRIBUTE_HANDLE_LIST, inherited, sizeof(inherited), nullptr, nullptr) != FALSE;
    PROCESS_INFORMATION pi{};
    const bool created = handles_ready && CreateProcessW(program.c_str(), command.data(),
        nullptr, nullptr, TRUE, CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT | EXTENDED_STARTUPINFO_PRESENT,
        minimal_env ? environment.data() : nullptr, directory.c_str(), &si.StartupInfo, &pi);
    DeleteProcThreadAttributeList(si.lpAttributeList);
    if (!created) return rr;
    WinHandle process, thread;
    process.value = pi.hProcess;
    thread.value = pi.hThread;
    if (!AssignProcessToJobObject(job.value, process.value) || ResumeThread(thread.value) == static_cast<DWORD>(-1)) {
        TerminateProcess(process.value, 1);  // Still suspended: never execute outside the job.
        return rr;
    }
    out_write.close();
    err_write.close();
    input.close();
    bool out_open = true, err_open = true, exited = false, stopped = false;
    char buffer[4096];
    auto drain = [&](HANDLE handle, bool& open, std::string& text) {
        if (!open || stopped) return;
        DWORD available = 0;
        if (!PeekNamedPipe(handle, nullptr, 0, nullptr, &available, nullptr)) { open = false; return; }
        if (!available) return;
        DWORD received = 0;
        const DWORD count = std::min(available, static_cast<DWORD>(sizeof(buffer)));
        if (!ReadFile(handle, buffer, count, &received, nullptr) || !received) { open = false; return; }
        if (!budget.append(text, buffer, received)) {
            TerminateJobObject(job.value, 1);
            stopped = true;
            out_open = err_open = false;
        }
    };
    while (!exited || out_open || err_open) {
        const DWORD state = WaitForSingleObject(process.value, 0);
        exited = state == WAIT_OBJECT_0;
        if (state == WAIT_FAILED) { TerminateJobObject(job.value, 1); break; }
        if (!stopped && expired()) {
            TerminateJobObject(job.value, 1);
            rr.timed_out = true;
            stopped = true;
            out_open = err_open = false;
        }
        drain(out_read.value, out_open, rr.stdout_text);
        drain(err_read.value, err_open, rr.stderr_text);
        if (!exited) WaitForSingleObject(process.value, 10);
        else if (out_open || err_open) Sleep(1);
    }
    if (!rr.timed_out && !rr.output_limit_exceeded && exited) {
        DWORD code = 0;
        if (GetExitCodeProcess(process.value, &code)) rr.returncode = static_cast<int>(code);
    }
#else
    // Prepare everything that allocates or searches PATH before fork. The child
    // uses only async-signal-safe syscalls before execve, even in a threaded host.
    std::vector<char*> arguments;
    arguments.reserve(argv.size() + 1);
    for (const auto& arg : argv) arguments.push_back(const_cast<char*>(arg.c_str()));
    arguments.push_back(nullptr);
    const std::string directory = workdir.string();
    std::vector<std::string> environment;
    std::vector<char*> environment_ptrs;
    if (minimal_env) {
        const char* path = std::getenv("PATH");
        environment = {std::string("PATH=") + (path ? path : "/usr/bin:/bin"),
                       "HOME=" + directory, "TMPDIR=" + directory};
        for (auto& entry : environment) environment_ptrs.push_back(entry.data());
        environment_ptrs.push_back(nullptr);
    }
    char** child_environment = minimal_env ? environment_ptrs.data() : environ;
    PosixFd input;
    long descriptor_bound = 0;
    if (minimal_env) {
        descriptor_bound = ::sysconf(_SC_OPEN_MAX);
        if (descriptor_bound < 0) return rr;
        input.value = ::open("/dev/null", O_RDONLY | O_CLOEXEC);
        if (input.value == -1 || !prepare_fd(input.value)) return rr;
    }
    int out_pipe[2], err_pipe[2];
    if (!capture_pipe(out_pipe)) return rr;
    if (!capture_pipe(err_pipe)) { ::close(out_pipe[0]); ::close(out_pipe[1]); return rr; }
    for (int fd : {out_pipe[0], err_pipe[0]}) {
        const int flags = ::fcntl(fd, F_GETFL, 0);
        if (flags == -1 || ::fcntl(fd, F_SETFL, flags | O_NONBLOCK) == -1) {
            for (int owned : {out_pipe[0], out_pipe[1], err_pipe[0], err_pipe[1]}) ::close(owned);
            return rr;
        }
    }
    const pid_t pid = ::fork();
    if (pid == -1) {
        for (int fd : {out_pipe[0], out_pipe[1], err_pipe[0], err_pipe[1]}) ::close(fd);
        return rr;
    }
    if (pid == 0) {
        if (::setpgid(0, 0) != 0 || ::chdir(directory.c_str()) != 0 ||
            ::dup2(out_pipe[1], STDOUT_FILENO) == -1 ||
            ::dup2(err_pipe[1], STDERR_FILENO) == -1 ||
            (minimal_env && ::dup2(input.value, STDIN_FILENO) == -1)) _exit(126);
        for (int fd : {out_pipe[0], out_pipe[1], err_pipe[0], err_pipe[1]}) ::close(fd);
        if (minimal_env) {
            bool closed = false;
#if defined(__linux__) && defined(SYS_close_range)
            closed = ::syscall(SYS_close_range, 3U, ~0U, 0U) == 0;
#endif
            if (!closed) {
                for (long fd = 3; fd < descriptor_bound; ++fd) ::close(static_cast<int>(fd));
            }
        }
        ::execve(executable.c_str(), arguments.data(), child_environment);
        _exit(127);
    }
    ::setpgid(pid, pid);  // Close the parent/child group-creation race before kill.
    ::close(out_pipe[1]);
    ::close(err_pipe[1]);
    auto terminate = [&] { ::kill(-pid, SIGKILL); };
    bool out_open = true, err_open = true, exited = false, stopped = false;
    bool have_status = false;
    int status = 0;
    char buffer[4096];
    while (!exited || out_open || err_open) {
        if (!exited) {
            const pid_t waited = ::waitpid(pid, &status, WNOHANG);
            if (waited == pid) { exited = true; have_status = true; }
            else if (waited == -1 && errno != EINTR) { exited = true; }
        }
        if (!stopped && expired()) {
            terminate();
            rr.timed_out = true;
            stopped = true;
            out_open = err_open = false;
        }
        struct pollfd fds[2]{};
        nfds_t count = 0;
        if (out_open) fds[count++] = {out_pipe[0], POLLIN, 0};
        if (err_open) fds[count++] = {err_pipe[0], POLLIN, 0};
        // EOF is not child exit. Keep polling waitpid under the same deadline,
        // never fall through to a blocking wait when a child closes its streams.
        const int polled = ::poll(fds, count, 10);
        if (polled < 0 && errno != EINTR) {
            terminate();
            stopped = true;
            out_open = err_open = false;
        }
        for (nfds_t i = 0; i < count && !stopped; ++i) {
            if (!(fds[i].revents & (POLLIN | POLLHUP | POLLERR | POLLNVAL))) continue;
            const ssize_t received = ::read(fds[i].fd, buffer, sizeof(buffer));
            if (received > 0) {
                auto& text = fds[i].fd == out_pipe[0] ? rr.stdout_text : rr.stderr_text;
                if (!budget.append(text, buffer, static_cast<size_t>(received))) {
                    terminate();
                    stopped = true;
                    out_open = err_open = false;
                }
            } else if (received == 0 || (received < 0 && errno != EAGAIN && errno != EINTR)) {
                if (fds[i].fd == out_pipe[0]) out_open = false;
                else err_open = false;
            }
        }
    }
    ::close(out_pipe[0]);
    ::close(err_pipe[0]);
    if (!rr.timed_out && !rr.output_limit_exceeded && have_status && !stopped) {
        if (WIFEXITED(status)) rr.returncode = WEXITSTATUS(status);
        else if (WIFSIGNALED(status)) rr.returncode = 128 + WTERMSIG(status);
    }
#endif
    budget.finish();
    return rr;
}

}  // namespace chudware::firmware
