// Opt-in, metadata-only CEF/vendor-wizard diagnostics.
// CHUDWARE_CEF_TRACE=1 retains one private log, at most 1 MiB total and 96 bytes
// per event. There are no rotated copies: once full, writes stop until the user
// removes the log while the app is stopped. Page console text is never retained.
#pragma once
#ifndef __EMSCRIPTEN__
#include "ipc_private.h"
#endif

#include <cstring>
#include <cstdlib>
#include <mutex>
#include <string>
#include <string_view>

#if !defined(_WIN32) && !defined(__EMSCRIPTEN__)
#include <sys/file.h>
#endif

namespace ui::cef {

inline constexpr size_t kTraceMaxBytes = 1024 * 1024;
inline constexpr size_t kTraceMaxLineBytes = 96;

inline bool trace_enabled() {
#ifdef __EMSCRIPTEN__
    return false; // Native diagnostics do not write into the browser filesystem.
#else
    static const bool enabled = [] {
        const char* value = std::getenv("CHUDWARE_CEF_TRACE");
        return value && std::strcmp(value, "1") == 0;
    }();
    return enabled;
#endif
}

inline std::string trace_file_path() {
    if (const char* x = std::getenv("XDG_DATA_HOME"); x && x[0])
        return std::string(x) + "/chudware/chudware_cef_trace.log";
    if (const char* h = std::getenv("HOME"); h && h[0])
        return std::string(h) + "/.local/share/chudware/chudware_cef_trace.log";
    return {}; // Never fall back to a shared, writable /tmp name.
}

inline void trace_file_line(std::string_view line) {
#ifdef __EMSCRIPTEN__
    (void)line;
#else
    if (!trace_enabled()) return;

    // Only native event names survive. URLs, paths, cookie/token values, errors
    // and page-controlled "[RC]" strings are not diagnostic provenance.
    constexpr std::string_view prefixes[] = {"[cef-trace] ", "[wizard] ", "[cef-netlog] "};
    std::string_view prefix;
    for (const auto candidate : prefixes) {
        if (line.substr(0, candidate.size()) == candidate) { prefix = candidate; break; }
    }
    if (prefix.empty()) return;
    char event[kTraceMaxLineBytes];
    std::memcpy(event, prefix.data(), prefix.size());
    size_t length = prefix.size();
    size_t offset = prefix.size();
    while (offset < line.size() && length + 1 < sizeof event) {
        const unsigned char c = line[offset++];
        if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
              (c >= '0' && c <= '9') || c == '_' || c == '-')) break;
        event[length++] = static_cast<char>(c);
    }
    if (length == prefix.size()) return;
    event[length++] = '\n';

    static const std::string path = trace_file_path();
    if (path.empty()) return;
    static std::mutex mutex;
    std::lock_guard<std::mutex> guard(mutex);
    const auto target = std::filesystem::u8path(path);
    if (!target.is_absolute()) return;
    static const std::string parent = path.substr(0, path.find_last_of("/\\"));
    if (!chudware_ipc::privateDir(parent)) return;

#ifdef _WIN32
    // Pin every ancestor without delete sharing so a directory cannot be
    // replaced by a writable reparse alias while the file is opened/written.
    std::vector<HANDLE> directories;
    std::filesystem::path current;
    bool safe = true;
    for (const auto& part : target.parent_path()) {
        current /= part;
        if (current == current.root_name()) continue;
        HANDLE dir = CreateFileW(current.c_str(), FILE_READ_ATTRIBUTES,
            FILE_SHARE_READ | FILE_SHARE_WRITE, nullptr, OPEN_EXISTING,
            FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT, nullptr);
        BY_HANDLE_FILE_INFORMATION info{};
        if (dir == INVALID_HANDLE_VALUE ||
            !GetFileInformationByHandle(dir, &info) ||
            !(info.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) ||
            (info.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT)) {
            if (dir != INVALID_HANDLE_VALUE) CloseHandle(dir);
            safe = false;
            break;
        }
        directories.push_back(dir);
    }
    // privateFile rejects hardlinks/reparse files and its Windows sharing mode
    // admits only readers. Another process cannot publish concurrently.
    FILE* stream = safe ? chudware_ipc::privateFile(path, true) : nullptr;
    if (stream) {
        const int fd = _fileno(stream);
        const HANDLE handle = reinterpret_cast<HANDLE>(_get_osfhandle(fd));
        LARGE_INTEGER size{};
        if (GetFileSizeEx(handle, &size) && size.QuadPart > static_cast<LONGLONG>(kTraceMaxBytes))
            _chsize_s(fd, kTraceMaxBytes); // Bound a pre-existing legacy trace too.
        if (GetFileSizeEx(handle, &size) && size.QuadPart >= 0 &&
            uint64_t(size.QuadPart) <= kTraceMaxBytes &&
            length <= kTraceMaxBytes - static_cast<size_t>(size.QuadPart)) {
            std::fwrite(event, 1, length, stream);
        }
        std::fclose(stream);
    }
    for (HANDLE dir : directories) CloseHandle(dir);
#else
    // privateDir creates private ancestors. Re-open with descriptor-relative,
    // no-follow traversal to close its pathname-to-file race.
    int directory = ::open("/", O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    if (directory < 0) return;
    for (const auto& part : target.parent_path().relative_path()) {
        const std::string name = part.string();
        if (name == ".") continue;
        const int next = name == ".." ? -1 :
            ::openat(directory, name.c_str(), O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
        ::close(directory);
        if (next < 0) return;
        directory = next;
    }
    const std::string name = target.filename().string();
    const int fd = ::openat(directory, name.c_str(),
        O_WRONLY | O_CREAT | O_APPEND | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC, 0600);
    ::close(directory);
    if (fd < 0) return;
    // Lock the retained inode itself; no rename/rotation can split the byte cap
    // between concurrent process writers. Threads also share the mutex above.
    if (::flock(fd, LOCK_EX | LOCK_NB) == 0) {
        struct stat info{};
        if (::fstat(fd, &info) == 0 && S_ISREG(info.st_mode) &&
            info.st_uid == ::geteuid() && info.st_nlink == 1 && info.st_size >= 0 &&
            ::fchmod(fd, 0600) == 0) {
            if (uint64_t(info.st_size) > kTraceMaxBytes) {
                ::ftruncate(fd, static_cast<off_t>(kTraceMaxBytes));
                ::flock(fd, LOCK_UN);
                ::close(fd);
                return;
            }
            if (length > kTraceMaxBytes - static_cast<size_t>(info.st_size)) {
                ::flock(fd, LOCK_UN);
                ::close(fd);
                return;
            }
            size_t written = 0;
            while (written < length) {
                const ssize_t n = ::write(fd, event + written, length - written);
                if (n < 0 && errno == EINTR) continue;
                if (n <= 0) break;
                written += static_cast<size_t>(n);
            }
        }
        ::flock(fd, LOCK_UN);
    }
    ::close(fd);
#endif
#endif
}

}  // namespace ui::cef
