// Demand/Capacity ratio post-processor — C++ port of
// engine_oracle/structural/checks.py: simplified AISC LRFD (steel) and NDS
// ASD (wood) member checks consuming solver output.
//
// Steel — AISC LRFD elastic (φFy, φ = 0.90): axial squash + bending first
// yield. Wood — NDS ASD (Fb / Fc public-domain values; no load-duration or
// wet-service adjustments). These are preliminary estimates only — the
// disclaimer string is byte-exact and non-negotiable.
#pragma once

#include <string>

#include <nlohmann/json.hpp>

namespace chudware::structural {

extern const char* kCheckDisclaimer;  // byte-exact DISCLAIMER

// One member's D/C result (mirrors CheckResult.to_dict()).
struct CheckResult {
    std::string element_id;
    std::string combo;
    double dc_ratio = 0.0;
    bool passed = false;
    std::string governing_limit;
    double dc_axial = 0.0;
    double dc_bending = 0.0;
    double dc_bending_major = 0.0;
    double dc_bending_minor = 0.0;
    double dc_strength = 0.0;

    nlohmann::json to_dict() const;
};

double dc_ratio_axial(double force_n, double area_mm2, double fy_mpa, double phi = 0.90);
double dc_ratio_bending(double moment_nmm, double sx_mm3, double fy_mpa, double phi = 0.90);
double dc_ratio_axial_wood(double force_n, double area_mm2, double fc_mpa);
double dc_ratio_bending_wood(double moment_nmm, double sx_mm3, double fb_mpa);

// Run axial + bending D/C checks for one analysis row against a section dict
// (from get_aisc_section / get_lumber_section — must carry "A" and "Sx") and a
// material key. Mirrors checks.check_member: member_result needs element_id,
// combo, axial_n, moment_nmm.
CheckResult check_member(const nlohmann::json& member_result, const nlohmann::json& section,
                         const std::string& material);

}  // namespace chudware::structural
