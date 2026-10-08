// core/piping/vessel.cpp — ASME VIII-1 pressure-vessel sizing (vessel_size).
// Formulas (all in mm / MPa, corroded condition: R and D grow by CA):
//   shell  UG-27(c)(1) t = PR/(SE − 0.6P)   MAWP = SEt/(R + 0.6t)
//          UG-27(c)(2) t = PR/(2SE + 0.4P)  MAWP = 2SEt/(R − 0.4t)
//          valid for t ≤ R/2 and P ≤ 0.385SE (else Appendix 1-2 → refuse)
//   heads  UG-32(d) 2:1 ellipsoidal t = PD/(2SE − 0.2P)
//          UG-32(f) hemispherical    t = PL/(2SE − 0.2P), L = D/2,
//                                    valid for t ≤ 0.356L and P ≤ 0.665SE
//          UG-32(e) torispherical    t = 0.885PL/(SE − 0.1P), L = D, r = 0.06L
//   UG-16(b) minimum 1.5 mm exclusive of corrosion allowance.
//   UG-99(b) hydrotest = 1.3 · MAWP · (S_test / S_design).
// Design pressure is the gauge pressure at the top of the vessel; the
// static head of the vessel filled with the operating fluid is added to the
// calculation pressure (UG-22(b)) and subtracted back out of the MAWP.
#include "piping/vessel.h"

#include <algorithm>
#include <cctype>
#include <cmath>
#include <string>
#include <vector>

namespace chudware::piping {
namespace {

using pyutil::fmt_f;
using pyutil::jerr;
using pyutil::pyround;

constexpr double kPi = 3.14159265358979323846;
constexpr double kG = 9.80665;                    // m/s²
constexpr double kKsiToMpa = 6.894757293168361;   // 1 ksi in MPa
constexpr double kWaterDensity = 1000.0;          // kg/m³, hydrotest / full-water mass
constexpr double kUg16MinMm = 1.5;                // UG-16(b), exclusive of CA
constexpr double kHydroTempC = 20.0;              // ambient hydrotest temperature
constexpr double kScopeMinKpa = 103.0;            // U-1(c)(2)(h): 15 psi
constexpr double kCarbonCreepC = 425.0;           // II-D graphitization note range

// Section II-D Part D Table 1A maximum allowable stress (customary units,
// lower values for austenitic grades where slight deformation matters).
// Below the first temperature the first value applies down to t_min_c.
// Source: BPVC II-D 2023 Table 1A (values identical in the 2021 edition); the
// carbon-steel rows to 800 °F and the austenitic rows also match the II-D values
// reproduced in ASME B31.1-2022 Tables A-1 / A-3, and the creep-range carbon
// steel values match B31.3-2024 Table A-1C (A671 CC70 / A672 A55).
struct StressPoint {
    double t_f;
    double s_ksi;
};

struct VesselMaterial {
    const char* name;
    const char* compact;
    const char* description;
    double density_kgm3;
    double t_min_c;
    bool carbon_steel;
    std::vector<StressPoint> table;
};

const std::vector<VesselMaterial>& vessel_materials() {
    static const std::vector<VesselMaterial> k = {
        {"SA-516-70", "SA51670", "carbon steel plate, Grade 70 (K02700)", 7850.0, -29.0, true,
         {{100, 20.0}, {500, 20.0}, {600, 19.4}, {650, 18.8}, {700, 18.1}, {750, 14.8},
          {800, 12.0}, {850, 9.3}, {900, 6.7}, {950, 4.0}, {1000, 2.5}}},
        {"SA-285-C", "SA285C", "carbon steel plate, Grade C (K02801)", 7850.0, -29.0, true,
         {{100, 15.7}, {500, 15.7}, {600, 15.3}, {650, 14.8}, {700, 14.3}, {750, 13.0},
          {800, 10.8}, {850, 8.7}, {900, 5.9}}},
        {"SA-240-304", "SA240304", "18Cr-8Ni plate, Type 304 (S30400)", 7930.0, -196.0, false,
         {{100, 20.0}, {200, 16.7}, {300, 15.0}, {400, 13.8}, {500, 12.9}, {600, 12.3},
          {650, 12.0}, {700, 11.7}, {750, 11.5}, {800, 11.2}, {850, 11.0}, {900, 10.8},
          {950, 10.6}, {1000, 10.4}, {1050, 10.1}, {1100, 9.8}, {1150, 7.7}, {1200, 6.1}}},
        {"SA-240-316", "SA240316", "16Cr-12Ni-2Mo plate, Type 316 (S31600)", 7980.0, -196.0, false,
         {{100, 20.0}, {200, 17.3}, {300, 15.6}, {400, 14.3}, {500, 13.3}, {600, 12.6},
          {650, 12.3}, {700, 12.1}, {750, 11.9}, {800, 11.8}, {850, 11.6}, {900, 11.5},
          {950, 11.4}, {1000, 11.3}, {1050, 11.2}, {1100, 11.1}, {1150, 9.8}, {1200, 7.4}}},
    };
    return k;
}

std::string supported_materials() {
    std::string s;
    for (const auto& m : vessel_materials()) {
        if (!s.empty()) s += ", ";
        s += m.name;
    }
    return s;
}

// "SA-516 Gr.70", "sa516-70", "SA 240 TP304" → canonical compact key.
std::string compact_material(const std::string& name) {
    std::string k;
    for (unsigned char c : name) {
        if (std::isalnum(c)) k += static_cast<char>(std::toupper(c));
    }
    for (const char* tok : {"GRADE", "GR", "TYPE", "TP"}) {
        const std::string t(tok);
        for (size_t pos = k.find(t); pos != std::string::npos; pos = k.find(t)) k.erase(pos, t.size());
    }
    return k;
}

const VesselMaterial* find_material(const std::string& name) {
    const std::string key = compact_material(name);
    for (const auto& m : vessel_materials()) {
        if (key == m.compact) return &m;
    }
    return nullptr;
}

double c_to_f(double c) { return c * 9.0 / 5.0 + 32.0; }
double f_to_c(double f) { return (f - 32.0) * 5.0 / 9.0; }

// Allowable stress (MPa) at temp_c, or a refusal naming the material.
bool allowable_mpa(const VesselMaterial& m, double temp_c, double& s_out, ojson& err) {
    if (temp_c < m.t_min_c - 1e-9) {
        err = jerr("temperature_below_table",
                   std::string(m.name) + " Table 1A stresses apply down to " + fmt_f(m.t_min_c, 0) +
                       " °C; " + fmt_f(temp_c, 1) +
                       " °C needs a UCS-66/UHA-51 impact-test (MDMT) evaluation");
        return false;
    }
    const double tf = c_to_f(temp_c);
    const auto& tab = m.table;
    if (tf > tab.back().t_f + 1e-9) {
        err = jerr("temperature_above_table",
                   std::string(m.name) + " Table 1A stresses end at " +
                       fmt_f(f_to_c(tab.back().t_f), 0) + " °C; " + fmt_f(temp_c, 1) +
                       " °C is beyond the material's VIII-1 range — choose another material");
        return false;
    }
    if (tf <= tab.front().t_f) {
        s_out = tab.front().s_ksi * kKsiToMpa;
        return true;
    }
    for (size_t i = 0; i + 1 < tab.size(); ++i) {
        if (tf <= tab[i + 1].t_f) {
            const double f = (tf - tab[i].t_f) / (tab[i + 1].t_f - tab[i].t_f);
            s_out = (tab[i].s_ksi + f * (tab[i + 1].s_ksi - tab[i].s_ksi)) * kKsiToMpa;
            return true;
        }
    }
    s_out = tab.back().s_ksi * kKsiToMpa;
    return true;
}

// Standard plate thicknesses (mm, EN 10029 / mill practice).
const std::vector<double>& plate_series() {
    static const std::vector<double> k = {3,  4,  5,  6,  8,  10, 12, 14,  16,  18,  20,
                                          22, 25, 28, 30, 32, 36, 40, 45,  50,  55,  60,
                                          65, 70, 75, 80, 90, 100, 110, 120, 130, 140, 150};
    return k;
}

double round_plate(double t_mm) {
    for (double p : plate_series()) {
        if (p >= t_mm - 1e-9) return p;
    }
    return -1.0;
}

enum class HeadType { Ellipsoidal, Hemispherical, Torispherical };

struct HeadGeom {
    HeadType type;
    const char* name;
    double vol_coeff;   // inside volume = coeff · D³
    double depth_frac;  // inside dish depth = frac · D
    const char* formula;
};

// Standard F&D: crown L = D, knuckle r = 0.06D → depth = D − √((L−r)² − (D/2−r)²).
const double kToriDepth = 1.0 - std::sqrt(0.94 * 0.94 - 0.44 * 0.44);

bool head_geom(const std::string& name, HeadGeom& g) {
    if (name == "ellipsoidal_2_1") {
        g = {HeadType::Ellipsoidal, "ellipsoidal_2_1", kPi / 24.0, 0.25,
             "UG-32(d) t = PD/(2SE - 0.2P)"};
        return true;
    }
    if (name == "hemispherical") {
        g = {HeadType::Hemispherical, "hemispherical", kPi / 12.0, 0.5,
             "UG-32(f) t = PL/(2SE - 0.2P), L = D/2"};
        return true;
    }
    if (name == "torispherical") {
        g = {HeadType::Torispherical, "torispherical", 0.0809, kToriDepth,
             "UG-32(e) t = 0.885PL/(SE - 0.1P), L = D, r = 0.06L"};
        return true;
    }
    return false;
}

// Pressure thickness (mm) for a head of corroded inside diameter d at p (MPa).
double head_t(const HeadGeom& g, double p, double d, double se) {
    switch (g.type) {
        case HeadType::Ellipsoidal: return p * d / (2.0 * se - 0.2 * p);
        case HeadType::Hemispherical: return p * (d / 2.0) / (2.0 * se - 0.2 * p);
        case HeadType::Torispherical: return 0.885 * p * d / (se - 0.1 * p);
    }
    return 0.0;
}

// Head MAWP (MPa) for available thickness t (mm) on corroded diameter d.
double head_mawp(const HeadGeom& g, double t, double d, double se) {
    switch (g.type) {
        case HeadType::Ellipsoidal: return 2.0 * se * t / (d + 0.2 * t);
        case HeadType::Hemispherical: return 2.0 * se * t / (d / 2.0 + 0.2 * t);
        case HeadType::Torispherical: return se * t / (0.885 * d + 0.1 * t);
    }
    return 0.0;
}

// Present-but-invalid numbers refuse; absent/null keeps the default.
bool read_num(const ojson& p, const char* key, double& out, ojson& err) {
    auto it = p.find(key);
    if (it == p.end() || it->is_null()) return true;
    if (!it->is_number() || !std::isfinite(it->get<double>())) {
        err = jerr("invalid_parameter", std::string(key) + " must be a finite number");
        return false;
    }
    out = it->get<double>();
    return true;
}

bool has_num(const ojson& p, const char* key) {
    auto it = p.find(key);
    return it != p.end() && !it->is_null();
}

}  // namespace

ojson vessel_size(const ojson& params) {
    if (!params.is_object()) return jerr("invalid_params", "pass a JSON object of vessel parameters");
    ojson err;

    if (!has_num(params, "inner_diameter_mm")) {
        return jerr("missing_inner_diameter", "pass inner_diameter_mm (inside diameter, mm)");
    }
    double di = 0, ltt = 0, vol_req = 0, p_kpa = 0, temp_c = 40.0, ca = 3.0, e = 0.85;
    double rho_fluid = 1000.0, fill = 0.8;
    if (!read_num(params, "inner_diameter_mm", di, err)) return err;
    if (!(di > 0)) return jerr("invalid_inner_diameter", "inner_diameter_mm must be > 0");

    const bool has_len = has_num(params, "tan_tan_length_mm");
    const bool has_vol = has_num(params, "volume_m3");
    if (has_len && has_vol) {
        return jerr("conflicting_length",
                    "pass either tan_tan_length_mm or volume_m3, not both (the other is derived)");
    }
    if (!has_len && !has_vol) {
        return jerr("missing_length", "pass tan_tan_length_mm or volume_m3 (tan-tan length is then solved)");
    }
    if (has_len) {
        if (!read_num(params, "tan_tan_length_mm", ltt, err)) return err;
        if (!(ltt >= 0)) return jerr("invalid_tan_tan_length", "tan_tan_length_mm must be >= 0");
    } else {
        if (!read_num(params, "volume_m3", vol_req, err)) return err;
        if (!(vol_req > 0)) return jerr("invalid_volume", "volume_m3 must be > 0");
    }

    if (!has_num(params, "design_pressure_kpa")) {
        return jerr("missing_design_pressure", "pass design_pressure_kpa (internal gauge pressure at the top, kPa)");
    }
    if (!read_num(params, "design_pressure_kpa", p_kpa, err)) return err;
    if (p_kpa < 0) {
        return jerr("external_pressure_not_supported",
                    "vessel_size covers internal pressure only; external pressure/vacuum needs UG-28 buckling design");
    }
    if (!(p_kpa > 0)) return jerr("invalid_design_pressure", "design_pressure_kpa must be > 0");
    if (!read_num(params, "design_temp_c", temp_c, err)) return err;
    if (!read_num(params, "corrosion_allowance_mm", ca, err)) return err;
    if (ca < 0) return jerr("invalid_corrosion_allowance", "corrosion_allowance_mm must be >= 0");
    if (!read_num(params, "joint_efficiency", e, err)) return err;
    if (!(e > 0 && e <= 1.0)) {
        return jerr("invalid_joint_efficiency",
                    "joint_efficiency must be in (0, 1] (UW-12: 1.0 full RT, 0.85 spot RT, 0.70 none)");
    }
    if (!read_num(params, "fluid_density_kgm3", rho_fluid, err)) return err;
    if (rho_fluid < 0) return jerr("invalid_fluid_density", "fluid_density_kgm3 must be >= 0 (0 = gas service)");
    if (!read_num(params, "fill_fraction", fill, err)) return err;
    if (!(fill >= 0 && fill <= 1.0)) return jerr("invalid_fill_fraction", "fill_fraction must be in [0, 1]");

    std::string mat_name = "SA-516-70";
    if (params.contains("material") && !params["material"].is_null()) {
        if (!params["material"].is_string()) return jerr("invalid_parameter", "material must be a string");
        mat_name = params["material"].get<std::string>();
    }
    const VesselMaterial* mat = find_material(mat_name);
    if (mat == nullptr) {
        return jerr("unknown_material", "material '" + mat_name + "' has no Table 1A data here; supported: " +
                                            supported_materials());
    }

    std::string head_name = "ellipsoidal_2_1";
    if (params.contains("head_type") && !params["head_type"].is_null()) {
        if (!params["head_type"].is_string()) return jerr("invalid_parameter", "head_type must be a string");
        head_name = params["head_type"].get<std::string>();
    }
    HeadGeom head{};
    if (!head_geom(head_name, head)) {
        return jerr("unknown_head_type", "head_type '" + head_name +
                                             "' is not supported; use ellipsoidal_2_1, hemispherical or torispherical");
    }

    std::string orientation = "vertical";
    if (params.contains("orientation") && !params["orientation"].is_null()) {
        if (!params["orientation"].is_string()) return jerr("invalid_parameter", "orientation must be a string");
        orientation = params["orientation"].get<std::string>();
    }
    if (orientation != "vertical" && orientation != "horizontal") {
        return jerr("invalid_orientation", "orientation must be 'vertical' or 'horizontal'");
    }

    double s = 0, s_test = 0;
    if (!allowable_mpa(*mat, temp_c, s, err)) return err;
    if (!allowable_mpa(*mat, std::max(kHydroTempC, mat->t_min_c), s_test, err)) return err;

    // Geometry: inside volume and (if asked) the tan-tan length that yields it.
    const double head_vol_mm3 = head.vol_coeff * di * di * di;
    const double shell_area_mm2 = kPi * di * di / 4.0;
    if (has_vol) {
        const double shell_vol = vol_req * 1e9 - 2.0 * head_vol_mm3;
        if (shell_vol <= 0) {
            return jerr("volume_too_small_for_diameter",
                        "two " + head_name + " heads on ID " + fmt_f(di, 0) + " mm already hold " +
                            fmt_f(2.0 * head_vol_mm3 / 1e9, 4) + " m3 > " + fmt_f(vol_req, 4) +
                            " m3; reduce inner_diameter_mm");
        }
        ltt = shell_vol / shell_area_mm2;
    }
    const double head_depth = head.depth_frac * di;
    const double volume_mm3 = shell_area_mm2 * ltt + 2.0 * head_vol_mm3;

    // UG-22(b) static head: vessel full of the operating fluid, at the bottom.
    const double liquid_h_mm = orientation == "vertical" ? ltt + 2.0 * head_depth : di;
    const double static_kpa = rho_fluid * kG * (liquid_h_mm / 1000.0) / 1000.0;
    const double p_calc = (p_kpa + static_kpa) / 1000.0;  // MPa
    const double se = s * e;

    ojson warnings = ojson::array();

    // ── shell, UG-27(c) ──
    const double r_corr = di / 2.0 + ca;
    if (p_calc > 0.385 * se) {
        return jerr("thick_wall_beyond_ug27",
                    "P = " + fmt_f(p_calc * 1000.0, 1) + " kPa exceeds 0.385SE = " + fmt_f(0.385 * se * 1000.0, 1) +
                        " kPa: the UG-27(c)(1) thin-wall formula is invalid; size with Appendix 1-2 "
                        "thick-shell equations or choose a stronger material / higher joint efficiency");
    }
    const double t_circ = p_calc * r_corr / (se - 0.6 * p_calc);
    const double t_long = p_calc * r_corr / (2.0 * se + 0.4 * p_calc);
    const double t_shell_p = std::max(t_circ, t_long);
    if (t_circ > r_corr / 2.0) {
        return jerr("thick_wall_beyond_ug27",
                    "required shell thickness " + fmt_f(t_circ, 2) + " mm exceeds R/2 = " + fmt_f(r_corr / 2.0, 2) +
                        " mm; UG-27(c)(1) is invalid — use Appendix 1-2");
    }
    if (p_calc > 0.8 * 0.385 * se) {
        warnings.push_back("calculation pressure is " + fmt_f(100.0 * p_calc / (0.385 * se), 0) +
                           "% of the UG-27 thin-wall limit 0.385SE; confirm with Appendix 1-2");
    }
    const double t_shell_req = std::max(t_shell_p, kUg16MinMm) + ca;
    const double t_shell_nom = round_plate(t_shell_req);
    if (t_shell_nom < 0) {
        return jerr("plate_beyond_series", "shell needs " + fmt_f(t_shell_req, 2) +
                                               " mm, beyond the 150 mm plate series; reduce diameter or pressure");
    }
    const double t_shell_av = t_shell_nom - ca;
    const double mawp_shell = std::min(se * t_shell_av / (r_corr + 0.6 * t_shell_av),
                                       2.0 * se * t_shell_av / (r_corr - 0.4 * t_shell_av));

    // ── heads, UG-32 ──
    const double d_corr = di + 2.0 * ca;
    if (head.type == HeadType::Hemispherical && p_calc > 0.665 * se) {
        return jerr("thick_head_beyond_ug32",
                    "P exceeds 0.665SE for a hemispherical head; UG-32(f) is invalid — use Appendix 1-3");
    }
    const double t_head_p = head_t(head, p_calc, d_corr, se);
    if (head.type == HeadType::Hemispherical && t_head_p > 0.356 * (d_corr / 2.0)) {
        return jerr("thick_head_beyond_ug32",
                    "hemispherical head thickness exceeds 0.356L; UG-32(f) is invalid — use Appendix 1-3");
    }
    const double t_head_req = std::max(t_head_p, kUg16MinMm) + ca;
    const double t_head_nom = round_plate(t_head_req);
    if (t_head_nom < 0) {
        return jerr("plate_beyond_series", "heads need " + fmt_f(t_head_req, 2) +
                                               " mm, beyond the 150 mm plate series; reduce diameter or pressure");
    }
    const double t_head_av = t_head_nom - ca;
    const double mawp_head = head_mawp(head, t_head_av, d_corr, se);
    if (head.type != HeadType::Hemispherical) {
        const double crown_l = head.type == HeadType::Torispherical ? d_corr : d_corr * 0.9;  // 2:1 K-factor crown ≈ 0.9D
        if (t_head_av / crown_l < 0.002) {
            warnings.push_back("head t/L = " + fmt_f(t_head_av / crown_l, 5) +
                               " < 0.002: thin-head knuckle buckling check per Appendix 1-4(f) required");
        }
    }

    // ── MAWP (at the top, net of static head), hydrotest ──
    const bool shell_governs = mawp_shell <= mawp_head;
    const double mawp_kpa = std::min(mawp_shell, mawp_head) * 1000.0 - static_kpa;
    const double lsr = s_test / s;
    const double hydro_kpa = 1.3 * mawp_kpa * lsr;
    const double hydro_bottom_kpa = hydro_kpa + kWaterDensity * kG * (liquid_h_mm / 1000.0) / 1000.0;

    // ── masses ──
    const double shell_metal_mm3 = kPi / 4.0 * ((di + 2.0 * t_shell_nom) * (di + 2.0 * t_shell_nom) - di * di) * ltt;
    const double do_head = di + 2.0 * t_head_nom;
    const double head_metal_mm3 = head.vol_coeff * (do_head * do_head * do_head - di * di * di);
    const double steel_kg = (shell_metal_mm3 + 2.0 * head_metal_mm3) * 1e-9 * mat->density_kgm3;
    const double volume_m3 = volume_mm3 * 1e-9;
    const double operating_kg = steel_kg + fill * volume_m3 * rho_fluid;
    const double full_water_kg = steel_kg + volume_m3 * kWaterDensity;

    if (p_kpa < kScopeMinKpa) {
        warnings.push_back("design pressure below 103 kPa (15 psi): outside VIII-1 scope per U-1(c)(2)(h); "
                           "the sizing still applies but code stamping is optional");
    }
    if (mat->carbon_steel && temp_c > kCarbonCreepC) {
        warnings.push_back(std::string(mat->name) +
                           " above 425 °C: allowable is creep-governed and prolonged exposure risks graphitization "
                           "(II-D Table 1A notes)");
    }
    const double slender = ltt / di;
    if (ltt > 0 && (slender < 1.5 || slender > 6.0)) {
        warnings.push_back("tan-tan/ID = " + fmt_f(slender, 2) +
                           " is outside the usual economic range 1.5–6; consider re-proportioning");
    }

    ojson out = ojson::object();
    out["ok"] = true;
    out["code"] = "ASME VIII-1";
    ojson m = ojson::object();
    m["name"] = mat->name;
    m["description"] = mat->description;
    m["allowable_mpa"] = pyround(s, 2);
    m["allowable_test_mpa"] = pyround(s_test, 2);
    m["density_kgm3"] = mat->density_kgm3;
    m["source"] = "ASME BPVC II-D 2023 Table 1A (customary-unit values, linear interpolation)";
    out["material"] = m;
    ojson design = ojson::object();
    design["pressure_kpa"] = p_kpa;
    design["temp_c"] = temp_c;
    design["static_head_kpa"] = pyround(static_kpa, 3);
    design["calc_pressure_kpa"] = pyround(p_calc * 1000.0, 3);
    design["corrosion_allowance_mm"] = ca;
    design["joint_efficiency"] = e;
    design["orientation"] = orientation;
    design["fluid_density_kgm3"] = rho_fluid;
    design["fill_fraction"] = fill;
    out["design"] = design;
    ojson shell = ojson::object();
    shell["inner_diameter_mm"] = di;
    shell["t_pressure_mm"] = pyround(t_shell_p, 3);
    shell["t_required_mm"] = pyround(t_shell_req, 3);
    shell["t_nominal_mm"] = t_shell_nom;
    shell["mawp_kpa"] = pyround(mawp_shell * 1000.0 - static_kpa, 2);
    shell["formula"] = t_circ >= t_long ? "UG-27(c)(1) t = PR/(SE - 0.6P)" : "UG-27(c)(2) t = PR/(2SE + 0.4P)";
    out["shell"] = shell;
    ojson heads = ojson::object();
    heads["type"] = head.name;
    heads["t_pressure_mm"] = pyround(t_head_p, 3);
    heads["t_required_mm"] = pyround(t_head_req, 3);
    heads["t_nominal_mm"] = t_head_nom;
    heads["mawp_kpa"] = pyround(mawp_head * 1000.0 - static_kpa, 2);
    heads["depth_mm"] = pyround(head_depth, 2);
    heads["volume_each_m3"] = pyround(head_vol_mm3 * 1e-9, 5);
    heads["formula"] = head.formula;
    out["heads"] = heads;
    out["mawp_kpa"] = pyround(mawp_kpa, 2);
    out["governing"] = shell_governs ? "shell" : "heads";
    out["lowest_stress_ratio"] = pyround(lsr, 4);
    out["hydrotest_kpa"] = pyround(hydro_kpa, 2);
    out["hydrotest_bottom_kpa"] = pyround(hydro_bottom_kpa, 2);
    out["volume_m3"] = pyround(volume_m3, 5);
    out["tan_tan_length_mm"] = pyround(ltt, 2);
    out["overall_length_mm"] = pyround(ltt + 2.0 * (head_depth + t_head_nom), 2);
    out["mass_empty_kg"] = pyround(steel_kg, 1);
    out["mass_operating_kg"] = pyround(operating_kg, 1);
    out["mass_full_water_kg"] = pyround(full_water_kg, 1);
    out["warnings"] = warnings;
    out["hint"] =
        "pressure parts only: nozzle reinforcement (UG-37), supports (skirt/saddles/legs), wind/seismic, "
        "MDMT (UCS-66) and head forming thinning are not evaluated; order heads with minimum thickness after "
        "forming >= heads.t_required_mm";
    out["next_actions"] = ojson::array(
        {"persist the sizing with sim_study_add {type:'vessel', engine:'cpp-industrial', params, results}",
         "size nozzles and connecting lines with piping_route / piping_spec_check"});
    return out;
}

}  // namespace chudware::piping
