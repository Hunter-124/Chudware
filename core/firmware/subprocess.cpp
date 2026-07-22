#include "firmware/subprocess.h"

#include <cstdlib>
#include <filesystem>

#ifdef _WIN32
#include <windows.h>
#else
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>
#include <chrono>
#include <cstring>
#endif

namespace aicad::firmware {

namespace {
// Python's subprocess.run(text=True) applies universal-newline translation
// (\r\n and lone \r both become \n) to captured stdout/stderr. Every Python
// firmware wrapper relied on that, so parity requires the same normalization.
void normalize_newlines(std::string& s) {
    if (s.find('\r') == std::string::npos) return;
    std::string out;
    out.reserve(s.size());
    for (size_t i = 0; i < s.size(); ++i) {
        char c = s[i];
        if (c == '\r') {
            if (i + 1 < s.size() && s[i + 1] == '\n') continue;  // \r\n -> the \n
            out += '\n';                                          // lone \r -> \n
        } else {
            out += c;
        }
    }
    s = std::move(out);
}
}  // namespace

std::string which(const std::string& name) {
    if (name.empty()) return "";
    namespace fs = std::filesystem;
    // Absolute/relative path with a directory component: check it directly.
    if (name.find('/') != std::string::npos
#ifdef _WIN32
        || name.find('\\') != std::string::npos
#endif
    ) {
        std::error_code ec;
        if (fs::exists(name, ec)) return name;
        return "";
    }
    const char* path_env = std::getenv("PATH");
    if (!path_env) return "";
    std::string paths(path_env);
#ifdef _WIN32
    const char sep = ';';
    // PATHEXT governs executable extensions on Windows.
    std::vector<std::string> exts = {".exe", ".bat", ".cmd", ".com", ""};
#else
    const char sep = ':';
#endif
    size_t pos = 0;
    while (pos <= paths.size()) {
        size_t next = paths.find(sep, pos);
        std::string dir = paths.substr(pos, next == std::string::npos ? std::string::npos
                                                                      : next - pos);
        pos = next == std::string::npos ? paths.size() + 1 : next + 1;
        if (dir.empty()) continue;
#ifdef _WIN32
        for (const auto& ext : exts) {
            fs::path cand = fs::path(dir) / (name + ext);
            std::error_code ec;
            if (fs::exists(cand, ec) && !fs::is_directory(cand, ec)) return cand.string();
        }
#else
        fs::path cand = fs::path(dir) / name;
        std::error_code ec;
        if (fs::exists(cand, ec) && !fs::is_directory(cand, ec) &&
            ::access(cand.c_str(), X_OK) == 0) {
            return cand.string();
        }
#endif
    }
    return "";
}

#ifdef _WIN32

RunResult run_capture(const std::vector<std::string>& argv, const std::string& cwd,
                      double timeout_s) {
    RunResult rr;
    if (argv.empty()) return rr;
    if (which(argv[0]).empty()) {
        rr.not_found = true;
        return rr;
    }
    // Build the command line (naive quoting: wrap each arg in double quotes).
    std::string cmdline;
    for (const auto& a : argv) {
        if (!cmdline.empty()) cmdline += ' ';
        cmdline += '"';
        for (char c : a) {
            if (c == '"') cmdline += '\\';
            cmdline += c;
        }
        cmdline += '"';
    }

    SECURITY_ATTRIBUTES sa{};
    sa.nLength = sizeof(sa);
    sa.bInheritHandle = TRUE;
    HANDLE outRead = nullptr, outWrite = nullptr;
    HANDLE errRead = nullptr, errWrite = nullptr;
    if (!CreatePipe(&outRead, &outWrite, &sa, 0) || !CreatePipe(&errRead, &errWrite, &sa, 0)) {
        return rr;
    }
    SetHandleInformation(outRead, HANDLE_FLAG_INHERIT, 0);
    SetHandleInformation(errRead, HANDLE_FLAG_INHERIT, 0);

    STARTUPINFOA si{};
    si.cb = sizeof(si);
    si.dwFlags = STARTF_USESTDHANDLES;
    si.hStdOutput = outWrite;
    si.hStdError = errWrite;
    si.hStdInput = GetStdHandle(STD_INPUT_HANDLE);
    PROCESS_INFORMATION pi{};
    std::string mutable_cmd = cmdline;
    const char* cwd_ptr = cwd.empty() ? nullptr : cwd.c_str();
    if (!CreateProcessA(nullptr, mutable_cmd.data(), nullptr, nullptr, TRUE, 0, nullptr,
                        cwd_ptr, &si, &pi)) {
        CloseHandle(outRead);
        CloseHandle(outWrite);
        CloseHandle(errRead);
        CloseHandle(errWrite);
        rr.not_found = true;  // best-effort mapping; caller preflights with which()
        return rr;
    }
    CloseHandle(outWrite);
    CloseHandle(errWrite);

    DWORD wait_ms = timeout_s > 0 ? static_cast<DWORD>(timeout_s * 1000.0) : INFINITE;
    DWORD wr = WaitForSingleObject(pi.hProcess, wait_ms);
    if (wr == WAIT_TIMEOUT) {
        TerminateProcess(pi.hProcess, 1);
        rr.timed_out = true;
        rr.returncode = -1;
    }
    auto drain = [](HANDLE h, std::string& out) {
        char buf[4096];
        DWORD n = 0;
        while (ReadFile(h, buf, sizeof(buf), &n, nullptr) && n > 0) out.append(buf, n);
    };
    drain(outRead, rr.stdout_text);
    drain(errRead, rr.stderr_text);
    if (!rr.timed_out) {
        DWORD code = 0;
        GetExitCodeProcess(pi.hProcess, &code);
        rr.returncode = static_cast<int>(code);
    }
    CloseHandle(pi.hProcess);
    CloseHandle(pi.hThread);
    CloseHandle(outRead);
    CloseHandle(errRead);
    normalize_newlines(rr.stdout_text);
    normalize_newlines(rr.stderr_text);
    return rr;
}

#else  // POSIX

RunResult run_capture(const std::vector<std::string>& argv, const std::string& cwd,
                      double timeout_s) {
    RunResult rr;
    if (argv.empty()) return rr;
    if (which(argv[0]).empty()) {
        rr.not_found = true;
        return rr;
    }

    int out_pipe[2], err_pipe[2];
    if (::pipe(out_pipe) != 0 || ::pipe(err_pipe) != 0) return rr;

    pid_t pid = ::fork();
    if (pid == -1) {
        ::close(out_pipe[0]); ::close(out_pipe[1]);
        ::close(err_pipe[0]); ::close(err_pipe[1]);
        return rr;
    }
    if (pid == 0) {
        // Child: wire pipes, chdir, exec.
        ::dup2(out_pipe[1], STDOUT_FILENO);
        ::dup2(err_pipe[1], STDERR_FILENO);
        ::close(out_pipe[0]); ::close(out_pipe[1]);
        ::close(err_pipe[0]); ::close(err_pipe[1]);
        if (!cwd.empty()) ::chdir(cwd.c_str());
        std::vector<char*> cargv;
        cargv.reserve(argv.size() + 1);
        for (const auto& a : argv) cargv.push_back(const_cast<char*>(a.c_str()));
        cargv.push_back(nullptr);
        ::execvp(cargv[0], cargv.data());
        _exit(127);  // exec failed
    }

    ::close(out_pipe[1]);
    ::close(err_pipe[1]);
    // Non-blocking reads so we can enforce the wall-clock timeout.
    for (int fd : {out_pipe[0], err_pipe[0]}) {
        int flags = ::fcntl(fd, F_GETFL, 0);
        ::fcntl(fd, F_SETFL, flags | O_NONBLOCK);
    }

    auto deadline = std::chrono::steady_clock::now() +
                    std::chrono::milliseconds(static_cast<long long>(timeout_s * 1000.0));
    bool out_open = true, err_open = true;
    bool exited = false;
    int status = 0;
    char buf[4096];
    while (out_open || err_open) {
        if (!exited) {
            pid_t w = ::waitpid(pid, &status, WNOHANG);
            if (w == pid) exited = true;
        }
        if (!exited && std::chrono::steady_clock::now() > deadline) {
            ::kill(pid, SIGKILL);
            ::waitpid(pid, &status, 0);
            rr.timed_out = true;
            rr.returncode = -1;
            exited = true;
        }
        struct pollfd pfds[2] = {{out_pipe[0], POLLIN, 0}, {err_pipe[0], POLLIN, 0}};
        int nfd = 0;
        if (out_open) pfds[nfd++].fd = out_pipe[0];
        if (err_open) pfds[nfd++].fd = err_pipe[0];
        int pr = ::poll(pfds, nfd, exited ? 0 : 50);
        if (pr < 0 && errno != EINTR) break;
        for (int i = 0; i < nfd; ++i) {
            if (!(pfds[i].revents & (POLLIN | POLLHUP | POLLERR))) continue;
            ssize_t n = ::read(pfds[i].fd, buf, sizeof(buf));
            if (n > 0) {
                if (pfds[i].fd == out_pipe[0]) rr.stdout_text.append(buf, n);
                else rr.stderr_text.append(buf, n);
            } else if (n == 0 || (n < 0 && errno != EAGAIN && errno != EINTR)) {
                if (pfds[i].fd == out_pipe[0]) out_open = false;
                else err_open = false;
            }
        }
        if (exited) {
            // Final drain after child exit.
            for (int fd : {out_pipe[0], err_pipe[0]}) {
                ssize_t n;
                while ((n = ::read(fd, buf, sizeof(buf))) > 0) {
                    if (fd == out_pipe[0]) rr.stdout_text.append(buf, n);
                    else rr.stderr_text.append(buf, n);
                }
            }
            out_open = err_open = false;
        }
    }
    ::close(out_pipe[0]);
    ::close(err_pipe[0]);
    if (!exited) ::waitpid(pid, &status, 0);
    if (!rr.timed_out) {
        if (WIFEXITED(status)) rr.returncode = WEXITSTATUS(status);
        else if (WIFSIGNALED(status)) rr.returncode = 128 + WTERMSIG(status);
    }
    normalize_newlines(rr.stdout_text);
    normalize_newlines(rr.stderr_text);
    return rr;
}

#endif

}  // namespace aicad::firmware
