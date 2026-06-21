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
    double char_len_mm = 0.0;    // characteristic element length used
    int n_nodes = 0;
    int n_elems = 0;
    int nodes_per_elem = 4;      // 4 (order 1, tet4) or 10 (order 2, tet10)
    double min_tet_volume_mm3 = 0.0;
    double max_tet_edge_ratio = 0.0;  // max longest-edge / shortest-edge over corner tets
    bool ok = false;
    std::string error;  // non-empty when ok==false
};

struct MeshOptions {
    double density = 0.5;          // clipped to [0,1] when target_size_mm is not set
    int order = 1;                 // 1=tet4, 2=tet10
    double target_size_mm = 0.0;   // >0 overrides density-derived char_len
    double min_size_mm = 0.0;      // >0 overrides char_len*0.1
    double max_size_mm = 0.0;      // >0 overrides char_len
    bool optimize = false;         // gmsh post-generation optimizer
    int algorithm3d = 1;           // gmsh Mesh.Algorithm3D; 1=Delaunay
};

// Mesh a STEP file into tets, also writing the .msh (next to the STEP, .step -> .msh)
// exactly like the Python oracle. density is clipped to [0,1];
// char_len = min_nonzero_bbox_dim * (0.25 - 0.20*density). Never throws: any gmsh
// failure returns ok=false with an error string (gmsh::finalize always runs).
//
// order==1 (default): first-order 4-node tets (MSH type 4); BYTE-IDENTICAL to the
//   prior behaviour — existing callers and the Python differential parity gate are
//   unaffected.
// order==2: second-order 10-node tets (MSH type 11). gmsh node ordering for tet10:
//   corners 0..3, then mid-edge nodes on edges
//   {0,1},{1,2},{2,0},{3,0},{3,2},{3,1} -> tet[4..9]. out.tets holds 10 indices/elem.
MeshOut mesh_step(const std::string& step_path, double density, int order = 1);
MeshOut mesh_step(const std::string& step_path, const MeshOptions& options);

}  // namespace aicad::sim
