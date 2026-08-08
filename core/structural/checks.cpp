// Demand/Capacity ratio post-processor — see checks.h. Direct port of
// engine_oracle/structural/checks.py.

#include "structural/checks.h"

#include <algorithm>
#include <cctype>
#include <cmath>
#include <cstring>
#include <stdexcept>
#include <utility>
#include <vector>

namespace chudware::structural {

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
const std::pair<const char*, double> kWoodFt[] = {
    {"df", 3.964}, {"df_no2", 3.964}, {"douglas", 3.964}, {"fir", 3.964},
    {"spf", 3.102}, {"cedar", 2.930}, {"lumber", 3.964},  {"wood", 3.964},
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

bool is_wood(const std::string& key) {
    for (const char* prefix : {"df", "douglas", "fir", "spf", "cedar", "lumber", "wood"}) {
        if (starts_with(key, prefix)) return true;
    }
    return false;
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
    if (!std::isfinite(force_n) || !std::isfinite(area_mm2) || area_mm2 <= 0.0 ||
        !std::isfinite(fy_mpa) || fy_mpa <= 0.0 || !std::isfinite(phi) || phi <= 0.0) {
        throw std::invalid_argument("axial D/C inputs and capacity must be finite and positive");
    }
    return std::abs(force_n) / (phi * area_mm2 * fy_mpa);
}

double dc_ratio_bending(double moment_nmm, double sx_mm3, double fy_mpa, double phi) {
    if (!std::isfinite(moment_nmm) || !std::isfinite(sx_mm3) || sx_mm3 <= 0.0 ||
        !std::isfinite(fy_mpa) || fy_mpa <= 0.0 || !std::isfinite(phi) || phi <= 0.0) {
        throw std::invalid_argument("bending D/C inputs and capacity must be finite and positive");
    }
    return std::abs(moment_nmm) / (phi * sx_mm3 * fy_mpa);
}

double dc_ratio_axial_wood(double force_n, double area_mm2, double fc_mpa) {
    if (!std::isfinite(force_n) || !std::isfinite(area_mm2) || area_mm2 <= 0.0 ||
        !std::isfinite(fc_mpa) || fc_mpa <= 0.0) {
        throw std::invalid_argument("wood axial D/C inputs and capacity must be finite and positive");
    }
    return std::abs(force_n) / (fc_mpa * area_mm2);
}

double dc_ratio_bending_wood(double moment_nmm, double sx_mm3, double fb_mpa) {
    if (!std::isfinite(moment_nmm) || !std::isfinite(sx_mm3) || sx_mm3 <= 0.0 ||
        !std::isfinite(fb_mpa) || fb_mpa <= 0.0) {
        throw std::invalid_argument("wood bending D/C inputs and capacity must be finite and positive");
    }
    return std::abs(moment_nmm) / (fb_mpa * sx_mm3);
}

nlohmann::json CheckResult::to_dict() const {
    return {{"element_id", element_id},
            {"combo", combo},
            {"dc_ratio", dc_ratio},
            {"passed", passed},
            {"governing_limit", governing_limit},
            {"dc_axial", dc_axial},
            {"dc_bending", dc_bending},
            {"dc_bending_major", dc_bending_major},
            {"dc_bending_minor", dc_bending_minor},
            {"dc_strength", dc_strength},
            {"disclaimer", kCheckDisclaimer}};
}

CheckResult check_member(const nlohmann::json& member_result, const nlohmann::json& section,
                         const std::string& material) {
    const std::string key = key_of(material);
    if (!is_steel(key) && !is_wood(key)) {
        throw std::invalid_argument("no structural strength check is available for material '" +
                                    material + "'");
    }
    if (!member_result.contains("axial_n") || !member_result.contains("moment_nmm")) {
        throw std::invalid_argument("member result is missing force demand channels");
    }
    const double axial_n = member_result["axial_n"].get<double>();
    const double moment_legacy = member_result["moment_nmm"].get<double>();
    const double moment_y = member_result.value("moment_y_nmm", 0.0);
    const double moment_z = member_result.contains("moment_z_nmm")
                                ? member_result["moment_z_nmm"].get<double>()
                                : moment_legacy;
    if (!section.contains("A") || !section.contains("Sx")) {
        throw std::invalid_argument("section is missing A or Sx capacity properties");
    }
    const double area_mm2 = section["A"].get<double>();
    const double sx_mm3 = section["Sx"].get<double>();
    const double sy_mm3 = section.value("Sy", sx_mm3);

    CheckResult r;
    r.element_id = member_result.value("element_id", std::string(""));
    r.combo = member_result.value("combo", std::string(""));

    std::string lim_ax, lim_bm, lim_combined;
    if (is_steel(key)) {
        const double fy = table_lookup(kSteelFy, key, 248.0);
        r.dc_axial = dc_ratio_axial(axial_n, area_mm2, fy);
        r.dc_bending_major = dc_ratio_bending(moment_z, sx_mm3, fy);
        r.dc_bending_minor = dc_ratio_bending(moment_y, sy_mm3, fy);
        r.dc_bending = r.dc_bending_major + r.dc_bending_minor;
        // AISC H1-1 interaction plus the independent axial/bending limits.
        const double interaction = r.dc_axial >= 0.2
                                       ? r.dc_axial + (8.0 / 9.0) * r.dc_bending
                                       : 0.5 * r.dc_axial + r.dc_bending;
        r.dc_strength = std::max({r.dc_axial, r.dc_bending, interaction});
        lim_ax = "axial_yield_LRFD";
        lim_bm = "bending_yield_LRFD";
        lim_combined = "combined_axial_bending_LRFD";
    } else {
        const double fb = table_lookup(kWoodFb, key, 6.205);
        const double axial_allowable =
            axial_n > 0.0 ? table_lookup(kWoodFt, key, 3.964)
                          : table_lookup(kWoodFc, key, 9.307);
        r.dc_axial = dc_ratio_axial_wood(axial_n, area_mm2, axial_allowable);
        r.dc_bending_major = dc_ratio_bending_wood(moment_z, sx_mm3, fb);
        r.dc_bending_minor = dc_ratio_bending_wood(moment_y, sy_mm3, fb);
        r.dc_bending = r.dc_bending_major + r.dc_bending_minor;
        r.dc_strength = r.dc_axial + r.dc_bending;
        lim_ax = axial_n > 0.0 ? "axial_tension_ASD_NDS" : "axial_compression_ASD_NDS";
        lim_bm = "bending_ASD_NDS";
        lim_combined = "combined_axial_bending_ASD_NDS";
    }
    r.dc_ratio = r.dc_strength;
    if (r.dc_strength == r.dc_axial && r.dc_axial >= r.dc_bending) {
        r.governing_limit = lim_ax;
    } else if (r.dc_strength == r.dc_bending && r.dc_bending >= r.dc_axial) {
        r.governing_limit = lim_bm;
    } else {
        r.governing_limit = lim_combined;
    }
    r.passed = r.dc_ratio <= 1.0;
    return r;
}

}  // namespace chudware::structural
