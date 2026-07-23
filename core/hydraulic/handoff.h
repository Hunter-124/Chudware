// core/hydraulic/handoff.h — hydraulic → piping / FEA handoff helpers.
// C++ port of the module-level (engine-less) half of
// services/mcp/hydraulic_tools.py: the ISO VG32 default fluid, the per-hose
// Darcy–Weisbach line-loss estimate, the hose-net → aicad.piping/1 network
// translation, the aicad.simhandoff/1 envelopes for the hydraulic_to_piping
// and hydraulic_to_fea couplings, and their verify envelopes.
#pragma once

#include <string>
#include <vector>

#include "piping/pyutil.h"

namespace chudware::hydraulic {

using pyutil::ojson;

// ISO VG32 mineral hydraulic oil at ~40 °C (ν = 32 cSt, ρ = 870 kg/m³).
ojson hydraulic_oil();  // {"name": "hydraulic_oil_iso_vg32", "rho": 870.0, "mu": 0.0278}

// Cheap per-hose Darcy–Weisbach pressure-drop estimate (laminar 64/Re below
// Re 2300, Swamee–Jain above): {ok, velocity_ms, reynolds, friction, regime,
// dp_kpa}.
ojson estimate_line_dp(double flow_lpm, double id_mm, double length_mm,
                       double rho = 870.0, double mu = 0.0278, double eps_m = 4.6e-5);

// Translate a hydraulic circuit's hose nets into an aicad.piping/1 network:
// {ok, piping_doc, boundary, lines, fluid, estimates, warnings}.
ojson hoses_to_piping(const ojson& hyd_doc, double flow_lpm, double supply_pressure_bar,
                      const ojson& lines = nullptr, double default_id_mm = 12.7,
                      double default_length_mm = 1000.0, const ojson& fluid = nullptr);

// aicad.simhandoff/1 handoff envelope for the hydraulic→piping coupling.
ojson piping_handoff_envelope(const ojson& hyd_doc, const ojson& net,
                              double supply_pressure_bar);

// Pump sizing margin fed back from line losses.
ojson margin_update(double supply_pressure_bar, const ojson& load_pressure_bar,
                    const ojson& est_loss_bar, const ojson& solved_loss_bar);

// Verify envelope for hydraulic→piping: per-line estimate vs solved dP.
// Returns (envelope, checks).
std::pair<ojson, ojson> verify_piping_envelope(const ojson& handoff,
                                               const ojson& solve_res,
                                               double tolerance_pct);

// Cylinder mount reaction magnitudes (N) at max pressure:
// {"extend_n", "retract_n"}.
ojson cylinder_mount_forces(double bore_mm, double rod_mm, double max_pressure_bar,
                            double efficiency = 1.0);

// Unit 3-vector (throws std::runtime_error like the oracle's ValueError).
std::vector<double> unit3(const ojson& axis);

// aicad.simhandoff/1 handoff envelope for the hydraulic→FEA coupling.
ojson fea_handoff_envelope(double bore_mm, double rod_mm, double max_pressure_bar,
                           const std::vector<double>& axis, double efficiency,
                           const std::string& load_face, const std::string& fixture_face,
                           const ojson& doc_id = nullptr,
                           const ojson& target_doc_id = nullptr);

// Verify envelope for hydraulic→FEA (independent A×P recompute).
ojson verify_fea_envelope(const ojson& handoff, double tolerance_pct,
                          const ojson& fea_result = nullptr);

}  // namespace chudware::hydraulic
