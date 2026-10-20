// Minimal size-rotated file logger for chudware-hostd. Logs live in the state dir
// (~/.local/share/Chudware/logs/hostd.log) — never the runtime dir, which is tmpfs.
// Rotation: rename-based (hostd.log -> hostd.log.1 -> .2), 5 MB x 3 generations;
// rename-not-overwrite per the project's live-file rule.
#pragma once

#include <cstdarg>
#include <cstdio>
#include <mutex>
#include <string>

namespace hostd {

class Log {
public:
    // Open (and rotate if oversized) <dir>/hostd.log. Safe to call once at boot.
    bool open(const std::string& dir);

    // printf-style, one line per call, prefixed with an ISO-8601 timestamp.
#if defined(__GNUC__) || defined(__clang__)
    void line(const char* fmt, ...) __attribute__((format(printf, 2, 3)));
#else
    void line(const char* fmt, ...);  // MSVC: no format attribute equivalent here
#endif

private:
    void rotateIfNeeded();
    std::mutex mtx_;
    std::string path_;
    FILE* f_ = nullptr;
};

Log& log();  // process singleton

}  // namespace hostd
