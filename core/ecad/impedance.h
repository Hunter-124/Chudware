// Controlled-impedance + propagation-delay calculators — C++ port of
// engine/ecad/impedance.py. Pure closed-form transmission-line models
// (IPC-2141 microstrip/stripline, Wadell/Wheeler grounded-CPW via complete
// elliptic integrals, edge-coupled differential pair) plus the stackup-driven
// track_impedance() board helper. Existing result keys/model strings stay stable;
// additive reference-layer diagnostics expose the closed-form model's plane assumption.
#pragma once

#include <string>

#include <nlohmann/json.hpp>

namespace chudware::ecad {

// All geometry in mm; er dimensionless. Non-finite/non-physical inputs throw OpError.
nlohmann::json microstrip_z0(double w, double h, double t, double er);
nlohmann::json stripline_z0(double w, double h, double t, double er);
nlohmann::json coplanar_z0(double w, double s, double h, double t, double er);

// Edge-coupled differential pair. kind: "microstrip" (default) | "stripline".
// Returns {z0_se, z0_diff, z0_odd, eeff, delay_ps_per_mm, kind, model}.
nlohmann::json diff_pair_z0(double w, double s, double h, double t, double er,
                            const std::string& kind = "microstrip");

// track_impedance(board, track_or_net): track dict, track id string, or net name.
// Returns existing impedance/delay keys plus reference_layers and
// reference_plane_assumption. Throws OpError("invalid_op") for invalid geometry/setup.
nlohmann::json track_impedance(const nlohmann::json& board, const nlohmann::json& track_or_net);

}  // namespace chudware::ecad
