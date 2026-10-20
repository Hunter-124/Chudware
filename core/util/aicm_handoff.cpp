// Implementation of AICM shared-memory handoff. See aicm_handoff.h.
#include "util/aicm_handoff.h"

#include <algorithm>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <limits>
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
std::string g_publication_root;

struct PublicationRoot {
    bool enabled = false;
    std::string path;
};

PublicationRoot publication_root() {
    std::lock_guard<std::mutex> lk(g_mu);
    if (!g_publication_root.empty()) return {true, g_publication_root};
    const char* state = std::getenv("CHUDWARE_CELL_STATE");
    if (!state) return {};
    return {true, *state ? std::string(state) + "/mesh" : std::string()};
}

struct CacheEntry {
    std::shared_ptr<const std::vector<uint8_t>> bytes;
    std::string uri;
    std::string private_root;
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

unsigned long process_id() {
#ifdef _WIN32
    return static_cast<unsigned long>(::GetCurrentProcessId());
#elif defined(__EMSCRIPTEN__)
    return 0;
#else
    return static_cast<unsigned long>(::getpid());
#endif
}

// Every named object and fallback file is scoped by the publishing process id. Callers key by
// per-process counters ("compile_<n>", "field_<n>", "overlay_<n>") and per-process sequential
// doc ids ("doc-cpp00000001"), so a bare key is shared by every engine on the box: the user's
// hostd, an in-process MCP/training engine, parallel render workers and, on Windows, every
// service-hosted CI runner (all services share session 0's Local\ namespace). Each publish
// replaced or reopened the other process's live object (SIGBUS in the reader, or a silently
// foreign mesh). Readers only ever use the returned URI, so the scope is invisible to them.
std::string object_key(const std::string& key) {
    return "p" + std::to_string(process_id()) + "_" + sanitize(key);
}

bool normalize_private_root(const std::string& root, std::string& normalized,
                            std::string& err) {
    if (root.empty() || root.find('\0') != std::string::npos) {
        err = "invalid AICM publication root";
        return false;
    }
    const auto path = std::filesystem::u8path(root);
    if (!path.is_absolute() || path == path.root_path()) {
        err = "AICM publication root must be an absolute directory";
        return false;
    }
    for (const auto& part : path) {
        if (part == "..") {
            err = "AICM publication root cannot contain traversal";
            return false;
        }
    }
    normalized = path.lexically_normal().string();
    while (normalized.size() > 1 && normalized.back() == '/') normalized.pop_back();
    if (normalized == "/") {
        err = "AICM publication root cannot be the filesystem root";
        return false;
    }
    return true;
}

bool private_filename(const std::string& name) {
    if (name.size() != 37 || name.compare(32, 5, ".aicm") != 0) return false;
    for (size_t i = 0; i < 32; ++i) {
        if (!((name[i] >= '0' && name[i] <= '9') ||
              (name[i] >= 'a' && name[i] <= 'f'))) return false;
    }
    return true;
}

#if !defined(_WIN32) && !defined(__EMSCRIPTEN__)
// Walk from / with no-follow directory descriptors: neither publication nor cleanup
// can be redirected through a symlink in the configured state path.
int open_private_root(const std::string& root, bool create, std::string& err) {
    int fd = ::open("/", O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    if (fd < 0) {
        err = "cannot open AICM filesystem root";
        return -1;
    }
    size_t start = 1;
    while (start < root.size()) {
        size_t end = root.find('/', start);
        if (end == std::string::npos) end = root.size();
        const std::string part = root.substr(start, end - start);
        bool created = false;
        int next = ::openat(fd, part.c_str(), O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
        if (next < 0 && errno == ENOENT && create && end == root.size()) {
            if (::mkdirat(fd, part.c_str(), 0700) == 0) created = true;
            else if (errno != EEXIST) {
                ::close(fd);
                err = "cannot create private AICM directory";
                return -1;
            }
            next = ::openat(fd, part.c_str(), O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
        }
        ::close(fd);
        if (next < 0) {
            err = "cannot open symlink-free AICM directory";
            return -1;
        }
        fd = next;
        if (created && ::fchmod(fd, 0700) != 0) {
            ::close(fd);
            err = "cannot protect private AICM directory";
            return -1;
        }
        start = end + 1;
    }
    struct stat st {};
    if (::fstat(fd, &st) != 0 || st.st_uid != ::geteuid() || (st.st_mode & 07777) != 0700) {
        ::close(fd);
        err = "AICM publication directory must be owned by this user and mode 0700";
        return -1;
    }
    return fd;
}

std::string publish_private_file(const std::string& root, const uint8_t* data, size_t n) {
    std::string err;
    const int dir = open_private_root(root, true, err);
    if (dir < 0) return {};
    std::string name;
    int fd = -1;
    for (int attempt = 0; attempt < 16; ++attempt) {
        uint8_t random[16];
        if (::getentropy(random, sizeof(random)) != 0) break;
        static constexpr char hex[] = "0123456789abcdef";
        name.resize(32);
        for (size_t i = 0; i < sizeof(random); ++i) {
            name[2 * i] = hex[random[i] >> 4];
            name[2 * i + 1] = hex[random[i] & 15];
        }
        name += ".aicm";
        fd = ::openat(dir, name.c_str(), O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0600);
        if (fd >= 0 || errno != EEXIST) break;
    }
    if (fd < 0) {
        ::close(dir);
        return {};
    }
    bool ok = ::fchmod(fd, 0600) == 0;
    size_t written = 0;
    while (ok && written < n) {
        const size_t chunk = std::min(n - written,
                                     static_cast<size_t>(std::numeric_limits<ssize_t>::max()));
        const ssize_t count = ::write(fd, data + written, chunk);
        if (count < 0 && errno == EINTR) continue;
        if (count <= 0) { ok = false; break; }
        written += static_cast<size_t>(count);
    }
    if (::close(fd) != 0) ok = false;
    if (!ok) ::unlinkat(dir, name.c_str(), 0);
    ::close(dir);
    // No URI is exposed until the complete exclusive file has been closed.
    return ok ? root + "/" + name : std::string();
}

void unlink_private_file(const std::string& root, const std::string& uri) {
    const std::string prefix = root + "/";
    if (uri.rfind(prefix, 0) != 0 || !private_filename(uri.substr(prefix.size()))) return;
    std::string err;
    const int dir = open_private_root(root, false, err);
    if (dir < 0) return;
    ::unlinkat(dir, uri.c_str() + prefix.size(), 0);
    ::close(dir);
}

bool map_private_file(const std::string& root, const std::string& uri,
                      Mapping& out, std::string& err) {
    const std::string prefix = root + "/";
    if (uri.rfind(prefix, 0) != 0 || !private_filename(uri.substr(prefix.size()))) {
        err = "AICM uri is outside the private publication directory";
        return false;
    }
    const int dir = open_private_root(root, false, err);
    if (dir < 0) return false;
    const int fd = ::openat(dir, uri.c_str() + prefix.size(),
                            O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC);
    ::close(dir);
    struct stat st {};
    if (fd < 0 || ::fstat(fd, &st) != 0 || !S_ISREG(st.st_mode) ||
        st.st_uid != ::geteuid() || (st.st_mode & 07777) != 0600 || st.st_size < 0 ||
        static_cast<uintmax_t>(st.st_size) > std::numeric_limits<size_t>::max()) {
        if (fd >= 0) ::close(fd);
        err = "cannot open private AICM file";
        return false;
    }
    const size_t size = static_cast<size_t>(st.st_size);
    void* view = size ? ::mmap(nullptr, size, PROT_READ, MAP_PRIVATE, fd, 0) : nullptr;
    ::close(fd);
    if (view == MAP_FAILED) {
        err = "cannot map private AICM file";
        return false;
    }
    auto* impl = new MapImpl;
    impl->kind = MapImpl::Shm; // The POSIX unmap path releases any mmap-backed bytes.
    impl->view = view;
    impl->map_size = size;
    out.impl = impl;
    out.data = static_cast<const uint8_t*>(view);
    out.size = size;
    return true;
}
#else
std::string publish_private_file(const std::string&, const uint8_t*, size_t) { return {}; }
void unlink_private_file(const std::string&, const std::string&) {}
bool map_private_file(const std::string&, const std::string&, Mapping&, std::string& err) {
    err = "private AICM publication requires POSIX";
    return false;
}
#endif

std::string publish_file(const std::string& key, const uint8_t* data, size_t n) {
    std::error_code ec;
    std::filesystem::path dir = std::filesystem::temp_directory_path(ec) / "chudware_cpp_docs";
    std::filesystem::create_directories(dir, ec);
    std::filesystem::path target = dir / (object_key(key) + ".aicm");
    std::ofstream f(target, std::ios::binary | std::ios::trunc);
    if (!f) return {};
    if (n) f.write(reinterpret_cast<const char*>(data), static_cast<std::streamsize>(n));
    f.close();
    if (!f) return {};
    return std::filesystem::absolute(target).string();
}

#ifdef _WIN32
std::string shm_object_name(const std::string& key) { return "Local\\chudware_" + object_key(key); }

void unlink_shm_name(const std::string& name) {
    std::lock_guard<std::mutex> lk(g_mu);
    auto it = g_win_keepers.find(name);
    if (it != g_win_keepers.end()) {
        CloseHandle(it->second);
        g_win_keepers.erase(it);
    }
}

std::string publish_shm(const std::string& key, const uint8_t* data, size_t n) {
    const std::string name = shm_object_name(key);
    const size_t total = 8 + n;
    // Drop this process's previous publication of the key first: a named mapping lives until
    // its last handle closes, so a republish must never write into the old object in place.
    unlink_shm_name(name);
    HANDLE h = CreateFileMappingA(INVALID_HANDLE_VALUE, nullptr, PAGE_READWRITE,
                                  static_cast<DWORD>((total >> 32) & 0xffffffffu),
                                  static_cast<DWORD>(total & 0xffffffffu), name.c_str());
    if (!h) return {};
    // An existing object is someone else's: a reader still holding the previous revision, or
    // a dead process whose id was reused. CreateFileMapping hands back that object at its old
    // size; writing it would corrupt a live mesh, so fall back to a private file instead.
    if (GetLastError() == ERROR_ALREADY_EXISTS) {
        CloseHandle(h);
        return {};
    }
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
#elif defined(__EMSCRIPTEN__)
// Browser client: no POSIX shm. publish falls back to publish_file (MEMFS);
// consumers fetch aicm-shm:// via GET /files/aicm?shm=… (see loadAicmFile).
std::string shm_object_name(const std::string& key) { return "/chudware_" + object_key(key); }

std::string publish_shm(const std::string& /*key*/, const uint8_t* /*data*/, size_t /*n*/) {
    return {};
}

bool map_shm(const std::string& name, Mapping& /*out*/, std::string& err) {
    err = "aicm-shm unavailable under emscripten (" + name + "); use HTTP /files/aicm";
    return false;
}

void unlink_shm_name(const std::string& /*name*/) {}
#else  // POSIX
#if defined(__APPLE__)
// macOS rejects POSIX shm names longer than PSHMNAMLEN (31) with ENAMETOOLONG, and the
// pid-scoped "/chudware_p<pid>_<doc>_r<rev>" name exceeds it, which silently demoted every
// publish to the temp-file fallback. Use a short product prefix, and past the limit a
// pid-scoped FNV-1a digest of the key (at most 4 + 10 + 1 + 16 = 31 characters).
constexpr size_t kMacShmNameMax = 31;

std::string shm_object_name(const std::string& key) {
    std::string name = "/cw" + object_key(key);
    if (name.size() <= kMacShmNameMax) return name;
    uint64_t hash = 14695981039346656037ull;
    for (unsigned char c : sanitize(key)) {
        hash ^= c;
        hash *= 1099511628211ull;
    }
    static constexpr char kHex[] = "0123456789abcdef";
    std::string digest(16, '0');
    for (int i = 15; i >= 0; --i, hash >>= 4) digest[static_cast<size_t>(i)] = kHex[hash & 0xf];
    return "/cwp" + std::to_string(process_id()) + "_" + digest;
}
#else
std::string shm_object_name(const std::string& key) { return "/chudware_" + object_key(key); }
#endif

std::string publish_shm(const std::string& key, const uint8_t* data, size_t n) {
    const std::string name = shm_object_name(key);
    shm_unlink(name.c_str());
    // O_EXCL right after the unlink: an object that reappears in between belongs to a racing
    // publisher, and silently reopening it would hand out a mesh this call did not write.
    int fd = shm_open(name.c_str(), O_CREAT | O_RDWR | O_EXCL, 0600);
    if (fd < 0) return {};
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

void remove_publication(const std::string& uri, const std::string& private_root) {
    if (uri.empty()) return;
    if (!private_root.empty()) {
        unlink_private_file(private_root, uri);
    } else if (is_shm_uri(uri)) {
        unlink_shm_name(name_from_uri(uri));
    } else {
        std::error_code ec;
        std::filesystem::remove(uri, ec);
    }
}

}  // namespace

bool is_shm_uri(const std::string& uri) { return uri.rfind(kUriPrefix, 0) == 0; }

bool set_publication_root(const std::string& root, std::string& err) {
    err.clear();
    {
        std::lock_guard<std::mutex> lk(g_mu);
        g_publication_root = root;
    }
    const PublicationRoot selected = publication_root();
    if (!selected.enabled) return true;
    std::string normalized;
    if (!normalize_private_root(selected.path, normalized, err)) return false;
#if !defined(_WIN32) && !defined(__EMSCRIPTEN__)
    const int fd = open_private_root(normalized, true, err);
    if (fd < 0) return false;
    ::close(fd);
    return true;
#else
    err = "private AICM publication requires POSIX";
    return false;
#endif
}

// Keys remain process-local, even when cell publication uses opaque file names.
static std::string doc_key_prefix(const std::string& doc_id) {
    return sanitize(doc_id) + "_r";
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
    const PublicationRoot selected = publication_root();
    std::string private_root;
    std::string uri;
    if (selected.enabled) {
        std::string err;
        if (!normalize_private_root(selected.path, private_root, err)) return {};
        uri = publish_private_file(private_root, data, n);
    } else {
        uri = publish_shm(key, data, n);
        if (uri.empty()) uri = publish_file(key, data, n);
    }
    if (uri.empty()) return {};

    CacheEntry e;
    e.bytes = n ? std::make_shared<const std::vector<uint8_t>>(data, data + n)
                : std::make_shared<const std::vector<uint8_t>>();
    e.uri = uri;
    e.private_root = private_root;
    std::string replaced;
    std::string replaced_root;
    {
        std::lock_guard<std::mutex> lk(g_mu);
        CacheEntry& slot = g_cache[key];
        replaced = std::move(slot.uri);
        replaced_root = std::move(slot.private_root);
        slot = std::move(e);
    }
    // Readers retain immutable bytes/mappings while replaced backing files are removed.
    if (!replaced.empty() && replaced != uri) remove_publication(replaced, replaced_root);
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

    const PublicationRoot selected = publication_root();
    if (selected.enabled) {
        std::string root;
        if (!normalize_private_root(selected.path, root, err)) return false;
        return map_private_file(root, uri, out, err);
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
    std::string private_root;
    {
        std::lock_guard<std::mutex> lk(g_mu);
        auto it = g_cache.find(key);
        if (it != g_cache.end()) {
            uri = it->second.uri;
            private_root = std::move(it->second.private_root);
            g_cache.erase(it);
        }
    }
    if (!uri.empty()) {
        remove_publication(uri, private_root);
    } else if (!publication_root().enabled) {
        // Desktop best-effort unlink without a cache hit. Cells never touch shm.
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
