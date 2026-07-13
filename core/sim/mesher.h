// Tet mesher — wraps Netgen / nglib 6.2 (the FEA tet4 / tet10 mesher).
//
// mesh_step reads a STEP, derives a base element size from the bounding-box diagonal +
// fineness, and drives Netgen's OCC meshing pipeline (edge -> surface -> volume, with
// curvature-adaptive refinement and optional second-order CAD-surface projection). The
// node/element output is normalized to a STABLE contract (flat point + tet arrays, gmsh
// MSH_TET_10 mid-edge ordering) so the FEA solvers stay backend-agnostic.
//
// This header is intentionally mesher-backend-FREE (nglib.h is included only in mesher.cpp),
// so it is always includable regardless of the AICAD_WITH_OCCT / Netgen-link config.
//
// LICENSE NOTE: Netgen/nglib is LGPL-2.1 (commercial-safe under dynamic linking). It
// replaced gmsh (GPL-2+, no linking exception), which had been linked into libaicad.dll.
// The implementation (mesher.cpp) is compiled only under AICAD_WITH_OCCT.
#pragma once

#include <memory>
#include <string>
#include <vector>

namespace aicad::sim {

struct MeshOut {
    std::vector<double> points;  // n*3 flat, row-major (node i -> [3i,3i+1,3i+2]) mm
    std::vector<int> tets;       // m*K flat, row-major, 0-based node indices
                                 // (K = nodes_per_elem: 4 for tet4, 10 for tet10)
    double char_len_mm = 0.0;    // characteristic element length used (base size)
    int n_nodes = 0;
    int n_elems = 0;
    int nodes_per_elem = 4;      // 4 (order 1, tet4) or 10 (order 2, tet10)
    double min_tet_volume_mm3 = 0.0;
    double max_tet_edge_ratio = 0.0;  // max longest-edge / shortest-edge over corner tets

    // --- Professional quality metrics (computed over the corner tets) ---
    // Aspect ratio per element = longest edge / shortest altitude (1.0 = perfect, ideal
    // ~ for an equilateral tet is sqrt(3/2)*... so we report the raw geometric ratio and
    // flag elements above kPoorAspect). SolidWorks calls poorly-shaped elements out the
    // same way; this is what the GUI surfaces so a slider move is visibly reflected.
    double aspect_min = 0.0;
    double aspect_avg = 0.0;
    double aspect_max = 0.0;
    // Scaled Jacobian: worst (minimum) over all elements/corners, range (0,1]; >0.3 is
    // healthy, <=0 means an inverted/degenerate element.
    double jacobian_min = 0.0;
    double pct_poor = 0.0;       // % of elements with aspect_ratio > kPoorAspect (10)
    std::vector<int> aspect_hist;  // 10 buckets over aspect in [1, kPoorAspect]+overflow

    // --- Exotic-type + auto-pick annotations (filled on every successful path) ---
    // The concrete element type actually produced, so a caller can tell hex from tet and
    // surface an auto-pick / fallback decision. One of: 'tet4', 'tet10', 'hex8',
    // 'tet4-autopick', 'hex8-autopick', 'tet4-thinwall', 'tet4-fallback', 'tet10-fallback',
    // 'tet4-quad-surface', 'tet10-quad-surface', 'tet4-quad-surface-fallback',
    // 'tet10-quad-surface-fallback'.
    std::string mesh_type_used;
    // Non-fatal informational note: empty unless a fallback or auto-pick decision was made,
    // e.g. 'auto-pick: axis-aligned box -> hex8' or 'structured hex not applicable: ...,
    // fell back to tet'. Surfaced by the simulation UI alongside the mesh.
    std::string note;

    bool ok = false;
    std::string error;  // non-empty when ok==false
};

struct MeshOptions {
    // Primary control: 0 = coarse, 1 = fine. Base element size is derived from the
    // bounding-box DIAGONAL (not the smallest side) so plates/brackets/shafts all get a
    // sane element count: base = diag / lerp(8, 60, fineness).
    double fineness = 0.5;
    // Legacy alias. If >= 0 it is mapped onto `fineness` (old callers/persisted studies
    // sent `density` in [0,1] where higher = finer; the mapping preserves that sense).
    double density = -1.0;

    int order = 1;                 // 1=tet4, 2=tet10

    // Curvature-adaptive refinement — THE feature that makes FEA capture stress
    // concentrations AND gives curved bodies (cylinders/fillets/holes) an even, smooth
    // surface mesh: the mesher refines a curve of radius r so a full 2*pi turn gets
    // ~curv_elems_per_circle elements. Off => uniform sizing.
    bool curvature = true;
    // 20 (was 12 ~= Netgen's default ~12.6/circle, which left cylinders visibly faceted).
    // 20 segments/circle reads smooth and even while staying bounded; typical 8..24.
    double curv_elems_per_circle = 20.0;

    double target_size_mm = 0.0;   // >0 hard override of the derived base size
    double min_size_mm = 0.0;      // >0 overrides base*0.5 (the auto 2:1 size band floor)
    double max_size_mm = 0.0;      // >0 overrides base
    bool optimize = true;          // run Netgen's surface + volume quality optimizers
    int algorithm3d = 1;           // legacy gmsh Mesh.Algorithm3D knob; accepted but not
                                   // mapped (Netgen uses Delaunay + advancing front)

    // Structured hexahedral meshing: when true, mesh_step delegates to mesh_box_hex,
    // which produces a regular nx*ny*nz grid of 8-node hexes for a box/rectangular-prism
    // part (nodes_per_elem==8). Non-mappable geometry returns ok=false with a message.
    bool structured = false;

    // --- Simple-mode auto-pick + exotic surface meshing (geometry-adaptive) ---
    // auto_type: classify the geometry (box / curved / thin-wall) and choose the element
    // type + sizing automatically (Simple mode). When set, mesh_step resolves effective
    // options via pick_options_for_geometry before dispatching.
    bool auto_type = false;
    // quad_surface: quad-dominated SURFACE mesh (Netgen mp.quad_dominated=1); the VOLUME
    // stays tetrahedral. Surface elements are not consumed by the FEA solvers, so this only
    // affects the surface topology (and the pyramids Netgen inserts at the quad->tet
    // transition, which the mesher splits to tets).
    // If the quad-dominated surface defeats Netgen's volume mesher (open quads on this build),
    // the mesh automatically falls back to a plain tet volume (mesh_type_used
    // '...-quad-surface-fallback', note set) — the surface hint never breaks the volume mesh.
    bool quad_surface = false;
    // fallback_to_tet: when structured==true fails on a non-box part, fall back to tet
    // meshing and annotate (mesh_type_used='tet4-fallback', note set) instead of hard-failing
    // as the post-gmsh path did. Set false to require hex and surface the original box error.
    bool fallback_to_tet = true;
    // thin_wall_layers: auto-pick target for the number of tet layers through the smallest
    // bbox dimension of a thin-wall part (drives the element-size formula).
    double thin_wall_layers = 4.0;
};

// Aspect-ratio threshold above which an element is counted "poor" (SolidWorks-style).
inline constexpr double kPoorAspect = 10.0;

// Mesh a STEP file into tets. Base element size is derived from the bounding-box diagonal
// and the `fineness` control; curvature-adaptive refinement and Netgen optimization are
// applied per the options. Never throws: any mesher failure (including the netgen exceptions
// raised on bad geometry) returns ok=false with an error string.
//
// order==1 (default): first-order 4-node tets.
// order==2: second-order 10-node tets. The output uses the canonical gmsh MSH_TET_10 node
//   ordering: corners 0..3, then mid-edge nodes on edges
//   {0,1},{1,2},{2,0},{3,0},{3,2},{3,1} -> tet[4..9]. out.tets holds 10 indices/elem.
//   (Netgen emits its mid nodes in lexicographic edge order; mesher.cpp remaps them to this
//   convention.) Mid-side nodes are projected to the CAD surface (curved elements) for
//   accurate stress recovery on fillets/holes.
//
// The (density, order) overload is kept for back-compat: density in [0,1] is mapped to
// fineness with the same "higher = finer" sense.
MeshOut mesh_step(const std::string& step_path, double density, int order = 1);
MeshOut mesh_step(const std::string& step_path, const MeshOptions& options);

// mesh_step_cached — memoized mesh_step. Meshing (Netgen) is the #1 FEA cost, and it is
// re-run on EVERY solve even when the user changed only loads / material / fixtures (which
// do not affect the mesh). This memoizes the result keyed on the STEP file CONTENTS (not the
// path — callers may write a fresh temp file per solve) combined with EVERY MeshOptions
// field, so an unchanged geometry + mesh settings reuses the mesh instead of re-meshing.
// The key can never false-HIT (a hit requires byte-identical geometry AND identical options),
// only safe false-MISS (re-mesh). Thread-safe; bounded LRU cache. On any file-read failure it
// falls back to a direct mesh_step. Drop-in replacement for the options overload above.
MeshOut mesh_step_cached(const std::string& step_path, const MeshOptions& options);

// P4: zero-copy flavour — a cache HIT returns the resident mesh (shared, immutable)
// instead of deep-copying megabytes of points/tets. Prefer this in read-only consumers
// (preview/overlay marshalling); the by-value form above stays for callers that mutate.
std::shared_ptr<const MeshOut> mesh_step_cached_ptr(const std::string& step_path,
                                                    const MeshOptions& options);

}  // namespace aicad::sim
