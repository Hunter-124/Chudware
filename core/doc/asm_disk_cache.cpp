#include "doc/asm_disk_cache.h"

#include "doc/native_policy.h"
#include "doc/stepplus.h"
#include "util/sha256.h"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <limits>
#include <mutex>
#include <system_error>
#include <thread>
#include <vector>

#if defined(_WIN32)
#include <process.h>
#else
#include <unistd.h>
#endif

namespace chudware::doc::asm_disk_cache {
namespace {
namespace fs = std::filesystem;

constexpr char kMagic[8] = {'C', 'W', 'A', 'S', 'M', 'C', '0', '1'};
// Bump with any change to what an entry holds (tessellator output, nested body layout)
// that the engine's geometry epoch does not already cover.
constexpr const char* kFormat = "asm-cache/1";
constexpr std::uint64_t kDefaultCapMb = 2048;

std::mutex g_evict_mtx;

bool disabled() {
    const char* off = std::getenv("CHUDWARE_ASM_CACHE");
    return off && (std::strcmp(off, "0") == 0 || std::strcmp(off, "off") == 0);
}

fs::path env_path(const char* name) {
#if defined(_WIN32)
    std::wstring wide(name, name + std::strlen(name));
    const wchar_t* value = _wgetenv(wide.c_str());
    return value && *value ? fs::path(value) : fs::path();
#else
    const char* value = std::getenv(name);
    return value && *value ? fs::path(value) : fs::path();
#endif
}

fs::path root() {
#if defined(__EMSCRIPTEN__)
    return {};
#else
    if (disabled()) return {};
    if (fs::path explicit_dir = env_path("CHUDWARE_ASM_CACHE_DIR"); !explicit_dir.empty())
        return explicit_dir;
    const std::string state = native_policy::setting(true);
    if (!state.empty()) return fs::u8path(state) / "cache" / "assembly";
#if defined(_WIN32)
    if (fs::path home = env_path("LOCALAPPDATA"); !home.empty())
        return home / "Chudware" / "cache" / "assembly";
#else
    if (fs::path xdg = env_path("XDG_CACHE_HOME"); !xdg.empty())
        return xdg / "chudware" / "assembly";
    if (fs::path home = env_path("HOME"); !home.empty())
        return home / ".cache" / "chudware" / "assembly";
#endif
    return {};
#endif
}

std::uint64_t cap_bytes() {
    std::uint64_t mb = kDefaultCapMb;
    if (const char* v = std::getenv("CHUDWARE_ASM_CACHE_MAX_MB"); v && *v) {
        char* end = nullptr;
        const unsigned long long parsed = std::strtoull(v, &end, 10);
        if (end && *end == '\0') mb = parsed;
    }
    return mb * 1024ull * 1024ull;
}

// The entry's full identity: the file name is its digest, the body repeats it so a
// digest collision or a renamed file can never serve another key's payload.
std::string identity(const std::string& kind, const std::string& key) {
    return std::string(kFormat) + "|" + stepplus_cache_engine_key() + "|" + kind + "|" + key;
}

fs::path entry_path(const fs::path& dir, const std::string& id) {
    return dir / (util::sha256_hex(id) + ".bin");
}

void put_u64(std::string& out, std::uint64_t v) {
    for (int i = 0; i < 8; ++i) out.push_back(static_cast<char>((v >> (8 * i)) & 0xff));
}

bool get_u64(const std::string& in, std::size_t& pos, std::uint64_t& v) {
    if (pos > in.size() || in.size() - pos < 8) return false;
    v = 0;
    for (int i = 0; i < 8; ++i)
        v |= static_cast<std::uint64_t>(static_cast<unsigned char>(in[pos + i])) << (8 * i);
    pos += 8;
    return true;
}

void evict(const fs::path& dir) {
    std::lock_guard<std::mutex> lock(g_evict_mtx);
    struct Item { fs::path path; fs::file_time_type time; std::uint64_t size; };
    std::vector<Item> items;
    std::uint64_t total = 0;
    std::error_code ec;
    const auto now = fs::file_time_type::clock::now();
    for (fs::directory_iterator it(dir, ec), end; !ec && it != end; it.increment(ec)) {
        std::error_code e;
        if (!it->is_regular_file(e)) continue;
        const auto time = it->last_write_time(e);
        if (e) continue;
        const std::string name = it->path().filename().string();
        if (name.find(".tmp-") != std::string::npos) {
            // A writer that died mid-publication; live writers rename within seconds.
            if (now - time > std::chrono::hours(1)) fs::remove(it->path(), e);
            continue;
        }
        const std::uint64_t size = it->file_size(e);
        if (e) continue;
        items.push_back({it->path(), time, size});
        total += size;
    }
    const std::uint64_t cap = cap_bytes();
    if (total <= cap) return;
    std::sort(items.begin(), items.end(),
              [](const Item& a, const Item& b) { return a.time < b.time; });
    // Trim below the cap so a steady stream of new entries does not rescan on every put.
    const std::uint64_t target = cap - cap / 5;
    for (const Item& item : items) {
        if (total <= target) break;
        std::error_code e;
        if (fs::remove(item.path, e)) total -= std::min(total, item.size);
    }
}

}  // namespace

std::string directory() {
    return native_policy::path_utf8(root());
}

std::uint64_t total_bytes() {
    std::uint64_t total = 0;
    std::error_code ec;
    const fs::path dir = root();
    if (dir.empty()) return 0;
    for (fs::directory_iterator it(dir, ec), end; !ec && it != end; it.increment(ec)) {
        std::error_code e;
        if (it->is_regular_file(e)) total += it->file_size(e);
    }
    return total;
}

bool get(const std::string& kind, const std::string& key, std::string& payload) {
    try {
        const fs::path dir = root();
        if (dir.empty() || key.empty()) return false;
        const std::string id = identity(kind, key);
        const fs::path path = entry_path(dir, id);
        std::string bytes;
        {
            std::ifstream in(path, std::ios::binary);
            if (!in) return false;
            in.seekg(0, std::ios::end);
            const std::streamoff size = in.tellg();
            if (size <= 0) return false;
            in.seekg(0, std::ios::beg);
            bytes.resize(static_cast<std::size_t>(size));
            if (!in.read(bytes.data(), size)) return false;
        }
        constexpr std::size_t kDigest = 64;
        if (bytes.size() < sizeof(kMagic) + kDigest ||
            std::memcmp(bytes.data(), kMagic, sizeof(kMagic)) != 0) return false;
        std::size_t pos = sizeof(kMagic);
        std::uint64_t id_len = 0, payload_len = 0;
        if (!get_u64(bytes, pos, id_len) || id_len > bytes.size() - pos ||
            bytes.compare(pos, static_cast<std::size_t>(id_len), id) != 0) return false;
        pos += static_cast<std::size_t>(id_len);
        if (!get_u64(bytes, pos, payload_len) || payload_len > bytes.size() - pos ||
            bytes.size() - pos - payload_len != kDigest) return false;
        const std::string_view body(bytes.data() + pos, static_cast<std::size_t>(payload_len));
        if (bytes.compare(pos + payload_len, kDigest, util::sha256_hex(body)) != 0) return false;
        bytes.resize(pos + static_cast<std::size_t>(payload_len));
        bytes.erase(0, pos);
        payload = std::move(bytes);
        std::error_code ec;
        fs::last_write_time(path, fs::file_time_type::clock::now(), ec);  // LRU age
        return true;
    } catch (...) {
        return false;
    }
}

void put(const std::string& kind, const std::string& key, const std::string& payload) {
    try {
        const fs::path dir = root();
        if (dir.empty() || key.empty()) return;
        if (payload.size() > cap_bytes() / 4) return;  // one entry never flushes the store
        std::error_code ec;
        fs::create_directories(dir, ec);
        if (ec) return;
        const std::string id = identity(kind, key);
        const fs::path path = entry_path(dir, id);
        static std::atomic<std::uint64_t> counter{0};
#if defined(_WIN32)
        const long pid = static_cast<long>(_getpid());
#else
        const long pid = static_cast<long>(getpid());
#endif
        fs::path temporary = path;
        temporary += ".tmp-" + std::to_string(pid) + "-" + std::to_string(++counter);
        {
            std::ofstream out(temporary, std::ios::binary | std::ios::trunc);
            if (!out) return;
            std::string header(kMagic, sizeof(kMagic));
            put_u64(header, id.size());
            header += id;
            put_u64(header, payload.size());
            out.write(header.data(), static_cast<std::streamsize>(header.size()));
            out.write(payload.data(), static_cast<std::streamsize>(payload.size()));
            const std::string digest = util::sha256_hex(payload);
            out.write(digest.data(), static_cast<std::streamsize>(digest.size()));
            out.close();
            if (!out) { fs::remove(temporary, ec); return; }
        }
        // A concurrent reader can hold the old entry open on Windows; antivirus can hold
        // the new one for a moment. Losing this race only costs a later re-mesh.
        for (int attempt = 0; attempt < 3; ++attempt) {
            ec.clear();
            fs::rename(temporary, path, ec);
            if (!ec) break;
            std::this_thread::sleep_for(std::chrono::milliseconds(20));
        }
        if (ec) { fs::remove(temporary, ec); return; }
        evict(dir);
    } catch (...) {
        // Best effort: the caller already holds the computed value.
    }
}

namespace {
template <typename T>
void put_vec(std::string& out, const std::vector<T>& v) {
    put_u64(out, v.size());
    if (!v.empty())
        out.append(reinterpret_cast<const char*>(v.data()), v.size() * sizeof(T));
}

template <typename Vec, typename T>
bool get_vec(const std::string& in, std::size_t& pos, Vec& out) {
    std::uint64_t count = 0;
    if (!get_u64(in, pos, count)) return false;
    if (count > (in.size() - pos) / sizeof(T)) return false;
    std::vector<T> values(static_cast<std::size_t>(count));
    if (count) std::memcpy(values.data(), in.data() + pos, values.size() * sizeof(T));
    pos += values.size() * sizeof(T);
    out.clear();
    out.insert_end(values.begin(), values.end());
    return true;
}

struct StdVecAdapter {
    std::vector<aicm::AppearanceRegion>& v;
    void clear() { v.clear(); }
    template <typename It> void insert_end(It a, It b) { v.insert(v.end(), a, b); }
};

std::uint64_t layout_tag() {
    return (static_cast<std::uint64_t>(sizeof(aicm::FaceRec)) << 0) |
           (static_cast<std::uint64_t>(sizeof(aicm::EdgeRec)) << 16) |
           (static_cast<std::uint64_t>(sizeof(aicm::VertRec)) << 32) |
           (static_cast<std::uint64_t>(sizeof(aicm::AppearanceRegion)) << 48);
}
}  // namespace

std::string encode_body(const aicm::AicmBody& b) {
    std::string out = "AMB1";
    put_u64(out, layout_tag());
    put_u64(out, b.body_id);
    put_u64(out, b.name.size());
    out += b.name;
    const float scalars[5] = {b.color[0], b.color[1], b.color[2], b.metallic, b.roughness};
    out.append(reinterpret_cast<const char*>(scalars), sizeof(scalars));
    put_vec(out, b.pos.vec());
    put_vec(out, b.nrm.vec());
    put_vec(out, b.idx.vec());
    put_vec(out, b.faces.vec());
    put_vec(out, b.edges.vec());
    put_vec(out, b.edge_pts.vec());
    put_vec(out, b.verts.vec());
    put_vec(out, b.appearance_regions);
    return out;
}

bool decode_body(const std::string& in, aicm::AicmBody& b) {
    if (in.size() < 4 || in.compare(0, 4, "AMB1") != 0) return false;
    std::size_t pos = 4;
    std::uint64_t tag = 0, id = 0, name_len = 0;
    if (!get_u64(in, pos, tag) || tag != layout_tag()) return false;
    if (!get_u64(in, pos, id) || id > std::numeric_limits<std::uint32_t>::max()) return false;
    if (!get_u64(in, pos, name_len) || name_len > in.size() - pos) return false;
    aicm::AicmBody out;
    out.body_id = static_cast<std::uint32_t>(id);
    out.name = in.substr(pos, static_cast<std::size_t>(name_len));
    pos += static_cast<std::size_t>(name_len);
    float scalars[5];
    if (in.size() - pos < sizeof(scalars)) return false;
    std::memcpy(scalars, in.data() + pos, sizeof(scalars));
    pos += sizeof(scalars);
    std::copy(scalars, scalars + 3, out.color);
    out.metallic = scalars[3];
    out.roughness = scalars[4];
    StdVecAdapter regions{out.appearance_regions};
    if (!get_vec<aicm::CowVec<float>, float>(in, pos, out.pos) ||
        !get_vec<aicm::CowVec<float>, float>(in, pos, out.nrm) ||
        !get_vec<aicm::CowVec<std::uint32_t>, std::uint32_t>(in, pos, out.idx) ||
        !get_vec<aicm::CowVec<aicm::FaceRec>, aicm::FaceRec>(in, pos, out.faces) ||
        !get_vec<aicm::CowVec<aicm::EdgeRec>, aicm::EdgeRec>(in, pos, out.edges) ||
        !get_vec<aicm::CowVec<float>, float>(in, pos, out.edge_pts) ||
        !get_vec<aicm::CowVec<aicm::VertRec>, aicm::VertRec>(in, pos, out.verts) ||
        !get_vec<StdVecAdapter, aicm::AppearanceRegion>(in, pos, regions) ||
        pos != in.size())
        return false;
    // Structural consistency: every index and record must address the decoded buffers.
    const std::size_t n_verts = out.pos.size() / 3, n_tris = out.idx.size() / 3;
    if (out.pos.size() % 3 || out.nrm.size() != out.pos.size() || out.idx.size() % 3 ||
        out.edge_pts.size() % 3) return false;
    for (std::uint32_t i : out.idx.vec()) if (i >= n_verts) return false;
    for (const aicm::FaceRec& f : out.faces.vec())
        if (f.first_tri > n_tris || f.tri_count > n_tris - f.first_tri) return false;
    const std::size_t n_pts = out.edge_pts.size() / 3;
    for (const aicm::EdgeRec& e : out.edges.vec())
        if (e.first_pt > n_pts || e.pt_count > n_pts - e.first_pt) return false;
    b = std::move(out);
    return true;
}

}  // namespace chudware::doc::asm_disk_cache
