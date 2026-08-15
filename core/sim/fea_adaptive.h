// Adaptive (error-driven) static FEA.
//
// Every study in the app asks the user to guess a mesh fineness and then trust the answer.
// A guess that is too coarse under-reports peak stress, which is the dangerous direction:
// it reads as a passing factor of safety. Refining uniformly to compensate is the usual
// workaround and it is expensive — element count grows with the cube of the size step, and
// almost all of those elements land where the stress field is already smooth.
//
// fea_run_adaptive closes the loop instead: mesh, solve, score the discretization error per
// element, refine only where the error actually is, and repeat until either the error
// target is met or the pass budget runs out. It reports the achieved error, so a result
// arrives with a statement about its own convergence rather than a fineness slider
// position.
//
// It reuses fea_run_static verbatim for every pass, feeding it the refined mesh inline, so
// boundary-condition resolution, bonded-contact ties, the cancel token, the material model
// and the result shape are IDENTICAL to an ordinary study. The only new machinery is the
// error estimator and the mesher's size field.
#pragma once

#include <nlohmann/json.hpp>

namespace chudware::sim {

// `study` is an ordinary fea_run_static study (see sim/fea_run.h) that MUST carry a
// `step_path` — an inline mesh cannot be refined — plus an optional `adapt` block:
//   adapt: {max_passes: int (default 3, clamped to [1,6]),
//           eta_target: float (default 0.05, dimensionless relative ZZ indicator)}
//
// Returns the LAST pass's fea_run_static result, augmented with:
//   adapt: {passes: [{pass, n_nodes, n_elems, n_dof, element_size_mm, global_eta,
//                     max_von_mises_mpa, max_disp_mm}...],
//           global_eta, eta_target, eta_target_met, passes_run, stop_reason}
// so the convergence history is inspectable, not just the final number.
//
// Adaptive runs require the polymesh backend (the Netgen route exposes no size field); the
// study's mesher_backend is forced to polymesh and that is stated in the result notes.
// Never throws: any failure returns {ok:false, error}.
nlohmann::json fea_run_adaptive(const nlohmann::json& study);

}  // namespace chudware::sim
