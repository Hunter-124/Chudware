// Sim-result overlay packaging — C++ port of engine/sim/overlay.py
// (static_overlay / modal_overlay / _map_to_surface).
//
// The FEA solvers (sim/fea_static, sim/fea_modal) produce results on the VOLUME
// tet mesh; the viewport renders the AICM v1 SURFACE tessellation. This module
// bridges the two: tessellate the analysed STEP surface, map each volume-node
// scalar/vector to the nearest surface vertex (brute-force kNN=1), and write an
// AICM carrying SCLR + DISP chunks the host loader consumes.
//
// Lives in the OCCT lib (aicad_geom_occt) because it reads a STEP and runs the
// BRepMesh surface tessellation (geom/tessellate.h). The sim-result vectors are
// passed in as plain doubles, so there is no link dep on the gmsh solver libs.
#pragma once

#include <string>
#include <vector>

#include <nlohmann/json.hpp>

namespace aicad::sim {

// For each surface vertex (surf_xyz, m*3), take the value(s) of the nearest volume
// node (vol_xyz, n*3) — brute-force kNN=1 with first-minimum tie-break (np.argmin).
// `dim` is the values stride (1 for scalar fields, 3 for vector fields); vol_values
// is n*dim flat. Returns m*dim flat. Mirrors engine/sim/overlay._map_to_surface.
std::vector<double> map_to_surface(const std::vector<double>& surf_xyz,
                                   const std::vector<double>& vol_xyz,
                                   const std::vector<double>& vol_values, int dim);

// Build the static-result overlay AICM and write it to a temp .aicm; returns the
// absolute path (empty string on any failure — best-effort, never throws). Mirrors
// engine/sim/overlay.static_overlay: SCLR(von_mises 0..max_vm), SCLR(disp_mag
// 0..max_disp), SCLR(fos 0..5), DISP(scale_hint). vol_points/displacement are n*3,
// von_mises/disp_mag are n. `summary` supplies max_von_mises_mpa / max_disp_mm
// (falls back to the field max when absent), `yield_mpa` the FoS numerator.
std::string static_overlay(const std::string& step_path,
                           const std::vector<double>& vol_points,
                           const std::vector<double>& von_mises,
                           const std::vector<double>& disp_mag,
                           const std::vector<double>& displacement,
                           double yield_mpa, const nlohmann::json& summary);

// Build a single-mode modal overlay: SCLR(mode_amplitude 0..norm) + DISP(scale_hint).
// mode_shape is n*3. Returns the temp .aicm path ("" on failure). Mirrors
// engine/sim/overlay.modal_overlay.
std::string modal_overlay(const std::string& step_path,
                          const std::vector<double>& vol_points,
                          const std::vector<double>& mode_shape, double freq_hz);

}  // namespace aicad::sim
