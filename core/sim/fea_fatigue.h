// Fatigue (high-cycle / S-N) study — a mechanical-engineering-grade layer on top
// of the existing linear-static FEA. It reuses the static stress field (von Mises
// per node = the peak/operating stress sigma_peak), then for each node converts to
// an alternating/mean stress pair (sigma_a, sigma_m) per the load case and computes
// a fatigue factor of safety n (Goodman / Gerber / Soderberg) plus a Basquin S-N
// life estimate N (cycles to failure).
//
// This is ADDITIVE: it never touches the static result numerics or summary keys. It
// meshes + solves through the same glue as fea_run_static (so it has both the per-
// node von Mises array AND the mesh points needed to build the fatigue overlay).
//
// Like fea_run.h this header is gmsh-FREE (fea_fatigue.cpp confines gmsh.h via the
// mesher TU), so it stays includable in any link config.
#pragma once

#include <nlohmann/json.hpp>

namespace aicad::sim {

// Run a fatigue study. `study` carries the SAME mesh + material + fixtures + loads
// fields as fea_run_static, PLUS an optional "fatigue" object. The ENGINE no longer
// owns Marin correction, reliability, or method selection: the host applies ka..kd
// and passes in a corrected Se, plus Sut and Sy. The engine evaluates ALL standard
// mean-stress theories and returns each theory's max equivalent-fully-reversed
// stress (sa_eq) so the host can compute factor-of-safety, life, and confidence
// bands itself.
//
// study["fatigue"]:
//   lmin, lmax:        load factors; the cycle spans lmin*applied .. lmax*applied
//                      stress. Fully reversed = {-1,1}; zero-based = {0,1};
//                      ratio R = {R,1}. If absent, derived from legacy keys
//                      load_type ("reversed"->{-1,1}, "zero_based"->{0,1},
//                      "ratio"+load_ratio R ->{R,1}). Default {-1,1}.
//   se_corrected_mpa:  corrected endurance limit Se (host applied Marin). REQUIRED>0
//                      (falls back to material.endurance_mpa, then 0.5*Sut).
//   se_base_mpa:       uncorrected Se' (echo only).
//   ultimate_mpa:      Sut. REQUIRED>0 (else {ok:false,error}); falls back to
//                      material.ultimate_mpa.
//   yield_mpa:         Sy (>0; falls back to material.yield_mpa; theories needing it
//                      use Sut when Sy<=0).
//
// Returns {ok, overlay_aicm?, n_nodes, n_elems, n_dof, summary{...}, solver_notes}
// where summary.theories carries goodman/gerber/soderberg/asme/morrow, each with
// sa_eq_mpa (max over nodes). On any failure returns {ok:false, error}. Never throws.
nlohmann::json fea_run_fatigue(const nlohmann::json& study);

}  // namespace aicad::sim
