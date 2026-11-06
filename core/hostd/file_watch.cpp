#include "file_watch.h"

#include <chrono>
#include <cstdint>
#include <exception>
#include <filesystem>
#include <map>
#include <set>
#include <utility>

#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#else
#include <sys/stat.h>
#endif

#include "hostd_log.h"

namespace hostd {
using nlohmann::json;

FileWatch::FileWatch(List list, Changed changed)
    : list_(std::move(list)), changed_(std::move(changed)), thread_([this] { run(); }) {}

FileWatch::~FileWatch() {
    {
        std::lock_guard<std::mutex> lock(mutex_);
        stopping_ = true;
    }
    wake_.notify_one();
    if (thread_.joinable()) thread_.join();
}

json FileWatch::stamp(const std::string& path) {
    const auto native = std::filesystem::u8path(path);
#ifdef _WIN32
    HANDLE handle = CreateFileW(native.c_str(), FILE_READ_ATTRIBUTES,
                                FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                                nullptr, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (handle == INVALID_HANDLE_VALUE) return nullptr;
    BY_HANDLE_FILE_INFORMATION info{};
    const bool ok = GetFileInformationByHandle(handle, &info) != 0;
    CloseHandle(handle);
    if (!ok || (info.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY)) return nullptr;
    const auto size = (static_cast<std::uint64_t>(info.nFileSizeHigh) << 32) | info.nFileSizeLow;
    const auto ticks = (static_cast<std::uint64_t>(info.ftLastWriteTime.dwHighDateTime) << 32) |
                       info.ftLastWriteTime.dwLowDateTime;
    const auto mtime = static_cast<long long>(ticks - 116444736000000000ULL) * 100;
    const auto ino = (static_cast<std::uint64_t>(info.nFileIndexHigh) << 32) | info.nFileIndexLow;
#else
    struct stat info{};
    if (::stat(native.c_str(), &info) != 0 || !S_ISREG(info.st_mode)) return nullptr;
    const auto size = static_cast<std::uint64_t>(info.st_size);
#if defined(__APPLE__)
    const auto mtime = static_cast<long long>(info.st_mtimespec.tv_sec) * 1000000000LL +
                       info.st_mtimespec.tv_nsec;
#else
    const auto mtime = static_cast<long long>(info.st_mtim.tv_sec) * 1000000000LL +
                       info.st_mtim.tv_nsec;
#endif
    const auto ino = static_cast<std::uint64_t>(info.st_ino);
#endif
    return json{{"size", size}, {"mtime_ns", mtime}, {"ino", ino}};
}

void FileWatch::run() {
    struct Candidate {
        std::string path;
        json stamp;
        unsigned polls = 0;
        bool conflictNotified = false;
    };
    std::map<std::string, Candidate> candidates;
    std::unique_lock<std::mutex> lock(mutex_);
    while (!wake_.wait_for(lock, std::chrono::seconds(2), [this] { return stopping_; })) {
        lock.unlock();
        try {
            const json listing = list_();
            if (listing.is_object() && listing.contains("docs") && listing["docs"].is_array()) {
                std::set<std::string> seen;
                for (const auto& row : listing["docs"]) {
                    if (!row.is_object() || !row.contains("doc_id") || !row["doc_id"].is_string() ||
                        !row.contains("path") || !row["path"].is_string() ||
                        !row.contains("file_stamp") || !row["file_stamp"].is_object() ||
                        row.value("busy", false)) continue;
                    const std::string id = row["doc_id"].get<std::string>();
                    const std::string path = row["path"].get<std::string>();
                    if (id.empty() || path.empty()) continue;
                    seen.insert(id);
                    const json current = stamp(path);
                    if (!current.is_object() || current == row["file_stamp"]) {
                        candidates.erase(id);
                        continue;
                    }
                    Candidate& candidate = candidates[id];
                    if (candidate.path != path || candidate.stamp != current) {
                        candidate = Candidate{path, current, 1, false};
                        continue;
                    }
                    ++candidate.polls;
                    if (candidate.polls < 2 ||
                        (candidate.conflictNotified && row.value("human_dirty", false))) continue;
                    const Outcome outcome = changed_(id, path, current);
                    if (outcome == Outcome::Reloaded) candidates.erase(id);
                    else if (outcome == Outcome::Conflict) candidate.conflictNotified = true;
                }
                for (auto it = candidates.begin(); it != candidates.end();) {
                    if (!seen.count(it->first)) it = candidates.erase(it);
                    else ++it;
                }
            }
        } catch (const std::exception& e) {
            log().line("file watch: poll failed: %s", e.what());
        } catch (...) {
            log().line("file watch: poll failed with unknown error");
        }
        lock.lock();
    }
}

}  // namespace hostd
