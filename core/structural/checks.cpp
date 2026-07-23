// Demand/Capacity ratio post-processor — see checks.h. Direct port of
// engine_oracle/structural/checks.py.

#include "structural/checks.h"

#include <algorithm>
#include <cctype>
#include <cmath>
#include <utility>
#include <vector>

namespace aicad::structural {

const char* kCheckDisclaimer =
    "Preliminary check — not sealed engineering. "
    "Verify with a licensed structural engineer before construction.";

namespace {

// Steel Fy values (MPa) — public-domain reference values.
const std::pair<const char*, double> kSteelFy[] = {
    {"steel", 248.0},       // A36 (Fy = 36 ksi)
    {"steel_a36", 248.0},
    {"a36", 248.0},
    {"steel_a992", 344.7},  // A992 (Fy = 50 ksi)
    {"a992", 344.7},
};

// NDS ASD allowable stresses (MPa) — public-domain, NDS Supplement Table 4A.
const std::pair<const char*, double> kWoodFb[] = {
    {"df", 6.205}, {"df_no2", 6.205}, {"douglas", 6.205}, {"fir", 6.205},
    {"spf", 5.516}, {"cedar", 6.205}, {"lumber", 6.205},  {"wood", 6.205},
};
const std::pair<const char*, double> kWoodFc[] = {
    {"df", 9.307}, {"df_no2", 9.307}, {"douglas", 9.307}, {"fir", 9.307},
    {"spf", 7.929}, {"cedar", 8.618}, {"lumber", 9.307},  {"wood", 9.307},
};

std::string key_of(const std::string& material) {
    // Python: (material or "").strip().lower() — trim ends only.
    const auto b = material.find_first_not_of(" \t\n\r");
    if (b == std::string::npos) return "";
    const auto e = material.find_last_not_of(" \t\n\r");
    std::string out = material.substr(b, e - b + 1);
    std::transform(out.begin(), out.end(), out.begin(),
                   [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
    return out;
}

bool starts_with(const std::string& s, const std::string& prefix) {
    return s.size() >= prefix.size() && s.compare(0, prefix.size(), prefix) == 0;
}

bool is_steel(const std::string& key) {
    return starts_with(key, "steel") || starts_with(key, "a36") || starts_with(key, "a992");
}

// Longest-key-first prefix match (mirrors sorted(dict, key=len, reverse=True)).
// Python's sort is stable, so equal-length keys keep dict insertion order.
template <size_t N>
double table_lookup(const std::pair<const char*, double> (&table)[N], const std::string& key,
                    double fallback) {
    std::vector<const std::pair<const char*, double>*> entries;
    for (const auto& e : table) entries.push_back(&e);
    std::stable_sort(entries.begin(), entries.end(), [](const auto* a, const auto* b) {
        return std::strlen(a->first) > std::strlen(b->first);
    });
    for (const auto* e : entries) {
        if (key == e->first || starts_with(key, e->first)) return e->second;
    }
    return fallback;
}

}  // namespace

double dc_ratio_axial(double force_n, double area_mm2, double fy_mpa, double phi) {
    const double capacity = phi * area_mm2 * fy_mpa;
    if (capacity == 0.0) return 0.0;
    return std::abs(force_n) / capacity;
}

double dc_ratio_bending(double moment_nmm, double sx_mm3, double fy_mpa, double phi) {
    const double capacity = phi * sx_mm3 * fy_mpa;
    if (capacity == 0.0) return 0.0;
    return std::abs(moment_nmm) / capacity;
}

double dc_ratio_axial_wood(double force_n, double area_mm2, double fc_mpa) {
    const double capacity = fc_mpa * area_mm2;
    if (capacity == 0.0) return 0.0;
    return std::abs(force_n) / capacity;
}

double dc_ratio_bending_wood(double moment_nmm, double sx_mm3, double fb_mpa) {
    const double capacity = fb_mpa * sx_mm3;
    if (capacity == 0.0) return 0.0;
    return std::abs(moment_nmm) / capacity;
}

nlohmann::json CheckResult::to_dict() const {
    return {{"element_id", element_id},   {"combo", combo},
            {"dc_ratio", dc_ratio},       {"passed", passed},
            {"governing_limit", governing_limit},
            {"dc_axial", dc_axial},       {"dc_bending", dc_bending},
            {"disclaimer", kCheckDisclaimer}};
}

CheckResult check_member(const nlohmann::json& member_result, const nlohmann::json& section,
                         const std::string& material) {
    const std::string key = key_of(material);
    const double axial_n = member_result.value("axial_n", 0.0);
    const double moment_nmm = member_result.value("moment_nmm", 0.0);
    const double area_mm2 = section.value("A", 0.0);
    const double sx_mm3 = section.value("Sx", 0.0);

    CheckResult r;
    r.element_id = member_result.value("element_id", std::string(""));
    r.combo = member_result.value("combo", std::string(""));

    std::string lim_ax, lim_bm;
    if (is_steel(key)) {
        const double fy = table_lookup(kSteelFy, key, 248.0);
        r.dc_axial = dc_ratio_axial(axial_n, area_mm2, fy);
        r.dc_bending = dc_ratio_bending(moment_nmm, sx_mm3, fy);
        lim_ax = "axial_yield_LRFD";
        lim_bm = "bending_yield_LRFD";
    } else {
        // NDS ASD wood model for all non-steel materials (oracle default).
        const double fb = table_lookup(kWoodFb, key, 6.205);
        const double fc = table_lookup(kWoodFc, key, 9.307);
        r.dc_axial = dc_ratio_axial_wood(axial_n, area_mm2, fc);
        r.dc_bending = dc_ratio_bending_wood(moment_nmm, sx_mm3, fb);
        lim_ax = "axial_compression_ASD_NDS";
        lim_bm = "bending_ASD_NDS";
    }
    if (r.dc_bending >= r.dc_axial) {
        r.governing_limit = lim_bm;
        r.dc_ratio = r.dc_bending;
    } else {
        r.governing_limit = lim_ax;
        r.dc_ratio = r.dc_axial;
    }
    r.passed = r.dc_ratio <= 1.0;
    return r;
}

}  // namespace aicad::structural
