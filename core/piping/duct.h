// core/piping/duct.h — HVAC / industrial air-duct sizing (contract RPC
// `duct_size`, contracts/industrial_schema.md). Equal-friction method:
// each segment's exact round diameter meets the target friction rate with
// the Colebrook friction factor (Clamond's exact solution) at the air
// density/viscosity of the design temperature; round ducts snap up to the
// ISO R20 / EN 1506 preferred series, rectangular ducts are sized so their
// Huebscher equivalent diameter covers the round size at the requested
// aspect ratio. Velocity-cap upsizing, fitting losses k·½ρv², cumulative
// pressure along the parent tree, critical path and fan static pressure.
// Pure C++, OCCT-free.
#pragma once

#include "piping/pyutil.h"

namespace chudware::piping {

using pyutil::ojson;

// params: {segments:[{id, parent?, airflow_m3s, length_m, k_fittings=0,
//          shape:"round"|"rect", aspect_ratio=2}], method="equal_friction",
//          friction_pa_per_m=1.0, max_velocity_ms=10, air_temp_c=20,
//          roughness_mm=0.09}
// Success: {ok:true, segments:[...], critical_path, fan_static_pa, warnings,
//           hint, next_actions}; refusal: {ok:false, reason, hint}.
ojson duct_size(const ojson& params);

// Huebscher equivalent round diameter of an a×b rectangle (same units):
// De = 1.30·(ab)^0.625 / (a+b)^0.25.
double huebscher_de(double a, double b);

}  // namespace chudware::piping
