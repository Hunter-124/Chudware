// core/piping/duct.cpp — equal-friction air-duct sizing (duct_size).
// Per segment: the exact round diameter D* solving
//   f(Re, ε/D)·(1/D)·½ρV² = target friction rate,   V = Q/(πD²/4)
// (Colebrook via Clamond's exact iteration; 64/Re below Re 2300) is found by
// bisection — the friction gradient falls monotonically with D. Round ducts
// snap up to the next ISO R20 / EN 1506 preferred diameter; rectangular
// ducts take h = D*/c(AR) from the Huebscher identity De = c(AR)·h, rounded
// up to 50 mm on both sides. Velocity-cap violations upsize further. Friction
// uses the (equivalent) round diameter at the segment flow; fitting losses
// use the actual mean velocity. Cumulative path pressure sums dp_total from
// each root to every segment; the largest is the critical path and the fan
// static pressure.
#include "piping/duct.h"

#include <algorithm>
#include <cmath>
#include <map>
#include <set>
#include <string>
#include <vector>

#include "piping/fluid_properties.h"

namespace chudware::piping {
namespace {

using pyutil::fmt_f;
using pyutil::jerr;
using pyutil::pyround;

constexpr double kPi = 3.14159265358979323846;
constexpr double kRectStepMm = 50.0;   // rectangular side increment
constexpr double kRectMinMm = 100.0;   // smallest rectangular side
constexpr double kLaminarRe = 2300.0;
constexpr double kAspectWarn = 4.0;    // SMACNA: aspect ratios above 4:1 are inefficient
constexpr double kAspectMax = 8.0;

// ISO 3 R20 preferred numbers as used by EN 1506 circular duct (mm).
const std::vector<double>& round_series() {
    static const std::vector<double> k = {100, 112, 125, 140, 150, 160, 180, 200, 224, 250,
                                          280, 300, 315, 355, 400, 450, 500, 560, 600, 630,
                                          710, 800, 900, 1000, 1120, 1250, 1400, 1600, 1800, 2000};
    return k;
}

// fluids.friction.Clamond (fast=False): exact Colebrook solution.
double clamond(double re, double ed) {
    const double x1 = ed * re * 0.1239681863354175460160858261654858382699;
    const double x2 = std::log(re) - 0.7793974884556819406441139701653776731705;
    double f = x2 - 0.2;
    double x1f = x1 + f;
    double x1f1 = 1.0 + x1f;
    double e = (std::log(x1f) - 0.2) / x1f1;
    f = f - (x1f1 + 0.5 * e) * e * x1f / (x1f1 + e * (1.0 + (1.0 / 3.0) * e));
    x1f = x1 + f;
    x1f1 = 1.0 + x1f;
    e = (std::log(x1f) + f - x2) / x1f1;
    const double b = x1f1 + e * (1.0 + (1.0 / 3.0) * e);
    f = b / (b * f - ((x1f1 + 0.5 * e) * e * x1f));
    return 1.325474527619599502640416597148504422899 * (f * f);
}

struct Air {
    double rho;
    double mu;
};

struct FrictionState {
    double velocity;  // m/s through the round (equivalent) section
    double re;
    double fd;
    double gradient;  // Pa/m
};

FrictionState friction_at(double q, double d_m, double eps_m, const Air& air) {
    FrictionState s{};
    s.velocity = q / (kPi * d_m * d_m / 4.0);
    s.re = air.rho * s.velocity * d_m / air.mu;
    s.fd = s.re < kLaminarRe ? 64.0 / s.re : clamond(s.re, eps_m / d_m);
    s.gradient = s.fd / d_m * 0.5 * air.rho * s.velocity * s.velocity;
    return s;
}

// Exact round diameter (m) giving `target` Pa/m at flow q.
double solve_diameter(double q, double target, double eps_m, const Air& air) {
    double lo = std::log(0.005), hi = std::log(20.0);
    for (int i = 0; i < 200; ++i) {
        const double mid = 0.5 * (lo + hi);
        if (friction_at(q, std::exp(mid), eps_m, air).gradient > target) lo = mid;
        else hi = mid;
    }
    return std::exp(0.5 * (lo + hi));
}

double ceil_step(double v, double step) { return std::ceil(v / step - 1e-9) * step; }

bool read_num(const ojson& p, const char* key, double& out, ojson& err, const std::string& where = "") {
    auto it = p.find(key);
    if (it == p.end() || it->is_null()) return true;
    if (!it->is_number() || !std::isfinite(it->get<double>())) {
        err = jerr("invalid_parameter", where + key + " must be a finite number");
        return false;
    }
    out = it->get<double>();
    return true;
}

// Segment ids: strings, or integers rendered as their decimal string.
bool read_id(const ojson& v, std::string& out) {
    if (v.is_string()) {
        out = v.get<std::string>();
        return !out.empty();
    }
    if (v.is_number_integer()) {
        out = v.dump();
        return true;
    }
    return false;
}

struct Segment {
    std::string id;
    std::string parent;  // "" = root (connected to the fan)
    double q = 0;
    double length_m = 0;
    double k = 0;
    bool rect = false;
    double aspect = 2.0;
    // results
    double d_exact_mm = 0;
    double de_mm = 0;
    double width_mm = 0;
    double height_mm = 0;
    double velocity = 0;
    FrictionState fr{};
    double dp_friction = 0;
    double dp_fittings = 0;
    double dp_total = 0;
    double path_pa = 0;
    bool upsized = false;
};

}  // namespace

double huebscher_de(double a, double b) {
    return 1.30 * std::pow(a * b, 0.625) / std::pow(a + b, 0.25);
}

ojson duct_size(const ojson& params) {
    if (!params.is_object()) return jerr("invalid_params", "pass a JSON object of duct parameters");
    ojson err;

    std::string method = "equal_friction";
    if (params.contains("method") && !params["method"].is_null()) {
        if (!params["method"].is_string()) return jerr("invalid_parameter", "method must be a string");
        method = params["method"].get<std::string>();
    }
    if (method != "equal_friction") {
        return jerr("unsupported_method", "method '" + method + "' is not supported; use 'equal_friction'");
    }
    double target = 1.0, vmax = 10.0, temp_c = 20.0, rough_mm = 0.09;
    if (!read_num(params, "friction_pa_per_m", target, err)) return err;
    if (!(target > 0)) return jerr("invalid_friction_rate", "friction_pa_per_m must be > 0 (typical 0.8–1.2 Pa/m)");
    if (!read_num(params, "max_velocity_ms", vmax, err)) return err;
    if (!(vmax > 0)) return jerr("invalid_max_velocity", "max_velocity_ms must be > 0");
    if (!read_num(params, "air_temp_c", temp_c, err)) return err;
    if (!read_num(params, "roughness_mm", rough_mm, err)) return err;
    if (rough_mm < 0) return jerr("invalid_roughness", "roughness_mm must be >= 0 (galvanized steel 0.09)");

    const ojson props = fluid_properties("air", temp_c + T0_C, P_ATM);
    if (!props.value("ok", false)) {
        return jerr("air_properties_unavailable",
                    props.value("reason", std::string("air property model failed at ") + fmt_f(temp_c, 1) + " °C"));
    }
    const Air air{props["rho"].get<double>(), props["mu"].get<double>()};
    const double eps_m = rough_mm / 1000.0;

    if (!params.contains("segments") || !params["segments"].is_array() || params["segments"].empty()) {
        return jerr("missing_segments", "pass segments:[{id, parent?, airflow_m3s, length_m, k_fittings?, shape?}]");
    }

    std::vector<Segment> segs;
    std::map<std::string, size_t> index;
    for (const ojson& js : params["segments"]) {
        if (!js.is_object()) return jerr("invalid_segment", "every segment must be an object");
        Segment s;
        if (!js.contains("id") || !read_id(js["id"], s.id)) {
            return jerr("invalid_segment_id", "every segment needs a non-empty string or integer id");
        }
        const std::string where = "segment '" + s.id + "': ";
        if (index.count(s.id)) return jerr("duplicate_segment_id", "segment id '" + s.id + "' appears twice");
        if (js.contains("parent") && !js["parent"].is_null()) {
            if (!read_id(js["parent"], s.parent)) {
                return jerr("invalid_parent", where + "parent must be a segment id or null");
            }
        }
        if (!js.contains("airflow_m3s")) return jerr("missing_airflow", where + "airflow_m3s is required");
        if (!read_num(js, "airflow_m3s", s.q, err, where)) return err;
        if (!(s.q > 0)) return jerr("invalid_airflow", where + "airflow_m3s must be > 0");
        if (!js.contains("length_m")) return jerr("missing_length", where + "length_m is required");
        if (!read_num(js, "length_m", s.length_m, err, where)) return err;
        if (s.length_m < 0) return jerr("invalid_length", where + "length_m must be >= 0");
        if (!read_num(js, "k_fittings", s.k, err, where)) return err;
        if (s.k < 0) return jerr("invalid_k_fittings", where + "k_fittings must be >= 0");
        std::string shape = "round";
        if (js.contains("shape") && !js["shape"].is_null()) {
            if (!js["shape"].is_string()) return jerr("invalid_parameter", where + "shape must be a string");
            shape = js["shape"].get<std::string>();
        }
        if (shape != "round" && shape != "rect") {
            return jerr("invalid_shape", where + "shape must be 'round' or 'rect'");
        }
        s.rect = shape == "rect";
        if (!read_num(js, "aspect_ratio", s.aspect, err, where)) return err;
        if (s.rect && !(s.aspect >= 1.0 && s.aspect <= kAspectMax)) {
            return jerr("invalid_aspect_ratio", where + "aspect_ratio (width/height) must be in [1, 8]");
        }
        index[s.id] = segs.size();
        segs.push_back(std::move(s));
    }

    // Tree validation: every parent exists, no segment reaches itself.
    for (const Segment& s : segs) {
        if (!s.parent.empty() && !index.count(s.parent)) {
            return jerr("unknown_parent", "segment '" + s.id + "' names parent '" + s.parent + "', which is not a segment");
        }
    }
    for (const Segment& s : segs) {
        std::set<std::string> seen{s.id};
        for (std::string p = s.parent; !p.empty(); p = segs[index[p]].parent) {
            if (!seen.insert(p).second) {
                return jerr("cycle_detected",
                            "segment '" + s.id + "' reaches '" + p + "' twice following parent links; the duct "
                            "network must be a tree rooted at the fan");
            }
        }
    }

    ojson warnings = ojson::array();
    const std::vector<double>& series = round_series();

    // ── size each segment ──
    for (Segment& s : segs) {
        const double d_exact = solve_diameter(s.q, target, eps_m, air);
        s.d_exact_mm = d_exact * 1000.0;
        if (!s.rect) {
            auto it = std::find_if(series.begin(), series.end(),
                                   [&](double d) { return d >= s.d_exact_mm - 1e-6; });
            if (it == series.end()) {
                return jerr("duct_exceeds_series",
                            "segment '" + s.id + "' needs Ø" + fmt_f(s.d_exact_mm, 0) +
                                " mm at the target friction rate, beyond the 2000 mm round series; split the "
                                "flow into parallel ducts or use shape 'rect'");
            }
            for (; it != series.end(); ++it) {
                const double d = *it / 1000.0;
                if (s.q / (kPi * d * d / 4.0) <= vmax + 1e-9) break;
                s.upsized = true;
            }
            if (it == series.end()) {
                return jerr("duct_exceeds_series",
                            "segment '" + s.id + "' cannot meet max_velocity_ms " + fmt_f(vmax, 1) +
                                " within the 2000 mm round series; split the flow or use shape 'rect'");
            }
            s.de_mm = *it;
            const double d = s.de_mm / 1000.0;
            s.velocity = s.q / (kPi * d * d / 4.0);
        } else {
            // De = c·h for w = AR·h, c = 1.30·AR^0.625/(1+AR)^0.25.
            const double c = 1.30 * std::pow(s.aspect, 0.625) / std::pow(1.0 + s.aspect, 0.25);
            const double h_exact = s.d_exact_mm / c;
            s.height_mm = std::max(kRectMinMm, ceil_step(h_exact, kRectStepMm));
            s.width_mm = std::max(kRectMinMm, ceil_step(s.aspect * h_exact, kRectStepMm));
            while (s.q / (s.width_mm * s.height_mm * 1e-6) > vmax + 1e-9) {
                s.width_mm += kRectStepMm;
                s.upsized = true;
            }
            s.de_mm = huebscher_de(s.width_mm, s.height_mm);
            s.velocity = s.q / (s.width_mm * s.height_mm * 1e-6);
            const double ar = s.width_mm / s.height_mm;
            if (ar > kAspectWarn) {
                warnings.push_back("segment '" + s.id + "': rounded aspect ratio " + fmt_f(ar, 2) +
                                   " exceeds 4:1 — high friction and sheet-metal cost (SMACNA)");
            }
        }
        if (s.upsized) {
            warnings.push_back("segment '" + s.id + "' upsized to keep velocity <= " + fmt_f(vmax, 1) +
                               " m/s (friction rate now below target)");
        }
        s.fr = friction_at(s.q, s.de_mm / 1000.0, eps_m, air);
        s.dp_friction = s.fr.gradient * s.length_m;
        s.dp_fittings = s.k * 0.5 * air.rho * s.velocity * s.velocity;
        s.dp_total = s.dp_friction + s.dp_fittings;
        if (s.fr.re < 4000.0) {
            warnings.push_back("segment '" + s.id + "': Re = " + fmt_f(s.fr.re, 0) +
                               " is laminar/transitional; friction is uncertain");
        }
    }

    // ── continuity: a parent should carry at least its children's flow ──
    std::map<std::string, double> child_sum;
    std::map<std::string, std::vector<std::string>> children;
    for (const Segment& s : segs) {
        if (s.parent.empty()) continue;
        child_sum[s.parent] += s.q;
        children[s.parent].push_back(s.id);
    }
    for (const auto& [pid, sum] : child_sum) {
        const Segment& p = segs[index[pid]];
        if (p.q < sum * (1.0 - 1e-3)) {
            warnings.push_back("segment '" + pid + "' carries " + fmt_f(p.q, 4) + " m3/s but its branches carry " +
                               fmt_f(sum, 4) + " m3/s; flow continuity is violated");
        }
    }

    // ── path sums, critical path ──
    for (Segment& s : segs) {
        double total = 0;
        for (const Segment* c = &s;; c = &segs[index[c->parent]]) {
            total += c->dp_total;
            if (c->parent.empty()) break;
        }
        s.path_pa = total;
    }
    size_t crit = 0;
    for (size_t i = 1; i < segs.size(); ++i) {
        if (segs[i].path_pa > segs[crit].path_pa) crit = i;
    }
    std::vector<std::string> path;
    for (const Segment* c = &segs[crit];; c = &segs[index[c->parent]]) {
        path.push_back(c->id);
        if (c->parent.empty()) break;
    }
    std::reverse(path.begin(), path.end());
    size_t roots = 0;
    for (const Segment& s : segs) roots += s.parent.empty() ? 1 : 0;
    if (roots > 1) {
        warnings.push_back(std::to_string(roots) +
                           " root segments: they are treated as parallel branches off one fan plenum");
    }

    ojson seg_out = ojson::array();
    for (const Segment& s : segs) {
        ojson o = ojson::object();
        o["id"] = s.id;
        o["parent"] = s.parent.empty() ? ojson(nullptr) : ojson(s.parent);
        o["shape"] = s.rect ? "rect" : "round";
        o["airflow_m3s"] = s.q;
        o["length_m"] = s.length_m;
        o["diameter_exact_mm"] = pyround(s.d_exact_mm, 1);
        if (s.rect) {
            o["diameter_mm"] = nullptr;
            o["width_mm"] = s.width_mm;
            o["height_mm"] = s.height_mm;
        } else {
            o["diameter_mm"] = s.de_mm;
        }
        o["equivalent_diameter_mm"] = pyround(s.de_mm, 1);
        o["velocity_ms"] = pyround(s.velocity, 3);
        o["reynolds"] = pyround(s.fr.re, 0);
        o["friction_factor"] = pyround(s.fr.fd, 5);
        o["friction_rate_pa_per_m"] = pyround(s.fr.gradient, 4);
        o["dp_friction_pa"] = pyround(s.dp_friction, 3);
        o["dp_fittings_pa"] = pyround(s.dp_fittings, 3);
        o["dp_total_pa"] = pyround(s.dp_total, 3);
        o["path_pa"] = pyround(s.path_pa, 3);
        o["upsized_for_velocity"] = s.upsized;
        o["children"] = children.count(s.id) ? ojson(children[s.id]) : ojson::array();
        seg_out.push_back(std::move(o));
    }

    ojson out = ojson::object();
    out["ok"] = true;
    out["method"] = method;
    ojson a = ojson::object();
    a["temp_c"] = temp_c;
    a["density_kgm3"] = pyround(air.rho, 5);
    a["viscosity_pas"] = air.mu;
    a["source"] = props.value("source", std::string());
    out["air"] = a;
    out["friction_pa_per_m"] = target;
    out["max_velocity_ms"] = vmax;
    out["roughness_mm"] = rough_mm;
    out["segments"] = seg_out;
    out["critical_path"] = path;
    out["fan_static_pa"] = pyround(segs[crit].path_pa, 2);
    out["warnings"] = warnings;
    out["hint"] =
        "fan_static_pa is the duct-system loss along the critical path; add terminal devices, coils, filters "
        "and dampers from manufacturer data before selecting the fan";
    out["next_actions"] = ojson::array(
        {"persist the sizing with sim_study_add {type:'duct_system', engine:'cpp-industrial', params, results}",
         "balance non-critical branches with dampers sized to their path_pa deficit"});
    return out;
}

}  // namespace chudware::piping
