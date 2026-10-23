// core/piping/cfd.h — piping → 3D CFD handoff, pure-computation half.
// C++ port of engine_oracle/piping/cfd_handoff.py MINUS internal_volume
// (the bore-solid STEP extraction needs the OCCT kernel sweep — that leg
// stays with the kernel side). Ported here:
// cfd_boundary_from_solve (1D solve -> CFD inlet/outlet BCs) and
// verify_pressure_drop (1D vs 3D dP cross-check + velocity-limit advisory).
#pragma once

#include <string>

#include "piping/pyutil.h"

namespace chudware::piping {

using pyutil::ojson;

// Translate a piping_solve result into CFD inlet/outlet boundary conditions.
ojson cfd_boundary_from_solve(const ojson& solve_result,
                              const ojson& inlet_run_id = nullptr,
                              const ojson& outlet_node_id = nullptr);

// Compare the 1D network pressure drop with the 3D CFD pressure drop
// (drag_n / frontal_area, or Cd x ½ρV² fallback), at tolerance_pct of the 1D
// value, plus the 3 m/s erosion advisory per link.
ojson verify_pressure_drop(const ojson& solve_result, const ojson& cfd_result,
                           double tolerance_pct = 20.0,
                           const ojson& run_id = nullptr);

}  // namespace chudware::piping
