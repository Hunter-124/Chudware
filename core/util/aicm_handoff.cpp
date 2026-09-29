// Implementation of AICM shared-memory handoff. See aicm_handoff.h.
#include "util/aicm_handoff.h"

#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <memory>
#include <mutex>
#include <string>
#include <unordered_map>
#include <vector>

#ifdef __EMSCRIPTEN__
// Browser never maps POSIX shm — loadAicmFile fetches via /files/aicm.
#elif defined(_WIN32)
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#else
#include <cerrno>
#include <fcntl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>
#endif

namespace chudware {
namespace aicm_handoff {
namespace {

constexpr const char* kUriPrefix = "aicm-shm://";

std::mutex g_mu;

struct CacheEntry {
    std::shared_ptr<const std::vector<uint8_t>> bytes;
    std::string uri;
};
std::unordered_map<std::string, CacheEntry> g_cache;

#ifdef _WIN32
// Keep named file-mapping handles alive until release() (last handle closes the object).
std::unordered_map<std::string, HANDLE> g_win_keepers;
#endif

struct MapImpl {
    enum Kind { Shm, Heap };
    Kind kind = Heap;
#ifdef _WIN32
    HANDLE map = nullptr;
    void* view = nullptr;
#else
    void* view = nullptr;
    size_t map_size = 0;
#endif
    std::vector<uint8_t> heap;
    std::shared_ptr<const std::vector<uint8_t>> retained;
};

std::string sanitize(const std::string& s) {
    std::string out;
    out.reserve(s.size());
    for (unsigned char c : s) {
        if ((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
            (c >= '0' && c <= '9') || c == '_' || c == '-' || c == '.') {
            out.push_back(static_cast<char>(c));
        } else {
            out.push_back('_');
        }
        if (out.size() >= 180) break;
    }
    if (out.empty()) out = "x";
    return out;
}

std::string uri_for_name(const std::string& name) { return std::string(kUriPrefix) + name; }

std::string name_from_uri(const std::string& uri) {
    if (uri.rfind(kUriPrefix, 0) != 0) return {};
    return uri.substr(std::strlen(kUriPrefix));
}

std::string publish_file(const std::string& key, const uint8_t* data, size_t n) {
    std::error_code ec;
    std::filesystem::path dir = std::filesystem::temp_directory_path(ec) / "chudware_cpp_docs";
    std::filesystem::create_directories(dir, ec);
    std::filesystem::path target = dir / (sanitize(key) + ".aicm");
    std::ofstream f(target, std::ios::binary | std::ios::trunc);
    if (!f) return {};
    if (n) f.write(reinterpret_cast<const char*>(data), static_cast<std::streamsize>(n));
    f.close();
    if (!f) return {};
    return std::filesystem::absolute(target).string();
}

#ifdef _WIN32
std::string shm_object_name(const std::string& key) { return "Local\\chudware_" + sanitize(key); }

std::string publish_shm(const std::string& key, const uint8_t* data, size_t n) {
    const std::string name = shm_object_name(key);
    const size_t total = 8 + n;
    HANDLE h = CreateFileMappingA(INVALID_HANDLE_VALUE, nullptr, PAGE_READWRITE,
                                  static_cast<DWORD>((total >> 32) & 0xffffffffu),
                                  static_cast<DWORD>(total & 0xffffffffu), name.c_str());
    if (!h) return {};
    void* view = MapViewOfFile(h, FILE_MAP_WRITE, 0, 0, total);
    if (!view) {
        CloseHandle(h);
        return {};
    }
    auto* p = static_cast<uint8_t*>(view);
    const uint64_t sz = static_cast<uint64_t>(n);
    std::memcpy(p, &sz, 8);
    if (n) std::memcpy(p + 8, data, n);
    UnmapViewOfFile(view);
    {
        std::lock_guard<std::mutex> lk(g_mu);
        auto it = g_win_keepers.find(name);
        if (it != g_win_keepers.end()) {
            CloseHandle(it->second);
            it->second = h;
        } else {
            g_win_keepers.emplace(name, h);
        }
    }
    return uri_for_name(name);
}

bool map_shm(const std::string& name, Mapping& out, std::string& err) {
    HANDLE h = OpenFileMappingA(FILE_MAP_READ, FALSE, name.c_str());
    if (!h) {
        err = "OpenFileMapping failed for " + name;
        return false;
    }
    void* view = MapViewOfFile(h, FILE_MAP_READ, 0, 0, 0);
    if (!view) {
        CloseHandle(h);
        err = "MapViewOfFile failed for " + name;
        return false;
    }
    uint64_t sz = 0;
    std::memcpy(&sz, view, 8);
    auto* impl = new MapImpl;
    impl->kind = MapImpl::Shm;
    impl->map = h;
    impl->view = view;
    out.impl = impl;
    out.data = static_cast<const uint8_t*>(view) + 8;
    out.size = static_cast<size_t>(sz);
    return true;
}

void unlink_shm_name(const std::string& name) {
    std::lock_guard<std::mutex> lk(g_mu);
    auto it = g_win_keepers.find(name);
    if (it != g_win_keepers.end()) {
        CloseHandle(it->second);
        g_win_keepers.erase(it);
    }
}
#elif defined(__EMSCRIPTEN__)
// Browser client: no POSIX shm. publish falls back to publish_file (MEMFS);
// consumers fetch aicm-shm:// via GET /files/aicm?shm=… (see loadAicmFile).
std::string shm_object_name(const std::string& key) { return "/chudware_" + sanitize(key); }

std::string publish_shm(const std::string& /*key*/, const uint8_t* /*data*/, size_t /*n*/) {
    return {};
}

bool map_shm(const std::string& name, Mapping& /*out*/, std::string& err) {
    err = "aicm-shm unavailable under emscripten (" + name + "); use HTTP /files/aicm";
    return false;
}

void unlink_shm_name(const std::string& /*name*/) {}
#else  // POSIX
std::string shm_object_name(const std::string& key) { return "/chudware_" + sanitize(key); }

std::string publish_shm(const std::string& key, const uint8_t* data, size_t n) {
    const std::string name = shm_object_name(key);
    shm_unlink(name.c_str());
    int fd = shm_open(name.c_str(), O_CREAT | O_RDWR | O_EXCL, 0600);
    if (fd < 0) {
        fd = shm_open(name.c_str(), O_RDWR, 0600);
        if (fd < 0) return {};
    }
    const size_t total = 8 + n;
    if (ftruncate(fd, static_cast<off_t>(total)) != 0) {
        close(fd);
        shm_unlink(name.c_str());
        return {};
    }
    void* view = mmap(nullptr, total, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    close(fd);
    if (view == MAP_FAILED) {
        shm_unlink(name.c_str());
        return {};
    }
    auto* p = static_cast<uint8_t*>(view);
    const uint64_t sz = static_cast<uint64_t>(n);
    std::memcpy(p, &sz, 8);
    if (n) std::memcpy(p + 8, data, n);
    munmap(view, total);
    return uri_for_name(name);
}

bool map_shm(const std::string& name, Mapping& out, std::string& err) {
    int fd = shm_open(name.c_str(), O_RDONLY, 0600);
    if (fd < 0) {
        err = std::string("shm_open failed: ") + std::strerror(errno) + " (" + name + ")";
        return false;
    }
    struct stat st {};
    if (fstat(fd, &st) != 0 || st.st_size < 8) {
        close(fd);
        err = "shm fstat failed or empty: " + name;
        return false;
    }
    const size_t map_size = static_cast<size_t>(st.st_size);
    void* view = mmap(nullptr, map_size, PROT_READ, MAP_SHARED, fd, 0);
    close(fd);
    if (view == MAP_FAILED) {
        err = std::string("mmap failed: ") + std::strerror(errno);
        return false;
    }
    uint64_t sz = 0;
    std::memcpy(&sz, view, 8);
    if (8 + sz > map_size) {
        munmap(view, map_size);
        err = "shm payload size exceeds mapping";
        return false;
    }
    auto* impl = new MapImpl;
    impl->kind = MapImpl::Shm;
    impl->view = view;
    impl->map_size = map_size;
    out.impl = impl;
    out.data = static_cast<const uint8_t*>(view) + 8;
    out.size = static_cast<size_t>(sz);
    return true;
}

void unlink_shm_name(const std::string& name) {
    if (!name.empty()) shm_unlink(name.c_str());
}
#endif

bool map_file(const std::string& path, Mapping& out, std::string& err) {
    std::ifstream f(std::filesystem::u8path(path), std::ios::binary);
    if (!f) {
        err = "cannot open " + path;
        return false;
    }
    f.seekg(0, std::ios::end);
    std::streamoff n = f.tellg();
    if (n < 0) {
        err = "cannot size " + path;
        return false;
    }
    f.seekg(0, std::ios::beg);
    auto* impl = new MapImpl;
    impl->kind = MapImpl::Heap;
    impl->heap.resize(static_cast<size_t>(n));
    if (n > 0) {
        f.read(reinterpret_cast<char*>(impl->heap.data()), n);
        if (!f) {
            delete impl;
            err = "short read on " + path;
            return false;
        }
    }
    out.impl = impl;
    out.data = impl->heap.data();
    out.size = impl->heap.size();
    return true;
}

}  // namespace

bool is_shm_uri(const std::string& uri) { return uri.rfind(kUriPrefix, 0) == 0; }

// Doc ids are per-process sequential ("doc-cpp00000001"), so a bare doc_id+rev name is shared
// by every engine on the box: the user's hostd, an in-process MCP/training engine and parallel
// render workers all published /dev/shm/chudware_doc-cpp00000001_r0, and each publish
// shm_unlink+ftruncate'd the other process's live object (SIGBUS in the reader, or a silently
// foreign mesh). The publishing process id scopes the name; readers only ever use the URI.
static std::string doc_key_prefix(const std::string& doc_id) {
#ifdef _WIN32
    const unsigned long pid = static_cast<unsigned long>(::GetCurrentProcessId());
#elif defined(__EMSCRIPTEN__)
    const unsigned long pid = 0;
#else
    const unsigned long pid = static_cast<unsigned long>(::getpid());
#endif
    return "p" + std::to_string(pid) + "_" + sanitize(doc_id) + "_r";
}

std::string key_for(const std::string& doc_id, long revision) {
    return doc_key_prefix(doc_id) + std::to_string(revision);
}

void release_all() {
    std::vector<std::string> keys;
    {
        std::lock_guard<std::mutex> lk(g_mu);
        keys.reserve(g_cache.size());
        for (const auto& kv : g_cache) keys.push_back(kv.first);
    }
    for (const auto& k : keys) release(k);
}

namespace {
void atexit_release_all() { release_all(); }
std::once_flag g_atexit_once;
void ensure_atexit_cleanup() {
    std::call_once(g_atexit_once, [] { std::atexit(atexit_release_all); });
}
}  // namespace

std::string publish(const std::string& key, const uint8_t* data, size_t n) {
    if (!data && n) return {};
    ensure_atexit_cleanup();
    std::string uri = publish_shm(key, data, n);
    if (uri.empty()) uri = publish_file(key, data, n);
    if (uri.empty()) return {};

    CacheEntry e;
    e.bytes = n ? std::make_shared<const std::vector<uint8_t>>(data, data + n)
                : std::make_shared<const std::vector<uint8_t>>();
    e.uri = uri;
    {
        std::lock_guard<std::mutex> lk(g_mu);
        g_cache[key] = std::move(e);
    }
    return uri;
}

bool map(const std::string& uri, Mapping& out, std::string& err) {
    out = Mapping{};
    if (uri.empty()) {
        err = "empty AICM uri";
        return false;
    }

    // Pin immutable producer bytes; replacement/release cannot invalidate a reader.
    {
        std::lock_guard<std::mutex> lk(g_mu);
        for (const auto& kv : g_cache) {
            if (kv.second.uri == uri) {
                auto* impl = new MapImpl;
                impl->kind = MapImpl::Heap;
                impl->retained = kv.second.bytes;
                out.impl = impl;
                out.data = impl->retained->data();
                out.size = impl->retained->size();
                return true;
            }
        }
    }

    if (is_shm_uri(uri)) {
        const std::string name = name_from_uri(uri);
        if (name.empty()) {
            err = "malformed aicm-shm uri";
            return false;
        }
        return map_shm(name, out, err);
    }
    return map_file(uri, out, err);
}

void unmap(Mapping& m) {
    if (!m.impl) {
        m = Mapping{};
        return;
    }
    auto* impl = static_cast<MapImpl*>(m.impl);
#ifdef _WIN32
    if (impl->kind == MapImpl::Shm) {
        if (impl->view) UnmapViewOfFile(impl->view);
        if (impl->map) CloseHandle(impl->map);
    }
#elif !defined(__EMSCRIPTEN__)
    if (impl->kind == MapImpl::Shm && impl->view) munmap(impl->view, impl->map_size);
#endif
    delete impl;
    m = Mapping{};
}

void release(const std::string& key) {
    std::string uri;
    {
        std::lock_guard<std::mutex> lk(g_mu);
        auto it = g_cache.find(key);
        if (it != g_cache.end()) {
            uri = it->second.uri;
            g_cache.erase(it);
        }
    }
    if (!uri.empty() && is_shm_uri(uri)) {
        unlink_shm_name(name_from_uri(uri));
    } else if (!uri.empty()) {
        std::error_code ec;
        std::filesystem::remove(uri, ec);
    } else {
        // Best-effort: unlink by constructed name even without a cache hit.
        unlink_shm_name(shm_object_name(key));
    }
}

void release_older_than(const std::string& doc_id, long keep_rev) {
    const std::string prefix = doc_key_prefix(doc_id);
    std::vector<std::string> drop;
    {
        std::lock_guard<std::mutex> lk(g_mu);
        for (const auto& kv : g_cache) {
            const std::string& k = kv.first;
            if (k.rfind(prefix, 0) != 0) continue;
            try {
                long rev = std::stol(k.substr(prefix.size()));
                if (rev < keep_rev) drop.push_back(k);
            } catch (...) {
            }
        }
    }
    for (const auto& k : drop) release(k);
}

void release_doc(const std::string& doc_id) {
    const std::string prefix = doc_key_prefix(doc_id);
    std::vector<std::string> drop;
    {
        std::lock_guard<std::mutex> lk(g_mu);
        for (const auto& kv : g_cache) {
            if (kv.first.rfind(prefix, 0) == 0) drop.push_back(kv.first);
        }
    }
    for (const auto& k : drop) release(k);
}

}  // namespace aicm_handoff
}  // namespace chudware
