// sim_cache.cpp — see sim_cache.h.
#include "sim/sim_cache.h"

#include "util/resource_governor.h"

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <list>
#include <mutex>
#include <sstream>
#include <unordered_map>
#include <vector>

#if !defined(_WIN32)
#  include <unistd.h>
#else
#  include <process.h>
#endif
namespace aicad::sim::cache {

using nlohmann::json;
namespace fs = std::filesystem;

namespace {

constexpr const char* kBlobMagic = "AICRES1";
constexpr const char* kCacheSchema = "aicad-simcache/1";
constexpr size_t kMemCapEntries = 64;

// ── FNV-1a (same constants as stepplus_doc_hash) ────────────────────────────
std::string fnv1a_hex(const std::string& s) {
    uint64_t h = 1469598103934665603ULL;
    for (unsigned char c : s) {
        h ^= c;
        h *= 1099511628211ULL;
    }
    char buf[17];
    std::snprintf(buf, sizeof(buf), "%016llx", (unsigned long long)h);
    return std::string(buf);
}


// Round floats for a deterministic canonical dump (mirrors stepplus).
json round_floats(const json& v, int decimals = 6) {
    const double factor = std::pow(10.0, decimals);
    if (v.is_number_float()) {
        double d = v.get<double>();
        if (!std::isfinite(d)) return v;
        return json(std::round(d * factor) / factor);
    }
    if (v.is_array()) {
        json out = json::array();
        for (const auto& elem : v) out.push_back(round_floats(elem, decimals));
        return out;
    }
    if (v.is_object()) {
        json out = json::object();
        for (auto it = v.begin(); it != v.end(); ++it) out[it.key()] = round_floats(it.value(), decimals);
        return out;
    }
    return v;
}

std::string canonical_dump(const json& v) { return round_floats(v).dump(); }

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
    Kind kind = Kind::FeaStatic;
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

bool low_mem() { return aicad::governor::low_memory_now(); }

// STEP content fingerprint (path string when unreadable — still deterministic).
std::string step_content_fingerprint(const std::string& path) {
    std::ifstream f(path, std::ios::binary | std::ios::ate);
    if (!f) return std::string("path:") + path;
    const auto sz = f.tellg();
    if (sz < 0) return std::string("path:") + path;
    f.seekg(0);
    std::string bytes(static_cast<size_t>(sz), '\0');
    if (!f.read(bytes.data(), sz)) return std::string("path:") + path;
    return std::string("bytes:") + fnv1a_hex(bytes);
}

// Sidecar path for a key.
fs::path sidecar_file(const std::string& dir, Kind kind, const std::string& input_hash) {
    return fs::path(dir) / (kind_tag(kind) + "_" + input_hash + ".json");
}

bool write_sidecar(const std::string& dir, Kind kind, const std::string& input_hash,
                   const json& result) {
    if (dir.empty()) return false;
    std::error_code ec;
    fs::create_directories(dir, ec);
    const fs::path p = sidecar_file(dir, kind, input_hash);
    const fs::path tmp = fs::path(p.string() + ".tmp");
    json envelope = {{"engine", sim_cache_engine_key()},
                     {"kind", kind_tag(kind)},
                     {"input_hash", input_hash},
                     {"solved", true},
                     {"result", result}};
    {
        std::ofstream out(tmp, std::ios::binary | std::ios::trunc);
        if (!out) return false;
        out << envelope.dump();
        if (!out) return false;
    }
    fs::rename(tmp, p, ec);
    if (ec) {
        fs::remove(tmp, ec);
        return false;
    }
    return true;
}

bool read_sidecar(const std::string& dir, Kind kind, const std::string& input_hash,
                  json& result_out) {
    if (dir.empty()) return false;
    const fs::path p = sidecar_file(dir, kind, input_hash);
    std::ifstream in(p, std::ios::binary);
    if (!in) return false;
    std::string bytes((std::istreambuf_iterator<char>(in)), std::istreambuf_iterator<char>());
    try {
        json env = json::parse(bytes);
        if (env.value("engine", std::string()) != sim_cache_engine_key()) return false;
        if (!env.value("solved", false)) return false;
        if (env.value("input_hash", std::string()) != input_hash) return false;
        if (env.value("kind", std::string()) != kind_tag(kind)) return false;
        if (!env.contains("result")) return false;
        result_out = env["result"];
        return true;
    } catch (...) {
        return false;
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
    json envelope = {{"kind", kind_tag(kind)}, {"inputs", inputs}};
    return fnv1a_hex(canonical_dump(envelope));
}
std::string hash_fea_study(Kind kind, const json& study) {
    json s = study.is_object() ? study : json::object();
    // Fold STEP file contents (not the path) so a rewritten temp path still hits.
    if (s.contains("step_path") && s["step_path"].is_string()) {
        const std::string path = s["step_path"].get<std::string>();
        if (!path.empty()) {
            s["step_content"] = step_content_fingerprint(path);
            s.erase("step_path");
        }
    }
    // Cache-control / progress side-channels must not change the physics key.
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
    if (input_hash.empty()) {
        L.reason = "miss";
        auto& st = store_state();
        std::lock_guard<std::mutex> lk(st.mtx);
        ++st.stats.misses;
        return L;
    }
    if (low_mem()) {
        L.reason = "low_memory";
        auto& st = store_state();
        std::lock_guard<std::mutex> lk(st.mtx);
        ++st.stats.misses;
        return L;
    }
    auto& st = store_state();
    {
        std::lock_guard<std::mutex> lk(st.mtx);
        auto it = st.map.find(input_hash);
        if (it != st.map.end() && it->second.kind == kind) {
            L.hit = true;
            L.result = it->second.result;
            L.reason = "hit";
            touch_lru(st, input_hash);
            ++st.stats.hits;
            return L;
        }
    }
    // Sidecar fallback.
    json side;
    if (read_sidecar(st.sidecar.empty() ? sidecar_dir() : st.sidecar, kind, input_hash, side)) {
        // Promote into memory.
        {
            std::lock_guard<std::mutex> lk(st.mtx);
            st.map[input_hash] = Entry{kind, side};
            touch_lru(st, input_hash);
            ++st.stats.hits;
        }
        L.hit = true;
        L.result = std::move(side);
        L.reason = "hit";
        return L;
    }
    {
        std::lock_guard<std::mutex> lk(st.mtx);
        ++st.stats.misses;
    }
    L.reason = "miss";
    return L;
}

bool store(Kind kind, const std::string& input_hash, const json& result) {
    if (input_hash.empty()) return false;
    if (low_mem()) {
        auto& st = store_state();
        std::lock_guard<std::mutex> lk(st.mtx);
        ++st.stats.drops;
        return false;
    }
    // Cap: refuse oversized results (same 20 MB study cap).
    const std::string dumped = result.dump();
    if (dumped.size() > kMaxStudyResultBytes) {
        auto& st = store_state();
        std::lock_guard<std::mutex> lk(st.mtx);
        ++st.stats.drops;
        return false;
    }
    std::string dir;
    {
        auto& st = store_state();
        std::lock_guard<std::mutex> lk(st.mtx);
        st.map[input_hash] = Entry{kind, result};
        touch_lru(st, input_hash);
        ++st.stats.stores;
        dir = st.sidecar;
    }
    // Best-effort sidecar (non-fatal).
    if (dir.empty()) dir = sidecar_dir();
    write_sidecar(dir, kind, input_hash, result);
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
    if (const char* env = std::getenv("AICAD_SIM_CACHE_DIR")) {
        if (env[0]) return std::string(env);
    }
    // Process-private temp: $TMPDIR/aicad-sim-cache-<pid>
    fs::path base;
    if (const char* t = std::getenv("TMPDIR")) base = t;
    else if (const char* t = std::getenv("TEMP")) base = t;
    else base = "/tmp";
    return (base / ("aicad-sim-cache-" + std::to_string(static_cast<long long>(
#if defined(_WIN32)
                                            _getpid()
#else
                                            getpid()
#endif
                                                ))))
        .string();
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
    if (low_mem()) return {};
    json header = {{"engine", sim_cache_engine_key()},
                   {"input_hash", input_hash},
                   {"kind", kind_tag(kind)},
                   {"solved", true}};
    const std::string header_s = header.dump();
    const std::string body_s = result.dump();
    // Cap: header + body + magic + len.
    if (header_s.size() + body_s.size() + 8 + 4 > kMaxStudyResultBytes) return {};
    std::string out;
    out.reserve(8 + 4 + header_s.size() + body_s.size());
    out.append(kBlobMagic, 8);
    put_u32_le(out, static_cast<uint32_t>(header_s.size()));
    out.append(header_s);
    out.append(body_s);
    return out;
}

bool decode_result_blob(const std::string& blob, Kind& kind_out, std::string& input_hash_out,
                        json& result_out) {
    if (blob.size() < 12) return false;
    if (blob.compare(0, 8, kBlobMagic, 8) != 0) return false;
    const uint32_t hlen = get_u32_le(blob, 8);
    if (static_cast<size_t>(12) + hlen > blob.size()) return false;
    try {
        json header = json::parse(blob.substr(12, hlen));
        if (header.value("engine", std::string()) != sim_cache_engine_key()) return false;
        if (!header.value("solved", false)) return false;
        input_hash_out = header.value("input_hash", std::string());
        const std::string k = header.value("kind", std::string());
        if (k == "fea_static") kind_out = Kind::FeaStatic;
        else if (k == "fea_modal") kind_out = Kind::FeaModal;
        else if (k == "fea_thermal") kind_out = Kind::FeaThermal;
        else if (k == "spice") kind_out = Kind::Spice;
        else if (k == "cfd") kind_out = Kind::Cfd;
        else if (k == "em") kind_out = Kind::Em;
        else return false;
        result_out = json::parse(blob.substr(12 + hlen));
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

}  // namespace aicad::sim::cache
