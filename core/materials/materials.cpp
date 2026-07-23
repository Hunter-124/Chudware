// core/materials/materials.cpp — see materials.h. Mirrors
// engine_oracle/materials/registry.py: the alias index (key/name/aliases, lower-cased,
// later rows overwrite), the FEA legacy-shape seam and the CAM fold-in seam.
#include "materials/materials.h"

#include <cctype>
#include <cstdlib>
#include <filesystem>
#include <fstream>

namespace aicad {
namespace materials {

const char* const kCamFields[10] = {"sfm_mill",     "sfm_drill",   "sfm_turn",
                                    "hss_factor",   "fz_ref",      "fz_ref_dia",
                                    "stepover_frac", "stepdown_frac", "plunge_frac",
                                    "cut_feed_ref"};

namespace {

using ojson = nlohmann::ordered_json;

// Resolve registry.json exactly like core/cam/feeds.cpp::resolve_registry_path:
// env override first (tests + explicit installs), then the %APPDATA%/AI-CAD (XDG on
// *nix) data root. Empty path when no root is discoverable.
std::filesystem::path resolve_registry_path() {
    namespace fs = std::filesystem;
    if (const char* env = std::getenv("AICAD_MATERIALS_REGISTRY"); env && env[0])
        return fs::path(env);
    fs::path root;
#ifdef _WIN32
    if (const char* base = std::getenv("APPDATA"); base && base[0]) root = fs::path(base) / "AI-CAD";
#else
    if (const char* xdg = std::getenv("XDG_DATA_HOME"); xdg && xdg[0])
        root = fs::path(xdg) / "AI-CAD";
    else if (const char* home = std::getenv("HOME"); home && home[0])
        root = fs::path(home) / ".local" / "share" / "AI-CAD";
#endif
    if (!root.empty()) {
        const fs::path p = root / "materials" / "registry.json";
        if (fs::exists(p)) return p;
    }
#ifdef AICAD_BUNDLED_MATERIALS_REGISTRY
    return fs::path(AICAD_BUNDLED_MATERIALS_REGISTRY);  // bundled repo seed (last resort)
#else
    return {};
#endif
}

std::string lower(const std::string& s) {
    std::string out = s;
    for (char& ch : out) ch = static_cast<char>(std::tolower(static_cast<unsigned char>(ch)));
    return out;
}

std::string strip(const std::string& s) {
    const size_t first = s.find_first_not_of(" \t\n\r\f\v");
    if (first == std::string::npos) return "";
    const size_t last = s.find_last_not_of(" \t\n\r\f\v");
    return s.substr(first, last - first + 1);
}

// The parsed registry doc, cached once (the Python oracle lru_caches the same way).
// ordered_json internally so iteration order matches the JSON file (alias-overwrite
// order, fea_table / cam_materials row order). Absent/unparseable -> null.
const ojson& doc() {
    static const ojson d = [] {
        const std::filesystem::path p = resolve_registry_path();
        if (p.empty()) return ojson();
        std::ifstream f(p);
        if (!f.good()) return ojson();
        try {
            return ojson::parse(f);
        } catch (...) {
            return ojson();
        }
    }();
    return d;
}

const ojson& mats() {
    static const ojson empty = ojson::object();
    const ojson& d = doc();
    if (!d.is_object() || !d.contains("materials")) return empty;
    return d["materials"];
}

// Lower-cased alias/name/key -> canonical key, built once from the seed (file order,
// later rows overwrite — Python dict semantics).
const ojson& alias_index() {
    static const ojson idx = [] {
        ojson out = ojson::object();
        for (auto it = mats().begin(); it != mats().end(); ++it) {
            const std::string key = it.key();
            const ojson& rec = it.value();
            out[lower(key)] = key;
            if (rec.contains("name") && rec["name"].is_string())
                out[strip(lower(rec["name"].get<std::string>()))] = key;
            if (rec.contains("aliases") && rec["aliases"].is_array())
                for (const ojson& a : rec["aliases"])
                    if (a.is_string()) out[strip(lower(a.get<std::string>()))] = key;
        }
        return out;
    }();
    return idx;
}

// ordered -> plain json boundary conversion (the registry is ~25 KB; records are tiny).
json plain(const ojson& v) { return json::parse(v.dump()); }

}  // namespace

std::string registry_path() { return resolve_registry_path().string(); }

bool available() { return doc().is_object(); }

json all_materials() { return plain(mats()); }

std::string resolve_key(const std::string& name) {
    if (name.empty()) return "";
    const std::string k = strip(lower(name));
    const auto it = alias_index().find(k);
    if (it == alias_index().end() || !it->is_string()) return "";
    return it->get<std::string>();
}

json get(const std::string& name) {
    const std::string key = resolve_key(name);
    if (key.empty()) return json();
    ojson rec = mats()[key];
    rec["key"] = key;
    return plain(rec);
}

json density(const std::string& name) {
    const json rec = get(name);
    if (rec.is_null()) return json();
    const auto it = rec.find("mechanical");
    if (it == rec.end() || !it->is_object()) return json();
    const auto d = it->find("density_g_cm3");
    return d == it->end() ? json() : *d;
}

json fea_table() {
    const ojson& d = doc();
    ojson out = ojson::object();
    if (!d.is_object() || !d.contains("fea_canonical")) return json::object();
    for (auto it = d["fea_canonical"].begin(); it != d["fea_canonical"].end(); ++it) {
        const ojson& rec = mats()[it.value().get<std::string>()];
        const ojson& mech = rec["mechanical"];
        ojson row = {{"name", rec["name"]}};
        for (const char* f : {"E_mpa", "nu", "density_g_cm3", "yield_mpa", "ultimate_mpa",
                              "endurance_mpa"})
            row[f] = mech[f];
        out[it.key()] = std::move(row);
    }
    return plain(out);
}

json fea_aliases() {
    const ojson& d = doc();
    if (!d.is_object() || !d.contains("fea_aliases")) return json::object();
    return plain(d["fea_aliases"]);
}

json cam_data(const std::string& name) {
    const json rec = get(name);
    if (rec.is_null()) return json();
    const auto it = rec.find("cam");
    if (it == rec.end() || !it->is_object()) return json();
    return *it;
}

std::string cam_material_id(const std::string& name) {
    const json cam = cam_data(name);
    if (cam.is_null()) return "";
    const auto it = cam.find("id");
    if (it == cam.end() || !it->is_string() || it->get<std::string>().empty()) return "";
    return it->get<std::string>();
}

json cam_materials() {
    ojson out = ojson::object();
    for (auto it = mats().begin(); it != mats().end(); ++it) {
        const auto c = it.value().find("cam");
        if (c != it.value().end() && c->is_object()) out[it.key()] = *c;
    }
    return plain(out);
}

bool call(const std::string& method, const json& params, json& result, std::string& err) {
    err.clear();
    const auto name_arg = [&params]() -> std::string {
        const auto it = params.find("name");
        return it != params.end() && it->is_string() ? it->get<std::string>() : "";
    };
    json value;
    if (method == "material_get")
        value = get(name_arg());
    else if (method == "material_density")
        value = density(name_arg());
    else if (method == "material_resolve_key") {
        const std::string k = resolve_key(name_arg());
        value = k.empty() ? json() : json(k);
    } else if (method == "material_fea_table")
        value = fea_table();
    else if (method == "material_fea_aliases")
        value = fea_aliases();
    else if (method == "material_cam_data")
        value = cam_data(name_arg());
    else if (method == "material_cam_id") {
        const std::string id = cam_material_id(name_arg());
        value = id.empty() ? json() : json(id);
    } else if (method == "material_cam_materials")
        value = cam_materials();
    else if (method == "material_all")
        value = all_materials();
    else
        return false;
    result = {{"ok", true}, {"result", std::move(value)}};
    return true;
}

}  // namespace materials
}  // namespace aicad
