// core/piping/bom.h — bill of materials, line list and spool breakdown for an
// aicad.piping/1 document (contract RPC `piping_bom`,
// contracts/piping_schema.md §8.3). Pure C++.
//
// Pipe quantities are cut lengths (centreline less fitting take-outs); pipe
// mass from catalog OD/ID and material density (7850 kg/m3 carbon steel,
// 8000 stainless). Fitting masses are idealised solids of revolution from the
// catalog dimensions on the fitting's schedule wall: elbows by Pappus (wall
// annulus × bend radius × angle, exact for a torus segment), tees as run +
// branch tubes, reducers as a mean-wall frustum, flanges as the B16.5 disc
// less bolt holes (hub excluded — a lower bound).
#pragma once

#include "piping/pyutil.h"

namespace chudware::piping {

using pyutil::ojson;

// params: {piping, max_spool_length_mm=12000}
// Success: {ok:true, line_list, items, spools, totals, warnings, hint,
//           next_actions}; refusal {ok:false, reason, hint} with reason in
// invalid_params / invalid_piping / unknown_pipe_size / unknown_material.
ojson piping_bom(const ojson& params);

}  // namespace chudware::piping
