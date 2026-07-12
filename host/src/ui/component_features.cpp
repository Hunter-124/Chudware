#include "component_features.h"

#include <cstring>
#include <filesystem>
#include <unordered_map>

#include <nlohmann/json.hpp>
#include "miniz.h"

namespace ui {
namespace {

// One cache slot per component `.step+` path. `mtime` keys invalidation: when the file on
// disk is re-saved (a component edited via Open Part), its mtime changes and we re-parse.
struct Slot {
    std::filesystem::file_time_type mtime{};
    std::vector<ComponentFeature> feats;
};

// GL-thread-only (componentFeatures is called from the feature-tree draw, all on the GL
// thread) — no lock needed. The empty sentinel is returned by const-ref on any miss.
std::unordered_map<std::string, Slot>& cache() {
    static std::unordered_map<std::string, Slot> c;
    return c;
}
const std::vector<ComponentFeature>& emptyFeats() {
    static const std::vector<ComponentFeature> e;
    return e;
}

// Extract a single named entry from a zip into `out`. Returns false if the archive can't
// be opened or the entry is missing. `.step+` is a zip container (thumbnail.png, meta.json,
// document.json, cache.aicm, cache.json); document.json is DEFLATE-compressed, so miniz
// (which owns inflate) does the decode.
bool readZipEntry(const std::string& zipPath, const char* entry, std::string& out) {
    mz_zip_archive z;
    std::memset(&z, 0, sizeof(z));
    if (!mz_zip_reader_init_file(&z, zipPath.c_str(), 0)) return false;
    bool ok = false;
    int idx = mz_zip_reader_locate_file(&z, entry, nullptr, 0);
    if (idx >= 0) {
        size_t sz = 0;
        void* p = mz_zip_reader_extract_to_heap(&z, (mz_uint)idx, &sz, 0);
        if (p) {
            out.assign(static_cast<const char*>(p), sz);
            mz_free(p);
            ok = true;
        }
    }
    mz_zip_reader_end(&z);
    return ok;
}

std::vector<ComponentFeature> parse(const std::string& absPath, std::string* outErr) {
    std::vector<ComponentFeature> feats;
    std::string doc;
    if (!readZipEntry(absPath, "document.json", doc)) {
        if (outErr) *outErr = "no document.json in " + absPath;
        return feats;
    }
    nlohmann::json j;
    try {
        j = nlohmann::json::parse(doc);
    } catch (const std::exception& e) {
        if (outErr) *outErr = std::string("bad document.json: ") + e.what();
        return feats;
    }
    // Native `.step+` document.json carries a flat "features" array of
    // {id, name, type, ...}. Tolerant of missing name/type (fall back to id/"feature").
    if (j.contains("features") && j["features"].is_array()) {
        for (const auto& f : j["features"]) {
            if (!f.is_object()) continue;
            ComponentFeature cf;
            cf.id   = f.value("id", std::string());
            cf.name = f.value("name", std::string());
            cf.type = f.value("type", std::string());
            if (cf.name.empty()) cf.name = cf.id.empty() ? std::string("feature") : cf.id;
            feats.push_back(std::move(cf));
        }
    }
    return feats;
}

} // namespace

const std::vector<ComponentFeature>& componentFeatures(const std::string& absPath,
                                                       std::string* outErr) {
    if (absPath.empty()) return emptyFeats();
    std::error_code ec;
    auto mt = std::filesystem::last_write_time(absPath, ec);
    if (ec) {
        if (outErr) *outErr = "missing file: " + absPath;
        return emptyFeats();
    }
    auto& c = cache();
    auto it = c.find(absPath);
    if (it != c.end() && it->second.mtime == mt) return it->second.feats;  // fresh cache hit
    // Miss (new path) or stale (file re-saved): re-parse and store under the current mtime.
    Slot slot;
    slot.mtime = mt;
    slot.feats = parse(absPath, outErr);
    auto res = c.insert_or_assign(absPath, std::move(slot));
    return res.first->second.feats;
}

} // namespace ui
