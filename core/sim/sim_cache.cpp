// sim_cache.cpp — see sim_cache.h.
#include "sim/sim_cache.h"

#include "util/resource_governor.h"

#include <atomic>
#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <limits>
#include <list>
#include <mutex>
#include <sstream>
#include <unordered_map>
#include <vector>

#if !defined(_WIN32)
#  include <unistd.h>
#else
#  include <process.h>
#  define NOMINMAX
#  include <windows.h>
#endif
namespace chudware::sim::cache {

using nlohmann::json;
namespace fs = std::filesystem;

namespace {

constexpr const char* kBlobMagic = "AICRES1";
constexpr const char* kCacheSchema = "chudware-simcache/2";
constexpr size_t kMemCapEntries = 64;
constexpr size_t kMaxSidecarOverhead = 4096;

// SHA-256 keeps content-address keys and corruption checks collision-resistant.
// Self-contained streaming implementation avoids copying large mesh/result payloads.
struct Sha256 {
    uint32_t h[8] = {0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
                     0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19};
    uint8_t buf[64] = {};
    size_t buflen = 0;
    uint64_t total = 0;

    static uint32_t rotr(uint32_t x, int n) { return (x >> n) | (x << (32 - n)); }

    void block(const uint8_t* p) {
        static const uint32_t k[64] = {
            0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1,
            0x923f82a4, 0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3,
            0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786,
            0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
            0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147,
            0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13,
            0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b,
            0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
            0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a,
            0x5b9cca4f, 0x682e6ff3, 0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208,
            0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2};
        uint32_t w[64];
        for (int i = 0; i < 16; ++i)
            w[i] = (uint32_t(p[i * 4]) << 24) | (uint32_t(p[i * 4 + 1]) << 16) |
                   (uint32_t(p[i * 4 + 2]) << 8) | uint32_t(p[i * 4 + 3]);
        for (int i = 16; i < 64; ++i) {
            const uint32_t s0 = rotr(w[i - 15], 7) ^ rotr(w[i - 15], 18) ^
                                (w[i - 15] >> 3);
            const uint32_t s1 = rotr(w[i - 2], 17) ^ rotr(w[i - 2], 19) ^
                                (w[i - 2] >> 10);
            w[i] = w[i - 16] + s0 + w[i - 7] + s1;
        }
        uint32_t a = h[0], b = h[1], c = h[2], d = h[3];
        uint32_t e = h[4], f = h[5], g = h[6], hh = h[7];
        for (int i = 0; i < 64; ++i) {
            const uint32_t s1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25);
            const uint32_t ch = (e & f) ^ (~e & g);
            const uint32_t t1 = hh + s1 + ch + k[i] + w[i];
            const uint32_t s0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22);
            const uint32_t maj = (a & b) ^ (a & c) ^ (b & c);
            const uint32_t t2 = s0 + maj;
            hh = g; g = f; f = e; e = d + t1;
            d = c; c = b; b = a; a = t1 + t2;
        }
        h[0] += a; h[1] += b; h[2] += c; h[3] += d;
        h[4] += e; h[5] += f; h[6] += g; h[7] += hh;
    }

    void update(const uint8_t* data, size_t len) {
        total += len;
        while (len > 0) {
            const size_t take = std::min(len, sizeof(buf) - buflen);
            std::memcpy(buf + buflen, data, take);
            buflen += take;
            data += take;
            len -= take;
            if (buflen == sizeof(buf)) {
                block(buf);
                buflen = 0;
            }
        }
    }

    void final(uint8_t out[32]) {
        const uint64_t bits = total * 8;
        uint8_t pad = 0x80;
        update(&pad, 1);
        pad = 0;
        while (buflen != 56) update(&pad, 1);
        uint8_t len_bytes[8];
        for (int i = 0; i < 8; ++i)
            len_bytes[i] = static_cast<uint8_t>(bits >> (56 - 8 * i));
        update(len_bytes, sizeof(len_bytes));
        for (int i = 0; i < 8; ++i) {
            out[i * 4] = static_cast<uint8_t>(h[i] >> 24);
            out[i * 4 + 1] = static_cast<uint8_t>(h[i] >> 16);
            out[i * 4 + 2] = static_cast<uint8_t>(h[i] >> 8);
            out[i * 4 + 3] = static_cast<uint8_t>(h[i]);
        }
    }
};

std::string sha256_hex(Sha256& sha) {
    uint8_t digest[32];
    sha.final(digest);
    static constexpr char hex[] = "0123456789abcdef";
    std::string out(64, '0');
    for (size_t i = 0; i < sizeof(digest); ++i) {
        out[i * 2] = hex[digest[i] >> 4];
        out[i * 2 + 1] = hex[digest[i] & 0x0f];
    }
    return out;
}

std::string sha256_hex(const std::string& bytes) {
    Sha256 sha;
    sha.update(reinterpret_cast<const uint8_t*>(bytes.data()), bytes.size());
    return sha256_hex(sha);
}


bool all_finite(const json& v) {
    if (v.is_number_float()) return std::isfinite(v.get<double>());
    if (v.is_array() || v.is_object()) {
        for (auto it = v.begin(); it != v.end(); ++it)
            if (!all_finite(it.value())) return false;
    }
    return true;
}

// nlohmann::json uses an ordered object map, so dump() is canonical for this
// JSON type. Do not round: even a one-ULP physics-input change must miss.
std::string canonical_dump(const json& v) {
    if (!all_finite(v)) return {};
    return v.dump();
}

std::string kind_tag(Kind k) {
    switch (k) {
        case Kind::FeaStatic: return "fea_static";
        case Kind::FeaModal: return "fea_modal";
        case Kind::FeaThermal: return "fea_thermal";
        case Kind::Spice: return "spice";
        case Kind::Cfd: return "cfd";
        case Kind::Em: return "em";
    }
    return "unknown";
}

// ── In-process store ────────────────────────────────────────────────────────
struct Entry {
    json result;
};
struct MemStore {
    std::mutex mtx;
    std::unordered_map<std::string, Entry> map;
    std::list<std::string> lru;  // most-recent front
    Stats stats;
    std::string sidecar;
};
MemStore& store_state() {
    static MemStore s;
    return s;
}

void touch_lru(MemStore& s, const std::string& key) {
    s.lru.remove(key);
    s.lru.push_front(key);
    while (s.lru.size() > kMemCapEntries) {
        s.map.erase(s.lru.back());
        s.lru.pop_back();
    }
}

bool low_mem() { return chudware::governor::low_memory_now(); }

std::string memory_key(Kind kind, const std::string& input_hash) {
    return kind_tag(kind) + ":" + input_hash;
}

bool valid_sidecar_hash(const std::string& input_hash) {
    if (input_hash.size() < 16 || input_hash.size() > 128) return false;
    for (char c : input_hash)
        if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'))) return false;
    return true;
}

std::mutex& sidecar_io_mutex() {
    static std::mutex m;
    return m;
}

std::atomic<unsigned long long>& sidecar_tmp_sequence() {
    static std::atomic<unsigned long long> seq{0};
    return seq;
}

long long process_id() {
#if defined(_WIN32)
    return static_cast<long long>(_getpid());
#else
    return static_cast<long long>(getpid());
#endif
}

std::string default_sidecar_dir() {
    if (const char* env = std::getenv("CHUDWARE_SIM_CACHE_DIR")) {
        if (env[0]) return std::string(env);
    }
    fs::path base;
    if (const char* t = std::getenv("TMPDIR")) base = t;
    else if (const char* t = std::getenv("TEMP")) base = t;
    else base = "/tmp";
    return (base / ("chudware-sim-cache-" + std::to_string(process_id()))).string();
}

// STEP content fingerprint. Failure returns empty so callers disable cache reuse
// instead of trusting a path that can later name different geometry.
std::string step_content_fingerprint(const std::string& path) {
    std::ifstream f(path, std::ios::binary);
    if (!f) return {};
    Sha256 sha;
    std::array<char, 64 * 1024> buf{};
    while (f) {
        f.read(buf.data(), static_cast<std::streamsize>(buf.size()));
        const std::streamsize got = f.gcount();
        if (got > 0)
            sha.update(reinterpret_cast<const uint8_t*>(buf.data()),
                       static_cast<size_t>(got));
    }
    if (!f.eof()) return {};
    return std::string("sha256:") + sha256_hex(sha);
}

fs::path sidecar_file(const std::string& dir, Kind kind, const std::string& input_hash) {
    return fs::path(dir) / (kind_tag(kind) + "_" + input_hash + ".json");
}

bool atomic_replace(const fs::path& from, const fs::path& to) {
#if defined(_WIN32)
    return MoveFileExW(from.c_str(), to.c_str(),
                       MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH) != 0;
#else
    return ::rename(from.c_str(), to.c_str()) == 0;
#endif
}

bool write_sidecar(const std::string& dir, Kind kind, const std::string& input_hash,
                   const json& result, const std::string& body) {
    if (dir.empty() || !valid_sidecar_hash(input_hash) || !all_finite(result)) return false;
    json envelope = {{"engine", sim_cache_engine_key()},
                     {"kind", kind_tag(kind)},
                     {"input_hash", input_hash},
                     {"solved", true},
                     {"body_bytes", body.size()},
                     {"body_checksum", sha256_hex(body)},
                     {"result", result}};
    const std::string bytes = envelope.dump();
    if (bytes.size() > kMaxStudyResultBytes + kMaxSidecarOverhead) return false;

    std::lock_guard<std::mutex> io_lock(sidecar_io_mutex());
    std::error_code ec;
    fs::create_directories(dir, ec);
    if (ec) return false;
    const fs::path p = sidecar_file(dir, kind, input_hash);
    const unsigned long long seq = sidecar_tmp_sequence().fetch_add(1);
    const fs::path tmp =
        fs::path(p.string() + ".tmp." + std::to_string(process_id()) +
                 "." + std::to_string(seq));
    {
        std::ofstream out(tmp, std::ios::binary | std::ios::trunc);
        if (!out) return false;
        out.write(bytes.data(), static_cast<std::streamsize>(bytes.size()));
        out.flush();
        if (!out) {
            out.close();
            fs::remove(tmp, ec);
            return false;
        }
    }
    if (!atomic_replace(tmp, p)) {
        fs::remove(tmp, ec);
        return false;
    }
    return true;
}

enum class SidecarRead { Missing, Hit, Corrupt, Stale };

SidecarRead read_sidecar(const std::string& dir, Kind kind, const std::string& input_hash,
                         json& result_out) {
    if (dir.empty() || !valid_sidecar_hash(input_hash)) return SidecarRead::Missing;
    std::lock_guard<std::mutex> io_lock(sidecar_io_mutex());
    const fs::path p = sidecar_file(dir, kind, input_hash);
    std::error_code ec;
    const auto size = fs::file_size(p, ec);
    if (ec) {
        std::error_code exists_ec;
        const bool exists = fs::exists(p, exists_ec);
        if (exists_ec) return SidecarRead::Corrupt;
        return exists ? SidecarRead::Corrupt : SidecarRead::Missing;
    }
    if (size > kMaxStudyResultBytes + kMaxSidecarOverhead) return SidecarRead::Corrupt;
    std::ifstream in(p, std::ios::binary);
    if (!in) return SidecarRead::Corrupt;
    std::string bytes((std::istreambuf_iterator<char>(in)), std::istreambuf_iterator<char>());
    if (bytes.size() != size) return SidecarRead::Corrupt;
    try {
        const json env = json::parse(bytes);
        if (!env.is_object()) return SidecarRead::Corrupt;
        if (env.value("engine", std::string()) != sim_cache_engine_key())
            return SidecarRead::Stale;
        if (!env.value("solved", false) ||
            env.value("input_hash", std::string()) != input_hash ||
            env.value("kind", std::string()) != kind_tag(kind) ||
            !env.contains("result") || !env.contains("body_bytes") ||
            !env.contains("body_checksum"))
            return SidecarRead::Corrupt;
        const json& result = env["result"];
        if (!all_finite(result)) return SidecarRead::Corrupt;
        const std::string body = result.dump();
        if (!env["body_bytes"].is_number_unsigned() ||
            env["body_bytes"].get<size_t>() != body.size() ||
            !env["body_checksum"].is_string() ||
            env["body_checksum"].get<std::string>() != sha256_hex(body))
            return SidecarRead::Corrupt;
        result_out = result;
        return SidecarRead::Hit;
    } catch (...) {
        return SidecarRead::Corrupt;
    }
}

void put_u32_le(std::string& out, uint32_t v) {
    out.push_back(static_cast<char>(v & 0xff));
    out.push_back(static_cast<char>((v >> 8) & 0xff));
    out.push_back(static_cast<char>((v >> 16) & 0xff));
    out.push_back(static_cast<char>((v >> 24) & 0xff));
}

uint32_t get_u32_le(const std::string& s, size_t pos) {
    if (pos + 4 > s.size()) return 0;
    return (uint32_t)(unsigned char)s[pos] |
           ((uint32_t)(unsigned char)s[pos + 1] << 8) |
           ((uint32_t)(unsigned char)s[pos + 2] << 16) |
           ((uint32_t)(unsigned char)s[pos + 3] << 24);
}

}  // namespace

const char* kind_name(Kind k) {
    switch (k) {
        case Kind::FeaStatic: return "fea_static";
        case Kind::FeaModal: return "fea_modal";
        case Kind::FeaThermal: return "fea_thermal";
        case Kind::Spice: return "spice";
        case Kind::Cfd: return "cfd";
        case Kind::Em: return "em";
    }
    return "unknown";
}

std::string sim_cache_engine_key() { return std::string(kCacheSchema); }

std::string hash_inputs(Kind kind, const json& inputs) {
    if (kind_tag(kind) == "unknown") return {};
    try {
        const json envelope = {{"engine", sim_cache_engine_key()},
                               {"kind", kind_tag(kind)},
                               {"inputs", inputs}};
        const std::string canonical = canonical_dump(envelope);
        return canonical.empty() ? std::string() : sha256_hex(canonical);
    } catch (...) {
        return {};
    }
}

std::string hash_fea_study(Kind kind, const json& study) {
    if (kind != Kind::FeaStatic && kind != Kind::FeaModal && kind != Kind::FeaThermal)
        return {};
    if (!study.is_object()) return {};
    json s = study;
    if (s.contains("step_path")) {
        if (!s["step_path"].is_string()) return {};
        const std::string path = s["step_path"].get<std::string>();
        if (!path.empty()) {
            const std::string fingerprint = step_content_fingerprint(path);
            if (fingerprint.empty()) return {};
            s["step_content"] = fingerprint;
            s.erase("step_path");
        }
    }
    // Cache-control / progress side-channels do not affect solver physics.
    s.erase("progress_path");
    s.erase("_cache");
    return hash_inputs(kind, s);
}

std::string hash_spice(const json& circuit, const json& study) {
    json inputs = {{"circuit", circuit}, {"study", study}};
    return hash_inputs(Kind::Spice, inputs);
}

std::string hash_cfd(const json& geom_and_params) {
    return hash_inputs(Kind::Cfd, geom_and_params);
}

Lookup lookup(Kind kind, const std::string& input_hash) {
    Lookup L;
    L.input_hash = input_hash;
    auto& st = store_state();
    if (!valid_sidecar_hash(input_hash) || kind_tag(kind) == "unknown") {
        std::lock_guard<std::mutex> lk(st.mtx);
        ++st.stats.misses;
        return L;
    }
    if (low_mem()) {
        L.reason = "low_memory";
        std::lock_guard<std::mutex> lk(st.mtx);
        ++st.stats.misses;
        return L;
    }

    const std::string fallback_dir = default_sidecar_dir();
    const std::string key = memory_key(kind, input_hash);

    // Keep directory selection and the memory generation locked across disk
    // fallback. set_sidecar_dir therefore has a clear before/after ordering.
    std::lock_guard<std::mutex> lk(st.mtx);
    const std::string& dir = st.sidecar.empty() ? fallback_dir : st.sidecar;
    auto it = st.map.find(key);
    if (it != st.map.end()) {
        L.hit = true;
        L.result = it->second.result;
        L.reason = "hit";
        touch_lru(st, key);
        ++st.stats.hits;
        return L;
    }
    json side;
    SidecarRead disk = SidecarRead::Corrupt;
    try {
        disk = read_sidecar(dir, kind, input_hash, side);
    } catch (...) {
        disk = SidecarRead::Corrupt;
    }
    if (disk == SidecarRead::Hit) {
        st.map[key] = Entry{side};
        touch_lru(st, key);
        ++st.stats.hits;
        L.hit = true;
        L.result = std::move(side);
        L.reason = "hit";
        return L;
    }

    ++st.stats.misses;
    L.reason = disk == SidecarRead::Corrupt ? "corrupt"
             : disk == SidecarRead::Stale   ? "stale"
                                            : "miss";
    return L;
}

bool store(Kind kind, const std::string& input_hash, const json& result) {
    auto& st = store_state();
    if (!valid_sidecar_hash(input_hash) || kind_tag(kind) == "unknown" ||
        !all_finite(result)) {
        std::lock_guard<std::mutex> lk(st.mtx);
        ++st.stats.drops;
        return false;
    }
    if (low_mem()) {
        std::lock_guard<std::mutex> lk(st.mtx);
        ++st.stats.drops;
        return false;
    }
    std::string dumped;
    try {
        dumped = result.dump();
    } catch (...) {
        std::lock_guard<std::mutex> lk(st.mtx);
        ++st.stats.drops;
        return false;
    }
    if (dumped.size() > kMaxStudyResultBytes) {
        std::lock_guard<std::mutex> lk(st.mtx);
        ++st.stats.drops;
        return false;
    }
    const std::string fallback_dir = default_sidecar_dir();
    {
        // Directory selection, disk replacement and memory publication are one
        // serialized generation relative to lookup/store/set_sidecar_dir.
        std::lock_guard<std::mutex> lk(st.mtx);
        const std::string& dir = st.sidecar.empty() ? fallback_dir : st.sidecar;
        try {
            write_sidecar(dir, kind, input_hash, result, dumped);  // best-effort
        } catch (...) {
            // Memory publication remains valid; persistence is optional.
        }
        const std::string key = memory_key(kind, input_hash);
        st.map[key] = Entry{result};
        touch_lru(st, key);
        ++st.stats.stores;
    }
    return true;
}

void set_sidecar_dir(const std::string& dir) {
    auto& st = store_state();
    std::lock_guard<std::mutex> lk(st.mtx);
    st.sidecar = dir;
}

std::string sidecar_dir() {
    auto& st = store_state();
    {
        std::lock_guard<std::mutex> lk(st.mtx);
        if (!st.sidecar.empty()) return st.sidecar;
    }
    return default_sidecar_dir();
}

void clear() {
    auto& st = store_state();
    std::lock_guard<std::mutex> lk(st.mtx);
    st.map.clear();
    st.lru.clear();
}

Stats stats() {
    auto& st = store_state();
    std::lock_guard<std::mutex> lk(st.mtx);
    return st.stats;
}

void reset_stats() {
    auto& st = store_state();
    std::lock_guard<std::mutex> lk(st.mtx);
    st.stats = Stats{};
}

std::string encode_result_blob(Kind kind, const std::string& input_hash, const json& result) {
    if (low_mem() || input_hash.empty() || kind_tag(kind) == "unknown" ||
        !all_finite(result))
        return {};
    try {
        const std::string body_s = result.dump();
        json header = {{"engine", sim_cache_engine_key()},
                       {"input_hash", input_hash},
                       {"kind", kind_tag(kind)},
                       {"solved", true},
                       {"body_bytes", body_s.size()},
                       {"body_checksum", sha256_hex(body_s)}};
        const std::string header_s = header.dump();
        if (header_s.empty() ||
            header_s.size() + body_s.size() + 8 + 4 > kMaxStudyResultBytes ||
            header_s.size() > std::numeric_limits<uint32_t>::max())
            return {};
        std::string out;
        out.reserve(8 + 4 + header_s.size() + body_s.size());
        out.append(kBlobMagic, 8);
        put_u32_le(out, static_cast<uint32_t>(header_s.size()));
        out.append(header_s);
        out.append(body_s);
        return out;
    } catch (...) {
        return {};
    }
}

bool decode_result_blob(const std::string& blob, Kind& kind_out, std::string& input_hash_out,
                        json& result_out) {
    if (blob.size() < 12 || blob.size() > kMaxStudyResultBytes) return false;
    if (blob.compare(0, 8, kBlobMagic, 8) != 0) return false;
    const uint32_t hlen = get_u32_le(blob, 8);
    if (hlen == 0 || static_cast<size_t>(hlen) > blob.size() - 12) return false;
    try {
        const json header = json::parse(blob.substr(12, hlen));
        if (!header.is_object() ||
            header.value("engine", std::string()) != sim_cache_engine_key() ||
            !header.value("solved", false) ||
            !header.contains("input_hash") || !header["input_hash"].is_string() ||
            header["input_hash"].get<std::string>().empty() ||
            !header.contains("body_bytes") || !header["body_bytes"].is_number_unsigned() ||
            !header.contains("body_checksum") || !header["body_checksum"].is_string())
            return false;

        Kind decoded_kind;
        const std::string k = header.value("kind", std::string());
        if (k == "fea_static") decoded_kind = Kind::FeaStatic;
        else if (k == "fea_modal") decoded_kind = Kind::FeaModal;
        else if (k == "fea_thermal") decoded_kind = Kind::FeaThermal;
        else if (k == "spice") decoded_kind = Kind::Spice;
        else if (k == "cfd") decoded_kind = Kind::Cfd;
        else if (k == "em") decoded_kind = Kind::Em;
        else return false;

        const std::string body = blob.substr(12 + hlen);
        if (header["body_bytes"].get<size_t>() != body.size() ||
            header["body_checksum"].get<std::string>() != sha256_hex(body))
            return false;
        json decoded_result = json::parse(body);
        if (!all_finite(decoded_result)) return false;

        kind_out = decoded_kind;
        input_hash_out = header["input_hash"].get<std::string>();
        result_out = std::move(decoded_result);
        return true;
    } catch (...) {
        return false;
    }
}

std::string study_result_entry(const std::string& study_id) {
    return "studies/" + study_id + "/results.npz";
}

void erase_study_results(std::map<std::string, std::string>& extra) {
    for (auto it = extra.begin(); it != extra.end();) {
        const std::string& n = it->first;
        // studies/<id>/results.npz  or  studies/<id>/results.*
        if (n.rfind("studies/", 0) == 0 && n.find("/results.") != std::string::npos)
            it = extra.erase(it);
        else
            ++it;
    }
}

bool put_study_result(std::map<std::string, std::string>& extra, const std::string& study_id,
                      Kind kind, const std::string& input_hash, const json& result) {
    if (study_id.empty()) return false;
    const std::string key = study_result_entry(study_id);
    extra.erase(key);  // erase-first
    std::string blob = encode_result_blob(kind, input_hash, result);
    if (blob.empty()) return false;
    extra[key] = std::move(blob);
    return true;
}

bool get_study_result(const std::map<std::string, std::string>& extra,
                      const std::string& study_id, Kind& kind_out, std::string& input_hash_out,
                      json& result_out) {
    auto it = extra.find(study_result_entry(study_id));
    if (it == extra.end()) return false;
    return decode_result_blob(it->second, kind_out, input_hash_out, result_out);
}

}  // namespace chudware::sim::cache
