// core/hydraulic/solve.cpp — port of engine_oracle/hydraulic/solve.py.
// Physics: cylinder force/flow/area, pump power/pressure, accumulator
// pre-charge, valve Kv flow equation. Hand-calc anchors (from the oracle
// docstrings): 50 mm bore @200 bar → ≈39.27 kN; 10 L/min @200 bar η=0.85 →
// shaft ≈3.922 kW; pre=100/min=150/max=250 bar, 5 L → ≈18.73 L.
#define _USE_MATH_DEFINES  // MSVC needs this before <cmath> for M_PI
#include "hydraulic/solve.h"

#include <cmath>
#include <initializer_list>

namespace chudware::hydraulic {
namespace {

using pyutil::jerr;
using pyutil::pyround;
bool all_finite(std::initializer_list<double> values) {
    for (double value : values)
        if (!std::isfinite(value)) return false;
    return true;
}


}  // namespace

ojson size_cylinder(double bore_mm, double rod_mm, double pressure_bar,
                    double stroke_mm, double velocity_mm_s, double efficiency) {
    if (!all_finite({bore_mm, rod_mm, pressure_bar, stroke_mm, velocity_mm_s, efficiency}))
        return jerr("cylinder sizing inputs must all be finite");
    if (bore_mm <= 0)
        return jerr("bore_mm must be > 0", "typical cylinders 25–500 mm bore");
    if (rod_mm < 0) return jerr("rod_mm must be >= 0", "use 0 for single-acting / rodless");
    if (rod_mm >= bore_mm)
        return jerr("rod_mm must be < bore_mm", "rod diameter must be smaller than bore");
    if (pressure_bar <= 0) return jerr("pressure_bar must be > 0");
    if (stroke_mm <= 0) return jerr("stroke_mm must be > 0");
    if (velocity_mm_s <= 0) return jerr("velocity_mm_s must be > 0");
    if (!(0.0 < efficiency && efficiency <= 1.0))
        return jerr("efficiency must be in (0, 1]");

    const double a_bore_m2 = M_PI / 4.0 * std::pow(bore_mm * 1e-3, 2);
    const double a_ann_m2 =
        M_PI / 4.0 * (std::pow(bore_mm * 1e-3, 2) - std::pow(rod_mm * 1e-3, 2));
    const double pressure_pa = pressure_bar * 1e5;
    const double extend_force_kn = a_bore_m2 * pressure_pa * efficiency * 1e-3;
    const double retract_force_kn = a_ann_m2 * pressure_pa * efficiency * 1e-3;
    const double velocity_m_s = velocity_mm_s * 1e-3;
    const double flow_extend_lpm = a_bore_m2 * velocity_m_s * 60.0 * 1000.0;
    const double flow_retract_lpm = a_ann_m2 * velocity_m_s * 60.0 * 1000.0;
    const double cycle_time_s = stroke_mm / velocity_mm_s;
    if (!all_finite({a_bore_m2, a_ann_m2, pressure_pa, extend_force_kn,
                     retract_force_kn, velocity_m_s, flow_extend_lpm,
                     flow_retract_lpm, cycle_time_s})) {
        return jerr("cylinder sizing calculation overflowed");
    }

    ojson out = ojson::object();
    out["ok"] = true;
    out["bore_mm"] = bore_mm;
    out["rod_mm"] = rod_mm;
    out["pressure_bar"] = pressure_bar;
    out["stroke_mm"] = stroke_mm;
    out["velocity_mm_s"] = velocity_mm_s;
    out["efficiency"] = efficiency;
    out["area_bore_cm2"] = pyround(a_bore_m2 * 1e4, 4);
    out["area_annulus_cm2"] = pyround(a_ann_m2 * 1e4, 4);
    out["extend_force_kn"] = pyround(extend_force_kn, 4);
    out["retract_force_kn"] = pyround(retract_force_kn, 4);
    out["flow_extend_lpm"] = pyround(flow_extend_lpm, 4);
    out["flow_retract_lpm"] = pyround(flow_retract_lpm, 4);
    out["cycle_time_s"] = pyround(cycle_time_s, 4);
    out["next_actions"] = ojson::array(
        {"confirm pump flow >= flow_extend_lpm to meet velocity target",
         "check rod buckling for long strokes (Euler column formula)"});
    return out;
}

ojson size_pump(double flow_lpm, double pressure_bar, double efficiency,
                double speed_rpm) {
    if (!all_finite({flow_lpm, pressure_bar, efficiency, speed_rpm}))
        return jerr("pump sizing inputs must all be finite");
    if (flow_lpm <= 0) return jerr("flow_lpm must be > 0");
    if (pressure_bar <= 0) return jerr("pressure_bar must be > 0");
    if (!(0.0 < efficiency && efficiency <= 1.0))
        return jerr("efficiency must be in (0, 1]");
    if (speed_rpm <= 0) return jerr("speed_rpm must be > 0");

    const double displacement_cc_rev = flow_lpm * 1000.0 / speed_rpm;
    const double flow_m3s = flow_lpm / 60.0 / 1000.0;
    const double pressure_pa = pressure_bar * 1e5;
    const double power_kw = flow_m3s * pressure_pa / 1000.0;
    const double shaft_power_kw = power_kw / efficiency;
    const double omega_rad_s = 2.0 * M_PI * speed_rpm / 60.0;
    const double torque_nm = shaft_power_kw * 1000.0 / omega_rad_s;
    if (!all_finite({displacement_cc_rev, flow_m3s, pressure_pa, power_kw,
                     shaft_power_kw, omega_rad_s, torque_nm})) {
        return jerr("pump sizing calculation overflowed");
    }

    ojson out = ojson::object();
    out["ok"] = true;
    out["flow_lpm"] = flow_lpm;
    out["pressure_bar"] = pressure_bar;
    out["efficiency"] = efficiency;
    out["speed_rpm"] = speed_rpm;
    out["displacement_cc_rev"] = pyround(displacement_cc_rev, 4);
    out["hydraulic_power_kw"] = pyround(power_kw, 4);
    out["shaft_power_kw"] = pyround(shaft_power_kw, 4);
    out["torque_nm"] = pyround(torque_nm, 4);
    out["next_actions"] = ojson::array(
        {"select a pump with displacement_cc_rev >= computed value",
         "verify motor shaft power >= shaft_power_kw"});
    return out;
}

ojson size_accumulator(double pre_charge_bar, double max_pressure_bar,
                       double min_pressure_bar, double volume_stored_l) {
    if (!all_finite(
            {pre_charge_bar, max_pressure_bar, min_pressure_bar, volume_stored_l}))
        return jerr("accumulator sizing inputs must all be finite");
    if (pre_charge_bar <= 0) return jerr("pre_charge_bar must be > 0");
    if (min_pressure_bar <= 0) return jerr("min_pressure_bar must be > 0");
    if (max_pressure_bar <= 0) return jerr("max_pressure_bar must be > 0");
    if (volume_stored_l <= 0) return jerr("volume_stored_l must be > 0");
    if (!(pre_charge_bar < min_pressure_bar)) {
        return jerr("pre_charge_bar must be < min_pressure_bar",
                    "nitrogen pre-charge pressure must be below the minimum working "
                    "pressure");
    }
    if (!(min_pressure_bar < max_pressure_bar))
        return jerr("min_pressure_bar must be < max_pressure_bar");

    const double factor =
        pre_charge_bar * (1.0 / min_pressure_bar - 1.0 / max_pressure_bar);
    if (factor <= 0)
        return jerr("accumulator sizing factor is zero or negative — check pressure values");
    const double total_volume_l = volume_stored_l / factor;
    const double pressure_ratio = max_pressure_bar / pre_charge_bar;
    if (!all_finite({factor, total_volume_l, pressure_ratio}))
        return jerr("accumulator sizing calculation overflowed");

    ojson out = ojson::object();
    out["ok"] = true;
    out["pre_charge_bar"] = pre_charge_bar;
    out["min_pressure_bar"] = min_pressure_bar;
    out["max_pressure_bar"] = max_pressure_bar;
    out["volume_stored_l"] = volume_stored_l;
    out["total_volume_l"] = pyround(total_volume_l, 4);
    out["pressure_ratio"] = pyround(pressure_ratio, 4);
    out["assumption"] = "isothermal (Boyle's law); ideal gas; no thermal correction";
    out["next_actions"] = ojson::array(
        {"select accumulator with total_volume_l >= computed value",
         "verify pre-charge pressure with nitrogen at ambient temperature before fill"});
    return out;
}

ojson valve_flow(double Cv, double pressure_drop_bar, double fluid_sg) {
    if (!all_finite({Cv, pressure_drop_bar, fluid_sg}))
        return jerr("valve sizing inputs must all be finite");
    if (Cv <= 0) return jerr("Cv must be > 0", "typical control valves 0.1–500 Cv");
    if (pressure_drop_bar <= 0) return jerr("pressure_drop_bar must be > 0");
    if (fluid_sg <= 0)
        return jerr("fluid_sg must be > 0", "hydraulic oil ≈ 0.87; water = 1.0");

    const double kv = Cv * 0.865;
    const double flow_m3h = kv * std::sqrt(pressure_drop_bar / fluid_sg);
    const double flow_lpm = flow_m3h * 1000.0 / 60.0;
    if (!all_finite({kv, flow_m3h, flow_lpm}))
        return jerr("valve sizing calculation overflowed");

    ojson out = ojson::object();
    out["ok"] = true;
    out["Cv"] = Cv;
    out["Kv"] = pyround(kv, 4);
    out["pressure_drop_bar"] = pressure_drop_bar;
    out["fluid_sg"] = fluid_sg;
    out["flow_m3h"] = pyround(flow_m3h, 4);
    out["flow_lpm"] = pyround(flow_lpm, 4);
    out["next_actions"] =
        ojson::array({"confirm valve Cv rating meets or exceeds required flow at design dP"});
    return out;
}

}  // namespace chudware::hydraulic
