// See util/platform_paths.h.
#include "util/platform_paths.h"

#if defined(__EMSCRIPTEN__)
// Browser: no executable image, no process table. Both queries answer "unknown".
#elif defined(_WIN32)
// Guarded: the CEF helper target already defines this on the command line and
// compiles /W4 /WX, so a bare redefinition is a hard error there (C4005).
#  ifndef WIN32_LEAN_AND_MEAN
#    define WIN32_LEAN_AND_MEAN
#  endif
#  include <windows.h>
#else
#  include <cerrno>
#  include <climits>
#  include <csignal>
#  include <cstdlib>
#  include <unistd.h>
#  if defined(__APPLE__)
#    include <mach-o/dyld.h>
#    include <sys/sysctl.h>
#    include <vector>
#  else
#    include <cstdio>
#    include <cstring>
#  endif
#endif

namespace chudware::platform {

std::string executablePath() {
#if defined(__EMSCRIPTEN__)
    return std::string();
#elif defined(_WIN32)
    // GetModuleFileNameW truncates instead of failing when the buffer is short
    // (and pre-Win10 does not even set ERROR_INSUFFICIENT_BUFFER), so grow
    // until the returned length is strictly inside the buffer.
    std::wstring w(MAX_PATH, L'\0');
    for (;;) {
        const DWORD n = ::GetModuleFileNameW(nullptr, w.data(), (DWORD)w.size());
        if (n == 0) return std::string();
        if (n < w.size()) {
            w.resize(n);
            break;
        }
        if (w.size() >= 32768) return std::string();  // longest NT path
        w.resize(w.size() * 2);
    }
    const int len = ::WideCharToMultiByte(CP_UTF8, 0, w.c_str(), (int)w.size(), nullptr, 0,
                                          nullptr, nullptr);
    if (len <= 0) return std::string();
    std::string out((size_t)len, '\0');
    ::WideCharToMultiByte(CP_UTF8, 0, w.c_str(), (int)w.size(), out.data(), len, nullptr,
                          nullptr);
    return out;
#elif defined(__APPLE__)
    uint32_t size = 0;
    ::_NSGetExecutablePath(nullptr, &size);  // size query; the -1 return is expected
    if (size == 0) return std::string();
    std::vector<char> raw(size);
    if (::_NSGetExecutablePath(raw.data(), &size) != 0) return std::string();
    // The raw value is whatever exec was handed: possibly relative, possibly
    // through a symlink. Callers compare it against staged install layouts, so
    // canonicalise it the way /proc/self/exe already is on Linux.
    char resolved[PATH_MAX];
    if (::realpath(raw.data(), resolved)) return std::string(resolved);
    return std::string(raw.data());
#else
    char buf[4096];
    const ssize_t n = ::readlink("/proc/self/exe", buf, sizeof buf - 1);
    if (n <= 0) return std::string();
    buf[n] = '\0';
    return std::string(buf, (size_t)n);
#endif
}

bool processAlive(long pid) {
    if (pid <= 1) return false;
#if defined(__EMSCRIPTEN__)
    return false;
#elif defined(_WIN32)
    const HANDLE h = ::OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, (DWORD)pid);
    if (!h) return false;
    DWORD code = 0;
    const bool alive = ::GetExitCodeProcess(h, &code) && code == STILL_ACTIVE;
    ::CloseHandle(h);
    return alive;
#else
    // ESRCH = gone. EPERM = alive but someone else's.
    if (::kill((pid_t)pid, 0) != 0 && errno != EPERM) return false;
#  if defined(__linux__)
    char path[64];
    std::snprintf(path, sizeof path, "/proc/%ld/stat", pid);
    std::FILE* f = std::fopen(path, "r");
    if (!f) return false;
    char buf[512];
    const bool read = std::fgets(buf, sizeof buf, f) != nullptr;
    std::fclose(f);
    if (!read) return true;
    // "pid (comm) state ..." — comm may contain spaces and parens, so the state
    // is the first token after the LAST ')'.
    const char* rp = std::strrchr(buf, ')');
    if (!rp || !rp[1]) return true;
    return rp[2] != 'Z';
#  elif defined(__APPLE__)
    int mib[4] = {CTL_KERN, KERN_PROC, KERN_PROC_PID, (int)pid};
    struct kinfo_proc kp;
    size_t len = sizeof kp;
    if (::sysctl(mib, 4, &kp, &len, nullptr, 0) != 0 || len == 0) return false;
    return kp.kp_proc.p_stat != SZOMB;
#  else
    return true;  // no portable zombie query; kill(2) said it exists
#  endif
#endif
}

std::filesystem::path developerSourceRoot() {
#if defined(__EMSCRIPTEN__)
    return {};
#else
    std::error_code ec;
    for (auto root : {std::filesystem::u8path(executablePath()).parent_path(),
                      std::filesystem::current_path(ec)}) {
        while (!root.empty()) {
            if (std::filesystem::is_regular_file(root / "services" / "worker_main.py", ec))
                return root;
            const auto parent = root.parent_path();
            if (parent == root) break;
            root = parent;
        }
    }
    return {};
#endif
}

}  // namespace chudware::platform
