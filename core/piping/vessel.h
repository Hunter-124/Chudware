// core/piping/vessel.h — ASME Section VIII Division 1 pressure-vessel sizing
// (contract RPC `vessel_size`, contracts/industrial_schema.md). Cylindrical
// shell per UG-27(c)(1)/(c)(2), formed heads per UG-32 (2:1 ellipsoidal,
// hemispherical, standard 6 % torispherical F&D), Section II-D Table 1A
// allowable stresses interpolated in temperature, UG-16(b) minimum
// thickness, corrosion allowance, standard plate rounding, MAWP back-calc
// (governing component, net of static head), UG-99(b) hydrotest, volume and
// masses. Pure C++, OCCT-free.
#pragma once

#include "piping/pyutil.h"

namespace chudware::piping {

using pyutil::ojson;

// params: {inner_diameter_mm, tan_tan_length_mm? | volume_m3?,
//          design_pressure_kpa, design_temp_c=40, material="SA-516-70",
//          corrosion_allowance_mm=3, joint_efficiency=0.85,
//          head_type="ellipsoidal_2_1", fluid_density_kgm3=1000,
//          fill_fraction=0.8, orientation="vertical"}
// Success: {ok:true, code:"ASME VIII-1", shell, heads, mawp_kpa,
//           hydrotest_kpa, volume_m3, tan_tan_length_mm, masses, warnings,
//           hint, next_actions}; refusal: {ok:false, reason, hint}.
ojson vessel_size(const ojson& params);

}  // namespace chudware::piping
