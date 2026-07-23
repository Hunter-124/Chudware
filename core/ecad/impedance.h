// Controlled-impedance + propagation-delay calculators — C++ port of
// engine/ecad/impedance.py. Pure closed-form transmission-line models
// (IPC-2141 microstrip/stripline, Wadell/Wheeler grounded-CPW via complete
// elliptic integrals, edge-coupled differential pair) plus the stackup-driven
// track_impedance() board helper. Returns the SAME dict shapes / keys / model
// strings as the Python oracle, to full double precision. OCCT-free; pure math.
#pragma once

#include <string>

#include <nlohmann/json.hpp>

namespace chudware::ecad {

// All geometry in mm; er dimensionless. Each returns {z0, eeff, delay_ps_per_mm, model}.
nlohmann::json microstrip_z0(double w, double h, double t, double er);
nlohmann::json stripline_z0(double w, double h, double t, double er);
nlohmann::json coplanar_z0(double w, double s, double h, double t, double er);

// Edge-coupled differential pair. kind: "microstrip" (default) | "stripline".
// Returns {z0_se, z0_diff, z0_odd, eeff, delay_ps_per_mm, kind, model}.
nlohmann::json diff_pair_z0(double w, double s, double h, double t, double er,
                            const std::string& kind = "microstrip");

// track_impedance(board, track_or_net): track dict, track id string, or net name.
// Returns {z0, eeff, delay_ps_per_mm, kind, model, width, h, er, t, layer, net,
// length_mm, delay_ps}. Throws OpError("invalid_op") if no track resolves.
nlohmann::json track_impedance(const nlohmann::json& board, const nlohmann::json& track_or_net);

}  // namespace chudware::ecad
