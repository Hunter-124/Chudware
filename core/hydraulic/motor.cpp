// core/hydraulic/motor.cpp — hydraulic_size_motor + pump_drive_size.
// Contract: contracts/drives_schema.md. Data tables below carry their sources
// inline; every selection picks the next standard size >= the requirement
// (never undersizes) and refuses when the requirement exceeds the ladder.
#define _USE_MATH_DEFINES
#include "hydraulic/motor.h"

#include <cmath>
#include <optional>
#include <string>
#include <vector>

namespace chudware::hydraulic {
namespace {

using pyutil::fmt_g;
using pyutil::pyround;

constexpr double kG = 9.80665;          // standard gravity, m/s²
constexpr double kWattsPerHp = 745.699872;  // mechanical horsepower
constexpr double kLadderTol = 1e-9;     // relative tolerance for "exactly on a step"

ojson refuse(const std::string& reason, const std::string& hint,
             const char* param = nullptr) {
    ojson out = ojson::object();
    out["ok"] = false;
    out["reason"] = reason;
    out["hint"] = hint;
    if (param) out["param"] = param;
    return out;
}

// Reads optional typed params, remembering the first type error as a refusal.
struct Reader {
    const ojson& p;
    ojson err = nullptr;

    const ojson* find(const char* key) const {
        if (!p.is_object()) return nullptr;
        auto it = p.find(key);
        if (it == p.end() || it->is_null()) return nullptr;
        return &*it;
    }
    std::optional<double> num(const char* key) {
        const ojson* v = find(key);
        if (!v) return std::nullopt;
        if (!v->is_number() || !std::isfinite(v->get<double>())) {
            if (err.is_null())
                err = refuse(std::string("invalid_") + key,
                             std::string(key) + " must be a finite number", key);
            return std::nullopt;
        }
        return v->get<double>();
    }
    std::optional<std::string> str(const char* key) {
        const ojson* v = find(key);
        if (!v) return std::nullopt;
        if (!v->is_string()) {
            if (err.is_null())
                err = refuse(std::string("invalid_") + key,
                             std::string(key) + " must be a string", key);
            return std::nullopt;
        }
        return v->get<std::string>();
    }
};

ojson missing(const char* key, const std::string& hint) {
    return refuse(std::string("missing_") + key, hint, key);
}

ojson not_positive(const char* key, double v) {
    return refuse(std::string("invalid_") + key,
                  std::string(key) + " must be > 0 (got " + fmt_g(v, 6) + ")", key);
}

ojson bad_efficiency(const char* key, double v) {
    return refuse(std::string("invalid_") + key,
                  std::string(key) + " must be in (0, 1] (got " + fmt_g(v, 6) + ")",
                  key);
}

// Smallest ladder value >= need; -1 when the need exceeds the ladder.
double next_step(const std::vector<double>& ladder, double need) {
    for (double v : ladder)
        if (v >= need * (1.0 - kLadderTol)) return v;
    return -1.0;
}

// ── hydraulic motor families ─────────────────────────────────────────────────
//
// Displacement ladders, cc/rev (catalogue steps; the next step >= the exact
// displacement is selected):
//   generic  ISO 3662 R10 preferred geometric displacements:
//            1 1.25 1.6 2 2.5 3.15 4 5 6.3 8 10 12.5 16 20 25 31.5 40 50 63 80
//            100 125 160 200 250 315 400 500 630 800 1000
//   gear     external gear motors, 4–100:
//            4 5 6 8 10 11 14 16 19 22 25 28 32 36 40 45 50 56 63 71 80 90 100
//   vane     balanced vane motors, 10–200:
//            10 16 20 25 32 40 50 63 80 100 125 160 200
//   piston   fixed bent-axis / axial piston motors, 10–500 (A2FM-type frame steps):
//            10 12 16 23 28 32 45 56 63 80 90 107 125 160 180 200 250 355 500
//   orbital  gerotor/geroler orbital (LSHT) motors, 8–800 (OML…OMV-type steps):
//            8 12.5 20 32 40 50 63 80 100 125 160 200 250 315 400 500 630 800
// Typical continuous speed windows and differential-pressure ratings are
// representative catalogue values used only for warnings.
struct MotorFamily {
    const char* type;
    const char* basis;
    std::vector<double> ladder;
    double min_rpm;     // 0 = no check
    double max_rpm;     // 0 = no check
    double max_dp_bar;  // continuous differential; 0 = no check
};

const std::vector<MotorFamily>& motor_families() {
    static const std::vector<MotorFamily> fams = {
        {"generic", "ISO 3662 R10 preferred displacements",
         {1, 1.25, 1.6, 2, 2.5, 3.15, 4, 5, 6.3, 8, 10, 12.5, 16, 20, 25, 31.5,
          40, 50, 63, 80, 100, 125, 160, 200, 250, 315, 400, 500, 630, 800, 1000},
         0, 0, 0},
        {"gear", "external gear motor catalogue steps (4-100 cc/rev)",
         {4, 5, 6, 8, 10, 11, 14, 16, 19, 22, 25, 28, 32, 36, 40, 45, 50, 56, 63,
          71, 80, 90, 100},
         500, 4000, 250},
        {"vane", "balanced vane motor catalogue steps (10-200 cc/rev)",
         {10, 16, 20, 25, 32, 40, 50, 63, 80, 100, 125, 160, 200},
         100, 3000, 210},
        {"piston", "fixed bent-axis piston motor frame steps (10-500 cc/rev)",
         {10, 12, 16, 23, 28, 32, 45, 56, 63, 80, 90, 107, 125, 160, 180, 200, 250,
          355, 500},
         50, 5000, 400},
        {"orbital", "orbital (LSHT) motor catalogue steps (8-800 cc/rev)",
         {8, 12.5, 20, 32, 40, 50, 63, 80, 100, 125, 160, 200, 250, 315, 400, 500,
          630, 800},
         10, 1000, 175},
    };
    return fams;
}

const MotorFamily* find_family(const std::string& type) {
    for (const auto& f : motor_families())
        if (type == f.type) return &f;
    return nullptr;
}

// ── electric motor data ──────────────────────────────────────────────────────

// IEC 60072-1 standard rated outputs, kW (0.12 … 400).
const std::vector<double>& iec_kw_ladder() {
    static const std::vector<double> l = {
        0.12, 0.18, 0.25, 0.37, 0.55, 0.75, 1.1, 1.5, 2.2, 3, 4, 5.5, 7.5, 11, 15,
        18.5, 22, 30, 37, 45, 55, 75, 90, 110, 132, 160, 200, 250, 315, 355, 400};
    return l;
}

// NEMA MG 1 standard horsepower ratings (¼ … 500 hp).
const std::vector<double>& nema_hp_ladder() {
    static const std::vector<double> l = {
        0.25, 0.33, 0.5, 0.75, 1, 1.5, 2, 3, 5, 7.5, 10, 15, 20, 25, 30,
        40, 50, 60, 75, 100, 125, 150, 200, 250, 300, 350, 400, 450, 500};
    return l;
}

struct FrameRow {
    double rating;
    const char* frame;
};

// IEC frame sizes, TEFC (IC411) IM B3, per EN 50347 / IEC 60072-1 output↔frame
// assignment (63M … 315M). 4-pole rows above 132 kW are manufacturer-typical
// (315L … 355L) since EN 50347 stops at 315M.
const std::vector<FrameRow>& iec_frames(int poles) {
    static const std::vector<FrameRow> p2 = {
        {0.18, "63M"},  {0.25, "63M"},  {0.37, "71M"},  {0.55, "71M"},
        {0.75, "80M"},  {1.1, "80M"},   {1.5, "90S"},   {2.2, "90L"},
        {3, "100L"},    {4, "112M"},    {5.5, "132S"},  {7.5, "132S"},
        {11, "160M"},   {15, "160M"},   {18.5, "160L"}, {22, "180M"},
        {30, "200L"},   {37, "200L"},   {45, "225M"},   {55, "250M"},
        {75, "280S"},   {90, "280M"},   {110, "315S"},  {132, "315M"}};
    static const std::vector<FrameRow> p4 = {
        {0.12, "63M"},  {0.18, "63M"},  {0.25, "71M"},  {0.37, "71M"},
        {0.55, "80M"},  {0.75, "80M"},  {1.1, "90S"},   {1.5, "90L"},
        {2.2, "100L"},  {3, "100L"},    {4, "112M"},    {5.5, "132S"},
        {7.5, "132M"},  {11, "160M"},   {15, "160L"},   {18.5, "180M"},
        {22, "180L"},   {30, "200L"},   {37, "225S"},   {45, "225M"},
        {55, "250M"},   {75, "280S"},   {90, "280M"},   {110, "315S"},
        {132, "315M"},  {160, "315L"},  {200, "315L"},  {250, "355M"},
        {315, "355M"},  {355, "355L"},  {400, "355L"}};
    static const std::vector<FrameRow> p6 = {
        {0.18, "71M"},  {0.25, "71M"},  {0.37, "80M"},  {0.55, "80M"},
        {0.75, "90S"},  {1.1, "90L"},   {1.5, "100L"},  {2.2, "112M"},
        {3, "132S"},    {4, "132M"},    {5.5, "132M"},  {7.5, "160M"},
        {11, "160L"},   {15, "180L"},   {18.5, "200L"}, {22, "200L"},
        {30, "225M"},   {37, "250M"},   {45, "280S"},   {55, "280M"},
        {75, "315S"},   {90, "315M"}};
    static const std::vector<FrameRow> none;
    switch (poles) {
        case 2: return p2;
        case 4: return p4;
        case 6: return p6;
        default: return none;
    }
}

// NEMA MG 1 T-frame assignments, 4-pole (1800 rpm sync) TEFC; fractional-hp
// ratings use the 56 frame. 250 hp and up are manufacturer-typical (447T/449T).
const std::vector<FrameRow>& nema_frames_4pole() {
    static const std::vector<FrameRow> f = {
        {0.25, "56"},  {0.33, "56"},  {0.5, "56"},   {0.75, "56"},  {1, "143T"},
        {1.5, "145T"}, {2, "145T"},   {3, "182T"},   {5, "184T"},   {7.5, "213T"},
        {10, "215T"},  {15, "254T"},  {20, "256T"},  {25, "284T"},  {30, "286T"},
        {40, "324T"},  {50, "326T"},  {60, "364T"},  {75, "365T"},  {100, "404T"},
        {125, "405T"}, {150, "444T"}, {200, "445T"}, {250, "447T"}, {300, "447T"},
        {350, "449T"}, {400, "449T"}, {450, "449T"}, {500, "449T"}};
    return f;
}

const char* lookup_frame(const std::vector<FrameRow>& rows, double rating) {
    for (const auto& r : rows)
        if (std::fabs(r.rating - rating) <= 1e-9 * rating) return r.frame;
    return nullptr;
}

// IEC 60034-30-1:2014 Table — nominal efficiency limits, 50 Hz, 4-pole, %.
// Columns IE2, IE3, IE4. 200 kW and above are constant to 1000 kW.
struct EffRow {
    double kw, ie2, ie3, ie4;
};
const std::vector<EffRow>& ie_table() {
    static const std::vector<EffRow> t = {
        {0.12, 59.1, 64.8, 69.8}, {0.18, 64.7, 69.9, 74.7}, {0.25, 68.5, 73.5, 77.9},
        {0.37, 72.7, 77.3, 81.1}, {0.55, 77.1, 80.8, 83.9}, {0.75, 79.6, 82.5, 85.7},
        {1.1, 81.4, 84.1, 87.2},  {1.5, 82.8, 85.3, 88.2},  {2.2, 84.3, 86.7, 89.5},
        {3, 85.5, 87.7, 90.4},    {4, 86.6, 88.6, 91.1},    {5.5, 87.7, 89.6, 91.9},
        {7.5, 88.7, 90.4, 92.6},  {11, 89.8, 91.4, 93.3},   {15, 90.6, 92.1, 93.9},
        {18.5, 91.2, 92.6, 94.2}, {22, 91.6, 93.0, 94.5},   {30, 92.3, 93.6, 94.9},
        {37, 92.7, 93.9, 95.2},   {45, 93.1, 94.2, 95.4},   {55, 93.5, 94.6, 95.7},
        {75, 94.0, 95.0, 96.0},   {90, 94.2, 95.2, 96.1},   {110, 94.5, 95.4, 96.3},
        {132, 94.7, 95.6, 96.4},  {160, 94.9, 95.8, 96.6},  {200, 95.1, 96.0, 96.7},
        {1000, 95.1, 96.0, 96.7}};
    return t;
}

// Typical full-load power factor, 4-pole TEFC IE3 induction motors at 400 V
// 50 Hz — representative values from manufacturer LV catalogues (ABB process
// performance M3BP/M3AA, Siemens SIMOTICS GP/SD 1LE1). Nameplate governs.
struct PfRow {
    double kw, pf;
};
const std::vector<PfRow>& pf_table() {
    static const std::vector<PfRow> t = {
        {0.12, 0.64}, {0.18, 0.66}, {0.25, 0.68}, {0.37, 0.70}, {0.55, 0.72},
        {0.75, 0.74}, {1.1, 0.77},  {1.5, 0.79},  {2.2, 0.81},  {3, 0.81},
        {4, 0.82},    {5.5, 0.83},  {7.5, 0.84},  {11, 0.84},   {15, 0.85},
        {18.5, 0.85}, {22, 0.85},   {30, 0.86},   {37, 0.86},   {45, 0.86},
        {55, 0.86},   {75, 0.87},   {90, 0.87},   {110, 0.87},  {132, 0.87},
        {160, 0.88},  {1000, 0.88}};
    return t;
}

// Linear interpolation in log(kW), clamped to the table ends.
template <typename Row, typename Get>
double interp_log_kw(const std::vector<Row>& t, double kw, Get get) {
    if (kw <= t.front().kw) return get(t.front());
    if (kw >= t.back().kw) return get(t.back());
    for (size_t i = 1; i < t.size(); ++i) {
        if (kw <= t[i].kw) {
            const double x0 = std::log(t[i - 1].kw), x1 = std::log(t[i].kw);
            const double s = (std::log(kw) - x0) / (x1 - x0);
            return get(t[i - 1]) + s * (get(t[i]) - get(t[i - 1]));
        }
    }
    return get(t.back());
}

ojson ladder_json(const std::vector<double>& ladder) {
    ojson a = ojson::array();
    for (double v : ladder) a.push_back(v);
    return a;
}

}  // namespace

// ── hydraulic_size_motor ─────────────────────────────────────────────────────

ojson size_motor(const ojson& params) {
    Reader rd{params};
    const auto torque = rd.num("torque_nm");
    const auto speed = rd.num("speed_rpm");
    const auto pressure = rd.num("pressure_bar");
    const auto eta_m_in = rd.num("mech_efficiency");
    const auto eta_v_in = rd.num("vol_efficiency");
    const auto type_in = rd.str("type");
    if (!rd.err.is_null()) return rd.err;

    if (!torque) return missing("torque_nm", "output torque required at the motor shaft, N·m");
    if (!speed) return missing("speed_rpm", "output shaft speed, rpm");
    if (!pressure)
        return missing("pressure_bar",
                       "differential pressure available across the motor, bar");
    if (*torque <= 0) return not_positive("torque_nm", *torque);
    if (*speed <= 0) return not_positive("speed_rpm", *speed);
    if (*pressure <= 0) return not_positive("pressure_bar", *pressure);
    const double eta_m = eta_m_in.value_or(0.9);
    const double eta_v = eta_v_in.value_or(0.95);
    if (!(eta_m > 0.0 && eta_m <= 1.0)) return bad_efficiency("mech_efficiency", eta_m);
    if (!(eta_v > 0.0 && eta_v <= 1.0)) return bad_efficiency("vol_efficiency", eta_v);

    const std::string type = type_in.value_or("generic");
    const MotorFamily* fam = find_family(type);
    if (!fam)
        return refuse("invalid_type",
                      "type must be one of gear, vane, piston, orbital (omit for the "
                      "ISO 3662 generic ladder); got '" + type + "'",
                      "type");

    const double dp_pa = *pressure * 1e5;
    // D = 2πT / (Δp·ηm)  [m³/rev] → cc/rev
    const double disp_cc = 2.0 * M_PI * *torque / (dp_pa * eta_m) * 1e6;
    // Q = D·n / ηv  [cc/min] → L/min
    const double flow_lpm = disp_cc * *speed / eta_v / 1000.0;
    const double omega = 2.0 * M_PI * *speed / 60.0;
    const double out_kw = *torque * omega / 1000.0;
    const double in_kw = dp_pa * (flow_lpm / 60000.0) / 1000.0;

    const double std_cc = next_step(fam->ladder, disp_cc);
    if (std_cc < 0) {
        ojson r = refuse(
            "displacement_exceeds_catalog",
            "required " + fmt_g(disp_cc, 5) + " cc/rev exceeds the largest " +
                std::string(fam->type) + " step (" + fmt_g(fam->ladder.back(), 5) +
                " cc/rev); raise pressure_bar, split the load across parallel "
                "motors, add a gear reduction, or choose a larger family "
                "(orbital/piston/radial-piston)");
        r["displacement_cc_rev"] = pyround(disp_cc, 4);
        r["catalog_max_cc_rev"] = fam->ladder.back();
        return r;
    }
    const double flow_std = std_cc * *speed / eta_v / 1000.0;
    const double dp_std_bar = 2.0 * M_PI * *torque / (std_cc * 1e-6 * eta_m) / 1e5;

    ojson warnings = ojson::array();
    if (fam->min_rpm > 0 && *speed < fam->min_rpm)
        warnings.push_back("speed_rpm " + fmt_g(*speed, 6) + " is below the typical "
                           "minimum " + fmt_g(fam->min_rpm, 6) + " rpm for " +
                           fam->type + " motors (torque ripple / stick-slip); consider "
                           "an orbital or radial-piston motor or a gear reduction");
    if (fam->max_rpm > 0 && *speed > fam->max_rpm)
        warnings.push_back("speed_rpm " + fmt_g(*speed, 6) + " exceeds the typical "
                           "maximum " + fmt_g(fam->max_rpm, 6) + " rpm for " +
                           fam->type + " motors; check the catalogue speed limit for "
                           "the selected displacement");
    if (fam->max_dp_bar > 0 && *pressure > fam->max_dp_bar)
        warnings.push_back("pressure_bar " + fmt_g(*pressure, 6) + " exceeds the "
                           "typical continuous rating " + fmt_g(fam->max_dp_bar, 6) +
                           " bar for " + fam->type + " motors");
    if (std_cc > 1.5 * disp_cc)
        warnings.push_back("standard step " + fmt_g(std_cc, 5) + " cc/rev is " +
                           fmt_g(std_cc / disp_cc, 3) + "x the required displacement; "
                           "flow rises to " + fmt_g(flow_std, 4) + " L/min and the "
                           "working differential drops to " + fmt_g(dp_std_bar, 4) +
                           " bar");

    ojson suitable = ojson::array();
    for (const auto& f : motor_families()) {
        if (std::string(f.type) == "generic") continue;
        if (*speed < f.min_rpm || *speed > f.max_rpm) continue;
        if (*pressure > f.max_dp_bar) continue;
        if (next_step(f.ladder, disp_cc) < 0) continue;
        suitable.push_back(f.type);
    }

    ojson out = ojson::object();
    out["ok"] = true;
    out["type"] = type;
    out["ladder_basis"] = fam->basis;
    out["torque_nm"] = *torque;
    out["speed_rpm"] = *speed;
    out["pressure_bar"] = *pressure;
    out["mech_efficiency"] = eta_m;
    out["vol_efficiency"] = eta_v;
    out["displacement_cc_rev"] = pyround(disp_cc, 4);
    out["standard_displacement_cc_rev"] = std_cc;
    out["flow_lpm"] = pyround(flow_lpm, 4);
    out["flow_lpm_at_standard"] = pyround(flow_std, 4);
    out["pressure_bar_at_standard"] = pyround(dp_std_bar, 4);
    out["output_power_kw"] = pyround(out_kw, 4);
    out["input_hydraulic_power_kw"] = pyround(in_kw, 4);
    out["overall_efficiency"] = pyround(eta_m * eta_v, 4);
    out["suitable_types"] = suitable;
    out["ladder_cc_rev"] = ladder_json(fam->ladder);
    out["warnings"] = warnings;
    out["hint"] = "select a " + std::string(type == "generic" ? "" : type + " ") +
                  "motor of " + fmt_g(std_cc, 5) + " cc/rev fed " +
                  fmt_g(pyround(flow_std, 2), 6) + " L/min at " +
                  fmt_g(pyround(dp_std_bar, 1), 6) + " bar differential";
    out["next_actions"] = ojson::array(
        {"size the supply pump for flow_lpm_at_standard plus leakage of other consumers",
         "set the relief valve above pressure_bar_at_standard plus line losses",
         "verify breakaway/starting torque (typically 80-90 % of running torque) "
         "against the load"});
    return out;
}

// ── pump_drive_size ──────────────────────────────────────────────────────────

ojson pump_drive_size(const ojson& params) {
    Reader rd{params};
    const auto kind_in = rd.str("kind");
    const auto flow_lpm_in = rd.num("flow_lpm");
    const auto flow_m3h_in = rd.num("flow_m3h");
    const auto pressure_in = rd.num("pressure_bar");
    const auto head_in = rd.num("head_m");
    const auto rho_in = rd.num("fluid_density_kgm3");
    const auto eta_in = rd.num("pump_efficiency");
    const auto speed_in = rd.num("speed_rpm");
    const auto standard_in = rd.str("standard");
    const auto sf_in = rd.num("service_factor");
    const auto volt_in = rd.num("voltage_v");
    const auto phases_in = rd.num("phases");
    const auto freq_in = rd.num("frequency_hz");
    const auto class_in = rd.str("efficiency_class");
    if (!rd.err.is_null()) return rd.err;

    if (!kind_in) return missing("kind", "kind must be 'hydraulic' or 'centrifugal'");
    const std::string kind = *kind_in;
    if (kind != "hydraulic" && kind != "centrifugal")
        return refuse("invalid_kind",
                      "kind must be 'hydraulic' or 'centrifugal'; got '" + kind + "'",
                      "kind");
    const bool hyd = kind == "hydraulic";

    if (flow_lpm_in && flow_m3h_in)
        return refuse("ambiguous_flow", "give exactly one of flow_lpm or flow_m3h");
    if (!flow_lpm_in && !flow_m3h_in)
        return missing("flow", "give flow_lpm or flow_m3h");
    if (flow_lpm_in && *flow_lpm_in <= 0) return not_positive("flow_lpm", *flow_lpm_in);
    if (flow_m3h_in && *flow_m3h_in <= 0) return not_positive("flow_m3h", *flow_m3h_in);
    if (pressure_in && head_in)
        return refuse("ambiguous_pressure", "give exactly one of pressure_bar or head_m");
    if (!pressure_in && !head_in)
        return missing("pressure", "give pressure_bar (pump differential) or head_m");
    if (pressure_in && *pressure_in <= 0) return not_positive("pressure_bar", *pressure_in);
    if (head_in && *head_in <= 0) return not_positive("head_m", *head_in);

    const double rho = rho_in.value_or(hyd ? 870.0 : 998.0);
    if (rho <= 0) return not_positive("fluid_density_kgm3", rho);
    const double eta_p = eta_in.value_or(hyd ? 0.88 : 0.7);
    if (!(eta_p > 0.0 && eta_p <= 1.0)) return bad_efficiency("pump_efficiency", eta_p);

    const std::string standard = standard_in.value_or("IEC");
    if (standard != "IEC" && standard != "NEMA")
        return refuse("invalid_standard",
                      "standard must be 'IEC' or 'NEMA'; got '" + standard + "'",
                      "standard");
    const bool nema = standard == "NEMA";

    const double sf = sf_in.value_or(1.15);
    if (!(sf >= 1.0))
        return refuse("invalid_service_factor",
                      "service_factor must be >= 1.0 (got " + fmt_g(sf, 6) +
                          "); the motor is never sized below the pump shaft power",
                      "service_factor");
    const double freq = freq_in.value_or(nema ? 60.0 : 50.0);
    if (freq <= 0) return not_positive("frequency_hz", freq);
    const double volt = volt_in.value_or(nema ? 460.0 : 400.0);
    if (volt <= 0) return not_positive("voltage_v", volt);
    const double phases_d = phases_in.value_or(3.0);
    if (phases_d != 1.0 && phases_d != 3.0)
        return refuse("invalid_phases",
                      "phases must be 1 or 3 (got " + fmt_g(phases_d, 6) + ")", "phases");
    const int phases = static_cast<int>(phases_d);
    const std::string eff_class = class_in.value_or("IE3");
    if (eff_class != "IE2" && eff_class != "IE3" && eff_class != "IE4")
        return refuse("invalid_efficiency_class",
                      "efficiency_class must be IE2, IE3 or IE4; got '" + eff_class + "'",
                      "efficiency_class");
    // Default duty speed: 4-pole synchronous speed at the supply frequency.
    const double speed = speed_in.value_or(30.0 * freq);
    if (speed <= 0) return not_positive("speed_rpm", speed);

    // Duty point.
    const double q_m3s = flow_lpm_in ? *flow_lpm_in / 60000.0 : *flow_m3h_in / 3600.0;
    const double dp_pa = pressure_in ? *pressure_in * 1e5 : rho * kG * *head_in;
    const double head_m = dp_pa / (rho * kG);
    const double hyd_kw = dp_pa * q_m3s / 1000.0;
    const double shaft_kw = hyd_kw / eta_p;
    const double required_kw = shaft_kw * sf;

    ojson warnings = ojson::array();

    // Poles: the highest pole count whose synchronous speed 120f/p is still >=
    // the duty speed (an induction motor runs below sync by its slip).
    int poles = 0;
    for (int p = 2; p <= 12; p += 2)
        if (120.0 * freq / p >= speed * (1.0 - kLadderTol)) poles = p;
    if (poles == 0) {
        poles = 2;
        warnings.push_back("speed_rpm " + fmt_g(speed, 6) + " exceeds the 2-pole "
                           "synchronous speed " + fmt_g(120.0 * freq / 2, 6) +
                           " rpm at " + fmt_g(freq, 4) + " Hz; needs a VFD "
                           "overspeed rating or a step-up drive");
    }
    const double sync = 120.0 * freq / poles;
    const double slip = (sync - speed) / sync;
    if (slip > 0.08)
        warnings.push_back("speed_rpm " + fmt_g(speed, 6) + " is " +
                           fmt_g(slip * 100.0, 3) + " % below the " +
                           std::to_string(poles) + "-pole synchronous speed " +
                           fmt_g(sync, 6) + " rpm (typical full-load slip 1-6 %); "
                           "use a VFD or a belt/gear drive");
    if (std::fabs(freq - 50.0) > 1.0 && std::fabs(freq - 60.0) > 1.0)
        warnings.push_back("frequency_hz " + fmt_g(freq, 4) + " is not a mains "
                           "frequency; ratings/efficiency assume a VFD supply");
    if (nema && std::fabs(freq - 60.0) > 1.0)
        warnings.push_back("NEMA motors are rated at 60 Hz; on 50 Hz the output and "
                           "speed fall to about 5/6 of nameplate");

    // Rating: next standard size >= required (never undersized).
    double rating_kw = 0.0, rating_hp = 0.0;
    if (nema) {
        const double required_hp = required_kw * 1000.0 / kWattsPerHp;
        rating_hp = next_step(nema_hp_ladder(), required_hp);
        if (rating_hp < 0)
            return refuse("drive_exceeds_ladder",
                          "required " + fmt_g(required_hp, 5) + " hp exceeds the "
                          "500 hp NEMA low-voltage ladder; use a medium-voltage motor "
                          "or split the duty across parallel pumps");
        rating_kw = rating_hp * kWattsPerHp / 1000.0;
    } else {
        rating_kw = next_step(iec_kw_ladder(), required_kw);
        if (rating_kw < 0)
            return refuse("drive_exceeds_ladder",
                          "required " + fmt_g(required_kw, 5) + " kW exceeds the "
                          "400 kW IEC 60072 low-voltage ladder; use a medium-voltage "
                          "motor or split the duty across parallel pumps");
    }

    const double eff_pct = interp_log_kw(ie_table(), rating_kw, [&](const EffRow& r) {
        return eff_class == "IE2" ? r.ie2 : eff_class == "IE3" ? r.ie3 : r.ie4;
    });
    const double eff = eff_pct / 100.0;
    const double pf = interp_log_kw(pf_table(), rating_kw, [](const PfRow& r) { return r.pf; });
    const double rated_in_kw = rating_kw / eff;
    const double line_factor = phases == 3 ? std::sqrt(3.0) : 1.0;
    const double flc_a = rated_in_kw * 1000.0 / (line_factor * volt * pf);
    const double input_kw = shaft_kw / eff;
    const double load_factor = shaft_kw / rating_kw;
    const double omega = 2.0 * M_PI * speed / 60.0;

    if (rating_kw > 1.5 * shaft_kw)
        warnings.push_back("motor rating " + fmt_g(rating_kw, 4) + " kW is " +
                           fmt_g(rating_kw / shaft_kw, 3) + "x the pump shaft power "
                           "(load factor " + fmt_g(load_factor * 100.0, 3) +
                           " %); efficiency and power factor fall at light load — "
                           "review service_factor or the pump selection");
    if (phases == 1 && rating_kw > 3.73)
        warnings.push_back("single-phase induction motors above about 3.7 kW (5 hp) "
                           "are uncommon; use a three-phase supply");

    const char* frame = nullptr;
    std::string frame_basis;
    if (nema) {
        if (poles == 4) {
            frame = lookup_frame(nema_frames_4pole(), rating_hp);
            frame_basis = "NEMA MG 1 T-frame, 4-pole TEFC (250 hp and up manufacturer-typical)";
        }
    } else {
        frame = lookup_frame(iec_frames(poles), rating_kw);
        frame_basis = poles == 4
                          ? "EN 50347 / IEC 60072-1, 4-pole TEFC (above 132 kW manufacturer-typical)"
                          : "EN 50347 / IEC 60072-1, " + std::to_string(poles) + "-pole TEFC";
    }
    if (!frame)
        warnings.push_back(std::string("no standard ") + (nema ? "NEMA" : "IEC") +
                           " frame tabulated for a " + std::to_string(poles) +
                           "-pole " +
                           (nema ? fmt_g(rating_hp, 4) + " hp" : fmt_g(rating_kw, 4) + " kW") +
                           " motor; take the frame from the manufacturer catalogue");

    std::string eff_basis =
        "IEC 60034-30-1 " + eff_class + " nominal, 50 Hz 4-pole table, log-kW interpolated";
    if (poles != 4) eff_basis += " (4-pole values applied to a " + std::to_string(poles) + "-pole motor)";
    if (std::fabs(freq - 50.0) > 1.0) eff_basis += " (50 Hz values applied at " + fmt_g(freq, 4) + " Hz)";

    ojson out = ojson::object();
    out["ok"] = true;
    out["kind"] = kind;
    out["standard"] = standard;
    out["flow_lpm"] = pyround(q_m3s * 60000.0, 4);
    out["flow_m3h"] = pyround(q_m3s * 3600.0, 4);
    out["pressure_bar"] = pyround(dp_pa / 1e5, 4);
    out["head_m"] = pyround(head_m, 4);
    out["fluid_density_kgm3"] = rho;
    out["pump_efficiency"] = eta_p;
    out["service_factor"] = sf;
    out["hydraulic_power_kw"] = pyround(hyd_kw, 4);
    out["shaft_power_kw"] = pyround(shaft_kw, 4);
    out["required_motor_kw"] = pyround(required_kw, 4);
    out["motor_rating_kw"] = pyround(rating_kw, 4);
    if (nema) out["motor_rating_hp"] = rating_hp;
    out["poles"] = poles;
    out["sync_speed_rpm"] = pyround(sync, 4);
    out["speed_rpm"] = speed;
    out["slip"] = pyround(slip, 4);
    out["load_factor"] = pyround(load_factor, 4);
    out["efficiency_class"] = eff_class;
    out["motor_efficiency"] = pyround(eff, 4);
    out["efficiency_basis"] = eff_basis;
    out["power_factor"] = pyround(pf, 3);
    out["power_factor_basis"] =
        "typical full-load pf, 4-pole TEFC IE3 LV catalogues (ABB M3BP/M3AA, Siemens "
        "1LE1); nameplate governs";
    out["input_power_kw"] = pyround(input_kw, 4);
    out["rated_input_power_kw"] = pyround(rated_in_kw, 4);
    out["full_load_current_a"] = pyround(flc_a, 3);
    out["voltage_v"] = volt;
    out["phases"] = phases;
    out["frequency_hz"] = freq;
    if (nema)
        out["nema_frame"] = frame ? ojson(frame) : ojson(nullptr);
    else
        out["iec_frame"] = frame ? ojson(frame) : ojson(nullptr);
    out["frame_basis"] = frame ? ojson(frame_basis) : ojson(nullptr);
    out["shaft_torque_nm"] = pyround(shaft_kw * 1000.0 / omega, 4);
    out["motor_rated_torque_nm"] = pyround(rating_kw * 1000.0 / omega, 4);
    out["warnings"] = warnings;
    out["hint"] = (nema ? fmt_g(rating_hp, 4) + " hp" : fmt_g(rating_kw, 4) + " kW") +
                  " " + std::to_string(poles) + "-pole " + standard + " motor" +
                  (frame ? std::string(" (frame ") + frame + ")" : std::string()) +
                  ", " + eff_class + " " + fmt_g(eff_pct, 3) + " %, FLC " +
                  fmt_g(pyround(flc_a, 1), 6) + " A at " + fmt_g(volt, 5) + " V";
    if (hyd)
        out["next_actions"] = ojson::array(
            {"size the pump displacement with hydraulic size_pump at speed_rpm",
             "start unloaded (open-centre/bypass or unloading valve) so the motor "
             "accelerates before system pressure builds",
             "set the relief valve at pressure_bar plus margin; motor torque at relief "
             "must stay below breakdown torque"});
    else
        out["next_actions"] = ojson::array(
            {"check the pump curve at the duty point and the run-out (end-of-curve) "
             "power against motor_rating_kw",
             "verify NPSH available exceeds NPSH required with margin",
             "size the starter/VFD and cable for full_load_current_a"});
    return out;
}

}  // namespace chudware::hydraulic
