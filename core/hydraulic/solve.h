// core/hydraulic/solve.h — hydraulic component sizing calculations.
// C++ port of engine_oracle/hydraulic/solve.py: cylinder force/flow/cycle,
// pump displacement/power/torque, accumulator pre-charge (Boyle isothermal),
// valve Kv/Cv flow. Math only, contract-shaped results
// ({"ok": true, ...} / {"ok": false, "reason", "hint"}).
#pragma once

#include "piping/pyutil.h"

namespace chudware::hydraulic {

using pyutil::ojson;

ojson size_cylinder(double bore_mm, double rod_mm, double pressure_bar,
                    double stroke_mm, double velocity_mm_s = 50.0,
                    double efficiency = 0.95);

ojson size_pump(double flow_lpm, double pressure_bar, double efficiency = 0.85,
                double speed_rpm = 1500.0);

ojson size_accumulator(double pre_charge_bar, double max_pressure_bar,
                       double min_pressure_bar, double volume_stored_l);

ojson valve_flow(double Cv, double pressure_drop_bar, double fluid_sg = 0.87);

// FMU simulation is a soft-fail seam: the C++ core never has fmpy, so this
// always returns the oracle's fmpy-absent skipped envelope.
ojson fmu_simulate(const ojson& circuit_doc, const ojson& fmu_path = nullptr,
                   double stop_time = 10.0);

}  // namespace chudware::hydraulic
