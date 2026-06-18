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
    std::vector<int> tets;       // m*4 flat, row-major, 0-based node indices (tet4)
    double char_len_mm = 0.0;    // characteristic element length used
    int n_nodes = 0;
    int n_elems = 0;
    bool ok = false;
    std::string error;  // non-empty when ok==false
};

// Mesh a STEP file into first-order tets, also writing the .msh (next to the STEP,
// .step -> .msh) exactly like the Python oracle. density is clipped to [0,1];
// char_len = min_nonzero_bbox_dim * (0.25 - 0.20*density). Never throws: any gmsh
// failure returns ok=false with an error string (gmsh::finalize always runs).
MeshOut mesh_step(const std::string& step_path, double density);

}  // namespace aicad::sim
