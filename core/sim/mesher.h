// Tet mesher — C++ port of engine/sim/mesher.py (the gmsh tet4 mesher).
//
// Wraps the SAME libgmsh 4.15.2 the Python oracle uses (via the gmsh C++ SDK),
// running the identical option sequence + characteristic-length math so the tet
// mesh is BIT-IDENTICAL to engine.sim.mesher.mesh_step. This unblocks pure-C++ FEA
// (mesh a STEP -> feed fea_static/fea_modal without round-tripping to Python).
//
// This header is intentionally gmsh-FREE (gmsh.h is included only in mesher.cpp),
// so it is always includable regardless of the AICAD_WITH_OCCT / gmsh-link config.
//
// LICENSE NOTE: gmsh is GPL-2+; the implementation (mesher.cpp) is compiled only
// under AICAD_WITH_OCCT, mirroring engine/sim's confinement of gmsh.
#pragma once

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
    // concentrations: gmsh refines around holes/fillets to hit ~curv_elems_per_circle
    // elements per full 2*pi of curvature. Off => uniform sizing.
    bool curvature = true;
    double curv_elems_per_circle = 12.0;  // typical 8..20

    double target_size_mm = 0.0;   // >0 hard override of the derived base size
    double min_size_mm = 0.0;      // >0 overrides base*0.1
    double max_size_mm = 0.0;      // >0 overrides base
    bool optimize = true;          // run gmsh + Netgen optimizers for element quality
    int algorithm3d = 1;           // gmsh Mesh.Algorithm3D; 1=Delaunay, 10=HXT

    // Structured hexahedral meshing: when true, mesh_step delegates to mesh_box_hex,
    // which produces a regular nx*ny*nz grid of 8-node hexes for a box/rectangular-prism
    // part (nodes_per_elem==8). Non-mappable geometry returns ok=false with a message.
    bool structured = false;
};

// Aspect-ratio threshold above which an element is counted "poor" (SolidWorks-style).
inline constexpr double kPoorAspect = 10.0;

// Mesh a STEP file into tets, also writing the .msh next to the STEP (.step -> .msh).
// Base element size is derived from the bounding-box diagonal and the `fineness`
// control; curvature-adaptive refinement and Netgen optimization are applied per the
// options. Never throws: any gmsh failure returns ok=false with an error string
// (gmshFinalize always runs).
//
// order==1 (default): first-order 4-node tets (MSH type 4).
// order==2: second-order 10-node tets (MSH type 11). gmsh node ordering for tet10:
//   corners 0..3, then mid-edge nodes on edges
//   {0,1},{1,2},{2,0},{3,0},{3,2},{3,1} -> tet[4..9]. out.tets holds 10 indices/elem.
//   Mid-side nodes are snapped to the CAD surface (curved elements) for accurate stress
//   recovery on fillets/holes.
//
// The (density, order) overload is kept for back-compat: density in [0,1] is mapped to
// fineness with the same "higher = finer" sense.
MeshOut mesh_step(const std::string& step_path, double density, int order = 1);
MeshOut mesh_step(const std::string& step_path, const MeshOptions& options);

}  // namespace aicad::sim
