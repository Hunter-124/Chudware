// Sim-result overlay packaging — C++ port of engine/sim/overlay.py
// (static_overlay / modal_overlay / _map_to_surface).
//
// The FEA solvers (sim/fea_static, sim/fea_modal) produce results on the VOLUME
// tet mesh; the viewport renders an AICM v1 SURFACE. This module bridges the two by
// extracting the FEA mesh BOUNDARY surface (fea_surface_body) — which has nodes all
// along the part, exactly like SolidWorks plots — and mapping each volume-node
// scalar/vector field onto it by DIRECT node indexing, then writing an AICM carrying
// SCLR + DISP chunks the host loader consumes. (Older path: tessellate the STEP and
// nearest-node map via map_to_surface — still available for diagnostics, but a flat
// STEP face has only its corner verts so a bent beam rendered as a straight tilt.)
//
// Lives in the OCCT lib (aicad_geom_occt) alongside the AICM writer; the sim-result
// vectors are passed in as plain doubles, so there is no link dep on the mesher libs.
#pragma once

#include <string>
#include <vector>

#include <nlohmann/json.hpp>

#include "geom/aicm.h"

namespace aicad::sim {

// The FEA boundary surface extracted from the VOLUME tet mesh: an AICM body whose
// vertices are the unique boundary nodes of the volume mesh (so a result plot has
// nodes all along the part, not just the 8 STEP corners — a bent beam renders as a
// curve, an axial field shows its distribution), plus surf_to_vol mapping each
// surface (compacted) vertex back to its GLOBAL volume node id. Per-node solver
// fields map to the surface by DIRECT INDEXING: field_surf[i] = field_vol[surf_to_vol[i]]
// (relying on the solver's per-node arrays sharing vol_points' node order).
struct FeaSurface {
    aicm::AicmBody body;
    std::vector<int> surf_to_vol;  // compacted surf vertex idx -> global vol node id
};

// Build the FEA boundary-surface AICM body from a volume tet mesh. `vol_points` is
// n*3 flat node coords; `tets` is m*nodes_per_elem flat 0-based connectivity (corner
// locals 0..3 are tets[e*stride+0..3]). A tet face is on the boundary iff its sorted
// corner-id key occurs in exactly one tet. Boundary triangles are emitted with
// OUTWARD winding. nodes_per_elem 4 -> one triangle per boundary face; 10 -> the face
// is subdivided into 4 sub-triangles using the quadratic mid-edge nodes (smoother
// surface that carries the curve). Per-vertex normals are area-weighted geometric.
FeaSurface fea_surface_body(const std::vector<double>& vol_points,
                            const std::vector<int>& tets, int nodes_per_elem);

// For each surface vertex (surf_xyz, m*3), take the value(s) of the nearest volume
// node (vol_xyz, n*3) — brute-force kNN=1 with first-minimum tie-break (np.argmin).
// `dim` is the values stride (1 for scalar fields, 3 for vector fields); vol_values
// is n*dim flat. Returns m*dim flat. Mirrors engine/sim/overlay._map_to_surface.
std::vector<double> map_to_surface(const std::vector<double>& surf_xyz,
                                   const std::vector<double>& vol_xyz,
                                   const std::vector<double>& vol_values, int dim);

// Size cap (T-SIM2): the boundary surface these writers emit is uploaded to a single GL
// VBO, CPU-re-deformed on every deform-scale change, and carries ~15 per-node scalar
// fields per vertex. When the extracted surface exceeds kMaxOverlayTris (~1.5M boundary
// triangles) the writer returns "" instead of emitting an overlay — a HARD GUARD, not a
// decimation: every surface vertex is bound to a volume node (surf_to_vol) and the solver
// fields are mapped by direct indexing, so dropping vertices would falsify the plotted
// field. The study still returns its scalar summary + narration; only the spatial contour
// is withheld, and the UI falls back to the editable model mesh. Hitting the cap means the
// mesh is pathologically fine — lower mesh_fineness or use draft (order 1) meshing.
//
// Build the static-result overlay AICM and write it to a temp .aicm; returns the
// absolute path (empty string on any failure — best-effort, never throws). Mirrors
// engine/sim/overlay.static_overlay: SCLR(von_mises 0..max_vm), SCLR(disp_mag
// 0..max_disp), SCLR(fos 0..5), DISP(scale_hint). vol_points/displacement are n*3,
// von_mises/disp_mag are n. `summary` supplies max_von_mises_mpa / max_disp_mm
// (falls back to the field max when absent), `yield_mpa` the FoS numerator.
//
// Additive: the principal1/principal3/max_shear VOLUME-node arrays (each length n,
// may be empty) are mapped to the surface and emitted as extra SCLR chunks ("p1",
// "p3", "max_shear"), plus per-component displacement chunks ("disp_x/y/z"). The
// original four chunks are emitted first, unchanged, so existing consumers are safe.
std::string static_overlay(const std::string& step_path,
                           const std::vector<double>& vol_points,
                           const std::vector<int>& tets, int nodes_per_elem,
                           const std::vector<double>& von_mises,
                           const std::vector<double>& disp_mag,
                           const std::vector<double>& displacement,
                           const std::vector<double>& principal1,
                           const std::vector<double>& principal3,
                           const std::vector<double>& max_shear,
                           double yield_mpa, const nlohmann::json& summary);

// Build the fatigue-result overlay AICM (best-effort, never throws; "" on failure).
// Mirrors static_overlay's structure (tessellate + map_to_surface + write_aicm), but
// carries only SCLR chunks (no displacement): "fatigue_fos" (0..5), "life_log10"
// (field min..max, log10 cycles), "von_mises" (0..field max, the peak stress driving
// the fatigue calc). All input volume arrays are length n (per node).
std::string fatigue_overlay(const std::string& step_path,
                            const std::vector<double>& vol_points,
                            const std::vector<int>& tets, int nodes_per_elem,
                            const std::vector<double>& fatigue_fos,
                            const std::vector<double>& life_log10,
                            const std::vector<double>& von_mises_peak);

// Build a single-mode modal overlay: SCLR(mode_amplitude 0..norm) + DISP(scale_hint).
// mode_shape is n*3. Returns the temp .aicm path ("" on failure). Mirrors
// engine/sim/overlay.modal_overlay.
std::string modal_overlay(const std::string& step_path,
                          const std::vector<double>& vol_points,
                          const std::vector<int>& tets, int nodes_per_elem,
                          const std::vector<double>& mode_shape, double freq_hz);

}  // namespace aicad::sim
