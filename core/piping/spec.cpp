// core/piping/spec.cpp — piping_spec_check: ASME B31.3 §304.1.2 eq. (3a)
// pressure design, B16.5 flange P-T rating, sustained-load support span.
#include "piping/spec.h"

#include <algorithm>
#include <cctype>
#include <cmath>
#include <map>
#include <string>
#include <vector>

#include "piping/catalog.h"
#include "piping/schema.h"

namespace chudware::piping {
namespace {

using pyutil::jerr;
using pyutil::pyround;

constexpr double kPi = 3.14159265358979323846;
constexpr double kKsiToMpa = 6.894757293168361;
constexpr double kG = 9.80665;
constexpr double kY = 0.4;          // B31.3 Table 304.1.1, ferritic/austenitic < 482 °C
constexpr double kW = 1.0;          // weld strength reduction, <= 427 °C (302.3.5(e))
constexpr double kSagLimitM = 0.0025;  // 2.5 mm mid-span deflection limit

double f_to_c(double f) { return (f - 32.0) / 1.8; }

struct Material {
    const char* name;
    double min_temp_c;              // B31.3 Table A-1 min temp (impact-test exemption)
    std::vector<double> temp_f;     // Table A-1 temperature columns
    std::vector<double> s_ksi;      // basic allowable stress S at each column
    const char* group;              // B16.5 material group of matching flanges
    double modulus_gpa;             // B31.3 Table C-6 at 21 °C
    double density;                 // kg/m3
    bool stainless;
};

// Sources (checked 2026-10 against the published code text):
//   carbon steel — ASME B31.3-2024 Table A-1C (U.S. Customary; called Table A-1 in
//     B31.3-2020, same values), lines A53 B / A106 B / A333 6, which are identical.
//     An older-edition row {…, 18.9, 17.3, 17.0, 16.5, 13.0, 10.8} is superseded.
//   austenitic — BPVC II-D (2023) Table 1A lower (non-G5) row for SA-312 TP304 /
//     TP316, reproduced in ASME B31.1-2022 Table A-3. B31.3-2024 Table A-1C lists
//     only the higher 90 %-of-yield row (TP304 20.0 20.0 20.0 18.6 17.5 16.6 16.2
//     15.8 15.5 15.2; TP316 20.0 20.0 20.0 19.3 18.0 17.0 16.6 16.3 16.1 15.9);
//     the lower row is the deliberate conservative choice documented in spec.h.
const std::vector<double> kTempF = {100, 200, 300, 400, 500, 600, 650, 700, 750, 800};
// SMYS 35 ksi / SMTS 60 ksi carbon steel (A106 B, A53 B seamless, A333 Gr 6).
const std::vector<double> kCsKsi = {20.0, 20.0, 20.0, 19.9, 19.0, 17.9, 17.3, 16.7, 13.9, 11.4};
const std::vector<double> kTp304Ksi = {20.0, 16.7, 15.0, 13.8, 12.9, 12.3, 12.0, 11.7, 11.5, 11.2};
const std::vector<double> kTp316Ksi = {20.0, 17.3, 15.6, 14.3, 13.3, 12.6, 12.3, 12.1, 11.9, 11.8};
constexpr const char* kSource =
    "ASME B31.3-2024 Table A-1C (A53 B / A106 B / A333 6); BPVC II-D 2023 Table 1A lower "
    "row (A312 TP304 / TP316); ASME B16.5-2025 Tables 2-1.1 / 2-2.1 / 2-2.2 (bar)";

const std::vector<Material>& materials() {
    static const std::vector<Material> m = {
        {"A106-B", -29.0, kTempF, kCsKsi, "1.1", 203.4, 7850.0, false},
        {"A53-B", -29.0, kTempF, kCsKsi, "1.1", 203.4, 7850.0, false},
        {"A333-6", -46.0, kTempF, kCsKsi, "1.1", 203.4, 7850.0, false},
        {"A312-TP304", -254.0, kTempF, kTp304Ksi, "2.1", 195.1, 8000.0, true},
        {"A312-TP316", -254.0, kTempF, kTp316Ksi, "2.2", 195.1, 8000.0, true},
    };
    return m;
}

std::string norm_key(const std::string& s) {
    std::string out;
    for (char c : s) {
        if (c == ' ' || c == '_' || c == '-' || c == '.') continue;
        out += static_cast<char>(std::toupper(static_cast<unsigned char>(c)));
    }
    return out;
}

const Material* find_material(const std::string& name) {
    static const std::map<std::string, std::string> alias = {
        {"A106B", "A106-B"},      {"A106GRB", "A106-B"},     {"A53B", "A53-B"},
        {"A53GRB", "A53-B"},      {"A3336", "A333-6"},       {"A333GR6", "A333-6"},
        {"A312304", "A312-TP304"}, {"A312TP304", "A312-TP304"}, {"TP304", "A312-TP304"},
        {"A312316", "A312-TP316"}, {"A312TP316", "A312-TP316"}, {"TP316", "A312-TP316"},
    };
    const auto it = alias.find(norm_key(name));
    if (it == alias.end()) return nullptr;
    for (const Material& m : materials())
        if (it->second == m.name) return &m;
    return nullptr;
}

const char* kSupported =
    "supported: A106-B, A53-B, A333-6, A312-TP304 (A312-304), A312-TP316 (A312-316)";

double interp(const std::vector<double>& xs, const std::vector<double>& ys, double x) {
    if (x <= xs.front()) return ys.front();
    for (size_t i = 1; i < xs.size(); ++i) {
        if (x <= xs[i]) {
            const double f = (x - xs[i - 1]) / (xs[i] - xs[i - 1]);
            return ys[i - 1] + f * (ys[i] - ys[i - 1]);
        }
    }
    return ys.back();
}

// ASME B16.5-2025 Tables 2-1.1 (group 1.1), 2-2.1 (2.1), 2-2.2 (2.2), SI units:
// working pressure in bar gauge at temperature °C, class 150/300/600. The 2025
// edition (revisions 21-1888, 22-564) supersedes the 2017/2020 bar values.
struct RatingTable {
    const char* group;
    std::vector<double> temp_c;
    std::vector<double> cl150;
    std::vector<double> cl300;
    std::vector<double> cl600;
};

const std::vector<double> kRatingT = {38,  50,  100, 150, 200, 250, 300, 325,
                                      350, 375, 400, 425, 450, 475, 500, 538};

const std::vector<RatingTable>& rating_tables() {
    static const std::vector<RatingTable> t = {
        {"1.1",
         kRatingT,
         {19.6, 19.1, 17.7, 15.8, 13.8, 12.1, 10.2, 9.3, 8.4, 7.4, 6.5, 5.5, 4.6, 3.7, 2.8, 1.4},
         {51.0, 49.8, 46.7, 45.1, 43.8, 42.0, 39.9, 38.7, 37.6, 36.4, 34.6, 28.7, 23.0, 17.5, 11.5, 5.8},
         {102.0, 99.6, 93.4, 90.1, 87.6, 83.9, 79.8, 77.3, 75.3, 72.8, 69.3, 57.5, 45.9, 35.0,
          23.0, 11.6}},
        {"2.1",
         kRatingT,
         {19.1, 18.5, 15.6, 14.2, 13.1, 12.1, 10.2, 9.3, 8.4, 7.4, 6.5, 5.5, 4.6, 3.7, 2.8, 1.4},
         {49.7, 48.3, 40.8, 37.0, 34.3, 32.2, 31.0, 30.2, 29.5, 29.0, 28.3, 28.1, 27.4, 26.9, 26.4,
          24.4},
         {99.4, 96.7, 81.6, 74.1, 68.6, 64.3, 61.9, 60.5, 59.0, 58.1, 56.6, 56.2, 54.7, 53.8, 52.8,
          48.8}},
        {"2.2",
         kRatingT,
         {19.1, 18.5, 16.2, 14.7, 13.7, 12.1, 10.2, 9.3, 8.4, 7.4, 6.5, 5.5, 4.6, 3.7, 2.8, 1.4},
         {49.7, 48.3, 42.2, 38.4, 35.7, 33.4, 31.4, 31.0, 30.2, 29.8, 29.5, 29.0, 28.8, 28.6, 28.3,
          26.7},
         {99.4, 96.7, 84.3, 76.8, 71.3, 66.7, 62.9, 61.9, 60.5, 59.5, 59.0, 58.1, 57.6, 57.1, 56.6,
          53.4}},
    };
    return t;
}

bool point_on_polyline(const ojson& p, const ojson& path, double tol) {
    if (!p.is_array() || p.size() != 3 || !path.is_array()) return false;
    const double px = p[0].get<double>(), py = p[1].get<double>(), pz = p[2].get<double>();
    for (size_t i = 0; i + 1 < path.size(); ++i) {
        const double ax = path[i][0].get<double>(), ay = path[i][1].get<double>(),
                     az = path[i][2].get<double>();
        const double bx = path[i + 1][0].get<double>(), by = path[i + 1][1].get<double>(),
                     bz = path[i + 1][2].get<double>();
        const double dx = bx - ax, dy = by - ay, dz = bz - az;
        const double L2 = dx * dx + dy * dy + dz * dz;
        double t = L2 > 0 ? ((px - ax) * dx + (py - ay) * dy + (pz - az) * dz) / L2 : 0.0;
        t = std::clamp(t, 0.0, 1.0);
        const double qx = ax + t * dx - px, qy = ay + t * dy - py, qz = az + t * dz - pz;
        if (std::sqrt(qx * qx + qy * qy + qz * qz) <= tol) return true;
    }
    return false;
}

std::string str_or(const ojson& v, const std::string& dflt) {
    return v.is_string() ? v.get<std::string>() : dflt;
}

// Pipe weight + sustained-stress support span for a simply supported span
// (conservative vs continuous): deflection 5wL^4/(384EI) <= 2.5 mm and
// bending wL^2/(8Z) <= S_h - S_lp (B31.3 302.3.5(c) sustained limit, with the
// longitudinal pressure stress PD/4t). Section on nominal wall less
// corrosion; weight on the nominal wall.
struct Span {
    double span_m = 0.0;
    double defl_m = 0.0;
    double stress_m = 0.0;
    double weight_npm = 0.0;
    std::string basis;
};

Span support_span(double D, double tn, double c, double S_mpa, double P_mpa, const Material& m,
                  double rho_fluid, double insul_kgm) {
    Span s;
    const double id_nom = D - 2.0 * tn;
    const double a_steel = kPi / 4.0 * (D * D - id_nom * id_nom) * 1e-6;  // m2
    const double a_bore = kPi / 4.0 * id_nom * id_nom * 1e-6;
    s.weight_npm = (a_steel * m.density + a_bore * rho_fluid + insul_kgm) * kG;
    const double ts = tn - c;
    if (ts <= 0.0 || s.weight_npm <= 0.0) {
        s.basis = "corroded_wall";
        return s;
    }
    const double di_c = D - 2.0 * ts;
    const double I = kPi / 64.0 * (std::pow(D, 4) - std::pow(di_c, 4)) * 1e-12;  // m4
    const double Z = I / (D / 2.0 * 1e-3);                                      // m3
    const double E = m.modulus_gpa * 1e9;
    s.defl_m = std::pow(384.0 * E * I * kSagLimitM / (5.0 * s.weight_npm), 0.25);
    const double s_lp = P_mpa * D / (4.0 * ts);
    const double sig = (S_mpa - s_lp) * 1e6;
    s.stress_m = sig > 0.0 ? std::sqrt(8.0 * Z * sig / s.weight_npm) : 0.0;
    if (s.defl_m <= s.stress_m) {
        s.span_m = s.defl_m;
        s.basis = "deflection_2.5mm";
    } else {
        s.span_m = s.stress_m;
        s.basis = "sustained_stress";
    }
    return s;
}

}  // namespace

ojson b31_3_allowable(const std::string& material, double temp_c) {
    const Material* m = find_material(material);
    if (m == nullptr) {
        return jerr("unknown_material",
                    "no ASME B31.3 Table A-1 allowable-stress table for " +
                        pyutil::py_repr_str(material) + "; " + kSupported);
    }
    std::vector<double> tc;
    for (double f : m->temp_f) tc.push_back(f_to_c(f));
    if (temp_c > tc.back() + 1e-9) {
        return jerr("temperature_out_of_range",
                    std::string(m->name) + " allowable stress is tabulated to " +
                        pyutil::fmt_f(tc.back(), 0) +
                        " °C here (creep range beyond); lower design_temp_c or use a "
                        "high-temperature material/code check");
    }
    const double s_ksi = interp(tc, m->s_ksi, temp_c);
    ojson out = ojson::object();
    out["ok"] = true;
    out["material"] = m->name;
    out["allowable_mpa"] = s_ksi * kKsiToMpa;
    out["allowable_ksi"] = s_ksi;
    out["temp_c"] = temp_c;
    out["below_min_temp"] = temp_c < m->min_temp_c;
    out["min_temp_c"] = m->min_temp_c;
    out["flange_group"] = m->group;
    out["modulus_gpa"] = m->modulus_gpa;
    out["density_kgm3"] = m->density;
    out["stainless"] = m->stainless;
    return out;
}

ojson b16_5_rating(double flange_class, const std::string& group, double temp_c) {
    const RatingTable* t = nullptr;
    for (const RatingTable& r : rating_tables())
        if (group == r.group) t = &r;
    if (t == nullptr) {
        return jerr("unknown_material", "no B16.5 rating table for material group " +
                                            pyutil::py_repr_str(group) +
                                            "; tabulated groups: 1.1, 2.1, 2.2");
    }
    if (temp_c > t->temp_c.back() + 1e-9) {
        return jerr("temperature_out_of_range",
                    "B16.5 group " + group + " ratings are tabulated to 538 °C here");
    }
    const long cls = std::lround(flange_class);
    double bar = 0.0;
    if (cls == 150) {
        bar = interp(t->temp_c, t->cl150, temp_c);
    } else if (cls == 300) {
        bar = interp(t->temp_c, t->cl300, temp_c);
    } else if (cls == 600 || cls == 900 || cls == 1500 || cls == 2500) {
        bar = interp(t->temp_c, t->cl600, temp_c) * static_cast<double>(cls) / 600.0;
    } else {
        return jerr("unknown_flange_class",
                    "flange_class must be one of 150, 300, 600, 900, 1500, 2500");
    }
    ojson out = ojson::object();
    out["ok"] = true;
    out["rating_kpa"] = bar * 100.0;
    out["flange_class"] = cls;
    out["group"] = group;
    return out;
}

ojson piping_spec_check(const ojson& params) {
    if (!params.is_object()) {
        return jerr("invalid_params", "pass {piping, design_pressure_kpa, design_temp_c, ...}");
    }
    const ojson doc = params.value("piping", ojson(nullptr));
    if (doc.is_null()) {
        return jerr("invalid_params", "piping (an aicad.piping/1 document) is required");
    }
    const std::vector<std::string> verrs = validate_piping(doc);
    if (!verrs.empty()) {
        ojson out = jerr("invalid_piping",
                         "fix the listed errors (see contracts/piping_schema.md)");
        out["errors"] = verrs;
        return out;
    }
    auto num = [&](const char* key, double dflt, double& out) -> bool {
        if (!params.contains(key) || params[key].is_null()) {
            out = dflt;
            return true;
        }
        if (!params[key].is_number() || params[key].is_boolean()) return false;
        out = params[key].get<double>();
        return std::isfinite(out);
    };
    if (!params.contains("design_pressure_kpa") || params["design_pressure_kpa"].is_null()) {
        return jerr("invalid_params", "design_pressure_kpa (gauge) is required");
    }
    double p_kpa = 0, temp = 0, ca = 0, mill = 0, ej = 0, rho_f = 0, insul = 0;
    if (!num("design_pressure_kpa", 0.0, p_kpa))
        return jerr("invalid_params", "design_pressure_kpa must be a number (kPa gauge)");
    if (p_kpa < 0) {
        return jerr("external_pressure_not_covered",
                    "negative gauge pressure is external-pressure design (B31.3 304.1.3 / "
                    "BPVC VIII-1 UG-28), which this check does not perform");
    }
    if (!num("design_temp_c", 40.0, temp))
        return jerr("invalid_params", "design_temp_c must be a number (°C)");
    if (!num("corrosion_allowance_mm", 1.5, ca) || ca < 0)
        return jerr("invalid_params", "corrosion_allowance_mm must be a number >= 0");
    if (!num("mill_tolerance_frac", 0.125, mill) || mill < 0 || mill >= 0.5)
        return jerr("invalid_params", "mill_tolerance_frac must be in [0, 0.5) (0.125 = 12.5 %)");
    if (!num("joint_factor", 1.0, ej) || ej <= 0 || ej > 1)
        return jerr("invalid_params", "joint_factor (E, Table A-1B) must be in (0, 1]");
    if (!num("fluid_density_kgm3", 1000.0, rho_f) || rho_f < 0)
        return jerr("invalid_params", "fluid_density_kgm3 must be a number >= 0");
    if (!num("insulation_kgm", 0.0, insul) || insul < 0)
        return jerr("invalid_params", "insulation_kgm must be a number >= 0");
    const double P = p_kpa / 1000.0;  // MPa
    const std::string doc_mat = str_or(doc.value("material", ojson(nullptr)), "A106-B");

    std::vector<std::string> warnings;
    ojson runs = ojson::array();
    std::vector<std::string> failing;
    for (const auto& run : doc["runs"]) {
        if (run.value("suppressed", false)) continue;
        const std::string rid = pyutil::py_str(run["id"]);
        const std::string mat = str_or(run.value("material", ojson(nullptr)), doc_mat);
        const ojson allow = b31_3_allowable(mat, temp);
        if (!allow.value("ok", false)) {
            ojson out = jerr(allow["reason"].get<std::string>(),
                             "run " + rid + ": " + allow.value("hint", std::string()));
            return out;
        }
        if (allow["below_min_temp"].get<bool>()) {
            warnings.push_back("run " + rid + ": " + allow["material"].get<std::string>() +
                               " below its Table A-1 minimum temperature (" +
                               pyutil::fmt_f(allow["min_temp_c"].get<double>(), 0) +
                               " °C) needs impact testing per B31.3 323.2.2");
        }
        const std::string sched = pyutil::py_str(run.value("schedule", ojson("40")));
        const ojson pipe = nearest_pipe(run["nps"], sched);
        if (!pipe.value("ok", false)) {
            return jerr("unknown_pipe_size", "run " + rid + ": " +
                                                 pipe.value("reason", std::string()));
        }
        const double D = pipe["outer_dia"].get<double>();
        const double tn = pipe["wall"].get<double>();
        const double S = allow["allowable_mpa"].get<double>();
        const double sew = S * ej * kW;
        const double t_p = P * D / (2.0 * (sew + P * kY));       // eq. (3a)
        const double t_m = t_p + ca;                              // eq. (2)
        const double t_req = t_m / (1.0 - mill);                  // nominal incl. mill tol
        const double t_avail = tn * (1.0 - mill) - ca;
        const double mawp = t_avail > 0 ? 2.0 * sew * t_avail / (D - 2.0 * kY * t_avail) : 0.0;
        const bool pass = tn + 1e-9 >= t_req;
        if (t_p >= D / 6.0 || P / sew > 0.385) {
            warnings.push_back("run " + rid +
                               ": t >= D/6 or P/(SE) > 0.385 — eq. (3a) is outside its range; "
                               "B31.3 304.1.2(b) requires special design");
        }
        const Material* m = find_material(mat);
        const Span span = support_span(D, tn, ca, S, P, *m, rho_f, insul);
        if (span.span_m <= 0.0) {
            warnings.push_back("run " + rid +
                               ": no sustained-load margin left for a support span (corroded wall "
                               "or longitudinal pressure stress exhausts S_h)");
        }

        ojson r = ojson::object();
        r["id"] = run["id"];
        r["nps"] = run["nps"];
        r["schedule"] = sched;
        r["material"] = allow["material"];
        r["od_mm"] = D;
        r["t_nominal_mm"] = tn;
        r["t_pressure_mm"] = pyround(t_p, 3);
        r["t_required_mm"] = pyround(t_req, 3);
        r["allowable_mpa"] = pyround(S, 2);
        r["mawp_kpa"] = pyround(mawp * 1000.0, 1);
        r["utilization"] = pyround(t_req / tn, 3);
        r["support_span_m"] = pyround(span.span_m, 2);
        r["support_span_basis"] = span.basis;
        r["weight_operating_npm"] = pyround(span.weight_npm, 1);
        r["pass"] = pass;
        if (!pass) {
            failing.push_back(rid);
            // Thinnest catalogued schedule of this NPS that passes.
            std::vector<std::pair<double, std::string>> cands;
            const std::vector<std::string> names =
                m->stainless ? std::vector<std::string>{"5S", "10S", "40S", "80S", "160", "XXS"}
                             : std::vector<std::string>{"10", "20", "30", "40", "60", "80", "100",
                                                        "120", "140", "160", "XXS"};
            for (const auto& nm : names) {
                const ojson c = nearest_pipe(run["nps"], nm);
                if (c.value("ok", false) && std::abs(c["outer_dia"].get<double>() - D) < 0.5)
                    cands.emplace_back(c["wall"].get<double>(), nm);
            }
            std::sort(cands.begin(), cands.end());
            ojson sug = nullptr;
            for (const auto& [w, nm] : cands) {
                if (w + 1e-9 >= t_req) {
                    sug = nm;
                    break;
                }
            }
            r["suggested_schedule"] = sug;
        }
        runs.push_back(std::move(r));
    }

    ojson flanges = ojson::array();
    for (const auto& ft : doc["fittings"]) {
        if (ft.value("suppressed", false) || ft.value("type", std::string()) != "flange") continue;
        const std::string fid = pyutil::py_str(ft["id"]);
        const ojson prm = ft.value("params", ojson::object());
        const double cls =
            prm.value("flange_class", ojson(nullptr)).is_number() ? prm["flange_class"].get<double>()
                                                                  : 150.0;
        std::string mat = str_or(ft.value("material", ojson(nullptr)), "");
        if (mat.empty()) {
            for (const auto& run : doc["runs"]) {
                if (point_on_polyline(ft["origin"], run["path"], 1.0)) {
                    mat = str_or(run.value("material", ojson(nullptr)), "");
                    break;
                }
            }
        }
        if (mat.empty()) mat = doc_mat;
        const Material* m = find_material(mat);
        if (m == nullptr) {
            return jerr("unknown_material", "flange " + fid + ": no B16.5 group for " +
                                                pyutil::py_repr_str(mat) + "; " + kSupported);
        }
        const ojson rating = b16_5_rating(cls, m->group, temp);
        if (!rating.value("ok", false)) {
            return jerr(rating["reason"].get<std::string>(),
                        "flange " + fid + ": " + rating.value("hint", std::string()));
        }
        const double rk = rating["rating_kpa"].get<double>();
        const bool pass = p_kpa <= rk + 1e-9;
        if (!pass) failing.push_back(fid);
        ojson f = ojson::object();
        f["id"] = ft["id"];
        f["nps"] = ft["nps"];
        f["flange_class"] = std::lround(cls);
        f["group"] = m->group;
        f["rating_kpa"] = pyround(rk, 1);
        f["utilization"] = rk > 0 ? ojson(pyround(p_kpa / rk, 3)) : ojson(nullptr);
        f["pass"] = pass;
        flanges.push_back(std::move(f));
    }

    ojson design = ojson::object();
    design["pressure_kpa"] = p_kpa;
    design["temp_c"] = temp;
    design["corrosion_allowance_mm"] = ca;
    design["mill_tolerance_frac"] = mill;
    design["joint_factor"] = ej;
    design["Y"] = kY;
    design["W"] = kW;

    ojson out = ojson::object();
    out["ok"] = true;
    out["code"] = "ASME B31.3";
    out["source"] = kSource;
    out["design"] = std::move(design);
    out["runs"] = std::move(runs);
    out["flanges"] = std::move(flanges);
    out["all_pass"] = failing.empty();
    out["warnings"] = warnings;
    if (failing.empty()) {
        out["hint"] = "every run and flange passes at " + pyutil::fmt_g(p_kpa, 6) + " kPa / " +
                      pyutil::fmt_g(temp, 6) + " °C";
        out["next_actions"] = ojson::array({"piping_bom", "piping_solve"});
    } else {
        std::string ids;
        for (const auto& f : failing) ids += (ids.empty() ? "" : ", ") + f;
        out["hint"] = "failing: " + ids +
                      " — raise the schedule (see suggested_schedule) or the flange class";
        out["next_actions"] = ojson::array({"edit the failing run schedule / flange class",
                                            "piping_spec_check"});
    }
    return out;
}

}  // namespace chudware::piping
